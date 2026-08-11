import html
import re


RFG_INTERNAL_MARKER_RE = re.compile(
    r"\s*%%\s*(?:rfg[_-]?)?\d{2,}\s*%%\s*",
    re.IGNORECASE,
)


def clean_rfg_display_text(value) -> str:
    """Remove RFG's embedded numeric UI markers from respondent-facing copy."""
    text = html.unescape(str(value or ""))
    text = RFG_INTERNAL_MARKER_RE.sub(" ", text)
    text = re.sub(r"\s+([?.!,;:])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_rfg_options(options) -> list:
    cleaned = []
    for option in options or []:
        if not isinstance(option, dict):
            cleaned.append(option)
            continue
        item = dict(option)
        if "OptionText" in item:
            item["OptionText"] = clean_rfg_display_text(item["OptionText"])
        cleaned.append(item)
    return cleaned
