from collections import Counter

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils.text import slugify

from accounts.models import EmployeeProfile, Role
from vendors.models import OrganizationUnit


def _email_local_part(value):
    return slugify(value).replace("-", ".")


class Command(BaseCommand):
    help = "Idempotently create or update employee accounts inside one organization Shift."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            required=True,
            help='Exact "Branch / Sub-branch / Shift" path.',
        )
        parser.add_argument("--domain", required=True, help="Email domain without @.")
        parser.add_argument(
            "--names",
            required=True,
            help="Semicolon-separated full names. Only the first name is stored on the account.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write changes. Without this flag the command performs a validation-only dry run.",
        )
        parser.add_argument(
            "--reset-existing-passwords",
            action="store_true",
            help="Also reset matching existing accounts to FirstName#123.",
        )

    def _shift(self, path):
        parts = [part.strip() for part in path.split("/") if part.strip()]
        if len(parts) != 3:
            raise CommandError("Path must contain exactly Branch / Sub-branch / Shift.")
        branch_name, sub_branch_name, shift_name = parts
        matches = OrganizationUnit.objects.select_related("parent__parent", "workspace_owner").filter(
            unit_type=OrganizationUnit.UnitType.SHIFT,
            name__iexact=shift_name,
            parent__unit_type=OrganizationUnit.UnitType.SUB_BRANCH,
            parent__name__iexact=sub_branch_name,
            parent__parent__unit_type=OrganizationUnit.UnitType.BRANCH,
            parent__parent__name__iexact=branch_name,
            is_active=True,
            parent__is_active=True,
            parent__parent__is_active=True,
        )
        count = matches.count()
        if count != 1:
            raise CommandError(
                f'Expected one active organization Shift at "{path}", found {count}.'
            )
        return matches.get()

    def _role(self):
        role = Role.objects.filter(
            slug__in=("employee", "employees"),
            is_active=True,
        ).order_by("rank", "id").first()
        if role is None:
            raise CommandError("No active employee/employees role exists.")
        return role

    def _records(self, raw_names, domain):
        names = [" ".join(value.split()) for value in raw_names.split(";") if value.strip()]
        if not names:
            raise CommandError("Provide at least one employee name.")
        domain = domain.strip().lower().lstrip("@")
        if not domain or "." not in domain:
            raise CommandError("Provide a valid email domain without @.")

        first_keys = [_email_local_part(name.split()[0]) for name in names]
        duplicates = Counter(first_keys)
        used = Counter()
        records = []
        for name, first_key in zip(names, first_keys):
            pieces = name.split()
            first_name = pieces[0].title()
            local_part = first_key
            if duplicates[first_key] > 1:
                suffix = _email_local_part(pieces[-1]) if len(pieces) > 1 else ""
                local_part = f"{first_key}.{suffix}" if suffix and suffix != first_key else first_key
            used[local_part] += 1
            if used[local_part] > 1:
                local_part = f"{local_part}{used[local_part]}"
            records.append({
                "source_name": name,
                "first_name": first_name,
                "email": f"{local_part}@{domain}",
                "password": f"{first_name}#123",
            })
        return records

    def handle(self, *args, **options):
        shift = self._shift(options["path"])
        role = self._role()
        records = self._records(options["names"], options["domain"])
        User = get_user_model()

        resolved = []
        for record in records:
            matches = User.objects.filter(
                Q(username__iexact=record["email"]) | Q(email__iexact=record["email"])
            ).distinct()
            if matches.count() > 1:
                raise CommandError(f'Multiple accounts already use {record["email"]}.')
            resolved.append((record, matches.first()))

        mode = "APPLY" if options["apply"] else "DRY RUN"
        self.stdout.write(f"{mode}: {shift.path_label} · role {role.slug}")
        for record, existing in resolved:
            action = "update" if existing else "create"
            self.stdout.write(
                f'{action:6}  {record["first_name"]:12}  {record["email"]:42}  {record["password"]}'
            )
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("No changes made. Re-run with --apply after reviewing."))
            return

        created_count = updated_count = 0
        with transaction.atomic():
            for record, user in resolved:
                created = user is None
                if created:
                    user = User(username=record["email"], email=record["email"])
                user.username = record["email"]
                user.email = record["email"]
                user.first_name = record["first_name"]
                user.last_name = ""
                user.is_active = True
                if created or options["reset_existing_passwords"]:
                    user.set_password(record["password"])
                user.save()
                profile, _ = EmployeeProfile.objects.get_or_create(user=user)
                profile.role = role
                profile.account_type = EmployeeProfile.AccountType.EMPLOYEE
                profile.organization_unit = shift
                profile.created_by = shift.workspace_owner
                profile.department = shift.parent.name
                profile.job_title = "Employee"
                profile.save(update_fields=[
                    "role", "account_type", "organization_unit", "created_by",
                    "department", "job_title", "updated_at",
                ])
                created_count += int(created)
                updated_count += int(not created)

        self.stdout.write(self.style.SUCCESS(
            f"Done: {created_count} created, {updated_count} updated in {shift.path_label}."
        ))
