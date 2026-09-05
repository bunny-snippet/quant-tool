from django.db import migrations


PRODUCTION_BASE_URL = "https://api.supplier.unimrktresponse.net"
STAGING_BASE_URL = "https://stg-api.supplier.unimrktresponse.net"


def switch_unimarket_to_production(apps, schema_editor):
    ClientIntegration = apps.get_model("vendors", "ClientIntegration")
    ClientIntegration.objects.filter(provider_code="unimarket").update(
        base_url=PRODUCTION_BASE_URL,
        credential_env_keys={"token": "UNIMARKET_X_ACCESS_KEY"},
    )


def switch_unimarket_to_staging(apps, schema_editor):
    ClientIntegration = apps.get_model("vendors", "ClientIntegration")
    ClientIntegration.objects.filter(provider_code="unimarket").update(
        base_url=STAGING_BASE_URL,
        credential_env_keys={"token": "UNIMARKET_API_KEY"},
    )


class Migration(migrations.Migration):

    dependencies = [
        ("vendors", "0022_switch_track_opinion_to_production"),
    ]

    operations = [
        migrations.RunPython(
            switch_unimarket_to_production,
            switch_unimarket_to_staging,
        ),
    ]
