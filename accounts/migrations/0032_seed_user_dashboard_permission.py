from django.db import migrations


def seed_user_dashboard_permission(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")

    function, _ = AccessFunction.objects.update_or_create(
        code="user_dashboard.view",
        defaults={
            "name": "View User Dashboard page and performance",
            "module": "User Dashboard - Page & navigation",
            "description": (
                "Open the monthly employee Final ID performance dashboard, "
                "display its sidebar item and read its scoped API."
            ),
            "is_active": True,
        },
    )
    for role in Role.objects.filter(slug__in=("super-admin", "superadmin"), is_active=True):
        RoleFunctionPermission.objects.update_or_create(
            role=role,
            function=function,
            defaults={"allowed": True},
        )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0031_seed_toluna_notification_permissions")]
    operations = [
        migrations.RunPython(seed_user_dashboard_permission, migrations.RunPython.noop),
    ]
