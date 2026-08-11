from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from accounts.models import EmployeeProfile

from .models import VendorAPIKey
from .security import digest_api_key


class VendorAPIKeyAuthentication(BaseAuthentication):
    """Authenticate an external supplier using X-API-Key or Authorization: Api-Key."""

    keyword = "Api-Key"

    def authenticate(self, request):
        raw_key = request.META.get("HTTP_X_API_KEY", "").strip()
        authorization = request.META.get("HTTP_AUTHORIZATION", "").strip()
        if not raw_key and authorization:
            parts = authorization.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == self.keyword.lower():
                raw_key = parts[1].strip()
        if not raw_key:
            return None

        api_key = VendorAPIKey.objects.select_related(
            "vendor", "vendor__employee_profile", "vendor__vendor_commercial_profile"
        ).filter(key_hash=digest_api_key(raw_key)).first()
        now = timezone.now()
        if not api_key or not api_key.is_active or api_key.revoked_at:
            raise AuthenticationFailed("Invalid or revoked supplier API key.")
        if api_key.expires_at and api_key.expires_at <= now:
            raise AuthenticationFailed("Vendor API key has expired.")
        vendor = api_key.vendor
        if not vendor.is_active:
            raise AuthenticationFailed("Vendor account is inactive.")
        profile = getattr(vendor, "employee_profile", None)
        if not profile or profile.account_type != EmployeeProfile.AccountType.EXTERNAL_VENDOR:
            raise AuthenticationFailed("API key is not assigned to an external supplier.")
        commercial = getattr(vendor, "vendor_commercial_profile", None)
        if not commercial or not commercial.is_active or not commercial.api_access_enabled:
            raise AuthenticationFailed("API delivery is not enabled for this vendor.")

        VendorAPIKey.objects.filter(pk=api_key.pk).update(last_used_at=now)
        api_key.last_used_at = now
        return vendor, api_key

    def authenticate_header(self, request):
        return self.keyword
