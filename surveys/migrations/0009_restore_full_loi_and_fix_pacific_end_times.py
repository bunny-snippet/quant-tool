from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser
from django.db import migrations
from django.utils import timezone


INNOVATEMR_TIMEZONE = ZoneInfo("America/Los_Angeles")
COMPLETION_KEYS = (
    "completeDateTime",
    "complete_date_time",
    "completedAt",
    "completed_at",
    "endDateTime",
    "end_date_time",
)


def parse_completion_time(payload):
    value = next((payload.get(key) for key in COMPLETION_KEYS if payload.get(key)), None)
    if not value:
        return None
    try:
        parsed = date_parser.parse(
            str(value),
            fuzzy=True,
            tzinfos={
                "PST": INNOVATEMR_TIMEZONE,
                "PDT": INNOVATEMR_TIMEZONE,
                "UTC": dt_timezone.utc,
                "GMT": dt_timezone.utc,
            },
        )
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, INNOVATEMR_TIMEZONE)
        return parsed.astimezone(dt_timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def restore_full_loi_and_fix_end_times(apps, schema_editor):
    SurveyAttempt = apps.get_model("surveys", "SurveyAttempt")
    updates = []
    queryset = SurveyAttempt.objects.exclude(callback_at=None)
    for attempt in queryset.iterator(chunk_size=1000):
        old_callback_at = attempt.callback_at
        if attempt.status_source == "innovatemr_transaction":
            corrected = parse_completion_time(attempt.upstream_transaction_data or {})
            if corrected and corrected >= attempt.initiated_at:
                attempt.callback_at = corrected
                if attempt.last_callback_at is None or attempt.last_callback_at == old_callback_at:
                    attempt.last_callback_at = corrected

        attempt.loi_seconds = max(0, int((attempt.callback_at - attempt.initiated_at).total_seconds()))
        updates.append(attempt)
        if len(updates) >= 1000:
            SurveyAttempt.objects.bulk_update(
                updates,
                ["callback_at", "last_callback_at", "loi_seconds"],
                batch_size=1000,
            )
            updates.clear()

    if updates:
        SurveyAttempt.objects.bulk_update(
            updates,
            ["callback_at", "last_callback_at", "loi_seconds"],
            batch_size=1000,
        )


class Migration(migrations.Migration):
    dependencies = [("surveys", "0008_recalculate_actual_survey_loi")]
    operations = [migrations.RunPython(restore_full_loi_and_fix_end_times, migrations.RunPython.noop)]
