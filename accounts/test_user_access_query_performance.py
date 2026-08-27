from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from .access import effective_permission_codes
from .models import (
    AccessFunction,
    EmployeeProfile,
    Role,
    RoleFunctionPermission,
    UserFunctionOverride,
)


class UserAccessQueryPerformanceTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(
            username="access-perf-owner",
            email="access-perf-owner@example.test",
            password="test-password",
        )
        self.role = Role.objects.create(
            name="Access performance role",
            slug="access-performance-role",
            rank=37,
        )
        self.base = AccessFunction.objects.create(
            code="access_perf.base",
            name="Base permission",
            module="Access performance",
        )
        self.denied_assignment = AccessFunction.objects.create(
            code="access_perf.role_denied",
            name="Denied role assignment",
            module="Access performance",
        )
        self.allowed_override = AccessFunction.objects.create(
            code="access_perf.override_allowed",
            name="Allowed override",
            module="Access performance",
        )
        self.inactive_override = AccessFunction.objects.create(
            code="access_perf.inactive_override",
            name="Inactive override",
            module="Access performance",
            is_active=False,
        )
        self.users_view = AccessFunction.objects.get(code="users.view")
        self.organization_view = AccessFunction.objects.get(code="organization.view")
        RoleFunctionPermission.objects.bulk_create([
            RoleFunctionPermission(role=self.role, function=self.base, allowed=True),
            RoleFunctionPermission(
                role=self.role, function=self.denied_assignment, allowed=False
            ),
            RoleFunctionPermission(role=self.role, function=self.users_view, allowed=True),
            RoleFunctionPermission(
                role=self.role, function=self.organization_view, allowed=True
            ),
        ])
        self.users = [self._create_user(index) for index in range(20)]
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def _create_user(self, index, *, account_type=EmployeeProfile.AccountType.EMPLOYEE):
        user = get_user_model().objects.create_user(
            username=f"accessperfuser{index:03d}",
            first_name=f"Access {index:03d}",
            email=f"access-perf-{index:03d}@example.test",
        )
        profile = user.employee_profile
        profile.role = self.role
        profile.account_type = account_type
        profile.save(update_fields=["role", "account_type", "updated_at"])
        UserFunctionOverride.objects.bulk_create([
            UserFunctionOverride(
                user=user,
                function=self.allowed_override,
                effect=UserFunctionOverride.Effect.ALLOW,
            ),
            UserFunctionOverride(
                user=user,
                function=self.inactive_override,
                effect=UserFunctionOverride.Effect.ALLOW,
            ),
            UserFunctionOverride(
                user=user,
                function=self.base,
                effect=UserFunctionOverride.Effect.DENY,
            ),
        ])
        return user

    @staticmethod
    def _legacy_access_output(user):
        return {
            "allowed_overrides": list(
                user.function_overrides.filter(
                    effect=UserFunctionOverride.Effect.ALLOW
                ).values_list("function__code", flat=True)
            ),
            "denied_overrides": list(
                user.function_overrides.filter(
                    effect=UserFunctionOverride.Effect.DENY
                ).values_list("function__code", flat=True)
            ),
            "effective_permissions": sorted(effective_permission_codes(user)),
        }

    def _list_users(self, search):
        with CaptureQueriesContext(connection) as queries:
            response = self.api.get(reverse("access-user-list"), {"search": search})
        self.assertEqual(response.status_code, 200)
        return response.data["results"], len(queries)

    def _list_roles(self, search):
        with CaptureQueriesContext(connection) as queries:
            response = self.api.get(reverse("access-role-list"), {"search": search})
        self.assertEqual(response.status_code, 200)
        return response.data["results"], len(queries)

    def test_list_query_count_is_flat_and_access_output_matches_legacy_rules(self):
        expected = {
            user.username: self._legacy_access_output(user)
            for user in self.users
        }

        one_result, one_queries = self._list_users("accessperfuser000")
        many_results, many_queries = self._list_users("accessperfuser")

        self.assertEqual(len(one_result), 1)
        self.assertEqual(len(many_results), 20)
        self.assertEqual(many_queries, one_queries)
        self.assertLessEqual(many_queries, 8)
        for item in many_results:
            self.assertEqual(
                {
                    "allowed_overrides": item["allowed_overrides"],
                    "denied_overrides": item["denied_overrides"],
                    "effective_permissions": item["effective_permissions"],
                },
                expected[item["username"]],
            )

    def test_external_supplier_effective_permissions_keep_existing_restrictions(self):
        external = self._create_user(
            100,
            account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR,
        )
        expected = self._legacy_access_output(external)

        results, _query_count = self._list_users(external.username)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["effective_permissions"], expected["effective_permissions"])
        self.assertNotIn("users.view", results[0]["effective_permissions"])
        self.assertFalse(
            any(code.startswith("organization.") for code in results[0]["effective_permissions"])
        )
        self.assertIn(
            self.inactive_override.code,
            results[0]["allowed_overrides"],
        )
        self.assertNotIn(
            self.inactive_override.code,
            results[0]["effective_permissions"],
        )

    def test_inactive_users_and_superusers_have_no_effective_permissions(self):
        inactive_user = self.users[0]
        inactive_user.is_active = False
        inactive_user.save(update_fields=["is_active"])
        inactive_owner = get_user_model().objects.create_superuser(
            username="accessperfinactiveroot",
            email="access-perf-inactive-root@example.test",
            password="test-password",
        )
        inactive_owner.is_active = False
        inactive_owner.save(update_fields=["is_active"])

        user_results, _ = self._list_users(inactive_user.username)
        owner_results, _ = self._list_users(inactive_owner.username)

        self.assertEqual(user_results[0]["effective_permissions"], [])
        self.assertEqual(owner_results[0]["effective_permissions"], [])
        self.assertTrue(user_results[0]["allowed_overrides"])

    def test_patch_response_uses_fresh_override_state(self):
        user = self.users[0]

        response = self.api.patch(
            reverse("access-user-detail", args=[user.pk]),
            {
                "allow_codes": [self.base.code],
                "deny_codes": [self.allowed_override.code],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        refreshed = get_user_model().objects.get(pk=user.pk)
        expected = self._legacy_access_output(refreshed)
        self.assertEqual(response.data["allowed_overrides"], expected["allowed_overrides"])
        self.assertEqual(response.data["denied_overrides"], expected["denied_overrides"])
        self.assertEqual(response.data["effective_permissions"], expected["effective_permissions"])

    def test_role_list_consumes_existing_assignment_prefetch_with_flat_queries(self):
        roles = []
        assignments = []
        for index in range(20):
            role = Role.objects.create(
                name=f"Access perf role {index:03d}",
                slug=f"accessperfrole{index:03d}",
                rank=50 + index,
            )
            roles.append(role)
            assignments.extend([
                RoleFunctionPermission(role=role, function=self.base, allowed=True),
                RoleFunctionPermission(
                    role=role, function=self.denied_assignment, allowed=False
                ),
                RoleFunctionPermission(
                    role=role, function=self.inactive_override, allowed=True
                ),
            ])
        RoleFunctionPermission.objects.bulk_create(assignments)

        one_result, one_queries = self._list_roles("accessperfrole000")
        many_results, many_queries = self._list_roles("accessperfrole")

        self.assertEqual(len(one_result), 1)
        self.assertEqual(len(many_results), 20)
        self.assertEqual(many_queries, one_queries)
        self.assertLessEqual(many_queries, 6)
        self.assertTrue(all(
            item["effective_permission_codes"] == [self.base.code]
            for item in many_results
        ))

    def test_role_patch_response_does_not_reuse_stale_assignment_prefetch(self):
        response = self.api.patch(
            reverse("access-role-detail", args=[self.role.slug]),
            {"permission_codes": [self.allowed_override.code]},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["effective_permission_codes"],
            [self.allowed_override.code],
        )
