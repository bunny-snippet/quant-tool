from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("surveys", "0013_survey_buyer_id_survey_survey_type_and_more")]

    operations = [
        migrations.AddField(
            model_name="surveyattempt",
            name="prescreener_uid",
            field=models.CharField(
                blank=True,
                editable=False,
                help_text="Stable XXXX-XXXX-XXXX-XXXX identity for the isolated prescreener vault.",
                max_length=19,
                null=True,
                unique=True,
            ),
        ),
    ]
