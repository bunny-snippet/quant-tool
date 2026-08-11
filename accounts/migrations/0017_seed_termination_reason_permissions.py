from django.db import migrations

from accounts.function_catalog import FUNCTION_CATALOG


NEW_CODES = {
    "termination_reasons.view",
    "termination_reasons.filter.rid",
    "termination_reasons.action.refresh",
    "termination_reasons.field.status",
    "termination_reasons.field.reason",
    "termination_reasons.field.respondent",
    "termination_reasons.field.survey",
    "termination_reasons.field.timing",
    "termination_reasons.field.audit",
}


def seed_termination_reason_permissions(apps, schema_editor):
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
    dependencies = [("accounts", "0016_seed_organization_remove_client_permission")]
    operations = [migrations.RunPython(seed_termination_reason_permissions, migrations.RunPython.noop)]
