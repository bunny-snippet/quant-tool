"""Backfill the stable client scope used by the reusable-profile queue."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from prescreener_vault.constants import DATABASE_ALIAS
from prescreener_vault.models import PrescreenerSubmission
from surveys.models import SurveyAttempt


class Command(BaseCommand):
    help = "Assign every legacy panelist profile to the client of its original RID."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        if not settings.PRESCREENER_VAULT_ENABLED:
            raise CommandError("Set PRESCREENER_VAULT_ENABLED=true before running this command.")

        batch_size = max(1, options["batch_size"])
        dry_run = options["dry_run"]
        scanned = assigned = unresolved = 0
        last_uid = ""
        while True:
            rows = list(
                PrescreenerSubmission.objects.using(DATABASE_ALIAS)
                .filter(source_client_code="", uid__gt=last_uid)
                .order_by("uid")
                .values("uid", "rid")[:batch_size]
            )
            if not rows:
                break
            last_uid = rows[-1]["uid"]
            scanned += len(rows)
            attempts = {
                attempt.rid: attempt
                for attempt in SurveyAttempt.objects.select_related(
                    "survey__integration__client"
                ).filter(rid__in=[row["rid"] for row in rows])
            }
            grouped = {}
            for row in rows:
                attempt = attempts.get(row["rid"])
                integration = getattr(getattr(attempt, "survey", None), "integration", None)
                client_code = str(
                    getattr(getattr(integration, "client", None), "code", "") or ""
                ).strip().lower()
                if not client_code:
                    unresolved += 1
                    continue
                grouped.setdefault(client_code, []).append(row["uid"])
            for client_code, uids in grouped.items():
                if not dry_run:
                    PrescreenerSubmission.objects.using(DATABASE_ALIAS).filter(
                        uid__in=uids, source_client_code=""
                    ).update(source_client_code=client_code)
                assigned += len(uids)

        action = "would_assign" if dry_run else "assigned"
        self.stdout.write(self.style.SUCCESS(
            f"scanned={scanned} {action}={assigned} unresolved={unresolved}"
        ))
