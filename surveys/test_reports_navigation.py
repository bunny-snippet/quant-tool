"""Navigation grouping must not grant access or change report destinations."""
import re

from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import SimpleTestCase, RequestFactory
from django.urls import reverse


class ReportsNavigationTests(SimpleTestCase):
    def test_traffic_revenue_card_obeys_existing_permission(self):
        request = RequestFactory().get('/reports/traffic/')
        request.user = AnonymousUser()
        for cards, expected in [(['total', 'revenue'], True), (['total'], False)]:
            html = render_to_string('surveys/studies.html', {'request': request, 'study_cards': cards})
            self.assertEqual('id="studyMetricRevenue"' in html, expected)
            self.assertNotIn('id="studyMetricInvoicedRevenue"', html)

    def render_shell(self, codes, page='dashboard'):
        request = RequestFactory().get('/')
        request.user = AnonymousUser()
        return render_to_string('surveys/base.html', {'access_codes':set(codes), 'active_page':page, 'request':request})

    def test_reports_is_disclosure_not_a_page_link(self):
        html = self.render_shell(['attempts.view','user_hits.view'])
        group = re.search(r'<details[^>]*id="reportsNavGroup"[^>]*>(.*?)</details>',html,re.S).group(1)
        summary = re.search(r'<summary\b.*?</summary>',group,re.S).group(0)
        self.assertIn('<span>Reports</span>',summary)
        self.assertNotIn('href=',summary)
        self.assertIn(f'href="{reverse("reports")}"',group)
        self.assertIn('Response Summary</a>',group)
        self.assertIn(f'href="{reverse("user-hits")}"',group)
        self.assertEqual(html.count('Supplier Activity</a>'),1)

    def test_each_child_retains_its_permissions_and_fallback(self):
        for code in ['attempts.view','termination_reasons.view']:
            html=self.render_shell([code])
            self.assertIn('Response Summary</a>',html)
            self.assertNotIn('Supplier Activity</a>',html)
        for code,route in [('user_hits.view','user-hits'),('user_dashboard.view','user-dashboard'),('vendors.view','vendor-management'),('vendors.manage','vendor-management'),('allocations.view','vendor-management'),('allocations.manage','vendor-management')]:
            with self.subTest(code=code):
                html=self.render_shell([code])
                self.assertIn('id="reportsNavGroup"',html)
                self.assertNotIn('Response Summary</a>',html)
                self.assertRegex(html,rf'href="{re.escape(reverse(route))}"[^>]*><i aria-hidden="true"></i>Supplier Activity')
        self.assertNotIn('id="reportsNavGroup"',self.render_shell(['projects.view']))

    def test_current_child_opens_group_and_is_marked_active(self):
        codes=['attempts.view','user_hits.view']
        for page in ['studies','termination-reasons','reconciliation','user-hits','vendors']:
            html=self.render_shell(codes,page)
            self.assertIn('id="reportsNavGroup" open',html)
            self.assertIn('aria-current="page"',html)
        self.assertNotIn('id="reportsNavGroup" open',self.render_shell(codes))
