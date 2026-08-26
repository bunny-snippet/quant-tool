import hashlib
import hmac

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from vendors.models import Client, ClientIntegration

from .models import Survey, SurveyAttempt, SurveyQuota, TolunaNotification


@override_settings(
    TOLUNA_NOTIFICATION_IP_ALLOWLIST=("203.0.113.10/32",),
    TOLUNA_NOTIFICATION_TRUSTED_PROXY_IPS=("127.0.0.1/32", "::1/128"),
    TOLUNA_NOTIFICATION_HMAC_KEY="notification-hmac-key",
)
class TolunaNotificationTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(
            username="toluna-notification-owner",
            email="owner@example.test",
            password="test-password",
        )
        self.client_record = Client.objects.create(
            code="toluna-notification-client",
            name="Toluna",
            provider_code="toluna",
        )
        self.integration = ClientIntegration.objects.create(
            client=self.client_record,
            name="Toluna notification test",
            provider_code="toluna",
            base_url="https://tws.toluna.com",
        )
        self.survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_id=123,
            source_key="123:100",
            name="Toluna notification survey",
            company_name="Toluna",
            country="United States",
            country_code="US",
            buyer_id="toluna-buyer",
            status=Survey.Status.LIVE,
        )
        self.attempt = SurveyAttempt.objects.create(
            rid="Toluna123A",
            prescreener_uid="Ab1c-De2f-Gh3i-Jk4l",
            provider_profile_uid="Ab1c-De2f-Gh3i-Jk4l",
            survey=self.survey,
            user_id="1",
            status=SurveyAttempt.Status.REDIRECTED,
            redirected_at=timezone.now(),
        )
        self.headers = {"REMOTE_ADDR": "203.0.113.10"}

    @staticmethod
    def _member_status_hmac(payload):
        signed_value = (
            f"{payload.get('SurveyId', payload.get('SurveyID'))}"
            f"{payload.get('WaveId', payload.get('WaveID'))}"
            f"{payload.get('UniqueCode')}"
        )
        return hmac.new(
            b"notification-hmac-key",
            signed_value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _post(self, route_name, payload, *, sign=True, **headers):
        payload = dict(payload)
        if sign and route_name in {
            "toluna-notification-member-complete",
            "toluna-notification-member-terminate",
        }:
            payload.setdefault("EncryptedValue", self._member_status_hmac(payload))
        return self.client.post(
            reverse(route_name),
            payload,
            content_type="application/json",
            **(headers or self.headers),
        )

    def test_member_termination_is_authenticated_matched_and_applied(self):
        response = self._post("toluna-notification-member-terminate", {
            "UniqueCode": self.attempt.prescreener_uid,
            "SurveyId": 123,
            "SurveyRef": "123560-US",
            "Reason": "QuotaFull",
            "DateTime": "2026-08-21 10:15:00",
            "WaveId": 100,
            "QuotaID": 900,
            "AdditionalData": f"rid={self.attempt.rid}&source=test",
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["applied"])
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, SurveyAttempt.Status.OVER_QUOTA)
        self.assertEqual(self.attempt.status_source, "toluna_notification_member_terminate")
        notification = TolunaNotification.objects.get()
        self.assertEqual(notification.attempt, self.attempt)
        self.assertEqual(notification.reason, "QuotaFull")
        self.assertNotIn("notification-test-token", str(notification.raw_payload))

    def test_exact_duplicate_is_acknowledged_without_second_status_mutation(self):
        payload = {
            "UniqueCode": self.attempt.prescreener_uid,
            "SurveyId": 123,
            "SurveyRef": "123560-US",
            "Revenue": 100,
            "DateTime": "2026-08-21 10:20:00",
            "WaveId": 100,
            "QuotaID": 900,
            "AdditionalData": f"rid={self.attempt.rid}",
        }
        first = self._post("toluna-notification-member-complete", payload)
        refreshed_payload = {**payload, "DateTime": "2026-08-21 10:20:04"}
        second = self._post("toluna-notification-member-complete", refreshed_payload)

        self.assertFalse(first.json()["duplicate"])
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(TolunaNotification.objects.count(), 1)
        notification = TolunaNotification.objects.get()
        self.assertEqual(notification.duplicate_count, 1)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, SurveyAttempt.Status.COMPLETED)
        self.assertEqual(self.attempt.callback_count, 1)

    def test_unapproved_direct_source_cannot_spoof_forwarding_headers(self):
        response = self._post(
            "toluna-notification-survey-closed",
            {"SurveyID": 123, "SurveyRef": "Survey", "Status": "Closed", "WaveId": 100},
            REMOTE_ADDR="198.51.100.40",
            HTTP_X_FORWARDED_FOR="203.0.113.10",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(TolunaNotification.objects.count(), 0)

    def test_trusted_proxy_uses_rightmost_untrusted_forwarded_hop(self):
        payload = {
            "SurveyID": 123,
            "SurveyRef": "Survey",
            "Status": "Closed",
            "WaveId": 100,
        }
        accepted = self._post(
            "toluna-notification-survey-closed",
            payload,
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_FORWARDED_FOR="198.51.100.40, 203.0.113.10",
        )
        rejected = self._post(
            "toluna-notification-survey-closed",
            payload,
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_FORWARDED_FOR="203.0.113.10, 198.51.100.40",
        )

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(TolunaNotification.objects.count(), 1)

    def test_member_status_requires_valid_encrypted_value(self):
        payload = {
            "UniqueCode": self.attempt.prescreener_uid,
            "SurveyId": 123,
            "SurveyRef": "123560-US",
            "Revenue": 100,
            "DateTime": "2026-08-21 10:20:00",
            "WaveId": 100,
            "QuotaID": 900,
            "AdditionalData": f"rid={self.attempt.rid}",
        }
        missing = self._post(
            "toluna-notification-member-complete",
            payload,
            sign=False,
        )
        invalid = self._post(
            "toluna-notification-member-complete",
            {**payload, "EncryptedValue": "0" * 64},
            sign=False,
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(TolunaNotification.objects.count(), 0)

    @override_settings(TOLUNA_NOTIFICATION_IP_ALLOWLIST=())
    def test_empty_source_allowlist_fails_closed(self):
        response = self._post(
            "toluna-notification-survey-closed",
            {"SurveyID": 123, "SurveyRef": "Survey", "Status": "Closed", "WaveId": 100},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(TolunaNotification.objects.count(), 0)

    def test_quota_and_survey_notifications_update_operational_records(self):
        quota = SurveyQuota.objects.create(
            survey=self.survey,
            source_key="900",
            quota_id=900,
            remaining=12,
            status="Open",
        )
        quota_response = self._post("toluna-notification-quota-status", {
            "QuotaID": 900,
            "SurveyID": 123,
            "WaveID": 100,
            "IsLive": False,
            "UpdateDateTimeUTC": "2026-08-21 10:25:00 +00:00",
        })
        close_response = self._post("toluna-notification-survey-closed", {
            "SurveyID": 123,
            "SurveyRef": "123560-US",
            "Status": "Closed",
            "DateTime": "2026-08-21 10:30:00",
            "WaveId": 100,
        })

        self.assertEqual(quota_response.status_code, 200)
        self.assertEqual(close_response.status_code, 200)
        quota.refresh_from_db()
        self.survey.refresh_from_db()
        self.assertEqual(quota.status, "Full")
        self.assertEqual(quota.remaining, 0)
        self.assertEqual(self.survey.status, Survey.Status.CLOSED)
        self.assertEqual(self.survey.remaining, 0)

    def test_provider_filter_opens_toluna_notification_tabs_and_clean_details(self):
        self._post("toluna-notification-enhanced-termination", {
            "UniqueCode": self.attempt.prescreener_uid,
            "SurveyId": 123,
            "SurveyRef": "123560-US",
            "Reason": "Terminated",
            "DateTime": "2026-08-21 10:35:00",
            "WaveId": 100,
            "AdditionalData": f"rid={self.attempt.rid}",
            "RejectionID": 103,
            "RejectionName": "NonQuotaDemographicRejection",
        })
        notification = TolunaNotification.objects.get()
        self.client.force_login(self.owner)

        response = self.client.get(reverse("termination-reasons"), {
            "provider": "toluna",
            "toluna_event": "enhanced_termination",
            "toluna_detail": notification.pk,
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["show_toluna_notifications"])
        self.assertContains(response, '?provider=toluna')
        self.assertContains(response, 'data-toluna-async-panel')
        self.assertContains(response, 'data-toluna-tab-link', count=6)
        self.assertContains(response, "Toluna notification centre")
        self.assertContains(response, "Enhanced termination")
        self.assertContains(response, "NonQuotaDemographicRejection")
        self.assertNotContains(response, "raw_payload")
