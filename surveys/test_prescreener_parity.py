from datetime import date
from unittest.mock import patch

from django.test import RequestFactory, TestCase

from vendors.models import Client, ClientIntegration

from .age_rules import normalize_age_range
from .models import Survey, SurveyQuota, TargetingQuestion
from .providers import ProviderError
from .views import (
    SurveyViewSet,
    _collect_prescreener_answers,
    _prescreener_questions,
)


class PrescreenerParityTests(TestCase):
    def integration(self, provider_code):
        client = Client.objects.create(
            code=f"parity-{provider_code}",
            name=f"Parity {provider_code}",
            provider_code=provider_code,
        )
        return ClientIntegration.objects.create(
            client=client,
            name=f"Parity {provider_code}",
            provider_code=provider_code,
            base_url=f"https://{provider_code}.example.test",
        )

    def test_cint_without_qualifications_collects_platform_only_age_and_gender(self):
        integration = self.integration("cint")
        survey = Survey.objects.create(
            client=integration.client,
            integration=integration,
            source_key="cint-no-qualifications",
            name="Cint no qualifications",
        )

        prepared = _prescreener_questions(survey)

        self.assertEqual(len(prepared), 2)
        self.assertEqual(
            (prepared[0]["model"].key, prepared[0]["min_value"], prepared[0]["max_value"]),
            ("AGE", 13, 99),
        )
        self.assertEqual(
            [option["value"] for option in prepared[1]["options"]],
            ["male", "female"],
        )

        request = RequestFactory().post("/survey/prescreener/", {
            prepared[0]["field_name"]: "66",
            prepared[1]["field_name"]: "female",
        })
        answers, errors = _collect_prescreener_answers(request, survey)

        self.assertEqual(errors, [])
        self.assertEqual(set(answers), {"platform_profile_age", "platform_profile_gender"})
        self.assertEqual(answers["platform_profile_age"]["values"], ["66"])
        self.assertEqual(answers["platform_profile_age"]["upstream_values"], [])
        self.assertEqual(answers["platform_profile_gender"]["upstream_values"], [])
        self.assertTrue(answers["platform_profile_age"]["platform_only"])

    def test_rfg_mandatory_profile_rows_consolidate_targeting_aliases(self):
        integration = self.integration("rfg")
        survey = Survey.objects.create(
            client=integration.client,
            integration=integration,
            source_key="rfg-aliases",
            name="RFG aliases",
        )
        birthday = TargetingQuestion.objects.create(
            survey=survey,
            question_id=-1,
            key="RFG_BIRTHDAY",
            text="What is your date of birth?",
            question_type="date",
            category="Required profile",
            raw_data={"targeting_age_ranges": [{"min": 18, "max": 99}]},
        )
        gender = TargetingQuestion.objects.create(
            survey=survey,
            question_id=-2,
            key="RFG_GENDER",
            text="What is your gender?",
            question_type="single",
            category="Required profile",
            options=[
                {"OptionId": "M", "OptionText": "Male"},
                {"OptionId": "F", "OptionText": "Female"},
            ],
            raw_data={"targeting_choices": ["M", "F"]},
        )
        postal = TargetingQuestion.objects.create(
            survey=survey,
            question_id=-3,
            key="RFG_POSTAL_CODE",
            text="What is your postal code?",
            question_type="text",
            category="Required profile",
        )
        age_alias = TargetingQuestion.objects.create(
            survey=survey,
            question_id=101,
            key="AGE_TARGET",
            text="What is your age?",
            question_type="numeric",
            raw_data={"targeting_age_ranges": [{"min": 25, "max": 64}]},
        )
        gender_alias = TargetingQuestion.objects.create(
            survey=survey,
            question_id=102,
            key="SEX_TARGET",
            text="Select your gender",
            question_type="single",
            options=[
                {"OptionId": "1", "OptionText": "Male"},
                {"OptionId": "2", "OptionText": "Female"},
            ],
            raw_data={"targeting_choices": ["2"]},
        )
        postal_alias = TargetingQuestion.objects.create(
            survey=survey,
            question_id=103,
            key="ZIP_TARGET",
            text="ZIP code",
            question_type="text",
            raw_data={"targeting_choices": ["10001", "10002"]},
        )

        prepared = _prescreener_questions(survey)
        self.assertEqual([row["model"].pk for row in prepared], [postal.pk, gender.pk, birthday.pk])
        by_key = {row["model"].key: row for row in prepared}
        self.assertEqual(
            [option["value"] for option in by_key["RFG_GENDER"]["options"]],
            ["F"],
        )
        self.assertIn(age_alias, by_key["RFG_BIRTHDAY"]["aliases"])
        self.assertIn(gender_alias, by_key["RFG_GENDER"]["aliases"])
        self.assertIn(postal_alias, by_key["RFG_POSTAL_CODE"]["aliases"])
        self.assertIn((25, 64), by_key["RFG_BIRTHDAY"]["age_ranges"])

        today = date.today()
        birth_date = date(today.year - 30, today.month, min(today.day, 28))
        request = RequestFactory().post("/survey/prescreener/", {
            by_key["RFG_BIRTHDAY"]["field_name"]: birth_date.strftime("%d-%m-%Y"),
            by_key["RFG_GENDER"]["field_name"]: "F",
            by_key["RFG_POSTAL_CODE"]["field_name"]: "10001",
        })
        answers, errors = _collect_prescreener_answers(request, survey)
        self.assertEqual(errors, [])
        self.assertEqual(answers[str(gender_alias.pk)]["upstream_values"], ["2"])
        self.assertEqual(answers[str(postal_alias.pk)]["upstream_values"], ["10001"])

    def test_generic_targeting_choices_filter_and_validate_age_and_options(self):
        integration = self.integration("biobrain")
        survey = Survey.objects.create(
            client=integration.client,
            integration=integration,
            source_key="generic-targeting-choices",
            name="Generic targeting choices",
        )
        age = TargetingQuestion.objects.create(
            survey=survey,
            question_id=201,
            key="AGE",
            text="What is your age?",
            question_type="Numeric Open Ended",
            options=[
                {"OptionId": "young", "OptionText": "18-24"},
                {"OptionId": "adult", "OptionText": "25-29"},
            ],
            raw_data={"targeting_choices": ["adult"]},
        )
        region = TargetingQuestion.objects.create(
            survey=survey,
            question_id=202,
            key="REGION",
            text="Where do you live?",
            question_type="Single Punch",
            options=[
                {"OptionId": "north", "OptionText": "North"},
                {"OptionId": "south", "OptionText": "South"},
            ],
            raw_data={"targeting_choices": ["north"]},
        )

        prepared = {row["model"].key: row for row in _prescreener_questions(survey)}
        self.assertEqual(prepared["AGE"]["age_ranges"], [(25, 29)])
        self.assertEqual(
            [option["value"] for option in prepared["REGION"]["options"]],
            ["north"],
        )

        rejected = RequestFactory().post("/survey/prescreener/", {
            f"question_{age.pk}": "20",
            f"question_{region.pk}": "south",
        })
        answers, errors = _collect_prescreener_answers(rejected, survey)
        self.assertNotIn(str(age.pk), answers)
        self.assertNotIn(str(region.pk), answers)
        self.assertEqual(len(errors), 2)

        accepted = RequestFactory().post("/survey/prescreener/", {
            f"question_{age.pk}": "27",
            f"question_{region.pk}": "north",
        })
        answers, errors = _collect_prescreener_answers(accepted, survey)
        self.assertEqual(errors, [])
        self.assertEqual(answers[str(age.pk)]["upstream_values"], ["27"])
        self.assertEqual(answers[str(region.pk)]["upstream_values"], ["north"])

    def test_age_ranges_merge_and_unknown_restrictions_do_not_become_1_to_99(self):
        integration = self.integration("custom")
        survey = Survey.objects.create(
            client=integration.client,
            integration=integration,
            source_key="merged-age-ranges",
            name="Merged age ranges",
        )
        merged = TargetingQuestion.objects.create(
            survey=survey,
            question_id=301,
            key="AGE",
            text="What is your age?",
            question_type="Numeric Open Ended",
            raw_data={
                "targeting_age_ranges": [
                    {"start": 21, "end": 29},
                    {"from": 30, "to": 45},
                    {"description": "Respondents aged 46 to 64 years"},
                    {"start": 60, "end": 70},
                ]
            },
        )
        unknown = TargetingQuestion.objects.create(
            survey=survey,
            question_id=302,
            key="AGE_UNKNOWN",
            text="What is your age?",
            question_type="Numeric Open Ended",
            raw_data={"targeting_age_ranges": [{"from": "unknown", "to": "bad"}]},
        )

        prepared = {row["model"].pk: row for row in _prescreener_questions(survey)}
        self.assertEqual(prepared[merged.pk]["age_ranges"], [(21, 70)])
        self.assertEqual(prepared[merged.pk]["targeting_note"], "Qualifying age: 21–70")
        self.assertEqual(prepared[unknown.pk]["age_ranges"], [])
        self.assertTrue(prepared[unknown.pk]["age_constraints_present"])

        request = RequestFactory().post("/survey/prescreener/", {
            f"question_{merged.pk}": "40",
            f"question_{unknown.pk}": "40",
        })
        answers, errors = _collect_prescreener_answers(request, survey)
        self.assertIn(str(merged.pk), answers)
        self.assertNotIn(str(unknown.pk), answers)
        self.assertEqual(len(errors), 1)


