from django.contrib.auth import logout
from django.contrib.auth.views import redirect_to_login

from accounts.models import EmployeeProfile
from accounts.profile_context import employee_profile_for_user


class VendorPanelAccessMiddleware:
    """Immediately end external-vendor sessions when their policy is API-only."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        profile = employee_profile_for_user(user)
        if getattr(profile, "account_type", "") == EmployeeProfile.AccountType.EXTERNAL_VENDOR:
            commercial = getattr(user, "vendor_commercial_profile", None)
            if not commercial or not commercial.is_active or not commercial.panel_access_enabled:
                is_api_request = request.path.startswith("/api/")
                logout(request)
                if not is_api_request:
                    return redirect_to_login(request.get_full_path())
        return self.get_response(request)
