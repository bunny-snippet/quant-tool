from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from . import test_partner_dashboard as fixtures
from .dashboard import _invoice_totals, _monthly_finance, _performance_series, dashboard_range_window
from .models import SurveyAttempt, FinalIDStatus
from .partner_dashboard import main_dashboard_tables


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"], CACHE_ENABLED=False)
class DashboardConsolidationTests(TestCase):
    setUp = fixtures.PartnerDashboardTests.setUp
    attempt = fixtures.PartnerDashboardTests.attempt

    def test_invoice_month_not_journey_month_and_later_decision(self):
        window = dashboard_range_window('date', now=self.now + timedelta(days=35), selected_date='2026-10-01')
        with self.assertNumQueries(1):
            totals = _invoice_totals(SurveyAttempt.objects.all(), window, self.admin)
        self.assertEqual(totals, {'2026-10-01': Decimal('2.50')})
        chart = _monthly_finance(SurveyAttempt.objects.none(), totals, window, self.admin, {'revenue':True})
        self.assertEqual(chart['points'][0]['hits'], 0)
        self.assertEqual(chart['points'][0]['invoiced_revenue'], Decimal('2.50'))
        september = dashboard_range_window('month', now=self.now)
        self.assertEqual(_invoice_totals(SurveyAttempt.objects.all(), september, self.admin), {})
        FinalIDStatus.objects.filter(attempt=self.attempts[1]).update(status='accepted')
        self.assertEqual(_invoice_totals(SurveyAttempt.objects.all(), window, self.admin)['2026-10-01'],Decimal('5.00'))
        self.assertEqual(_invoice_totals(SurveyAttempt.objects.filter(platform_user=self.admin), window, self.admin),{})

    def test_rejections_and_partner_tables_use_latest_decisions(self):
        qs=SurveyAttempt.objects.all()
        points=_performance_series(qs, dashboard_range_window('month',now=self.now))
        self.assertEqual(sum(p['rejected'] for p in points),1)
        with self.assertNumQueries(2):
            tables=main_dashboard_tables(qs,self.admin,3)
        for key in ('client','supplier'):
            self.assertEqual([(r['completes'],r['accepted'],r['rejected'],r['share']) for r in tables[key]],[(3,1,1,100)])
            self.assertEqual(tables[key][0]['rejection_percentage'],33.33)
        with patch('surveys.partner_dashboard.effective_permission_codes',return_value=set()), self.assertNumQueries(0):
            self.assertEqual(main_dashboard_tables(qs,self.worker,3),{'client':None,'supplier':None})

    def test_global_client_filters_summary_charts_tables_and_invoice_card(self):
        self.attempt(10,survey=self.other_survey,status='1',initiated_at=self.now-timedelta(days=60))
        api=APIClient(); api.force_authenticate(self.admin)
        with patch('surveys.views.timezone.now',return_value=self.now), patch('surveys.partner_report_cache.cached_report_payload',side_effect=lambda ns,req,factory:factory()):
            response=api.get(reverse('dashboard-api'),{'range':'month','client':self.other.pk})
            self.assertEqual(response.status_code,200)
            p=response.data
            self.assertEqual(p['summary']['hits'],0)
            self.assertEqual(p['summary']['invoiced_revenue'],0)
            self.assertEqual(p['partner_tables']['client'],[])
            self.assertEqual(sum(r['hits'] for r in p['traffic_chart']['points']),0)
            response=api.get(reverse('dashboard-api'),{'range':'date','date':'2026-09-07','client':self.client_record.pk})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.data['summary']['hits'],4)
            self.assertEqual(api.get(reverse('dashboard-api'),{'client':999999}).status_code,400)

    def test_calendar_date_bounds_validation(self):
        window=dashboard_range_window('date',now=self.now,selected_date='2026-09-06')
        self.assertEqual(window['start'].hour,0)
        self.assertEqual(window['end']-window['start'],timedelta(days=1))
        self.assertEqual(len(window['buckets']),12)
        for invalid in ['invalid','2026-09-40','2027-01-01',None]:
            with self.assertRaises(ValueError): dashboard_range_window('date',now=self.now,selected_date=invalid)

    def test_reports_permission_routing_and_card_removal(self):
        self.client.force_login(self.admin)
        self.assertRedirects(self.client.get(reverse('reports')),reverse('reports-traffic'),fetch_redirect_response=False)
        for name in ['reports-traffic','reports-term']:
            page=self.client.get(reverse(name))
            self.assertEqual(page.status_code,200)
            self.assertContains(page,'aria-label="Reports"')
            self.assertContains(page,'data-report-panel=')
            self.assertContains(page,'surveys/reports.css')
            self.assertContains(page,'surveys/reports.js')
            self.assertNotContains(page,'id="studyMetricRevenue"')
            self.assertNotContains(page,'id="studyMetricInvoicedRevenue"')
        with patch('surveys.views.has_function_access',side_effect=lambda user,code:code=='termination_reasons.view'):
            self.assertRedirects(self.client.get(reverse('reports')),reverse('reports-term'),fetch_redirect_response=False)

    def test_activity_data_is_selected_in_initial_html_without_hits_flash(self):
        self.client.force_login(self.admin)
        page=self.client.get(reverse('user-hits'),{'tab':'user-data'})
        self.assertEqual(page.status_code,200)
        self.assertContains(page,'surveys/reports.css')
        self.assertContains(page,'data-activity-panel="user-hits" hidden')
        self.assertContains(page,'data-activity-panel="user-data">')
        self.assertContains(page,'<h1>User Data</h1>')
        self.assertContains(page,'data-activity-tab="suppliers"')
        self.assertContains(page,'Supplier Activity')

    def test_supplier_tab_and_reconciliation_keep_existing_permissions(self):
        self.client.force_login(self.admin)
        page=self.client.get(reverse('vendor-management'))
        self.assertEqual(page.status_code,200)
        self.assertContains(page,'data-activity-panel="suppliers"')
        self.assertContains(page,'data-activity-script="suppliers"')
        page=self.client.get(reverse('reports-reconciliation'))
        self.assertEqual(page.status_code,200)
        self.assertContains(page,'Coming soon')
        with patch('surveys.views.has_function_access',return_value=False):
            self.assertEqual(self.client.get(reverse('reports-reconciliation')).status_code,403)
        page=self.client.get(reverse('dashboard'))
        self.assertNotContains(page,'id="dashboardTopSuppliers"')
        self.assertContains(page,'Rejection %')
