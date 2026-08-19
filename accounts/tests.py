import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from .access import has_function_access
from .function_catalog import sync_access_function_catalog
from .models import AccessFunction, EmployeeProfile, Role, RoleFunctionPermission, UserFunctionOverride


class RoleConfigurationCommandTests(TestCase):
    def test_import_replaces_roles_and_preserves_user_overrides(self):
        projects = AccessFunction.objects.get(code="projects.view")
        attempts = AccessFunction.objects.get(code="attempts.view")
        source_role = Role.objects.create(
            name="Quest operator",
            slug="quest-operator",
            description="Transferred role",
            rank=27,
            cpi_visibility_percent="72.50",
        )
        RoleFunctionPermission.objects.create(role=source_role, function=projects, allowed=True)
        RoleFunctionPermission.objects.create(role=source_role, function=attempts, allowed=False)

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export_path = root / "quest-roles.json"
            backup_path = root / "quant-roles-before-import.json"
            call_command("role_config", export_path=str(export_path))

            source_role.name = "Changed locally"
            source_role.cpi_visibility_percent = "10.00"
            source_role.save(update_fields=["name", "cpi_visibility_percent", "updated_at"])
            source_role.function_assignments.all().delete()

            extra_role = Role.objects.create(name="Quant only", slug="quant-only", rank=90)
            user = get_user_model().objects.create_user(username="quant-user", password="password-123")
            profile = user.employee_profile
            profile.role = extra_role
            profile.save(update_fields=["role", "updated_at"])
            override = UserFunctionOverride.objects.create(
                user=user,
                function=attempts,
                effect=UserFunctionOverride.Effect.ALLOW,
            )

            call_command("role_config", import_path=str(export_path), dry_run=True)
            self.assertTrue(Role.objects.filter(slug="quant-only").exists())

            call_command(
                "role_config",
                import_path=str(export_path),
                replace=True,
                backup=str(backup_path),
            )

            restored = Role.objects.get(slug="quest-operator")
            self.assertEqual(restored.name, "Quest operator")
            self.assertEqual(str(restored.cpi_visibility_percent), "72.50")
            self.assertEqual(
                dict(restored.function_assignments.values_list("function__code", "allowed")),
                {"attempts.view": False, "projects.view": True},
            )
            self.assertFalse(Role.objects.filter(slug="quant-only").exists())
            profile.refresh_from_db()
            self.assertEqual(profile.role.slug, "employee")
            self.assertTrue(UserFunctionOverride.objects.filter(pk=override.pk).exists())
            self.assertTrue(backup_path.is_file())

    def test_import_requires_explicit_replace_confirmation(self):
        with TemporaryDirectory() as temporary_directory:
            export_path = Path(temporary_directory) / "roles.json"
            call_command("role_config", export_path=str(export_path))
            with self.assertRaises(CommandError):
                call_command("role_config", import_path=str(export_path))

    def test_import_accepts_source_without_employee_role(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export_path = root / "roles-without-employee.json"
            call_command("role_config", export_path=str(export_path))
            payload = json.loads(export_path.read_text(encoding="utf-8"))
            payload["roles"] = [role for role in payload["roles"] if role["slug"] != "employee"]
            export_path.write_text(json.dumps(payload), encoding="utf-8")

            extra_role = Role.objects.create(name="Quant only", slug="quant-only", rank=90)
            user = get_user_model().objects.create_user(username="roleless-user", password="password-123")
            user.employee_profile.role = extra_role
            user.employee_profile.save(update_fields=["role", "updated_at"])

            call_command(
                "role_config",
                import_path=str(export_path),
                replace=True,
                backup=str(root / "target-backup.json"),
            )

            user.employee_profile.refresh_from_db()
            self.assertIsNone(user.employee_profile.role)
            self.assertFalse(Role.objects.filter(slug__in=["employee", "quant-only"]).exists())

    def test_import_translates_legacy_summary_permission_to_current_cards(self):
        legacy = AccessFunction.objects.create(
            code="user_hits.summary",
            name="Legacy User Hits summary",
            module="User Hits",
        )
        source_role = Role.objects.create(name="Legacy operator", slug="legacy-operator", rank=25)
        RoleFunctionPermission.objects.create(role=source_role, function=legacy, allowed=False)

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export_path = root / "legacy-roles.json"
            call_command("role_config", export_path=str(export_path))
            legacy.delete()

            call_command(
                "role_config",
                import_path=str(export_path),
                replace=True,
                backup=str(root / "target-backup.json"),
            )

            assignments = dict(
                Role.objects.get(slug="legacy-operator").function_assignments.filter(
                    function__code__startswith="user_hits.card."
                ).values_list("function__code", "allowed")
            )
            self.assertEqual(set(assignments), {
                "user_hits.card.total_hits",
                "user_hits.card.completes",
                "user_hits.card.conversion",
                "user_hits.card.active_users",
                "user_hits.card.devices",
                "user_hits.card.ir",
            })
            self.assertEqual(set(assignments.values()), {False})


class LoginAndSetupTests(TestCase):
    def test_anonymous_internal_page_redirects_to_login(self):
        response = self.client.get(reverse("projects"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_first_owner_setup_creates_super_admin_and_closes_setup(self):
        response = self.client.post(reverse("first-admin-setup"), {
            "first_name": "Workspace", "last_name": "Owner", "username": "owner",
            "email": "owner@example.test", "password1": "safe-password-123", "password2": "safe-password-123",
        })
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        user = get_user_model().objects.get(username="owner")
        self.assertTrue(user.is_superuser)
        self.assertEqual(user.employee_profile.role.slug, "super-admin")
        self.assertEqual(self.client.get(reverse("first-admin-setup")).status_code, 404)


class FunctionAccessTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="employee", password="password-123")
        self.client.force_login(self.user)

    def test_employee_role_can_view_projects_but_not_access_control(self):
        self.assertTrue(has_function_access(self.user, "projects.view"))
        self.assertEqual(self.client.get(reverse("projects")).status_code, 200)
        self.assertEqual(self.client.get(reverse("access-control")).status_code, 403)

    def test_user_allow_and_deny_override_role_baseline(self):
        attempts = AccessFunction.objects.get(code="attempts.view")
        projects = AccessFunction.objects.get(code="projects.view")
        UserFunctionOverride.objects.create(user=self.user, function=attempts, effect="allow")
        UserFunctionOverride.objects.create(user=self.user, function=projects, effect="deny")
        self.assertTrue(has_function_access(self.user, "attempts.view"))
        self.assertFalse(has_function_access(self.user, "projects.view"))
        self.assertEqual(self.client.get(reverse("projects")).status_code, 403)
        self.assertRedirects(self.client.get(reverse("home")), reverse("traffic-reports"), fetch_redirect_response=False)

    def test_dashboard_access_follows_role_or_user_function_permission(self):
        dashboard = AccessFunction.objects.get(code="dashboard.view")
        UserFunctionOverride.objects.update_or_create(
            user=self.user, function=dashboard, defaults={"effect": "allow"}
        )
        self.assertTrue(has_function_access(self.user, "dashboard.view"))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        api = APIClient()
        api.force_authenticate(self.user)
        self.assertEqual(api.get(reverse("dashboard-api")).status_code, 200)

        owner = get_user_model().objects.create_user(username="role-super-admin", password="password-123")
        owner.employee_profile.role = Role.objects.get(slug="super-admin")
        owner.employee_profile.save(update_fields=["role", "updated_at"])
        self.client.force_login(owner)
        self.assertTrue(has_function_access(owner, "dashboard.view"))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_denied_navigation_and_project_column_are_not_rendered(self):
        UserFunctionOverride.objects.create(
            user=self.user, function=AccessFunction.objects.get(code="dashboard.view"), effect="deny"
        )
        UserFunctionOverride.objects.create(
            user=self.user, function=AccessFunction.objects.get(code="projects.column.cpi"), effect="deny"
        )
        response = self.client.get(reverse("projects"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f'href="{reverse("dashboard")}"')
        self.assertNotContains(response, "<th>CPI</th>", html=True)
        self.assertContains(response, "<th>Market</th>", html=True)
        self.assertNotContains(response, 'id="syncButton"')

    def test_project_export_and_cpi_filter_support_role_and_user_overrides(self):
        response = self.client.get(reverse("projects"))
        self.assertContains(response, 'id="exportProjects"')
        self.assertNotContains(response, 'id="cpiFilterTrigger"')

        UserFunctionOverride.objects.create(
            user=self.user,
            function=AccessFunction.objects.get(code="projects.export"),
            effect="deny",
        )
        UserFunctionOverride.objects.create(
            user=self.user,
            function=AccessFunction.objects.get(code="projects.filter.cpi"),
            effect="allow",
        )
        response = self.client.get(reverse("projects"))
        self.assertNotContains(response, 'id="exportProjects"')
        self.assertContains(response, 'id="cpiFilterTrigger"')

        api = APIClient()
        api.force_authenticate(self.user)
        self.assertEqual(api.get(reverse("survey-export")).status_code, 403)
        self.assertEqual(api.get(reverse("survey-list"), {"min_cpi": "1.00"}).status_code, 200)

    def test_each_project_filter_control_and_column_can_be_denied_individually(self):
        for code in (
            "projects.filter.country", "projects.filters.clear",
            "projects.control.pagination", "projects.column.market",
        ):
            UserFunctionOverride.objects.create(
                user=self.user,
                function=AccessFunction.objects.get(code=code),
                effect=UserFunctionOverride.Effect.DENY,
            )

        page = self.client.get(reverse("projects"))
        self.assertNotContains(page, 'id="countryLabel"')
        self.assertNotContains(page, 'id="clearFilters"')
        self.assertNotContains(page, 'aria-label="Survey pages"')
        self.assertNotContains(page, "<th>Market</th>", html=True)
        self.assertContains(page, 'id="searchInput"')

        api = APIClient()
        api.force_authenticate(self.user)
        self.assertEqual(api.get(reverse("survey-list"), {"country": "US"}).status_code, 403)
        self.assertEqual(api.get(reverse("survey-list"), {"search": "banking"}).status_code, 200)

    def test_code_catalog_restores_new_permissions_for_access_editor(self):
        AccessFunction.objects.filter(code="projects.export").delete()
        sync_access_function_catalog()
        function = AccessFunction.objects.get(code="projects.export")
        self.assertTrue(
            Role.objects.get(slug="employee").function_assignments.filter(function=function, allowed=True).exists()
        )

    def test_employee_cannot_call_protected_tracking_api(self):
        response = APIClient().get(reverse("survey-attempt-list"))
        self.assertIn(response.status_code, {401, 403})
        api = APIClient()
        api.force_authenticate(self.user)
        self.assertEqual(api.get(reverse("survey-attempt-list")).status_code, 403)

    def test_super_admin_can_crud_role_permissions(self):
        owner = get_user_model().objects.create_superuser(username="owner", password="password-123")
        api = APIClient()
        api.force_authenticate(owner)
        response = api.post(reverse("access-role-list"), {
            "name": "Recruiter", "slug": "recruiter", "rank": 15,
            "permission_codes": ["projects.view", "survey_links.copy"],
        }, format="json")
        self.assertEqual(response.status_code, 201)
        role = Role.objects.get(slug="recruiter")
        self.assertEqual(set(role.function_assignments.values_list("function__code", flat=True)), {"projects.view", "survey_links.copy"})

        self.client.force_login(owner)
        page = self.client.get(reverse("access-control"))
        self.assertContains(page, "Add user")
        self.assertContains(page, "userModal")
        self.assertContains(page, "projects.export")
        self.assertContains(page, "projects.filter.cpi")
        self.assertContains(page, "projects.column.completes")
        self.assertContains(page, "studies.filter.date")
        self.assertContains(page, "studies.filter.country")
        self.assertContains(page, "studies.column.cpi")
        self.assertContains(page, "studies.card.revenue")
        self.assertContains(page, "user_hits.card.completes")
        self.assertContains(page, "termination_reasons.card.quality")
        self.assertContains(page, "organization.card.client_grants")
        self.assertContains(page, "vendors.card.quantity")
        self.assertContains(page, "user_hits.column.completes")
        self.assertContains(page, "Select entire group")
        self.assertContains(page, "assigned functions")


class DelegatedVendorTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(username="owner", email="owner@example.test", password="password-123")
        self.vendor = get_user_model().objects.create_user(username="vendor@example.test", email="vendor@example.test", password="password-123")
        self.vendor.employee_profile.created_by = self.owner
        self.vendor.employee_profile.account_type = EmployeeProfile.AccountType.INTERNAL_VENDOR
        self.vendor.employee_profile.save()
        for code in ["permissions.view", "roles.view", "roles.create", "roles.update", "roles.delete", "users.view", "respondents.create", "users.update", "users.delete"]:
            UserFunctionOverride.objects.create(
                user=self.vendor, function=AccessFunction.objects.get(code=code), effect=UserFunctionOverride.Effect.ALLOW
            )
        self.api = APIClient()
        self.api.force_authenticate(self.vendor)

    def test_vendor_can_create_scoped_role_and_subordinate_user(self):
        self.client.force_login(self.vendor)
        page = self.client.get(reverse("access-control"))
        self.assertContains(page, "Add respondent")
        self.assertContains(page, reverse("access-control"))

        role_response = self.api.post(reverse("access-role-list"), {
            "name": "Vendor operator", "slug": "vendor-operator", "rank": 12,
            "permission_codes": ["projects.view", "survey_details.view"],
        }, format="json")
        self.assertEqual(role_response.status_code, 201)
        self.assertEqual(Role.objects.get(slug="vendor-operator").created_by, self.vendor)

        user_response = self.api.post(reverse("access-user-list"), {
            "first_name": "Nested", "last_name": "Employee", "email": "nested@example.test",
            "password": "password-123", "role": "employee", "account_type": "employee",
            "company_name": "Nested Respondent", "department": "Operations", "allow_codes": [], "deny_codes": [],
        }, format="json")
        self.assertEqual(user_response.status_code, 201)
        nested = get_user_model().objects.get(email="nested@example.test")
        self.assertEqual(nested.employee_profile.created_by, self.vendor)
        self.assertEqual(nested.employee_profile.account_type, EmployeeProfile.AccountType.EMPLOYEE)
        self.assertEqual(nested.employee_profile.company_name, "Nested Respondent")
        self.assertEqual(nested.employee_profile.department, "Operations")
        self.assertEqual(user_response.data["sub_branch"], "Operations")

    def test_vendor_cannot_delegate_permission_it_does_not_have(self):
        response = self.api.post(reverse("access-role-list"), {
            "name": "Escalated", "slug": "escalated", "rank": 99,
            "permission_codes": ["sync.run"],
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot delegate", str(response.data).lower())

    def test_vendor_cannot_see_sibling_vendor(self):
        sibling = get_user_model().objects.create_user(username="sibling", email="sibling@example.test")
        sibling.employee_profile.created_by = self.owner
        sibling.employee_profile.save()
        response = self.api.get(reverse("access-user-list"))
        self.assertEqual(response.status_code, 200)
        usernames = {item["username"] for item in response.data["results"]}
        self.assertNotIn("sibling", usernames)

    def test_external_vendor_cannot_create_subordinates_even_with_permission_override(self):
        self.vendor.employee_profile.account_type = EmployeeProfile.AccountType.EXTERNAL_VENDOR
        self.vendor.employee_profile.save(update_fields=["account_type", "updated_at"])
        self.vendor.employee_profile.role = Role.objects.get(slug="admin")
        self.vendor.employee_profile.save(update_fields=["role", "updated_at"])
        self.assertFalse(has_function_access(self.vendor, "users.create"))
        self.assertFalse(has_function_access(self.vendor, "roles.create"))
        self.client.force_login(self.vendor)
        self.assertIn(self.client.get(reverse("access-control")).status_code, {302, 403})
        response = self.api.post(reverse("access-user-list"), {
            "first_name": "Blocked", "last_name": "Respondent", "email": "blocked@example.test",
            "password": "password-123", "role": "employee", "account_type": "employee",
            "allow_codes": [], "deny_codes": [],
        }, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.api.post(reverse("access-role-list"), {
            "name": "Blocked role", "slug": "blocked-role", "rank": 10,
            "permission_codes": ["projects.view"],
        }, format="json").status_code, 403)

    def test_owner_created_vendor_types_receive_forced_safe_roles_and_policy(self):
        api = APIClient()
        api.force_authenticate(self.owner)
        internal_response = api.post(reverse("access-user-list"), {
            "first_name": "Internal", "last_name": "Partner", "email": "internal@example.test",
            "password": "password-123", "role": "employee", "account_type": "internal_vendor",
            "allow_codes": [], "deny_codes": [],
        }, format="json")
        external_response = api.post(reverse("access-user-list"), {
            "first_name": "External", "last_name": "Partner", "email": "external@example.test",
            "password": "password-123", "role": "admin", "account_type": "external_vendor",
            "allow_codes": [], "deny_codes": [],
        }, format="json")
        self.assertEqual(internal_response.status_code, 201)
        self.assertEqual(external_response.status_code, 201)
        internal = get_user_model().objects.get(email="internal@example.test")
        external = get_user_model().objects.get(email="external@example.test")
        self.assertEqual(internal.employee_profile.role.slug, "admin")
        self.assertEqual(external.employee_profile.role.slug, "external-vendor")
        self.assertEqual(internal.vendor_commercial_profile.delivery_mode, "panel")
        self.assertEqual(internal.vendor_commercial_profile.default_cpi_cut_percent, 0)
        self.assertEqual(external.vendor_commercial_profile.delivery_mode, "panel")
        self.assertFalse(has_function_access(external, "users.create"))

    def test_external_vendor_rejects_forbidden_explicit_override(self):
        api = APIClient()
        api.force_authenticate(self.owner)
        response = api.post(reverse("access-user-list"), {
            "first_name": "External", "last_name": "Blocked", "email": "external-blocked@example.test",
            "password": "password-123", "role": "admin", "account_type": "external_vendor",
            "allow_codes": ["users.create"], "deny_codes": [],
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("external suppliers cannot receive", str(response.data).lower())
