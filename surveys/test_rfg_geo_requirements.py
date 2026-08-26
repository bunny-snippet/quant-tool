from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.core.cache import cache
from django.test import TestCase

from vendors.models import Client, ClientIntegration

from .models import Survey, SurveyAttempt
from .providers.rfg import ResearchForGoodProvider
from .serializers import TargetingQuestionSerializer
from .views import _prescreener_questions


class RFGGeoRequirementDisplayTests(TestCase):
    def setUp(self):
        cache.clear()
        client = Client.objects.create(
            code="rfg-geo-display",
            name="RFG Geo Display",
            provider_code="rfg",
        )
        self.integration = ClientIntegration.objects.create(
            client=client,
            name="RFG Geo Display",
            provider_code="rfg",
            base_url="https://api.researchforgood.com/API/",
            credential_env_keys={"apid": "RFG_APID", "secret": "RFG_SECRET"},
            sync_interval_seconds=60,
        )
        self.survey = Survey.objects.create(
            client=client,
            integration=self.integration,
            source_key="RFG605150-geo-display",
            country_code="US",
            status=Survey.Status.LIVE,
        )

    def _answers(self, **overrides):
        values = {
            "RFG_BIRTHDAY": "35",
            "RFG_GENDER": "M",
            "RFG_POSTAL_CODE": "10001",
        }
        values.update(overrides)
        return {
            str(question.pk): {
                "question_key": question.key,
                "values": [str(values[question.key])],
                "upstream_values": [str(values[question.key])],
            }
            for question in self.survey.targeting_questions.all()
            if question.key in values
        }

    @patch.dict(
        "os.environ",
        {
            "RFG_APID": "publisher",
            "RFG_SECRET": "00112233445566778899aabbccddeeff",
        },
        clear=False,
    )
    def test_project_dma_and_zip_are_displayed_and_enforced(self):
        provider = ResearchForGoodProvider(self.integration)
        targeting = {
            "datapoints": [
                {"name": "DMA (US)", "values": [{"choice": 1}, {"choice": 2}]},
                {
                    "name": "List of Zips",
                    "values": [{"freelist": '"10001",90001,981*'}],
                    "usesWildcards": True,
                },
            ],
            "quotas": [],
        }
        metadata = {
            "DMA (US)": {
                "name": "DMA (US)",
                "type": 13,
                "answers": [None, {"en-US": "LOS ANGELES"}, {"en-US": "NEW YORK"}],
            },
            "List of Zips": {
                "name": "List of Zips",
                "type": 16,
                "answers": [],
            },
        }
        with patch.object(provider, "targeting", return_value=targeting), patch.object(
            provider, "datapoint", side_effect=lambda name: metadata[name]
        ), patch.object(
            provider,
            "create_link",
            return_value="https://survey.saysoforgood.com/live/example",
        ):
            provider.refresh_details(self.survey)

        postal = self.survey.targeting_questions.get(key="RFG_POSTAL_CODE")
        expected_note = (
            "Required DMA: LOS ANGELES, NEW YORK · "
            "Required ZIP codes/patterns: 10001, 90001, 981*"
        )
        self.assertEqual(postal.raw_data["targeting_note"], expected_note)
        self.assertEqual(
            TargetingQuestionSerializer(postal).data["targeting_note"],
            expected_note,
        )
        prepared = next(
            item for item in _prescreener_questions(self.survey)
            if item["model"].key == "RFG_POSTAL_CODE"
        )
        self.assertEqual(prepared["targeting_note"], expected_note)

        with patch.object(provider, "zip_to_geo", return_value={"DMA (US)": 1}):
            self.assertEqual(provider.validate_prescreener(self.survey, self._answers()), (True, ""))
        with patch.object(provider, "zip_to_geo", return_value={"DMA (US)": 99}):
            eligible, reason = provider.validate_prescreener(
                self.survey, self._answers(RFG_POSTAL_CODE="90001")
            )
        self.assertFalse(eligible)
        self.assertIn("required dma", reason.lower())
        with patch.object(provider, "zip_to_geo", return_value={"DMA (US)": 1}):
            eligible, reason = provider.validate_prescreener(
                self.survey, self._answers(RFG_POSTAL_CODE="30301")
            )
        self.assertFalse(eligible)
        self.assertIn("zip codes", reason.lower())

    @patch.dict(
        "os.environ",
        {
            "RFG_APID": "publisher",
            "RFG_SECRET": "00112233445566778899aabbccddeeff",
        },
        clear=False,
    )
    def test_only_open_quota_region_is_hinted_and_quota_state_is_enforced(self):
        provider = ResearchForGoodProvider(self.integration)
        targeting = {
            "datapoints": [{"name": "Age", "values": [{"min": 18, "max": 99}]}],
            "excludeNonMatching": True,
            "quotas": [
                {
                    "completesLeft": 27,
                    "datapoints": [{"name": "Region1GB", "values": [{"choice": 1}]}],
                },
                {
                    "completesLeft": 0,
                    "datapoints": [{"name": "Region1GB", "values": [{"choice": 2}]}],
                },
            ],
        }
        metadata = {
            "Region1GB": {
                "name": "Region1GB",
                "type": 13,
                "question": {"en-US": "In which Region do you live?"},
                "answers": [
                    None,
                    {"en-US": "Derry and Strabane"},
                    {"en-US": "Belfast"},
                ],
            },
        }
        with patch.object(provider, "targeting", return_value=targeting), patch.object(
            provider, "datapoint", side_effect=lambda name: metadata[name]
        ), patch.object(
            provider,
            "create_link",
            return_value="https://survey.saysoforgood.com/live/example",
        ):
            provider.refresh_details(self.survey)

        postal = self.survey.targeting_questions.get(key="RFG_POSTAL_CODE")
        self.assertEqual(
            postal.raw_data["targeting_note"],
            "Open quota region: Derry and Strabane",
        )
        self.assertNotIn("Belfast", postal.raw_data["targeting_note"])
        quota_details = list(self.survey.quotas.order_by("remaining").values_list("raw_data", flat=True))
        self.assertEqual(quota_details[1]["targeting_details"][0]["values"], ["Derry and Strabane"])

        with patch.object(provider, "zip_to_geo", return_value={"Region1GB": 1}):
            self.assertEqual(provider.validate_prescreener(self.survey, self._answers()), (True, ""))
        with patch.object(provider, "zip_to_geo", return_value={"Region1GB": 2}):
            eligible, reason = provider.validate_prescreener(
                self.survey, self._answers(RFG_POSTAL_CODE="90001")
            )
        self.assertFalse(eligible)
        self.assertIn("full or throttled", reason.lower())
        with patch.object(provider, "zip_to_geo", return_value={"Region1GB": 3}):
            eligible, reason = provider.validate_prescreener(
                self.survey, self._answers(RFG_POSTAL_CODE="30301")
            )
        self.assertFalse(eligible)
        self.assertIn("open rfg quota", reason.lower())

    @patch.dict(
        "os.environ",
        {
            "RFG_APID": "publisher",
            "RFG_SECRET": "00112233445566778899aabbccddeeff",
        },
        clear=False,
    )
    def test_quota_combinations_and_platform_only_children_are_enforced(self):
        provider = ResearchForGoodProvider(self.integration)
        targeting = {
            "datapoints": [],
            "excludeNonMatching": True,
            "quotas": [
                {
                    "completesLeft": 12,
                    "datapoints": [
                        {
                            "name": "Children",
                            "values": [{"gender": 1, "min": 5, "max": 10}],
                        },
                        {"name": "Income", "values": [{"choice": 1}]},
                    ],
                },
                {
                    "completesLeft": 0,
                    "datapoints": [
                        {
                            "name": "Children",
                            "values": [{"gender": 2, "min": 5, "max": 10}],
                        },
                        {"name": "Income", "values": [{"choice": 2}]},
                    ],
                },
            ],
        }
        metadata = {
            "Children": {
                "name": "Children",
                "property": "children",
                "type": 17,
                "question": {"en-US": "Children"},
                "answers": [],
            },
            "Income": {
                "name": "Income",
                "property": "income",
                "type": 0,
                "question": {"en-US": "What is your household income?"},
                "answers": [
                    None,
                    {"en-US": "Band one"},
                    {"en-US": "Band two"},
                    {"en-US": "Outside quota"},
                ],
            },
        }
        with patch.object(provider, "targeting", return_value=targeting), patch.object(
            provider, "datapoint", side_effect=lambda name: metadata[name]
        ), patch.object(
            provider,
            "create_link",
            return_value="https://survey.saysoforgood.com/live/example",
        ):
            provider.refresh_details(self.survey)

        income = self.survey.targeting_questions.get(key="income")
        children = list(
            self.survey.targeting_questions.filter(key__startswith="RFG_CHILDREN_MATCH_")
        )
        self.assertEqual(len(children), 2)
        self.assertTrue(
            all((question.raw_data or {}).get("platform_only") for question in children)
        )
        self.assertEqual(income.raw_data["targeting_choices"], [1, 2])
        boy = next(question for question in children if "boy" in question.text)
        girl = next(question for question in children if "girl" in question.text)

        open_answers = self._answers(**{
            "income": "1",
            boy.key: "1",
            girl.key: "0",
        })
        self.assertEqual(provider.validate_prescreener(self.survey, open_answers), (True, ""))
        attempt = SurveyAttempt.objects.create(
            survey=self.survey,
            rid="RfgGeo1234",
            prescreener_uid="RFG-Geo-Uid-0001",
            user_id="1",
        )
        outbound = parse_qs(
            urlsplit(provider.build_outbound_url(self.survey, attempt, open_answers)).query
        )
        self.assertEqual(outbound["income"], ["1"])
        self.assertFalse(any(key.startswith("RFG_CHILDREN_MATCH_") for key in outbound))

        cross_answers = self._answers(**{
            "income": "2",
            boy.key: "1",
            girl.key: "0",
        })
        eligible, reason = provider.validate_prescreener(self.survey, cross_answers)
        self.assertFalse(eligible)
        self.assertIn("open rfg quota", reason.lower())

        closed_answers = self._answers(**{
            "income": "2",
            boy.key: "0",
            girl.key: "1",
        })
        eligible, reason = provider.validate_prescreener(self.survey, closed_answers)
        self.assertFalse(eligible)
        self.assertIn("full or throttled", reason.lower())
