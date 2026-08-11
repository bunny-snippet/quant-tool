from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from prescreener_vault.services import capture_prescreener_submission
from surveys.models import SurveyAttempt
from surveys.survey_flow import ensure_attempt_prescreener_uid


class Command(BaseCommand):
    help = "Copy existing SurveyAttempt prescreener answers into the dedicated vault."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument("--after-id", type=int, default=0)
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--clear-source",
            action="store_true",
            help="Clear operational SurveyAttempt.answers only after each vault record is verified.",
        )

    def handle(self, *args, **options):
        if not settings.PRESCREENER_VAULT_ENABLED:
            raise CommandError("Set PRESCREENER_VAULT_ENABLED=true before running this command.")
        queryset = SurveyAttempt.objects.select_related(
            "survey__integration", "survey__client", "client", "platform_user"
        ).filter(id__gt=options["after_id"]).exclude(answers={}).order_by("id")
        if options["limit"]:
            queryset = queryset[: options["limit"]]

        scanned = copied = existing = cleared = failed = 0
        for attempt in queryset.iterator(chunk_size=max(1, options["batch_size"])):
            scanned += 1
            try:
                if options["dry_run"]:
                    if not attempt.prescreener_uid:
                        self.stdout.write(f"would allocate UID for attempt={attempt.pk} rid={attempt.rid}")
                    continue
                ensure_attempt_prescreener_uid(attempt)
                _, created = capture_prescreener_submission(
                    attempt,
                    attempt.answers,
                    submitted_at=attempt.submitted_at or attempt.initiated_at,
                )
                copied += int(created)
                existing += int(not created)
                if options["clear_source"]:
                    with transaction.atomic():
                        SurveyAttempt.objects.filter(pk=attempt.pk).update(answers={})
                    cleared += 1
            except Exception as exc:
                failed += 1
                self.stderr.write(f"attempt={attempt.pk} rid={attempt.rid}: {exc}")

        self.stdout.write(self.style.SUCCESS(
            f"scanned={scanned} copied={copied} existing={existing} cleared={cleared} failed={failed}"
        ))
        if failed:
            raise CommandError(f"Backfill completed with {failed} failed records; source answers were retained for them.")
