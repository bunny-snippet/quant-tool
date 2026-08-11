from django.db import migrations

from accounts.function_catalog import FUNCTION_CATALOG


NEW_CODES = {
    "termination_reasons.filter.status",
    "termination_reasons.filter.client",
    "termination_reasons.filters.clear",
    "termination_reasons.summary",
    "termination_reasons.control.pagination",
    "termination_reasons.action.details",
    "termination_reasons.column.rid",
    "termination_reasons.column.survey",
    "termination_reasons.column.client",
    "termination_reasons.column.respondent",
    "termination_reasons.column.status",
    "termination_reasons.column.ended",
    "termination_reasons.column.actions",
}


def seed_termination_reason_list_permissions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")
    for code, name, module, description, default_roles in FUNCTION_CATALOG:
        if code not in NEW_CODES:
            continue
        function, _ = AccessFunction.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "module": module,
                "description": description,
                "is_active": True,
            },
        )
        for role in Role.objects.filter(slug__in=default_roles, is_active=True):
            RoleFunctionPermission.objects.get_or_create(
                role=role,
                function=function,
                defaults={"allowed": True},
            )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0017_seed_termination_reason_permissions")]
    operations = [migrations.RunPython(seed_termination_reason_list_permissions, migrations.RunPython.noop)]
