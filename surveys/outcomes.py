"""Provider-neutral survey outcome normalization for reports and APIs."""

from .rfg_outcomes import describe_rfg_outcome


# Toluna sends a signed browser callback status separately from its more
# detailed notification stream.  Keep this deliberately small and explicit:
# raw callback parameters remain in the attempt audit, while only reviewed
# provider rejection identifiers are promoted into operator/respondent text.
TOLUNA_CALLBACK_REJECTIONS = {
    "73": {
        "reason": (
            "Toluna rejected this attempt because the same internet identity "
            "has already attempted this survey."
        ),
        "category": "Duplicate survey attempt",
    },
}


def _nested_value(payload, path):
    if not path:
        return ""
    value = payload
    for part in str(path).split("."):
        if not isinstance(value, dict):
            return ""
        value = value.get(part)
    return value if value is not None else ""


def _text(value):
    """Return a safe human-readable scalar; never leak raw JSON into the UI."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, dict):
        for key in (
            "title", "label", "name", "text", "value", "status", "reason",
            "message", "description", "category", "code",
        ):
            text_value = _text(value.get(key))
            if text_value:
                return text_value
        return ""
    if isinstance(value, (list, tuple)):
        values = [_text(item) for item in value]
        return ", ".join(dict.fromkeys(item for item in values if item))
    return ""


def _casefold_value(payload, *keys):
    if not isinstance(payload, dict):
        return ""
    folded = {str(key).casefold(): value for key, value in payload.items()}
    for key in keys:
        value = _text(folded.get(str(key).casefold()))
        if value:
            return value
    return ""


def describe_toluna_callback(parameters, *, code="", status="", title=""):
    """Return whitelisted, human-readable fields from a Toluna callback."""

    rejection_id = _casefold_value(
        parameters,
        "rejectionID",
        "rejectionId",
        "rejection_id",
    )
    rejection = TOLUNA_CALLBACK_REJECTIONS.get(rejection_id, {})
    outcome = {
        "code": _text(code or _casefold_value(parameters, "status")),
        "status": _text(status),
        "title": _text(title),
    }
    if rejection_id:
        outcome["rejection_id"] = rejection_id[:40]
    if rejection:
        outcome.update(rejection)
    return outcome


def provider_outcome(attempt):
    """Return clean status/reason/category strings for any configured provider."""

    raw_data = attempt.upstream_transaction_data or {}
    if isinstance(raw_data, dict):
        data = raw_data
    elif isinstance(raw_data, list):
        rows = [row for row in raw_data if isinstance(row, dict)]
        data = next(
            (
                row for row in rows
                if any(str(row.get(key) or "") == attempt.rid for key in ("PID", "pid", "trackId", "rid", "RID"))
            ),
            rows[0] if rows else {},
        )
    else:
        data = {}
    integration = attempt.survey.integration if attempt.survey.integration_id else None
    provider_code = (integration.provider_code if integration else "innovatemr").lower()
    if provider_code == "rfg":
        parameters = data.get("rfg_callback") or data.get("rfg_local_outcome") or {}
        outcome = data.get("rfg_outcome") or describe_rfg_outcome(parameters, attempt=attempt)
        return {
            "status": _text(outcome.get("title") or outcome.get("status") or attempt.get_status_display()),
            "reason": _text(outcome.get("reason") or parameters.get("ruledOutBy") or parameters.get("local_reason")),
            "category": _text(parameters.get("ruledOutBy")),
        }

    if provider_code == "toluna":
        stored_outcome = data.get("toluna_outcome") or {}
        callback = data.get("toluna_callback") or {}
        has_callback_outcome = bool(
            (isinstance(callback, dict) and callback)
            or (isinstance(stored_outcome, dict) and stored_outcome)
        )
        stored_rejection_id = (
            _text(stored_outcome.get("rejection_id"))
            if isinstance(stored_outcome, dict)
            else ""
        )
        callback_for_description = callback if isinstance(callback, dict) else {}
        if not callback_for_description and stored_rejection_id:
            callback_for_description = {"rejectionID": stored_rejection_id}
        callback_outcome = describe_toluna_callback(
            callback_for_description,
            code=(stored_outcome.get("code") if isinstance(stored_outcome, dict) else ""),
            status=(stored_outcome.get("status") if isinstance(stored_outcome, dict) else ""),
            title=(stored_outcome.get("title") if isinstance(stored_outcome, dict) else ""),
        )
        notification = data.get("toluna_notification") or {}
        if isinstance(notification, dict) and notification:
            return {
                "status": _text(
                    notification.get("status")
                    or callback_outcome.get("status")
                    or attempt.get_status_display()
                ),
                "reason": _text(notification.get("reason") or callback_outcome.get("reason")),
                "category": _text(
                    notification.get("category")
                    or notification.get("event_type")
                    or callback_outcome.get("category")
                ),
            }
        if has_callback_outcome:
            return {
                "status": _text(
                    callback_outcome.get("status")
                    or callback_outcome.get("title")
                    or attempt.get_status_display()
                ),
                "reason": _text(callback_outcome.get("reason")),
                "category": _text(callback_outcome.get("category")),
            }

    config_mapping = ((integration.config or {}).get("outcome_mapping") or {}) if integration else {}
    field_mapping = (integration.field_mapping or {}) if integration else {}
    mapping = {
        "status": config_mapping.get("status") or field_mapping.get("outcome_status"),
        "reason": config_mapping.get("reason") or field_mapping.get("outcome_reason"),
        "category": config_mapping.get("category") or field_mapping.get("outcome_category"),
    }
    candidates = [data]
    candidates.extend(
        value for key in ("transaction", "outcome", "result", "local_country_guard", "local_ip_guard")
        if isinstance((value := data.get(key)), dict)
    )

    def mapped_or_common(canonical, common_keys):
        mapped = _text(_nested_value(data, mapping.get(canonical)))
        if mapped:
            return mapped
        for candidate in candidates:
            for key in common_keys:
                value = _text(candidate.get(key))
                if value:
                    return value
        return ""

    outcome = {
        "status": mapped_or_common("status", ("status", "Status", "resultStatus", "outcome")),
        "reason": mapped_or_common(
            "reason", ("termReason", "term_reason", "reason", "ruledOutBy", "message", "description")
        ),
        "category": mapped_or_common(
            "category", ("termReasonCategory", "termReasonCategoryCode", "termCategory", "reasonCategory")
        ),
    }
    # The signed InnovateMR redirect gives the fastest, attempt-specific reason.
    # Keep the transaction API payload above as the detailed history/status
    # source, but prefer the authenticated redirect reason when it is present.
    if provider_code == "innovatemr" and attempt.is_verified:
        exit_data = attempt.exit_client_data or {}
        callback = exit_data.get("innovatemr_callback")
        if isinstance(callback, dict):
            callback_reason = _text(callback.get("termReason"))
            if callback_reason:
                outcome["reason"] = callback_reason
    return outcome
