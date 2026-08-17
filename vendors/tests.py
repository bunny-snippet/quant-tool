from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import AccessFunction, EmployeeProfile, Role, UserFunctionOverride
from surveys.models import Survey, SurveyAttempt

from .models import (
    AllocationReservation,
    Client,
    ClientIntegration,
    OrganizationClientAccess,
    OrganizationUnit,
    VendorAPIKey,
    VendorClientAllocation,
    VendorCommercialProfile,
    VendorSurveyAllocation,
)
from .services import (
    AllocationUnavailable,
    finalize_attempt_capacity,
    payable_cpi,
    reserve_attempt_capacity,
    resolve_vendor_survey_context,
    survey_pricing_for_user,
)
from .tasks import expire_allocation_reservations_task


class VendorFoundationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser("vendor-owner", "owner@example.test", "test-password")
        self.internal = User.objects.create_user("internal-vendor", first_name="Internal")
        self.external = User.objects.create_user("external-vendor", first_name="External")
        self.employee = User.objects.create_user("ordinary-employee")
        EmployeeProfile.objects.filter(user=self.internal).update(
            account_type=EmployeeProfile.AccountType.INTERNAL_VENDOR,
            role=Role.objects.get(slug="admin"),
            created_by=self.owner,
        )
        EmployeeProfile.objects.filter(user=self.external).update(
            account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR,
            role=Role.objects.get(slug="external-vendor"),
            created_by=self.owner,
        )
        self.client_record = Client.objects.create(
            code="uat-client",
            name="UAT Client",
            provider_code="innovatemr",
            created_by=self.owner,
        )
        self.survey = Survey.objects.create(
            client=self.client_record,
            source_id=88001,
            name="Allocation test survey",
            status=Survey.Status.LIVE,
            remaining=20,
            cpi=Decimal("10.00"),
        )
        self.external_policy = VendorCommercialProfile.objects.create(
            vendor=self.external,
            default_cpi_cut_percent=Decimal("30.00"),
            created_by=self.owner,
        )
        self.external_client_allocation = VendorClientAllocation.objects.create(
            vendor=self.external,
            client=self.client_record,
            quantity_limit=5,
            created_by=self.owner,
        )
        self.external_survey_allocation = VendorSurveyAllocation.objects.create(
            client_allocation=self.external_client_allocation,
            survey=self.survey,
            quantity_limit=2,
            created_by=self.owner,
        )

    def attempt(self, rid, status=SurveyAttempt.Status.INITIATED):
        return SurveyAttempt.objects.create(
            rid=rid,
            survey=self.survey,
            platform_user=self.external,
            user_id=str(self.external.pk),
            status=status,
        )

    def test_hidden_biobrain_client_is_not_published_before_inventory(self):
        hidden = Client.objects.create(
            code="catalog-hidden-biobrain", name="BioBrain", provider_code="biobrain", is_active=False
        )
        integration = ClientIntegration.objects.create(
            client=hidden,
            name="Hidden BioBrain",
            provider_code="biobrain",
            base_url="https://partner-api.voqall.com/api/v1/surveys",
            credential_env_key="BIOBRAIN_API_KEY",
        )
        api = APIClient()
        api.force_authenticate(self.owner)
        response = api.get(reverse("vendor-client-list"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(hidden.pk, [row["id"] for row in response.data["results"]])
        response = api.get(reverse("client-integration-list"))
        self.assertNotIn(integration.pk, [row["id"] for row in response.data["results"]])
        response = api.get(reverse("client-integration-providers"))
        self.assertNotIn("biobrain", [row["code"] for row in response.data])
        hidden.is_active = True
        hidden.save(update_fields=["is_active", "updated_at"])
        response = api.get(reverse("vendor-client-list"))
        self.assertIn(hidden.pk, [row["id"] for row in response.data["results"]])
        response = api.get(reverse("client-integration-list"))
        self.assertIn(integration.pk, [row["id"] for row in response.data["results"]])
        response = api.get(reverse("client-integration-providers"))
        self.assertIn("biobrain", [row["code"] for row in response.data])

    def test_external_cut_and_internal_full_cpi_rules(self):
        self.assertEqual(payable_cpi(Decimal("10.00"), Decimal("30.00")), Decimal("7.00"))
        self.assertEqual(self.external_survey_allocation.effective_cpi_cut_percent, Decimal("30.00"))

        internal_policy = VendorCommercialProfile(
            vendor=self.internal,
            default_cpi_cut_percent=Decimal("1.00"),
            created_by=self.owner,
        )
        with self.assertRaises(ValidationError):
            internal_policy.full_clean()

    def test_employee_role_can_show_a_configured_cpi_percentage(self):
        role = Role.objects.get(slug="team-lead")
        role.cpi_visibility_percent = Decimal("70.00")
        role.save(update_fields=["cpi_visibility_percent"])
        EmployeeProfile.objects.filter(user=self.employee).update(role=role)
        self.employee.employee_profile.refresh_from_db()

        visible_cpi, applied_cut = survey_pricing_for_user(self.employee, self.survey)

        self.assertEqual(visible_cpi, Decimal("7.00"))
        self.assertEqual(applied_cut, Decimal("30.00"))

    def test_reservation_freezes_cpi_and_completion_consumes_both_limits(self):
        attempt = self.attempt("Ua1Bb2Cc3D")
        reservation = reserve_attempt_capacity(attempt, self.external_survey_allocation)
        self.assertEqual(reservation.status, AllocationReservation.Status.RESERVED)
        attempt.refresh_from_db()
        self.assertEqual(attempt.vendor, self.external)
        self.assertEqual(attempt.client, self.client_record)
        self.assertEqual(attempt.source_cpi_snapshot, Decimal("10.00"))
        self.assertEqual(attempt.cpi_cut_percent_snapshot, Decimal("30.00"))
        self.assertEqual(attempt.payable_cpi_snapshot, Decimal("7.00"))

        self.survey.cpi = Decimal("6.00")
        self.survey.save(update_fields=["cpi"])
        attempt.status = SurveyAttempt.Status.COMPLETED
        attempt.save(update_fields=["status"])
        finalized = finalize_attempt_capacity(attempt)
        self.assertEqual(finalized.status, AllocationReservation.Status.CONSUMED)
        attempt.refresh_from_db()
        self.assertEqual(attempt.payable_cpi_snapshot, Decimal("7.00"))
        self.external_client_allocation.refresh_from_db()
        self.external_survey_allocation.refresh_from_db()
        self.assertEqual(self.external_client_allocation.consumed_quantity, 1)
        self.assertEqual(self.external_survey_allocation.consumed_quantity, 1)
        self.assertEqual(finalize_attempt_capacity(attempt).status, AllocationReservation.Status.CONSUMED)

        new_attempt = self.attempt("Ua9Mm8Nn7P")
        reserve_attempt_capacity(new_attempt, self.external_survey_allocation)
        new_attempt.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(attempt.payable_cpi_snapshot, Decimal("7.00"))
        self.assertEqual(new_attempt.source_cpi_snapshot, Decimal("6.00"))
        self.assertEqual(new_attempt.payable_cpi_snapshot, Decimal("4.20"))

    def test_non_complete_releases_and_exhausted_survey_rejects(self):
        attempt = self.attempt("Ua4Ee5Ff6G")
        reserve_attempt_capacity(attempt, self.external_survey_allocation)
        attempt.status = SurveyAttempt.Status.TERMINATED
        attempt.save(update_fields=["status"])
        self.assertEqual(finalize_attempt_capacity(attempt).status, AllocationReservation.Status.RELEASED)
        self.external_survey_allocation.refresh_from_db()
        self.assertEqual(self.external_survey_allocation.remaining_quantity, 2)

        self.external_survey_allocation.quantity_limit = 0
        self.external_survey_allocation.save(update_fields=["quantity_limit"])
        with self.assertRaisesMessage(AllocationUnavailable, "Project complete cap is exhausted"):
            reserve_attempt_capacity(self.attempt("Ua7Hh8Ii9J"), self.external_survey_allocation)

    def test_client_grant_exposes_all_projects_and_uses_shared_capacity(self):
        unallocated_survey = Survey.objects.create(
            client=self.client_record,
            source_id=88002,
            name="Client-level survey",
            status=Survey.Status.LIVE,
            remaining=10,
            cpi=Decimal("10.00"),
            entry_link="https://edgeapi.innovatemr.net/startSurvey?survNum=uat&supCode=1150&PID=[%%pid%%]",
            targeting_synced_at=timezone.now(),
        )
        hidden_client = Client.objects.create(code="hidden", name="Hidden client", created_by=self.owner)
        Survey.objects.create(
            client=hidden_client,
            source_id=88003,
            name="Hidden survey",
            status=Survey.Status.LIVE,
            remaining=10,
            cpi=Decimal("20.00"),
        )
        for code in ["projects.view", "survey_links.copy", "attempts.view"]:
            UserFunctionOverride.objects.create(
                user=self.external,
                function=AccessFunction.objects.get(code=code),
                effect=UserFunctionOverride.Effect.ALLOW,
            )

        api = APIClient()
        api.force_authenticate(self.external)
        listing = api.get(reverse("survey-list"))
        self.assertEqual(listing.status_code, 200)
        rows = {item["source_id"]: item for item in listing.data["results"]}
        self.assertEqual(set(rows), {88001, 88002})

        start = self.client.get(
            reverse("survey-start"),
            {
                "surveyId": unallocated_survey.source_id,
                "supplierCode": "1000",
                "userId": self.external.pk,
                "code": unallocated_survey.local_id,
            },
        )
        self.assertEqual(start.status_code, 302)
        created_attempt = SurveyAttempt.objects.get(survey=unallocated_survey)
        reservation = created_attempt.allocation_reservation
        self.assertEqual(reservation.client_allocation, self.external_client_allocation)
        self.assertIsNone(reservation.survey_allocation)

        created_attempt.status = SurveyAttempt.Status.COMPLETED
        created_attempt.save(update_fields=["status"])
        finalize_attempt_capacity(created_attempt)
        self.external_client_allocation.refresh_from_db()
        self.assertEqual(self.external_client_allocation.consumed_quantity, 1)

    def test_superuser_can_manage_foundation_api_and_employee_cannot(self):
        owner_api = APIClient()
        owner_api.force_authenticate(self.owner)
        response = owner_api.post(reverse("vendor-client-list"), {
            "code": "second-client",
            "name": "Second Client",
            "provider_code": "custom",
        })
        self.assertEqual(response.status_code, 201)
        directory = owner_api.get(reverse("vendor-directory-list"))
        self.assertEqual(directory.status_code, 200)
        self.assertEqual(directory.data["count"], 2)

        employee_api = APIClient()
        employee_api.force_authenticate(self.employee)
        self.assertEqual(employee_api.get(reverse("vendor-client-list")).status_code, 403)

    def test_vendor_api_is_read_only_and_scoped_to_its_own_allocations(self):
        sibling = get_user_model().objects.create_user("sibling-vendor")
        EmployeeProfile.objects.filter(user=sibling).update(
            account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR,
            created_by=self.owner,
        )
        sibling_allocation = VendorClientAllocation.objects.create(
            vendor=sibling,
            client=self.client_record,
            quantity_limit=3,
            created_by=self.owner,
        )
        for code in ["clients.view", "vendors.view", "vendors.tab.clients"]:
            UserFunctionOverride.objects.create(
                user=self.external,
                function=AccessFunction.objects.get(code=code),
                effect=UserFunctionOverride.Effect.ALLOW,
            )

        api = APIClient()
        api.force_authenticate(self.external)
        listing = api.get(reverse("vendor-client-allocation-list"))
        self.assertEqual(listing.status_code, 200)
        self.assertEqual([item["id"] for item in listing.data["results"]], [self.external_client_allocation.id])
        self.assertEqual(
            api.get(reverse("vendor-client-allocation-detail", kwargs={"pk": sibling_allocation.pk})).status_code,
            404,
        )
        self.assertEqual(
            api.patch(
                reverse("vendor-client-allocation-detail", kwargs={"pk": self.external_client_allocation.pk}),
                {"quantity_limit": 99},
                format="json",
            ).status_code,
            403,
        )

    def test_expiry_boundary_is_recorded_for_future_cleanup_job(self):
        attempt = self.attempt("Ua0Kk1Ll2M")
        reservation = reserve_attempt_capacity(
            attempt,
            self.external_survey_allocation,
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        self.assertGreater(reservation.expires_at, timezone.now())

        AllocationReservation.objects.filter(pk=reservation.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        result = expire_allocation_reservations_task.run()
        reservation.refresh_from_db()
        self.external_client_allocation.refresh_from_db()
        self.external_survey_allocation.refresh_from_db()
        self.assertEqual(result["expired"], 1)
        self.assertEqual(reservation.status, AllocationReservation.Status.EXPIRED)
        self.assertEqual(self.external_client_allocation.reserved_quantity, 0)
        self.assertEqual(self.external_survey_allocation.reserved_quantity, 0)

    def test_inactive_project_override_hides_only_that_project(self):
        second = Survey.objects.create(
            client=self.client_record,
            source_id=88004,
            name="Client fallback survey",
            status=Survey.Status.LIVE,
            remaining=4,
            cpi=Decimal("4.00"),
        )
        self.external_survey_allocation.is_active = False
        self.external_survey_allocation.save(update_fields=["is_active"])
        UserFunctionOverride.objects.create(
            user=self.external,
            function=AccessFunction.objects.get(code="projects.view"),
            effect=UserFunctionOverride.Effect.ALLOW,
        )
        api = APIClient()
        api.force_authenticate(self.external)
        ids = {row["source_id"] for row in api.get(reverse("survey-list")).data["results"]}
        self.assertEqual(ids, {88004})

    def test_allocation_manager_can_open_workspace_and_use_safe_options(self):
        for code in ("allocations.view", "vendors.tab.clients", "vendors.column.client.client"):
            UserFunctionOverride.objects.create(
                user=self.employee,
                function=AccessFunction.objects.get(code=code),
                effect=UserFunctionOverride.Effect.ALLOW,
            )
        self.client.force_login(self.employee)
        page = self.client.get(reverse("vendor-management"))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Commercial policies")
        self.assertContains(page, "Client allocations")
        self.assertNotContains(page, "Project allocations")
        options = self.client.get(reverse("vendor-management-options"))
        self.assertEqual(options.status_code, 200)
        self.assertEqual(len(options.json()["vendors"]), 2)
        self.assertIn(self.client_record.pk, {item["id"] for item in options.json()["clients"]})
        self.assertEqual(self.client.get(reverse("vendor-survey-allocation-list")).status_code, 403)

    def test_vendor_management_uses_separate_task_modals(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("vendor-management"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="vendorPolicyModal"')
        self.assertContains(page, 'id="clientAllocationModal"')
        self.assertContains(page, 'id="surveyAllocationModal"')
        self.assertContains(page, 'id="vendorApiKeyModal"')
        self.assertNotContains(page, 'id="vendorManagementForm"')
        script = (Path(settings.BASE_DIR) / "static" / "vendors" / "management.js").read_text(encoding="utf-8")
        self.assertIn("closest('button[data-edit-policy]')", script)
        self.assertNotIn("closest('[data-edit-policy]')", script)
        self.assertIn("closest('button[data-revoke-api-key]')", script)
        self.assertNotIn("closest('[data-revoke-api-key]')", script)

    def test_supplier_directory_hides_profiles_without_the_required_role(self):
        EmployeeProfile.objects.filter(user=self.internal).update(role=None)
        api = APIClient()
        api.force_authenticate(self.owner)

        directory = api.get(reverse("vendor-directory-list"))
        self.assertEqual(directory.status_code, 200)
        self.assertNotIn(self.internal.pk, [row["id"] for row in directory.data["results"]])
        options = api.get(reverse("vendor-management-options"))
        self.assertNotIn(self.internal.pk, [row["id"] for row in options.data["vendors"]])

    def test_directory_reports_average_effective_client_cut(self):
        second_client = Client.objects.create(
            code="average-cut-client", name="Average Cut Client", created_by=self.owner,
        )
        VendorClientAllocation.objects.create(
            vendor=self.external,
            client=second_client,
            quantity_limit=10,
            cpi_cut_override_percent=Decimal("50.00"),
            created_by=self.owner,
        )
        api = APIClient()
        api.force_authenticate(self.owner)
        response = api.get(reverse("vendor-directory-list"))
        row = next(item for item in response.data["results"] if item["id"] == self.external.pk)
        self.assertEqual(row["active_client_allocation_count"], 2)
        self.assertEqual(Decimal(row["average_client_cpi_cut_percent"]), Decimal("40.00"))

    def test_api_key_can_be_scoped_to_one_selected_client(self):
        self.external_policy.delivery_mode = VendorCommercialProfile.DeliveryMode.BOTH
        self.external_policy.save(update_fields=["delivery_mode", "updated_at"])
        second_client = Client.objects.create(
            code="key-scope-client", name="Key Scope Client", created_by=self.owner,
        )
        second_survey = Survey.objects.create(
            client=second_client,
            source_id=88007,
            name="Key scoped survey",
            status=Survey.Status.LIVE,
            remaining=5,
            cpi=Decimal("8.00"),
        )
        second_allocation = VendorClientAllocation.objects.create(
            vendor=self.external,
            client=second_client,
            quantity_limit=5,
            created_by=self.owner,
        )
        owner_api = APIClient()
        owner_api.force_authenticate(self.owner)
        issued = owner_api.post(reverse("vendor-api-key-list"), {
            "vendor": self.external.pk,
            "name": "One client only",
            "client_allocations": [second_allocation.pk],
        }, format="json")
        self.assertEqual(issued.status_code, 201)
        self.assertEqual(issued.data["client_names"], [second_client.name])

        listing = APIClient().get(reverse("survey-list"), HTTP_X_API_KEY=issued.data["api_key"])
        self.assertEqual(listing.status_code, 200)
        self.assertEqual([row["source_id"] for row in listing.data["results"]], [second_survey.source_id])

        allocation_detail = owner_api.get(
            reverse("vendor-client-allocation-detail", kwargs={"pk": second_allocation.pk})
        )
        self.assertEqual(allocation_detail.data["api_key_scopes"][0]["name"], "One client only")

    def test_api_key_inherits_live_client_scope_and_different_client_cuts(self):
        self.external_policy.delivery_mode = VendorCommercialProfile.DeliveryMode.BOTH
        self.external_policy.save(update_fields=["delivery_mode", "updated_at"])
        second_client = Client.objects.create(
            code="second-cut-client", name="Second Cut Client", provider_code="custom", created_by=self.owner,
        )
        second_survey = Survey.objects.create(
            client=second_client,
            source_id=88005,
            name="Fifty percent survey",
            status=Survey.Status.LIVE,
            remaining=9,
            cpi=Decimal("10.00"),
        )
        second_client_allocation = VendorClientAllocation.objects.create(
            vendor=self.external,
            client=second_client,
            quantity_limit=4,
            cpi_cut_override_percent=Decimal("50.00"),
            created_by=self.owner,
        )
        VendorSurveyAllocation.objects.create(
            client_allocation=second_client_allocation,
            survey=second_survey,
            quantity_limit=4,
            created_by=self.owner,
        )
        hidden_client = Client.objects.create(code="api-hidden", name="API Hidden", created_by=self.owner)
        Survey.objects.create(
            client=hidden_client,
            source_id=88006,
            name="Not allocated",
            status=Survey.Status.LIVE,
            remaining=5,
            cpi=Decimal("99.00"),
        )

        owner_api = APIClient()
        owner_api.force_authenticate(self.owner)
        issued = owner_api.post(reverse("vendor-api-key-list"), {
            "vendor": self.external.pk,
            "name": "UAT integration",
            "client_allocations": [self.external_client_allocation.pk, second_client_allocation.pk],
        }, format="json")
        self.assertEqual(issued.status_code, 201)
        raw_key = issued.data["api_key"]
        self.assertTrue(raw_key.startswith("exh_"))
        self.assertFalse(VendorAPIKey.objects.get(pk=issued.data["id"]).key_hash == raw_key)

        vendor_api = APIClient()
        listing = vendor_api.get(reverse("survey-list"), HTTP_X_API_KEY=raw_key)
        self.assertEqual(listing.status_code, 200)
        rows = {item["source_id"]: item for item in listing.data["results"]}
        self.assertEqual(set(rows), {self.survey.source_id, second_survey.source_id})
        self.assertEqual(Decimal(rows[self.survey.source_id]["cpi"]), Decimal("7.00"))
        self.assertEqual(Decimal(rows[self.survey.source_id]["cpi_cut_percent"]), Decimal("30.00"))
        self.assertEqual(Decimal(rows[second_survey.source_id]["cpi"]), Decimal("5.00"))
        self.assertEqual(Decimal(rows[second_survey.source_id]["cpi_cut_percent"]), Decimal("50.00"))
        self.assertEqual(rows[second_survey.source_id]["display_company_name"], second_client.name)

        filtered = vendor_api.get(reverse("survey-list"), {"client_name": second_client.name}, HTTP_X_API_KEY=raw_key)
        self.assertEqual([row["source_id"] for row in filtered.data["results"]], [second_survey.source_id])
        UserFunctionOverride.objects.create(
            user=self.external,
            function=AccessFunction.objects.get(code="projects.filter.cpi"),
            effect=UserFunctionOverride.Effect.ALLOW,
        )
        visible_cpi_filtered = vendor_api.get(
            reverse("survey-list"),
            {"min_cpi": "5.50", "max_cpi": "7.50", "ordering": "-cpi"},
            HTTP_X_API_KEY=raw_key,
        )
        self.assertEqual(visible_cpi_filtered.status_code, 200)
        self.assertEqual(
            [row["source_id"] for row in visible_cpi_filtered.data["results"]],
            [self.survey.source_id],
        )
        key_record = VendorAPIKey.objects.get(pk=issued.data["id"])
        self.assertIsNotNone(key_record.last_used_at)

        key_list = owner_api.get(reverse("vendor-api-key-list"))
        self.assertEqual(key_list.status_code, 200)
        self.assertNotIn("api_key", key_list.data["results"][0])
        self.assertEqual(
            owner_api.delete(reverse("vendor-api-key-detail", kwargs={"pk": key_record.pk})).status_code,
            204,
        )
        self.assertIn(
            vendor_api.get(reverse("survey-list"), HTTP_X_API_KEY=raw_key).status_code,
            {401, 403},
        )

    def test_delivery_mode_blocks_wrong_channel(self):
        owner_api = APIClient()
        owner_api.force_authenticate(self.owner)
        panel_only_key = owner_api.post(reverse("vendor-api-key-list"), {
            "vendor": self.external.pk,
            "name": "Blocked panel-only key",
        }, format="json")
        self.assertEqual(panel_only_key.status_code, 400)

        self.external_policy.delivery_mode = VendorCommercialProfile.DeliveryMode.API
        self.external_policy.save(update_fields=["delivery_mode", "updated_at"])
        self.external.set_password("password-123")
        self.external.save(update_fields=["password"])
        login = self.client.post(reverse("login"), {
            "username": self.external.username,
            "password": "password-123",
        })
        self.assertEqual(login.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(login, "Username or password is incorrect")

        issued = owner_api.post(reverse("vendor-api-key-list"), {
            "vendor": self.external.pk,
            "name": "API-only integration",
            "client_allocations": [self.external_client_allocation.pk],
        }, format="json")
        self.assertEqual(issued.status_code, 201)
        self.assertEqual(
            APIClient().get(reverse("survey-list"), HTTP_X_API_KEY=issued.data["api_key"]).status_code,
            200,
        )

    def test_vendor_scoped_admin_cannot_manage_owner_api_keys(self):
        for code in ("vendors.tab.api_keys", "vendors.action.create_api_key"):
            UserFunctionOverride.objects.create(
                user=self.internal,
                function=AccessFunction.objects.get(code=code),
                effect=UserFunctionOverride.Effect.ALLOW,
            )
        internal_api = APIClient()
        internal_api.force_authenticate(self.internal)
        listing = internal_api.get(reverse("vendor-api-key-list"))
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data["count"], 0)
        response = internal_api.post(reverse("vendor-api-key-list"), {
            "vendor": self.external.pk,
            "name": "Forbidden delegated key",
        }, format="json")
        self.assertEqual(response.status_code, 403)


class OrganizationHierarchyTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser("organization-owner", "org-owner@example.test", "test-password")
        self.internal = User.objects.create_user("organization-internal", first_name="Internal", last_name="Vendor")
        EmployeeProfile.objects.filter(user=self.internal).update(
            account_type=EmployeeProfile.AccountType.INTERNAL_VENDOR,
            role=Role.objects.get(slug="admin"),
            created_by=self.owner,
        )
        VendorCommercialProfile.objects.create(vendor=self.internal, created_by=self.owner)
        self.client_a = Client.objects.create(code="client-a", name="Client A", created_by=self.owner)
        self.client_b = Client.objects.create(code="client-b", name="Client B", created_by=self.owner)
        self.survey_a = Survey.objects.create(
            client=self.client_a, source_id=99001, name="Client A survey", status=Survey.Status.LIVE,
            remaining=10, cpi=Decimal("3.00"),
        )
        self.survey_b = Survey.objects.create(
            client=self.client_b, source_id=99002, name="Client B survey", status=Survey.Status.LIVE,
            remaining=10, cpi=Decimal("4.00"),
        )
        self.owner_api = APIClient()
        self.owner_api.force_authenticate(self.owner)

    def create_tree(self, owner, prefix):
        branch = OrganizationUnit.objects.create(
            workspace_owner=owner, unit_type=OrganizationUnit.UnitType.BRANCH,
            name=f"{prefix} Branch", code=f"{prefix}-branch", created_by=self.owner,
        )
        sub_branch = OrganizationUnit.objects.create(
            workspace_owner=owner, parent=branch, unit_type=OrganizationUnit.UnitType.SUB_BRANCH,
            name="Operations", code="operations", created_by=self.owner,
        )
        shift = OrganizationUnit.objects.create(
            workspace_owner=owner, parent=sub_branch, unit_type=OrganizationUnit.UnitType.SHIFT,
            name="Morning", code="morning", created_by=self.owner,
        )
        return branch, sub_branch, shift

    def test_owner_can_build_strict_tree_and_internal_vendor_is_scoped(self):
        branch = self.owner_api.post(reverse("organization-unit-list"), {
            "workspace_owner": self.internal.pk,
            "unit_type": "branch",
            "name": "Gurgaon",
            "code": "gurgaon",
            "is_active": True,
        }, format="json")
        self.assertEqual(branch.status_code, 201)
        sub_branch = self.owner_api.post(reverse("organization-unit-list"), {
            "workspace_owner": self.internal.pk,
            "parent": branch.data["id"],
            "unit_type": "sub_branch",
            "name": "Operations",
            "code": "operations",
            "is_active": True,
        }, format="json")
        self.assertEqual(sub_branch.status_code, 201)
        invalid_shift = self.owner_api.post(reverse("organization-unit-list"), {
            "workspace_owner": self.internal.pk,
            "parent": branch.data["id"],
            "unit_type": "shift",
            "name": "Invalid",
            "code": "invalid",
        }, format="json")
        self.assertEqual(invalid_shift.status_code, 400)
        shift = self.owner_api.post(reverse("organization-unit-list"), {
            "workspace_owner": self.internal.pk,
            "parent": sub_branch.data["id"],
            "unit_type": "shift",
            "name": "Morning",
            "code": "morning",
        }, format="json")
        self.assertEqual(shift.status_code, 201)

        internal_api = APIClient()
        internal_api.force_authenticate(self.internal)
        listing = internal_api.get(reverse("organization-unit-list"))
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data["count"], 3)
        self.assertTrue(all(row["workspace_owner"] == self.internal.pk for row in listing.data["results"]))
        self.client.force_login(self.internal)
        page = self.client.get(reverse("organization-management"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Organization hierarchy")

    def test_unit_client_grants_filter_main_office_projects(self):
        branch, _, shift = self.create_tree(self.owner, "main")
        OrganizationClientAccess.objects.create(
            organization_unit=branch, client=self.client_a, created_by=self.owner,
        )
        employee = get_user_model().objects.create_user("main-shift-employee")
        EmployeeProfile.objects.filter(user=employee).update(
            organization_unit=shift,
            created_by=self.owner,
            role=Role.objects.get(slug="employee"),
        )
        api = APIClient()
        api.force_authenticate(employee)
        response = api.get(reverse("survey-list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row["source_id"] for row in response.data["results"]}, {self.survey_a.source_id})
        with self.assertRaisesMessage(AllocationUnavailable, "not assigned"):
            resolve_vendor_survey_context(employee, self.survey_b)

    def test_shift_client_grants_override_broader_branch_grants(self):
        branch, _, shift = self.create_tree(self.owner, "shift-override")
        OrganizationClientAccess.objects.bulk_create([
            OrganizationClientAccess(
                organization_unit=branch, client=self.client_a, created_by=self.owner,
            ),
            OrganizationClientAccess(
                organization_unit=branch, client=self.client_b, created_by=self.owner,
            ),
            OrganizationClientAccess(
                organization_unit=shift, client=self.client_a, created_by=self.owner,
            ),
        ])
        employee = get_user_model().objects.create_user("shift-override-employee")
        EmployeeProfile.objects.filter(user=employee).update(
            organization_unit=shift,
            created_by=self.owner,
            role=Role.objects.get(slug="employee"),
        )
        api = APIClient()
        api.force_authenticate(employee)

        response = api.get(reverse("survey-list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {row["source_id"] for row in response.data["results"]},
            {self.survey_a.source_id},
        )
        with self.assertRaisesMessage(AllocationUnavailable, "not assigned"):
            resolve_vendor_survey_context(employee, self.survey_b)

    def test_shift_members_and_clients_roll_up_to_parent_totals(self):
        branch, sub_branch, shift = self.create_tree(self.owner, "rollup")
        OrganizationClientAccess.objects.create(
            organization_unit=shift, client=self.client_a, created_by=self.owner,
        )
        for index in range(2):
            employee = get_user_model().objects.create_user(f"rollup-employee-{index}")
            EmployeeProfile.objects.filter(user=employee).update(
                organization_unit=shift,
                created_by=self.owner,
                role=Role.objects.get(slug="employee"),
            )

        response = self.owner_api.get(reverse("organization-unit-list"))

        self.assertEqual(response.status_code, 200)
        units = {row["id"]: row for row in response.data["results"]}
        for unit in (branch, sub_branch, shift):
            self.assertEqual(units[unit.pk]["member_count"], 2)
            self.assertEqual(units[unit.pk]["client_count"], 1)
        self.assertEqual(units[branch.pk]["direct_member_count"], 0)
        self.assertEqual(units[branch.pk]["direct_client_count"], 0)
        self.assertEqual(units[shift.pk]["direct_member_count"], 2)
        self.assertEqual(units[shift.pk]["direct_client_count"], 1)

    def test_user_creation_assigns_team_leads_and_employees_to_shifts(self):
        branch, _, shift = self.create_tree(self.owner, "people")
        invalid = self.owner_api.post(reverse("access-user-list"), {
            "first_name": "Invalid", "last_name": "Lead", "email": "invalid-lead@example.test",
            "password": "password-123", "role": "team-lead", "account_type": "employee",
            "organization_unit": branch.pk, "allow_codes": [], "deny_codes": [],
        }, format="json")
        self.assertEqual(invalid.status_code, 400)
        created = self.owner_api.post(reverse("access-user-list"), {
            "first_name": "Morning", "last_name": "Lead", "email": "morning-lead@example.test",
            "password": "password-123", "role": "team-lead", "account_type": "employee",
            "organization_unit": shift.pk, "allow_codes": [], "deny_codes": [],
        }, format="json")
        self.assertEqual(created.status_code, 201)
        lead = get_user_model().objects.get(email="morning-lead@example.test")
        self.assertEqual(lead.employee_profile.organization_unit, shift)
        self.assertEqual(created.data["organization_unit_details"]["path"], "people Branch / Operations / Morning")

    def test_internal_vendor_unit_grants_intersect_vendor_allocations(self):
        allocation_a = VendorClientAllocation.objects.create(
            vendor=self.internal, client=self.client_a, quantity_limit=10, created_by=self.owner,
        )
        allocation_b = VendorClientAllocation.objects.create(
            vendor=self.internal, client=self.client_b, quantity_limit=10, created_by=self.owner,
        )
        VendorSurveyAllocation.objects.create(
            client_allocation=allocation_a, survey=self.survey_a, quantity_limit=10, created_by=self.owner,
        )
        VendorSurveyAllocation.objects.create(
            client_allocation=allocation_b, survey=self.survey_b, quantity_limit=10, created_by=self.owner,
        )
        branch, _, shift = self.create_tree(self.internal, "internal")
        OrganizationClientAccess.objects.create(
            organization_unit=branch, client=self.client_a, created_by=self.owner,
        )
        employee = get_user_model().objects.create_user("internal-shift-employee")
        EmployeeProfile.objects.filter(user=employee).update(
            organization_unit=shift,
            created_by=self.owner,
            role=Role.objects.get(slug="employee"),
        )
        api = APIClient()
        api.force_authenticate(employee)
        response = api.get(reverse("survey-list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row["source_id"] for row in response.data["results"]}, {self.survey_a.source_id})

        unallocated_client = Client.objects.create(code="not-allocated", name="Not Allocated", created_by=self.owner)
        rejected = self.owner_api.post(reverse("organization-client-access-list"), {
            "organization_unit": branch.pk,
            "client": unallocated_client.pk,
            "is_active": True,
        }, format="json")
        self.assertEqual(rejected.status_code, 400)

    def test_existing_tree_cannot_be_reparented_into_a_cycle(self):
        branch, sub_branch, _ = self.create_tree(self.owner, "cycle")
        response = self.owner_api.patch(reverse("organization-unit-detail", args=[branch.pk]), {
            "unit_type": OrganizationUnit.UnitType.SHIFT,
            "parent": sub_branch.pk,
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("parent", response.data)

    def test_existing_branch_can_be_corrected_to_sub_branch_under_another_branch(self):
        destination = OrganizationUnit.objects.create(
            workspace_owner=self.owner, unit_type=OrganizationUnit.UnitType.BRANCH,
            name="Destination", code="destination", created_by=self.owner,
        )
        mistaken = OrganizationUnit.objects.create(
            workspace_owner=self.owner, unit_type=OrganizationUnit.UnitType.BRANCH,
            name="Mistaken Branch", code="mistaken-branch", created_by=self.owner,
        )
        response = self.owner_api.patch(reverse("organization-unit-detail", args=[mistaken.pk]), {
            "unit_type": OrganizationUnit.UnitType.SUB_BRANCH,
            "parent": destination.pk,
            "name": "Corrected Sub-branch",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        mistaken.refresh_from_db()
        self.assertEqual(mistaken.unit_type, OrganizationUnit.UnitType.SUB_BRANCH)
        self.assertEqual(mistaken.parent, destination)
        self.assertEqual(mistaken.name, "Corrected Sub-branch")

    def test_unused_unit_deletes_and_parent_with_children_returns_conflict(self):
        unused = OrganizationUnit.objects.create(
            workspace_owner=self.owner, unit_type=OrganizationUnit.UnitType.BRANCH,
            name="Unused", code="unused", created_by=self.owner,
        )
        deleted = self.owner_api.delete(reverse("organization-unit-detail", args=[unused.pk]))
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(OrganizationUnit.objects.filter(pk=unused.pk).exists())

        branch, _, _ = self.create_tree(self.owner, "protected")
        protected = self.owner_api.delete(reverse("organization-unit-detail", args=[branch.pk]))
        self.assertEqual(protected.status_code, 409)
        self.assertIn("child units", protected.data["detail"])

    def test_client_access_can_be_removed_before_deleting_unit(self):
        branch = OrganizationUnit.objects.create(
            workspace_owner=self.owner, unit_type=OrganizationUnit.UnitType.BRANCH,
            name="Client Branch", code="client-branch", created_by=self.owner,
        )
        grant = OrganizationClientAccess.objects.create(
            organization_unit=branch, client=self.client_a, created_by=self.owner,
        )
        blocked = self.owner_api.delete(reverse("organization-unit-detail", args=[branch.pk]))
        self.assertEqual(blocked.status_code, 409)
        removed = self.owner_api.delete(reverse("organization-client-access-detail", args=[grant.pk]))
        self.assertEqual(removed.status_code, 204)
        self.assertFalse(OrganizationClientAccess.objects.filter(pk=grant.pk).exists())
        deleted = self.owner_api.delete(reverse("organization-unit-detail", args=[branch.pk]))
        self.assertEqual(deleted.status_code, 204)

    def test_user_cannot_be_assigned_below_an_inactive_ancestor(self):
        branch, _, shift = self.create_tree(self.owner, "inactive")
        branch.is_active = False
        branch.save(update_fields=["is_active", "updated_at"])
        response = self.owner_api.post(reverse("access-user-list"), {
            "first_name": "Blocked", "last_name": "Employee", "email": "blocked@example.test",
            "password": "password-123", "role": "employee", "account_type": "employee",
            "organization_unit": shift.pk, "allow_codes": [], "deny_codes": [],
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("organization_unit", response.data)

    def test_external_vendor_cannot_receive_organization_access_by_override(self):
        external = get_user_model().objects.create_user("organization-external")
        EmployeeProfile.objects.filter(user=external).update(
            account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR,
            role=Role.objects.get(slug="external-vendor"),
            created_by=self.owner,
        )
        function = AccessFunction.objects.get(code="organization.view")
        UserFunctionOverride.objects.create(
            user=external,
            function=function,
            effect=UserFunctionOverride.Effect.ALLOW,
        )
        external_api = APIClient()
        external_api.force_authenticate(external)
        response = external_api.get(reverse("organization-unit-list"))
        self.assertEqual(response.status_code, 403)

    def test_internal_vendor_organization_options_do_not_expose_unallocated_clients(self):
        VendorClientAllocation.objects.create(
            vendor=self.internal,
            client=self.client_a,
            quantity_limit=10,
            created_by=self.owner,
        )
        internal_api = APIClient()
        internal_api.force_authenticate(self.internal)
        response = internal_api.get(reverse("organization-options"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row["id"] for row in response.data["clients"]}, {self.client_a.pk})
        self.assertEqual(response.data["client_eligibility"][str(self.internal.pk)], [self.client_a.pk])
