"""Provider-neutral prescreener age-band normalization."""

import re


OPEN_ENDED_AGE_MAX = 99

_CLOSED_RANGE = re.compile(r"^(\d{1,3})\s*(?:-|\u2013|\u2014|to)\s*(\d{1,3})$", re.I)
_OPEN_RANGE = re.compile(r"^(\d{1,3})\s*(?:\+|and\s+older|or\s+older)$", re.I)
_EXACT_AGE = re.compile(r"^\d{1,3}$")


def normalize_age_range(value):
    """Return ``(minimum, maximum)`` with every open maximum capped at 99."""

    if isinstance(value, dict):
        label = str(value.get("OptionText") or value.get("label") or "").strip()
        # A range-bearing label is authoritative, but a bare label such as
        # ``65`` must not turn ``ageStart=65, ageEnd=None`` into the exact age
        # 65; missing metadata maxima are open-ended and therefore end at 99.
        if label and (_CLOSED_RANGE.fullmatch(label) or _OPEN_RANGE.fullmatch(label)):
            if (parsed := normalize_age_range(label)) is not None:
                return parsed
        minimum = value.get("min", value.get("ageStart"))
        maximum_present = "max" in value or "ageEnd" in value
        maximum = value.get("max") if "max" in value else value.get("ageEnd")
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
        raw = str(value or "").strip()
        if match := _CLOSED_RANGE.fullmatch(raw):
            minimum, maximum = int(match.group(1)), int(match.group(2))
        elif match := _OPEN_RANGE.fullmatch(raw):
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