class AgeRuleLabelParityTests(TestCase):
    def test_open_ended_label_variants_normalize_to_99(self):
        for value in (
            "65+",
            "65 plus",
            "65 years and older",
            "65 years or older",
            "65 and above",
            "65 & over",
            "over 65",
            "above 65",
            "older than 65",
            "Age 65+",
            "65 and older.",
            "Respondents over 65 years",
        ):
            with self.subTest(value=value):
                self.assertEqual(normalize_age_range(value), (65, 99))
        self.assertEqual(
            normalize_age_range({"OptionText": "65 years and older", "ageStart": 65, "ageEnd": 65}),
            (65, 99),
        )

    def test_explicit_closed_range_is_not_widened(self):
        self.assertEqual(normalize_age_range("25-29"), (25, 29))
        self.assertEqual(
            normalize_age_range({"OptionText": "25-29", "ageStart": 25, "ageEnd": None}),
            (25, 29),
        )

    def test_alternate_payload_keys_and_descriptive_shapes_are_supported(self):
        self.assertEqual(normalize_age_range({"start": 21, "end": 29}), (21, 29))
        self.assertEqual(normalize_age_range({"from": 30, "to": 45}), (30, 45))
        self.assertEqual(
            normalize_age_range({"description": "Respondents aged 46 to 64 years"}),
            (46, 64),
        )
        self.assertEqual(
            normalize_age_range({"range": {"from": 65, "to": None}}),
            (65, 99),
        )


