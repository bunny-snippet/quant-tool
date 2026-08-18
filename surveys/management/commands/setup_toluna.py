import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from surveys.provider_services import sync_client_integration, test_provider_connection
from vendors.models import Client, ClientIntegration


TOLUNA_CREDENTIAL_ENV_NAMES = {
    "api_auth_key": "TOLUNA_API_AUTH_KEY",
    "partner_auth_key": "TOLUNA_PARTNER_AUTH_KEY",
    "hmac_key": "TOLUNA_HMAC_KEY",
    "partner_guid": "TOLUNA_PARTNER_GUID",
    "panel_en_ca": "TOLUNA_PANEL_EN_CA",
    "panel_en_gb": "TOLUNA_PANEL_EN_GB",
    "panel_en_in": "TOLUNA_PANEL_EN_IN",
    "panel_en_sg": "TOLUNA_PANEL_EN_SG",
    "panel_en_us": "TOLUNA_PANEL_EN_US",
}


class Command(BaseCommand):
    help = (
        "Create or update the production Toluna client integration from standard environment "
        "variable names, optionally verify it and run the first inventory sync."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Persist the configuration.")
        parser.add_argument("--test", action="store_true", help="Verify Toluna API and panel access after applying.")
        parser.add_argument("--sync", action="store_true", help="Run the first inventory sync after a successful test.")
        parser.add_argument("--owner-email", default="", help="Optional super-admin email recorded as creator.")
        parser.add_argument("--client-code", default="toluna")
        parser.add_argument("--client-name", default="Toluna")
        parser.add_argument("--integration-name", default="Toluna Production")

    @staticmethod
    def _configured_credentials():
        # Store only environment-variable names in the database. Secret values
        # remain exclusively in the process environment/.env file.
        return {
            key: env_name
            for key, env_name in TOLUNA_CREDENTIAL_ENV_NAMES.items()
            if os.getenv(env_name, "").strip()
        }

    @staticmethod
    def _validate_inventory_prerequisites(credentials):
        missing = [
            env_name
            for key, env_name in TOLUNA_CREDENTIAL_ENV_NAMES.items()
            if key in {"api_auth_key", "partner_auth_key"} and key not in credentials
        ]
        panels = [key for key in credentials if key.startswith("panel_")]
        if missing or not panels:
            parts = []
            if missing:
                parts.append(f"missing environment variables: {', '.join(missing)}")
            if not panels:
                parts.append(
                    "no culture PanelGUID is configured; set at least one TOLUNA_PANEL_EN_XX variable"
                )
            raise CommandError("Toluna inventory is not ready: " + "; ".join(parts))

    def _creator(self, owner_email):
        users = get_user_model().objects
        if owner_email:
            try:
                return users.get(email__iexact=owner_email)
            except get_user_model().DoesNotExist as exc:
                raise CommandError(f"No user exists with email {owner_email}.") from exc
        return users.filter(is_superuser=True).order_by("id").first()

    def handle(self, *args, **options):
        if options["sync"] and not options["test"]:
            raise CommandError("Use --sync together with --test so an unverified connection is never synced.")

        credentials = self._configured_credentials()
        self._validate_inventory_prerequisites(credentials)
        cultures = sorted(key.removeprefix("panel_").replace("_", "-") for key in credentials if key.startswith("panel_"))
        partner_ready = "partner_guid" in credentials
        callback_ready = "hmac_key" in credentials

        self.stdout.write(f"Toluna cultures: {', '.join(cultures)}")
        self.stdout.write(
            "Member/invite flow: " + ("ready" if partner_ready else "guarded (TOLUNA_PARTNER_GUID not configured)")
        )
        self.stdout.write(
            "Callback verification: " + ("enabled" if callback_ready else "guarded (TOLUNA_HMAC_KEY not configured)")
        )

        if not options["apply"]:
            self.stdout.write(self.style.WARNING("Dry run only; use --apply to persist this integration."))
            return

        creator = self._creator(options["owner_email"])
        config = {
            "environment": "production",
            "external_sample_base_url": "https://tws.toluna.com",
            "reference_base_url": "https://tws.toluna.com",
            "member_base_url": "https://ip.surveyrouter.com",
            "timeout_seconds": 30,
            "detail_refresh_batch": 10,
            "reference_refresh_hours": 24,
            "is_test_member": False,
            "callback_hash_required": callback_ready,
        }

        with transaction.atomic():
            client, client_created = Client.objects.update_or_create(
                code=options["client_code"],
                defaults={
                    "name": options["client_name"],
                    "provider_code": "toluna",
                    "is_active": True,
                    "created_by": creator,
                },
            )
            integration = ClientIntegration.objects.filter(
                client=client, provider_code="toluna"
            ).order_by("id").first()
            integration_created = integration is None
            if integration is None:
                integration = ClientIntegration(client=client, provider_code="toluna")
            integration.name = options["integration_name"]
            integration.base_url = "https://tws.toluna.com"
            integration.credential_env_key = ""
            integration.credential_env_keys = credentials
            integration.config = config
            integration.supplier_code = "1000"
            integration.inventory_endpoint = ""
            integration.auth_header_name = "API_AUTH_KEY"
            integration.inventory_result_key = "Surveys"
            integration.sync_interval_seconds = 60
            integration.detail_refresh_batch = 10
            integration.is_active = True
            integration.created_by = creator
            # A changed credential map must be verified again before the beat
            # scheduler is allowed to poll it.
            integration.scheduled_sync_enabled = False
            integration.full_clean(exclude=["last_test_status"])
            integration.save()

        action = "created" if client_created or integration_created else "updated"
        self.stdout.write(self.style.SUCCESS(f"Toluna integration {action}: id={integration.pk}"))

        if options["test"]:
            result = test_provider_connection(integration)
            integration.refresh_from_db()
            self.stdout.write(self.style.SUCCESS(
                "Connection verified: "
                f"cultures={len(result.get('configured_cultures') or [])}, "
                f"reference_questions={result.get('reference_questions', 0)}"
            ))
        if options["sync"]:
            run = sync_client_integration(integration, refresh_details=True)
            self.stdout.write(self.style.SUCCESS(
                f"Inventory sync {run.status}: created={run.created}, updated={run.updated}, "
                f"unchanged={run.unchanged}, closed={run.closed}, detail_failures={run.detail_failures}"
            ))
