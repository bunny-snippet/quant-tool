from django.db import migrations, models


def backfill_source_keys(apps, schema_editor):
    Survey = apps.get_model("surveys", "Survey")
    for survey in Survey.objects.filter(source_key="").iterator():
        survey.source_key = str(survey.source_id) if survey.source_id is not None else f"legacy-{survey.pk}"
        survey.save(update_fields=["source_key"])


class Migration(migrations.Migration):
    dependencies = [
        ("surveys", "0011_multiclient_integrations"),
        ("vendors", "0009_clientintegration_provider_config"),
    ]

    operations = [
        migrations.AddField(
            model_name="survey",
            name="source_key",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Provider survey identifier, including non-numeric IDs.",
                max_length=160,
            ),
        ),
        migrations.AlterField(
            model_name="survey",
            name="source_id",
            field=models.PositiveBigIntegerField(
                blank=True,
                db_index=True,
                help_text="Legacy numeric upstream survey ID when the provider uses one.",
                null=True,
            ),
        ),
        migrations.RunPython(backfill_source_keys, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="survey",
            constraint=models.UniqueConstraint(
                fields=("integration", "source_key"),
                name="unique_integration_survey_key",
            ),
        ),
    ]
