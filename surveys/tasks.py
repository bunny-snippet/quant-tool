import logging
import os
import shutil
from datetime import timedelta, timezone as dt_timezone
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.test import RequestFactory
from rest_framework.test import APIRequestFactory, force_authenticate

from vendors.models import ClientIntegration
from vendors.credentials import resolve_integration_token

from .integrations import InnovateMRAPIError, InnovateMRClient
from .models import ExportJob, Survey, SurveyAttempt, SyncLease
from .services import reconcile_attempt_status, replace_survey_details, sync_surveys
from .provider_services import refresh_client_integration_details, sync_client_integration
from .providers import installed_provider_codes
from .supplier_callbacks import (
    DELIVERY_AUDIT_KEY,
    SupplierCallbackRetryableError,
    deliver_supplier_result_callback,
)


logger = logging.getLogger(__name__)


def _internal_request_host():
    return next((host for host in settings.ALLOWED_HOSTS if host and host != "*"), "localhost")


def _queued_export_response(job):
    from .views import (
        SurveyAttemptViewSet,
        SurveyViewSet,
        prescreener_data_export,
        termination_reasons_export,
        user_dashboard_export,
    )
    query = {
        str(key): [str(item) for item in values]
        for key, values in (job.query or {}).items()
    }
    host = _internal_request_host()
    if job.kind == ExportJob.Kind.PANELIST:
        request = RequestFactory().get("/prescreened-data/export/", data=query, HTTP_HOST=host)
        request.user = job.requested_by
        return prescreener_data_export(request)
    if job.kind == ExportJob.Kind.TERMS:
        request = RequestFactory().get("/termination-reasons/export/", data=query, HTTP_HOST=host)
        request.user = job.requested_by
        return termination_reasons_export(request)
    if job.kind == ExportJob.Kind.USER_DASHBOARD:
        request = RequestFactory().get("/user-dashboard/export/", data=query, HTTP_HOST=host)
        request.user = job.requested_by
        return user_dashboard_export(request)
    factory = APIRequestFactory()
    if job.kind == ExportJob.Kind.PROJECTS:
        request = factory.get("/api/v1/surveys/export/", data=query, HTTP_HOST=host)
        force_authenticate(request, user=job.requested_by)
        return SurveyViewSet.as_view({"get": "export"})(request)
    if job.kind == ExportJob.Kind.TRAFFIC:
        request = factory.get("/api/v1/survey-attempts/export/", data=query, HTTP_HOST=host)
        force_authenticate(request, user=job.requested_by)
        return SurveyAttemptViewSet.as_view({"get": "export"})(request)
    raise ValueError(f"Unsupported export job kind: {job.kind}")


