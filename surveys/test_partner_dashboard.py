import json
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase, SimpleTestCase, RequestFactory, override_settings
from django.urls import reverse

from accounts.models import EmployeeProfile
from vendors.models import Client, OrganizationUnit
from .models import Survey, SurveyAttempt, FinalIDStatus, FinalIDUpload
from .partner_dashboard import METRICS, build_partner_payload, component_access, partner_dashboard


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PartnerDashboardTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(username="partner-admin", password="test-only")
        self.worker = get_user_model().objects.create_user(username="partner-worker")
        self.vendor = get_user_model().objects.create_user(username="supplier-one", first_name="Supplier", last_name="One")
        self.branch = OrganizationUnit.objects.create(workspace_owner=self.admin, name="Branch one", code="b1", unit_type="branch", created_by=self.admin)
        self.sub = OrganizationUnit.objects.create(workspace_owner=self.admin, name="Sub one", code="sub1", unit_type="sub_branch", parent=self.branch, created_by=self.admin)
        self.shift = OrganizationUnit.objects.create(workspace_owner=self.admin, name="Shift one", code="shift1", unit_type="shift", parent=self.sub, created_by=self.admin)
        EmployeeProfile.objects.filter(user=self.worker).update(organization_unit=self.shift)
        self.client_record = Client.objects.create(name="Example Client", code="partner-client")
        self.other = Client.objects.create(name="Other Client", code="partner-other")
        self.survey = Survey.objects.create(client=self.client_record, source_id=9001, source_key="9001", country_code="US", country="United States")
        self.other_survey = Survey.objects.create(client=self.other, source_id=9002, source_key="9002", country_code="CA", country="Canada")
        self.now = datetime(2026, 9, 7, 12, tzinfo=dt_timezone.utc)
        self.attempts = [self.attempt(i, status="1" if i < 3 else "2") for i in range(4)]
        upload = FinalIDUpload.objects.create(client=self.client_record, accounting_month=date(2026,10,1), decision="accepted", original_filename="test.csv", file_sha256="a"*64, uploaded_by=self.admin)
        for i, status in enumerate(["accepted", "rejected"]):
            FinalIDStatus.objects.create(attempt=self.attempts[i], client=self.client_record, accounting_month=date(2026,10,1), status=status, upload=upload)
        self.access = {key: True for key in (*METRICS, "trend", "review", "table", "date", "partner", "segment")}

    def attempt(self, index, **kwargs):
        defaults = dict(rid=f"Partner{index:03}", survey=self.survey, platform_user=self.worker, vendor=self.vendor, user_id=str(self.worker.pk), initiated_at=self.now-timedelta(hours=1), source_cpi_snapshot=Decimal("2.50"), cpi_currency_snapshot="USD")
        defaults.update(kwargs)
        return SurveyAttempt.objects.create(**defaults)

    def payload(self, section="client", params=None, user=None, access=None):
        return build_partner_payload(user or self.admin, params or {}, section, access or self.access, now=self.now)

    def test_totals_latest_decisions_and_invoice_month_independent(self):
        p = self.payload()
        self.assertEqual([p["summary"][k] for k in ("hits","completes","accepted","rejected","pending")], [4,3,1,1,1])
        self.assertEqual(p["summary"]["revenue"], Decimal("7.50"))
        self.assertEqual(p["summary"]["rpc"], Decimal("1.88"))
        self.assertEqual(sum(x["hits"] for x in p["trend"]),4)
        self.assertEqual(sum(x["completes"] for x in p["trend"]),3)
        FinalIDStatus.objects.filter(attempt=self.attempts[1]).update(status="accepted")
        self.assertEqual(self.payload()["summary"]["accepted"],2)

    def test_global_client_country_and_supplier_branch_filters(self):
        self.attempt(5, survey=self.other_survey, vendor=None, platform_user=self.admin, status="1")
        for section, params in [("client", {"partner":str(self.client_record.pk), "segment":"US"}), ("supplier", {"partner":str(self.vendor.pk), "segment":str(self.branch.pk)})]:
            p = self.payload(section, params)
            self.assertEqual(p["summary"]["hits"],4)
            self.assertEqual(sum(r["hits"] for r in p["rows"]),4)
            self.assertEqual(sum(r["hits"] for r in p["trend"]),4)
        self.assertEqual(self.payload("supplier")["rows"][1]["segment_name"], "Branch one")

    def test_invisible_partner_never_expands_visibility(self):
        self.attempt(5, survey=self.other_survey, platform_user=self.admin, status="1")
        with patch("surveys.dashboard.activity_visible_user_ids", return_value={self.worker.pk}):
            p = self.payload(user=self.worker)
            self.assertEqual(p["summary"]["hits"],4)
            self.assertNotIn("Other Client", str(p["options"]))
            self.assertEqual(self.payload(user=self.worker, params={"partner":str(self.other.pk)})["summary"]["hits"],0)

    def test_money_hidden_everywhere_and_mixed_currencies_not_summed(self):
        self.attempt(5, status="1", cpi_currency_snapshot="EUR")
        self.assertIsNone(self.payload()["summary"]["revenue"])
        access = dict(self.access, revenue=False, rpc=False, completes=False, accepted=False, rejected=False, pending=False, share=False)
        p = self.payload(access=access)
        for row in [p["summary"],p["previous"],*p["rows"]]:
            for key in ("revenue","rpc","completes","accepted","rejected","pending","share"):
                self.assertIsNone(row[key])
        self.assertTrue(all(r["completes"] is None for r in p["trend"]))

    def test_missing_cpi_does_not_fallback_to_current_survey_price(self):
        SurveyAttempt.objects.filter(pk=self.attempts[0].pk).update(source_cpi_snapshot=None)
        self.assertIsNone(self.payload()["summary"]["revenue"])

    def test_current_month_comparison_uses_same_previous_month_elapsed_time(self):
        self.attempt(5, status="1", initiated_at=datetime(2026,8,3,12,tzinfo=dt_timezone.utc))
        self.attempt(6, status="1", initiated_at=datetime(2026,8,20,12,tzinfo=dt_timezone.utc))
        p = self.payload(params={"range":"month"})
        self.assertEqual(p["previous"]["hits"],1)
        self.assertEqual(p["comparison_label"],"Previous month to date")

    def test_half_open_window_no_duplicate_boundary(self):
        self.attempt(5, status="1", initiated_at=self.now)
        self.attempt(6, status="1", initiated_at=self.now-timedelta(days=7))
        self.assertEqual(self.payload()["summary"]["hits"],5)
        self.assertEqual(self.payload()["previous"]["hits"],0)

    def test_empty_and_bad_filters(self):
        p = self.payload(params={"partner":"999999"})
        self.assertEqual(p["summary"]["hits"],0)
        self.assertEqual(p["summary"]["rpc"],0)
        for params in [{"range":"all"},{"partner":"x"},{"segment":"x"}]:
            with self.assertRaises(ValueError):
                self.payload("supplier",params)
        for params in [{"partner":"1"},{"segment":"US"},{"range":"month"}]:
            with self.assertRaises(PermissionDenied):
                self.payload(params=params, access=dict(self.access,partner=False,segment=False,date=False))

    def test_queries_constant_per_partner_and_bucket(self):
        # Visibility is superuser-scoped; no individual attempt objects are loaded.
        with self.assertNumQueries(5):
            self.payload(params={"range":"month"})

    @override_settings(DEBUG=False, STORAGES={"default":{"BACKEND":"django.core.files.storage.FileSystemStorage"},"staticfiles":{"BACKEND":"django.contrib.staticfiles.storage.StaticFilesStorage"}})
    def test_production_pages_authenticated_and_no_sample_values(self):
        for section in ("client","supplier"):
            request=RequestFactory().get(reverse(section+"-dashboard"))
            request.user=self.admin
            response=partner_dashboard(request,section)
            self.assertEqual(response.status_code,200)
            self.assertIn(b'partner_dashboard.js',response.content)
            self.assertNotIn(b'Sample data',response.content)
            self.assertIn(b'Client Dashboard',response.content)
            self.assertIn(b'Supplier Dashboard',response.content)

    def test_page_permission_and_anonymous_redirect(self):
        from django.contrib.auth.models import AnonymousUser
        request=RequestFactory().get('/dashboard/client/')
        request.user=AnonymousUser()
        self.assertEqual(partner_dashboard(request,'client').status_code,302)
        request.user=self.worker
        with patch('accounts.access.has_function_access',return_value=False), self.assertRaises(PermissionDenied):
            partner_dashboard(request,'client')

    def test_component_mapping_defaults_deny(self):
        with patch('surveys.partner_dashboard.effective_permission_codes',return_value={'dashboard.view'}):
            self.assertFalse(any(component_access(self.worker,'supplier').values()))

    def test_overview_route_unchanged(self):
        self.assertEqual(reverse('dashboard'),'/dashboard/')

    def test_json_route_serializes_metrics_and_rejects_invalid_range(self):
        request = RequestFactory().get('/dashboard/client/', {'format':'json','range':'month'})
        request.user = self.admin
        with patch('surveys.partner_dashboard.cached_report_payload', side_effect=lambda namespace, req, factory, **kw: factory()):
            response = partner_dashboard(request, 'client')
            self.assertEqual(response.status_code, 200)
            data = json.loads(response.content)
            self.assertIn('summary', data)
            self.assertIn('no-store', response['Cache-Control'])
            request.GET = request.GET.copy()
            request.GET['range'] = 'invalid'
            self.assertEqual(partner_dashboard(request, 'client').status_code, 400)

    def test_denied_panels_and_options_have_no_data(self):
        payload = self.payload(access=dict(self.access, table=False, trend=False, partner=False, segment=False))
        self.assertEqual(payload['rows'], [])
        self.assertEqual(payload['trend'], [])
        self.assertEqual(payload['options'], {'partners':[], 'segments':[]})


