import re

from .models import TolunaNotification


_RESPONDENT_NOTIFICATION_TYPES = {
    TolunaNotification.EventType.MEMBER_COMPLETE,
    TolunaNotification.EventType.MEMBER_TERMINATE,
    TolunaNotification.EventType.ENHANCED_TERMINATION,
    TolunaNotification.EventType.RECONCILIATION,
}


def _friendly_text(value, limit):
    text = str(value or "").strip().replace("_", " ").replace("-", " ")
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = " ".join(text.split())
    return (text[:1].upper() + text[1:])[:limit] if text else ""


def verified_toluna_notification_summary(attempt):
    """Return a small, safe summary of the event that set this attempt status."""

    if (
        attempt is None
        or not attempt.is_verified
        or not str(attempt.status_source or "").startswith("toluna_notification_")
        or not attempt.survey.integration_id
        or attempt.survey.integration.provider_code != "toluna"
    ):
        return None
    audit = (attempt.upstream_transaction_data or {}).get("toluna_notification") or {}
    event_id = audit.get("event_id")
    if not event_id:
        return None
    notification = (
        TolunaNotification.objects.filter(
            pk=event_id,
            attempt_id=attempt.pk,
            integration_id=attempt.survey.integration_id,
            applied=True,
            event_type__in=_RESPONDENT_NOTIFICATION_TYPES,
        )
        .defer("raw_payload")
        .first()
    )
    if (
        notification is None
        or attempt.status_source != f"toluna_notification_{notification.event_type}"
    ):
        return None

    outcome = _friendly_text(notification.provider_status, 80)
    if not outcome:
        outcome = notification.get_event_type_display()
    reason = _friendly_text(notification.rejection_name or notification.reason, 200)
    return {
        "source": "Verified Toluna notification",
        "outcome": outcome,
        "reason": reason,
    }
