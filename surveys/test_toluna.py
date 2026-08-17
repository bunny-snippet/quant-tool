import hashlib
import hmac
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import RequestFactory, TestCase
from django.utils import timezone

from vendors.models import Client, ClientIntegration
from vendors.serializers import ClientIntegrationSerializer

from .models import Survey, SurveyAttempt, TolunaMember, TolunaReferenceQuestion
from .providers.toluna import TolunaProvider


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.content = b"" if payload is None else b"json"

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
    "TOLUNA_PARTNER_GUID": "partner-guid",
    "TOLUNA_HMAC_KEY": "hmac-secret",
    "TOLUNA_PANEL_EN_US": "panel-guid",
}, clear=False)
class TolunaProviderTests(TestCase):
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
                "partner_guid": "TOLUNA_PARTNER_GUID",
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
        outbound = TolunaProvider(self.integration, session=session).build_outbound_url(survey, attempt, answers)

        self.assertEqual([call[0] for call in session.calls], ["POST", "GET"])
        member_body = session.calls[0][2]["json"]
        self.assertEqual(member_body["MemberCode"], attempt.prescreener_uid)
        born = datetime.strptime(member_body["BirthDate"], "%m/%d/%Y").date()
        calculated_age = date.today().year - born.year - (
            (date.today().month, date.today().day) < (born.month, born.day)
        )
        self.assertEqual(calculated_age, 27)
        self.assertEqual(
            member_body["BirthDate"],
            TolunaProvider._birth_date(27, attempt.prescreener_uid),
        )
        self.assertEqual(member_body["RegistrationAnswers"][0]["QuestionID"], 1001007)
        self.assertEqual(parse_qs(urlsplit(outbound).query)["rid"], [attempt.rid])
        self.assertEqual(attempt.source_cpi_snapshot, Decimal("3.25"))
        self.assertTrue(TolunaMember.objects.get(member_code=attempt.prescreener_uid).is_registered)

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
