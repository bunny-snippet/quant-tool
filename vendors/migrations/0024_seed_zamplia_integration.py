from django.db import migrations


def seed_zamplia(apps, schema_editor):
    Client = apps.get_model("vendors", "Client")
    ClientIntegration = apps.get_model("vendors", "ClientIntegration")

    client, _ = Client.objects.update_or_create(
        code="zamplia",
        defaults={
            "name": "Zamplia",
            "provider_code": "zamplia",
            "is_active": True,
        },
    )
    ClientIntegration.objects.update_or_create(
        client=client,
        name="Zamplia Supply",
        defaults={
            "provider_code": "zamplia",
            "base_url": "https://surveysupply.zamplia.com/api/v1",
            "credential_env_keys": {
                "client_id": "ZAMPLIA_CLIENT_ID",
                "token": "ZAMPLIA_ZAMP_KEY",
            },
            "config": {
                "timeout_seconds": 30,
                "detail_refresh_batch": 20,
                "participant_base_url": "https://zampparticipant.zamplia.com/",
            },
            "sync_interval_seconds": 300,
            "detail_refresh_batch": 20,
            "scheduled_sync_enabled": False,
            "is_active": True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [("vendors", "0023_switch_unimarket_to_production")]

    operations = [migrations.RunPython(seed_zamplia, migrations.RunPython.noop)]
