"""Authenticated Toluna notification ingestion and normalization.

The public views in :mod:`surveys.webhook_views` deliberately delegate all
matching and mutation to this module so every notification type follows the
same idempotency and audit rules.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from urllib.parse import parse_qsl, urlsplit

from django.db import transaction
from django.db.models import F, OuterRef, Q, Subquery
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from vendors.services import finalize_attempt_capacity

from .models import Survey, SurveyAttempt, SurveyQuota, TolunaNotification


TERMINATION_STATUS_MAP = {
    "quotafull": SurveyAttempt.Status.OVER_QUOTA,
    "surveytaken": SurveyAttempt.Status.SURVEY_TAKEN,
    "terminated": SurveyAttempt.Status.TERMINATED,
    "surveynotavailable": SurveyAttempt.Status.SURVEY_NOT_AVAILABLE,
    "nosurveysavailable": SurveyAttempt.Status.NO_SURVEYS,
    "nocookie": SurveyAttempt.Status.NO_COOKIES,
    "maxsurveysreached": SurveyAttempt.Status.MAX_SURVEYS_REACHED,
    "notqualified": SurveyAttempt.Status.NOT_QUALIFIED,
}

OPERATIONAL_EVENT_TYPES = {
    TolunaNotification.EventType.QUOTA_STATUS,
    TolunaNotification.EventType.SURVEY_CLOSED,
}
OPERATIONAL_IDENTITY_CHUNK_SIZE = 100
OPERATIONAL_RECONCILE_BATCH_SIZE = 200


class TolunaNotificationError(ValueError):
    """Raised when Toluna sends a structurally invalid notification."""


@dataclass(frozen=True)
class IngestedNotification:
    notification: TolunaNotification
    duplicate: bool


def _pick(payload, *names, default=None):
    if not isinstance(payload, dict):
        return default
    for name in names:
        if name in payload:
            return payload[name]
    lowered = {str(key).lower(): value for key, value in payload.items()}
    for name in names:
        if str(name).lower() in lowered:
            return lowered[str(name).lower()]
    return default


def _text(value) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _integer(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _boolean(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = _text(value).lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _provider_datetime(value):
    raw = _text(value)
    if not raw:
        return None
    parsed = parse_datetime(raw)
    if parsed is None:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f %z"):
            try:
                parsed = datetime.strptime(raw, pattern)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed.astimezone(dt_timezone.utc)


def _canonical_payload_hash(event_type: str, payload: dict, integration_id=None) -> str:
    # Toluna explicitly recommends de-duplicating completion callbacks by
    # SurveyID + UniqueCode because an end-page refresh can deliver the same
    # completion again with otherwise non-identical metadata.
    if event_type == TolunaNotification.EventType.MEMBER_COMPLETE:
        identity = {
            "IntegrationId": integration_id,
            "SurveyId": _integer(_pick(payload, "SurveyId", "SurveyID")),
            "WaveId": _integer(_pick(payload, "WaveId", "WaveID")),
            "UniqueCode": _text(_pick(payload, "UniqueCode")),
        }
    else:
        identity = {"IntegrationId": integration_id, "Payload": payload}
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _rid_from_additional_data(payload: dict) -> str:
    additional = _text(_pick(payload, "AdditionalData", "additionalData"))
    if not additional:
        return ""
    query = urlsplit(additional).query if "://" in additional else additional.lstrip("?")
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key.lower() in {"rid", "trackid", "pid"} and value:
            return value.strip()
    return ""


def _toluna_surveys(provider_survey_id, wave_id=None):
    queryset = Survey.objects.select_related("integration__client").filter(
        integration__provider_code="toluna"
    )
    if provider_survey_id is None:
        return queryset.none()
    if wave_id is not None:
        # WaveID identifies a concrete Toluna inventory row.  Falling back to
        # another wave of the same SurveyID can close or fill an unrelated
        # live survey, so an exact miss must remain unmatched until inventory
        # reconciliation sees that precise row.
        return queryset.filter(source_key=f"{provider_survey_id}:{wave_id}")
    return queryset.filter(
        Q(source_id=provider_survey_id)
        | Q(source_key=str(provider_survey_id))
        | Q(source_key__startswith=f"{provider_survey_id}:")
    )


def _unambiguous_survey(queryset):
    matches = list(queryset.order_by("-last_seen_at", "-pk")[:2])
    return matches[0] if len(matches) == 1 else None


def _filter_attempts_by_survey_identity(queryset, survey_id, wave_id):
    if survey_id is None:
        return queryset
    if wave_id is not None:
        return queryset.filter(survey__source_key=f"{survey_id}:{wave_id}")
    return queryset.filter(
        Q(survey__source_id=survey_id)
        | Q(survey__source_key=str(survey_id))
        | Q(survey__source_key__startswith=f"{survey_id}:")
    )


def _resolve_links(payload: dict):
    survey_id = _integer(_pick(payload, "SurveyId", "SurveyID"))
    wave_id = _integer(_pick(payload, "WaveId", "WaveID"))
    unique_code = _text(_pick(payload, "UniqueCode"))
    supplied_rid = _rid_from_additional_data(payload)
    surveys = _toluna_surveys(survey_id, wave_id)
    survey = _unambiguous_survey(surveys)

    attempts = SurveyAttempt.objects.select_related("survey__integration").filter(
        survey__integration__provider_code="toluna"
    )
    attempts = _filter_attempts_by_survey_identity(attempts, survey_id, wave_id)
    attempt = attempts.filter(rid=supplied_rid).first() if supplied_rid else None
    if attempt is None and unique_code:
        attempts = attempts.filter(
            Q(provider_profile_uid=unique_code) | Q(prescreener_uid=unique_code)
        )
        attempt = attempts.order_by("-initiated_at").first()
    if attempt is not None:
        survey = attempt.survey
    integration = survey.integration if survey and survey.integration_id else None
    return survey, attempt, integration


def _validate_payload(event_type: str, payload: dict):
    if not isinstance(payload, dict):
        raise TolunaNotificationError("A JSON object is required.")
    if _integer(_pick(payload, "SurveyId", "SurveyID")) is None:
        raise TolunaNotificationError("SurveyId is required.")
    if _integer(_pick(payload, "WaveId", "WaveID")) is None:
        # Toluna defines SurveyID + WaveID as the unique survey interaction.
        # Never fall back to another wave, including for respondent events
        # where a reused MemberCode can exist on multiple journeys.
        raise TolunaNotificationError("WaveID is required.")
    member_events = {
        TolunaNotification.EventType.MEMBER_COMPLETE,
        TolunaNotification.EventType.MEMBER_TERMINATE,
        TolunaNotification.EventType.ENHANCED_TERMINATION,
        TolunaNotification.EventType.RECONCILIATION,
    }
    if event_type in member_events and not _text(_pick(payload, "UniqueCode")):
        raise TolunaNotificationError("UniqueCode is required.")
    if event_type in {
        TolunaNotification.EventType.MEMBER_TERMINATE,
        TolunaNotification.EventType.ENHANCED_TERMINATION,
    } and not _text(_pick(payload, "Reason")):
        raise TolunaNotificationError("Reason is required.")
    if event_type == TolunaNotification.EventType.QUOTA_STATUS:
        if _integer(_pick(payload, "QuotaId", "QuotaID")) is None:
            raise TolunaNotificationError("QuotaID is required.")
        if _boolean(_pick(payload, "IsLive")) is None:
            raise TolunaNotificationError("IsLive must be true or false.")


def _notification_defaults(event_type: str, payload: dict, survey, attempt, integration):
    reason = _text(_pick(payload, "Reason"))
    rejection_name = _text(_pick(payload, "RejectionName"))
    status_text = {
        TolunaNotification.EventType.MEMBER_COMPLETE: "Completed",
        TolunaNotification.EventType.MEMBER_TERMINATE: reason or "Terminated",
        TolunaNotification.EventType.ENHANCED_TERMINATION: rejection_name or reason or "Enhanced termination",
        TolunaNotification.EventType.QUOTA_STATUS: "Open" if _boolean(_pick(payload, "IsLive")) else "Unavailable",
        TolunaNotification.EventType.SURVEY_CLOSED: _text(_pick(payload, "Status")) or "Closed",
        TolunaNotification.EventType.RECONCILIATION: "Reconciled",
    }[event_type]
    occurred_value = (
        _pick(payload, "UpdateDateTimeUTC")
        if event_type == TolunaNotification.EventType.QUOTA_STATUS
        else _pick(payload, "ReconciliationDateTime")
        if event_type == TolunaNotification.EventType.RECONCILIATION
        else _pick(payload, "DateTime")
    )
    return {
        "integration": integration,
        "survey": survey,
        "attempt": attempt,
        "unique_code": _text(_pick(payload, "UniqueCode")),
        "provider_survey_id": _integer(_pick(payload, "SurveyId", "SurveyID")),
        "survey_ref": _text(_pick(payload, "SurveyRef")),
        "wave_id": _integer(_pick(payload, "WaveId", "WaveID")),
        "quota_id": _integer(_pick(payload, "QuotaId", "QuotaID")),
        "provider_status": status_text,
        "reason": reason,
        "rejection_id": _integer(_pick(payload, "RejectionId", "RejectionID")),
        "rejection_name": rejection_name,
        "reconciliation_id": _integer(_pick(payload, "ReconciliationId", "ReconciliationID")),
        "revenue_cents": _integer(_pick(payload, "Revenue")),
        "is_live": _boolean(_pick(payload, "IsLive")),
        "occurred_at": _provider_datetime(occurred_value),
        "raw_payload": payload,
    }


def _attempt_status_for(notification: TolunaNotification):
    if notification.event_type == TolunaNotification.EventType.MEMBER_COMPLETE:
        return SurveyAttempt.Status.COMPLETED
    if notification.event_type == TolunaNotification.EventType.RECONCILIATION:
        return SurveyAttempt.Status.QUALITY_TERMINATED
    if notification.event_type in {
        TolunaNotification.EventType.MEMBER_TERMINATE,
        TolunaNotification.EventType.ENHANCED_TERMINATION,
    }:
        rejection = notification.rejection_name.lower()
        if any(token in rejection for token in ("fraud", "quality", "security", "threat")):
            return SurveyAttempt.Status.QUALITY_TERMINATED
        return TERMINATION_STATUS_MAP.get(
            notification.reason.replace(" ", "").lower(),
            SurveyAttempt.Status.TERMINATED,
        )
    return None


def _apply_attempt_event(notification: TolunaNotification):
    if not notification.attempt_id:
        return False, "Accepted; matching respondent journey was not found yet."
    with transaction.atomic():
        attempt = SurveyAttempt.objects.select_for_update().get(pk=notification.attempt_id)
        target_status = _attempt_status_for(notification)
        if target_status is None:
            return False, "Accepted as an operational notification."
        if (
            attempt.status == SurveyAttempt.Status.COMPLETED
            and notification.event_type not in {
                TolunaNotification.EventType.MEMBER_COMPLETE,
                TolunaNotification.EventType.RECONCILIATION,
            }
        ):
            return False, "Accepted; a late termination did not replace the verified completion."

        ended_at = notification.occurred_at or timezone.now()
        attempt.status = target_status
        attempt.callback_at = attempt.callback_at or ended_at
        attempt.last_callback_at = timezone.now()
        attempt.callback_count += 1
        attempt.loi_seconds = attempt.calculate_loi_seconds(ended_at)
        attempt.status_source = f"toluna_notification_{notification.event_type}"
        attempt.is_verified = True
        attempt.upstream_transaction_data = {
            **(attempt.upstream_transaction_data or {}),
            "toluna_notification": {
                "event_id": notification.pk,
                "event_type": notification.event_type,
                "status": notification.provider_status,
                "reason": notification.rejection_name or notification.reason,
                "category": notification.reason if notification.rejection_name else "",
                "quota_id": notification.quota_id,
                "rejection_id": notification.rejection_id,
                "reconciliation_id": notification.reconciliation_id,
            },
        }
        attempt.save(update_fields=[
            "status", "callback_at", "last_callback_at", "callback_count", "loi_seconds",
            "status_source", "is_verified", "upstream_transaction_data", "updated_at",
        ])
        finalize_attempt_capacity(attempt)
    return True, "Respondent journey updated."


def _apply_operational_event(notification: TolunaNotification):
    if notification.event_type == TolunaNotification.EventType.QUOTA_STATUS:
        survey_label = _notification_survey_label(notification)
        if not notification.survey_id:
            return False, f"Pending reconciliation; exact Toluna survey {survey_label} is not in local inventory."
        with transaction.atomic():
            quota = SurveyQuota.objects.select_for_update().filter(
                survey_id=notification.survey_id,
                quota_id=notification.quota_id,
                survey__integration__provider_code="toluna",
            ).order_by("pk").first()
            if quota is None:
                return (
                    False,
                    f"Pending reconciliation; quota {notification.quota_id} is not available on exact "
                    f"Toluna survey {survey_label} yet.",
                )
            if _newer_applied_quota_notification_exists(notification):
                return (
                    True,
                    f"Processed without mutation; a newer provider update already controls quota "
                    f"{notification.quota_id} on Toluna survey {survey_label}.",
                )
            quota.status = "Open" if notification.is_live else "Full"
            fields = ["status", "updated_at"]
            if notification.is_live is False:
                quota.remaining = 0
                fields.append("remaining")
            elif quota.remaining <= 0:
                # IsLive explicitly means Toluna can accept sample again. The
                # notification carries no numeric capacity, so retain an
                # existing positive value or use the conservative lower bound
                # until the next quota inventory refresh supplies the count.
                quota.remaining = 1
                fields.append("remaining")
            quota.save(update_fields=fields)
        return (
            True,
            f"Applied to exact Toluna survey {survey_label} and quota {notification.quota_id}.",
        )

    if notification.event_type == TolunaNotification.EventType.SURVEY_CLOSED:
        survey_label = _notification_survey_label(notification)
        if not notification.survey_id:
            return False, f"Pending reconciliation; exact Toluna survey {survey_label} is not in local inventory."
        notification.survey.status = Survey.Status.CLOSED
        notification.survey.remaining = 0
        notification.survey.save(update_fields=["status", "remaining", "updated_at"])
        return True, f"Applied to exact Toluna survey {survey_label}; survey marked closed."
    return _apply_attempt_event(notification)


def _notification_survey_label(notification: TolunaNotification) -> str:
    survey_id = notification.provider_survey_id
    wave_id = notification.wave_id
    if survey_id is None:
        return "(identifier not supplied)"
    return f"{survey_id} / wave {wave_id}" if wave_id is not None else str(survey_id)


def _newer_applied_quota_notification_exists(notification: TolunaNotification) -> bool:
    """Use Toluna's provider timestamp to keep late deliveries from regressing state."""

    queryset = TolunaNotification.objects.filter(
        event_type=TolunaNotification.EventType.QUOTA_STATUS,
        applied=True,
        survey_id=notification.survey_id,
        quota_id=notification.quota_id,
    ).exclude(pk=notification.pk)
    if notification.occurred_at is None:
        # An undated delivery cannot safely supersede any timestamped provider
        # state.  Among other undated events, delivery order is deterministic.
        return queryset.filter(
            Q(occurred_at__isnull=False)
            | Q(occurred_at__isnull=True, received_at__gt=notification.received_at)
            | Q(
                occurred_at__isnull=True,
                received_at=notification.received_at,
                pk__gt=notification.pk,
            )
        ).exists()
    return queryset.filter(
        Q(occurred_at__gt=notification.occurred_at)
        | Q(occurred_at=notification.occurred_at, received_at__gt=notification.received_at)
        | Q(
            occurred_at=notification.occurred_at,
            received_at=notification.received_at,
            pk__gt=notification.pk,
        )
    ).exists()


