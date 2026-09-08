from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.db import connection
from accounts.models import EmployeeProfile, Role
from accounts.serializers import RoleSerializer
from .test_partner_dashboard import PartnerDashboardTests
from .partner_dashboard import build_partner_payload, partner_dashboard
from .performer_policy import configured_performers
from .dashboard import dashboard_attempts, dashboard_range_window


class DashboardControlsTests(PartnerDashboardTests):
    def test_today_previous_day_exact_ist_boundaries(self):
        now=datetime(2026,9,8,15,30,tzinfo=ZoneInfo('Asia/Kolkata'))
        today=now.replace(hour=0,minute=0)
        self.attempt(20,status='1',initiated_at=today)
        self.attempt(21,status='1',initiated_at=today-timedelta(seconds=1))
        self.attempt(22,status='1',initiated_at=today-timedelta(days=1))
        self.attempt(23,status='1',initiated_at=now)
        p=build_partner_payload(self.admin,{},'client',self.access,now=now)
        self.assertEqual(p['start'],today)
        self.assertEqual(p['comparison_start'],today-timedelta(days=1))
        self.assertEqual(p['comparison_end'],today)
        self.assertEqual(p['summary']['hits'],1)
        self.assertEqual(p['previous']['hits'],6)  # four fixture attempts + two boundaries
        self.assertEqual(sum(r['hits'] for r in p['trend']),1)

    def test_all_partner_ranges_and_country_map_conserve_counts(self):
        for range_key in ['today','48h','7d','15d','21d','28d','month','3m','6m','fy']:
            p=self.payload(params={'range':range_key},access=dict(self.access,world_map=True))
            self.assertEqual(sum(c['completes'] for c in p['countries']),p['summary']['completes'])
            self.assertEqual(sum(c['completes'] for row in p['countries'] for c in row['clients']),p['summary']['completes'])
            self.assertEqual(sum(c['hits'] for c in p['trend']),p['summary']['hits'])
        self.assertEqual(self.payload(access=dict(self.access,world_map=False))['countries'],[])
        self.assertEqual(self.payload(access=dict(self.access,world_map=True,completes=False))['countries'],[])

    def test_partner_pages_independent_authorization(self):
        for section in ['client','supplier']:
            request=RequestFactory().get('/dashboard/'+section+'/',{'format':'json'})
            request.user=self.worker
            with patch('surveys.partner_dashboard.has_function_access',side_effect=lambda u,code:code=='dashboard.client.view'):
                if section=='supplier':
                    with self.assertRaises(PermissionDenied):partner_dashboard(request,section)
                else:
                    with patch('surveys.partner_dashboard.cached_report_payload',return_value={'ok':True}):
                        self.assertEqual(partner_dashboard(request,section).status_code,200)

    def test_team_policy_exposes_only_team_names_completes_not_other_traffic(self):
        role=Role.objects.create(name='Team dashboard',slug='test-team-dash',dashboard_performers={'mode':'team'})
        EmployeeProfile.objects.filter(user=self.worker).update(role=role)
        viewer=get_user_model().objects.select_related('employee_profile__role','employee_profile__organization_unit').get(pk=self.worker.pk)
        peer=get_user_model().objects.create_user(username='peer')
        EmployeeProfile.objects.filter(user=peer).update(organization_unit=self.shift)
        self.attempt(30,platform_user=peer,status='1')
        self.attempt(31,platform_user=self.admin,status='1')
        window=dashboard_range_window('today',now=self.now)
        rows=configured_performers(dashboard_attempts(viewer,{}),viewer,{'revenue':True},3,window)
        self.assertEqual({r['name'] for r in rows},{'partner-worker','peer'})
        self.assertTrue(all(set(r)=={'name','branch_name','completes'} for r in rows))
        self.assertFalse(dashboard_attempts(viewer,{}).filter(platform_user=peer).exists())

    def test_performer_config_owner_only_and_validates_selection(self):
        request=RequestFactory().post('/roles/')
        request.user=self.worker
        serializer=RoleSerializer(data={'name':'x','slug':'x','dashboard_performers':{'mode':'team'}},context={'request':request})
        self.assertFalse(serializer.is_valid())
        request.user=self.admin
        serializer=RoleSerializer(data={'name':'x','slug':'x','dashboard_performers':{'mode':'users','supplier_ids':[999999]}},context={'request':request})
        self.assertFalse(serializer.is_valid())
        serializer=RoleSerializer(data={'name':'x','slug':'x','dashboard_performers':{'mode':'team'}},context={'request':request})
        self.assertTrue(serializer.is_valid(),serializer.errors)

    def test_supplier_summaries_do_not_carry_unused_dimension_joins(self):
        with CaptureQueriesContext(connection) as queries:
            self.payload('supplier',params={'range':'month'})
        summary=next(q['sql'] for q in queries if 'current_hits' in q['sql'])
        self.assertNotIn('vendors_organizationunit',summary)
        self.assertNotIn('auth_user',summary)

    def test_supplier_selection_and_user_modes_respect_visible_scope(self):
        EmployeeProfile.objects.filter(user=self.vendor).update(account_type='external_vendor')
        role=Role.objects.create(name='Selected supplier',slug='test-selected-supplier',dashboard_performers={'mode':'suppliers','supplier_ids':[self.vendor.pk]})
        EmployeeProfile.objects.filter(user=self.admin).update(role=role)
        owner=get_user_model().objects.get(pk=self.admin.pk)
        window=dashboard_range_window('today',now=self.now)
        rows=configured_performers(dashboard_attempts(owner,{},window),owner,{},3,window)
        self.assertEqual([(r['name'],r['completes']) for r in rows],[('Supplier One',3)])
        role.dashboard_performers={'mode':'users','supplier_ids':[],'branch_ids':[self.branch.pk]}
        role.save()
        owner=get_user_model().objects.get(pk=self.admin.pk)
        rows=configured_performers(dashboard_attempts(owner,{},window),owner,{},3,window)
        self.assertEqual([(r['name'],r['completes']) for r in rows],[('partner-worker',3)])
