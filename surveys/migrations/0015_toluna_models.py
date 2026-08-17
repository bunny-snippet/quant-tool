from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("surveys", "0014_surveyattempt_prescreener_uid"),
        ("vendors", "0013_vendorapikey_client_allocations"),
    ]

    operations = [
        migrations.CreateModel(
            name="TolunaMember",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("member_code", models.CharField(db_index=True, max_length=80)),
                ("culture_code", models.CharField(blank=True, db_index=True, max_length=12)),
                ("profile_hash", models.CharField(blank=True, max_length=64)),
                ("is_registered", models.BooleanField(default=False)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("integration", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="toluna_members", to="vendors.clientintegration")),
            ],
            options={"ordering": ["-updated_at"]},
        ),
        migrations.CreateModel(
            name="TolunaReferenceQuestion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("culture_code", models.CharField(db_index=True, max_length=12)),
                ("culture_id", models.PositiveIntegerField(db_index=True)),
                ("question_id", models.BigIntegerField(db_index=True)),
                ("internal_name", models.CharField(blank=True, max_length=300)),
                ("display_name", models.TextField(blank=True)),
                ("answer_type", models.CharField(blank=True, max_length=80)),
                ("is_routable", models.BooleanField(default=False)),
                ("options", models.JSONField(blank=True, default=list)),
                ("raw_data", models.JSONField(blank=True, default=dict)),
                ("source_updated_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("integration", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="toluna_reference_questions", to="vendors.clientintegration")),
            ],
            options={"ordering": ["culture_code", "question_id"]},
        ),
        migrations.AddConstraint(
            model_name="tolunamember",
            constraint=models.UniqueConstraint(fields=("integration", "member_code"), name="unique_toluna_member_code"),
        ),
        migrations.AddConstraint(
            model_name="tolunareferencequestion",
            constraint=models.UniqueConstraint(fields=("integration", "culture_code", "question_id"), name="unique_toluna_reference_question"),
        ),
        migrations.AddIndex(
            model_name="tolunareferencequestion",
            index=models.Index(fields=["integration", "culture_code"], name="surveys_tol_integra_c1de71_idx"),
        ),
    ]