def _reconcile_notification(notification, *, replay_applied=False):
    """Relink and safely retry one persisted notification under a row lock."""

    notification_id = notification.pk if isinstance(notification, TolunaNotification) else notification
    with transaction.atomic():
        locked = TolunaNotification.objects.select_for_update().select_related(
            "survey__integration", "attempt__survey__integration", "integration"
        ).get(pk=notification_id)
        if locked.applied and not replay_applied:
            return locked
        survey, attempt, integration = _resolve_links(locked.raw_payload or {})
        if survey is None and locked.survey_id:
            existing_survey_id, existing_wave_id = _survey_provider_identity(locked.survey)
            if (
                existing_survey_id == locked.provider_survey_id
                and existing_wave_id == locked.wave_id
            ):
                survey = locked.survey
        if attempt is None and locked.attempt_id:
            # Respondent notifications can become temporarily ambiguous after
            # newer reuse journeys exist. Never erase a previously exact audit
            # link merely because a later reconciliation cannot improve it.
            attempt = locked.attempt
        if integration is None:
            integration = (
                survey.integration
                if survey and survey.integration_id
                else locked.integration
            )
        locked.survey = survey
        locked.attempt = attempt
        locked.integration = integration
        applied, message = _apply_operational_event(locked)
        locked.applied = applied
        locked.processing_message = message
        locked.save(update_fields=[
            "survey", "attempt", "integration", "applied", "processing_message",
        ])
        return locked


