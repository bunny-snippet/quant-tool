from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from vendors.models import Client, ClientIntegration

from .models import Survey, SurveyAttempt, TolunaNotification
from .providers import ProviderError


class TolunaNotificationStatusPageTests(TestCase):
    def setUp(self):
        client = Client.objects.create(
            code="toluna-status-notification",
            name="Toluna",
            provider_code="toluna",
        )
        integration = ClientIntegration.objects.create(
            client=client,
            name="Toluna notification status",
            provider_code="toluna",
            base_url="https://tws.toluna.com",
        )
        survey = Survey.objects.create(
            client=client,
            integration=integration,
            source_key="123:100",
            company_name="Toluna",
            status=Survey.Status.LIVE,
        )
        self.attempt = SurveyAttempt.objects.create(
            rid="NtFyTol001",
            survey=survey,
            user_id="1",
            status=SurveyAttempt.Status.OVER_QUOTA,
            status_source="toluna_notification_member_terminate",
            is_verified=True,
            callback_at=timezone.now(),
        )
        self.notification = TolunaNotification.objects.create(
            event_type=TolunaNotification.EventType.MEMBER_TERMINATE,
            payload_hash="a" * 64,
            integration=integration,
            survey=survey,
            attempt=self.attempt,
            provider_status="Terminated",
            reason="QuotaFull",
            applied=True,
            processing_message="Respondent journey updated.",
            raw_payload={
                "UniqueCode": "Sensitive-Member-Code",
                "EncryptedValue": "Sensitive-Signature",
            },
        )
        self.attempt.upstream_transaction_data = {
            "toluna_notification": {
                "event_id": self.notification.pk,
                "event_type": self.notification.event_type,
            }
        }
        self.attempt.save(update_fields=["upstream_transaction_data", "updated_at"])

    def test_verified_notification_status_renders_without_browser_hmac(self):
        response = self.client.get(
            reverse("survey-status"),
            {"status": SurveyAttempt.Status.OVER_QUOTA, "rid": self.attempt.rid},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Verified Toluna notification")
        self.assertContains(response, "Terminated")
        self.assertContains(response, "Quota full")
        self.assertNotContains(response, "Sensitive-Member-Code")
        self.assertNotContains(response, "Sensitive-Signature")

    def test_verified_notification_status_refresh_is_read_only(self):
        original_callback_count = self.attempt.callback_count
        original_last_callback_at = self.attempt.last_callback_at

        for _ in range(2):
            response = self.client.get(
                reverse("survey-status"),
                {"status": SurveyAttempt.Status.OVER_QUOTA, "rid": self.attempt.rid},
            )
            self.assertEqual(response.status_code, 200)

        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.callback_count, original_callback_count)
        self.assertEqual(self.attempt.last_callback_at, original_last_callback_at)

    @patch("surveys.views.get_provider")
    def test_mismatched_status_still_requires_and_rejects_missing_hmac(self, get_provider):
        get_provider.return_value.verify_callback.side_effect = ProviderError(
            "Missing callback signature."
        )

        response = self.client.get(
            reverse("survey-status"),
            {"status": SurveyAttempt.Status.COMPLETED, "rid": self.attempt.rid},
        )

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Invalid Toluna callback", status_code=403)
        get_provider.return_value.verify_callback.assert_called_once()

    @patch("surveys.views.get_provider")
    def test_unverified_notification_source_cannot_bypass_hmac(self, get_provider):
        self.attempt.is_verified = False
        self.attempt.save(update_fields=["is_verified", "updated_at"])
        get_provider.return_value.verify_callback.side_effect = ProviderError(
            "Missing callback signature."
        )

        response = self.client.get(
            reverse("survey-status"),
            {"status": SurveyAttempt.Status.OVER_QUOTA, "rid": self.attempt.rid},
        )

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Invalid Toluna callback", status_code=403)
        get_provider.return_value.verify_callback.assert_called_once()
