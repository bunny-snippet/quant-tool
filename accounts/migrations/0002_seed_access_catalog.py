from django.conf import settings
from django.db import migrations


ROLE_SPECS = [
    ("Employee", "employee", 10, "Standard employee access to survey projects."),
    ("Team Lead", "team-lead", 20, "Team-level survey operations and respondent visibility."),
    ("Manager", "manager", 30, "Operational monitoring and synchronization visibility."),
    ("Admin", "admin", 40, "Employee administration and operational control."),
    ("Super Admin", "super-admin", 50, "Complete workspace and access-control administration."),
]

FUNCTION_SPECS = [
    ("dashboard.view", "View dashboard", "Dashboard", "Open the internal dashboard."),
    ("projects.view", "View projects", "Projects", "Browse the synchronized survey inventory."),
    ("survey_details.view", "View survey details", "Projects", "Open pre-screening and quota details."),
    ("survey_links.copy", "Copy pre-screener links", "Projects", "Copy internal respondent start links."),
    ("attempts.view", "View survey tracking", "Tracking", "View respondent attempts, IPs, statuses and LOI."),
    ("attempts.export", "Export survey tracking", "Tracking", "Export respondent tracking records."),
    ("sync.view", "View synchronization history", "Synchronization", "View inventory synchronization runs."),
    ("sync.run", "Run synchronization", "Synchronization", "Manually trigger an InnovateMR synchronization."),
    ("users.manage", "Manage employees", "Access control", "Create, update, disable and delete employee accounts."),
    ("access.manage", "Manage roles and permissions", "Access control", "Configure roles, functions and user overrides."),
    ("api_docs.view", "View API documentation", "Development", "Open internal Swagger and API documentation."),
]

ROLE_GRANTS = {
    "employee": ["dashboard.view", "projects.view", "survey_details.view", "survey_links.copy"],
    "team-lead": ["dashboard.view", "projects.view", "survey_details.view", "survey_links.copy", "attempts.view"],
    "manager": ["dashboard.view", "projects.view", "survey_details.view", "survey_links.copy", "attempts.view", "sync.view"],
    "admin": ["dashboard.view", "projects.view", "survey_details.view", "survey_links.copy", "attempts.view", "sync.view", "sync.run", "users.manage", "api_docs.view"],
    "super-admin": [code for code, *_ in FUNCTION_SPECS],
}


def seed_access(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")
    EmployeeProfile = apps.get_model("accounts", "EmployeeProfile")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))

    roles = {}
    for name, slug, rank, description in ROLE_SPECS:
        roles[slug], _ = Role.objects.update_or_create(
            slug=slug,
            defaults={"name": name, "rank": rank, "description": description, "is_system": True, "is_active": True},
        )
    functions = {}
    for code, name, module, description in FUNCTION_SPECS:
        functions[code], _ = AccessFunction.objects.update_or_create(
            code=code,
            defaults={"name": name, "module": module, "description": description, "is_active": True},
        )
    for role_slug, codes in ROLE_GRANTS.items():
        for code in codes:
            RoleFunctionPermission.objects.update_or_create(
                role=roles[role_slug], function=functions[code], defaults={"allowed": True}
            )
    for user in User.objects.all():
        role = roles["super-admin"] if user.is_superuser else roles["employee"]
        EmployeeProfile.objects.update_or_create(user=user, defaults={"role": role})


def remove_seeded_access(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    AccessFunction.objects.filter(code__in=[code for code, *_ in FUNCTION_SPECS]).delete()
    Role.objects.filter(slug__in=[slug for _, slug, *_ in ROLE_SPECS]).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]
    operations = [migrations.RunPython(seed_access, remove_seeded_access)]