@shared_task(name="surveys.build_export_job", soft_time_limit=270, time_limit=300)
def build_export_job(public_id):
    try:
        job = ExportJob.objects.select_related("requested_by").get(public_id=public_id)
    except ExportJob.DoesNotExist:
        return {"status": "missing"}
    if job.expires_at <= timezone.now():
        return {"status": "expired"}
    updated = ExportJob.objects.filter(
        pk=job.pk, status=ExportJob.Status.QUEUED,
    ).update(status=ExportJob.Status.RUNNING, started_at=timezone.now(), error="")
    if not updated:
        return {"status": "already-processed"}
    job.refresh_from_db()
    try:
        response = _queued_export_response(job)
        if getattr(response, "status_code", 200) != 200:
            raise RuntimeError(f"Export builder returned HTTP {response.status_code}.")
        workbook = getattr(response, "_export_workbook", None)
        if workbook is None:
            raise RuntimeError("Export builder did not return a workbook.")
        filename = response.get("Content-Disposition", "").split("filename=")[-1].strip('"') or "export.xlsx"
        storage_key = job.storage_key or f"{job.public_id}.xlsx"
        directory = Path(settings.EXPORT_JOB_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f".{storage_key}.tmp-{os.getpid()}"
        destination = directory / storage_key
        workbook.seek(0)
        with temporary.open("wb") as output:
            shutil.copyfileobj(workbook, output, length=1024 * 1024)
        temporary.replace(destination)
        workbook.close()
        ExportJob.objects.filter(pk=job.pk).update(
            status=ExportJob.Status.COMPLETED, filename=filename[:255], storage_key=storage_key,
            finished_at=timezone.now(), error="",
        )
        return {"status": "completed", "id": str(job.public_id)}
    except Exception as exc:
        logger.exception("Export job %s failed", public_id)
        ExportJob.objects.filter(pk=job.pk).update(
            status=ExportJob.Status.FAILED, error=str(exc)[:500], finished_at=timezone.now(),
        )
        return {"status": "failed", "id": str(job.public_id)}


@shared_task(name="surveys.cleanup_expired_export_jobs")
def cleanup_expired_export_jobs():
    expired = ExportJob.objects.filter(expires_at__lte=timezone.now())
    keys = list(expired.exclude(storage_key="").values_list("storage_key", flat=True))
    expired.delete()
    directory = Path(settings.EXPORT_JOB_DIR)
    for key in keys:
        try:
            (directory / key).unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove expired export file %s", directory / key)
    return {"deleted": len(keys)}


PROVIDER_MINIMUM_SYNC_INTERVAL_SECONDS = {
    "innovatemr": 150,
    "rfg": 600,
    "toluna": 60,
    "track_opinion": 300,
    "acuity": 300,
    "unimarket": 300,
}


def effective_sync_interval_seconds(integration):
    """Return the slowest configured/provider-approved inventory interval.

    The integration value is operator-visible and must never be silently
    ignored. Provider defaults are additional safety floors, not overrides.
    """

    configured_default = {
        "innovatemr": settings.CLIENT_INTEGRATION_INNOVATEMR_SYNC_INTERVAL_SECONDS,
        "rfg": settings.CLIENT_INTEGRATION_RFG_SYNC_INTERVAL_SECONDS,
        "toluna": settings.CLIENT_INTEGRATION_TOLUNA_SYNC_INTERVAL_SECONDS,
        "track_opinion": 300,
        "acuity": 300,
        "unimarket": 300,
    }.get(integration.provider_code, 60)
    return max(
        60,
        int(integration.sync_interval_seconds or 60),
        int(configured_default or 60),
        PROVIDER_MINIMUM_SYNC_INTERVAL_SECONDS.get(integration.provider_code, 60),
    )


def _provider_cache_due_at(integration):
    """Return an upstream cache expiry stored by the last successful sync."""

    if integration.provider_code != "toluna":
        return None
    raw_value = (integration.last_sync_summary or {}).get(
        "provider_cache_expires_at"
    )
    cached_until = parse_datetime(str(raw_value or ""))
    if cached_until is None:
        return None
    if timezone.is_naive(cached_until):
        cached_until = timezone.make_aware(cached_until, dt_timezone.utc)
    return cached_until


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
        | Q(provider_code="toluna", last_test_status="success")
    ).only(
        "id", "provider_code", "sync_interval_seconds", "last_sync_started_at", "last_sync_status",
        "last_sync_summary", "credential_env_key", "encrypted_api_token",
    )
    for integration in integrations:
        if integration.provider_code in {"biobrain", "voqall"} and not resolve_integration_token(integration):
            continue
        interval_seconds = effective_sync_interval_seconds(integration)
        # Do not keep adding duplicate jobs while a previous sync is queued or
        # running. A genuinely stale marker is allowed through after the lease
        # window so a worker restart can recover automatically.
        active_sync_cutoff = now - timedelta(
            seconds=max(300, interval_seconds * 3)
        )
        if (
            integration.last_sync_status in {"queued", "running"}
            and integration.last_sync_started_at
            and integration.last_sync_started_at > active_sync_cutoff
        ):
            continue
        due_at = (integration.last_sync_started_at or (now - timedelta(days=1))) + timedelta(
            seconds=interval_seconds
        )
        provider_cache_due_at = _provider_cache_due_at(integration)
        if provider_cache_due_at is not None:
            due_at = max(due_at, provider_cache_due_at)
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
    if not SyncLease.acquire(
        lease_name, seconds=max(300, effective_sync_interval_seconds(integration))
    ):
        return {"status": "skipped", "reason": "previous integration sync is still running"}
    integration.last_sync_started_at = timezone.now()
    integration.last_sync_status = "running"
    integration.last_sync_error = ""
    integration.save(update_fields=["last_sync_started_at", "last_sync_status", "last_sync_error", "updated_at"])
    try:
        if integration.provider_code in installed_provider_codes():
            run = sync_client_integration(integration, refresh_details=False)
            details = refresh_client_integration_details(integration)
            summary = {
                "run_id": run.pk,
                "status": run.status,
                "fetched_full": run.fetched_full,
                "unique_surveys": run.unique_surveys,
                "created": run.created,
                "updated": run.updated,
                "unchanged": run.unchanged,
                "closed": run.closed,
                "details_refreshed": details["refreshed"],
                "detail_failures": details["failures"],
            }
            provider_cache_expires_at = getattr(
                run, "provider_cache_expires_at", None
            )
            if provider_cache_expires_at is not None:
                summary["provider_cache_expires_at"] = (
                    provider_cache_expires_at.isoformat()
                )
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
    ).exclude(survey__integration__provider_code__in=installed_provider_codes()).filter(
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


@shared_task(
    bind=True,
    name="surveys.deliver_supplier_result_callback",
    max_retries=5,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=45,
    time_limit=60,
)
def deliver_supplier_result_callback_task(self, attempt_id, event_id):
    """Send one persisted supplier result with bounded exponential retries."""

    try:
        return deliver_supplier_result_callback(attempt_id, event_id)
    except SupplierCallbackRetryableError as exc:
        countdown = min(300, 5 * (2 ** int(self.request.retries or 0)))
        raise self.retry(exc=exc, countdown=countdown)


@shared_task(name="surveys.dispatch_pending_supplier_callbacks")
def dispatch_pending_supplier_callbacks_task():
    """Recover callbacks stranded by broker outages or killed workers."""

    now = timezone.now()
    stale_before = now - timedelta(seconds=60)
    lookback = now - timedelta(hours=settings.SUPPLIER_CALLBACK_RECOVERY_LOOKBACK_HOURS)
    candidates = SurveyAttempt.objects.filter(
        status__in=(
            SurveyAttempt.Status.COMPLETED,
            SurveyAttempt.Status.TERMINATED,
            SurveyAttempt.Status.OVER_QUOTA,
            SurveyAttempt.Status.QUALITY_TERMINATED,
        ),
        callback_at__gte=lookback,
        upstream_transaction_data__supplier_callback_delivery__state__in=(
            "queued", "queue_failed", "delivering",
        ),
    ).only(
        "id", "upstream_transaction_data", "callback_at",
    ).order_by("-callback_at")[: settings.SUPPLIER_CALLBACK_RECOVERY_BATCH]
    queued = []
    failures = 0
    for attempt in candidates:
        audit = attempt.upstream_transaction_data
        record = audit.get(DELIVERY_AUDIT_KEY, {}) if isinstance(audit, dict) else {}
        event_id = str(record.get("event_id") or "")
        if not event_id:
            continue
        updated_at = parse_datetime(str(record.get("updated_at") or ""))
        if record.get("state") != "queue_failed" and updated_at and updated_at > stale_before:
            continue
        try:
            deliver_supplier_result_callback_task.delay(attempt.pk, event_id)
            queued.append(attempt.pk)
        except Exception as exc:  # pragma: no cover - environment-specific broker failures
            logger.error(
                "Could not recover supplier callback attempt_id=%s error_type=%s",
                attempt.pk,
                type(exc).__name__,
            )
            failures += 1
    return {"queued": queued, "count": len(queued), "failures": failures}
