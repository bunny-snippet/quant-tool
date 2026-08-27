from django.db import migrations

from accounts.function_catalog import FUNCTION_CATALOG


NEW_CODES = {
    code for code, _name, _module, _description, _roles in FUNCTION_CATALOG
    if code.startswith("toluna_notifications.")
}


def seed_toluna_notification_permissions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")
    for code, name, module, description, default_roles in FUNCTION_CATALOG:
        if code not in NEW_CODES:
            continue
        function, _created = AccessFunction.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "module": module,
                "description": description,
                "is_active": True,
            },
        )
        for role in Role.objects.filter(slug__in=default_roles, is_active=True):
            RoleFunctionPermission.objects.update_or_create(
                role=role,
                function=function,
                defaults={"allowed": True},
            )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0030_deactivate_supplier_quantity_permissions")]
    operations = [
        migrations.RunPython(
            seed_toluna_notification_permissions,
            migrations.RunPython.noop,
        ),
    ]
