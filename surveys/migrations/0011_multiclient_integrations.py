from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings


def assign_primary_integration(apps, schema_editor):
    Integration = apps.get_model("vendors", "ClientIntegration")
    Survey = apps.get_model("surveys", "Survey")
    SyncRun = apps.get_model("surveys", "SyncRun")
    integration = Integration.objects.order_by("id").first()
    if integration:
        integration.supplier_code = settings.PUBLIC_SUPPLIER_CODE
        integration.save(update_fields=["supplier_code"])
        Survey.objects.filter(integration__isnull=True).update(integration=integration, client_id=integration.client_id)
        SyncRun.objects.filter(integration__isnull=True).update(integration=integration)


class Migration(migrations.Migration):
    dependencies = [
        ("surveys", "0010_merge_loi_and_vendor_allocation_history"),
        ("vendors", "0007_clientintegration_runtime_credentials"),
    ]
    operations = [
        migrations.AddField(model_name="survey", name="integration", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="surveys", to="vendors.clientintegration")),
        migrations.AddField(model_name="syncrun", name="integration", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="sync_runs", to="vendors.clientintegration")),
        migrations.AlterField(model_name="survey", name="source_id", field=models.PositiveBigIntegerField(db_index=True, help_text="Provider survey ID")),
        migrations.RunPython(assign_primary_integration, migrations.RunPython.noop),
        migrations.AddConstraint(model_name="survey", constraint=models.UniqueConstraint(fields=("integration", "source_id"), name="unique_integration_survey_source")),
    ]
