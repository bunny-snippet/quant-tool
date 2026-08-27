import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from .access import (
    _activity_visible_user_ids_uncached,
    activity_visible_user_ids,
    effective_permission_codes,
    has_function_access,
    subordinate_user_ids,
)
from .models import AccessFunction, EmployeeProfile, Role, RoleFunctionPermission
from .profile_context import employee_profile_for_user
from .request_cache import RequestAccessCacheMiddleware, request_cached
from vendors.middleware import VendorPanelAccessMiddleware
from vendors.models import VendorCommercialProfile


class RequestAccessDatabaseCacheTests(TestCase):
    def setUp(self):
        self.role = Role.objects.create(
            name="Request cache role",
            slug="request-cache-role",
            rank=10,
            is_active=True,
        )
        self.function = AccessFunction.objects.create(
            code="request_cache.probe",
            name="Request cache probe",
            module="Tests",
            is_active=True,
        )
        self.assignment = RoleFunctionPermission.objects.create(
            role=self.role,
            function=self.function,
            allowed=True,
        )
        self.user = get_user_model().objects.create_user(
            username="request-cache-user",
            password="test-password",
        )
        EmployeeProfile.objects.filter(user=self.user).update(role=self.role)
        self.child = get_user_model().objects.create_user(
            username="request-cache-child",
            password="test-password",
        )
        EmployeeProfile.objects.filter(user=self.child).update(created_by=self.user)

    @staticmethod
    def _run_request(user, response):
        request = SimpleNamespace(user=user)
        return RequestAccessCacheMiddleware(response)(request)

    def test_profile_permissions_and_hierarchy_resolve_once_per_request(self):
        observed = {}

        def response(request):
            with CaptureQueriesContext(connection) as captured:
                first_profile = employee_profile_for_user(request.user)
                after_first_profile = len(captured)
                second_profile = employee_profile_for_user(request.user)
                after_second_profile = len(captured)

                first_codes = effective_permission_codes(request.user)
                after_first_permissions = len(captured)
                # Returned sets are defensive copies, not the cached snapshot.
                first_codes.discard(self.function.code)
                second_codes = effective_permission_codes(request.user)
                after_second_permissions = len(captured)

                first_subordinates = subordinate_user_ids(request.user)
                after_first_hierarchy = len(captured)
                first_subordinates.clear()
                second_subordinates = subordinate_user_ids(request.user)
                after_second_hierarchy = len(captured)

                with patch(
                    "accounts.access._activity_visible_user_ids_uncached",
                    wraps=_activity_visible_user_ids_uncached,
                ) as resolver:
                    activity_visible_user_ids(request.user)
                    activity_visible_user_ids(request.user)
                    observed["activity_resolutions"] = resolver.call_count

            observed.update({
                "same_profile": first_profile is second_profile,
                "profile_first_queries": after_first_profile,
                "profile_second_queries": after_second_profile - after_first_profile,
                "permission_first_queries": after_first_permissions - after_second_profile,
                "permission_second_queries": after_second_permissions - after_first_permissions,
                "hierarchy_first_queries": after_first_hierarchy - after_second_permissions,
                "hierarchy_second_queries": after_second_hierarchy - after_first_hierarchy,
                "second_codes": second_codes,
                "second_subordinates": second_subordinates,
            })
            return object()

        self._run_request(self.user, response)

        self.assertTrue(observed["same_profile"])
        self.assertGreater(observed["profile_first_queries"], 0)
        self.assertEqual(observed["profile_second_queries"], 0)
        self.assertGreater(observed["permission_first_queries"], 0)
        self.assertEqual(observed["permission_second_queries"], 0)
        self.assertGreater(observed["hierarchy_first_queries"], 0)
        self.assertEqual(observed["hierarchy_second_queries"], 0)
        self.assertEqual(observed["activity_resolutions"], 1)
        self.assertIn(self.function.code, observed["second_codes"])
        self.assertIn(self.child.pk, observed["second_subordinates"])

    def test_revocation_is_visible_on_the_next_request(self):
        before_queries = []
        after_queries = []

        def before_response(request):
            with CaptureQueriesContext(connection) as captured:
                allowed = has_function_access(request.user, self.function.code)
                self.assertTrue(has_function_access(request.user, self.function.code))
            before_queries.append(len(captured))
            return allowed

        self.assertTrue(self._run_request(self.user, before_response))
        self.assignment.delete()

        def after_response(request):
            with CaptureQueriesContext(connection) as captured:
                allowed = has_function_access(request.user, self.function.code)
                self.assertFalse(has_function_access(request.user, self.function.code))
            after_queries.append(len(captured))
            return allowed

        self.assertFalse(self._run_request(self.user, after_response))
        self.assertGreater(before_queries[0], 0)
        self.assertGreater(after_queries[0], 0)

    def test_repeated_permission_checks_reduce_database_work(self):
        def resolve_repeatedly(_request):
            with CaptureQueriesContext(connection) as captured:
                results = [
                    has_function_access(self.user, self.function.code)
                    for _index in range(12)
                ]
            return results, len(captured)

        uncached_results, uncached_queries = resolve_repeatedly(SimpleNamespace())
        cached_results, cached_queries = self._run_request(self.user, resolve_repeatedly)

        self.assertTrue(all(uncached_results))
        self.assertEqual(cached_results, uncached_results)
        self.assertGreater(uncached_queries, cached_queries)
        self.assertLessEqual(cached_queries * 6, uncached_queries)


