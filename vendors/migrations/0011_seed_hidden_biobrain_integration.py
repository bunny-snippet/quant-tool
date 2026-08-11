from django.db import migrations


def seed_biobrain(apps, schema_editor):
    Client = apps.get_model("vendors", "Client")
    ClientIntegration = apps.get_model("vendors", "ClientIntegration")
    client, created = Client.objects.get_or_create(
        code="biobrain",
        defaults={
            "name": "BioBrain",
            "provider_code": "biobrain",
            "company_name_match": "BioBrain",
            "is_active": False,
        },
    )
    if not created and client.provider_code not in {"biobrain", "voqall"}:
        return
    ClientIntegration.objects.get_or_create(
        client=client,
        name="Primary BioBrain",
        defaults={
            "provider_code": "biobrain",
            "base_url": "https://partner-api.voqall.com/api/v1/surveys",
            "credential_env_key": "BIOBRAIN_API_KEY",
            "supplier_code": "1000",
            "inventory_endpoint": "",
            "quota_endpoint_template": "https://partner-api.voqall.com/api/v1/survey-quotas/{survey_id}",
            "targeting_endpoint_template": "https://partner-api.voqall.com/api/v1/survey-qualifications/{survey_id}",
            "auth_header_name": "EQ-PARTNER-ACCESS-KEY",
            "inventory_result_key": "Surveys",
            "quota_result_key": "Quotas",
            "targeting_result_key": "Qualifications",
            "scheduled_sync_enabled": True,
            "sync_interval_seconds": 60,
            "detail_refresh_batch": 3,
            "is_active": True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [("vendors", "0010_enable_automatic_provider_sync")]
    operations = [migrations.RunPython(seed_biobrain, migrations.RunPython.noop)]
