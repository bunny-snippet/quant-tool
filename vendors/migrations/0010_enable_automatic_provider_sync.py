from django.db import migrations


def enable_automatic_provider_sync(apps, schema_editor):
    ClientIntegration = apps.get_model("vendors", "ClientIntegration")
    ClientIntegration.objects.filter(provider_code="innovatemr", is_active=True).update(
        sync_interval_seconds=150,
        scheduled_sync_enabled=True,
    )
    ClientIntegration.objects.filter(
        provider_code="rfg", is_active=True, last_test_status="success",
    ).update(
        sync_interval_seconds=60,
        scheduled_sync_enabled=True,
    )


class Migration(migrations.Migration):
    dependencies = [("vendors", "0009_clientintegration_provider_config")]
    operations = [migrations.RunPython(enable_automatic_provider_sync, migrations.RunPython.noop)]
