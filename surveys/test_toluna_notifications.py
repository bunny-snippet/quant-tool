import hashlib
import hmac
import io
import zipfile
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import EmployeeProfile, Role
from vendors.models import Client, ClientIntegration
from vendors.models import OrganizationUnit

from .models import Survey, SurveyAttempt, SurveyQuota, TolunaNotification
from .toluna_notifications import reconcile_toluna_operational_notifications


@override_settings(
    TOLUNA_NOTIFICATION_IP_ALLOWLIST=("203.0.113.10/32",),
    TOLUNA_NOTIFICATION_TRUSTED_PROXY_IPS=("127.0.0.1/32", "::1/128"),
    TOLUNA_NOTIFICATION_HMAC_KEY="notification-hmac-key",
    TOLUNA_NOTIFICATION_REQUIRE_HMAC=False,
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

    def test_member_status_accepts_unsigned_payload_from_allowlisted_source(self):
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

        self.assertEqual(missing.status_code, 200)
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(TolunaNotification.objects.count(), 2)

    @override_settings(TOLUNA_NOTIFICATION_REQUIRE_HMAC=True)
    def test_member_status_can_require_a_valid_encrypted_value(self):
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

    def test_operational_notifications_without_wave_are_rejected_without_mutation(self):
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
            "IsLive": False,
            "UpdateDateTimeUTC": "2026-08-21 10:25:00 +00:00",
        })
        close_response = self._post("toluna-notification-survey-closed", {
            "SurveyID": 123,
            "SurveyRef": "123560-US",
            "Status": "Closed",
            "DateTime": "2026-08-21 10:30:00",
        })

        self.assertEqual(quota_response.status_code, 400)
        self.assertEqual(close_response.status_code, 400)
        self.assertEqual(TolunaNotification.objects.count(), 0)
        quota.refresh_from_db()
        self.survey.refresh_from_db()
        self.assertEqual(quota.status, "Open")
        self.assertEqual(quota.remaining, 12)
        self.assertEqual(self.survey.status, Survey.Status.LIVE)

    def test_wave_id_never_falls_back_to_another_wave(self):
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
            "WaveID": 101,
            "IsLive": False,
            "UpdateDateTimeUTC": "2026-08-21 10:25:00 +00:00",
        })
        close_response = self._post("toluna-notification-survey-closed", {
            "SurveyID": 123,
            "SurveyRef": "123560-US",
            "Status": "Closed",
            "DateTime": "2026-08-21 10:30:00",
            "WaveId": 101,
        })

        self.assertFalse(quota_response.json()["applied"])
        self.assertFalse(close_response.json()["applied"])
        quota.refresh_from_db()
        self.survey.refresh_from_db()
        self.assertEqual(quota.status, "Open")
        self.assertEqual(quota.remaining, 12)
        self.assertEqual(self.survey.status, Survey.Status.LIVE)
        self.assertFalse(TolunaNotification.objects.filter(survey=self.survey).exists())
        self.assertTrue(all(
            row.processing_message.startswith("Pending reconciliation; exact Toluna survey 123 / wave 101")
            for row in TolunaNotification.objects.all()
        ))

    def test_quota_id_is_never_matched_outside_the_exact_survey(self):
        other_survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_id=456,
            source_key="456:200",
            name="Other Toluna survey",
            company_name="Toluna",
            country="United States",
            country_code="US",
            buyer_id="toluna-buyer",
            status=Survey.Status.LIVE,
        )
        other_quota = SurveyQuota.objects.create(
            survey=other_survey,
            source_key="901",
            quota_id=901,
            remaining=8,
            status="Open",
        )

        response = self._post("toluna-notification-quota-status", {
            "QuotaID": 901,
            "SurveyID": 123,
            "WaveID": 100,
            "IsLive": False,
            "UpdateDateTimeUTC": "2026-08-21 10:25:00 +00:00",
        })

        self.assertFalse(response.json()["applied"])
        other_quota.refresh_from_db()
        self.assertEqual(other_quota.status, "Open")
        notification = TolunaNotification.objects.get()
        self.assertEqual(notification.survey, self.survey)
        self.assertIn("quota 901 is not available on exact Toluna survey 123 / wave 100", notification.processing_message)

    def test_duplicate_pending_delivery_relinks_and_applies_after_inventory_arrives(self):
        payload = {
            "QuotaID": 902,
            "SurveyID": 123,
            "WaveID": 101,
            "IsLive": False,
            "UpdateDateTimeUTC": "2026-08-21 10:25:00 +00:00",
        }
        first = self._post("toluna-notification-quota-status", payload)
        self.assertFalse(first.json()["applied"])

        exact_survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="123:101",
            name="Exact later Toluna wave",
            company_name="Toluna",
            country="United States",
            country_code="US",
            buyer_id="toluna-buyer",
            status=Survey.Status.LIVE,
        )
        exact_quota = SurveyQuota.objects.create(
            survey=exact_survey,
            source_key="902",
            quota_id=902,
            remaining=7,
            status="Open",
        )

        second = self._post("toluna-notification-quota-status", payload)

        self.assertTrue(second.json()["duplicate"])
        self.assertTrue(second.json()["applied"])
        self.assertEqual(TolunaNotification.objects.count(), 1)
        notification = TolunaNotification.objects.get()
        self.assertEqual(notification.duplicate_count, 1)
        self.assertEqual(notification.survey, exact_survey)
        exact_quota.refresh_from_db()
        self.assertEqual(exact_quota.status, "Full")
        self.assertEqual(exact_quota.remaining, 0)

    def test_pending_operational_notification_reconciles_after_rows_exist(self):
        response = self._post("toluna-notification-quota-status", {
            "QuotaID": 903,
            "SurveyID": 123,
            "WaveID": 100,
            "IsLive": False,
            "UpdateDateTimeUTC": "2026-08-21 10:25:00 +00:00",
        })
        self.assertFalse(response.json()["applied"])
        quota = SurveyQuota.objects.create(
            survey=self.survey,
            source_key="903",
            quota_id=903,
            remaining=9,
            status="Open",
        )

        reconciled = reconcile_toluna_operational_notifications(self.survey)

        self.assertEqual(reconciled, 1)
        quota.refresh_from_db()
        notification = TolunaNotification.objects.get()
        self.assertTrue(notification.applied)
        self.assertEqual(quota.status, "Full")
        self.assertEqual(quota.remaining, 0)

    def test_stale_quota_event_cannot_overwrite_newer_provider_state(self):
        quota = SurveyQuota.objects.create(
            survey=self.survey,
            source_key="904",
            quota_id=904,
            remaining=12,
            status="Open",
        )
        newer = self._post("toluna-notification-quota-status", {
            "QuotaID": 904,
            "SurveyID": 123,
            "WaveID": 100,
            "IsLive": False,
            "UpdateDateTimeUTC": "2026-08-21 10:30:00 +00:00",
        })
        older = self._post("toluna-notification-quota-status", {
            "QuotaID": 904,
            "SurveyID": 123,
            "WaveID": 100,
            "IsLive": True,
            "UpdateDateTimeUTC": "2026-08-21 10:25:00 +00:00",
        })

        self.assertTrue(newer.json()["applied"])
        self.assertTrue(older.json()["applied"])
        quota.refresh_from_db()
        self.assertEqual(quota.status, "Full")
        self.assertEqual(quota.remaining, 0)
        stale = TolunaNotification.objects.get(is_live=True)
        self.assertIn("newer provider update already controls quota 904", stale.processing_message)

        # A detail refresh can replace the row from an older inventory cache.
        # Replaying applied events must restore the latest provider state only.
        quota.status = "Open"
        quota.remaining = 12
        quota.save(update_fields=["status", "remaining", "updated_at"])
        reconcile_toluna_operational_notifications(self.survey, replay_applied=True)
        quota.refresh_from_db()
        self.assertEqual(quota.status, "Full")
        self.assertEqual(quota.remaining, 0)

    def test_newer_open_quota_event_restores_minimum_routable_capacity(self):
        quota = SurveyQuota.objects.create(
            survey=self.survey,
            source_key="905",
            quota_id=905,
            remaining=12,
            status="Open",
        )
        self._post("toluna-notification-quota-status", {
            "QuotaID": 905,
            "SurveyID": 123,
            "WaveID": 100,
            "IsLive": False,
            "UpdateDateTimeUTC": "2026-08-21 10:25:00 +00:00",
        })
        reopened = self._post("toluna-notification-quota-status", {
            "QuotaID": 905,
            "SurveyID": 123,
            "WaveID": 100,
            "IsLive": True,
            "UpdateDateTimeUTC": "2026-08-21 10:30:00 +00:00",
        })

        self.assertTrue(reopened.json()["applied"])
        quota.refresh_from_db()
        self.assertEqual(quota.status, "Open")
        self.assertEqual(quota.remaining, 1)

    def test_applied_notification_received_after_inventory_boundary_is_replayed(self):
        quota = SurveyQuota.objects.create(
            survey=self.survey,
            source_key="906",
            quota_id=906,
            remaining=12,
            status="Open",
        )
        self._post("toluna-notification-quota-status", {
            "QuotaID": 906,
            "SurveyID": 123,
            "WaveID": 100,
            "IsLive": False,
            "UpdateDateTimeUTC": "2026-08-21 10:35:00 +00:00",
        })
        notification = TolunaNotification.objects.get(quota_id=906)
        quota.status = "Open"
        quota.remaining = 12
        quota.save(update_fields=["status", "remaining", "updated_at"])

        reconcile_toluna_operational_notifications(
            self.survey,
            replay_applied=True,
            applied_since=notification.received_at - timedelta(microseconds=1),
        )

        quota.refresh_from_db()
        self.assertEqual(quota.status, "Full")
        self.assertEqual(quota.remaining, 0)

    @patch("surveys.toluna_notifications.OPERATIONAL_RECONCILE_BATCH_SIZE", 3)
    def test_pending_operational_reconciliation_is_bounded(self):
        quota = SurveyQuota.objects.create(
            survey=self.survey,
            source_key="907",
            quota_id=907,
            remaining=12,
            status="Open",
        )
        for index in range(4):
            TolunaNotification.objects.create(
                event_type=TolunaNotification.EventType.QUOTA_STATUS,
                payload_hash=f"bounded-pending-{index}",
                integration=self.integration,
                survey=self.survey,
                provider_survey_id=123,
                wave_id=100,
                quota_id=907,
                provider_status="Unavailable",
                is_live=False,
                applied=False,
                raw_payload={
                    "SurveyID": 123,
                    "WaveID": 100,
                    "QuotaID": 907,
                    "IsLive": False,
                    "UpdateDateTimeUTC": f"2026-08-21 10:4{index}:00 +00:00",
                },
            )

        reconciled = reconcile_toluna_operational_notifications(self.survey)

        self.assertEqual(reconciled, 3)
        self.assertEqual(TolunaNotification.objects.filter(applied=True).count(), 3)
        self.assertEqual(TolunaNotification.objects.filter(applied=False).count(), 1)
        quota.refresh_from_db()
        self.assertEqual(quota.status, "Full")

    @patch("surveys.toluna_notifications.OPERATIONAL_RECONCILE_BATCH_SIZE", 1)
    def test_recent_applied_survey_close_has_priority_over_pending_backlog(self):
        pending = TolunaNotification.objects.create(
            event_type=TolunaNotification.EventType.QUOTA_STATUS,
            payload_hash="old-pending-before-close",
            integration=self.integration,
            survey=self.survey,
            provider_survey_id=123,
            wave_id=100,
            quota_id=999,
            provider_status="Unavailable",
            is_live=False,
            applied=False,
            raw_payload={
                "SurveyID": 123,
                "WaveID": 100,
                "QuotaID": 999,
                "IsLive": False,
            },
        )
        close = TolunaNotification.objects.create(
            event_type=TolunaNotification.EventType.SURVEY_CLOSED,
            payload_hash="recent-applied-close",
            integration=self.integration,
            survey=self.survey,
            provider_survey_id=123,
            wave_id=100,
            provider_status="Closed",
            applied=True,
            raw_payload={
                "SurveyID": 123,
                "WaveID": 100,
                "Status": "Closed",
            },
        )
        self.survey.status = Survey.Status.LIVE
        self.survey.remaining = 10
        self.survey.save(update_fields=["status", "remaining", "updated_at"])

        reconciled = reconcile_toluna_operational_notifications(
            self.survey,
            replay_applied=True,
            applied_since=close.received_at - timedelta(microseconds=1),
        )

        self.assertEqual(reconciled, 1)
        self.survey.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(self.survey.status, Survey.Status.CLOSED)
        self.assertEqual(self.survey.remaining, 0)
        self.assertFalse(pending.applied)

    def test_dedicated_page_shows_clean_notification_details_and_term_report_does_not(self):
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

        response = self.client.get(reverse("toluna-notifications"), {
            "event": "enhanced_termination",
            "detail": notification.pk,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_page"], "toluna-notifications")
        self.assertContains(response, "Toluna notification centre")
        self.assertContains(response, "Enhanced termination")
        self.assertContains(response, "NonQuotaDemographicRejection")
        self.assertNotContains(response, "raw_payload")

        term_response = self.client.get(reverse("termination-reasons"), {
            "provider": "toluna",
        })
        self.assertEqual(term_response.status_code, 200)
        self.assertNotContains(term_response, "Toluna notification centre")

    def test_notification_filters_cover_hierarchy_provider_and_survey_fields(self):
        branch = OrganizationUnit.objects.create(
            workspace_owner=self.owner,
            unit_type=OrganizationUnit.UnitType.BRANCH,
            name="Delhi",
            code="toluna-notification-delhi",
            created_by=self.owner,
        )
        sub_branch = OrganizationUnit.objects.create(
            workspace_owner=self.owner,
            parent=branch,
            unit_type=OrganizationUnit.UnitType.SUB_BRANCH,
            name="Operations",
            code="toluna-notification-operations",
            created_by=self.owner,
        )
        shift = OrganizationUnit.objects.create(
            workspace_owner=self.owner,
            parent=sub_branch,
            unit_type=OrganizationUnit.UnitType.SHIFT,
            name="Morning",
            code="toluna-notification-morning",
            created_by=self.owner,
        )
        EmployeeProfile.objects.filter(user=self.owner).update(organization_unit=shift)
        self.attempt.platform_user = self.owner
        self.attempt.save(update_fields=["platform_user", "updated_at"])
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
        notification.provider_status = "Rejected"
        notification.applied = True
        notification.save(update_fields=["provider_status", "applied", "last_received_at"])
        self.client.force_login(self.owner)

        response = self.client.get(reverse("toluna-notifications"), {
            "search": self.survey.local_id,
            "branch": branch.pk,
            "sub_branch": sub_branch.pk,
            "shift": shift.pk,
            "user": self.owner.pk,
            "event": TolunaNotification.EventType.ENHANCED_TERMINATION,
            "notification_status": "Rejected",
            "applied": "applied",
            "country": "US",
            "client": self.client_record.pk,
            "buyer_id": self.survey.buyer_id,
            "date_field": "received",
            "date_from": (timezone.now() - timedelta(minutes=5)).isoformat(timespec="minutes"),
            "date_to": (timezone.now() + timedelta(minutes=5)).isoformat(timespec="minutes"),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        self.assertEqual(response.context["page_obj"].object_list[0].pk, notification.pk)

    def test_unmatched_operational_notification_stays_visible_until_survey_filter(self):
        matched = TolunaNotification.objects.create(
            event_type=TolunaNotification.EventType.QUOTA_STATUS,
            payload_hash="matched-operational-notification",
            integration=self.integration,
            survey=self.survey,
            provider_survey_id=123,
            wave_id=100,
            provider_status="Open",
            applied=True,
        )
        unmatched = TolunaNotification.objects.create(
            event_type=TolunaNotification.EventType.SURVEY_CLOSED,
            payload_hash="unmatched-operational-notification",
            integration=self.integration,
            provider_survey_id=987654,
            wave_id=321,
            provider_status="Closed",
            applied=False,
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("toluna-notifications"))
        self.assertEqual(response.context["page_obj"].paginator.count, 2)
        self.assertIn(unmatched, response.context["page_obj"].object_list)

        filtered = self.client.get(reverse("toluna-notifications"), {"country": "US"})
        self.assertEqual(filtered.context["page_obj"].paginator.count, 1)
        self.assertEqual(filtered.context["page_obj"].object_list[0].pk, matched.pk)

    def test_dedicated_permissions_export_and_term_search_submit_are_isolated(self):
        admin = get_user_model().objects.create_user(username="toluna-notification-admin")
        EmployeeProfile.objects.filter(user=admin).update(role=Role.objects.get(slug="admin"))
        self.client.force_login(admin)

        page = self.client.get(reverse("toluna-notifications"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "<h1>Notifications</h1>", html=True)

        term_page = self.client.get(reverse("termination-reasons"))
        self.assertEqual(term_page.status_code, 200)
        self.assertNotContains(term_page, 'form="reasonFilters" formaction=')
        self.assertContains(term_page, 'class="primary-button export-button" href=')

        TolunaNotification.objects.create(
            event_type=TolunaNotification.EventType.SURVEY_CLOSED,
            payload_hash="notification-export-row",
            integration=self.integration,
            provider_survey_id=123,
            provider_status="Closed",
        )
        notification_export = self.client.get(reverse("toluna-notifications-export"))
        self.assertEqual(notification_export.status_code, 200)
        notification_workbook = b"".join(notification_export.streaming_content)
        with zipfile.ZipFile(io.BytesIO(notification_workbook)) as workbook:
            workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
        self.assertIn("Toluna Notifications", workbook_xml)

        term_export = self.client.get(reverse("termination-reasons-export"), {"provider": "toluna"})
        self.assertEqual(term_export.status_code, 200)
        term_workbook = b"".join(term_export.streaming_content)
        with zipfile.ZipFile(io.BytesIO(term_workbook)) as workbook:
            term_workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
        self.assertNotIn("Toluna Notifications", term_workbook_xml)
