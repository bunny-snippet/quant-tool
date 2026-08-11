import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import AccessFunction, EmployeeProfile, Role, RoleFunctionPermission


FORMAT_VERSION = 1
REQUIRED_FALLBACK_ROLES = {"employee", "super-admin"}


def serialize_role_config():
    roles = []
    queryset = Role.objects.prefetch_related("function_assignments__function").order_by("rank", "slug")
    for role in queryset:
        assignments = sorted(
            role.function_assignments.all(),
            key=lambda assignment: assignment.function.code,
        )
        roles.append({
            "name": role.name,
            "slug": role.slug,
            "description": role.description,
            "rank": role.rank,
            "cpi_visibility_percent": str(role.cpi_visibility_percent),
            "is_system": role.is_system,
            "is_active": role.is_active,
            "permissions": [
                {"code": assignment.function.code, "allowed": assignment.allowed}
                for assignment in assignments
            ],
        })
    return {
        "format_version": FORMAT_VERSION,
        "exported_at": timezone.now().isoformat(),
        "roles": roles,
    }


def write_role_config(path, payload=None):
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload or serialize_role_config(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return target


def validate_role_config(payload):
    if not isinstance(payload, dict) or payload.get("format_version") != FORMAT_VERSION:
        raise CommandError(f"Role configuration must use format_version {FORMAT_VERSION}.")
    roles = payload.get("roles")
    if not isinstance(roles, list) or not roles:
        raise CommandError("Role configuration does not contain any roles.")
    slugs = [str(role.get("slug") or "").strip() for role in roles]
    if any(not slug for slug in slugs) or len(slugs) != len(set(slugs)):
        raise CommandError("Role slugs must be non-empty and unique.")
    missing_fallbacks = REQUIRED_FALLBACK_ROLES - set(slugs)
    if missing_fallbacks:
        raise CommandError(f"Missing required fallback roles: {', '.join(sorted(missing_fallbacks))}.")
    permission_codes = []
    for role in roles:
        permissions = role.get("permissions", [])
        if not isinstance(permissions, list):
            raise CommandError(f"Permissions for role {role['slug']} must be a list.")
        codes = [str(item.get("code") or "").strip() for item in permissions]
        if any(not code for code in codes) or len(codes) != len(set(codes)):
            raise CommandError(f"Permission codes for role {role['slug']} must be non-empty and unique.")
        permission_codes.extend(codes)
    known_codes = set(
        AccessFunction.objects.filter(code__in=set(permission_codes)).values_list("code", flat=True)
    )
    missing_codes = sorted(set(permission_codes) - known_codes)
    if missing_codes:
        raise CommandError(
            "Target is missing AccessFunction codes required by the source: " + ", ".join(missing_codes)
        )
    return roles


class Command(BaseCommand):
    help = "Export or transactionally replace role definitions and role-level function permissions."

    def add_arguments(self, parser):
        action = parser.add_mutually_exclusive_group(required=True)
        action.add_argument("--export", dest="export_path", help="Write current role configuration to this JSON file.")
        action.add_argument("--import", dest="import_path", help="Read role configuration from this JSON file.")
        parser.add_argument(
            "--replace",
            action="store_true",
            help="Required for import. Replaces target roles and role-level permission assignments.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Validate an import without changing the database.")
        parser.add_argument("--backup", help="Optional target backup path created before import.")

    def handle(self, *args, **options):
        if options["export_path"]:
            target = write_role_config(options["export_path"])
            self.stdout.write(self.style.SUCCESS(f"Exported {Role.objects.count()} roles to {target}"))
            return

        if not options["replace"] and not options["dry_run"]:
            raise CommandError("Import is destructive; pass --replace after reviewing the source JSON.")
        source = Path(options["import_path"]).expanduser().resolve()
        if not source.is_file():
            raise CommandError(f"Role configuration file not found: {source}")
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Could not read role configuration: {exc}") from exc
        roles = validate_role_config(payload)
        assignment_count = sum(len(role.get("permissions", [])) for role in roles)
        if options["dry_run"]:
            self.stdout.write(self.style.SUCCESS(
                f"Validated {len(roles)} roles and {assignment_count} role permissions; no changes made."
            ))
            return

        timestamp = timezone.now().strftime("%Y%m%d-%H%M%S")
        backup = options["backup"] or Path.cwd() / f"role-config-backup-{timestamp}.json"
        backup_path = write_role_config(backup)

        with transaction.atomic():
            desired_slugs = {role["slug"] for role in roles}
            role_objects = {}
            for item in roles:
                role, _created = Role.objects.update_or_create(
                    slug=item["slug"],
                    defaults={
                        "name": item.get("name") or item["slug"],
                        "description": item.get("description") or "",
                        "rank": int(item.get("rank") or 0),
                        "cpi_visibility_percent": item.get("cpi_visibility_percent") or "100.00",
                        "is_system": bool(item.get("is_system", False)),
                        "is_active": bool(item.get("is_active", True)),
                        "created_by": None,
                    },
                )
                role_objects[item["slug"]] = role

            extra_roles = Role.objects.exclude(slug__in=desired_slugs)
            extra_role_ids = list(extra_roles.values_list("id", flat=True))
            if extra_role_ids:
                EmployeeProfile.objects.filter(
                    role_id__in=extra_role_ids, user__is_superuser=True,
                ).update(role=role_objects["super-admin"])
                EmployeeProfile.objects.filter(
                    role_id__in=extra_role_ids, user__is_superuser=False,
                ).update(role=role_objects["employee"])
                extra_roles.delete()

            imported_roles = list(role_objects.values())
            RoleFunctionPermission.objects.filter(role__in=imported_roles).delete()
            functions = {
                function.code: function
                for function in AccessFunction.objects.filter(
                    code__in={
                        permission["code"]
                        for role in roles
                        for permission in role.get("permissions", [])
                    }
                )
            }
            assignments = [
                RoleFunctionPermission(
                    role=role_objects[item["slug"]],
                    function=functions[permission["code"]],
                    allowed=bool(permission.get("allowed", True)),
                )
                for item in roles
                for permission in item.get("permissions", [])
            ]
            RoleFunctionPermission.objects.bulk_create(assignments)

        self.stdout.write(self.style.SUCCESS(
            f"Imported {len(roles)} roles and {assignment_count} role permissions. Backup: {backup_path}"
        ))
