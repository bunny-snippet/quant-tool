from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from accounts.models import AccessFunction, UserFunctionOverride


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class WorkspaceLandingTests(TestCase):
    def setUp(self):
        self.owner=get_user_model().objects.create_superuser(username='landing-owner',password='test-only')
        self.worker=get_user_model().objects.create_user(username='landing-worker',password='test-only')

    def test_owner_root_and_login_ignore_old_partner_next(self):
        response=self.client.post('/login/?next=/dashboard/client/',{'username':'landing-owner','password':'test-only','next':'/dashboard/supplier/'})
        self.assertEqual(response.status_code,302)
        self.assertEqual(response.url,'/dashboard/')
        self.assertEqual(self.client.get('/').url,'/dashboard/')
        self.assertEqual(self.client.get('/login/?next=/dashboard/client/').url,'/dashboard/')

    def test_no_dashboard_grant_goes_to_projects(self):
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get('/').url,'/projects/')
        self.assertEqual(self.client.get('/login/?next=/dashboard/client/').url,'/projects/')

    def test_explicit_employee_dashboard_permission_controls_landing(self):
        function=AccessFunction.objects.get(code='dashboard.view')
        override=UserFunctionOverride.objects.create(user=self.worker,function=function,effect='allow')
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get('/').url,'/dashboard/')
        override.effect='deny';override.save()
        self.assertEqual(self.client.get('/').url,'/projects/')

    def test_anonymous_root_still_requires_login(self):
        self.assertEqual(self.client.get('/').url,'/login/?next=/')
