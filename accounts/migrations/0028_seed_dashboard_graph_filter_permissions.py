from django.db import migrations


FUNCTIONS = (
    (
        "dashboard.graph.traffic_filters",
        "Filter Traffic dashboard graph",
        "Use an independent client and time-range filter on the Entrants, Completes and Conversion graph.",
        ("employee", "employees", "team-lead", "tl", "manager", "admin", "super-admin"),
    ),
    (
        "dashboard.graph.finance_filters",
        "Filter Revenue dashboard graph",
        "Use an independent client and time-range filter on the Revenue and RPC graph.",
        ("admin", "super-admin"),
    ),
)


def seed_permissions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")
    for code, name, description, role_slugs in FUNCTIONS:
        function, _created = AccessFunction.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "module": "Dashboard - Graph filters",
                "description": description,
                "is_active": True,
            },
        )
        for role in Role.objects.filter(slug__in=role_slugs, is_active=True):
            RoleFunctionPermission.objects.update_or_create(
                role=role,
                function=function,
                defaults={"allowed": True},
            )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0027_seed_dashboard_financial_permissions")]
    operations = [migrations.RunPython(seed_permissions, migrations.RunPython.noop)]
