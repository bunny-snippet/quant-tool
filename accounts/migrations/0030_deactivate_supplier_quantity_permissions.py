from django.db import migrations


QUANTITY_PERMISSION_CODES = (
    "vendors.card.quantity",
    "vendors.column.client.quantity",
    "vendors.column.project.quantity",
)


def deactivate_quantity_permissions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    AccessFunction.objects.filter(code__in=QUANTITY_PERMISSION_CODES).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0029_restrict_dashboard_to_super_admin"),
    ]

    operations = [
        migrations.RunPython(deactivate_quantity_permissions, migrations.RunPython.noop),
    ]
