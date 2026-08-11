from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("prescreener_vault", "0001_initial")]

    operations = [
        migrations.RemoveIndex(
            model_name="prescreenersubmission",
            name="vault_client_survey_idx",
        ),
        migrations.RemoveField(model_name="prescreenersubmission", name="source_attempt_id"),
        migrations.RemoveField(model_name="prescreenersubmission", name="platform_user_id"),
        migrations.RemoveField(model_name="prescreenersubmission", name="platform_user_email"),
        migrations.RemoveField(model_name="prescreenersubmission", name="platform_user_name"),
        migrations.RemoveField(model_name="prescreenersubmission", name="provider_code"),
        migrations.RemoveField(model_name="prescreenersubmission", name="client_id"),
        migrations.RemoveField(model_name="prescreenersubmission", name="client_name"),
        migrations.RemoveField(model_name="prescreenersubmission", name="survey_local_id"),
        migrations.RemoveField(model_name="prescreenersubmission", name="survey_source_key"),
        migrations.RemoveField(model_name="prescreenersubmission", name="survey_name"),
    ]
