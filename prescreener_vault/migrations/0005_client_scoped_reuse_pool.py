from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("prescreener_vault", "0004_prescreenersubmission_vault_reuse_queue_idx")]

    operations = [
        migrations.AddField(
            model_name="prescreenersubmission",
            name="source_client_code",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Stable client scope that prevents profiles crossing between clients.",
                max_length=80,
            ),
        ),
        migrations.AddField(
            model_name="prescreenersubmission",
            name="last_reused_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddIndex(
            model_name="prescreenersubmission",
            index=models.Index(
                fields=[
                    "source_client_code", "country_code", "respondent_age_group",
                    "respondent_gender", "usage_count",
                ],
                name="vault_client_reuse_idx",
            ),
        ),
    ]
