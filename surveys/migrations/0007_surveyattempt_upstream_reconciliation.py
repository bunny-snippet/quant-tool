from django.db import migrations, models


def remove_loopback_ips(apps, schema_editor):
    SurveyAttempt = apps.get_model("surveys", "SurveyAttempt")
    loopback_values = ["127.0.0.1", "::1"]
    SurveyAttempt.objects.filter(initiation_ip__in=loopback_values).update(initiation_ip=None)
    SurveyAttempt.objects.filter(callback_ip__in=loopback_values).update(callback_ip=None)


class Migration(migrations.Migration):
    dependencies = [("surveys", "0006_surveyattempt_client_audit")]

    operations = [
        migrations.AddField(
            model_name="surveyattempt",
            name="status_source",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="surveyattempt",
            name="upstream_checked_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="surveyattempt",
            name="upstream_transaction_data",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(remove_loopback_ips, migrations.RunPython.noop),
    ]
