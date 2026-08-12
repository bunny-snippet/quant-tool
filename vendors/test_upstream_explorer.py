from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import Role

from .models import Client, ClientIntegration


class UpstreamExplorerTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="upstream-admin", email="upstream@example.com", password="test-password"
        )
        self.client.force_login(self.admin)
        self.buyer = Client.objects.create(code="innovate", name="InnovateMR", provider_code="innovatemr")
        self.integration = ClientIntegration.objects.create(
            client=self.buyer,
            name="Innovate production",
            provider_code="innovatemr",
            base_url="https://supplier.innovatemr.net/api/v2",
            credential_env_key="TEST_INNOVATE_TOKEN",
            inventory_endpoint="/supply/getAllocatedSurveys",
            paged_inventory_endpoint="/supply/getAllocatedSurveysPaged",
            quota_endpoint_template="/supply/getQuotaForSurvey/{survey_id}",
            targeting_endpoint_template="/supply/getSurveyTargeting/{survey_id}",
            transaction_endpoint_template="/supply/getSurveyTransactionsByCond/{survey_id}/{pid}",
        )

    @staticmethod
    def response(payload):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        return response

    def test_employee_cannot_open_upstream_explorer(self):
        role = Role.objects.get(slug="employee")
        employee = get_user_model().objects.create_user("upstream-employee")
        employee.employee_profile.role = role
        employee.employee_profile.save(update_fields=["role", "updated_at"])
        self.client.force_login(employee)
        response = self.client.get(reverse("upstream-explorer-list"))
        self.assertEqual(response.status_code, 403)

    @patch.dict("os.environ", {"TEST_INNOVATE_TOKEN": "server-only-token"})
    def test_catalog_documents_urls_without_exposing_secret(self):
        response = self.client.get(reverse("upstream-explorer-list"))
        self.assertEqual(response.status_code, 200)
        payload = next(item for item in response.json() if item["id"] == self.integration.pk)
        self.assertEqual(payload["base_url"], "https://supplier.innovatemr.net/api/v2")
        self.assertTrue(payload["credential"]["configured"])
        self.assertEqual(payload["credential"]["environment_variables"], ["TEST_INNOVATE_TOKEN"])
        self.assertIn("quota", {item["code"] for item in payload["operations"]})
        self.assertNotIn("server-only-token", response.content.decode())

    @patch.dict("os.environ", {"TEST_INNOVATE_TOKEN": "server-only-token"})
    @patch("surveys.integrations.requests.Session.get")
    def test_inventory_uses_server_credential_and_limits_swagger_response(self, mock_get):
        mock_get.return_value = self.response({
            "apiStatus": "success",
            "result": [{"surveyId": 1}, {"surveyId": 2}],
        })
        url = reverse("upstream-explorer-inventory", args=[self.integration.pk])
        response = self.client.get(url, {"limit": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["result"], [{"surveyId": 1}])
        self.assertTrue(response.json()["response_truncated"])
        self.assertEqual(mock_get.call_args.kwargs["headers"]["x-access-token"], "server-only-token")
        self.assertNotIn("server-only-token", response.content.decode())

    @patch.dict("os.environ", {"TEST_INNOVATE_TOKEN": "server-only-token"})
    @patch("surveys.integrations.requests.Session.get")
    def test_quota_builds_documented_survey_endpoint(self, mock_get):
        mock_get.return_value = self.response({"apiStatus": "success", "result": []})
        url = reverse("upstream-explorer-quota", args=[self.integration.pk])
        response = self.client.get(url, {"survey_id": "15978952"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            mock_get.call_args.args[0],
            "https://supplier.innovatemr.net/api/v2/supply/getQuotaForSurvey/15978952",
        )

    @patch.dict("os.environ", {"TEST_INNOVATE_TOKEN": "server-only-token"})
    @patch("surveys.integrations.requests.Session.post")
    def test_respondent_precheck_posts_allow_listed_body_server_side(self, mock_post):
        mock_post.return_value = self.response({
            "apiStatus": "success", "result": {"status": "prequalified"}
        })
        url = reverse(
            "upstream-explorer-execute",
            kwargs={"pk": self.integration.pk, "operation": "respondent_precheck"},
        )
        response = self.client.get(url, {
            "survey_id": "16003381", "pid": "respondent-1", "ip": "203.0.113.20",
            "device_type": "desktop",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            mock_post.call_args.args[0],
            "https://supplier.innovatemr.net/api/v2/supply/respondentPreSurveyCheck",
        )
        self.assertEqual(mock_post.call_args.kwargs["json"], {
            "pid": "respondent-1", "ip": "203.0.113.20",
            "survNum": "16003381", "deviceType": "desktop",
        })
        self.assertEqual(mock_post.call_args.kwargs["headers"]["x-access-token"], "server-only-token")
        self.assertNotIn("server-only-token", response.content.decode())

    @patch.dict(
        "os.environ",
        {"TEST_RFG_APID": "apid-value", "TEST_RFG_SECRET": "0123456789abcdef0123456789abcdef"},
    )
    @patch("surveys.providers.rfg.ResearchForGoodProvider.explorer_read")
    def test_rfg_targeting_uses_signed_provider_adapter(self, explorer_read):
        explorer_read.return_value = {"datapoints": [], "quotas": []}
        rfg_client = Client.objects.create(code="rfg", name="RFG", provider_code="rfg")
        integration = ClientIntegration.objects.create(
            client=rfg_client,
            name="RFG production",
            provider_code="rfg",
            base_url="https://api.researchforgood.com/API",
            credential_env_keys={"apid": "TEST_RFG_APID", "secret": "TEST_RFG_SECRET"},
        )
        url = reverse("upstream-explorer-targeting", args=[integration.pk])
        response = self.client.get(url, {"survey_id": "RFG2300540746-001"})
        self.assertEqual(response.status_code, 200)
        explorer_read.assert_called_once_with(
            "livealert/targeting/1", rfg_id="RFG2300540746-001", zipsOnly=False
        )
        body = response.content.decode()
        self.assertNotIn("apid-value", body)
        self.assertNotIn("0123456789abcdef", body)

    @patch.dict("os.environ", {"TEST_INNOVATE_TOKEN": "server-only-token"})
    @patch("surveys.integrations.requests.Session.get")
    def test_configured_future_read_operation_runs_but_arbitrary_url_does_not(self, mock_get):
        mock_get.return_value = self.response({"items": [{"id": 1}]})
        self.integration.provider_code = "custom"
        self.integration.config = {
            "read_api_operations": [{
                "code": "markets",
                "label": "Markets",
                "endpoint": "/v1/markets",
                "documentation_url": "https://provider.example/docs/markets",
                "query_parameters": ["country"],
            }]
        }
        self.integration.save(update_fields=["provider_code", "config", "updated_at"])
        execute_url = reverse(
            "upstream-explorer-execute",
            kwargs={"pk": self.integration.pk, "operation": "markets"},
        )
        response = self.client.get(execute_url, {"country": "US"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_get.call_args.args[0], "https://supplier.innovatemr.net/api/v2/v1/markets")
        rejected = self.client.get(
            reverse(
                "upstream-explorer-execute",
                kwargs={"pk": self.integration.pk, "operation": "arbitrary"},
            ),
            {"url": "https://attacker.example"},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertNotIn("attacker.example", mock_get.call_args.args[0])
