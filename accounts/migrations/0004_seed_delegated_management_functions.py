from django.db import migrations


FUNCTIONS = [
    ("permissions.view", "View permission catalog", "Access control", "View functions that may be delegated."),
    ("roles.view", "View roles", "Access control", "View roles available within the user's scope."),
    ("roles.create", "Create subordinate roles", "Access control", "Create roles using only functions the creator already has."),
    ("roles.update", "Update owned roles", "Access control", "Update roles created by the current user."),
    ("roles.delete", "Delete owned roles", "Access control", "Delete unused roles created by the current user."),
    ("users.view", "View subordinate users", "Access control", "View users created below the current account."),
    ("users.create", "Create subordinate users", "Access control", "Create employee, internal-vendor or external-vendor accounts."),
    ("users.update", "Update subordinate users", "Access control", "Update roles and overrides for subordinate accounts."),
    ("users.delete", "Delete subordinate users", "Access control", "Delete subordinate accounts."),
]

GRANTS = {
    "team-lead": ["permissions.view", "roles.view", "users.view"],
    "manager": ["permissions.view", "roles.view", "users.view", "users.create", "users.update"],
    "admin": [code for code, *_ in FUNCTIONS],
    "super-admin": [code for code, *_ in FUNCTIONS],
}


def add_functions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")
    catalog = {}
    for code, name, module, description in FUNCTIONS:
        catalog[code], _ = AccessFunction.objects.update_or_create(
            code=code, defaults={"name": name, "module": module, "description": description, "is_active": True}
        )
    for role_slug, codes in GRANTS.items():
        role = Role.objects.get(slug=role_slug)
        for code in codes:
            RoleFunctionPermission.objects.update_or_create(
                role=role, function=catalog[code], defaults={"allowed": True}
            )


def remove_functions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    AccessFunction.objects.filter(code__in=[code for code, *_ in FUNCTIONS]).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_employeeprofile_account_type_and_more")]
    operations = [migrations.RunPython(add_functions, remove_functions)]

