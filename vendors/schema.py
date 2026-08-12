from drf_spectacular.extensions import OpenApiAuthenticationExtension
from django.db import DatabaseError


class VendorAPIKeyAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "vendors.authentication.VendorAPIKeyAuthentication"
    name = "VendorApiKeyAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "External-supplier API key. The authenticated supplier's permissions, client grants, survey rules and CPI cuts are applied automatically.",
        }


PROVIDER_TAGS = {
    "innovatemr": {"InnovateMR APIs"},
    "rfg": {"RFG APIs", "RFG Callbacks"},
}


def configured_upstream_provider_keys():
    """Providers that have a live, active client connection in this database."""
    try:
        from .models import ClientIntegration

        values = ClientIntegration.objects.filter(
            is_active=True,
            client__is_active=True,
        ).values_list("provider_code", flat=True)
        return {str(value or "").lower().replace("-", "").replace("_", "") for value in values}
    except DatabaseError:
        # Schema generation must remain available during first-install/migration
        # windows. Runtime API resolution still rejects missing integrations.
        return None


def filter_unconfigured_upstream_provider_endpoints(endpoints):
    configured = configured_upstream_provider_keys()
    if configured is None:
        return endpoints
    filtered = []
    for endpoint in endpoints:
        path = endpoint[0]
        if "/upstream-explorer/{client_code}/innovatemr/" in path and "innovatemr" not in configured:
            continue
        if (
            "/upstream-explorer/{client_code}/rfg/" in path
            or path.rstrip("/") == "/survey/rfg/callback"
        ) and "rfg" not in configured:
            continue
        filtered.append(endpoint)
    return filtered


def remove_unconfigured_upstream_provider_tags(result, generator, request, public):
    configured = configured_upstream_provider_keys()
    if configured is None:
        return result
    hidden_tags = {
        tag
        for provider, tags in PROVIDER_TAGS.items()
        if provider not in configured
        for tag in tags
    }
    result["tags"] = [tag for tag in result.get("tags", []) if tag.get("name") not in hidden_tags]
    return result
