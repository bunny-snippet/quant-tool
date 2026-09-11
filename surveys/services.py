import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser
from django.conf import settings
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from vendors.models import ClientIntegration

from .integrations import (
    BIOBRAIN_DETAIL_ADAPTER_VERSION,
    InnovateMRAPIError,
    InnovateMRClient,
    InnovateMRNotFound,
)
from .models import LocalIdSequence, Survey, SurveyAttempt, SurveyQuota, SyncRun, TargetingQuestion
from .project_cache import invalidate_project_cache
from .survey_flow import normalize_client_ip

logger = logging.getLogger(__name__)
INNOVATEMR_TIMEZONE = ZoneInfo("America/Los_Angeles")


def _inventory_write_batch_size() -> int:
    """Keep inventory transactions and ``IN`` clauses bounded."""

    return max(
        100,
        min(
            int(getattr(settings, "INNOVATEMR_INVENTORY_WRITE_BATCH_SIZE", 1000)),
            5000,
        ),
    )


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _stable_question_id(item: dict[str, Any]) -> int:
    parsed = _integer(item.get("QuestionId"), -1)
    if parsed >= 0:
        return parsed
    digest = hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()
    return -int(digest[:12], 16)


def _biobrain_language_id(survey: Survey):
    pending = [survey.raw_data or {}]
    accepted = {"languageid", "language_id"}
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).replace("-", "_").lower() in accepted and nested not in (None, ""):
                    return nested
                if isinstance(nested, (dict, list)):
                    pending.append(nested)
        elif isinstance(value, list):
            pending.extend(item for item in value if isinstance(item, (dict, list)))
    return None


def parse_upstream_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = date_parser.parse(
            str(value),
            fuzzy=True,
            tzinfos={
                "PST": INNOVATEMR_TIMEZONE,
                "PDT": INNOVATEMR_TIMEZONE,
                "UTC": dt_timezone.utc,
                "GMT": dt_timezone.utc,
            },
        )
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, INNOVATEMR_TIMEZONE)
        return parsed.astimezone(dt_timezone.utc)
    except (ValueError, TypeError, OverflowError):
        logger.warning("Could not parse InnovateMR datetime %r", value)
        return None


def payload_modified_at(payload: dict[str, Any]) -> datetime:
    return parse_upstream_datetime(payload.get("modifiedDate")) or parse_upstream_datetime(payload.get("createdDate")) or datetime.min.replace(tzinfo=dt_timezone.utc)


