from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from vendors.models import Client, ClientIntegration

from .models import Survey
from .providers.acuity import AcuityAnalyticsProvider
from .providers.track_opinion import TrackOpinionProvider
from .providers.unimarket import UniMarketProvider
from .views import _prescreener_questions


class SupplyProviderContractTests(TestCase):
    def integration(self, provider_code, base_url, credentials):
        client = Client.objects.create(
            code=f"{provider_code}-test",
            name=provider_code.replace("_", " ").title(),
            provider_code=provider_code,
        )
        return ClientIntegration.objects.create(
            client=client,
            name="Test supply",
            provider_code=provider_code,
            base_url=base_url,
            credential_env_keys=credentials,
            sync_interval_seconds=300,
        )

    @patch.dict("os.environ", {"TRACK_TEST_TOKEN": "secret"})
    def test_track_merges_repeated_quota_rows_and_hydrates_required_questions(self):
        integration = self.integration(
            "track_opinion",
            "https://stagingsupply.opinionest.com",
            {"token": "TRACK_TEST_TOKEN"},
        )
        survey = Survey.objects.create(
            client=integration.client,
            integration=integration,
            source_key="127564",
            source_id=127564,
            company_name=integration.client.name,
            status=Survey.Status.LIVE,
            country_code="US",
            raw_data={"CountryId": 1},
        )
        provider = TrackOpinionProvider(integration)

        def response(path, **kwargs):
            if "Qualifications" in path:
                return {"surveyQualifications": [
                    {"qualificationId": 10560, "answerIds": [133078]},
                    {"qualificationId": 10558, "answerIds": ["50-64"]},
                ]}
            if "survey-quotas" in path:
                return {"surveyQuotas": [
                    {"quotaId": "169506692", "quotaName": "", "totalRemaining": 25, "criteria": [{"qualificationId": 10560, "answerIds": [133078]}]},
                    {"quotaId": "169506692", "quotaName": "", "totalRemaining": 25, "criteria": [{"qualificationId": 10558, "answerIds": ["50-64"]}]},
                ]}
            return {"TotalRemaining": 25}

        with patch.object(provider, "_request", side_effect=response), patch.object(
            provider,
            "_question_metadata",
            return_value={
                "10560": {"QuestionId": 10560, "Description": "What is your gender?", "QuestionTypeId": 4},
                "10558": {"QuestionId": 10558, "Description": "What is your age?", "QuestionTypeId": 2},
            },
        ), patch.object(
            provider,
            "_answer_labels",
            side_effect=lambda country, question: {"133078": "Male"} if str(question) == "10560" else {},
        ):
            provider.refresh_details(survey)

        self.assertEqual(survey.quotas.count(), 1)
        self.assertEqual(len(survey.quotas.get().raw_data["targeting_details"]), 2)
        prepared = _prescreener_questions(survey)
        self.assertEqual({row["input_kind"] for row in prepared}, {"radio", "number"})
        age = next(row for row in prepared if row["is_age_question"])
        self.assertEqual(age["age_ranges"], [(50, 64)])

    @patch.dict("os.environ", {"UNIMARKET_TEST_TOKEN": "secret"})
    def test_unimarket_catalog_decodes_ids_and_comma_separated_postal_values(self):
        integration = self.integration(
            "unimarket",
            "https://stg-api.supplier.unimrktresponse.net",
            {"token": "UNIMARKET_TEST_TOKEN"},
        )
        survey = Survey.objects.create(
            client=integration.client,
            integration=integration,
            source_key="992312",
            source_id=992312,
            company_name=integration.client.name,
            status=Survey.Status.LIVE,
            country_code="US",
            remaining=30,
            raw_data={"_country_code": "US"},
            entry_link="https://respond.example/landing?umid={umid}&uid={uid}",
        )
        provider = UniMarketProvider(integration)

        def response(path, **kwargs):
            if path.endswith("/questions"):
                return {"questions": [
                    {"questionId": 1002, "typeId": 1, "options": ["1001", "1002"]},
                    {"questionId": 1085, "typeId": 3, "options": ["01001, 01002"]},
                ]}
            if path.endswith("/quotas"):
                return {"quotas": [{
                    "quotaId": 2744,
                    "remaining": 30,
                    "conditions": [
                        {"questionId": 1002, "typeId": 1, "options": ["1001"]},
                        {"questionId": 1085, "typeId": 3, "options": ["01001,01002"]},
                    ],
                }]}
            if path.endswith("/groups"):
                return {"groups": [{"groupId": 489, "groupSurveys": [992312, 992313]}]}
            return {"supplierStats": {"starts": 4, "completes": 2}}

        with patch.object(provider, "_request", side_effect=response):
            provider.refresh_details(survey)

        gender = survey.targeting_questions.get(question_id=1002)
        self.assertEqual([row["OptionText"] for row in gender.options], ["Male", "Female"])
        postal = survey.targeting_questions.get(question_id=1085)
        self.assertEqual(postal.raw_data["targeting_choices"], ["01001", "01002"])
        prepared_postal = next(row for row in _prescreener_questions(survey) if row["is_postal_question"])
        self.assertEqual(prepared_postal["allowed_values"], ["01001", "01002"])
        outbound = provider.build_outbound_url(
            survey,
            SimpleNamespace(rid="Rid1234567", provider_profile_uid="ABCD-EFGH-IJKL-MNOP", prescreener_uid=None),
            {},
        )
        self.assertIn("umid=ABCD-EFGH-IJKL-MNOP", outbound)
        self.assertIn("uid=Rid1234567", outbound)

    @patch.dict("os.environ", {"ACUITY_TEST_SUPPLIER": "supplier", "ACUITY_TEST_TOKEN": "secret"})
    def test_acuity_decodes_range_and_option_qualifications(self):
        integration = self.integration(
            "acuity",
            "https://api.acuitykp.online",
            {"supplier_id": "ACUITY_TEST_SUPPLIER", "token": "ACUITY_TEST_TOKEN"},
        )
        survey = Survey.objects.create(
            client=integration.client,
            integration=integration,
            source_key="21264",
            source_id=21264,
            company_name=integration.client.name,
            status=Survey.Status.LIVE,
            country="United States",
            language="English",
            sample_size=100,
            raw_data={
                "country": "United States",
                "language": "English",
                "quota": 100,
                "live_quota": 10,
                "qualifications": [
                    {"question_id": 2, "control_type": 6, "range": [{"range": "39-55"}]},
                    {"question_id": 1, "control_type": 1, "option": [{"option": 2}]},
                ],
            },
            entry_link="https://respond.example/start?uid=[identifier]",
        )
        provider = AcuityAnalyticsProvider(integration)
        catalog = {
            "1": {"id": 1, "question": "What is your gender?", "control_type": 1, "mapped_option": {"1-1": "Male", "1-2": "Female"}},
            "2": {"id": 2, "question": "What is your age?", "control_type": 6, "mapped_option": {}},
        }
        with patch.object(provider, "_question_catalog", return_value=catalog):
            provider.refresh_details(survey)

        prepared = _prescreener_questions(survey)
        age = next(row for row in prepared if row["is_age_question"])
        gender = next(row for row in prepared if row["model"].key == "GENDER")
        self.assertEqual(age["age_ranges"], [(39, 55)])
        self.assertEqual(gender["options"], [{"value": "2", "label": "Female", "selected": False}])
        self.assertEqual(survey.quotas.get().remaining, 90)
        self.assertEqual(
            provider.build_outbound_url(survey, SimpleNamespace(rid="Rid1234567"), {}),
            "https://respond.example/start?uid=Rid1234567",
        )
