"""Provider-neutral prescreener age-band normalization."""

import re


OPEN_ENDED_AGE_MAX = 99

_CLOSED_RANGE = re.compile(
    r"(?<!\d)(\d{1,3})\s*(?:-|\u2013|\u2014|to|through)\s*(\d{1,3})(?!\d)",
    re.I,
)
_OPEN_RANGE = re.compile(
    r"(?<!\d)(\d{1,3})\s*(?:years?|yrs?)?\s*(?:\+|plus\b|"
    r"(?:(?:and|or)\s+)?(?:older|over|above|more|up)\b)",
    re.I,
)
_PREFIX_OPEN_RANGE = re.compile(
    r"\b(?:over|above|older\s+than)\s*(\d{1,3})\b",
    re.I,
)
_EXACT_AGE = re.compile(r"^\d{1,3}$")


def normalize_age_range(value):
    """Return ``(minimum, maximum)`` with every open maximum capped at 99."""

    if isinstance(value, dict):
        label = ""
        for key in (
            "OptionText", "Translation", "Label", "label", "Range", "range",
            "DisplayText", "display_text", "Name", "name", "Text", "text",
            "Description", "description", "Title", "title",
        ):
            candidate_value = value.get(key)
            if isinstance(candidate_value, dict):
                if (parsed := normalize_age_range(candidate_value)) is not None:
                    return parsed
                continue
            candidate = str(candidate_value or "").strip()
            if candidate:
                label = candidate
                break
        # A range-bearing label is authoritative, but a bare label such as
        # ``65`` must not turn ``ageStart=65, ageEnd=None`` into the exact age
        # 65; missing metadata maxima are open-ended and therefore end at 99.
        normalized_label = label.replace("&", " and ")
        if label and (
            _CLOSED_RANGE.search(normalized_label)
            or _OPEN_RANGE.search(normalized_label)
            or _PREFIX_OPEN_RANGE.search(normalized_label)
        ):
            if (parsed := normalize_age_range(normalized_label)) is not None:
                return parsed

        def first_value(*keys):
            for key in keys:
                if key in value and value.get(key) not in (None, ""):
                    return value.get(key)
            return None

        minimum = first_value("min", "ageStart", "start", "from")
        maximum_keys = ("max", "ageEnd", "end", "to")
        maximum_present = any(key in value for key in maximum_keys)
        maximum = first_value(*maximum_keys)
        try:
            minimum = int(minimum)
            maximum = (
                OPEN_ENDED_AGE_MAX
                if not maximum_present or maximum in (None, "")
                else int(maximum)
            )
        except (TypeError, ValueError):
            return None
    else:
        raw = str(value or "").strip().replace("&", " and ")
        if match := _CLOSED_RANGE.search(raw):
            minimum, maximum = int(match.group(1)), int(match.group(2))
        elif match := _OPEN_RANGE.search(raw):
            minimum, maximum = int(match.group(1)), OPEN_ENDED_AGE_MAX
        elif match := _PREFIX_OPEN_RANGE.search(raw):
            minimum, maximum = int(match.group(1)), OPEN_ENDED_AGE_MAX
        elif _EXACT_AGE.fullmatch(raw):
            minimum = maximum = int(raw)
        else:
            return None
    if minimum < 1 or minimum > OPEN_ENDED_AGE_MAX:
        return None
    maximum = min(maximum, OPEN_ENDED_AGE_MAX)
    if maximum < minimum:
        return None
    return minimum, maximum


def age_range_dict(value):
    parsed = normalize_age_range(value)
    return {"min": parsed[0], "max": parsed[1]} if parsed else None
