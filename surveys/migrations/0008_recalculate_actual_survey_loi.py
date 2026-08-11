from django.db import migrations


def recalculate_actual_survey_loi(apps, schema_editor):
    SurveyAttempt = apps.get_model("surveys", "SurveyAttempt")
    updates = []
    for attempt in SurveyAttempt.objects.exclude(callback_at=None).iterator(chunk_size=1000):
        started_at = attempt.redirected_at or attempt.submitted_at or attempt.initiated_at
        corrected = max(0, int((attempt.callback_at - started_at).total_seconds()))
        if attempt.loi_seconds != corrected:
            attempt.loi_seconds = corrected
            updates.append(attempt)
        if len(updates) >= 1000:
            SurveyAttempt.objects.bulk_update(updates, ["loi_seconds"], batch_size=1000)
            updates.clear()
    if updates:
        SurveyAttempt.objects.bulk_update(updates, ["loi_seconds"], batch_size=1000)


class Migration(migrations.Migration):
    dependencies = [("surveys", "0007_surveyattempt_upstream_reconciliation")]
    operations = [migrations.RunPython(recalculate_actual_survey_loi, migrations.RunPython.noop)]
