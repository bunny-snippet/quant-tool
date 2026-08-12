from django.db import migrations


CODE = "prescreener_data.export"


def seed_permission(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")
    function, _created = AccessFunction.objects.update_or_create(
        code=CODE,
        defaults={
            "name": "Export Prescreened Data workbook",
            "module": "Prescreened Data - Actions",
            "description": "Export every filtered submission and its linked answers as a formatted Excel workbook.",
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
    dependencies = [("accounts", "0025_supplier_product_terminology")]
    operations = [migrations.RunPython(seed_permission, migrations.RunPython.noop)]
