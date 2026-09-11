import secrets
import string
import uuid
from datetime import timezone as datetime_timezone

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

import surveys.identifiers
import surveys.models


ALPHABET = string.ascii_letters + string.digits


def migration_pid():
    characters = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        *(secrets.choice(ALPHABET) for _ in range(9)),
    ]
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)


def backfill_shared_fields(apps, schema_editor):
    Survey = apps.get_model("surveys", "Survey")
    SurveyAttempt = apps.get_model("surveys", "SurveyAttempt")

    used = set(SurveyAttempt.objects.exclude(pid__isnull=True).exclude(pid="").values_list("pid", flat=True))
    pending = []
    for attempt in SurveyAttempt.objects.filter(Q(pid__isnull=True) | Q(pid="")).iterator(chunk_size=1000):
        candidate = migration_pid()
        while candidate in used:
            candidate = migration_pid()
        used.add(candidate)
        attempt.pid = candidate
        pending.append(attempt)
        if len(pending) >= 1000:
            SurveyAttempt.objects.bulk_update(pending, ["pid"], batch_size=1000)
            pending = []
    if pending:
        SurveyAttempt.objects.bulk_update(pending, ["pid"], batch_size=1000)

    now = timezone.now()
    pending = []
    biobrain = Survey.objects.filter(integration__provider_code__in=("biobrain", "voqall"))
    for survey in biobrain.iterator(chunk_size=1000):
        value = (survey.raw_data or {}).get("endDate") or (survey.raw_data or {}).get("EndDate")
        parsed = parse_datetime(str(value)) if value else None
        if parsed and timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, datetime_timezone.utc)
        survey.source_end_at = parsed
        survey.status = "closed" if parsed and parsed <= now else "live"
        pending.append(survey)
        if len(pending) >= 1000:
            Survey.objects.bulk_update(pending, ["source_end_at", "status"], batch_size=1000)
            pending = []
    if pending:
        Survey.objects.bulk_update(pending, ["source_end_at", "status"], batch_size=1000)


class Migration(migrations.Migration):
    dependencies = [
        ("surveys", "0027_final_id_upload_counters"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="survey",
            name="source_end_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="surveyattempt",
            name="pid",
            field=models.CharField(blank=True, db_index=True, editable=False, max_length=13, null=True, unique=True),
        ),
        migrations.RunPython(backfill_shared_fields, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="surveyattempt",
            name="pid",
            field=models.CharField(
                db_index=True,
                default=surveys.identifiers.generate_platform_pid,
                editable=False,
                help_text="Platform tracking ID. Newly generated as 12-13 mixed alphanumeric characters; legacy 6-9 character values remain valid.",
                max_length=13,
                unique=True,
            ),
        ),
        migrations.CreateModel(
            name="ExportJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("public_id", models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True)),
                ("kind", models.CharField(choices=[("projects", "Projects"), ("traffic", "Traffic reports"), ("terms", "Term reports"), ("panelist", "Panelist data"), ("user_dashboard", "User dashboard")], max_length=16)),
                ("query", models.JSONField(blank=True, default=dict)),
                ("status", models.CharField(choices=[("queued", "Queued"), ("running", "Running"), ("completed", "Completed"), ("failed", "Failed")], db_index=True, default="queued", max_length=16)),
                ("filename", models.CharField(blank=True, max_length=255)),
                ("storage_key", models.CharField(default=surveys.models.generate_export_storage_key, editable=False, max_length=80, unique=True)),
                ("error", models.CharField(blank=True, max_length=500)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("downloaded_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("requested_by", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="export_jobs", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [models.Index(fields=["requested_by", "status", "-created_at"], name="quant_export_user_status_idx")],
            },
        ),
        migrations.AddIndex(
            model_name="survey",
            index=models.Index(fields=["status", "country_code", "cpi", "source_modified_at", "created_at", "client", "source_created_at"], name="survey_filtered_page_idx"),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(fields=["initiated_at", "status", "survey", "platform_user", "vendor", "source_cpi_snapshot", "payable_cpi_snapshot", "cpi_currency_snapshot", "status_source", "entry_device", "loi_seconds", "callback_at"], name="attempt_report_cover_idx"),
        ),
    ]
