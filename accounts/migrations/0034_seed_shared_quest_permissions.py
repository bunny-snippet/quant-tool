from django.db import migrations


FUNCTIONS = (
    ("studies.column.pid", "Show PID column", "Traffic Reports - Table columns", "Display the independent per-attempt platform PID.", "studies.column.respondent_id"),
    ("studies.column.client_name", "Show Client name in Traffic Reports", "Traffic Reports - Table columns", "Display the client name in Traffic Reports and exports.", "studies.column.project_id"),
    ("studies.filter.supplier", "Use Supplier filter", "Traffic Reports - Filters", "Filter journeys by external supplier.", "studies.filter.user"),
    ("studies.field.provider_status", "Show provider outcome under Status", "Traffic Reports - Row details", "Display provider status, reason and category.", "studies.column.status"),
    ("studies.field.status_source", "Show status source in export", "Traffic Reports - Row details", "Include the internal status source in exports.", "studies.column.status"),
    ("studies.detail.sensitive_audit", "View sensitive attempt audit payload", "Traffic Reports - Row details", "Read raw answers, callback payloads and internal audit fields.", None),
    ("termination_reasons.table.provider_status", "Show provider status in table", "Term Reports - Table details", "Display provider status in the result cell.", "termination_reasons.column.status"),
    ("termination_reasons.table.reason", "Show term reason in table", "Term Reports - Table details", "Display provider term reason or category.", "termination_reasons.column.status"),
    ("termination_reasons.export.status_source", "Export status source", "Term Reports - Export fields", "Include the internal status source in exports.", "termination_reasons.column.status"),
    ("termination_reasons.filter.supplier", "Filter by supplier", "Term Reports - Filters", "Filter unsuccessful journeys by external supplier.", "termination_reasons.filter.user"),
    ("user_hits.filter.supplier", "Use Supplier filter", "User Hits - Filters", "Filter user activity by external supplier.", "user_hits.filter.user"),
    ("prescreener_data.card.records", "Show Records card", "Panelist Data - Summary cards", "Display the filtered panelist count.", "prescreener_data.view"),
    ("prescreener_data.card.countries", "Show Countries card", "Panelist Data - Summary cards", "Display visible country count.", "prescreener_data.view"),
    ("prescreener_data.card.age_groups", "Show Age groups card", "Panelist Data - Summary cards", "Display visible age groups.", "prescreener_data.view"),
    ("prescreener_data.card.genders", "Show Genders card", "Panelist Data - Summary cards", "Display captured gender count.", "prescreener_data.view"),
)


def seed_permissions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")
    for code, name, module, description, source_code in FUNCTIONS:
        function, _ = AccessFunction.objects.update_or_create(
            code=code,
            defaults={"name": name, "module": module, "description": description, "is_active": True},
        )
        if source_code is None:
            continue
        source = AccessFunction.objects.filter(code=source_code).first()
        if source is None:
            continue
        for role_id in RoleFunctionPermission.objects.filter(
            function=source, allowed=True,
        ).values_list("role_id", flat=True):
            RoleFunctionPermission.objects.update_or_create(
                role_id=role_id, function=function, defaults={"allowed": True},
            )

    # Keep identity-bearing exports internally consistent.
    for target_code, source_code in (
        ("studies.column.respondent_id", "studies.column.pid"),
        ("termination_reasons.column.rid", "termination_reasons.export"),
    ):
        target = AccessFunction.objects.filter(code=target_code).first()
        source = AccessFunction.objects.filter(code=source_code).first()
        if not target or not source:
            continue
        for role_id in RoleFunctionPermission.objects.filter(
            function=source, allowed=True,
        ).values_list("role_id", flat=True):
            RoleFunctionPermission.objects.update_or_create(
                role_id=role_id, function=target, defaults={"allowed": True},
            )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0033_role_dashboard_performers")]
    operations = [migrations.RunPython(seed_permissions, migrations.RunPython.noop)]
