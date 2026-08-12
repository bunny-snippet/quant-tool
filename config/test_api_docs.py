import base64

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Role


@override_settings(API_DOCS_BASIC_USERNAME="docs-user", API_DOCS_BASIC_PASSWORD="docs-password")
class APIDocumentationProtectionTests(TestCase):
    @staticmethod
    def basic(username="docs-user", password="docs-password"):
        value = base64.b64encode(f"{username}:{password}".encode()).decode()
        return f"Basic {value}"

    def setUp(self):
        self.superuser = get_user_model().objects.create_superuser(
            username="root-docs", email="root@example.com", password="test-password"
        )

    def test_anonymous_request_is_sent_to_django_login(self):
        response = self.client.get(reverse("swagger-ui"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_admin_session_is_also_challenged_for_docs_password(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("swagger-ui"))
        self.assertEqual(response.status_code, 401)
        self.assertIn("Basic", response["WWW-Authenticate"])

    def test_wrong_docs_password_is_rejected(self):
        self.client.force_login(self.superuser)
        response = self.client.get(
            reverse("swagger-ui"), HTTP_AUTHORIZATION=self.basic(password="wrong")
        )
        self.assertEqual(response.status_code, 401)

    def test_admin_session_and_docs_password_open_ui_and_schema(self):
        self.client.force_login(self.superuser)
        headers = {"HTTP_AUTHORIZATION": self.basic()}
        self.assertEqual(self.client.get(reverse("swagger-ui"), **headers).status_code, 200)
        schema = self.client.get(reverse("api-schema"), **headers)
        self.assertEqual(schema.status_code, 200)
        self.assertNotIn("docs-password", schema.content.decode("utf-8"))

    def test_application_admin_role_is_allowed_without_django_staff_flag(self):
        role = Role.objects.get(slug="admin")
        user = get_user_model().objects.create_user(username="workspace-admin", password="test-password")
        user.employee_profile.role = role
        user.employee_profile.save(update_fields=["role", "updated_at"])
        self.client.force_login(user)
        response = self.client.get(reverse("swagger-ui"), HTTP_AUTHORIZATION=self.basic())
        self.assertEqual(response.status_code, 200)

    def test_employee_is_forbidden_even_with_docs_password(self):
        role = Role.objects.get(slug="employee")
        user = get_user_model().objects.create_user(username="employee", password="test-password")
        user.employee_profile.role = role
        user.employee_profile.save(update_fields=["role", "updated_at"])
        self.client.force_login(user)
        response = self.client.get(reverse("swagger-ui"), HTTP_AUTHORIZATION=self.basic())
        self.assertEqual(response.status_code, 403)

    @override_settings(API_DOCS_BASIC_PASSWORD="")
    def test_missing_server_password_fails_closed(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("swagger-ui"), HTTP_AUTHORIZATION=self.basic())
        self.assertEqual(response.status_code, 503)
