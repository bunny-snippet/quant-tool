import logging
from django.db import transaction
from django.utils import timezone

from vendors.models import ClientIntegration

from .models import Survey, SurveyQuota, SyncRun, TolunaNotification
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


def _toluna_value(payload, *names, default=None):
    if not isinstance(payload, dict):
        return default
    lowered = {str(key).casefold(): value for key, value in payload.items()}
    for name in names:
        if str(name).casefold() in lowered:
            return lowered[str(name).casefold()]
    return default


def _toluna_integer(value, default=-1):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _toluna_answer_values(payload):
    values = _toluna_value(payload, "AnswerValues", default=[]) or []
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    return tuple(sorted({
        item.strip()
        for value in values
        for item in str(value).split(",")
        if item.strip()
    }))


def _toluna_targeting_contract(raw_data):
    """Return only the quota fields that can change prescreener eligibility.

    Completion counters move continuously and must update capacity without
    invalidating a form that the respondent is currently submitting.
    """

    quota_contracts = []
    for quota in _toluna_value(raw_data or {}, "Quotas", default=[]) or []:
        layer_contracts = []
        for layer in _toluna_value(quota, "Layers", default=[]) or []:
            subquota_contracts = []
            for subquota in _toluna_value(layer, "SubQuotas", default=[]) or []:
                conditions = []
                for condition in _toluna_value(
                    subquota, "QuestionsAndAnswers", default=[]
                ) or []:
                    conditions.append((
                        _toluna_integer(_toluna_value(condition, "QuestionID")),
                        bool(_toluna_value(condition, "IsRoutable", default=False)),
                        tuple(sorted({
                            _toluna_integer(value)
                            for value in (
                                _toluna_value(condition, "AnswerIDs", default=[]) or []
                            )
                        })),
                        _toluna_answer_values(condition),
                    ))
                subquota_contracts.append((
                    _toluna_integer(_toluna_value(subquota, "SubQuotaID")),
                    tuple(sorted(conditions, key=repr)),
                ))
            layer_contracts.append((
                _toluna_integer(_toluna_value(layer, "LayerID")),
                tuple(sorted(subquota_contracts, key=repr)),
            ))
        quota_contracts.append((
            _toluna_integer(_toluna_value(quota, "QuotaID")),
            tuple(sorted(layer_contracts, key=repr)),
        ))
    return tuple(sorted(quota_contracts, key=repr))


def _sync_toluna_quota_snapshots(survey: Survey, raw_data) -> bool:
    """Refresh numeric capacity/raw snapshots without rebuilding questions."""

    expected_ids = set()
    updated_ids = set()
    for quota in _toluna_value(raw_data or {}, "Quotas", default=[]) or []:
        quota_id = _toluna_integer(_toluna_value(quota, "QuotaID"))
        if quota_id < 0:
            continue
        expected_ids.add(quota_id)
        target = max(0, _toluna_integer(_toluna_value(quota, "CompletesRequired"), 0))
        remaining = max(
            0,
            _toluna_integer(
                _toluna_value(quota, "EstimatedCompletesRemaining"), 0
            ),
        )
        updated = SurveyQuota.objects.filter(
            survey=survey,
            quota_id=quota_id,
        ).update(
            sample_size=target,
            completes=max(0, target - remaining),
            remaining=remaining,
            status="Open" if remaining > 0 else "Full",
            raw_data=quota,
            targeting={
                "layers": _toluna_value(quota, "Layers", default=[]) or []
            },
            updated_at=timezone.now(),
        )
        if updated:
            updated_ids.add(quota_id)
    return expected_ids.issubset(updated_ids)


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
                survey = Survey.objects.select_for_update().filter(
                    integration=integration,
                    source_key=source_key,
                ).first()
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
                    is_toluna = integration.provider_code == "toluna"
                    # Toluna counters change continuously but its survey-level
                    # modified timestamp is not dependable. Invalidate the
                    # rendered form only when the actual layer/subquota answer
                    # contract changes; capacity-only updates are applied to
                    # existing quota rows below without bouncing a respondent.
                    toluna_targeting_changed = bool(
                        is_toluna
                        and _toluna_targeting_contract(survey.raw_data)
                        != _toluna_targeting_contract(values.get("raw_data"))
                    )
                    for field, value in values.items():
                        setattr(survey, field, value)
                    if toluna_targeting_changed or (source_changed and not is_toluna):
                        survey.detail_synced_at = None
                    survey.save()
                    if is_toluna and not toluna_targeting_changed:
                        if _sync_toluna_quota_snapshots(survey, values.get("raw_data")):
                            survey.quota_synced_at = now
                            Survey.objects.filter(pk=survey.pk).update(quota_synced_at=now)
                        else:
                            # A supposedly hydrated survey is missing a derived
                            # quota row. Fail closed and rebuild it on the next
                            # detail pass instead of routing on partial data.
                            survey.detail_synced_at = None
                            Survey.objects.filter(pk=survey.pk).update(detail_synced_at=None)
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

        if integration.provider_code == "toluna":
            # Notifications can arrive just before the matching inventory row
            # commits. Reconcile the touched provider IDs in one query, and
            # replay the latest applied operational state because fresh
            # inventory capacity can otherwise reopen a provider-closed row.
            from .toluna_notifications import (
                reconcile_toluna_operational_notifications_for_surveys,
            )

            reconcile_toluna_operational_notifications_for_surveys(
                touched,
                include_applied_event_types={
                    TolunaNotification.EventType.QUOTA_STATUS,
                    TolunaNotification.EventType.SURVEY_CLOSED,
                },
                applied_since=now,
            )

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
                "fetched_full": run.fetched_full,
                "unique_surveys": run.unique_surveys,
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
