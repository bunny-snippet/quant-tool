from .access import effective_permission_codes
from .profile_context import employee_profile_for_user


def access_context(request):
    if not request.user.is_authenticated:
        return {"access_codes": set(), "current_employee_profile": None}
    profile = employee_profile_for_user(request.user)
    return {"access_codes": effective_permission_codes(request.user), "current_employee_profile": profile}
