from django.db import migrations, models


def configure_existing_integrations(apps, schema_editor):
    Integration = apps.get_model("vendors", "ClientIntegration")
    for integration in Integration.objects.all():
        provider = (integration.provider_code or "").lower().replace("-", "").replace("_", "")
        if provider == "innovatemr":
            integration.inventory_endpoint = "/supply/getAllocatedSurveys"
            integration.paged_inventory_endpoint = "/supply/getAllocatedSurveysPaged"
            integration.quota_endpoint_template = "/supply/getQuotaForSurvey/{survey_id}"
            integration.targeting_endpoint_template = "/supply/getSurveyTargeting/{survey_id}"
            integration.transaction_endpoint_template = "/supply/getSurveyTransactionsByCond/{survey_id}/{pid}"
        elif provider in {"biobrain", "voqall"} or "voqall.com" in integration.base_url.lower():
            root = integration.base_url.rstrip("/")
            if root.lower().endswith("/surveys"):
                root = root[:-8]
                integration.inventory_endpoint = ""
            else:
                integration.inventory_endpoint = "/surveys"
            integration.auth_header_name = "EQ-PARTNER-ACCESS-KEY"
            integration.inventory_result_key = "Surveys"
            integration.quota_endpoint_template = f"{root}/survey-quotas/{{survey_id}}"
            integration.targeting_endpoint_template = f"{root}/survey-qualifications/{{survey_id}}"
            integration.quota_result_key = "Quotas"
            integration.targeting_result_key = "Qualifications"
        integration.save()


class Migration(migrations.Migration):
    dependencies = [("vendors", "0007_clientintegration_runtime_credentials")]
    operations = [
        migrations.AddField(model_name="clientintegration", name="auth_header_name", field=models.CharField(default="x-access-token", max_length=120)),
        migrations.AddField(model_name="clientintegration", name="auth_header_prefix", field=models.CharField(blank=True, help_text="For example: Bearer", max_length=40)),
        migrations.AddField(model_name="clientintegration", name="field_mapping", field=models.JSONField(blank=True, default=dict, help_text="Optional canonical-field to upstream-field mapping for custom providers.")),
        migrations.AddField(model_name="clientintegration", name="inventory_endpoint", field=models.CharField(blank=True, help_text="Relative or absolute inventory endpoint. Blank calls Base URL exactly.", max_length=500)),
        migrations.AddField(model_name="clientintegration", name="inventory_result_key", field=models.CharField(default="result", max_length=120)),
        migrations.AddField(model_name="clientintegration", name="paged_inventory_endpoint", field=models.CharField(blank=True, max_length=500)),
        migrations.AddField(model_name="clientintegration", name="quota_endpoint_template", field=models.CharField(blank=True, help_text="Optional endpoint containing {survey_id}.", max_length=500)),
        migrations.AddField(model_name="clientintegration", name="quota_result_key", field=models.CharField(default="result", max_length=120)),
        migrations.AddField(model_name="clientintegration", name="targeting_endpoint_template", field=models.CharField(blank=True, help_text="Optional endpoint containing {survey_id}.", max_length=500)),
        migrations.AddField(model_name="clientintegration", name="targeting_result_key", field=models.CharField(default="result", max_length=120)),
        migrations.AddField(model_name="clientintegration", name="transaction_endpoint_template", field=models.CharField(blank=True, help_text="Optional endpoint containing {survey_id} and {pid}.", max_length=500)),
        migrations.AddField(model_name="clientintegration", name="transaction_result_key", field=models.CharField(default="result", max_length=120)),
        migrations.RunPython(configure_existing_integrations, migrations.RunPython.noop),
    ]
