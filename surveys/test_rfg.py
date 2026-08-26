import hashlib
import hmac
import json
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from vendors.models import Client, ClientIntegration

from .models import Survey, SurveyAttempt, SurveyQuota, TargetingQuestion
from .provider_services import sync_client_integration
from .providers.base import NormalizedSurvey, ProviderConfigurationError
from .providers.rfg import ResearchForGoodProvider
from .serializers import SurveyQuotaSerializer, TargetingQuestionSerializer
from .views import SurveyViewSet


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class RecordingSession:
    def __init__(self, payload):
        self.payload = payload
        self.request = None

    def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return FakeResponse(self.payload)


class FakeProvider:
    def __init__(self, integration):
        self.integration = integration

    def inventory(self):
        return [{"rfg_id": "RFG123456-001", "lastModified": "2026-08-09T10:00:00Z"}]

    def normalize_inventory_item(self, payload, seen_at):
        return NormalizedSurvey(
            source_key=payload["rfg_id"],
            numeric_source_id=None,
            modified_at=datetime(2026, 8, 9, 10, tzinfo=dt_timezone.utc),
            values={
                "company_name": self.integration.client.name,
                "name": "RFG Opinion Study",
                "status": Survey.Status.LIVE,
                "sample_size": 100,
                "completes": 10,
                "remaining": 90,
                "cpi": Decimal("2.50"),
                "country": "US",
                "country_code": "US",
                "source_modified_at": datetime(2026, 8, 9, 10, tzinfo=dt_timezone.utc),
                "last_seen_at": seen_at,
                "raw_data": payload,
            },
            raw_data=payload,
        )


