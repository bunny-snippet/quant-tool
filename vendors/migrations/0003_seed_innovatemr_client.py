from django.conf import settings
from django.db import migrations


def seed_innovatemr_client(apps, schema_editor):
    Client = apps.get_model("vendors", "Client")
    ClientIntegration = apps.get_model("vendors", "ClientIntegration")
    Survey = apps.get_model("surveys", "Survey")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))

    owner = User.objects.filter(is_superuser=True).order_by("id").first()
    client, _ = Client.objects.get_or_create(
        code="innovatemr",
        defaults={
            "name": "InnovateMR",
            "provider_code": "innovatemr",
            "company_name_match": "InnovateMR",
            "created_by": owner,
        },
    )
    ClientIntegration.objects.get_or_create(
        client=client,
        name="Primary InnovateMR",
        defaults={
            "provider_code": "innovatemr",
            "base_url": "https://supplier.innovatemr.net/api/v2",
            "credential_env_key": "INNOVATEMR_API_TOKEN",
            "scheduled_sync_enabled": False,
            "created_by": owner,
        },
    )
    Survey.objects.filter(company_name__iexact="InnovateMR", client__isnull=True).update(client=client)


class Migration(migrations.Migration):
    dependencies = [
        ("vendors", "0002_vendorsurveyallocation_allocationreservation_and_more"),
        ("surveys", "0009_surveyattempt_survey_allocation"),
    ]
    operations = [migrations.RunPython(seed_innovatemr_client, migrations.RunPython.noop)]
