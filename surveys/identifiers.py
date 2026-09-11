"""Collision-resistant platform identifier generators.

Identifier types intentionally use separate shapes: newly generated PIDs are
twelve or thirteen characters, RID is ten characters, and the panelist UID is
nineteen characters with hyphens.  Legacy six-to-nine-character PIDs remain
valid so already copied survey links keep working.
"""

import secrets
import string


PID_ALPHABET = string.ascii_letters + string.digits
GENERATED_PID_LENGTHS = (12, 13)
LEGACY_PID_LENGTHS = range(6, 10)
PLATFORM_PID_MAX_LENGTH = max(GENERATED_PID_LENGTHS)


def is_valid_platform_pid(value: str) -> bool:
    """Accept legacy PIDs and the new shape without overlapping RID length."""

    return bool(
        value
        and value.isalnum()
        and (
            len(value) in LEGACY_PID_LENGTHS
            or len(value) in GENERATED_PID_LENGTHS
        )
    )


def generate_platform_pid(length: int | None = None) -> str:
    """Return a 12-13 character PID with upper, lower and numeric characters."""

    length = secrets.choice(GENERATED_PID_LENGTHS) if length is None else length
    if length not in GENERATED_PID_LENGTHS:
        raise ValueError("New PID length must be 12 or 13 characters.")
    characters = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        *(secrets.choice(PID_ALPHABET) for _ in range(length - 3)),
    ]
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)
