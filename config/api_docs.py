"""Authentication helpers for the internal API documentation.

The documentation has two independent gates:

* the browser must carry an authenticated Django admin/super-admin session; and
* the request must pass the documentation-only HTTP Basic challenge.

The Basic credentials are read from environment variables and are never part of
the generated OpenAPI document.
"""

import base64
import binascii
import secrets

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse, HttpResponseForbidden
from rest_framework.permissions import BasePermission


DOCUMENTATION_ADMIN_ROLE_SLUGS = frozenset({"admin", "super-admin", "superadmin"})


def is_documentation_admin(user) -> bool:
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser or user.is_staff:
        return True
    profile = getattr(user, "employee_profile", None)
    role = getattr(profile, "role", None)
    return bool(role and role.is_active and role.slug.lower() in DOCUMENTATION_ADMIN_ROLE_SLUGS)


def _basic_credentials(request) -> tuple[str, str] | None:
    authorization = str(request.META.get("HTTP_AUTHORIZATION", "") or "").strip()
    scheme, separator, encoded = authorization.partition(" ")
    if not separator or scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    username, separator, password = decoded.partition(":")
    return (username, password) if separator else None


def _basic_challenge(message: str = "API documentation credentials are required.") -> HttpResponse:
    response = HttpResponse(message, status=401, content_type="text/plain; charset=utf-8")
    response["WWW-Authenticate"] = 'Basic realm="Survey API documentation", charset="UTF-8"'
    response["Cache-Control"] = "no-store"
    return response


class DocumentationProtectionMixin:
    """Protect schema, Swagger UI and ReDoc before DRF renders any content."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        if not is_documentation_admin(request.user):
            return HttpResponseForbidden("API documentation is restricted to admin accounts.")

        expected_username = str(settings.API_DOCS_BASIC_USERNAME or "")
        expected_password = str(settings.API_DOCS_BASIC_PASSWORD or "")
        if not expected_username or not expected_password:
            return HttpResponse(
                "API documentation password protection is not configured.",
                status=503,
                content_type="text/plain; charset=utf-8",
            )

        supplied = _basic_credentials(request)
        if supplied is None:
            return _basic_challenge()
        supplied_username, supplied_password = supplied
        if not (
            secrets.compare_digest(supplied_username, expected_username)
            and secrets.compare_digest(supplied_password, expected_password)
        ):
            return _basic_challenge("Invalid API documentation credentials.")
        return super().dispatch(request, *args, **kwargs)


class IsDocumentationAdmin(BasePermission):
    message = "This operation is restricted to admin and super-admin accounts."

    def has_permission(self, request, view):
        return is_documentation_admin(request.user)
