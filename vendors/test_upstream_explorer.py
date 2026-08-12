from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import Role

from .models import Client, ClientIntegration
from .schema import filter_unconfigured_upstream_provider_endpoints, remove_unconfigured_upstream_provider_tags
from .upstream import INNOVATE_OPERATIONS, RFG_OPERATIONS, operation_response_description


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

    def test_every_documented_provider_operation_has_named_route_and_plain_language_contract(self):
        self.assertEqual(len(INNOVATE_OPERATIONS), 34)
        self.assertEqual(len(RFG_OPERATIONS), 12)
        for provider, operations in (
            ("innovatemr", INNOVATE_OPERATIONS),
            ("rfg", RFG_OPERATIONS),
        ):
            for code, spec in operations.items():
                route_name = f"upstream-explorer-{provider}-{code.replace('_', '-')}"
                route = reverse(route_name, args=["innovate" if provider == "innovatemr" else "rfg"])
                self.assertIn(f"/{provider}/{code.replace('_', '-')}/", route)
                self.assertTrue(spec.description)
                self.assertTrue(spec.documentation_url.startswith("http"))
                self.assertNotEqual(
                    operation_response_description(provider, spec),
                    "The provider's authenticated JSON response.",
                    f"{provider}.{code} needs a specific response explanation",
                )

    @patch("vendors.schema.configured_upstream_provider_keys", return_value={"innovatemr"})
    def test_swagger_hides_provider_sections_without_active_client(self, _configured):
        endpoints = [
            ("/api/v1/vendors/upstream-explorer/{client_code}/innovatemr/inventory/", "", "GET", object()),
            ("/api/v1/vendors/upstream-explorer/{client_code}/rfg/inventory/", "", "GET", object()),
            ("/survey/rfg/callback", "", "GET", object()),
            ("/api/v1/vendors/upstream-explorer/", "", "GET", object()),
        ]
        filtered = filter_unconfigured_upstream_provider_endpoints(endpoints)
        self.assertEqual([item[0] for item in filtered], [endpoints[0][0], endpoints[3][0]])
        schema = {"tags": [
            {"name": "Client API catalog"}, {"name": "InnovateMR APIs"},
            {"name": "RFG APIs"}, {"name": "RFG Callbacks"},
        ]}
        result = remove_unconfigured_upstream_provider_tags(schema, None, None, False)
        self.assertEqual(
            [item["name"] for item in result["tags"]],
            ["Client API catalog", "InnovateMR APIs"],
        )

    @patch.dict("os.environ", {"TEST_INNOVATE_TOKEN": "server-only-token"})
    def test_catalog_documents_urls_without_exposing_secret(self):
        response = self.client.get(reverse("upstream-explorer-list"))
        self.assertEqual(response.status_code, 200)
        payload = next(item for item in response.json() if item["client_code"] == "innovate")
        self.assertIn("innovate", payload["lookup_aliases"])
        self.assertEqual(payload["base_url"], "https://supplier.innovatemr.net/api/v2")
        self.assertTrue(payload["credential"]["configured"])
        self.assertEqual(payload["credential"]["environment_variables"], ["TEST_INNOVATE_TOKEN"])
        self.assertIn("quota", {item["code"] for item in payload["operations"]})
        self.assertNotIn("server-only-token", response.content.decode())

    def test_catalog_can_be_searched_and_client_name_alias_needs_no_database_id(self):
        searched = self.client.get(reverse("upstream-explorer-list"), {"search": "Innovate"})
        self.assertEqual(searched.status_code, 200)
        self.assertIn("innovate", [item["client_code"] for item in searched.json()])
        detail = self.client.get(reverse("upstream-explorer-detail", args=["innovate"]))
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["client_code"], "innovate")

    @patch.dict("os.environ", {"TEST_INNOVATE_TOKEN": "server-only-token"})
    @patch("surveys.integrations.requests.Session.get")
    def test_inventory_uses_server_credential_and_limits_swagger_response(self, mock_get):
        mock_get.return_value = self.response({
            "apiStatus": "success",
            "result": [{"surveyId": 1}, {"surveyId": 2}],
        })
        url = reverse("upstream-explorer-innovatemr-inventory", args=["innovate"])
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
        url = reverse("upstream-explorer-innovatemr-quota", args=["innovate"])
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
            "upstream-explorer-innovatemr-respondent-precheck",
            kwargs={"client_code": "innovate"},
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
        url = reverse("upstream-explorer-rfg-targeting", args=["rfg"])
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
            kwargs={"client_code": "innovate", "operation": "markets"},
        )
        response = self.client.get(execute_url, {"country": "US"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_get.call_args.args[0], "https://supplier.innovatemr.net/api/v2/v1/markets")
        rejected = self.client.get(
            reverse(
                "upstream-explorer-execute",
                kwargs={"client_code": "innovate", "operation": "arbitrary"},
            ),
            {"url": "https://attacker.example"},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertNotIn("attacker.example", mock_get.call_args.args[0])

    @patch.dict("os.environ", {"TEST_INNOVATE_TOKEN": "server-only-token"})
    @patch("surveys.integrations.requests.Session.request")
    def test_live_redirect_mutation_requires_confirmation_and_uses_documented_endpoint(self, mock_request):
        mock_request.return_value = self.response({"apiStatus": "success", "msg": "updated"})
        url = reverse(
            "upstream-explorer-innovatemr-set-survey-redirects",
            args=["innovate"],
        )
        rejected = self.client.post(url, {
            "survey_id": "16003381",
            "payload": {"sUrl": "https://example.com/complete"},
        }, content_type="application/json")
        self.assertEqual(rejected.status_code, 400)
        mock_request.assert_not_called()

        response = self.client.post(url, {
            "confirm_upstream_mutation": True,
            "survey_id": "16003381",
            "payload": {"sUrl": "https://example.com/complete"},
        }, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_request.call_args.args[:2], (
            "PUT",
            "https://supplier.innovatemr.net/api/v2/supply/setRedirectionForSurvey/16003381",
        ))
        self.assertEqual(mock_request.call_args.kwargs["json"], {
            "sUrl": "https://example.com/complete"
        })

    @patch.dict(
        "os.environ",
        {"TEST_RFG_APID": "apid-value", "TEST_RFG_SECRET": "0123456789abcdef0123456789abcdef"},
    )
    @patch("surveys.providers.rfg.ResearchForGoodProvider.explorer_read")
    def test_rfg_bulk_duplicate_log_and_zip_commands_are_individually_runnable(self, explorer_read):
        explorer_read.return_value = {"projects": []}
        rfg_client = Client.objects.create(code="rfg", name="Research For Good", provider_code="rfg")
        ClientIntegration.objects.create(
            client=rfg_client,
            name="RFG production",
            provider_code="rfg",
            base_url="https://api.researchforgood.com/API",
            credential_env_keys={"apid": "TEST_RFG_APID", "secret": "TEST_RFG_SECRET"},
        )

        response = self.client.get(
            reverse("upstream-explorer-rfg-duplicate-checks", args=["rfg"]),
            {
                "survey_ids": "RFG1,RFG2", "rid": "Ab12Cd34Ef",
                "ip": "203.0.113.2", "fingerprint": "0",
            },
        )
        self.assertEqual(response.status_code, 200)
        explorer_read.assert_called_with(
            "livealert/duplicateChecks/1",
            rfg_ids=["RFG1", "RFG2"], rid="Ab12Cd34Ef", ip="203.0.113.2", fingerprint="0",
        )

        self.client.get(
            reverse("upstream-explorer-rfg-log", args=["rfg"]),
            {"survey_id": "RFG1", "result": "1"},
        )
        explorer_read.assert_called_with("livealert/log/1", rfg_id="RFG1", result="1")

        self.client.get(
            reverse("upstream-explorer-rfg-zip-to-geo", args=["rfg"]),
            {"zip": "10001", "country_code": "us"},
        )
        explorer_read.assert_called_with("livealert/zipToGeo/1", zip="10001", countryCode="US")

    def test_rfg_callback_preview_is_safe_and_human_readable(self):
        rfg_client = Client.objects.create(code="rfg", name="Research For Good", provider_code="rfg")
        ClientIntegration.objects.create(
            client=rfg_client,
            name="RFG production",
            provider_code="rfg",
            base_url="https://api.researchforgood.com/API",
        )
        response = self.client.post(
            reverse("upstream-explorer-rfg-callback-preview", args=["rfg"]),
            {"result": "30", "liveS": "2"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["known_result_code"])
        self.assertEqual(response.json()["title"], "Fraud prevention")
        self.assertNotIn("rfg_callback", response.json())
