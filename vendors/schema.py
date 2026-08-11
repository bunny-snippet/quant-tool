from drf_spectacular.extensions import OpenApiAuthenticationExtension


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
