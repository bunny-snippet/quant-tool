from datetime import date
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from vendors.models import Client, ClientIntegration
from openpyxl import Workbook

from .models import FinalIDStatus, FinalIDUpload, FinalIDUploadItem, Survey, SurveyAttempt


class FinalIDImportTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="final-id-admin", email="final@example.test", password="test-password"
        )
        self.client_record = Client.objects.create(code="final-client", name="Final Client")
        self.other_client = Client.objects.create(code="other-client", name="Other Client")
        self.integration = ClientIntegration.objects.create(
            client=self.client_record,
            name="Final integration",
            provider_code="test-provider",
            base_url="https://example.test",
        )
        self.survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_id=101,
            source_key="101",
            company_name="Final Client",
        )
        self.other_survey = Survey.objects.create(
            client=self.other_client,
            source_id=202,
            source_key="202",
            company_name="Other Client",
        )
        self.completed = SurveyAttempt.objects.create(
            rid="FinalRID01",
            survey=self.survey,
            user_id="1",
            status=SurveyAttempt.Status.COMPLETED,
        )
        self.pending = SurveyAttempt.objects.create(
            rid="FinalRID02",
            survey=self.survey,
            user_id="1",
            status=SurveyAttempt.Status.REDIRECTED,
        )
        self.other_client_attempt = SurveyAttempt.objects.create(
            rid="OtherRID01",
            survey=self.other_survey,
            user_id="1",
            status=SurveyAttempt.Status.COMPLETED,
        )
        self.client.force_login(self.admin)

    @staticmethod
    def upload(contents: str, filename="final-ids.csv"):
        return SimpleUploadedFile(filename, contents.encode("utf-8"), content_type="text/csv")

    @staticmethod
    def xlsx_upload():
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["RID"])
        sheet.append(["FinalRID01"])
        payload = BytesIO()
        workbook.save(payload)
        return SimpleUploadedFile(
            "final-ids.xlsx",
            payload.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_import_marks_completed_matching_client_and_keeps_audit(self):
        response = self.client.post(reverse("final-ids-import"), {
            "client": self.client_record.pk,
            "month": "8",
            "year": "2026",
            "status": "accepted",
            "file": self.upload("RID\nFinalRID01\nFinalRID01\nMissRID001\n"),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"], {
            "upload_id": 1,
            "submitted": 3,
            "unique": 2,
            "applied": 1,
            "not_found": 1,
            "client_mismatch": 0,
            "not_completed": 0,
        })
        status = FinalIDStatus.objects.get(attempt=self.completed)
        self.assertEqual(status.status, "accepted")
        self.assertEqual(status.accounting_month, date(2026, 8, 1))
        upload = FinalIDUpload.objects.get()
        self.assertEqual(upload.submitted_count, 3)
        self.assertEqual(upload.unique_rid_count, 2)
        self.assertEqual(upload.applied_count, 1)
        self.assertEqual(
            set(upload.items.values_list("outcome", flat=True)),
            {FinalIDUploadItem.Outcome.APPLIED, FinalIDUploadItem.Outcome.NOT_FOUND},
        )

    def test_later_file_updates_current_status_and_selected_invoice_month(self):
        first = self.client.post(reverse("final-ids-import"), {
            "client": self.client_record.pk, "month": "8", "year": "2026",
            "status": "accepted", "file": self.upload("RID\nFinalRID01\n"),
        })
        second = self.client.post(reverse("final-ids-import"), {
            "client": self.client_record.pk, "month": "10", "year": "2026",
            "status": "rejected", "file": self.upload("RID\nFinalRID01\n"),
        })

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        status = FinalIDStatus.objects.get(attempt=self.completed)
        self.assertEqual(status.status, "rejected")
        self.assertEqual(status.accounting_month, date(2026, 10, 1))
        self.assertEqual(FinalIDUpload.objects.count(), 2)
        second_item = FinalIDUploadItem.objects.filter(upload=status.upload).get()
        self.assertEqual(second_item.previous_status, "accepted")
        self.assertEqual(second_item.applied_status, "rejected")

    def test_import_does_not_update_wrong_client_or_noncompleted_rids(self):
        response = self.client.post(reverse("final-ids-import"), {
            "client": self.client_record.pk,
            "month": "9",
            "year": "2026",
            "status": "accepted",
            "file": self.upload("RID\nFinalRID02\nOtherRID01\n"),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["not_completed"], 1)
        self.assertEqual(response.json()["result"]["client_mismatch"], 1)
        self.assertFalse(FinalIDStatus.objects.exists())

    def test_import_requires_a_rid_column(self):
        response = self.client.post(reverse("final-ids-import"), {
            "client": self.client_record.pk,
            "month": "9",
            "year": "2026",
            "status": "accepted",
            "file": self.upload("External ID\nFinalRID01\n"),
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "The file must contain an RID column.")
        self.assertFalse(FinalIDUpload.objects.exists())

    def test_import_accepts_excel_with_rid_column(self):
        response = self.client.post(reverse("final-ids-import"), {
            "client": self.client_record.pk,
            "month": "9",
            "year": "2026",
            "status": "accepted",
            "file": self.xlsx_upload(),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["applied"], 1)
        self.assertEqual(FinalIDStatus.objects.get().status, "accepted")
