from django.db import migrations


def raise_rfg_inventory_interval(apps, schema_editor):
    ClientIntegration = apps.get_model("vendors", "ClientIntegration")
    ClientIntegration.objects.filter(
        provider_code="rfg",
        sync_interval_seconds__lt=600,
    ).update(sync_interval_seconds=600)


class Migration(migrations.Migration):
    dependencies = [
        ("vendors", "0019_vendorapikey_callback_secret_last_four_and_more"),
    ]

    operations = [
        migrations.RunPython(
            raise_rfg_inventory_interval,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
