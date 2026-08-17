"""Provider-neutral survey outcome normalization for reports and APIs."""

from .rfg_outcomes import describe_rfg_outcome


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

    config_mapping = ((integration.config or {}).get("outcome_mapping") or {}) if integration else {}
    field_mapping = (integration.field_mapping or {}) if integration else {}
    mapping = {
        "status": config_mapping.get("status") or field_mapping.get("outcome_status"),
        "reason": config_mapping.get("reason") or field_mapping.get("outcome_reason"),
        "category": config_mapping.get("category") or field_mapping.get("outcome_category"),
    }
    candidates = [data]
    candidates.extend(
        value for key in ("transaction", "outcome", "result", "local_country_guard")
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

    return {
        "status": mapped_or_common("status", ("status", "Status", "resultStatus", "outcome")),
        "reason": mapped_or_common(
            "reason", ("termReason", "term_reason", "reason", "ruledOutBy", "message", "description")
        ),
        "category": mapped_or_common(
            "category", ("termReasonCategory", "termReasonCategoryCode", "termCategory", "reasonCategory")
        ),
    }
