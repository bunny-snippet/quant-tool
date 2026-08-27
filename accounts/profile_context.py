"""Request-local loading for the account profile used by access code."""

from .models import EmployeeProfile
from .request_cache import request_cached


PROFILE_RELATED_FIELDS = (
    "role",
    "created_by",
    "created_by__employee_profile__role",
    "organization_unit",
    "organization_unit__workspace_owner",
    "organization_unit__workspace_owner__employee_profile__role",
    "organization_unit__parent",
    "organization_unit__parent__parent",
)


def employee_profile_for_user(user):
    """Return one enriched profile snapshot per user and HTTP request."""

    if not user or not getattr(user, "is_authenticated", False) or not user.pk:
        return None

    def load():
        profile = (
            EmployeeProfile.objects.select_related(*PROFILE_RELATED_FIELDS)
            .filter(user_id=user.pk)
            .first()
        )
        # Keep Django's reverse one-to-one cache aligned so remaining direct
        # ``user.employee_profile`` access reuses the same enriched row.
        user._state.fields_cache["employee_profile"] = profile
        return profile

    return request_cached(("employee-profile-context", user.pk), load)
