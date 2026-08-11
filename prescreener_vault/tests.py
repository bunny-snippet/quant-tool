import re
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from surveys.models import Survey, SurveyAttempt, TargetingQuestion
from surveys.survey_flow import create_attempt

from .constants import DATABASE_ALIAS
from .models import PrescreenerAnswer, PrescreenerSubmission
from .services import PrescreenerVaultError


@override_settings(PRESCREENER_VAULT_ENABLED=True)
class PrescreenerVaultFlowTests(TestCase):
    databases = {"default", DATABASE_ALIAS}

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="vault-user",
            first_name="Vault",
            last_name="User",
            email="vault@example.test",
        )
        self.survey = Survey.objects.create(
            source_id=801122,
            name="US profile survey",
            status=Survey.Status.LIVE,
            company_name="Example client",
            country="United States",
            country_code="US",
            language="English",
            language_code="EN",
            entry_link="https://provider.example/start?PID=[%%pid%%]",
        )
        self.age = TargetingQuestion.objects.create(
            survey=self.survey,
            question_id=1,
            key="AGE",
            text="What is your age?",
            question_type="Numeric",
            category="Demographic",
        )
        self.gender = TargetingQuestion.objects.create(
            survey=self.survey,
            question_id=2,
            key="GENDER",
            text="What is your gender?",
            question_type="Single Punch",
            category="Demographic",
            options=[
                {"OptionId": 1, "OptionText": "Male"},
                {"OptionId": 2, "OptionText": "Female"},
            ],
        )

    def _attempt(self):
        return create_attempt(self.survey, self.user, "8.8.8.8")

    def _submit(self, attempt, age="24", gender="1"):
        return self.client.post(reverse("survey-start"), {
            "rid": attempt.rid,
            f"question_{self.age.pk}": age,
            f"question_{self.gender.pk}": gender,
        })

    def test_valid_submission_is_saved_only_in_vault_with_profile_snapshots(self):
        attempt = self._attempt()
        response = self._submit(attempt)
        self.assertEqual(response.status_code, 302)

        attempt.refresh_from_db()
        self.assertRegex(attempt.prescreener_uid, r"^[A-Za-z0-9]{4}(?:-[A-Za-z0-9]{4}){3}$")
        self.assertNotEqual(attempt.prescreener_uid.replace("-", "")[:10], attempt.rid)
        self.assertEqual(attempt.answers, {})

        submission = PrescreenerSubmission.objects.using(DATABASE_ALIAS).get(uid=attempt.prescreener_uid)
        self.assertEqual(submission.rid, attempt.rid)
        self.assertEqual(submission.country_code, "US")
        self.assertEqual(submission.language_code, "EN")
        self.assertEqual(submission.respondent_age, 24)
        self.assertEqual(submission.respondent_age_group, "18-24")
        self.assertEqual(submission.respondent_gender, "male")
        self.assertEqual(submission.answer_count, 2)
        gender = PrescreenerAnswer.objects.using(DATABASE_ALIAS).get(
            submission=submission, question_key="GENDER"
        )
        self.assertEqual(gender.question_text, "What is your gender?")
        self.assertEqual(gender.answer_values, ["1"])
        self.assertEqual(gender.answer_labels, ["Male"])
        self.assertEqual(gender.upstream_values, ["1"])

    def test_same_profile_answers_on_new_links_create_distinct_uid_rows(self):
        first = self._attempt()
        second = self._attempt()
        self.assertEqual(self._submit(first).status_code, 302)
        self.assertEqual(self._submit(second).status_code, 302)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertNotEqual(first.rid, second.rid)
        self.assertNotEqual(first.prescreener_uid, second.prescreener_uid)
        self.assertEqual(PrescreenerSubmission.objects.using(DATABASE_ALIAS).count(), 2)

    def test_vault_failure_does_not_redirect_or_lose_the_retry(self):
        attempt = self._attempt()
        with patch(
            "surveys.views.capture_prescreener_submission",
            side_effect=PrescreenerVaultError("database unavailable"),
        ):
            response = self._submit(attempt)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Secure prescreener storage is temporarily unavailable")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.INITIATED)
        self.assertEqual(attempt.answers, {})

    def test_backfill_can_verify_then_clear_existing_operational_answers(self):
        attempt = SurveyAttempt.objects.create(
            rid="Abc123Xyz9",
            survey=self.survey,
            platform_user=self.user,
            user_id=str(self.user.pk),
            answers={
                str(self.gender.pk): {
                    "question_id": self.gender.question_id,
                    "question_key": self.gender.key,
                    "question_text": self.gender.text,
                    "values": ["2"],
                    "upstream_values": ["2"],
                }
            },
        )
        output = StringIO()
        call_command("backfill_prescreener_vault", "--clear-source", stdout=output)
        attempt.refresh_from_db()
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9]{4}(?:-[A-Za-z0-9]{4}){3}", attempt.prescreener_uid))
        self.assertEqual(attempt.answers, {})
        self.assertTrue(
            PrescreenerSubmission.objects.using(DATABASE_ALIAS).filter(
                uid=attempt.prescreener_uid, rid=attempt.rid
            ).exists()
        )
        self.assertIn("failed=0", output.getvalue())
