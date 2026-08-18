from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from surveys.models import SyncRun
from vendors.models import Client, ClientIntegration


TOLUNA_ENV = {
    "TOLUNA_API_AUTH_KEY": "api-secret",
    "TOLUNA_PARTNER_AUTH_KEY": "reference-secret",
    "TOLUNA_HMAC_KEY": "hmac-secret",
    "TOLUNA_PANEL_EN_IN": "panel-in",
    "TOLUNA_PANEL_EN_US": "panel-us",
}


class SetupTolunaCommandTests(TestCase):
    @patch.dict("os.environ", TOLUNA_ENV, clear=False)
    def test_dry_run_is_non_mutating(self):
        call_command("setup_toluna", stdout=StringIO())
        self.assertFalse(Client.objects.filter(code="toluna").exists())
        self.assertFalse(ClientIntegration.objects.filter(provider_code="toluna").exists())

    @patch.dict("os.environ", TOLUNA_ENV, clear=False)
    def test_apply_is_idempotent_and_partner_guid_is_optional_for_inventory(self):
        call_command("setup_toluna", apply=True, stdout=StringIO())
        call_command("setup_toluna", apply=True, stdout=StringIO())

        client = Client.objects.get(code="toluna")
        integration = ClientIntegration.objects.get(client=client, provider_code="toluna")
        self.assertEqual(ClientIntegration.objects.filter(provider_code="toluna").count(), 1)
        self.assertEqual(integration.credential_env_keys["api_auth_key"], "TOLUNA_API_AUTH_KEY")
        self.assertEqual(integration.credential_env_keys["panel_en_us"], "TOLUNA_PANEL_EN_US")
        self.assertNotIn("partner_guid", integration.credential_env_keys)
        self.assertTrue(integration.config["callback_hash_required"])
        self.assertFalse(integration.scheduled_sync_enabled)

    @patch("surveys.management.commands.setup_toluna.sync_client_integration")
    @patch("surveys.management.commands.setup_toluna.test_provider_connection")
    @patch.dict("os.environ", TOLUNA_ENV, clear=False)
    def test_test_and_sync_run_after_configuration(self, test_connection, sync_integration):
        test_connection.return_value = {
            "configured_cultures": ["en-in", "en-us"],
            "reference_questions": 42,
        }
        sync_integration.return_value = SyncRun(
            status=SyncRun.Status.SUCCESS,
            created=4,
            updated=3,
            unchanged=2,
            closed=1,
            detail_failures=0,
        )

        call_command("setup_toluna", apply=True, test=True, sync=True, stdout=StringIO())

        integration = ClientIntegration.objects.get(provider_code="toluna")
        test_connection.assert_called_once_with(integration)
        sync_integration.assert_called_once_with(integration, refresh_details=True)