def _survey_provider_identity(survey: Survey):
    raw = survey.raw_data or {}
    survey_id = _integer(_pick(raw, "SurveyID", "SurveyId"))
    wave_id = _integer(_pick(raw, "WaveID", "WaveId"))
    if survey_id is None:
        source_survey_id, separator, source_wave_id = str(survey.source_key or "").partition(":")
        survey_id = _integer(source_survey_id)
        if separator and wave_id is None:
            wave_id = _integer(source_wave_id)
    return survey_id, wave_id


def _latest_applied_operational_ids(queryset, event_types, *, limit=None) -> list[int]:
    """Select one authoritative applied row per exact operational identity."""

    event_types = set(event_types or ()) & OPERATIONAL_EVENT_TYPES
    notification_ids = []
    remaining = OPERATIONAL_RECONCILE_BATCH_SIZE if limit is None else max(0, int(limit))
    ordering = (
        F("occurred_at").desc(nulls_last=True),
        F("received_at").desc(),
        F("pk").desc(),
    )
    # A whole-survey close has broader impact than any individual quota event,
    # so it receives the first replay slot when a run reaches its hard bound.
    if remaining and TolunaNotification.EventType.SURVEY_CLOSED in event_types:
        latest_close = TolunaNotification.objects.filter(
            applied=True,
            event_type=TolunaNotification.EventType.SURVEY_CLOSED,
            provider_survey_id=OuterRef("provider_survey_id"),
            wave_id=OuterRef("wave_id"),
        ).order_by(*ordering).values("pk")[:1]
        close_ids = list(
            queryset.filter(
                applied=True,
                event_type=TolunaNotification.EventType.SURVEY_CLOSED,
            ).annotate(
                latest_operational_id=Subquery(latest_close)
            ).filter(
                pk=F("latest_operational_id")
            ).order_by("received_at", "pk").values_list("pk", flat=True)[:remaining]
        )
        notification_ids.extend(close_ids)
        remaining -= len(close_ids)
    if remaining and TolunaNotification.EventType.QUOTA_STATUS in event_types:
        latest_quota = TolunaNotification.objects.filter(
            applied=True,
            event_type=TolunaNotification.EventType.QUOTA_STATUS,
            provider_survey_id=OuterRef("provider_survey_id"),
            wave_id=OuterRef("wave_id"),
            quota_id=OuterRef("quota_id"),
        ).order_by(*ordering).values("pk")[:1]
        notification_ids.extend(
            queryset.filter(
                applied=True,
                event_type=TolunaNotification.EventType.QUOTA_STATUS,
            ).annotate(
                latest_operational_id=Subquery(latest_quota)
            ).filter(
                pk=F("latest_operational_id")
            ).order_by("received_at", "pk").values_list("pk", flat=True)[:remaining]
        )
    return notification_ids


