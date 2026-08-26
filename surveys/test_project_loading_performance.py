from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from surveys.models import Survey, SurveyAttempt, SurveyQuota, TargetingQuestion


class ProjectLoadingPerformanceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="project-performance-owner",
            password="test-password",
        )
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.survey = Survey.objects.create(
            source_id=810001,
            buyer_id="PERF-BUYER-PRIMARY",
            name="Performance survey",
            company_name="Performance client",
            country_code="US",
            country="United States",
            sample_size=10,
        )

    def test_projects_html_does_not_embed_the_complete_buyer_inventory(self):
        Survey.objects.bulk_create([
            Survey(
                local_id=f"91000000{index:06d}",
                source_id=820000 + index,
                source_key=str(820000 + index),
                buyer_id=f"BUYER-PAYLOAD-{index:04d}",
                company_name="Performance client",
                country_code="US",
                country="United States",
            )
            for index in range(300)
        ])
        self.client.force_login(self.user)

        response = self.client.get(reverse("projects"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("survey-buyer-options"))
        self.assertContains(response, "Open or search to load buyer IDs")
        self.assertNotContains(response, "BUYER-PAYLOAD-0299")
        self.assertLess(len(response.content), 150_000)

    def test_buyer_options_are_bounded_searchable_and_scoped_to_client_filter(self):
        Survey.objects.create(
            source_id=810002,
            buyer_id="PERF-BUYER-SECONDARY",
            company_name="Other client",
        )

        response = self.api.get(
            reverse("survey-buyer-options"),
            {"search": "primary", "company": "Performance client"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"], [{
            "value": "PERF-BUYER-PRIMARY",
            "client_value": "Performance client",
        }])
        self.assertFalse(response.data["has_more"])

    def test_default_list_skips_wide_detail_prefetch_and_correlated_annotations(self):
        SurveyQuota.objects.create(
            survey=self.survey,
            source_key="performance-quota",
        )
        TargetingQuestion.objects.create(
            survey=self.survey,
            question_id=810001,
            key="PERFORMANCE",
        )
        SurveyAttempt.objects.create(
            rid="Perf000001",
            survey=self.survey,
            platform_user=self.user,
            user_id=str(self.user.pk),
            status=SurveyAttempt.Status.COMPLETED,
        )

        with (
            patch("surveys.views.annotate_survey_pricing_for_user") as pricing,
            CaptureQueriesContext(connection) as captured,
        ):
            response = self.api.get(reverse("survey-list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"][0]["completes"], 1)
        pricing.assert_not_called()
        sql = [query["sql"].lower() for query in captured.captured_queries]
        self.assertFalse(any("surveys_surveyquota" in query for query in sql))
        self.assertFalse(any("surveys_targetingquestion" in query for query in sql))
        self.assertFalse(any(
            "left outer join" in query and "vendors_client" in query
            for query in sql
            if "surveys_survey" in query
        ))
        self.assertEqual(
            sum("surveys_surveyattempt" in query for query in sql),
            1,
        )
