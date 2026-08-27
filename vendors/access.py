from django.db.models import Q

from accounts.models import EmployeeProfile
from accounts.profile_context import employee_profile_for_user
from accounts.request_cache import request_cached


VENDOR_ACCOUNT_TYPES = {
    EmployeeProfile.AccountType.INTERNAL_VENDOR,
    EmployeeProfile.AccountType.EXTERNAL_VENDOR,
}


def valid_supplier_profile_q(prefix=""):
    """Match supplier account types only when their required role still exists."""

    return (
        Q(**{
            f"{prefix}account_type": EmployeeProfile.AccountType.INTERNAL_VENDOR,
            f"{prefix}role__slug": "admin",
            f"{prefix}role__is_active": True,
        })
        | Q(**{
            f"{prefix}account_type": EmployeeProfile.AccountType.EXTERNAL_VENDOR,
            f"{prefix}role__slug": "external-vendor",
            f"{prefix}role__is_active": True,
        })
    )


def is_valid_supplier_profile(profile) -> bool:
    if not profile or not profile.role_id or not profile.role.is_active:
        return False
    expected_role = {
        EmployeeProfile.AccountType.INTERNAL_VENDOR: "admin",
        EmployeeProfile.AccountType.EXTERNAL_VENDOR: "external-vendor",
    }.get(profile.account_type)
    return bool(expected_role and profile.role.slug == expected_role)


def vendor_scope_user_id(user) -> int | None:
    """Return the vendor owning this account, including internal-vendor descendants."""

    if not user or not user.is_authenticated:
        return None

    def load():
        current = user
        visited: set[int] = set()
        while current and current.pk not in visited:
            visited.add(current.pk)
            profile = employee_profile_for_user(current)
            if not profile:
                return None
            if profile.organization_unit_id:
                owner = profile.organization_unit.workspace_owner
                owner_profile = getattr(owner, "employee_profile", None)
                if getattr(owner_profile, "account_type", "") in VENDOR_ACCOUNT_TYPES:
                    return owner.pk
            if profile.account_type in VENDOR_ACCOUNT_TYPES:
                return current.pk
            current = profile.created_by
        return None

    return request_cached(("vendor-scope-user-id", user.pk), load)


def is_external_vendor_scope(user) -> bool:
    """Whether the current account is an external vendor or one of its descendants."""

    if not user or not user.is_authenticated:
        return False

    def load():
        vendor_id = vendor_scope_user_id(user)
        return bool(vendor_id and EmployeeProfile.objects.filter(
            user_id=vendor_id,
            account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR,
        ).exists())

    return bool(request_cached(("external-vendor-scope", user.pk), load))


def organization_workspace_owner_ids(user) -> set[int]:
    """Workspaces whose Branch/Sub-branch/Shift tree the user may manage."""

    if not user or not user.is_authenticated:
        return set()

    def load():
        if user.is_superuser:
            internal_vendor_ids = EmployeeProfile.objects.filter(
                account_type=EmployeeProfile.AccountType.INTERNAL_VENDOR,
                user__is_active=True,
            ).values_list("user_id", flat=True)
            return frozenset({user.pk, *internal_vendor_ids})
        vendor_id = vendor_scope_user_id(user)
        if vendor_id and EmployeeProfile.objects.filter(
            user_id=vendor_id,
            account_type=EmployeeProfile.AccountType.INTERNAL_VENDOR,
        ).exists():
            return frozenset({vendor_id})
        return frozenset()

    return set(request_cached(("organization-workspace-owners", user.pk), load))


def organization_unit_descendant_ids(unit, include_self=True) -> set[int]:
    if not unit:
        return set()
    ids = {unit.pk} if include_self else set()
    frontier = {unit.pk}
    from .models import OrganizationUnit
    while frontier:
        children = set(
            OrganizationUnit.objects.filter(parent_id__in=frontier).values_list("id", flat=True)
        ) - ids
        ids.update(children)
        frontier = children
    return ids


def organization_unit_ancestor_ids(unit, include_self=True) -> set[int]:
    if not unit:
        return set()
    ids = {unit.pk} if include_self else set()
    current = unit.parent
    while current and current.pk not in ids:
        ids.add(current.pk)
        current = current.parent
    return ids