def _operational_notification_ids(
    queryset,
    *,
    replay_event_types=(),
    applied_since=None,
    limit=None,
) -> list[int]:
    limit = OPERATIONAL_RECONCILE_BATCH_SIZE if limit is None else max(0, int(limit))
    if not limit:
        return []
    applied_queryset = queryset
    if applied_since is not None:
        applied_queryset = applied_queryset.filter(received_at__gte=applied_since)
    applied_ids = _latest_applied_operational_ids(
        applied_queryset,
        replay_event_types,
        limit=limit,
    )
    remaining = limit - len(applied_ids)
    pending_ids = list(
        queryset.filter(applied=False)
        .order_by("occurred_at", "received_at", "pk")
        .values_list("pk", flat=True)[:remaining]
    )
    return list(dict.fromkeys([*applied_ids, *pending_ids]))


def reconcile_toluna_operational_notifications(
    survey: Survey,
    *,
    replay_applied=False,
    applied_since=None,
) -> int:
    """Retry notifications after exact Toluna inventory/detail data is available.

    ``replay_applied`` is safe for post-sync use: operational updates are
    idempotent and quota ordering ensures only the newest provider state wins.
    It also restores a notification state if a detail refresh replaced quota
    rows from a slightly older inventory snapshot.
    """

    if not survey.integration_id or survey.integration.provider_code != "toluna":
        return 0
    provider_survey_id, wave_id = _survey_provider_identity(survey)
    if provider_survey_id is None or wave_id is None:
        return 0
    queryset = TolunaNotification.objects.filter(
        event_type__in=OPERATIONAL_EVENT_TYPES,
        provider_survey_id=provider_survey_id,
        wave_id=wave_id,
    )
    notification_ids = _operational_notification_ids(
        queryset,
        replay_event_types=OPERATIONAL_EVENT_TYPES if replay_applied else (),
        applied_since=applied_since,
    )
    for notification_id in notification_ids:
        _reconcile_notification(notification_id, replay_applied=replay_applied)
    return len(notification_ids)


