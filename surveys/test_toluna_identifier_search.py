from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

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
            source_key="5919062:4479439",
            buyer_id="4479439",
            company_name="Toluna",
            name="Toluna identifier search",
            status=Survey.Status.LIVE,
            raw_data={"SurveyID": 5919062, "WaveID": 4479439},
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
