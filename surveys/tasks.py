from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from vendors.models import ClientIntegration
from vendors.credentials import resolve_integration_token

from .integrations import InnovateMRAPIError, InnovateMRClient
from .models import Survey, SurveyAttempt, SyncLease
from .services import reconcile_attempt_status, replace_survey_details, sync_surveys
from .provider_services import refresh_client_integration_details, sync_client_integration


def _stale_surveys(integration, limit):
    return Survey.objects.filter(integration=integration, status=Survey.Status.LIVE).filter(
        Q(quota_synced_at__isnull=True)
        | Q(targeting_synced_at__isnull=True)
        | Q(source_modified_at__isnull=False, quota_synced_at__lt=F("source_modified_at"))
        | Q(source_modified_at__isnull=False, targeting_synced_at__lt=F("source_modified_at"))
    ).order_by("detail_synced_at", "-source_modified_at")[:limit]


@shared_task(name="surveys.dispatch_due_integrations")
def dispatch_due_integrations_task():
    now = timezone.now()
    queued = []
    integrations = ClientIntegration.objects.filter(is_active=True).filter(
        Q(client__is_active=True) | Q(provider_code__in=("biobrain", "voqall")),
    ).filter(
        Q(scheduled_sync_enabled=True)
        | Q(provider_code="innovatemr")
        | Q(provider_code="rfg", last_test_status="success")
    ).only(
        "id", "provider_code", "sync_interval_seconds", "last_sync_started_at",
        "credential_env_key", "encrypted_api_token",
    )
    for integration in integrations:
        if integration.provider_code in {"biobrain", "voqall"} and not resolve_integration_token(integration):
            continue
        interval_seconds = {
            "innovatemr": settings.CLIENT_INTEGRATION_INNOVATEMR_SYNC_INTERVAL_SECONDS,
            "rfg": settings.CLIENT_INTEGRATION_RFG_SYNC_INTERVAL_SECONDS,
        }.get(integration.provider_code, integration.sync_interval_seconds)
        interval_seconds = max(60, interval_seconds)
        due_at = (integration.last_sync_started_at or (now - timedelta(days=1))) + timedelta(
            seconds=interval_seconds
        )
        if due_at <= now:
            ClientIntegration.objects.filter(pk=integration.pk).update(
                last_sync_started_at=now, last_sync_status="queued", last_sync_error="",
            )
            sync_client_integration_task.delay(integration.pk)
            queued.append(integration.pk)
    return {"queued": queued, "count": len(queued)}


@shared_task(name="surveys.sync_client_integration", autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def sync_client_integration_task(integration_id):
    integration = ClientIntegration.objects.select_related("client").get(pk=integration_id, is_active=True)
    lease_name = f"integration-{integration_id}-sync"
    if not SyncLease.acquire(lease_name, seconds=max(300, integration.sync_interval_seconds)):
        return {"status": "skipped", "reason": "previous integration sync is still running"}
    integration.last_sync_started_at = timezone.now()
    integration.last_sync_status = "running"
    integration.last_sync_error = ""
    integration.save(update_fields=["last_sync_started_at", "last_sync_status", "last_sync_error", "updated_at"])
    try:
        if integration.provider_code == "rfg":
            run = sync_client_integration(integration, refresh_details=False)
            details = refresh_client_integration_details(integration)
            summary = {
                "run_id": run.pk,
                "status": run.status,
                "created": run.created,
                "updated": run.updated,
                "unchanged": run.unchanged,
                "closed": run.closed,
                "details_refreshed": details["refreshed"],
                "detail_failures": details["failures"],
            }
            integration.last_sync_status = (
                "success" if run.status == "success" and not details["failures"] else "partial"
            )
            integration.last_sync_summary = summary
            return summary
        api = InnovateMRClient(integration=integration)
        summary = sync_surveys(api, integration=integration).__dict__
        inventory_count = sum(int(summary.get(key) or 0) for key in ("created", "updated", "unchanged"))
        if integration.provider_code in {"biobrain", "voqall"} and inventory_count > 0 and not integration.client.is_active:
            integration.client.is_active = True
            integration.client.save(update_fields=["is_active", "updated_at"])
        refreshed = failures = 0
        for survey in _stale_surveys(integration, integration.detail_refresh_batch):
            try:
                replace_survey_details(api, survey)
                refreshed += 1
            except Exception:
                failures += 1
        summary.update({"details_refreshed": refreshed, "detail_failures": failures})
        integration.last_sync_status = "success" if not failures else "partial"
        integration.last_sync_summary = summary
        return summary
    except Exception as exc:
        integration.last_sync_status = "failed"
        integration.last_sync_error = str(exc)[:10000]
        raise
    finally:
        integration.last_sync_finished_at = timezone.now()
        integration.save(update_fields=[
            "last_sync_finished_at", "last_sync_status", "last_sync_error", "last_sync_summary", "updated_at",
        ])
        SyncLease.release(lease_name)


@shared_task(name="surveys.sync_innovatemr_surveys")
def sync_innovatemr_surveys_task():
    integration = ClientIntegration.objects.filter(
        is_active=True, client__is_active=True, provider_code="innovatemr"
    ).order_by("id").first()
    if not integration:
        return {"status": "skipped", "reason": "no active integration"}
    return sync_client_integration_task(integration.pk)


@shared_task(name="surveys.refresh_stale_details")
def refresh_stale_details_task():
    integration = ClientIntegration.objects.filter(is_active=True, client__is_active=True).order_by("id").first()
    if not integration:
        return {"status": "skipped", "reason": "no active integration"}
    api = InnovateMRClient(integration=integration)
    refreshed = failures = 0
    for survey in _stale_surveys(integration, integration.detail_refresh_batch):
        try:
            replace_survey_details(api, survey)
            refreshed += 1
        except Exception:
            failures += 1
    return {"refreshed": refreshed, "failures": failures}


@shared_task(name="surveys.reconcile_pending_attempts")
def reconcile_pending_attempts_task():
    lease_name = "innovatemr-attempt-reconciliation"
    if not SyncLease.acquire(lease_name, seconds=300):
        return {"status": "skipped", "reason": "previous attempt reconciliation is still running"}
    now = timezone.now()
    retry_before = now - timedelta(seconds=settings.INNOVATEMR_ATTEMPT_RECONCILE_INTERVAL_SECONDS)
    lookback = now - timedelta(hours=settings.INNOVATEMR_ATTEMPT_RECONCILE_LOOKBACK_HOURS)
    pending = SurveyAttempt.objects.select_related("survey__integration").filter(
        status=SurveyAttempt.Status.REDIRECTED, callback_at__isnull=True, initiated_at__gte=lookback,
    ).exclude(survey__integration__provider_code="rfg").filter(
        Q(upstream_checked_at__isnull=True) | Q(upstream_checked_at__lte=retry_before)
    ).order_by(
        "upstream_checked_at", "-initiated_at"
    )[: settings.INNOVATEMR_ATTEMPT_RECONCILE_BATCH]
    clients = {}
    checked = terminal = failures = 0
    try:
        for attempt in pending:
            try:
                integration = attempt.survey.integration
                client = clients.setdefault(integration.pk if integration else None, InnovateMRClient(integration=integration))
                terminal += int(reconcile_attempt_status(client, attempt))
                checked += 1
            except InnovateMRAPIError:
                SurveyAttempt.objects.filter(pk=attempt.pk).update(upstream_checked_at=now)
                failures += 1
        return {"checked": checked, "terminal": terminal, "failures": failures}
    finally:
        SyncLease.release(lease_name)