def merge_inventory(*inventories: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Deduplicate by surveyId; latest modifiedDate wins (later source wins ties)."""
    merged: dict[int, dict[str, Any]] = {}
    for inventory in inventories:
        for payload in inventory:
            source_id = _integer(payload.get("surveyId"), -1)
            if source_id < 0:
                logger.warning("Ignoring survey payload without a valid surveyId")
                continue
            current = merged.get(source_id)
            if current is None or payload_modified_at(payload) >= payload_modified_at(current):
                merged[source_id] = payload
    return merged


def _survey_values(
    payload: dict[str, Any],
    seen_at: datetime,
    *,
    end_date_controls_status: bool = False,
) -> dict[str, Any]:
    group_type = str(payload.get("groupType") or payload.get("surveyType") or "").strip()
    normalized_group = "".join(character for character in group_type.upper() if character.isalnum())
    if normalized_group in {"B2B", "BUSINESS", "BUSINESSTOBUSINESS"}:
        survey_type = "B2B"
    elif normalized_group in {"B2C", "CONSUMER", "BUSINESSTOCONSUMER"}:
        survey_type = "B2C"
    else:
        survey_type = group_type[:20]
    source_end_at = parse_upstream_datetime(payload.get("endDate") or payload.get("EndDate"))
    status = (
        Survey.Status.CLOSED
        if end_date_controls_status and source_end_at is not None and source_end_at <= timezone.now()
        else Survey.Status.LIVE
    )
    return {
        "company_name": str(payload.get("_provider_name") or "InnovateMR"),
        "name": str(payload.get("surveyName") or ""),
        "status": status,
        "sample_size": max(0, _integer(payload.get("N"))),
        "completes": max(0, _integer(payload.get("supCmps"))),
        "remaining": max(0, _integer(payload.get("remainingN"))),
        "starts": max(0, _integer(payload.get("numberOfStarts"))),
        "cpi": _decimal(payload.get("CPI")),
        "loi": max(0, _integer(payload.get("LOI"))) if payload.get("LOI") is not None else None,
        "incidence_rate": _decimal(payload.get("IR")),
        "country": str(payload.get("Country") or ""),
        "country_code": str(payload.get("CountryCode") or "").upper(),
        "language": str(payload.get("Language") or ""),
        "language_code": str(payload.get("LanguageCode") or "").upper(),
        "group_type": group_type,
        "buyer_id": str(payload.get("BuyerId") or payload.get("buyerId") or "").strip(),
        "survey_type": survey_type,
        "device_type": str(payload.get("deviceType") or ""),
        "entry_link": str(payload.get("entryLink") or ""),
        "test_entry_link": str(payload.get("testEntryLink") or ""),
        "job_category": str(payload.get("jobCategory") or ""),
        "has_quota": bool(payload.get("isQuota")),
        "is_pii_required": bool(payload.get("isPIIRequired")),
        "is_recontact": bool(payload.get("reContact")),
        "source_created_at": parse_upstream_datetime(payload.get("createdDate")),
        "source_end_at": source_end_at,
        "source_modified_at": parse_upstream_datetime(payload.get("modifiedDate")),
        "last_seen_at": seen_at,
        "raw_data": payload,
    }


def _detail_changed(
    existing: Survey,
    incoming: dict[str, Any],
    normalized_values: dict[str, Any] | None = None,
) -> bool:
    normalized_values = normalized_values or {}
    incoming_modified = (
        normalized_values.get("source_modified_at")
        or normalized_values.get("source_created_at")
        or payload_modified_at(incoming)
    )
    existing_modified = existing.source_modified_at or existing.source_created_at or datetime.min.replace(tzinfo=dt_timezone.utc)
    if incoming_modified > existing_modified:
        return True
    # JSONField deserializes objects into ordinary Python structures, whose
    # equality is independent of dictionary key order.  Avoid serializing two
    # large payloads for every unchanged survey in a 90k+ item inventory.
    desired_status = normalized_values.get("status", Survey.Status.LIVE)
    return (
        existing.raw_data != incoming
        or existing.status != desired_status
        or existing.source_end_at != normalized_values.get("source_end_at")
    )


def replace_survey_quotas(client: InnovateMRClient, survey: Survey) -> None:
    try:
        request_options = {}
        if getattr(client, "is_biobrain", False):
            request_options["language_id"] = _biobrain_language_id(survey)
        quotas = client.get_quota_for_survey(survey.source_id, **request_options)
    except InnovateMRNotFound:
        quotas = []
    with transaction.atomic():
        survey.quotas.all().delete()
        SurveyQuota.objects.bulk_create([
            SurveyQuota(
                survey=survey,
                source_key=str(item.get("_id") or item.get("id") or hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()),
                quota_id=_integer(item.get("id"), 0) or None,
                title=str(item.get("title") or ""),
                name=str(item.get("quotaName") or ""),
                sample_size=max(0, _integer(item.get("quotaN"))),
                remaining=max(0, _integer(item.get("RemainingN"))),
                completes=max(0, _integer(item.get("cmp"))),
                clicks=max(0, _integer(item.get("clk"))),
                status=str(item.get("quotaStatus") or ""),
                targeting=item.get("targeting") if isinstance(item.get("targeting"), dict) else {},
                raw_data=item,
            )
            for item in quotas
        ])
        survey.quota_synced_at = timezone.now()
        survey.save(update_fields=["quota_synced_at", "updated_at"])


def replace_survey_targeting(client: InnovateMRClient, survey: Survey) -> None:
    try:
        request_options = {}
        if getattr(client, "is_biobrain", False):
            request_options["language_id"] = _biobrain_language_id(survey)
        targeting = client.get_survey_targeting(survey.source_id, **request_options)
    except InnovateMRNotFound:
        targeting = []
    with transaction.atomic():
        survey.targeting_questions.all().delete()
        TargetingQuestion.objects.bulk_create([
            TargetingQuestion(
                survey=survey,
                question_id=_stable_question_id(item),
                key=str(item.get("QuestionKey") or ""),
                text=str(item.get("QuestionText") or ""),
                question_type=str(item.get("QuestionType") or ""),
                category=str(item.get("QuestionCategory") or ""),
                options=item.get("Options") if isinstance(item.get("Options"), list) else [],
                raw_data=item,
            )
            for item in targeting
        ])
        survey.targeting_synced_at = timezone.now()
        survey.save(update_fields=["targeting_synced_at", "updated_at"])


def replace_survey_details(client: InnovateMRClient, survey: Survey) -> None:
    """Refresh both collections independently, preserving any successful side."""
    errors: list[InnovateMRAPIError] = []
    for refresh in (replace_survey_quotas, replace_survey_targeting):
        try:
            refresh(client, survey)
        except InnovateMRAPIError as exc:
            errors.append(exc)
    survey.refresh_from_db(fields=["quota_synced_at", "targeting_synced_at"])
    if survey.quota_synced_at and survey.targeting_synced_at:
        survey.detail_synced_at = min(survey.quota_synced_at, survey.targeting_synced_at)
        survey.save(update_fields=["detail_synced_at", "updated_at"])
    if errors:
        raise errors[0]


def _transaction_status(value: Any) -> str | None:
    normalized = "".join(character for character in str(value or "").lower() if character.isalnum())
    if normalized in {"1", "complete", "completed", "success", "qualified"}:
        return SurveyAttempt.Status.COMPLETED
    if normalized in {"4", "8"} or "quality" in normalized or "qualterm" in normalized:
        return SurveyAttempt.Status.QUALITY_TERMINATED
    if normalized in {"3", "7"} or "quota" in normalized:
        return SurveyAttempt.Status.OVER_QUOTA
    if normalized in {"2", "5"} or "fail" in normalized or "term" in normalized:
        return SurveyAttempt.Status.TERMINATED
    return None


def _attempt_transaction(attempt: SurveyAttempt, rows: list[dict[str, Any]]) -> dict[str, Any]:
    for row in rows:
        identifiers = {str(row.get(key) or "") for key in ("trackId", "PID", "pid")}
        if attempt.rid in identifiers:
            return row
    if not rows:
        return {}
    return max(
        rows,
        key=lambda row: parse_upstream_datetime(
            row.get("completeDateTime") or row.get("st_date_time") or row.get("clkDateTime")
        ) or datetime.min.replace(tzinfo=dt_timezone.utc),
    )


def reconcile_attempt_status(client: InnovateMRClient, attempt: SurveyAttempt) -> bool:
    """Reconcile one redirected attempt when legacy redirect URLs bypass our callback."""
    rows = client.get_survey_transactions_by_pid(attempt.survey.source_id, attempt.rid)
    upstream = _attempt_transaction(attempt, rows)
    checked_at = timezone.now()
    terminal_status = _transaction_status(upstream.get("status")) if upstream else None

    with transaction.atomic():
        locked = SurveyAttempt.objects.select_for_update().get(pk=attempt.pk)
        locked.upstream_checked_at = checked_at
        locked.upstream_transaction_data = upstream
        update_fields = ["upstream_checked_at", "upstream_transaction_data", "updated_at"]

        upstream_ip = normalize_client_ip(upstream.get("ip")) if upstream else None
        if upstream_ip and not normalize_client_ip(locked.initiation_ip):
            locked.initiation_ip = upstream_ip
            update_fields.append("initiation_ip")

        if terminal_status and locked.callback_at is None and locked.status in {
            SurveyAttempt.Status.INITIATED,
            SurveyAttempt.Status.REDIRECTED,
        }:
            completed_at = parse_upstream_datetime(
                upstream.get("completeDateTime") or upstream.get("st_date_time")
            ) or checked_at
            if completed_at < locked.loi_started_at:
                completed_at = checked_at
            locked.status = terminal_status
            locked.status_source = "innovatemr_transaction"
            locked.callback_at = completed_at
            locked.last_callback_at = completed_at
            locked.callback_ip = upstream_ip
            locked.loi_seconds = locked.calculate_loi_seconds(completed_at)
            locked.is_verified = str(upstream.get("verifyToken") or "").lower() == "valid"
            update_fields.extend([
                "status", "status_source", "callback_at", "last_callback_at", "callback_ip", "loi_seconds",
                "is_verified",
            ])

        locked.save(update_fields=list(dict.fromkeys(update_fields)))
        if terminal_status:
            from vendors.services import finalize_attempt_capacity

            finalize_attempt_capacity(locked)
    return bool(terminal_status)


@dataclass
class SyncSummary:
    run_id: int
    status: str
    created: int
    updated: int
    unchanged: int
    closed: int
    detail_failures: int


def sync_surveys(client: InnovateMRClient | None = None, integration: ClientIntegration | None = None) -> SyncSummary:
    if integration is None:
        integration = ClientIntegration.objects.filter(is_active=True, client__is_active=True).order_by("id").first()
    client = client or InnovateMRClient(integration=integration)
    run = SyncRun.objects.create(integration=integration)
    now = timezone.now()
    latest_marker = Survey.objects.filter(integration=integration).aggregate(
        value=Max("last_seen_at")
    )["value"]
    snapshot_marker = now.replace(microsecond=0)
    if latest_marker is not None:
        latest_marker = latest_marker.replace(microsecond=0)
        if snapshot_marker <= latest_marker:
            snapshot_marker = latest_marker + timedelta(seconds=1)

    try:
        full_inventory = client.get_allocated_surveys()
        paged = client.get_allocated_surveys_paged()
        merged = merge_inventory(full_inventory, paged.surveys)
        run.fetched_full = len(full_inventory)
        run.fetched_paged = len(paged.surveys)
        run.unique_surveys = len(merged)

        source_client = integration.client if integration else None
        merged_items = list(merged.items())
        write_batch_size = _inventory_write_batch_size()
        update_fields = [
            "source_id",
            "source_key",
            "client",
            "integration",
            "company_name",
            "name",
            "status",
            "sample_size",
            "completes",
            "remaining",
            "starts",
            "cpi",
            "loi",
            "incidence_rate",
            "country",
            "country_code",
            "language",
            "language_code",
            "group_type",
            "buyer_id",
            "survey_type",
            "device_type",
            "entry_link",
            "test_entry_link",
            "job_category",
            "has_quota",
            "is_pii_required",
            "is_recontact",
            "source_created_at",
            "source_end_at",
            "source_modified_at",
            "last_seen_at",
            "detail_synced_at",
            "quota_synced_at",
            "targeting_synced_at",
            "raw_data",
            "updated_at",
        ]

        # Process bounded batches so the recurring 90k+ Innovate inventory
        # never holds one transaction (or one row lock) for the whole sync.
        # Each batch performs one preload query and a bounded number of bulk
        # writes instead of a SELECT + UPDATE for every survey.
        for offset in range(0, len(merged_items), write_batch_size):
            batch = merged_items[offset:offset + write_batch_size]
            source_keys = [str(source_id) for source_id, _payload in batch]
            existing_query = Survey.objects.filter(source_key__in=source_keys)
            existing_query = (
                existing_query.filter(integration=integration)
                if integration
                else existing_query.filter(integration__isnull=True)
            )
            existing_by_key = {
                survey.source_key: survey
                for survey in existing_query
            }
            pending_creates: list[Survey] = []
            pending_updates: list[Survey] = []
            unchanged_ids: list[int] = []

            for source_id, payload in batch:
                source_key = str(source_id)
                existing = existing_by_key.get(source_key)
                values = _survey_values(
                    payload,
                    snapshot_marker,
                    end_date_controls_status=getattr(client, "is_biobrain", False),
                )
                values["client"] = source_client
                values["integration"] = integration
                values["source_key"] = source_key
                if (
                    existing is not None
                    and getattr(client, "is_biobrain", False)
                    and int((existing.raw_data or {}).get("_biobrain_detail_adapter_version") or 0)
                    < BIOBRAIN_DETAIL_ADAPTER_VERSION
                ):
                    # One-time, non-destructive rehydration: inventory and all
                    # respondent traffic remain untouched while the bounded
                    # detail worker replaces only raw-ID targeting/quota rows.
                    values["detail_synced_at"] = None
                    values["quota_synced_at"] = None
                    values["targeting_synced_at"] = None
                if existing is None:
                    pending_creates.append(Survey(source_id=source_id, **values))
                    run.created += 1
                elif _detail_changed(existing, payload, values):
                    values["source_id"] = source_id
                    for field, value in values.items():
                        setattr(existing, field, value)
                    existing.updated_at = now
                    pending_updates.append(existing)
                    run.updated += 1
                else:
                    unchanged_ids.append(existing.pk)
                    run.unchanged += 1

            with transaction.atomic():
                if pending_creates:
                    local_ids = LocalIdSequence.next_ids(len(pending_creates))
                    for survey, local_id in zip(pending_creates, local_ids):
                        survey.local_id = local_id
                    Survey.objects.bulk_create(
                        pending_creates,
                        batch_size=write_batch_size,
                    )
                if pending_updates:
                    Survey.objects.bulk_update(
                        pending_updates,
                        update_fields,
                        batch_size=write_batch_size,
                    )
                if unchanged_ids:
                    Survey.objects.filter(pk__in=unchanged_ids).update(
                        last_seen_at=snapshot_marker
                    )

        if getattr(client, "is_biobrain", False):
            # BioBrain lifecycle is governed solely by its documented EndDate,
            # not by whether a row happens to be absent from one inventory
            # snapshot. Reconcile every stored row as well as this response so
            # legacy rows incorrectly marked closed are repaired immediately.
            biobrain_surveys = Survey.objects.filter(integration=integration)
            run.closed = biobrain_surveys.filter(
                source_end_at__isnull=False,
                source_end_at__lte=now,
            ).exclude(status=Survey.Status.CLOSED).update(
                status=Survey.Status.CLOSED,
                updated_at=now,
            )
            biobrain_surveys.filter(
                Q(source_end_at__isnull=True) | Q(source_end_at__gt=now),
            ).exclude(status=Survey.Status.LIVE).update(
                status=Survey.Status.LIVE,
                updated_at=now,
            )
        else:
            # Every survey present in this snapshot was stamped with exactly
            # the unique marker above, so closing missing rows no longer needs
            # a 95k-value ``NOT IN`` clause.
            run.closed = Survey.objects.filter(
                status=Survey.Status.LIVE,
                integration=integration,
            ).exclude(last_seen_at=snapshot_marker).update(
                status=Survey.Status.CLOSED,
                updated_at=now,
            )

        # Detail endpoints are refreshed separately in bounded batches. This
        # keeps a large initial inventory import inside its one-minute window.
        run.status = SyncRun.Status.SUCCESS
    except Exception as exc:
        run.status = SyncRun.Status.FAILED
        run.error = str(exc)[:10000]
        logger.exception("InnovateMR survey sync failed")
        raise
    finally:
        run.finished_at = timezone.now()
        run.save()

    if run.status == SyncRun.Status.SUCCESS and (run.created or run.updated or run.closed):
        invalidate_project_cache()

    return SyncSummary(
        run_id=run.id,
        status=run.status,
        created=run.created,
        updated=run.updated,
        unchanged=run.unchanged,
        closed=run.closed,
        detail_failures=run.detail_failures,
    )
