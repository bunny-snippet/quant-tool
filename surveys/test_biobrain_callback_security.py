from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from vendors.models import Client, ClientIntegration

from .innovatemr_callbacks import sign_callback_url
from .models import Survey, SurveyAttempt


@override_settings(
    BIOBRAIN_CALLBACK_HASH_KEY="biobrain-postback-secret",
    BIOBRAIN_CALLBACK_HASH_ALGORITHM="sha256",
    BIOBRAIN_CALLBACK_HASH_REQUIRED=True,
)
class BioBrainCallbackSecurityTests(TestCase):
    def setUp(self):
        client = Client.objects.create(
            code="biobrain-callback-test",
            name="BioBrain",
            provider_code="biobrain",
        )
        integration = ClientIntegration.objects.create(
            client=client,
            name="BioBrain callback test",
            provider_code="biobrain",
            base_url="https://partner-api.voqall.com/api/v1/surveys",
        )
        survey = Survey.objects.create(
            source_id=77001,
            source_key="77001",
            client=client,
            integration=integration,
            status=Survey.Status.LIVE,
        )
        self.attempt = SurveyAttempt.objects.create(
            rid="BioHash001",
            survey=survey,
            user_id="callback-test",
            status=SurveyAttempt.Status.REDIRECTED,
            initiated_at=timezone.now() - timedelta(minutes=5),
        )

    def _signed_url(self, status, attempt=None):
        attempt = attempt or self.attempt
        path = reverse("survey-status")
        unsigned = f"http://testserver{path}?status={status}&pid={attempt.pid}"
        signature = sign_callback_url(unsigned, "biobrain-postback-secret", "sha256")
        return f"{path}?status={status}&pid={attempt.pid}&hash={signature}"

    def test_all_signed_terminal_statuses_are_accepted(self):
        expected = {
            "1": SurveyAttempt.Status.COMPLETED,
            "2": SurveyAttempt.Status.TERMINATED,
            "3": SurveyAttempt.Status.OVER_QUOTA,
            "4": SurveyAttempt.Status.QUALITY_TERMINATED,
        }
        for index, (status_code, attempt_status) in enumerate(expected.items()):
            with self.subTest(status=status_code):
                attempt = self.attempt if index == 0 else SurveyAttempt.objects.create(
                    rid=f"BioHash00{index + 1}",
                    survey=self.attempt.survey,
                    user_id=f"callback-test-{index + 1}",
                    status=SurveyAttempt.Status.REDIRECTED,
                    initiated_at=timezone.now() - timedelta(minutes=5),
                )
                response = self.client.get(self._signed_url(status_code, attempt))
                self.assertEqual(response.status_code, 302)
                attempt.refresh_from_db()
                self.assertEqual(attempt.status, attempt_status)
                self.assertEqual(attempt.status_source, "biobrain_signed_redirect")
                self.assertTrue(attempt.is_verified)

    def test_status_changed_before_first_hit_is_rejected(self):
        tampered = self._signed_url("4").replace("status=4", "status=1")
        response = self.client.get(tampered)
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Invalid survey callback", status_code=403)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, SurveyAttempt.Status.REDIRECTED)
        self.assertIsNone(self.attempt.callback_at)

    def test_terminal_result_cannot_be_replaced_by_another_signed_status(self):
        first = self.client.get(self._signed_url("4"))
        self.assertEqual(first.status_code, 302)
        replay = self.client.get(self._signed_url("1"))
        self.assertEqual(replay.status_code, 409)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, SurveyAttempt.Status.QUALITY_TERMINATED)
        self.assertEqual(self.attempt.callback_count, 1)

    def test_unknown_attempt_never_renders_success_page(self):
        response = self.client.get(reverse("survey-status"), {
            "status": "1",
            "rid": "Unknown001",
        })
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Invalid survey callback", status_code=404)
        self.assertNotContains(response, "Thank you for participating", status_code=404)

    @override_settings(
        BIOBRAIN_CALLBACK_HASH_KEY="",
        BIOBRAIN_CALLBACK_HASH_REQUIRED=False,
    )
    def test_unsigned_completion_is_accepted_when_hash_enforcement_is_disabled(self):
        response = self.client.get(reverse("survey-status"), {
            "status": "1",
            "vq_token": self.attempt.pid,
        })
        self.assertEqual(response.status_code, 302)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, SurveyAttempt.Status.COMPLETED)
        self.assertIsNotNone(self.attempt.callback_at)
