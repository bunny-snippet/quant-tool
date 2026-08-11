from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("vendors", "0006_organizationunit_organizationclientaccess_and_more")]
    operations = [
        migrations.AddField(model_name="clientintegration", name="credential_changed_at", field=models.DateTimeField(blank=True, editable=False, null=True)),
        migrations.AddField(model_name="clientintegration", name="credential_fingerprint", field=models.CharField(blank=True, editable=False, max_length=64)),
        migrations.AddField(model_name="clientintegration", name="credential_last_four", field=models.CharField(blank=True, editable=False, max_length=4)),
        migrations.AddField(model_name="clientintegration", name="detail_refresh_batch", field=models.PositiveSmallIntegerField(default=3, help_text="Survey detail records refreshed after each inventory sync.", validators=[MinValueValidator(0), MaxValueValidator(25)])),
        migrations.AddField(model_name="clientintegration", name="encrypted_api_token", field=models.TextField(blank=True, editable=False)),
        migrations.AddField(model_name="clientintegration", name="last_sync_error", field=models.TextField(blank=True, editable=False)),
        migrations.AddField(model_name="clientintegration", name="last_sync_finished_at", field=models.DateTimeField(blank=True, editable=False, null=True)),
        migrations.AddField(model_name="clientintegration", name="last_sync_started_at", field=models.DateTimeField(blank=True, db_index=True, editable=False, null=True)),
        migrations.AddField(model_name="clientintegration", name="last_sync_status", field=models.CharField(blank=True, editable=False, max_length=20)),
        migrations.AddField(model_name="clientintegration", name="last_sync_summary", field=models.JSONField(blank=True, default=dict, editable=False)),
        migrations.AddField(model_name="clientintegration", name="last_test_error", field=models.TextField(blank=True, editable=False)),
        migrations.AddField(model_name="clientintegration", name="last_test_status", field=models.CharField(blank=True, editable=False, max_length=20)),
        migrations.AddField(model_name="clientintegration", name="last_tested_at", field=models.DateTimeField(blank=True, editable=False, null=True)),
        migrations.AddField(model_name="clientintegration", name="supplier_code", field=models.CharField(default="1000", max_length=40)),
        migrations.AddField(model_name="clientintegration", name="sync_interval_seconds", field=models.PositiveIntegerField(default=60, help_text="Minimum interval between inventory syncs for this integration.", validators=[MinValueValidator(60)])),
    ]
