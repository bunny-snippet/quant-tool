from .access import effective_permission_codes


def access_context(request):
    if not request.user.is_authenticated:
        return {"access_codes": set(), "current_employee_profile": None}
    profile = getattr(request.user, "employee_profile", None)
    return {"access_codes": effective_permission_codes(request.user), "current_employee_profile": profile}

