from django.db import migrations


SUPER_ADMIN_SLUGS = ("super-admin", "superadmin")


def restrict_dashboard(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")
    UserFunctionOverride = apps.get_model("accounts", "UserFunctionOverride")

    functions = list(AccessFunction.objects.filter(code__startswith="dashboard."))
    if not functions:
        return

    RoleFunctionPermission.objects.filter(function__in=functions).exclude(
        role__slug__in=SUPER_ADMIN_SLUGS
    ).delete()
    for role in Role.objects.filter(slug__in=SUPER_ADMIN_SLUGS, is_active=True):
        for function in functions:
            RoleFunctionPermission.objects.update_or_create(
                role=role,
                function=function,
                defaults={"allowed": True},
            )

    UserFunctionOverride.objects.filter(function__in=functions).exclude(
        user__is_superuser=True
    ).exclude(
        user__employee_profile__role__slug__in=SUPER_ADMIN_SLUGS
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0028_seed_dashboard_graph_filter_permissions")]

    operations = [
        migrations.RunPython(restrict_dashboard, migrations.RunPython.noop),
    ]

