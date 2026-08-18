from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("surveys", "0017_surveyattempt_provider_profile_uid_profilereuseevent_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="profilereuseevent",
            name="reused_rid",
            field=models.CharField(db_index=True, default="", max_length=10),
            preserve_default=False,
        ),
    ]
