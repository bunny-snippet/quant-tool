from accounts.models import EmployeeProfile


VENDOR_ACCOUNT_TYPES = {
    EmployeeProfile.AccountType.INTERNAL_VENDOR,
    EmployeeProfile.AccountType.EXTERNAL_VENDOR,
}


def vendor_scope_user_id(user) -> int | None:
    """Return the vendor owning this account, including internal-vendor descendants."""

    if not user or not user.is_authenticated:
        return None
    if hasattr(user, "_vendor_scope_user_id_cache"):
        return user._vendor_scope_user_id_cache
    current = user
    visited: set[int] = set()
    while current and current.pk not in visited:
        visited.add(current.pk)
        profile = EmployeeProfile.objects.select_related(
            "created_by", "organization_unit__workspace_owner__employee_profile"
        ).filter(user=current).first()
        if not profile:
            user._vendor_scope_user_id_cache = None
            return None
        if profile.organization_unit_id:
            owner = profile.organization_unit.workspace_owner
            owner_profile = getattr(owner, "employee_profile", None)
            if getattr(owner_profile, "account_type", "") in VENDOR_ACCOUNT_TYPES:
                user._vendor_scope_user_id_cache = owner.pk
                return owner.pk
        if profile.account_type in VENDOR_ACCOUNT_TYPES:
            user._vendor_scope_user_id_cache = current.pk
            return current.pk
        current = profile.created_by
    user._vendor_scope_user_id_cache = None
    return None


def is_external_vendor_scope(user) -> bool:
    """Whether the current account is an external vendor or one of its descendants."""

    if not user or not user.is_authenticated:
        return False
    if hasattr(user, "_external_vendor_scope_cache"):
        return user._external_vendor_scope_cache
    vendor_id = vendor_scope_user_id(user)
    result = bool(vendor_id and EmployeeProfile.objects.filter(
        user_id=vendor_id,
        account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR,
    ).exists())
    user._external_vendor_scope_cache = result
    return result


def organization_workspace_owner_ids(user) -> set[int]:
    """Workspaces whose Branch/Sub-branch/Shift tree the user may manage."""

    if not user or not user.is_authenticated:
        return set()
    if user.is_superuser:
        internal_vendor_ids = EmployeeProfile.objects.filter(
            account_type=EmployeeProfile.AccountType.INTERNAL_VENDOR,
            user__is_active=True,
        ).values_list("user_id", flat=True)
        return {user.pk, *internal_vendor_ids}
    vendor_id = vendor_scope_user_id(user)
    if vendor_id and EmployeeProfile.objects.filter(
        user_id=vendor_id,
        account_type=EmployeeProfile.AccountType.INTERNAL_VENDOR,
    ).exists():
        return {vendor_id}
    return set()


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
