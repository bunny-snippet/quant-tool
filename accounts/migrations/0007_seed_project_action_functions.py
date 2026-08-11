from django.db import migrations


FUNCTIONS = (
    (
        "projects.export",
        "Export projects CSV",
        "Projects · Actions",
        "Download all projects matching the current filters.",
        ("employee", "team-lead", "manager", "admin", "super-admin"),
    ),
    (
        "projects.filter.cpi",
        "Use CPI filter and sorting",
        "Projects · Filters",
        "Filter projects by CPI range and sort by CPI.",
        ("admin", "super-admin"),
    ),
)


def add_functions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")
    for code, name, module, description, default_roles in FUNCTIONS:
        function, _ = AccessFunction.objects.update_or_create(
            code=code,
            defaults={"name": name, "module": module, "description": description, "is_active": True},
        )
        for role in Role.objects.filter(slug__in=default_roles):
            RoleFunctionPermission.objects.update_or_create(
                role=role,
                function=function,
                defaults={"allowed": True},
            )


def remove_functions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    AccessFunction.objects.filter(code__in=[item[0] for item in FUNCTIONS]).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0006_seed_user_hits_function")]
    operations = [migrations.RunPython(add_functions, remove_functions)]
