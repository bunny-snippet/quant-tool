from django.db import migrations

from accounts.function_catalog import FUNCTION_CATALOG


def sync_page_component_permissions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")

    for code, name, module, description, default_roles in FUNCTION_CATALOG:
        function, created = AccessFunction.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "module": module,
                "description": description,
                "is_active": True,
            },
        )
        if not created:
            continue
        for role in Role.objects.filter(slug__in=default_roles, is_active=True):
            RoleFunctionPermission.objects.get_or_create(
                role=role,
                function=function,
                defaults={"allowed": True},
            )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0007_seed_project_action_functions")]
    operations = [migrations.RunPython(sync_page_component_permissions, migrations.RunPython.noop)]
