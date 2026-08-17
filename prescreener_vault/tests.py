import re
import zipfile
from io import BytesIO
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from xml.etree import ElementTree

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
        self.assertEqual(submission.usage_count, 1)
        gender = PrescreenerAnswer.objects.using(DATABASE_ALIAS).get(
            submission=submission, question_key="GENDER"
        )
        self.assertEqual(gender.question_text, "What is your gender?")
        self.assertEqual(gender.answer_values, ["1"])
        self.assertEqual(gender.answer_labels, ["Male"])
        self.assertEqual(gender.upstream_values, ["1"])

    @patch("surveys.views.resolve_entry_geolocation")
    def test_wrong_entry_country_is_s4_before_prescreener_and_audited(self, geo_lookup):
        geo_lookup.return_value = {
            "ip": "8.8.8.8", "country_code": "IN", "country": "India",
            "postal_code": "110001", "source": "test",
        }
        attempt = self._attempt()

        response = self.client.get(reverse("survey-start"), {"rid": attempt.rid})

        self.assertEqual(response.status_code, 302)
        self.assertIn("status=4", response["Location"])
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.QUALITY_TERMINATED)
        self.assertEqual(attempt.status_source, "local_country_guard")
        self.assertEqual(
            attempt.upstream_transaction_data["local_country_guard"]["reason"],
            "Wrong target country",
        )
        submission = PrescreenerSubmission.objects.using(DATABASE_ALIAS).get(rid=attempt.rid)
        self.assertEqual(submission.respondent_postal_code, "110001")
        self.assertTrue(
            PrescreenerAnswer.objects.using(DATABASE_ALIAS).filter(
                submission=submission,
                question_text="Entry validation result",
                answer_labels=["Wrong target country"],
            ).exists()
        )

    def test_entry_ip_postal_is_added_to_vault_without_a_prescreener_question(self):
        attempt = create_attempt(
            self.survey,
            self.user,
            "8.8.8.8",
            client_data={"geo_country_code": "US", "geo_postal_code": "90210"},
        )

        response = self._submit(attempt)

        self.assertEqual(response.status_code, 302)
        submission = PrescreenerSubmission.objects.using(DATABASE_ALIAS).get(rid=attempt.rid)
        self.assertEqual(submission.respondent_postal_code, "90210")
        self.assertEqual(submission.answer_count, 3)
        self.assertTrue(
            PrescreenerAnswer.objects.using(DATABASE_ALIAS).filter(
                submission=submission,
                question_key="postal_code",
                question_text="Postal code (derived from entry IP)",
            ).exists()
        )

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

    def test_admin_can_filter_and_expand_prescreened_data_page(self):
        attempt = self._attempt()
        self.assertEqual(self._submit(attempt, age="24", gender="1").status_code, 302)
        admin = get_user_model().objects.create_superuser(
            username="vault-admin", email="vault-admin@example.test", password="test-password"
        )
        self.client.force_login(admin)
        response = self.client.get(reverse("prescreened-data"), {
            "country": "US", "age_group": "18-24", "gender": "male",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Panelist Data")
        self.assertContains(response, "Country / Language")
        self.assertContains(response, "Profile Specs")
        self.assertContains(response, "Registered at")
        self.assertContains(response, "Visits")
        self.assertContains(response, "Profile Information")
        self.assertContains(response, "Profile details")
        self.assertNotContains(response, attempt.rid)
        self.assertContains(response, "What is your age?")
        self.assertContains(response, "Male")
        self.assertContains(response, "All countries")
        self.assertContains(response, "vault-answer-drawer")
        self.assertNotContains(response, "<details")

        rid_search = self.client.get(reverse("prescreened-data"), {"search": attempt.rid})
        self.assertContains(rid_search, "No profiles available")
        uid_search = self.client.get(reverse("prescreened-data"), {
            "search": PrescreenerSubmission.objects.using(DATABASE_ALIAS).get(
                rid=attempt.rid
            ).uid,
        })
        self.assertContains(uid_search, "Profile details")

        exported = self.client.get(reverse("prescreened-data-export"), {
            "country": "US", "age_group": "18-24", "gender": "male",
        })
        self.assertEqual(exported.status_code, 200)
        self.assertIn(".xlsx", exported["Content-Disposition"])
        content = b"".join(exported.streaming_content)
        with zipfile.ZipFile(BytesIO(content)) as workbook:
            self.assertIn("xl/worksheets/sheet2.xml", workbook.namelist())
            submissions = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
            answers = ElementTree.fromstring(workbook.read("xl/worksheets/sheet2.xml"))
        submission_text = " ".join(submissions.itertext())
        answer_text = " ".join(answers.itertext())
        self.assertIn(attempt.rid, submission_text)
        self.assertNotIn("Answer count", submission_text)
        self.assertIn("Visits", submission_text)
        self.assertIn("What is your age?", answer_text)
        self.assertIn("Male", answer_text)

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
