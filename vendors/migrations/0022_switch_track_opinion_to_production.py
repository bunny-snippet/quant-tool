from django.db import migrations


def switch_track_opinion_to_production(apps, schema_editor):
    ClientIntegration = apps.get_model("vendors", "ClientIntegration")
    ClientIntegration.objects.filter(
        provider_code="track_opinion",
        base_url="https://stagingsupply.opinionest.com",
    ).update(base_url="https://supply.opinionest.com")


class Migration(migrations.Migration):

    dependencies = [
        ("vendors", "0021_seed_track_acuity_unimarket_integrations"),
    ]

    operations = [
        migrations.RunPython(switch_track_opinion_to_production, migrations.RunPython.noop),
    ]
