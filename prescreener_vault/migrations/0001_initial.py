from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="PrescreenerSubmission",
            fields=[
                ("uid", models.CharField(max_length=19, primary_key=True, serialize=False)),
                ("rid", models.CharField(max_length=10, unique=True)),
                ("source_attempt_id", models.PositiveBigIntegerField(blank=True, db_index=True, null=True)),
                ("platform_user_id", models.CharField(blank=True, db_index=True, max_length=160)),
                ("platform_user_email", models.EmailField(blank=True, max_length=254)),
                ("platform_user_name", models.CharField(blank=True, max_length=300)),
                ("provider_code", models.CharField(blank=True, db_index=True, max_length=80)),
                ("client_id", models.PositiveBigIntegerField(blank=True, db_index=True, null=True)),
                ("client_name", models.CharField(blank=True, db_index=True, max_length=160)),
                ("survey_local_id", models.CharField(blank=True, db_index=True, max_length=14)),
                ("survey_source_key", models.CharField(blank=True, db_index=True, max_length=160)),
                ("survey_name", models.CharField(blank=True, max_length=500)),
                ("country", models.CharField(blank=True, max_length=120)),
                ("country_code", models.CharField(blank=True, db_index=True, max_length=8)),
                ("language", models.CharField(blank=True, max_length=80)),
                ("language_code", models.CharField(blank=True, db_index=True, max_length=8)),
                ("respondent_age", models.PositiveSmallIntegerField(blank=True, db_index=True, null=True)),
                ("respondent_age_group", models.CharField(blank=True, db_index=True, max_length=20)),
                ("respondent_gender", models.CharField(blank=True, db_index=True, max_length=80)),
                ("respondent_ethnicity", models.CharField(blank=True, db_index=True, max_length=160)),
                ("respondent_postal_code", models.CharField(blank=True, db_index=True, max_length=40)),
                ("profile_dimensions", models.JSONField(blank=True, default=dict)),
                ("raw_answers", models.JSONField(blank=True, default=dict)),
                ("answer_count", models.PositiveSmallIntegerField(default=0)),
                ("submitted_at", models.DateTimeField(db_index=True)),
                ("captured_at", models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={"ordering": ["-submitted_at"]},
        ),
        migrations.CreateModel(
            name="PrescreenerAnswer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("position", models.PositiveSmallIntegerField()),
                ("question_record_id", models.CharField(blank=True, max_length=40)),
                ("question_id", models.CharField(blank=True, max_length=160)),
                ("question_key", models.CharField(blank=True, db_index=True, max_length=180)),
                ("question_text", models.TextField(blank=True)),
                ("question_type", models.CharField(blank=True, max_length=120)),
                ("question_category", models.CharField(blank=True, max_length=120)),
                ("canonical_attribute", models.CharField(blank=True, db_index=True, max_length=80)),
                ("answer_values", models.JSONField(blank=True, default=list)),
                ("answer_labels", models.JSONField(blank=True, default=list)),
                ("upstream_values", models.JSONField(blank=True, default=list)),
                ("submission", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="question_answers", to="prescreener_vault.prescreenersubmission")),
            ],
            options={"ordering": ["position"]},
        ),
        migrations.CreateModel(
            name="PrescreenerAnswerValue",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("position", models.PositiveSmallIntegerField()),
                ("value", models.TextField(blank=True)),
                ("label", models.TextField(blank=True)),
                ("normalized_value", models.CharField(blank=True, db_index=True, max_length=191)),
                ("canonical_attribute", models.CharField(blank=True, db_index=True, max_length=80)),
                ("country_code", models.CharField(blank=True, db_index=True, max_length=8)),
                ("answer", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="values", to="prescreener_vault.prescreeneranswer")),
            ],
            options={"ordering": ["position"]},
        ),
        migrations.AddIndex(model_name="prescreenersubmission", index=models.Index(fields=["country_code", "respondent_age_group", "respondent_gender"], name="vault_country_profile_idx")),
        migrations.AddIndex(model_name="prescreenersubmission", index=models.Index(fields=["client_id", "survey_source_key", "-submitted_at"], name="vault_client_survey_idx")),
        migrations.AddConstraint(model_name="prescreeneranswer", constraint=models.UniqueConstraint(fields=("submission", "position"), name="vault_unique_answer_position")),
        migrations.AddIndex(model_name="prescreeneranswer", index=models.Index(fields=["canonical_attribute", "question_key"], name="vault_answer_attribute_idx")),
        migrations.AddConstraint(model_name="prescreeneranswervalue", constraint=models.UniqueConstraint(fields=("answer", "position"), name="vault_unique_value_position")),
        migrations.AddIndex(model_name="prescreeneranswervalue", index=models.Index(fields=["country_code", "canonical_attribute", "normalized_value"], name="vault_matching_value_idx")),
    ]
