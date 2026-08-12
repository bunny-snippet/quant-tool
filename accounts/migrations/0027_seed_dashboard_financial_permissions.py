from django.db import migrations


FUNCTIONS = (
    (
        "dashboard.card.average_cpi",
        "Show Average CPI card",
        "Display average immutable hit-time CPI across completed journeys.",
    ),
    (
        "dashboard.card.rpc",
        "Show Revenue per hit card",
        "Display visible completed revenue divided by total respondent hits.",
    ),
)


def seed_permissions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")
    for code, name, description in FUNCTIONS:
        function, _created = AccessFunction.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "module": "Dashboard - Summary cards",
                "description": description,
                "is_active": True,
            },
        )
        for role in Role.objects.filter(slug__in=("admin", "super-admin"), is_active=True):
            RoleFunctionPermission.objects.update_or_create(
                role=role,
                function=function,
                defaults={"allowed": True},
            )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0026_seed_prescreener_export_permission")]
    operations = [migrations.RunPython(seed_permissions, migrations.RunPython.noop)]