def reconcile_toluna_operational_notifications_for_surveys(
    surveys,
    *,
    include_applied_event_types=(),
    applied_since=None,
) -> int:
    """Batch-reconcile one inventory sync without an N+1 notification query."""

    provider_identities = {
        (provider_survey_id, wave_id)
        for survey in surveys
        for provider_survey_id, wave_id in [_survey_provider_identity(survey)]
        if provider_survey_id is not None and wave_id is not None
    }
    if not provider_identities:
        return 0
    replay_types = set(include_applied_event_types or ()) & OPERATIONAL_EVENT_TYPES
    identities = sorted(provider_identities)
    remaining = OPERATIONAL_RECONCILE_BATCH_SIZE
    reconciled = 0
    for offset in range(0, len(identities), OPERATIONAL_IDENTITY_CHUNK_SIZE):
        if remaining <= 0:
            break
        exact_identity = Q()
        for provider_survey_id, wave_id in identities[
            offset:offset + OPERATIONAL_IDENTITY_CHUNK_SIZE
        ]:
            exact_identity |= Q(
                provider_survey_id=provider_survey_id,
                wave_id=wave_id,
            )
        queryset = TolunaNotification.objects.filter(
            event_type__in=OPERATIONAL_EVENT_TYPES,
        ).filter(exact_identity)
        notification_ids = _operational_notification_ids(
            queryset,
            replay_event_types=replay_types,
            applied_since=applied_since,
            limit=remaining,
        )
        for notification_id in notification_ids:
            _reconcile_notification(notification_id, replay_applied=True)
        reconciled += len(notification_ids)
        remaining -= len(notification_ids)
    return reconciled


