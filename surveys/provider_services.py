import logging
from django.db import transaction
from django.utils import timezone

from vendors.models import ClientIntegration

from .models import Survey, SyncRun
from .providers import ProviderError, get_provider


logger = logging.getLogger(__name__)


def provider_preview(integration: ClientIntegration, limit: int = 10) -> dict:
    """Fetch a bounded, read-only inventory preview without changing local surveys."""
    provider = get_provider(integration)
    seen_at = timezone.now()
    rows = []
    inventory = provider.inventory()
    for payload in inventory[: max(1, min(int(limit), 25))]:
        normalized = provider.normalize_inventory_item(payload, seen_at)
        rows.append({
            "source_id": normalized.source_key,
            "name": normalized.values.get("name", ""),
            "country": normalized.values.get("country_code", ""),
            "cpi": normalized.values.get("cpi"),
            "loi": normalized.values.get("loi"),
            "status": normalized.values.get("status"),
            "modified_at": normalized.modified_at,
        })
    return {"total_received": len(inventory), "results": rows}


def test_provider_connection(integration: ClientIntegration) -> dict:
    now = timezone.now()
    try:
        result = get_provider(integration).test_connection()
    except Exception as exc:
        ClientIntegration.objects.filter(pk=integration.pk).update(
            last_tested_at=now,
            last_test_status="failed",
            last_test_error=str(exc)[:2000],
            scheduled_sync_enabled=False,
        )
        raise
    ClientIntegration.objects.filter(pk=integration.pk).update(
        last_tested_at=now,
        last_test_status="success",
        last_test_error="",
        scheduled_sync_enabled=True,
        sync_interval_seconds=60,
    )
    return result


def _survey_changed(survey: Survey, normalized) -> bool:
    if survey.raw_data != normalized.raw_data:
        return True
    return any(
        getattr(survey, field) != value
        for field, value in normalized.values.items()
        if field != "last_seen_at"
    )


def sync_client_integration(integration: ClientIntegration, *, refresh_details=False) -> SyncRun:
    """Synchronize one verified provider connection into its owning client."""
    if not integration.is_active:
        raise ProviderError("This client integration is inactive.")
    provider = get_provider(integration)
    now = timezone.now()
    run = SyncRun.objects.create(integration=integration)
    touched = []
    try:
        inventory = provider.inventory()
        run.fetched_full = len(inventory)
        normalized_rows = {}
        for payload in inventory:
            normalized = provider.normalize_inventory_item(payload, now)
            normalized_rows[normalized.source_key] = normalized
        run.unique_surveys = len(normalized_rows)

        with transaction.atomic():
            for source_key, normalized in normalized_rows.items():
                survey = Survey.objects.filter(integration=integration, source_key=source_key).first()
                # Some provider inventory APIs (including Toluna Get Quotas)
                # do not return created/updated timestamps. Persist stable local
                # fallbacks once so project sorting, date filters and the table
                # never receive null dates. Reuse them on later syncs; using
                # `now` every minute would incorrectly mark every survey as
                # updated on every poll.
                normalized.values["source_created_at"] = (
                    normalized.values.get("source_created_at")
                    or (survey.source_created_at if survey else None)
                    or (survey.created_at if survey else now)
                )
                normalized.values["source_modified_at"] = (
                    normalized.values.get("source_modified_at")
                    or (survey.source_modified_at if survey else None)
                    or (survey.updated_at if survey else normalized.values["source_created_at"])
                )
                values = {
                    **normalized.values,
                    "client": integration.client,
                    "integration": integration,
                    "source_key": source_key,
                    "source_id": normalized.numeric_source_id,
                }
                if survey is None:
                    survey = Survey.objects.create(**values)
                    run.created += 1
                    touched.append(survey)
                elif _survey_changed(survey, normalized):
                    source_changed = survey.source_modified_at != values["source_modified_at"]
                    for field, value in values.items():
                        setattr(survey, field, value)
                    if source_changed:
                        survey.detail_synced_at = None
                    survey.save()
                    run.updated += 1
                    touched.append(survey)
                else:
                    survey.last_seen_at = now
                    survey.integration = integration
                    survey.save(update_fields=["last_seen_at", "integration", "updated_at"])
                    run.unchanged += 1

            run.closed = Survey.objects.filter(
                integration=integration,
                status=Survey.Status.LIVE,
            ).exclude(source_key__in=normalized_rows).update(status=Survey.Status.CLOSED, updated_at=now)

        if refresh_details:
            detail_batch = int((integration.config or {}).get("detail_refresh_batch", integration.detail_refresh_batch))
            candidates = touched[: max(0, min(detail_batch, 50))]
            for survey in candidates:
                try:
                    provider.refresh_details(survey)
                except Exception:
                    run.detail_failures += 1
                    logger.exception("Provider detail refresh failed for integration=%s survey=%s", integration.pk, survey.pk)
        run.status = SyncRun.Status.PARTIAL if run.detail_failures else SyncRun.Status.SUCCESS
    except Exception as exc:
        run.status = SyncRun.Status.FAILED
        run.error = str(exc)[:10000]
        ClientIntegration.objects.filter(pk=integration.pk).update(last_test_error=str(exc)[:2000])
        logger.exception("Provider sync failed for integration=%s", integration.pk)
        raise
    finally:
        finished = timezone.now()
        run.finished_at = finished
        run.save()
        ClientIntegration.objects.filter(pk=integration.pk).update(
            last_sync_finished_at=finished,
            last_sync_status={
                SyncRun.Status.SUCCESS: "success",
                SyncRun.Status.PARTIAL: "partial",
                SyncRun.Status.FAILED: "failed",
            }.get(run.status, run.status),
            last_sync_error=run.error,
            last_sync_summary={
                "run_id": run.pk,
                "created": run.created,
                "updated": run.updated,
                "unchanged": run.unchanged,
                "closed": run.closed,
                "detail_failures": run.detail_failures,
            },
        )
    return run


def refresh_client_integration_details(integration: ClientIntegration, *, limit=None) -> dict:
    """Refresh changed provider targeting/link data outside the inventory transaction."""
    if not integration.is_active:
        raise ProviderError("This client integration is inactive.")
    provider = get_provider(integration)
    requested = limit if limit is not None else (integration.config or {}).get(
        "detail_refresh_batch", integration.detail_refresh_batch
    )
    batch = max(1, min(int(requested), 20))
    candidates = Survey.objects.filter(
        integration=integration,
        status=Survey.Status.LIVE,
    ).filter(
        detail_synced_at__isnull=True
    ).order_by("-source_modified_at", "pk")[:batch]
    refreshed = failures = 0
    for survey in candidates:
        try:
            provider.refresh_details(survey)
            refreshed += 1
        except Exception:
            failures += 1
            logger.exception("Provider detail refresh failed for integration=%s survey=%s", integration.pk, survey.pk)
    return {"refreshed": refreshed, "failures": failures}
