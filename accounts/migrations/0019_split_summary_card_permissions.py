from django.db import migrations

from accounts.function_catalog import FUNCTION_CATALOG


NEW_CODES = {
    code
    for code, _name, _module, _description, _roles in FUNCTION_CATALOG
    if ".card." in code or code in {
        "studies.filter.country",
        "studies.column.country",
        "studies.column.cpi",
    }
}

PARENT_CARD_MAP = {
    "attempts.view": tuple(
        code for code in NEW_CODES
        if code.startswith("studies.card.") and code != "studies.card.revenue"
    ),
    "termination_reasons.summary": tuple(
        code for code in NEW_CODES if code.startswith("termination_reasons.card.")
    ),
    "user_hits.summary": tuple(
        code for code in NEW_CODES if code.startswith("user_hits.card.")
    ),
    "organization.summary": tuple(
        code for code in NEW_CODES if code.startswith("organization.card.")
    ),
    "vendors.summary": tuple(
        code for code in NEW_CODES if code.startswith("vendors.card.")
    ),
}


def split_summary_card_permissions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")
    UserFunctionOverride = apps.get_model("accounts", "UserFunctionOverride")

    functions = {}
    for code, name, module, description, default_roles in FUNCTION_CATALOG:
        if code not in NEW_CODES:
            continue
        function, _created = AccessFunction.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "module": module,
                "description": description,
                "is_active": True,
            },
        )
        functions[code] = function
        for role in Role.objects.filter(slug__in=default_roles, is_active=True):
            RoleFunctionPermission.objects.get_or_create(
                role=role,
                function=function,
                defaults={"allowed": True},
            )

    for parent_code, child_codes in PARENT_CARD_MAP.items():
        parent = AccessFunction.objects.filter(code=parent_code).first()
        if not parent:
            continue
        for assignment in RoleFunctionPermission.objects.filter(function=parent):
            for child_code in child_codes:
                RoleFunctionPermission.objects.update_or_create(
                    role_id=assignment.role_id,
                    function=functions[child_code],
                    defaults={"allowed": assignment.allowed},
                )
        for override in UserFunctionOverride.objects.filter(function=parent):
            for child_code in child_codes:
                UserFunctionOverride.objects.update_or_create(
                    user_id=override.user_id,
                    function=functions[child_code],
                    defaults={"effect": override.effect, "reason": override.reason},
                )

    AccessFunction.objects.filter(
        code__in=(
            "termination_reasons.summary",
            "user_hits.summary",
            "organization.summary",
            "vendors.summary",
        )
    ).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0018_seed_termination_reason_list_permissions")]
    operations = [
        migrations.RunPython(split_summary_card_permissions, migrations.RunPython.noop),
    ]
