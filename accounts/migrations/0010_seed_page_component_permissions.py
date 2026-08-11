from django.db import migrations

from accounts.function_catalog import FUNCTION_CATALOG


EXTERNAL_VENDOR_COMPONENT_CODES = (
    "projects.filter.search", "projects.filter.country", "projects.filter.status", "projects.filter.client",
    "projects.filter.date", "projects.filters.clear", "projects.control.page_size", "projects.control.pagination",
    "studies.filter.search", "studies.filter.user", "studies.filter.status", "studies.filter.date",
    "studies.filters.clear", "studies.control.page_size", "studies.control.pagination",
    "studies.column.project_id", "studies.column.survey_id", "studies.column.respondent_id",
    "studies.column.user", "studies.column.device", "studies.column.ip", "studies.column.loi",
    "studies.column.status", "studies.column.start", "studies.column.end",
    "user_hits.filter.search", "user_hits.filter.user", "user_hits.filter.date", "user_hits.filters.clear",
    "user_hits.summary", "user_hits.control.page_size", "user_hits.control.pagination",
    "user_hits.column.user", "user_hits.column.date", "user_hits.column.hits", "user_hits.column.completes",
)


def sync_page_component_permissions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")

    for code, name, module, description, default_roles in FUNCTION_CATALOG:
        function, created = AccessFunction.objects.update_or_create(
            code=code,
            defaults={"name": name, "module": module, "description": description, "is_active": True},
        )
        if created:
            for role in Role.objects.filter(slug__in=default_roles, is_active=True):
                RoleFunctionPermission.objects.get_or_create(
                    role=role, function=function, defaults={"allowed": True},
                )

    external_role = Role.objects.filter(slug="external-vendor", is_active=True).first()
    if external_role:
        for function in AccessFunction.objects.filter(code__in=EXTERNAL_VENDOR_COMPONENT_CODES, is_active=True):
            RoleFunctionPermission.objects.get_or_create(
                role=external_role, function=function, defaults={"allowed": True},
            )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0009_seed_project_action_functions")]
    operations = [migrations.RunPython(sync_page_component_permissions, migrations.RunPython.noop)]
