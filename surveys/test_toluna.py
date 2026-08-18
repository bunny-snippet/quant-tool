import hashlib
import hmac
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from vendors.models import Client, ClientIntegration
from vendors.serializers import ClientIntegrationSerializer
from prescreener_vault.constants import DATABASE_ALIAS
from prescreener_vault.models import PrescreenerSubmission

from .models import Survey, SurveyAttempt, TolunaMember, TolunaReferenceQuestion
from .provider_services import sync_client_integration
from .providers import ProviderError
from .providers.toluna import TolunaProvider
from .serializers import SurveyListSerializer


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self.payload = payload
        self.status_code = status_code
        self.content = b"" if payload is None else b"json"
        self.text = text

    def json(self):
        return self.payload


class RecordingSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


CULTURES = [{"CultureID": 1, "Name": "en-us", "Description": "United States English"}]
REFERENCE = [
    {
        "IsRoutable": False,
        "InternalName": "Gender",
        "TranslatedQuestion": {"QuestionID": 1001007, "CultureID": 1, "DisplayNameTranslation": "What is your gender?"},
        "TranslatedAnswers": [
            {"AnswerID": 2000246, "Translation": "Female", "AnswerInternalName": "Female"},
            {"AnswerID": 2000247, "Translation": "Male", "AnswerInternalName": "Male"},
        ],
        "AnswerType": "SingleSelect",
    },
]
QUOTAS = {
    "CountryID": 1,
    "CacheExpires": "2026-08-18T10:00:00Z",
    "Surveys": [{
        "SurveyID": 71,
        "SurveyName": "Toluna test survey",
        "WaveID": 72,
        "LOI": 8,
        "IR": 45,
        "StudyTypeID": 1,
        "DeviceTypeIDs": [1, 2],
        "CompletesRequired": 20,
        "EstimatedCompletesRemaining": 12,
        "Price": {"Amount": 2.75, "CurrencyID": 1},
        "Quotas": [{
            "QuotaID": 900,
            "CompletesRequired": 20,
            "EstimatedCompletesRemaining": 12,
            "Layers": [
                {"LayerID": 1, "SubQuotas": [{"SubQuotaID": 10, "QuestionsAndAnswers": [{"QuestionID": 1001538, "AnswerIDs": [2006353], "AnswerValues": ["25-29"]}]}]},
                {"LayerID": 2, "SubQuotas": [{"SubQuotaID": 20, "QuestionsAndAnswers": [{"QuestionID": 1001007, "AnswerIDs": [2000247], "AnswerValues": []}]}]},
            ],
        }],
    }],
}