@override_settings(PUBLIC_SUPPLIER_CODE="1000")
class ResearchForGoodIntegrationTests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(code="rfg-client", name="Research For Good", provider_code="rfg")
        self.integration = ClientIntegration.objects.create(
            client=self.client_record,
            name="RFG Live Alert",
            provider_code="rfg",
            base_url="https://api.researchforgood.com/API/",
            credential_env_keys={"apid": "RFG_APID", "secret": "RFG_SECRET"},
            sync_interval_seconds=60,
        )

    @patch.dict("os.environ", {"RFG_APID": "publisher", "RFG_SECRET": "00112233445566778899aabbccddeeff"}, clear=False)
    def test_request_signs_exact_json_with_hmac_sha1(self):
        session = RecordingSession({"result": 0, "response": {"marker": "quest-tool-1700000000"}})
        provider = ResearchForGoodProvider(self.integration, session=session, clock=lambda: 1700000000)
        provider.test_connection()
        request_url, kwargs = session.request
        self.assertEqual(request_url, "https://api.researchforgood.com/API")
        body = kwargs["data"].decode()
        expected = hmac.new(
            bytes.fromhex("00112233445566778899aabbccddeeff"),
            f"1700000000{body}".encode(),
            hashlib.sha1,
        ).hexdigest()
        self.assertEqual(kwargs["params"], {"apid": "publisher", "time": "1700000000", "hash": expected})
        self.assertEqual(json.loads(body)["command"], "test/copy/1")

    def test_missing_environment_secret_fails_without_storing_secret(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ProviderConfigurationError):
                ResearchForGoodProvider(self.integration)
        self.assertEqual(self.integration.credential_env_keys["secret"], "RFG_SECRET")

    @patch("surveys.provider_services.get_provider")
    def test_sync_is_scoped_by_client_and_accepts_string_provider_id(self, get_provider_mock):
        get_provider_mock.return_value = FakeProvider(self.integration)
        other_client = Client.objects.create(code="other-rfg", name="Other RFG", provider_code="rfg")
        Survey.objects.create(client=other_client, source_key="RFG123456-001", name="Same provider ID, other account")
        run = sync_client_integration(self.integration, refresh_details=False)
        self.assertEqual(run.created, 1)
        survey = Survey.objects.get(client=self.client_record, source_key="RFG123456-001")
        self.assertEqual(survey.integration, self.integration)
        self.assertEqual(survey.cpi, Decimal("2.50"))
        self.assertEqual(Survey.objects.filter(source_key="RFG123456-001").count(), 2)

    def test_superadmin_can_discover_provider_and_create_non_secret_integration(self):
        admin = get_user_model().objects.create_superuser("owner", "owner@example.com", "pass")
        api = APIClient(); api.force_authenticate(admin)
        response = api.get("/api/v1/vendors/integrations/providers/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["code"], "rfg")
        self.assertEqual(
            {provider["code"] for provider in response.json()},
            {"rfg", "toluna", "innovatemr", "custom"},
        )
        response = api.post("/api/v1/vendors/integrations/", {
            "client": self.client_record.pk,
            "name": "RFG UI connection",
            "provider_code": "rfg",
            "base_url": "https://api.researchforgood.com/API/",
            "credential_env_keys": {"apid": "RFG_APID", "secret": "RFG_SECRET"},
            "config": {
                "country": "US",
                "category": "B2C",
                "allow_recontacts": False,
                "enforce_local_targeting": True,
                "callback_security_mode": "ip",
            },
            "supplier_code": "1000",
            "sync_interval_seconds": 60,
            "detail_refresh_batch": 3,
            "scheduled_sync_enabled": False,
            "is_active": True,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(response.json()["sync_interval_seconds"], 60)
        created_integration_id = response.json()["id"]
        response = api.patch(f"/api/v1/vendors/integrations/{created_integration_id}/", {
            "config": {
                "country": "US",
                "category": "B2C",
                "allow_recontacts": False,
                "enforce_local_targeting": False,
                "callback_security_mode": "ip",
            },
        }, format="json")
        self.assertEqual(response.status_code, 200, response.json())
        self.assertFalse(response.json()["config"]["enforce_local_targeting"])
        response = api.get(f"/api/v1/vendors/integrations/{self.integration.pk}/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["credential_env_keys"]["secret"], "RFG_SECRET")
        self.assertNotIn("00112233445566778899aabbccddeeff", json.dumps(body))
        self.client.force_login(admin)
        page = self.client.get("/organization/")
        self.assertEqual(page.status_code, 200)

        integration_page = self.client.get("/client-integrations/")
        self.assertEqual(integration_page.status_code, 200)
        self.assertContains(integration_page, "Test connection")
        self.assertContains(integration_page, "RFG credential references")
        self.assertContains(integration_page, "No provider is assumed automatically")
        self.assertContains(integration_page, "Custom REST API")
        self.assertContains(integration_page, "Strict local targeting")
        self.assertContains(integration_page, "Relaxed flow")
        self.assertNotContains(integration_page, 'id="provider" value="innovatemr"')
        self.assertNotContains(integration_page, 'placeholder="InnovateMR production"')

    @patch("surveys.views.get_provider")
    def test_unsynced_live_rfg_survey_has_copy_link_and_hydrates_on_first_start(self, get_provider_mock):
        admin = get_user_model().objects.create_superuser(
            "rfg-link-owner", "rfg-link-owner@example.com", "pass"
        )
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="RFG605150-lazy-link",
            country_code="US",
            status=Survey.Status.LIVE,
            entry_link="",
        )
        api = APIClient()
        api.force_authenticate(admin)
        listing = api.get("/api/v1/surveys/", {"search": survey.source_key})
        self.assertEqual(listing.status_code, 200)
        start_link = listing.data["results"][0]["start_link"]
        self.assertIn(f"surveyId={survey.source_key}", start_link)
        self.assertIn("supplierCode=1000", start_link)

        def hydrate(target):
            target.entry_link = "https://survey.saysoforgood.com/live/survey/lazy?init=token"
            target.targeting_synced_at = timezone.now()
            target.quota_synced_at = timezone.now()
            target.detail_synced_at = timezone.now()
            target.save(update_fields=[
                "entry_link", "targeting_synced_at", "quota_synced_at",
                "detail_synced_at", "updated_at",
            ])

        get_provider_mock.return_value.refresh_details.side_effect = hydrate
        response = self.client.get(start_link)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/survey/start?rid=", response["Location"])
        get_provider_mock.assert_called_once_with(self.integration)
        get_provider_mock.return_value.refresh_details.assert_called_once()
        survey.refresh_from_db()
        self.assertTrue(survey.entry_link)

    @patch.dict("os.environ", {"RFG_APID": "publisher", "RFG_SECRET": "00112233445566778899aabbccddeeff"}, clear=False)
    def test_prescreener_validates_targeting_and_builds_required_rfg_url(self):
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="RFG605150-001",
            local_id="20260812345678",
            country_code="US",
            status=Survey.Status.LIVE,
            entry_link="https://survey.saysoforgood.com/live/survey/example?init=token",
        )
        birthday = TargetingQuestion.objects.create(
            survey=survey, question_id=-1, key="RFG_BIRTHDAY", question_type="date",
            raw_data={"targeting_age_ranges": [{"min": 18, "max": 35}]},
        )
        gender = TargetingQuestion.objects.create(
            survey=survey, question_id=-2, key="RFG_GENDER", question_type="single",
            raw_data={"targeting_choices": [1]},
        )
        postal = TargetingQuestion.objects.create(
            survey=survey, question_id=-3, key="RFG_POSTAL_CODE", question_type="text",
        )
        income = TargetingQuestion.objects.create(
            survey=survey, question_id=-4, key="household_income", text="Household income",
            question_type="single", options=[
                {"OptionId": 1, "OptionText": "Under target"},
                {"OptionId": 2, "OptionText": "Target answer"},
            ], raw_data={"targeting_choices": [2]},
        )
        answers = {
            str(birthday.pk): {"question_key": birthday.key, "upstream_values": ["2000-01-01"]},
            str(gender.pk): {"question_key": gender.key, "upstream_values": ["M"]},
            str(postal.pk): {"question_key": postal.key, "upstream_values": ["12 345"]},
            str(income.pk): {"question_key": income.key, "upstream_values": ["2"]},
        }
        attempt = SurveyAttempt.objects.create(
            rid="Abc123Xyz9", survey=survey, user_id="42"
        )
        provider = ResearchForGoodProvider(self.integration)
        self.assertEqual(provider.validate_prescreener(survey, answers), (True, ""))
        outbound = parse_qs(urlsplit(provider.build_outbound_url(survey, attempt, answers)).query)
        self.assertEqual(outbound["rid"], [attempt.rid])
        self.assertEqual(outbound["country"], ["US"])
        self.assertEqual(outbound["postalCode"], ["12345"])
        self.assertEqual(outbound["gender"], ["M"])
        self.assertEqual(outbound["birthday"], ["2000-01-01"])
        self.assertEqual(outbound["household_income"], ["2"])
        self.assertEqual(outbound["code"], [survey.local_id])

        answers[str(birthday.pk)]["upstream_values"] = ["32"]
        self.assertEqual(provider.validate_prescreener(survey, answers), (True, ""))
        age_outbound = parse_qs(
            urlsplit(provider.build_outbound_url(survey, attempt, answers)).query
        )
        self.assertEqual(provider._age_on(age_outbound["birthday"][0]), 32)

        answers[str(income.pk)]["upstream_values"] = ["1"]
        eligible, reason = provider.validate_prescreener(survey, answers)
        self.assertFalse(eligible)
        self.assertIn("Household income", reason)
        answers[str(income.pk)]["upstream_values"] = ["2"]
        answers[str(postal.pk)]["upstream_values"] = ["invalid"]
        self.assertFalse(provider.validate_prescreener(survey, answers)[0])

        self.integration.config = {"enforce_local_targeting": False}
        self.integration.save(update_fields=["config"])
        answers[str(postal.pk)]["upstream_values"] = ["12345"]
        answers[str(income.pk)]["upstream_values"] = ["1"]
        relaxed_provider = ResearchForGoodProvider(self.integration)
        self.assertEqual(relaxed_provider.validate_prescreener(survey, answers), (True, ""))
        relaxed_url = parse_qs(urlsplit(relaxed_provider.build_outbound_url(survey, attempt, answers)).query)
        self.assertEqual(relaxed_url["household_income"], ["1"])

    @patch.dict("os.environ", {"RFG_APID": "publisher", "RFG_SECRET": "00112233445566778899aabbccddeeff"}, clear=False)
    def test_detail_refresh_keeps_all_answers_and_marks_qualifying_choices(self):
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="RFG605150-002",
            country_code="US",
            status=Survey.Status.LIVE,
        )
        provider = ResearchForGoodProvider(self.integration)
        targeting = {
            "datapoints": [{"name": "Income", "values": [{"choice": 2}]}],
            "quotas": [],
        }
        metadata = {
            "property": "income",
            "type": 0,
            "question": {"en-US": "What is your income? %%3867%%"},
            "answers": [None, {"en-US": "Lower"}, {"en-US": "Target %%3867%%"}],
        }
        with patch.object(provider, "targeting", return_value=targeting), patch.object(
            provider, "datapoint", return_value=metadata
        ), patch.object(provider, "create_link", return_value="https://survey.saysoforgood.com/live/example"):
            provider.refresh_details(survey)
        question = survey.targeting_questions.get(key="income")
        self.assertEqual(question.text, "What is your income?")
        self.assertEqual([item["OptionId"] for item in question.options], [1, 2])
        self.assertEqual(question.options[1]["OptionText"], "Target")
        self.assertEqual(question.raw_data["targeting_choices"], [2])
        self.assertEqual(question.raw_data["adapter_version"], 2)
        serialized = TargetingQuestionSerializer(question).data
        self.assertFalse(serialized["options"][0]["Qualifies"])
        self.assertTrue(serialized["options"][1]["Qualifies"])
        self.assertEqual(serialized["targeting_note"], "Qualifying answer: Target")

    def test_rfg_quota_serializer_does_not_present_unknown_totals_as_zero(self):
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="RFG605150-quota",
        )
        TargetingQuestion.objects.create(
            survey=survey,
            question_id=-401,
            key="income",
            text="Income",
            options=[
                {"OptionId": 1, "OptionText": "Lower"},
                {"OptionId": 2, "OptionText": "Target"},
            ],
            raw_data={"targeting": {"name": "Income"}, "targeting_choices": [2]},
        )
        quota = SurveyQuota.objects.create(
            survey=survey,
            source_key="quota-one",
            name="Completes quota",
            remaining=0,
            status="Open",
            targeting={"datapoints": [{"name": "Income", "values": [{"choice": 2}]}]},
            raw_data={
                "completesLeft": 0,
                "quotaLimitBy": "completes",
                "datapoints": [{"name": "Income", "values": [{"choice": 2}]}],
            },
        )
        payload = SurveyQuotaSerializer(quota).data
        self.assertFalse(payload["target_known"])
        self.assertFalse(payload["completed_known"])
        self.assertEqual(payload["status"], "Full")
        self.assertEqual(payload["scope_label"], "Targeted respondent quota")
        self.assertEqual(payload["targeting_details"], [{"name": "Income", "values": ["Target"]}])

    def test_legacy_rfg_markers_are_cleaned_in_detail_api_output(self):
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="RFG605150-legacy",
        )
        question = TargetingQuestion.objects.create(
            survey=survey,
            question_id=-99,
            key="rfg63867",
            text="What type of Spotify subscription do you currently have? Please select one.%%3867%%",
            options=[{"OptionId": 1, "OptionText": "Premium %%3867%%"}],
        )
        payload = TargetingQuestionSerializer(question).data
        self.assertEqual(
            payload["text"],
            "What type of Spotify subscription do you currently have? Please select one.",
        )
        self.assertEqual(payload["options"][0]["OptionText"], "Premium")

    @patch("surveys.views.get_provider")
    def test_stale_rfg_eye_details_use_rfg_provider(self, get_provider_mock):
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="RFG605150-stale",
        )
        SurveyViewSet._refresh_if_stale(survey, "targeting")
        get_provider_mock.assert_called_once_with(self.integration)
        get_provider_mock.return_value.refresh_details.assert_called_once_with(survey)

    @patch("surveys.views.get_provider")
    def test_legacy_rfg_age_adapter_is_refreshed(self, get_provider_mock):
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="RFG605150-legacy-age",
            targeting_synced_at=timezone.now(),
        )
        TargetingQuestion.objects.create(
            survey=survey,
            question_id=-101,
            key="RFG_BIRTHDAY",
            text="What is your date of birth?",
            question_type="date",
            raw_data={"adapter_version": 3, "targeting_age_ranges": []},
        )

        SurveyViewSet._refresh_if_stale(survey, "targeting")

        get_provider_mock.assert_called_once_with(self.integration)
        get_provider_mock.return_value.refresh_details.assert_called_once_with(survey)

    def test_rfg_age_conversion_rejects_age_over_99(self):
        self.assertEqual(
            ResearchForGoodProvider._age_from_age_or_date("99"),
            99,
        )
        with self.assertRaises(ValueError):
            ResearchForGoodProvider._age_from_age_or_date("100")
        with self.assertRaises(ValueError):
            ResearchForGoodProvider._birthday_from_age_or_date("1900-01-01")

    @patch.dict("os.environ", {"RFG_APID": "publisher", "RFG_SECRET": "00112233445566778899aabbccddeeff"}, clear=False)
    def test_duplicate_check_uses_official_fingerprint_when_available(self):
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="RFG605150-004",
        )
        attempt = SurveyAttempt.objects.create(
            rid="Ghi789QrS4", survey=survey, user_id="42"
        )
        session = RecordingSession({"result": 0, "response": {"isDuplicate": False}})
        provider = ResearchForGoodProvider(self.integration, session=session)
        fingerprint = "c042ac342900efdfceee4a2edb549f5c"
        self.assertFalse(provider.duplicate_check(
            survey, attempt, "187.143.120.25", fingerprint
        ))
        payload = json.loads(session.request[1]["data"].decode())
        self.assertEqual(payload["fingerprint"], fingerprint)
        self.assertEqual(payload["rid"], attempt.rid)
        self.assertEqual(payload["ip"], "187.143.120.25")

    def test_browser_result_shows_reason_without_trusting_unverified_complete(self):
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="RFG605150-003",
            local_id="20260812345679",
            country_code="US",
        )
        attempt = SurveyAttempt.objects.create(
            rid="Def456UvW7",
            survey=survey,
            user_id="42",
            status=SurveyAttempt.Status.REDIRECTED,
        )
        response = self.client.get("/survey/rfg/result", {
            "rid": attempt.rid,
            "result": "41",
            "ruledOutBy": "Postal code failed provider validation %%3867%%",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid postal code")
        self.assertContains(response, "Postal code failed provider validation")
        self.assertNotContains(response, "%%3867%%")
        self.assertContains(response, "Secure server confirmation is still pending")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.REDIRECTED)
        self.assertFalse(attempt.is_verified)
        self.assertEqual(
            attempt.upstream_transaction_data["rfg_browser_return"]["result"], "41"
        )

    @patch.dict("os.environ", {"RFG_APID": "publisher", "RFG_SECRET": "00112233445566778899aabbccddeeff"}, clear=False)
    def test_non_matching_prescreener_finishes_locally_with_reason_page(self):
        user = get_user_model().objects.create_superuser(
            "respondent-admin", "respondent@example.com", "pass"
        )
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="RFG605150-005",
            local_id="20260812345680",
            country_code="US",
            status=Survey.Status.LIVE,
            entry_link="https://survey.saysoforgood.com/live/survey/example?init=token",
        )
        birthday = TargetingQuestion.objects.create(
            survey=survey, question_id=-11, key="RFG_BIRTHDAY", text="Date of birth",
            question_type="date", raw_data={"targeting_age_ranges": [{"min": 18, "max": 35}]},
        )
        gender = TargetingQuestion.objects.create(
            survey=survey, question_id=-12, key="RFG_GENDER", text="Gender",
            question_type="single", options=[
                {"OptionId": "M", "OptionText": "Male"},
                {"OptionId": "F", "OptionText": "Female"},
            ], raw_data={"targeting_choices": [1]},
        )
        postal = TargetingQuestion.objects.create(
            survey=survey, question_id=-13, key="RFG_POSTAL_CODE", text="Postal code",
            question_type="text",
        )
        income = TargetingQuestion.objects.create(
            survey=survey, question_id=-14, key="income", text="Household income %%3867%%",
            question_type="single", options=[
                {"OptionId": 1, "OptionText": "Non-matching"},
                {"OptionId": 2, "OptionText": "Matching"},
            ], raw_data={"targeting_choices": [2]},
        )
        attempt = SurveyAttempt.objects.create(
            rid="Jkl012MnO5", survey=survey, platform_user=user, user_id=str(user.pk)
        )
        prescreener = self.client.get("/survey/start", {"rid": attempt.rid})
        self.assertEqual(prescreener.status_code, 200)
        self.assertContains(prescreener, "Matching")
        self.assertNotContains(prescreener, "Non-matching")
        self.assertContains(prescreener, "Male")
        self.assertNotContains(prescreener, "Female")
        self.assertContains(prescreener, "Only answers accepted by this survey are shown.")
        self.assertContains(prescreener, "What is your date of birth?")
        self.assertContains(prescreener, "Qualifying age: 18–35")
        self.assertContains(prescreener, 'placeholder="DD-MM-YYYY"', html=False)
        self.assertContains(prescreener, f'name="question_{birthday.pk}"', html=False)
        self.assertContains(prescreener, 'data-date-mask', html=False)
        self.assertNotContains(prescreener, 'type="date"', html=False)
        self.assertNotContains(prescreener, 'class="survey-context"', html=False)
        self.assertNotContains(prescreener, "About 18 min")
        self.assertNotContains(prescreener, "Continue to survey")
        self.assertContains(prescreener, ">Submit</button>", html=False)
        invalid_date = self.client.post("/survey/start", {
            "rid": attempt.rid,
            f"question_{birthday.pk}": "31-02-2000",
            f"question_{gender.pk}": "M",
            f"question_{postal.pk}": "12345",
            f"question_{income.pk}": "2",
            "rfg_fingerprint": "0",
        })
        self.assertEqual(invalid_date.status_code, 200)
        self.assertContains(invalid_date, "Enter a valid date")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.INITIATED)
        response = self.client.post("/survey/start", {
            "rid": attempt.rid,
            f"question_{birthday.pk}": "01-01-2000",
            f"question_{gender.pk}": "M",
            f"question_{postal.pk}": "12345",
            f"question_{income.pk}": "1",
            "rfg_fingerprint": "0",
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Early termination")
        self.assertContains(response, "Household income")
        self.assertNotContains(response, "%%3867%%")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.TERMINATED)
        self.assertEqual(attempt.status_source, "local_prescreener")
        self.assertIsNotNone(attempt.callback_at)
        self.assertIn("rfg_local_outcome", attempt.upstream_transaction_data)

        self.integration.config = {"enforce_local_targeting": False}
        self.integration.save(update_fields=["config"])
        relaxed_attempt = SurveyAttempt.objects.create(
            rid="Mno345PqR6", survey=survey, platform_user=user, user_id=str(user.pk)
        )
        with patch.object(ResearchForGoodProvider, "duplicate_check", return_value=False):
            response = self.client.post("/survey/start", {
                "rid": relaxed_attempt.rid,
                f"question_{birthday.pk}": "01-01-2000",
                f"question_{gender.pk}": "M",
                f"question_{postal.pk}": "12345",
                f"question_{income.pk}": "1",
                "rfg_fingerprint": "0",
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlsplit(response["Location"]).netloc, "survey.saysoforgood.com")
        outbound_query = parse_qs(urlsplit(response["Location"]).query)
        self.assertEqual(outbound_query["income"], ["1"])
        self.assertEqual(outbound_query["birthday"], ["2000-01-01"])
        relaxed_attempt.refresh_from_db()
        self.assertEqual(relaxed_attempt.status, SurveyAttempt.Status.REDIRECTED)

    def test_trusted_rfg_callback_completes_attempt(self):
        self.integration.last_test_status = "success"
        self.integration.save(update_fields=["last_test_status"])
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="RFG123456-001",
            entry_link="https://rfg.example/start",
            country_code="US",
        )
        attempt = SurveyAttempt.objects.create(rid="Abc123Xyz9", survey=survey, user_id="42", status=SurveyAttempt.Status.REDIRECTED)
        response = self.client.get("/survey/rfg/callback", {
            "result": "1", "rid": attempt.rid, "sesskey": "rfg-session-1",
        }, REMOTE_ADDR="15.222.163.99")
        self.assertEqual(response.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.COMPLETED)
        self.assertTrue(attempt.is_verified)
        self.assertEqual(attempt.status_source, "rfg_callback")
        self.assertEqual(
            attempt.upstream_transaction_data["rfg_outcome"]["title"],
            "Survey completed",
        )
        result_page = self.client.get("/survey/rfg/result", {"rid": attempt.rid})
        self.assertContains(result_page, "Survey completed")
        self.assertContains(result_page, "Result recorded")
        self.assertNotContains(result_page, "Secure server confirmation is still pending")
