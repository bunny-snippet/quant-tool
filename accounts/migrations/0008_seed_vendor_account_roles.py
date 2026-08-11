from django.db import migrations


EXTERNAL_DEFAULT_CODES = [
    "dashboard.view",
    "projects.view",
    "survey_details.view",
    "survey_links.copy",
    "attempts.view",
    "attempts.export",
    "user_hits.view",
    "projects.column.project_id",
    "projects.column.survey",
    "projects.column.market",
    "projects.column.completes",
    "projects.column.cpi",
    "projects.column.loi_ir",
    "projects.column.entry_link",
    "projects.column.modified",
    "projects.column.actions",
]

EXTERNAL_FORBIDDEN_CODES = [
    "access.manage",
    "permissions.view",
    "roles.view", "roles.create", "roles.update", "roles.delete",
    "users.manage", "users.view", "users.create", "users.update", "users.delete",
    "respondents.create",
    "clients.manage", "vendors.manage", "allocations.manage",
    "sync.run",
]


def seed_vendor_roles(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    EmployeeProfile = apps.get_model("accounts", "EmployeeProfile")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")
    UserFunctionOverride = apps.get_model("accounts", "UserFunctionOverride")

    external_role, _ = Role.objects.update_or_create(
        slug="external-vendor",
        defaults={
            "name": "External Vendor",
            "description": "Safe default project and tracking access for terminal external vendor accounts.",
            "rank": 15,
            "is_system": True,
            "is_active": True,
        },
    )
    external_functions = AccessFunction.objects.filter(code__in=EXTERNAL_DEFAULT_CODES)
    external_role.function_assignments.all().delete()
    for function in external_functions:
        RoleFunctionPermission.objects.create(role=external_role, function=function, allowed=True)

    admin_role = Role.objects.filter(slug="admin").first()
    respondent_function = AccessFunction.objects.filter(code="respondents.create").first()
    if admin_role and respondent_function:
        RoleFunctionPermission.objects.update_or_create(
            role=admin_role,
            function=respondent_function,
            defaults={"allowed": True},
        )

    external_profiles = EmployeeProfile.objects.filter(account_type="external_vendor")
    external_profiles.update(role=external_role, company_name="", department="")
    if admin_role:
        EmployeeProfile.objects.filter(account_type="internal_vendor").update(role=admin_role)
    UserFunctionOverride.objects.filter(
        user_id__in=external_profiles.values_list("user_id", flat=True),
        function__code__in=EXTERNAL_FORBIDDEN_CODES,
    ).delete()


def remove_vendor_role(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Role.objects.filter(slug="external-vendor").delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0007_seed_vendor_allocation_functions")]
    operations = [migrations.RunPython(seed_vendor_roles, remove_vendor_role)]