class RequestAccessIsolationTests(SimpleTestCase):
    def test_middleware_is_immediately_after_authentication(self):
        authentication = settings.MIDDLEWARE.index(
            "django.contrib.auth.middleware.AuthenticationMiddleware"
        )
        request_cache = settings.MIDDLEWARE.index(
            "accounts.request_cache.RequestAccessCacheMiddleware"
        )
        self.assertEqual(request_cache, authentication + 1)

    def test_cache_is_reset_between_requests_and_never_global(self):
        calls = []

        def factory():
            calls.append(len(calls) + 1)
            return calls[-1]

        # Outside HTTP middleware there is deliberately no caching.
        self.assertEqual(request_cached(("probe",), factory), 1)
        self.assertEqual(request_cached(("probe",), factory), 2)

        middleware = RequestAccessCacheMiddleware(
            lambda request: (
                request_cached(("probe",), factory),
                request_cached(("probe",), factory),
            )
        )
        self.assertEqual(middleware(SimpleNamespace()), (3, 3))
        self.assertEqual(middleware(SimpleNamespace()), (4, 4))

        # The second request was reset too; nothing leaked to ambient context.
        self.assertEqual(request_cached(("probe",), factory), 5)

    def test_concurrent_async_requests_have_isolated_caches(self):
        async def exercise():
            both_entered = asyncio.Event()
            entry_lock = asyncio.Lock()
            entered = 0

            async def response(request):
                nonlocal entered
                first = request_cached(("shared-key",), lambda: request.value)
                async with entry_lock:
                    entered += 1
                    if entered == 2:
                        both_entered.set()
                await both_entered.wait()
                await asyncio.sleep(0)
                second = request_cached(("shared-key",), lambda: "leaked")
                return first, second

            middleware = RequestAccessCacheMiddleware(response)
            return await asyncio.gather(
                middleware(SimpleNamespace(value="alpha")),
                middleware(SimpleNamespace(value="beta")),
            )

        alpha, beta = asyncio.run(exercise())
        self.assertEqual(alpha, ("alpha", "alpha"))
        self.assertEqual(beta, ("beta", "beta"))


class VendorPanelMiddlewareRequestCacheTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="request-cache-external-vendor",
            password="test-password",
        )
        EmployeeProfile.objects.filter(user=self.user).update(
            account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR,
        )
        self.policy = VendorCommercialProfile.objects.create(
            vendor=self.user,
            delivery_mode=VendorCommercialProfile.DeliveryMode.PANEL,
        )

    def _run(self, path, response):
        request = SimpleNamespace(
            user=self.user,
            path=path,
            get_full_path=lambda: path,
        )
        stack = RequestAccessCacheMiddleware(VendorPanelAccessMiddleware(response))
        with patch("vendors.middleware.logout") as mocked_logout:
            result = stack(request)
        return result, mocked_logout

    def test_panel_enabled_external_vendor_reaches_the_view(self):
        sentinel = object()
        response, mocked_logout = self._run("/projects/", lambda request: sentinel)
        self.assertIs(response, sentinel)
        mocked_logout.assert_not_called()

    def test_api_only_external_vendor_is_logged_out_and_redirected_from_panel(self):
        self.policy.delivery_mode = VendorCommercialProfile.DeliveryMode.API
        self.policy.save(update_fields=["delivery_mode", "updated_at"])
        response, mocked_logout = self._run(
            "/projects/",
            lambda request: self.fail("Panel view must not execute for an API-only supplier."),
        )
        mocked_logout.assert_called_once()
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_api_only_external_vendor_api_request_continues_after_logout(self):
        self.policy.delivery_mode = VendorCommercialProfile.DeliveryMode.API
        self.policy.save(update_fields=["delivery_mode", "updated_at"])
        sentinel = object()
        response, mocked_logout = self._run("/api/v1/surveys/", lambda request: sentinel)
        mocked_logout.assert_called_once()
        self.assertIs(response, sentinel)