@patch.dict("os.environ", {
    "TOLUNA_API_AUTH_KEY": "api-key",
    "TOLUNA_PARTNER_AUTH_KEY": "reference-key",
    "TOLUNA_HMAC_KEY": "hmac-secret",
    "TOLUNA_PANEL_EN_US": "panel-guid",
}, clear=False)
class TolunaProviderTests(TestCase):
    databases = {"default", DATABASE_ALIAS}
    def setUp(self):
        client = Client.objects.create(code="toluna", name="Toluna", provider_code="toluna")
        self.integration = ClientIntegration.objects.create(
            client=client,
            name="Toluna production",
            provider_code="toluna",
            base_url="https://tws.toluna.com",
            credential_env_keys={
                "api_auth_key": "TOLUNA_API_AUTH_KEY",
                "partner_auth_key": "TOLUNA_PARTNER_AUTH_KEY",
                "hmac_key": "TOLUNA_HMAC_KEY",
                "panel_en_us": "TOLUNA_PANEL_EN_US",
            },
            config={"environment": "production", "callback_hash_required": True},
        )

    def test_inventory_uses_reference_and_quota_apis_without_persisting_panel_guid(self):
        session = RecordingSession(FakeResponse(CULTURES), FakeResponse(REFERENCE), FakeResponse(QUOTAS))
        provider = TolunaProvider(self.integration, session=session)
        rows = provider.inventory()
        normalized = provider.normalize_inventory_item(rows[0], timezone.now())

        self.assertEqual(normalized.source_key, "71:72")
        self.assertIsNone(normalized.numeric_source_id)
        self.assertEqual(normalized.values["cpi"], Decimal("2.75"))
        self.assertEqual(normalized.values["country_code"], "US")
        self.assertEqual(TolunaReferenceQuestion.objects.filter(integration=self.integration).count(), 2)
        self.assertNotIn("panel-guid", str(rows))
        self.assertEqual(session.calls[2][2]["headers"]["API_AUTH_KEY"], "api-key")

    @patch("surveys.provider_services.get_provider")
    def test_inventory_sync_persists_stable_fallback_timestamps(self, get_provider_mock):
        get_provider_mock.return_value = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse(CULTURES), FakeResponse(REFERENCE), FakeResponse(QUOTAS)),
        )
        first = sync_client_integration(self.integration)
        survey = Survey.objects.get(integration=self.integration, source_key="71:72")
        created_at = survey.source_created_at
        modified_at = survey.source_modified_at

        self.assertEqual(first.created, 1)
        self.assertIsNotNone(created_at)
        self.assertEqual(modified_at, created_at)

        get_provider_mock.return_value = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse(CULTURES), FakeResponse(QUOTAS)),
        )
        second = sync_client_integration(self.integration)
        survey.refresh_from_db()

        self.assertEqual(second.unchanged, 1)
        self.assertEqual(survey.source_created_at, created_at)
        self.assertEqual(survey.source_modified_at, modified_at)

    @patch("surveys.serializers.has_function_access", return_value=True)
    def test_blank_upstream_link_still_returns_toluna_platform_copy_link(self, _access):
        user = get_user_model().objects.create_user(
            username="toluna-copy", email="toluna-copy@example.test", password="test-password"
        )
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key="71:72",
            company_name="Toluna",
            name="Toluna test survey",
            status=Survey.Status.LIVE,
            entry_link="",
        )
        request = RequestFactory().get("/api/surveys/")
        request.user = user

        data = SurveyListSerializer(survey, context={"request": request}).data

        self.assertIn("/survey/start?", data["start_link"])
        self.assertIn("surveyId=71%3A72", data["start_link"])

    def test_member_registration_quota_match_and_invite_build(self):
        bootstrap = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse(CULTURES), FakeResponse(REFERENCE), FakeResponse(QUOTAS)),
        )
        payload = bootstrap.inventory()[0]
        normalized = bootstrap.normalize_inventory_item(payload, timezone.now())
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key=normalized.source_key,
            **normalized.values,
        )
        bootstrap.refresh_details(survey)
        attempt = SurveyAttempt.objects.create(
            rid="Abc123XyZ9",
            prescreener_uid="Ab1c-De2f-Gh3i-Jk4l",
            survey=survey,
            user_id="1",
        )
        questions = {row.question_id: row for row in survey.targeting_questions.all()}
        self.assertEqual(questions[1001538].question_type, "numeric")
        self.assertEqual(questions[1001538].text, "What is your age?")
        answers = {
            str(questions[1001538].pk): {
                "question_id": 1001538, "question_key": questions[1001538].key,
                "values": ["27"], "upstream_values": ["2006353"],
            },
            str(questions[1001007].pk): {
                "question_id": 1001007, "question_key": questions[1001007].key,
                "values": ["2000247"], "upstream_values": ["2000247"],
            },
        }
        invite = {
            "SurveyId": 71, "WaveID": 72, "QuotaID": 900,
            "MemberAmount": 0, "PartnerAmount": 3.25,
            "URL": "https://router.toluna.test/invite?token=abc", "LOI": 7, "IR": 40,
        }
        session = RecordingSession(FakeResponse(None, 201), FakeResponse(invite))
        provider = TolunaProvider(self.integration, session=session)
        outbound = provider.build_outbound_url(survey, attempt, answers)

        self.assertEqual([call[0] for call in session.calls], ["POST", "GET"])
        member_body = session.calls[0][2]["json"]
        self.assertEqual(member_body["PartnerGUID"], "panel-guid")
        self.assertEqual(member_body["MemberCode"], attempt.prescreener_uid)
        self.assertNotIn("PostalCode", member_body)
        born = datetime.strptime(member_body["BirthDate"], "%m/%d/%Y").date()
        calculated_age = date.today().year - born.year - (
            (date.today().month, date.today().day) < (born.month, born.day)
        )
        self.assertEqual(calculated_age, 27)
        self.assertEqual(
            member_body["BirthDate"],
            TolunaProvider._birth_date(27, attempt.prescreener_uid),
        )
        self.assertEqual(provider.last_member_summary, {
            "member_id": attempt.prescreener_uid,
            "birth_date": member_body["BirthDate"],
        })
        self.assertEqual(member_body["RegistrationAnswers"][0]["QuestionID"], 1001007)
        self.assertEqual(parse_qs(urlsplit(outbound).query)["rid"], [attempt.rid])
        self.assertEqual(attempt.source_cpi_snapshot, Decimal("3.25"))
        self.assertTrue(TolunaMember.objects.get(member_code=attempt.prescreener_uid).is_registered)

        # The same vault UID and unchanged profile must not be registered a
        # second time. Only a fresh invite is requested for the new journey.
        repeat_session = RecordingSession(FakeResponse(invite))
        repeat_outbound = TolunaProvider(
            self.integration, session=repeat_session
        ).build_outbound_url(survey, attempt, answers)
        self.assertEqual([call[0] for call in repeat_session.calls], ["GET"])
        self.assertEqual(parse_qs(urlsplit(repeat_outbound).query)["rid"], [attempt.rid])

    def test_member_ready_page_shows_identity_then_redirects_once(self):
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key="71:72",
            company_name="Toluna",
            name="Toluna test survey",
            status=Survey.Status.LIVE,
        )
        attempt = SurveyAttempt.objects.create(
            rid="Rdy123AbC9",
            prescreener_uid="Ab1c-De2f-Gh3i-Jk4l",
            survey=survey,
            user_id="1",
            submitted_at=timezone.now(),
            outbound_url="https://router.toluna.test/invite?token=abc",
        )
        session = self.client.session
        session[f"toluna_member_ready_{attempt.rid}"] = {
            "member_id": attempt.prescreener_uid,
            "birth_date": "08/12/1999",
        }
        session.save()
        url = reverse("toluna-member-ready")

        response = self.client.get(url, {"rid": attempt.rid})
        self.assertContains(response, attempt.prescreener_uid)
        self.assertContains(response, "08/12/1999")
        self.assertContains(response, "Continue to survey")

        response = self.client.post(url, {"rid": attempt.rid})
        self.assertRedirects(
            response,
            attempt.outbound_url,
            fetch_redirect_response=False,
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.REDIRECTED)
        self.assertIsNotNone(attempt.redirected_at)

        replay = self.client.post(url, {"rid": attempt.rid})
        self.assertEqual(replay.status_code, 409)

    @override_settings(PRESCREENER_VAULT_ENABLED=True)
    @patch("surveys.views.get_provider")
    def test_complete_prescreener_vault_member_confirmation_and_redirect_flow(self, get_provider_mock):
        bootstrap = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse(CULTURES), FakeResponse(REFERENCE), FakeResponse(QUOTAS)),
        )
        normalized = bootstrap.normalize_inventory_item(bootstrap.inventory()[0], timezone.now())
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key=normalized.source_key,
            **normalized.values,
        )
        bootstrap.refresh_details(survey)
        user = get_user_model().objects.create_user(
            username="toluna-flow", email="toluna-flow@example.test", password="test-password"
        )
        attempt = SurveyAttempt.objects.create(
            rid="Flw123AbC9",
            prescreener_uid="Lm1n-Op2q-Rs3t-Uv4w",
            survey=survey,
            platform_user=user,
            user_id=str(user.pk),
        )
        questions = {row.question_id: row for row in survey.targeting_questions.all()}
        invite = {
            "SurveyId": 71, "WaveID": 72, "QuotaID": 900,
            "MemberAmount": 0, "PartnerAmount": 3.25,
            "URL": "https://router.toluna.test/invite?token=full-flow", "LOI": 7, "IR": 40,
        }
        provider = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse(None, 201), FakeResponse(invite)),
        )
        get_provider_mock.return_value = provider

        response = self.client.post(reverse("survey-start"), {
            "rid": attempt.rid,
            f"question_{questions[1001538].pk}": "27",
            f"question_{questions[1001007].pk}": "2000247",
        })

        self.assertRedirects(
            response,
            f"{reverse('toluna-member-ready')}?rid={attempt.rid}",
            fetch_redirect_response=False,
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.INITIATED)
        self.assertIsNotNone(attempt.submitted_at)
        self.assertIsNone(attempt.redirected_at)
        self.assertTrue(attempt.outbound_url)
        self.assertTrue(
            PrescreenerSubmission.objects.using(DATABASE_ALIAS).filter(
                rid=attempt.rid,
                uid=attempt.prescreener_uid,
                respondent_age=27,
                respondent_gender="male",
            ).exists()
        )

        ready = self.client.get(reverse("toluna-member-ready"), {"rid": attempt.rid})
        self.assertContains(ready, attempt.prescreener_uid)
        self.assertContains(ready, provider.last_member_summary["birth_date"])

        continued = self.client.post(reverse("toluna-member-ready"), {"rid": attempt.rid})
        self.assertRedirects(
            continued,
            attempt.outbound_url,
            fetch_redirect_response=False,
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.REDIRECTED)
        self.assertIsNotNone(attempt.redirected_at)

    def test_toluna_validation_error_exposes_only_safe_diagnostic_fields(self):
        provider = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse({
                "Result": "INVALID_PROPERTY_DATA",
                "ResultCode": 7,
                "Message": "PostalCode is invalid",
                "PartnerGUID": "must-not-be-echoed",
                "MemberCode": "must-not-be-echoed",
            }, status_code=400)),
        )

        with self.assertRaisesRegex(ProviderError, "INVALID_PROPERTY_DATA") as raised:
            provider._request("POST", "https://toluna.test/member")

        self.assertNotIn("must-not-be-echoed", str(raised.exception))

    def test_callback_hmac_verifies_exact_url_with_trailing_ampersand(self):
        unsigned = "http://testserver/survey?status=1&rid=Abc123XyZ9&"
        signature = hmac.new(b"hmac-secret", unsigned.encode(), hashlib.sha256).hexdigest()
        request = RequestFactory().get(f"/survey?status=1&rid=Abc123XyZ9&hash={signature}")
        provider = TolunaProvider(self.integration, session=RecordingSession())
        self.assertTrue(provider.verify_callback(request))

    def test_serializer_rejects_non_numeric_interval_and_resets_verification_after_edit(self):
        invalid = ClientIntegrationSerializer(
            self.integration,
            data={"sync_interval_seconds": "soon"},
            partial=True,
        )
        self.assertFalse(invalid.is_valid())
        self.assertIn("sync_interval_seconds", invalid.errors)

        self.integration.last_test_status = "success"
        self.integration.scheduled_sync_enabled = True
        self.integration.save(update_fields=["last_test_status", "scheduled_sync_enabled", "updated_at"])
        serializer = ClientIntegrationSerializer(
            self.integration,
            data={"config": {"environment": "sandbox", "callback_hash_required": True}},
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        updated = serializer.save()
        self.assertEqual(updated.last_test_status, "")
        self.assertFalse(updated.scheduled_sync_enabled)

    def test_serializer_requires_hmac_reference_when_callback_verification_is_enabled(self):
        credential_refs = dict(self.integration.credential_env_keys)
        credential_refs.pop("hmac_key")
        serializer = ClientIntegrationSerializer(
            self.integration,
            data={
                "credential_env_keys": credential_refs,
                "config": {"environment": "production", "callback_hash_required": True},
            },
            partial=True,
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("credential_env_keys", serializer.errors)
