from django.db import migrations


COLUMNS = [
    ("projects.column.project_id", "Show Project ID column", "Projects · Table columns", "Display internal and InnovateMR project identifiers."),
    ("projects.column.survey", "Show Survey column", "Projects · Table columns", "Display survey name and company."),
    ("projects.column.market", "Show Market column", "Projects · Table columns", "Display country and language."),
    ("projects.column.completes", "Show Completes column", "Projects · Table columns", "Display completion progress and sample size."),
    ("projects.column.cpi", "Show CPI column", "Projects · Table columns", "Display cost per interview."),
    ("projects.column.loi_ir", "Show LOI / IR column", "Projects · Table columns", "Display length of interview and incidence rate."),
    ("projects.column.entry_link", "Show Entry link column", "Projects · Table columns", "Display the internal pre-screener copy action."),
    ("projects.column.modified", "Show Modified column", "Projects · Table columns", "Display source timestamp and survey status."),
    ("projects.column.actions", "Show Actions column", "Projects · Table columns", "Display the survey details action."),
]


def add_column_functions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")
    functions = []
    for code, name, module, description in COLUMNS:
        function, _ = AccessFunction.objects.update_or_create(
            code=code, defaults={"name": name, "module": module, "description": description, "is_active": True}
        )
        functions.append(function)
    for role in Role.objects.filter(slug__in=["employee", "team-lead", "manager", "admin", "super-admin"]):
        for function in functions:
            RoleFunctionPermission.objects.update_or_create(
                role=role, function=function, defaults={"allowed": True}
            )


def remove_column_functions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    AccessFunction.objects.filter(code__in=[code for code, *_ in COLUMNS]).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0004_seed_delegated_management_functions")]
    operations = [migrations.RunPython(add_column_functions, remove_column_functions)]

