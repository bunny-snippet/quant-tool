import logging
from datetime import timedelta
from django.db import transaction
from django.utils import timezone

from vendors.models import ClientIntegration

from .models import Survey, SurveyQuota, SyncRun, TolunaNotification
from .providers import ProviderError, get_provider


logger = logging.getLogger(__name__)


# Keep each CASE-based statement comfortably below common database packet and
# parameter limits.  A Toluna inventory can contain thousands of JSON-heavy
# rows, so one unbounded statement would merely replace many small writes with
# one risky oversized write.
_PROVIDER_BULK_UPDATE_BATCH_SIZE = 100
_SURVEY_CONCRETE_UPDATE_FIELDS = frozenset(
    field.name
    for field in Survey._meta.concrete_fields
    if not field.primary_key
)


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
        provider = get_provider(integration)
        result = provider.test_connection()
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
        sync_interval_seconds=max(
            int(integration.sync_interval_seconds or 60),
            int(provider.minimum_sync_interval_seconds or 60),
        ),
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


def _sync_toluna_quota_snapshots_bulk(candidates) -> dict[int, bool]:
    """Refresh numeric quota snapshots in bounded batches.

    The old merge issued one ``UPDATE`` for every quota in every changed
    survey.  Load and lock the same derived rows once, apply the same values in
    memory, and let ``bulk_update`` emit bounded CASE statements.  The boolean
    result retains the old fail-closed contract: every provider quota ID must
    already have at least one derived row or the survey detail is marked stale.
    """

    prepared = []
    survey_ids = set()
    quota_ids = set()
    for survey, raw_data in candidates:
        quota_payloads = []
        for quota in _toluna_value(raw_data or {}, "Quotas", default=[]) or []:
            quota_id = _toluna_integer(_toluna_value(quota, "QuotaID"))
            if quota_id < 0:
                continue
            quota_payloads.append((quota_id, quota))
            quota_ids.add(quota_id)
        prepared.append((survey, quota_payloads))
        survey_ids.add(survey.pk)

    rows_by_key = {}
    if survey_ids and quota_ids:
        quota_rows = SurveyQuota.objects.select_for_update().filter(
            survey_id__in=survey_ids,
            quota_id__in=quota_ids,
        )
        for quota_row in quota_rows:
            rows_by_key.setdefault(
                (quota_row.survey_id, quota_row.quota_id), []
            ).append(quota_row)

    updates_by_pk = {}
    results = {}
    for survey, quota_payloads in prepared:
        expected_ids = set()
        updated_ids = set()
        for quota_id, quota in quota_payloads:
            expected_ids.add(quota_id)
            matching_rows = rows_by_key.get((survey.pk, quota_id), ())
            if matching_rows:
                updated_ids.add(quota_id)
            target = max(
                0,
                _toluna_integer(
                    _toluna_value(quota, "CompletesRequired"), 0
                ),
            )
            remaining = max(
                0,
                _toluna_integer(
                    _toluna_value(quota, "EstimatedCompletesRemaining"), 0
                ),
            )
            updated_at = timezone.now()
            for quota_row in matching_rows:
                quota_row.sample_size = target
                quota_row.completes = max(0, target - remaining)
                quota_row.remaining = remaining
                quota_row.status = "Open" if remaining > 0 else "Full"
                quota_row.raw_data = quota
                quota_row.targeting = {
                    "layers": _toluna_value(
                        quota, "Layers", default=[]
                    ) or []
                }
                quota_row.updated_at = updated_at
                # Duplicate provider IDs retain the old last-write-wins
                # behavior while each physical derived row is written once.
                updates_by_pk[quota_row.pk] = quota_row
        results[survey.pk] = expected_ids.issubset(updated_ids)

    if updates_by_pk:
        SurveyQuota.objects.bulk_update(
            list(updates_by_pk.values()),
            [
                "sample_size",
                "completes",
                "remaining",
                "status",
                "raw_data",
                "targeting",
                "updated_at",
            ],
            batch_size=_PROVIDER_BULK_UPDATE_BATCH_SIZE,
        )
    return results


