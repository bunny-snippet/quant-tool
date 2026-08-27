import zipfile
from io import BytesIO
from xml.etree import ElementTree

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import EmployeeProfile, Role
from vendors.models import (
    Client,
    ClientIntegration,
    OrganizationClientAccess,
    OrganizationUnit,
    VendorClientAllocation,
    VendorCommercialProfile,
)

from .models import Survey, SurveyAttempt, TolunaNotification


def _xlsx_rows(response):
    content = b"".join(response.streaming_content)
    with zipfile.ZipFile(BytesIO(content)) as workbook:
        root = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows = []
    for row in root.findall(".//x:sheetData/x:row", namespace):
        values = []
        for cell in row.findall("x:c", namespace):
            inline = cell.find("x:is/x:t", namespace)
            numeric = cell.find("x:v", namespace)
            values.append(
                inline.text
                if inline is not None
                else numeric.text
                if numeric is not None
                else ""
            )
        rows.append(values)
    return rows


class ReportScopeSecurityTests(TestCase):
    def setUp(self):
        self.superuser = get_user_model().objects.create_superuser(
            username="report-scope-root",
            email="report-scope-root@example.test",
            password="test-password",
        )
        self.admin_role = Role.objects.get(slug="admin")
        self.alice = self._admin_user("report-scope-alice")
        self.bob = self._admin_user("report-scope-bob")

        self.client_a = Client.objects.create(
            code="report-scope-a", name="Scoped Toluna A", provider_code="toluna"
        )
        self.client_b = Client.objects.create(
            code="report-scope-b", name="Scoped Toluna B", provider_code="toluna"
        )
        self.integration_a = ClientIntegration.objects.create(
            client=self.client_a,
            name="Scoped Toluna A",
            provider_code="toluna",
            base_url="https://tws-a.example.test",
        )
        self.integration_b = ClientIntegration.objects.create(
            client=self.client_b,
            name="Scoped Toluna B",
            provider_code="toluna",
            base_url="https://tws-b.example.test",
        )
        self.survey_a = Survey.objects.create(
            client=self.client_a,
            integration=self.integration_a,
            source_id=810001,
            source_key="810001:910001",
            name="Scoped survey A",
            country="United States",
            country_code="US",
            buyer_id="buyer-a",
        )
        self.survey_b = Survey.objects.create(
            client=self.client_b,
            integration=self.integration_b,
            source_id=820001,
            source_key="820001:920001",
            name="Scoped survey B",
            country="Canada",
            country_code="CA",
            buyer_id="buyer-b",
        )

    def _admin_user(self, username):
        user = get_user_model().objects.create_user(username=username)
        EmployeeProfile.objects.filter(user=user).update(role=self.admin_role)
        return get_user_model().objects.get(pk=user.pk)

    @staticmethod
    def _attempt(rid, survey, *, platform_user=None, legacy_user_id=""):
        return SurveyAttempt.objects.create(
            rid=rid,
            survey=survey,
            platform_user=platform_user,
            user_id=legacy_user_id,
            status=SurveyAttempt.Status.TERMINATED,
            callback_at=timezone.now(),
        )

    def test_term_reports_scope_page_options_detail_export_and_legacy_rows(self):
        alice_current = self._attempt(
            "AlCur00001",
            self.survey_a,
            platform_user=self.alice,
            legacy_user_id=str(self.alice.pk),
        )
        alice_legacy = self._attempt(
            "AlLeg00001", self.survey_a, legacy_user_id=str(self.alice.pk)
        )
        bob_current = self._attempt(
            "BoCur00001",
            self.survey_b,
            platform_user=self.bob,
            legacy_user_id=str(self.bob.pk),
        )
        self._attempt("BoLeg00001", self.survey_b, legacy_user_id=str(self.bob.pk))
        self._attempt(
            "FkBob00001",
            self.survey_b,
            platform_user=self.bob,
            legacy_user_id=str(self.alice.pk),
        )

        self.client.force_login(self.alice)
        page = self.client.get(reverse("termination-reasons"))

        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["summary"]["total"], 2)
        self.assertContains(page, alice_current.rid)
        self.assertContains(page, alice_legacy.rid)
        self.assertNotContains(page, bob_current.rid)
        self.assertEqual(
            {row["survey__client_id"] for row in page.context["term_reason_clients"]},
            {self.client_a.pk},
        )

        hidden_detail = self.client.get(
            reverse("termination-reasons"), {"detail": bob_current.rid}
        )
        self.assertIsNone(hidden_detail.context["detail_attempt"])
        self.assertContains(hidden_detail, "No survey attempt was found")

        export = self.client.get(reverse("termination-reasons-export"))
        flattened_export = [value for row in _xlsx_rows(export) for value in row]
        self.assertIn(alice_current.rid, flattened_export)
        self.assertIn(alice_legacy.rid, flattened_export)
        self.assertNotIn(bob_current.rid, flattened_export)

        self.client.force_login(self.superuser)
        root_page = self.client.get(reverse("termination-reasons"))
        self.assertEqual(root_page.context["summary"]["total"], 5)

    def test_member_notifications_follow_attempt_activity_scope(self):
        alice_attempt = self._attempt(
            "NtAli00001",
            self.survey_a,
            platform_user=self.alice,
            legacy_user_id=str(self.alice.pk),
        )
        alice_legacy = self._attempt(
            "NtLeg00001", self.survey_a, legacy_user_id=str(self.alice.pk)
        )
        bob_attempt = self._attempt(
            "NtBob00001",
            self.survey_b,
            platform_user=self.bob,
            legacy_user_id=str(self.bob.pk),
        )
        alice_notification = TolunaNotification.objects.create(
            event_type=TolunaNotification.EventType.MEMBER_TERMINATE,
            payload_hash="scope-member-alice",
            integration=self.integration_a,
            survey=self.survey_a,
            attempt=alice_attempt,
            unique_code="alice-member",
            provider_status="Terminated",
        )
        TolunaNotification.objects.create(
            event_type=TolunaNotification.EventType.RECONCILIATION,
            payload_hash="scope-member-alice-legacy",
            integration=self.integration_a,
            survey=self.survey_a,
            attempt=alice_legacy,
            unique_code="alice-legacy-member",
            provider_status="Reconciled",
        )
        bob_notification = TolunaNotification.objects.create(
            event_type=TolunaNotification.EventType.MEMBER_TERMINATE,
            payload_hash="scope-member-bob",
            integration=self.integration_b,
            survey=self.survey_b,
            attempt=bob_attempt,
            unique_code="bob-member",
            provider_status="Terminated",
        )
        TolunaNotification.objects.create(
            event_type=TolunaNotification.EventType.MEMBER_COMPLETE,
            payload_hash="scope-unmatched-member",
            integration=self.integration_a,
            survey=self.survey_a,
            unique_code="unmatched-member",
            provider_status="Completed",
        )

        self.client.force_login(self.alice)
        page = self.client.get(
            reverse("toluna-notifications"), {"detail": bob_notification.pk}
        )

        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["summary"]["total"], 2)
        self.assertIsNone(page.context["detail"])
        self.assertContains(page, alice_notification.unique_code)
        self.assertNotContains(page, bob_notification.unique_code)
        self.assertEqual(
            {row["id"] for row in page.context["notification_clients"]},
            {self.client_a.pk},
        )

        export = self.client.get(reverse("toluna-notifications-export"))
        flattened_export = [value for row in _xlsx_rows(export) for value in row]
        self.assertIn(alice_attempt.rid, flattened_export)
        self.assertNotIn(bob_attempt.rid, flattened_export)

        self.client.force_login(self.superuser)
        root_page = self.client.get(reverse("toluna-notifications"))
        self.assertEqual(root_page.context["summary"]["total"], 4)

    def test_client_scoped_operational_events_hide_other_and_unmatched_rows(self):
        scoped_admin = self._admin_user("report-scope-client-admin")
        branch = OrganizationUnit.objects.create(
            workspace_owner=self.superuser,
            unit_type=OrganizationUnit.UnitType.BRANCH,
            name="Scoped branch",
            code="report-scope-branch",
            created_by=self.superuser,
        )
        sub_branch = OrganizationUnit.objects.create(
            workspace_owner=self.superuser,
            parent=branch,
            unit_type=OrganizationUnit.UnitType.SUB_BRANCH,
            name="Scoped sub-branch",
            code="report-scope-sub-branch",
            created_by=self.superuser,
        )
        shift = OrganizationUnit.objects.create(
            workspace_owner=self.superuser,
            parent=sub_branch,
            unit_type=OrganizationUnit.UnitType.SHIFT,
            name="Scoped shift",
            code="report-scope-shift",
            created_by=self.superuser,
        )
        EmployeeProfile.objects.filter(user=scoped_admin).update(organization_unit=shift)
        OrganizationClientAccess.objects.create(
            organization_unit=branch,
            client=self.client_a,
            created_by=self.superuser,
        )
        allowed = TolunaNotification.objects.create(
            event_type=TolunaNotification.EventType.QUOTA_STATUS,
            payload_hash="scope-operational-allowed",
            integration=self.integration_a,
            survey=self.survey_a,
            provider_survey_id=self.survey_a.source_id,
            wave_id=910001,
            quota_id=101,
            provider_status="Open",
        )
        hidden_client = TolunaNotification.objects.create(
            event_type=TolunaNotification.EventType.SURVEY_CLOSED,
            payload_hash="scope-operational-hidden-client",
            integration=self.integration_b,
            survey=self.survey_b,
            provider_survey_id=self.survey_b.source_id,
            wave_id=920001,
            provider_status="Closed",
        )
        unmatched = TolunaNotification.objects.create(
            event_type=TolunaNotification.EventType.SURVEY_CLOSED,
            payload_hash="scope-operational-unmatched",
            integration=self.integration_a,
            provider_survey_id=999999,
            wave_id=999001,
            provider_status="Closed",
        )

        self.client.force_login(scoped_admin)
        page = self.client.get(reverse("toluna-notifications"))

        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["summary"]["total"], 1)
        self.assertEqual(page.context["page_obj"].object_list[0].pk, allowed.pk)
        self.assertNotContains(page, str(hidden_client.provider_survey_id))
        self.assertNotContains(page, str(unmatched.provider_survey_id))
        self.assertEqual(
            {row["id"] for row in page.context["notification_clients"]},
            {self.client_a.pk},
        )

        self.client.force_login(self.superuser)
        root_page = self.client.get(reverse("toluna-notifications"))
        self.assertEqual(root_page.context["summary"]["total"], 3)

    def test_internal_vendor_audit_sees_allocated_closed_survey_without_capacity(self):
        internal_vendor = self._admin_user("report-scope-internal-vendor")
        EmployeeProfile.objects.filter(user=internal_vendor).update(
            account_type=EmployeeProfile.AccountType.INTERNAL_VENDOR,
            created_by=self.superuser,
        )
        VendorCommercialProfile.objects.create(
            vendor=internal_vendor,
            created_by=self.superuser,
        )
        VendorClientAllocation.objects.create(
            vendor=internal_vendor,
            client=self.client_a,
            quantity_limit=10,
            created_by=self.superuser,
        )
        Survey.objects.filter(pk__in=[self.survey_a.pk, self.survey_b.pk]).update(
            status=Survey.Status.CLOSED,
            remaining=0,
        )
        allowed = TolunaNotification.objects.create(
            event_type=TolunaNotification.EventType.SURVEY_CLOSED,
            payload_hash="scope-internal-closed-allowed",
            integration=self.integration_a,
            survey=self.survey_a,
            provider_survey_id=self.survey_a.source_id,
            wave_id=910001,
            provider_status="Closed",
            applied=True,
        )
        hidden_client = TolunaNotification.objects.create(
            event_type=TolunaNotification.EventType.SURVEY_CLOSED,
            payload_hash="scope-internal-closed-hidden",
            integration=self.integration_b,
            survey=self.survey_b,
            provider_survey_id=self.survey_b.source_id,
            wave_id=920001,
            provider_status="Closed",
            applied=True,
        )

        self.client.force_login(internal_vendor)

        # Normal project/routing visibility remains capacity-gated.
        projects = self.client.get(reverse("survey-list"))
        self.assertEqual(projects.status_code, 200)
        self.assertEqual(projects.data["count"], 0)

        audit = self.client.get(reverse("toluna-notifications"))
        self.assertEqual(audit.status_code, 200)
        self.assertEqual(audit.context["summary"]["total"], 1)
        self.assertEqual(audit.context["page_obj"].object_list[0].pk, allowed.pk)
        self.assertNotContains(audit, str(hidden_client.provider_survey_id))
        self.assertEqual(
            {row["id"] for row in audit.context["notification_clients"]},
            {self.client_a.pk},
        )
