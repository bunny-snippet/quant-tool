from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase, override_settings

from .integrations import InnovateMRAPIError, InnovateMRClient
from .models import Survey, TargetingQuestion
from .survey_flow import build_biobrain_outbound_url
from .views import _prescreener_questions


class FakeResponse:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): return None
    def json(self): return self.payload


class CapturingSession:
    def __init__(self, *payloads): self.payloads = list(payloads); self.calls = []
    def get(self, url, **kwargs): self.calls.append((url, kwargs)); return FakeResponse(self.payloads.pop(0))


def integration(**overrides):
    values = {
        "provider_code": "biobrain", "base_url": "https://partner-api.voqall.com/api/v1/surveys",
        "inventory_endpoint": "", "paged_inventory_endpoint": "",
        "quota_endpoint_template": "https://partner-api.voqall.com/api/v1/survey-quotas/{survey_id}",
        "targeting_endpoint_template": "https://partner-api.voqall.com/api/v1/survey-qualifications/{survey_id}",
        "transaction_endpoint_template": "", "auth_header_name": "EQ-PARTNER-ACCESS-KEY", "auth_header_prefix": "",
        "inventory_result_key": "Surveys", "quota_result_key": "Quotas", "targeting_result_key": "Qualifications",
        "transaction_result_key": "result", "field_mapping": {}, "client": SimpleNamespace(name="Bio Brain"),
    }
    values.update(overrides); return SimpleNamespace(**values)


class ConfigurableProviderClientTests(SimpleTestCase):
    @override_settings(INNOVATEMR_API_TOKEN="global-innovate-key")
    def test_client_integration_never_borrows_global_innovate_key(self):
        client = InnovateMRClient(token="", integration=integration(provider_code="custom"))
        with self.assertRaisesRegex(InnovateMRAPIError, "token is not configured"):
            client._headers()

    def test_biobrain_uses_exact_endpoint_header_and_normalizes_inventory(self):
        session = CapturingSession(
            {"status": "ok", "hasError": False, "Surveys": [{"SurveyId": 44, "Name": "Bio study", "Revenue": 2.5, "IncidentRate": 35, "LengthOfInterview": 12, "Completes": 80, "LanguageId": 9, "SurveyUrl": "https://respond.voqall.com/l?vq_sid=44", "Has_Quotas": True, "LastUpdatedOnUTC": "2026-08-09T10:00:00Z"}]},
            {"status": "ok", "hasError": False, "Languages": [{"Id": 9, "Name": "English", "CountryCode": "US"}]},
        )
        surveys = InnovateMRClient(token="secret", session=session, integration=integration()).get_allocated_surveys()
        self.assertEqual(session.calls[0][0], "https://partner-api.voqall.com/api/v1/surveys")
        self.assertEqual(session.calls[0][1]["headers"]["EQ-PARTNER-ACCESS-KEY"], "secret")
        self.assertNotIn("x-access-token", session.calls[0][1]["headers"])
        self.assertEqual((surveys[0]["surveyId"], surveys[0]["surveyName"], surveys[0]["CPI"]), (44, "Bio study", 2.5))
        self.assertEqual((surveys[0]["CountryCode"], surveys[0]["N"], surveys[0]["supCmps"]), ("US", 80, 0))
        self.assertTrue(session.calls[1][0].endswith("/api/v1/collection/languages"))

    def test_biobrain_detail_endpoints_are_configurable(self):
        session = CapturingSession({"hasError": False, "Quotas": [{"QuotaId": 7, "Conditions": []}]}, {"hasError": False, "Qualifications": [{"QualificationId": 9, "OptionIds": [1, 2]}]})
        client = InnovateMRClient(token="secret", session=session, integration=integration())
        self.assertEqual(client.get_quota_for_survey(44)[0]["id"], 7); self.assertEqual(client.get_survey_targeting(44)[0]["QuestionId"], 9)
        self.assertTrue(session.calls[0][0].endswith("/survey-quotas/44")); self.assertTrue(session.calls[1][0].endswith("/survey-qualifications/44"))

    def test_biobrain_targeting_uses_localized_question_and_option_labels(self):
        session = CapturingSession(
            {"hasError": False, "Qualifications": [{"QualificationId": 59, "QualificationTypeId": 1, "OptionIds": [100, 200], "OptionCodes": [1, 2]}]},
            {"hasError": False, "Qualification": {"Id": 59, "Code": "Gender", "QuestionText": "What is your gender?", "TypeName": "Single", "Options": [{"OptionCode": 1, "OptionText": "Male"}, {"OptionCode": 2, "OptionText": "Female"}]}},
        )
        question = InnovateMRClient(token="secret", session=session, integration=integration()).get_survey_targeting(44, language_id=9)[0]
        self.assertEqual(question["QuestionText"], "What is your gender?")
        self.assertEqual(question["QuestionKey"], "Q59")
        self.assertEqual(question["Options"], [
            {"OptionId": 100, "OptionCode": 1, "OptionText": "Male", "Qualifies": True},
            {"OptionId": 200, "OptionCode": 2, "OptionText": "Female", "Qualifies": True},
        ])

    def test_biobrain_outbound_url_uses_canonical_rid_and_profile_uid(self):
        outbound = build_biobrain_outbound_url(
            "https://respond.voqall.com/l?vq_sid=44&vq_vid=7",
            "Abc123xYz9", "uidA-123",
            {"1": {"question_id": 59, "upstream_values": [100]}},
        )
        self.assertIn("vq_token=Abc123xYz9", outbound)
        self.assertIn("vq_uid=uidA-123", outbound)
        self.assertIn("Q59=100", outbound)
        self.assertNotIn("trackId=", outbound)

    def test_custom_provider_field_mapping(self):
        session = CapturingSession({"data": {"items": [{"id": 8, "title": "Custom study"}]}})
        configured = integration(provider_code="custom", base_url="https://example.test/api", inventory_endpoint="surveys", auth_header_name="X-API-Key", inventory_result_key="data.items", field_mapping={"surveyId": "id", "surveyName": "title"})
        survey = InnovateMRClient(token="secret", session=session, integration=configured).get_allocated_surveys()[0]
        self.assertEqual(session.calls[0][0], "https://example.test/api/surveys"); self.assertEqual((survey["surveyId"], survey["surveyName"]), (8, "Custom study"))


class BioBrainPrescreenerCompatibilityTests(TestCase):
    def test_legacy_numeric_options_render_without_server_error(self):
        survey = Survey.objects.create(
            source_id=44,
            source_key="44",
            name="Bio study",
            company_name="BioBrain",
        )
        TargetingQuestion.objects.create(
            survey=survey,
            question_id=59,
            key="Q59",
            text="What is your gender?",
            question_type="Single",
            options=[100, 200],
        )

        question = next(
            item for item in _prescreener_questions(survey)
            if item["model"].question_id == 59
        )

        self.assertEqual(
            question["options"],
            [
                {"value": "100", "label": "100", "selected": False},
                {"value": "200", "label": "200", "selected": False},
            ],
        )