def ingest_toluna_notification(event_type: str, payload: dict) -> IngestedNotification:
    """Persist and apply a Toluna notification exactly once."""

    valid_types = {value for value, _label in TolunaNotification.EventType.choices}
    if event_type not in valid_types:
        raise TolunaNotificationError("Unsupported Toluna notification type.")
    _validate_payload(event_type, payload)
    survey, attempt, integration = _resolve_links(payload)
    payload_hash = _canonical_payload_hash(
        event_type,
        payload,
        integration_id=integration.pk if integration else None,
    )
    anonymous_payload_hash = _canonical_payload_hash(event_type, payload, integration_id=None)
    defaults = _notification_defaults(event_type, payload, survey, attempt, integration)

    # A first delivery can precede inventory and therefore have no integration
    # link.  Once the exact survey arrives, use its anonymous hash as a
    # backwards-compatible duplicate candidate instead of creating a second
    # audit row with an integration-qualified hash.
    notification = TolunaNotification.objects.filter(
        event_type=event_type,
        payload_hash=payload_hash,
    ).first()
    if notification is None and anonymous_payload_hash != payload_hash:
        notification = TolunaNotification.objects.filter(
            event_type=event_type,
            payload_hash=anonymous_payload_hash,
        ).first()
    if notification is None:
        notification, created = TolunaNotification.objects.get_or_create(
            event_type=event_type,
            payload_hash=payload_hash,
            defaults=defaults,
        )
    else:
        created = False
    if not created:
        TolunaNotification.objects.filter(pk=notification.pk).update(
            duplicate_count=F("duplicate_count") + 1,
            last_received_at=timezone.now(),
        )
        notification.refresh_from_db()
        if not notification.applied:
            notification = _reconcile_notification(notification)
        return IngestedNotification(notification=notification, duplicate=True)

    notification = _reconcile_notification(notification)
    return IngestedNotification(notification=notification, duplicate=False)