@override_settings(CACHES={"default":{"BACKEND":"django.core.cache.backends.locmem.LocMemCache","LOCATION":"partner-tests-only"}})
class PartnerReportCacheTests(SimpleTestCase):
    def test_cache_scope_changes_with_permissions_visibility_and_user(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from django.core.cache import cache
        from .partner_report_cache import cached_report_payload
        cache.clear()
        request = RequestFactory().get('/dashboard/client/', {'format':'json'})
        request.user = SimpleNamespace(pk=100, is_superuser=False)
        factory = Mock(side_effect=lambda: {'build': factory.call_count})
        with patch('surveys.partner_report_cache.employee_profile_for_user', return_value=None), patch('surveys.partner_report_cache.effective_permission_codes', return_value={'dashboard.view'}) as permissions, patch('surveys.partner_report_cache.activity_visible_user_ids', return_value={100}) as visible:
            call = lambda: cached_report_payload('test', request, factory, extra_scope={'section':'client'})
            self.assertEqual(call(), call())
            self.assertEqual(factory.call_count, 1)
            permissions.return_value = {'dashboard.view','dashboard.card.revenue'}
            call()
            self.assertEqual(factory.call_count, 2)
            visible.return_value = {100,101}
            call()
            self.assertEqual(factory.call_count, 3)
            request.user.pk = 200
            call()
            self.assertEqual(factory.call_count, 4)
        cache.clear()
