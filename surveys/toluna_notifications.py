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
from django.db.models import F, Q
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
        exact = queryset.filter(source_key=f"{provider_survey_id}:{wave_id}")
        if exact.exists():
            return exact
    return queryset.filter(
        Q(source_id=provider_survey_id)
        | Q(source_key=str(provider_survey_id))
        | Q(source_key__startswith=f"{provider_survey_id}:")
    )


def _resolve_links(payload: dict):
    survey_id = _integer(_pick(payload, "SurveyId", "SurveyID"))
    wave_id = _integer(_pick(payload, "WaveId", "WaveID"))
    unique_code = _text(_pick(payload, "UniqueCode"))
    supplied_rid = _rid_from_additional_data(payload)
    surveys = _toluna_surveys(survey_id, wave_id)
    survey = surveys.order_by("-last_seen_at").first()

    attempts = SurveyAttempt.objects.select_related("survey__integration").filter(
        survey__integration__provider_code="toluna"
    )
    attempt = attempts.filter(rid=supplied_rid).first() if supplied_rid else None
    if attempt is None and unique_code:
        attempts = attempts.filter(
            Q(provider_profile_uid=unique_code) | Q(prescreener_uid=unique_code)
        )
        if survey_id is not None:
            attempts = attempts.filter(
                Q(survey__source_id=survey_id)
                | Q(survey__source_key=str(survey_id))
                | Q(survey__source_key__startswith=f"{survey_id}:")
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
        quota_queryset = SurveyQuota.objects.filter(
            quota_id=notification.quota_id,
            survey__integration__provider_code="toluna",
        )
        if notification.survey_id:
            quota_queryset = quota_queryset.filter(survey_id=notification.survey_id)
        quota = quota_queryset.order_by("-updated_at").first()
        if quota is None:
            return False, "Accepted; matching quota was not found yet."
        quota.status = "Open" if notification.is_live else "Full"
        fields = ["status", "updated_at"]
        if notification.is_live is False:
            quota.remaining = 0
            fields.append("remaining")
        quota.save(update_fields=fields)
        return True, "Quota availability updated."

    if notification.event_type == TolunaNotification.EventType.SURVEY_CLOSED:
        if not notification.survey_id:
            return False, "Accepted; matching survey was not found yet."
        notification.survey.status = Survey.Status.CLOSED
        notification.survey.remaining = 0
        notification.survey.save(update_fields=["status", "remaining", "updated_at"])
        return True, "Survey marked closed."
    return _apply_attempt_event(notification)


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
    defaults = _notification_defaults(event_type, payload, survey, attempt, integration)

    notification, created = TolunaNotification.objects.get_or_create(
        event_type=event_type,
        payload_hash=payload_hash,
        defaults=defaults,
    )
    if not created:
        TolunaNotification.objects.filter(pk=notification.pk).update(
            duplicate_count=F("duplicate_count") + 1,
            last_received_at=timezone.now(),
        )
        notification.refresh_from_db()
        return IngestedNotification(notification=notification, duplicate=True)

    applied, message = _apply_operational_event(notification)
    notification.applied = applied
    notification.processing_message = message
    notification.save(update_fields=["applied", "processing_message", "last_received_at"])
    return IngestedNotification(notification=notification, duplicate=False)
