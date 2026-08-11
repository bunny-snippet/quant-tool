from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("vendors", "0008_clientintegration_api_contract")]

    operations = [
        migrations.AddField(
            model_name="clientintegration",
            name="credential_env_keys",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Provider credential names mapped to environment-variable names; never secret values.",
            ),
        ),
        migrations.AddField(
            model_name="clientintegration",
            name="config",
            field=models.JSONField(blank=True, default=dict, help_text="Non-secret provider configuration."),
        ),
    ]
