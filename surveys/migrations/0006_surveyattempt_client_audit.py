from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("surveys", "0005_surveyattempt_platform_user")]

    operations = [
        migrations.AddField(model_name="surveyattempt", name="entry_accept_language", field=models.CharField(blank=True, max_length=500)),
        migrations.AddField(model_name="surveyattempt", name="entry_browser", field=models.CharField(blank=True, max_length=160)),
        migrations.AddField(model_name="surveyattempt", name="entry_client_data", field=models.JSONField(blank=True, default=dict)),
        migrations.AddField(model_name="surveyattempt", name="entry_device", field=models.CharField(blank=True, max_length=80)),
        migrations.AddField(model_name="surveyattempt", name="entry_os", field=models.CharField(blank=True, max_length=160)),
        migrations.AddField(model_name="surveyattempt", name="entry_referrer", field=models.TextField(blank=True)),
        migrations.AddField(model_name="surveyattempt", name="entry_user_agent", field=models.TextField(blank=True)),
        migrations.AddField(model_name="surveyattempt", name="exit_browser", field=models.CharField(blank=True, max_length=160)),
        migrations.AddField(model_name="surveyattempt", name="exit_client_data", field=models.JSONField(blank=True, default=dict)),
        migrations.AddField(model_name="surveyattempt", name="exit_device", field=models.CharField(blank=True, max_length=80)),
        migrations.AddField(model_name="surveyattempt", name="exit_os", field=models.CharField(blank=True, max_length=160)),
        migrations.AddField(model_name="surveyattempt", name="exit_user_agent", field=models.TextField(blank=True)),
    ]
