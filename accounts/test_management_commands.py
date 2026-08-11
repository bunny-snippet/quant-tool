from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from accounts.models import EmployeeProfile
from vendors.models import OrganizationUnit


class BulkShiftEmployeesCommandTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(
            username="bulk-owner", email="owner@example.test", password="test-password"
        )
        self.branch = OrganizationUnit.objects.create(
            workspace_owner=self.owner,
            unit_type=OrganizationUnit.UnitType.BRANCH,
            name="Opinion",
            code="opinion",
            created_by=self.owner,
        )
        self.sub_branch = OrganizationUnit.objects.create(
            workspace_owner=self.owner,
            parent=self.branch,
            unit_type=OrganizationUnit.UnitType.SUB_BRANCH,
            name="Quantish Opinion Spaze",
            code="quantish-opinion-spaze",
            created_by=self.owner,
        )
        self.shift = OrganizationUnit.objects.create(
            workspace_owner=self.owner,
            parent=self.sub_branch,
            unit_type=OrganizationUnit.UnitType.SHIFT,
            name="Morning",
            code="morning",
            created_by=self.owner,
        )

    def command(self, apply=False):
        args = {
            "path": "Opinion / Quantish Opinion Spaze / Morning",
            "domain": "quantishspaze.com",
            "names": "Priyanshu K;Peeyush;Priyanshu Panchal;Deepak Singh;Deepak Vaishnav",
            "stdout": StringIO(),
        }
        if apply:
            args["apply"] = True
        call_command("bulk_shift_employees", **args)

    def test_dry_run_is_non_mutating_and_apply_is_idempotent(self):
        self.command()
        self.assertFalse(get_user_model().objects.filter(email__endswith="@quantishspaze.com").exists())

        self.command(apply=True)
        expected_emails = {
            "priyanshu.k@quantishspaze.com",
            "peeyush@quantishspaze.com",
            "priyanshu.panchal@quantishspaze.com",
            "deepak.singh@quantishspaze.com",
            "deepak.vaishnav@quantishspaze.com",
        }
        users = get_user_model().objects.filter(email__in=expected_emails)
        self.assertEqual(set(users.values_list("email", flat=True)), expected_emails)
        self.assertTrue(users.get(email="priyanshu.k@quantishspaze.com").check_password("Priyanshu#123"))
        self.assertTrue(users.get(email="deepak.singh@quantishspaze.com").check_password("Deepak#123"))
        profiles = EmployeeProfile.objects.filter(user__in=users)
        self.assertEqual(profiles.count(), 5)
        self.assertFalse(profiles.exclude(organization_unit=self.shift).exists())
        self.assertFalse(profiles.exclude(role__slug__in=("employee", "employees")).exists())

        self.command(apply=True)
        self.assertEqual(get_user_model().objects.filter(email__in=expected_emails).count(), 5)
