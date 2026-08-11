from django.core.management.base import BaseCommand, CommandError

from surveys.integrations import InnovateMRAPIError
from surveys.services import sync_surveys


class Command(BaseCommand):
    help = "Fetch, merge and persist InnovateMR inventory, quotas and targeting."

    def handle(self, *args, **options):
        try:
            summary = sync_surveys()
        except InnovateMRAPIError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"Sync {summary.run_id} {summary.status}: created={summary.created}, updated={summary.updated}, "
            f"unchanged={summary.unchanged}, closed={summary.closed}, detail_failures={summary.detail_failures}"
        ))

