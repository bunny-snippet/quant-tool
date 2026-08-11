from django.db import migrations


FUNCTIONS = [
    ("clients.view", "View clients", "Vendors & allocations", "View client and integration metadata."),
    ("clients.manage", "Manage clients", "Vendors & allocations", "Create and update clients and integration metadata."),
    ("vendors.view", "View vendor policies", "Vendors & allocations", "View internal/external vendor commercial policies."),
    ("vendors.manage", "Manage vendor policies", "Vendors & allocations", "Configure vendor CPI policies."),
    ("allocations.view", "View allocations", "Vendors & allocations", "View client and survey quantity allocations."),
    ("allocations.manage", "Manage allocations", "Vendors & allocations", "Create and update client and survey allocations."),
    ("respondents.create", "Create vendor respondents", "Vendors & allocations", "Create respondents below an internal vendor."),
]


def add_functions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")
    role = Role.objects.filter(slug="super-admin").first()
    for code, name, module, description in FUNCTIONS:
        function, _ = AccessFunction.objects.update_or_create(
            code=code,
            defaults={"name": name, "module": module, "description": description, "is_active": True},
        )
        if role:
            RoleFunctionPermission.objects.update_or_create(
                role=role,
                function=function,
                defaults={"allowed": True},
            )


def remove_functions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    AccessFunction.objects.filter(code__in=[code for code, *_ in FUNCTIONS]).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0006_seed_user_hits_function")]
    operations = [migrations.RunPython(add_functions, remove_functions)]
