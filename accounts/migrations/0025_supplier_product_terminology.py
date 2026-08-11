from django.db import migrations, models


def rename_visible_role(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Role.objects.filter(slug="external-vendor", name="External Vendor").update(name="External Supplier")


def restore_visible_role(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Role.objects.filter(slug="external-vendor", name="External Supplier").update(name="External Vendor")


class Migration(migrations.Migration):
    dependencies = [("accounts", "0024_seed_prescreener_data_permissions")]
    operations = [
        migrations.AlterField(
            model_name="employeeprofile",
            name="account_type",
            field=models.CharField(
                choices=[
                    ("employee", "Employee"),
                    ("internal_vendor", "Internal supplier"),
                    ("external_vendor", "External supplier"),
                ],
                db_index=True,
                default="employee",
                max_length=20,
            ),
        ),
        migrations.RunPython(rename_visible_role, restore_visible_role),
    ]
