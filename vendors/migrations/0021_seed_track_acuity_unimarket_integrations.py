from django.db import migrations


PROVIDERS = (
    {
        "client": {"code": "track-opinion", "name": "Track Opinion", "provider_code": "track_opinion"},
        "integration": {
            "name": "Track Opinion Supply",
            "provider_code": "track_opinion",
            "base_url": "https://stagingsupply.opinionest.com",
            "credential_env_keys": {"token": "TRACK_OPINION_API_KEY"},
            "config": {
                "timeout_seconds": 30,
                "detail_refresh_batch": 20,
                "configure_redirects": True,
                "public_callback_base": "https://exchange.api-grid.com",
            },
        },
    },
    {
        "client": {"code": "acuity-analytics", "name": "Acuity Analytics", "provider_code": "acuity"},
        "integration": {
            "name": "Acuity Analytics Supply",
            "provider_code": "acuity",
            "base_url": "https://api.acuitykp.online",
            "credential_env_keys": {
                "supplier_id": "ACUITY_SUPPLIER_ID",
                "token": "ACUITY_API_TOKEN",
            },
            "config": {
                "timeout_seconds": 45,
                "detail_refresh_batch": 20,
                "callback_urls": {
                    "complete": "https://exchange.api-grid.com/survey?status=1&rid=[identifier]",
                    "terminate": "https://exchange.api-grid.com/survey?status=2&rid=[identifier]",
                    "over_quota": "https://exchange.api-grid.com/survey?status=3&rid=[identifier]",
                    "security": "https://exchange.api-grid.com/survey?status=4&rid=[identifier]",
                },
            },
        },
    },
    {
        "client": {"code": "unimarket", "name": "UniMarket", "provider_code": "unimarket"},
        "integration": {
            "name": "UniMarket Supply",
            "provider_code": "unimarket",
            "base_url": "https://stg-api.supplier.unimrktresponse.net",
            "credential_env_keys": {"token": "UNIMARKET_API_KEY"},
            "config": {
                "timeout_seconds": 30,
                "detail_refresh_batch": 20,
                "country_codes": ["US", "CA", "GB", "AU", "DE", "FR", "ES", "MX", "CN", "NL", "BR", "IT", "IN"],
                "callback_urls": {
                    "complete": "https://exchange.api-grid.com/survey?status=1&rid={uid}",
                    "terminate": "https://exchange.api-grid.com/survey?status=2&rid={uid}",
                    "over_quota": "https://exchange.api-grid.com/survey?status=3&rid={uid}",
                    "security": "https://exchange.api-grid.com/survey?status=4&rid={uid}",
                },
            },
        },
    },
)


def seed_integrations(apps, schema_editor):
    Client = apps.get_model("vendors", "Client")
    ClientIntegration = apps.get_model("vendors", "ClientIntegration")
    for provider in PROVIDERS:
        client_defaults = dict(provider["client"])
        code = client_defaults.pop("code")
        client, _ = Client.objects.update_or_create(code=code, defaults={**client_defaults, "is_active": True})
        integration_defaults = dict(provider["integration"])
        name = integration_defaults.pop("name")
        ClientIntegration.objects.update_or_create(
            client=client,
            name=name,
            defaults={
                **integration_defaults,
                "sync_interval_seconds": 300,
                "detail_refresh_batch": 20,
                "scheduled_sync_enabled": False,
                "is_active": True,
            },
        )


class Migration(migrations.Migration):
    dependencies = [("vendors", "0020_raise_rfg_inventory_interval")]

    operations = [migrations.RunPython(seed_integrations, migrations.RunPython.noop)]
