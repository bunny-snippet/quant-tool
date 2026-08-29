import hashlib
import re
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from django.db import transaction
from django.utils import timezone

from surveys.age_rules import age_range_dict
from surveys.models import SurveyQuota, TargetingQuestion

from .base import ProviderError


def value(payload, *names, default=None):
    if not isinstance(payload, dict):
        return default
    lowered = {str(key).casefold(): item for key, item in payload.items()}
    for name in names:
        key = str(name).casefold()
        if key in lowered:
            return lowered[key]
    return default


def integer(raw, default=0):
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def money(raw):
    try:
        return Decimal(str(raw)) if raw not in (None, "") else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def decimal_value(raw):
    return money(raw)


def datetime_value(raw):
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    for parser in (
        lambda: datetime.fromisoformat(text),
        lambda: datetime.strptime(text, "%Y-%m-%d %H:%M:%S"),
        lambda: datetime.strptime(text, "%Y-%m-%d"),
    ):
        try:
            parsed = parser()
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt_timezone.utc)
            return parsed.astimezone(dt_timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


def stable_question_id(provider_code, question_id):
    raw = str(question_id or "").strip()
    if raw.isdigit() and int(raw) <= 9223372036854775807:
        return int(raw)
    digest = hashlib.sha256(f"{provider_code}:{raw}".encode("utf-8")).digest()
    return int.from_bytes(digest[:7], "big")


def split_values(raw_values):
    if raw_values in (None, ""):
        return []
    values = raw_values if isinstance(raw_values, (list, tuple, set)) else [raw_values]
    result = []
    for item in values:
        if isinstance(item, dict):
            item = value(item, "option", "answerId", "value", "id", default="")
        for part in str(item or "").split(","):
            cleaned = part.strip()
            if cleaned and cleaned not in result:
                result.append(cleaned)
    return result


def profile_dimension(key, text):
    haystack = f"{key} {text}".casefold()
    if any(term in haystack for term in ("postal", "zip code", "zipcode", "post code")):
        return "postal"
    if "gender" in haystack or "sex" in haystack:
        return "gender"
    if re.search(r"\bage\b|birth", haystack):
        return "age"
    return ""


def question_row(
    *, provider_code, survey, question_id, text, question_type, allowed_values,
    option_labels=None, category="Provider targeting", raw_data=None,
    dimension_hint="",
):
    question_key = str(question_id or "").strip()
    readable_text = str(text or "").replace("\ufffd", "").strip() or "Provider qualification"
    dimension = str(dimension_hint or "").strip().lower() or profile_dimension(question_key, readable_text)
    key = {"age": "AGE", "gender": "GENDER", "postal": "POSTAL_CODE"}.get(
        dimension, question_key
    )
    allowed = []
    for item in split_values(allowed_values):
        if item not in allowed:
            allowed.append(item)
    labels = {str(k): str(v) for k, v in (option_labels or {}).items()}
    ranges = []
    options = []
    for selected in allowed:
        label = labels.get(selected, selected)
        if dimension == "age" and (parsed := age_range_dict(label)):
            if parsed not in ranges:
                ranges.append(parsed)
            continue
        options.append({
            "OptionId": selected,
            "OptionText": label,
            "Qualifies": True,
        })
    normalized_raw = {
        "adapter_version": 1,
        "provider_code": provider_code,
        "targeting_choices": allowed,
        **(raw_data or {}),
    }
    if ranges:
        normalized_raw["targeting_age_ranges"] = ranges
        normalized_raw["targeting_note"] = "Qualifying age: " + ", ".join(
            f"{item['min']}–{item['max']}" for item in ranges
        )
    elif dimension == "postal" and allowed:
        normalized_raw["targeting_note"] = "Required ZIP / postal codes: " + ", ".join(allowed)
    elif allowed:
        normalized_raw["targeting_note"] = "Qualifying answers: " + ", ".join(
            labels.get(item, item) for item in allowed
        )
    normalized_type = str(question_type or "").strip()
    if ranges:
        normalized_type = "Numeric"
    elif len(options) > 1 and "multi" in normalized_type.casefold():
        normalized_type = "Multi Punch"
    elif options:
        normalized_type = "Single Punch"
    return TargetingQuestion(
        survey=survey,
        question_id=stable_question_id(provider_code, question_id),
        key=key,
        text=("What is your age?" if dimension == "age" else readable_text),
        question_type=normalized_type or ("Text" if dimension == "postal" else "Single Punch"),
        category=category,
        options=options,
        raw_data=normalized_raw,
    )


def merge_question_rows(rows):
    merged = {}
    for row in rows:
        existing = merged.get(row.question_id)
        if existing is None:
            merged[row.question_id] = row
            continue
        raw = dict(existing.raw_data or {})
        choices = list(raw.get("targeting_choices") or [])
        for choice in (row.raw_data or {}).get("targeting_choices") or []:
            if choice not in choices:
                choices.append(choice)
        raw["targeting_choices"] = choices
        ranges = list(raw.get("targeting_age_ranges") or [])
        for item in (row.raw_data or {}).get("targeting_age_ranges") or []:
            if item not in ranges:
                ranges.append(item)
        if ranges:
            raw["targeting_age_ranges"] = ranges
            raw["targeting_note"] = "Qualifying age: " + ", ".join(
                f"{item['min']}–{item['max']}" for item in ranges
            )
        option_ids = {str(item.get("OptionId")) for item in existing.options if isinstance(item, dict)}
        for option in row.options:
            if isinstance(option, dict) and str(option.get("OptionId")) not in option_ids:
                existing.options.append(option)
                option_ids.add(str(option.get("OptionId")))
        existing.raw_data = raw
    return list(merged.values())


def persist_details(survey, questions, quotas, *, survey_updates=None):
    now = timezone.now()
    questions = merge_question_rows(questions)
    with transaction.atomic():
        locked = survey.__class__.objects.select_for_update().get(pk=survey.pk)
        locked.targeting_questions.all().delete()
        locked.quotas.all().delete()
        TargetingQuestion.objects.bulk_create(questions)
        SurveyQuota.objects.bulk_create(quotas)
        for field, field_value in (survey_updates or {}).items():
            setattr(locked, field, field_value)
        locked.has_quota = bool(quotas)
        locked.targeting_synced_at = now
        locked.quota_synced_at = now
        locked.detail_synced_at = now
        update_fields = list((survey_updates or {}).keys()) + [
            "has_quota", "targeting_synced_at", "quota_synced_at",
            "detail_synced_at", "updated_at",
        ]
        locked.save(update_fields=list(dict.fromkeys(update_fields)))
    survey.refresh_from_db()


def replace_placeholders(url, replacements):
    result = str(url or "").strip()
    if not result:
        raise ProviderError("The provider entry link is unavailable.")
    for placeholder, replacement in replacements.items():
        result = re.sub(
            re.escape(placeholder),
            quote(str(replacement or ""), safe=""),
            result,
            flags=re.IGNORECASE,
        )
    if re.search(r"\[[^\]]+\]|\{[^}]+\}", result):
        raise ProviderError("The provider entry link still contains an unsupported placeholder.")
    if not result.lower().startswith("https://"):
        raise ProviderError("The provider entry link must use HTTPS.")
    return result