class CachedDetailParityTests(TestCase):
    def setUp(self):
        client = Client.objects.create(
            code="cached-detail-client",
            name="Cached detail client",
            provider_code="innovatemr",
        )
        self.integration = ClientIntegration.objects.create(
            client=client,
            name="Cached detail integration",
            provider_code="innovatemr",
            base_url="https://innovatemr.example.test",
        )
        self.survey = Survey.objects.create(
            client=client,
            integration=self.integration,
            source_key="cached-details",
            name="Cached details",
            quota_synced_at=None,
            targeting_synced_at=None,
        )
        SurveyQuota.objects.create(
            survey=self.survey,
            source_key="quota-1",
            quota_id=1,
            remaining=7,
        )
        TargetingQuestion.objects.create(
            survey=self.survey,
            question_id=1,
            key="GENDER",
            text="What is your gender?",
            question_type="Single Punch",
            options=[{"OptionId": "1", "OptionText": "Male"}],
        )

    def test_null_sync_timestamps_do_not_hide_cached_detail_rows(self):
        view = SurveyViewSet()
        view.get_object = lambda: self.survey
        request = RequestFactory().get("/api/surveys/cached/details/")

        with patch.object(
            SurveyViewSet,
            "_refresh_if_stale",
            side_effect=ProviderError("Provider temporarily unavailable"),
        ):
            quotas = view.quotas(request, local_id=self.survey.local_id)
            targeting = view.targeting(request, local_id=self.survey.local_id)

        self.assertEqual(quotas.status_code, 200)
        self.assertEqual(quotas.data[0]["quota_id"], 1)
        self.assertEqual(targeting.status_code, 200)
        self.assertEqual(targeting.data[0]["key"], "GENDER")
