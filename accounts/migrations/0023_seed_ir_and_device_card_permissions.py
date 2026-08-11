from django.db import migrations


NEW_FUNCTIONS = (
    (
        "dashboard.card.ir",
        "Show IR card",
        "Dashboard - Summary cards",
        "Display completes divided by completes plus survey terminations; local pre-screen terminations, quota and security outcomes are excluded.",
        "dashboard.view",
    ),
    (
        "studies.card.ir",
        "Show IR card",
        "Studies - Summary cards",
        "Display completes divided by completes plus actual survey terminations.",
        "attempts.view",
    ),
    (
        "user_hits.card.devices",
        "Show Completed devices card",
        "User Hits - Summary cards",
        "Display completed Desktop, Mobile and Tablet journeys.",
        "user_hits.view",
    ),
    (
        "user_hits.card.ir",
        "Show IR card",
        "User Hits - Summary cards",
        "Display completes divided by completes plus actual survey terminations.",
        "user_hits.view",
    ),
)


def seed_permissions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")
    AccessFunction.objects.filter(
        code__in=[
            "dashboard.filter.client", "dashboard.filter.country", "dashboard.filter.branch",
            "dashboard.filter.sub_branch", "dashboard.filter.shift", "dashboard.filter.user",
            "dashboard.filter.date", "dashboard.filters.clear", "dashboard.chart.recent",
        ]
    ).update(is_active=False)
    for code, name, module, description, parent_code in NEW_FUNCTIONS:
        function, _created = AccessFunction.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "module": module,
                "description": description,
                "is_active": True,
            },
        )
        parent = AccessFunction.objects.filter(code=parent_code).first()
        if parent is None:
            continue
        role_ids = RoleFunctionPermission.objects.filter(
            function=parent,
            allowed=True,
            role__is_active=True,
        ).values_list("role_id", flat=True)
        for role_id in role_ids:
            RoleFunctionPermission.objects.update_or_create(
                role_id=role_id,
                function=function,
                defaults={"allowed": True},
            )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0022_seed_dashboard_component_permissions")]
    operations = [migrations.RunPython(seed_permissions, migrations.RunPython.noop)]
