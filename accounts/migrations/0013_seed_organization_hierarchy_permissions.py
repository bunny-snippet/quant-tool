from django.db import migrations

from accounts.function_catalog import FUNCTION_CATALOG


NEW_CODES = {
    code for code, *_ in FUNCTION_CATALOG
    if code.startswith("organization.") or code in {"user_hits.filter.shift", "user_hits.column.shift"}
}


def seed_organization_permissions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")

    for code, name, module, description, default_roles in FUNCTION_CATALOG:
        if code not in NEW_CODES:
            continue
        function, _ = AccessFunction.objects.update_or_create(
            code=code,
            defaults={"name": name, "module": module, "description": description, "is_active": True},
        )
        for role in Role.objects.filter(slug__in=default_roles, is_active=True):
            RoleFunctionPermission.objects.get_or_create(
                role=role,
                function=function,
                defaults={"allowed": True},
            )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0012_employeeprofile_organization_unit")]
    operations = [migrations.RunPython(seed_organization_permissions, migrations.RunPython.noop)]
