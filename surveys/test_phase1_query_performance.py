from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import AccessFunction, EmployeeProfile, Role, RoleFunctionPermission
from surveys.models import Survey, SurveyAttempt
from vendors.models import Client, ClientIntegration


class PhaseOneQueryPerformanceTests(TestCase):
    """Keep first-page API query work independent of the number of rendered rows."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="phase-one-performance-admin",
            password="test-password",
        )
        admin_role = Role.objects.get(slug="admin")
        EmployeeProfile.objects.filter(user=cls.user).update(role=admin_role)
        for code in ("clients.view", "clients.integration.view"):
            RoleFunctionPermission.objects.update_or_create(
                role=admin_role,
                function=AccessFunction.objects.get(code=code),
                defaults={"allowed": True},
            )
        cls.client_record = Client.objects.create(
            code="phase-one-client",
            name="Phase One Client",
            provider_code="innovatemr",
            created_by=cls.user,
        )
        cls.integrations = [
            ClientIntegration.objects.create(
                client=cls.client_record,
                name=f"Phase One Integration {index:02d}",
                provider_code="innovatemr",
                base_url=f"https://provider-{index:02d}.example.test/api",
                created_by=cls.user,
            )
            for index in range(20)
        ]
        cls.single_client_record = Client.objects.create(
            code="phase-one-single-client",
            name="Single Integration Client",
            provider_code="innovatemr",
            created_by=cls.user,
        )
        cls.single_integration = ClientIntegration.objects.create(
            client=cls.single_client_record,
            name="Single Integration",
            provider_code="innovatemr",
            base_url="https://single-provider.example.test/api",
            created_by=cls.user,
        )
        cls.surveys = Survey.objects.bulk_create([
            Survey(
                local_id=f"20990101{index:06d}",
                client=cls.client_record,
                integration=cls.integrations[0],
                source_id=9_100_000 + index,
                source_key=str(9_100_000 + index),
                company_name=cls.client_record.name,
                name=f"Phase One Survey {index:02d}",
                status=Survey.Status.LIVE,
                sample_size=100,
                remaining=100,
                country_code="US",
                country="United States",
                entry_link="https://provider.example.test/live",
            )
            for index in range(20)
        ])
        SurveyAttempt.objects.bulk_create([
            SurveyAttempt(
                rid=f"Perf{index:06d}",
                survey=cls.surveys[0],
                platform_user=cls.user,
                user_id=str(cls.user.pk),
                status=SurveyAttempt.Status.COMPLETED,
                source_cpi_snapshot="1.00",
                cpi_currency_snapshot="USD",
            )
            for index in range(20)
        ])

    def _get_with_query_count(self, route_name, params=None, route_kwargs=None):
        # A fresh User instance prevents request-local relationship caches from
        # making the second measurement look artificially cheaper.
        api = APIClient()
        api.force_authenticate(get_user_model().objects.get(pk=self.user.pk))
        with CaptureQueriesContext(connection) as captured:
            response = api.get(reverse(route_name, kwargs=route_kwargs), params or {})
        self.assertEqual(response.status_code, 200)
        return response, len(captured)

    def test_project_list_query_baseline(self):
        one, one_count = self._get_with_query_count("survey-list", {"search": "9100000"})
        full, full_count = self._get_with_query_count("survey-list")
        self.assertEqual(len(one.data["results"]), 1)
        self.assertEqual(len(full.data["results"]), 20)
        matching = next(
            row for row in full.data["results"]
            if row["local_id"] == one.data["results"][0]["local_id"]
        )
        self.assertEqual(one.data["results"][0], matching)
        self.assertLessEqual(full_count, one_count + 1)
        self.assertLessEqual(full_count, 20)

    def test_traffic_list_query_baseline(self):
        one, one_count = self._get_with_query_count("survey-attempt-list", {"search": "Perf000000"})
        full, full_count = self._get_with_query_count("survey-attempt-list")
        self.assertEqual(len(one.data["results"]), 1)
        self.assertEqual(len(full.data["results"]), 20)
        matching = next(
            row for row in full.data["results"]
            if row["rid"] == one.data["results"][0]["rid"]
        )
        self.assertEqual(one.data["results"][0], matching)
        self.assertLessEqual(full_count, one_count + 1)
        self.assertLessEqual(full_count, 18)

    def test_integration_list_query_baseline(self):
        one, one_count = self._get_with_query_count(
            "client-integration-list", {"search": "Integration 00"}
        )
        full, full_count = self._get_with_query_count("client-integration-list")
        self.assertEqual(len(one.data["results"]), 1)
        self.assertEqual(len(full.data["results"]), 20)
        matching = next(
            row for row in full.data["results"]
            if row["id"] == one.data["results"][0]["id"]
        )
        self.assertEqual(one.data["results"][0], matching)
        detail, _detail_count = self._get_with_query_count(
            "client-integration-detail", route_kwargs={"pk": matching["id"]}
        )
        for field in (
            "survey_count",
            "profile_reuse_available_country_codes",
            "profile_reuse_status",
        ):
            self.assertEqual(detail.data[field], matching[field])
        self.assertLessEqual(full_count, one_count + 1)
        self.assertLessEqual(full_count, 10)

    def test_nested_client_integrations_are_query_flat(self):
        one, one_count = self._get_with_query_count(
            "vendor-client-list", {"search": "Single Integration Client"}
        )
        full, full_count = self._get_with_query_count("vendor-client-list")
        self.assertEqual(len(one.data["results"]), 1)
        matching = next(
            row for row in full.data["results"]
            if row["id"] == one.data["results"][0]["id"]
        )
        self.assertEqual(one.data["results"][0], matching)
        self.assertEqual(len(matching["integrations"]), 1)
        nested_ids = [
            integration["id"]
            for row in full.data["results"]
            for integration in row["integrations"]
        ]
        expected_ids = {
            *(integration.pk for integration in self.integrations),
            self.single_integration.pk,
        }
        self.assertTrue(expected_ids.issubset(set(nested_ids)))
        self.assertEqual(len(nested_ids), len(set(nested_ids)))
        self.assertLessEqual(full_count, one_count + 1)
        self.assertLessEqual(full_count, 14)
