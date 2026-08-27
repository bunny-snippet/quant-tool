from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from accounts.models import AccessFunction, UserFunctionOverride
from vendors.models import Client, ClientIntegration

from .models import Survey, SurveyAttempt


class TolunaIdentifierSearchTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="toluna-search-admin",
            email="toluna-search@example.test",
            password="test-password",
        )
        client = Client.objects.create(
            code="toluna-search",
            name="Toluna",
            provider_code="toluna",
        )
        integration = ClientIntegration.objects.create(
            client=client,
            name="Toluna search",
            provider_code="toluna",
            base_url="https://tws.toluna.com",
        )
        self.survey = Survey.objects.create(
            client=client,
            integration=integration,
            source_id=5919062,
            source_key="5919062:4479439",
            buyer_id="4479439",
            company_name="Toluna",
            name="Toluna identifier search campaign 76001234",
            status=Survey.Status.LIVE,
            raw_data={"SurveyID": 5919062, "WaveID": 4479439},
        )
        self.rfg_survey = Survey.objects.create(
            client=client,
            integration=integration,
            source_key="RFG2295538382-001",
            buyer_id="rfg-buyer-identifier",
            company_name="RFG",
            name="RFG identifier search",
            status=Survey.Status.LIVE,
        )
        self.attempt = SurveyAttempt.objects.create(
            rid="TolSrch001",
            survey=self.survey,
            platform_user=self.user,
            client=client,
            user_id=str(self.user.pk),
        )
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def test_project_search_accepts_displayed_toluna_survey_id(self):
        response = self.api.get("/api/v1/surveys/", {"search": "5919062"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["local_id"], self.survey.local_id)
        self.assertEqual(response.data["results"][0]["display_source_id"], "5919062")

    def test_project_identifier_filter_accepts_indexed_id_forms(self):
        for identifier in (
            self.survey.local_id,
            "5919062:4479439",
            "5919062",
            "4479439",
        ):
            with self.subTest(identifier=identifier):
                response = self.api.get(
                    "/api/v1/surveys/", {"identifier": identifier}
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["count"], 1)
                self.assertEqual(
                    response.data["results"][0]["local_id"], self.survey.local_id
                )

        lower_case_rfg = self.api.get(
            "/api/v1/surveys/", {"identifier": "rfg2295538382-001"}
        )
        self.assertEqual(lower_case_rfg.status_code, 200)
        self.assertEqual(lower_case_rfg.data["count"], 1)
        self.assertEqual(
            lower_case_rfg.data["results"][0]["local_id"], self.rfg_survey.local_id
        )

    def test_unmatched_numeric_identifier_keeps_generic_search_available(self):
        identifier_response = self.api.get(
            "/api/v1/surveys/", {"identifier": "76001234"}
        )
        generic_response = self.api.get(
            "/api/v1/surveys/", {"search": "76001234"}
        )

        self.assertEqual(identifier_response.status_code, 200)
        self.assertEqual(identifier_response.data["count"], 0)
        self.assertEqual(generic_response.status_code, 200)
        self.assertEqual(generic_response.data["count"], 1)
        self.assertEqual(
            generic_response.data["results"][0]["local_id"], self.survey.local_id
        )

    def test_oversized_numeric_identifier_is_a_safe_no_match(self):
        response = self.api.get(
            "/api/v1/surveys/", {"identifier": "9" * 5000}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

    def test_identifier_count_uses_materialized_index_candidates(self):
        with CaptureQueriesContext(connection) as captured:
            response = self.api.get(
                "/api/v1/surveys/", {"identifier": "5919062"}
            )

        self.assertEqual(response.status_code, 200)
        count_sql = next(
            query["sql"]
            for query in captured.captured_queries
            if "COUNT(" in query["sql"].upper()
            and "identifier_candidates" in query["sql"]
        )
        normalized_sql = " ".join(count_sql.upper().split())
        self.assertIn("UNION ALL", normalized_sql)
        self.assertIn("AS IDENTIFIER_CANDIDATES", normalized_sql)
        for descriptive_column in (
            '"NAME"',
            '"COMPANY_NAME"',
            '"COUNTRY"',
            '"JOB_CATEGORY"',
        ):
            self.assertNotIn(descriptive_column, normalized_sql)

    def test_identifier_filter_uses_the_existing_project_search_permission(self):
        denied_user = get_user_model().objects.create_user(
            username="toluna-identifier-denied"
        )
        UserFunctionOverride.objects.create(
            user=denied_user,
            function=AccessFunction.objects.get(code="projects.filter.search"),
            effect=UserFunctionOverride.Effect.DENY,
        )
        denied_api = APIClient()
        denied_api.force_authenticate(denied_user)

        response = denied_api.get(
            "/api/v1/surveys/", {"identifier": self.survey.local_id}
        )

        self.assertEqual(response.status_code, 403)

    def test_traffic_search_accepts_toluna_survey_and_wave_ids(self):
        by_survey = self.api.get("/api/v1/survey-attempts/", {"search": "5919062"})
        by_wave = self.api.get("/api/v1/survey-attempts/", {"search": "4479439"})

        self.assertEqual(by_survey.status_code, 200)
        self.assertEqual(by_survey.data["count"], 1)
        self.assertEqual(by_survey.data["results"][0]["rid"], self.attempt.rid)
        self.assertEqual(by_survey.data["results"][0]["survey_source_id"], "5919062")
        self.assertEqual(by_wave.status_code, 200)
        self.assertEqual(by_wave.data["count"], 1)
        self.assertEqual(by_wave.data["results"][0]["rid"], self.attempt.rid)

    def test_exact_traffic_survey_filter_accepts_all_toluna_identifiers(self):
        for identifier in ("5919062:4479439", "5919062", "4479439"):
            with self.subTest(identifier=identifier):
                response = self.api.get(
                    "/api/v1/survey-attempts/", {"survey_id": identifier}
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["count"], 1)
                self.assertEqual(response.data["results"][0]["rid"], self.attempt.rid)
