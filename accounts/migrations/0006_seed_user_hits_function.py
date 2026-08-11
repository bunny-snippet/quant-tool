from django.db import migrations


PERMISSION = (
    "user_hits.view",
    "View user hits",
    "Tracking",
    "View date-wise user hits and completes split by device type.",
)
ROLE_SLUGS = ("team-lead", "manager", "admin", "super-admin")


def add_user_hits_permission(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")
    code, name, module, description = PERMISSION
    function, _ = AccessFunction.objects.update_or_create(
        code=code,
        defaults={"name": name, "module": module, "description": description, "is_active": True},
    )
    for role in Role.objects.filter(slug__in=ROLE_SLUGS):
        RoleFunctionPermission.objects.update_or_create(
            role=role, function=function, defaults={"allowed": True}
        )


def remove_user_hits_permission(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    AccessFunction.objects.filter(code=PERMISSION[0]).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0005_seed_project_column_functions")]
    operations = [migrations.RunPython(add_user_hits_permission, remove_user_hits_permission)]
