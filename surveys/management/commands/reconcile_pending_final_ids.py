"""Audit-backed, explicit repair for accepted uploads predating auto-rejection."""
import csv
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from surveys.final_ids import _accounting_month_bounds, _client_attempt_filter, import_final_ids
from surveys.models import FinalIDUpload, SurveyAttempt
from vendors.models import Client


class Command(BaseCommand):
    help = "Dry-run pending complete rejections for one historical accepted upload; --apply needs --expected-count."

    def add_arguments(self, parser):
        parser.add_argument('--upload-id', type=int, required=True)
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--expected-count', type=int)

    def handle(self, *args, **options):
        with transaction.atomic():
            try:
                source = FinalIDUpload.objects.get(pk=options['upload_id'], decision='accepted', applied_count__gt=0)
            except FinalIDUpload.DoesNotExist as exc:
                raise CommandError('An accepted upload with applied RIDs is required.') from exc
            client = Client.objects.select_for_update().get(pk=source.client_id)
            lower, upper = _accounting_month_bounds(source.accounting_month)
            rids = list(SurveyAttempt.objects.filter(
                _client_attempt_filter(client.pk), status=SurveyAttempt.Status.COMPLETED,
                initiated_at__gte=lower, initiated_at__lt=upper,
                final_id_status__isnull=True,
            ).order_by('pk').values_list('rid', flat=True))
            self.stdout.write(f'Client {client.pk}: {client.name}; activity month {source.accounting_month:%Y-%m}; pending completes {len(rids)}')
            if not options['apply']:
                self.stdout.write('DRY RUN: no records changed.')
                return
            if options['expected_count'] != len(rids):
                raise CommandError('Pending count changed or --expected-count missing; rerun dry run.')
            if not rids:
                self.stdout.write('No pending completes; nothing changed.')
                return
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerow(['RID'])
            writer.writerows((rid,) for rid in rids)
            result = import_final_ids(
                uploaded_file=SimpleUploadedFile(f'pending-repair-source-upload-{source.pk}.csv', stream.getvalue().encode()),
                client=client, accounting_month=source.accounting_month,
                decision='rejected', uploaded_by=source.uploaded_by,
            )
            if result['applied'] != len(rids) or result['invalid'] or result['not_found'] or result['client_mismatch']:
                raise CommandError('Repair did not match the dry-run scope; transaction rolled back.')
            self.stdout.write(f"Applied {result['applied']} rejections; audit upload {result['upload_id']}. Original journey timestamps unchanged.")
