from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("surveys", "0024_surveyentryipclaim_and_entry_ip_index"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="survey",
            index=models.Index(
                fields=["-source_modified_at", "-created_at"],
                name="survey_modified_created_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="survey",
            index=models.Index(
                fields=["country_code", "country"],
                name="survey_country_label_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="survey",
            index=models.Index(
                fields=["buyer_id", "client", "company_name"],
                name="survey_buyer_scope_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="survey",
            index=models.Index(
                fields=["integration", "status", "last_seen_at"],
                name="survey_int_status_seen_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="survey",
            index=models.Index(
                fields=["integration", "country_code"],
                name="survey_int_country_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(
                fields=["status", "-initiated_at"],
                name="attempt_status_init_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(
                fields=["platform_user", "-initiated_at"],
                name="attempt_user_init_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(
                fields=["platform_user", "status", "-initiated_at"],
                name="attempt_user_status_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(
                fields=["survey", "status"],
                name="attempt_survey_status_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(
                fields=["status", "-callback_at"],
                name="attempt_status_cb_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(
                fields=["client", "-initiated_at"],
                name="attempt_client_init_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(fields=["-callback_at"], name="attempt_callback_idx"),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(fields=["callback_ip"], name="attempt_exit_ip_idx"),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(
                fields=["user_id", "-initiated_at"],
                name="attempt_legacy_user_init_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(
                fields=["platform_user", "status", "-callback_at"],
                name="attempt_user_status_cb_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(
                fields=["user_id", "status", "-callback_at"],
                name="attempt_legacy_status_cb_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(
                fields=["-callback_at", "-initiated_at", "status"],
                name="attempt_term_order_idx",
            ),
        ),
    ]