def sync_client_integration(integration: ClientIntegration, *, refresh_details=False) -> SyncRun:
    """Synchronize one verified provider connection into its owning client."""
    if not integration.is_active:
        raise ProviderError("This client integration is inactive.")
    provider = get_provider(integration)
    now = timezone.now()
    # Touched rows must sort strictly after the close boundary. Some host
    # clocks expose timestamps at a coarser resolution than Python's datetime,
    # so a pre-existing missing row can legitimately equal ``now``. A separate
    # marker lets the close query include that equality without ever closing a
    # row merged by this snapshot.
    snapshot_marker = now + timedelta(microseconds=1)
    run = SyncRun.objects.create(integration=integration)
    touched = []
    try:
        inventory = provider.inventory()
        run.provider_cache_expires_at = getattr(
            provider, "inventory_cache_expires_at", None
        )
        run.fetched_full = len(inventory)
        normalized_rows = {}
        for payload in inventory:
            normalized = provider.normalize_inventory_item(payload, now)
            normalized_rows[normalized.source_key] = normalized
        run.unique_surveys = len(normalized_rows)

        with transaction.atomic():
            existing_surveys = {
                survey.source_key: survey
                for survey in Survey.objects.select_for_update().filter(
                    integration=integration,
                    source_key__in=normalized_rows,
                )
            }
            unchanged_ids = []
            changed_surveys = []
            changed_survey_fields = set()
            toluna_quota_candidates = []
            for source_key, normalized in normalized_rows.items():
                survey = existing_surveys.get(source_key)
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
                    # The merge owns one snapshot marker.  Provider adapters
                    # cannot accidentally supply a different timestamp, and
                    # missing live rows can be closed with an indexed range
                    # instead of a potentially huge NOT IN source-key list.
                    "last_seen_at": snapshot_marker,
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
                        changed_survey_fields.add("detail_synced_at")
                    # ``bulk_update`` does not invoke ``auto_now``.  Preserve
                    # the previous save() timestamp semantics explicitly.
                    survey.updated_at = timezone.now()
                    changed_surveys.append(survey)
                    changed_survey_fields.update(values)
                    changed_survey_fields.add("updated_at")
                    if is_toluna and not toluna_targeting_changed:
                        toluna_quota_candidates.append(
                            (survey, values.get("raw_data"))
                        )
                    run.updated += 1
                    touched.append(survey)
                else:
                    unchanged_ids.append(survey.pk)
                    run.unchanged += 1

            quota_snapshot_results = _sync_toluna_quota_snapshots_bulk(
                toluna_quota_candidates
            )
            for survey, _raw_data in toluna_quota_candidates:
                if quota_snapshot_results.get(survey.pk, False):
                    survey.quota_synced_at = now
                    changed_survey_fields.add("quota_synced_at")
                else:
                    # A supposedly hydrated survey is missing a derived quota
                    # row. Fail closed and rebuild it on the next detail pass
                    # instead of routing on partial data.
                    survey.detail_synced_at = None
                    changed_survey_fields.add("detail_synced_at")

            if changed_surveys:
                Survey.objects.bulk_update(
                    changed_surveys,
                    sorted(
                        changed_survey_fields
                        & _SURVEY_CONCRETE_UPDATE_FIELDS
                    ),
                    batch_size=_PROVIDER_BULK_UPDATE_BATCH_SIZE,
                )

            # One set-based heartbeat replaces an UPDATE per unchanged survey.
            # Do not touch updated_at: an identical provider row was observed,
            # but the survey itself was not modified.
            if unchanged_ids:
                Survey.objects.filter(pk__in=unchanged_ids).update(
                    last_seen_at=snapshot_marker
                )

            run.closed = Survey.objects.filter(
                integration=integration,
                status=Survey.Status.LIVE,
                # A newer concurrent snapshot must never be closed by this
                # older worker. Every row merged by this snapshot carries the
                # strictly newer ``snapshot_marker`` and therefore falls
                # outside this inclusive cutoff.
                last_seen_at__lte=now,
            ).update(status=Survey.Status.CLOSED, updated_at=now)

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
