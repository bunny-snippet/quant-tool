from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from .models import AccessFunction, EmployeeProfile, Role, UserFunctionOverride


EXTERNAL_VENDOR_FORBIDDEN_CODES = frozenset({
    "access.manage",
    "permissions.view",
    "roles.view", "roles.create", "roles.update", "roles.delete",
    "users.manage", "users.view", "users.create", "users.update", "users.delete",
    "respondents.create",
    "clients.manage", "vendors.manage", "allocations.manage",
    "clients.integration.view", "clients.integration.manage", "clients.integration.test",
    "clients.integration.preview", "clients.integration.sync",
    "organization.view", "organization.manage", "organization.clients.manage",
    "termination_reasons.view", "termination_reasons.filter.rid", "termination_reasons.action.refresh",
    "termination_reasons.filter.status", "termination_reasons.filter.client", "termination_reasons.filters.clear",
    "termination_reasons.summary", "termination_reasons.control.pagination", "termination_reasons.action.details",
    "termination_reasons.card.total", "termination_reasons.card.terminated",
    "termination_reasons.card.quota", "termination_reasons.card.quality",
    "termination_reasons.column.rid", "termination_reasons.column.survey", "termination_reasons.column.client",
    "termination_reasons.column.respondent", "termination_reasons.column.status", "termination_reasons.column.ended",
    "termination_reasons.column.actions",
    "termination_reasons.field.status", "termination_reasons.field.reason",
    "termination_reasons.field.respondent", "termination_reasons.field.survey",
    "termination_reasons.field.timing", "termination_reasons.field.audit",
    "studies.card.revenue", "dashboard.card.revenue",
    "dashboard.card.average_cpi", "dashboard.card.rpc",
    "dashboard.graph.finance_filters",
    "sync.run",
})


def is_super_admin_account(user) -> bool:
    """Return whether the account may access temporarily restricted super-admin areas."""
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True
    profile = EmployeeProfile.objects.select_related("role").filter(user=user).first()
    return bool(profile and profile.role and profile.role.is_active and profile.role.slug in {"super-admin", "superadmin"})


def effective_permission_codes(user) -> set[str]:
    if not user or not user.is_authenticated or not user.is_active:
        return set()
    if user.is_superuser:
        return set(AccessFunction.objects.filter(is_active=True).values_list("code", flat=True))

    profile = EmployeeProfile.objects.select_related("role").filter(user=user).first()
    codes: set[str] = set()
    if profile and profile.role and profile.role.is_active:
        codes.update(
            profile.role.function_assignments.filter(allowed=True, function__is_active=True)
            .values_list("function__code", flat=True)
        )
    for code, effect in user.function_overrides.filter(function__is_active=True).values_list("function__code", "effect"):
        if effect == UserFunctionOverride.Effect.ALLOW:
            codes.add(code)
        else:
            codes.discard(code)
    if profile and profile.account_type == EmployeeProfile.AccountType.EXTERNAL_VENDOR:
        codes.difference_update(EXTERNAL_VENDOR_FORBIDDEN_CODES)
        codes = {code for code in codes if not code.startswith("organization.")}
    return codes


def has_function_access(user, code: str) -> bool:
    return bool(
        user
        and user.is_authenticated
        and user.is_active
        and (
            user.is_superuser
            or code in effective_permission_codes(user)
        )
    )


def function_permission_required(code: str):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not has_function_access(request.user, code):
                raise PermissionDenied(f"You do not have access to {code}.")
            return view_func(request, *args, **kwargs)

        return wrapped
    return decorator


def any_function_permission_required(*codes: str):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not any(has_function_access(request.user, code) for code in codes):
                raise PermissionDenied("You do not have access to this management area.")
            return view_func(request, *args, **kwargs)
        return wrapped
    return decorator


def subordinate_user_ids(user) -> set[int]:
    if not user or not user.is_authenticated:
        return set()
    if user.is_superuser:
        from django.contrib.auth import get_user_model
        return set(get_user_model().objects.values_list("id", flat=True))
    descendants: set[int] = set()
    frontier = {user.id}
    while frontier:
        children = set(EmployeeProfile.objects.filter(created_by_id__in=frontier).values_list("user_id", flat=True)) - descendants
        descendants.update(children)
        frontier = children
    descendants.discard(user.id)
    return descendants


def manageable_user_ids(user) -> set[int]:
    """Subordinates plus members explicitly placed in an internal vendor workspace."""

    ids = subordinate_user_ids(user)
    if not user or not user.is_authenticated or user.is_superuser:
        return ids
    profile = EmployeeProfile.objects.filter(user=user).first()
    if profile and profile.account_type == EmployeeProfile.AccountType.INTERNAL_VENDOR:
        ids.update(
            EmployeeProfile.objects.filter(
                organization_unit__workspace_owner=user,
                account_type=EmployeeProfile.AccountType.EMPLOYEE,
            ).values_list("user_id", flat=True)
        )
    return ids


def activity_visible_user_ids(user) -> set[int]:
    """Return users whose tracking activity is visible to ``user``.

    Shift assignment determines the Team Lead tracking boundary. A Team Lead
    sees lower-ranked employees only inside the exact Shift to which the lead is
    assigned. Managers and higher employee roles retain Branch-wide visibility.
    A normal employee can only see their own tracking records, even when another
    user was created beneath them. Vendor and super-admin workspace rules remain
    intact.
    """
    if not user or not user.is_authenticated:
        return set()
    if user.is_superuser:
        from django.contrib.auth import get_user_model
        return set(get_user_model().objects.values_list("id", flat=True))

    visible_ids = {user.id}
    profile = EmployeeProfile.objects.select_related(
        "role", "organization_unit__workspace_owner",
        "organization_unit__parent", "organization_unit__parent__parent",
    ).filter(user=user).first()
    if (
        not profile
        or not profile.role
    ):
        return visible_ids

    from vendors.access import organization_unit_descendant_ids, vendor_scope_user_id

    vendor_id = vendor_scope_user_id(user)
    if profile.account_type == EmployeeProfile.AccountType.INTERNAL_VENDOR and vendor_id:
        visible_ids.update(subordinate_user_ids(user))
        visible_ids.update(
            EmployeeProfile.objects.filter(
                organization_unit__workspace_owner_id=vendor_id,
                account_type=EmployeeProfile.AccountType.EMPLOYEE,
            ).values_list("user_id", flat=True)
        )
        return visible_ids
    if profile.account_type != EmployeeProfile.AccountType.EMPLOYEE or profile.role.rank < 20:
        return visible_ids

    if profile.organization_unit_id:
        scope_unit = profile.organization_unit
        if profile.role.rank > 20:
            while scope_unit.parent_id and scope_unit.unit_type != "branch":
                scope_unit = scope_unit.parent
        unit_ids = organization_unit_descendant_ids(scope_unit)
        visible_ids.update(
            EmployeeProfile.objects.filter(
                organization_unit_id__in=unit_ids,
                account_type=EmployeeProfile.AccountType.EMPLOYEE,
                role__isnull=False,
                role__rank__lt=profile.role.rank,
            ).values_list("user_id", flat=True)
        )
        return visible_ids

    if not profile.created_by_id:
        visible_ids.update(subordinate_user_ids(user))
        return visible_ids

    lower_rank_peers = EmployeeProfile.objects.filter(
        created_by_id=profile.created_by_id,
        account_type=EmployeeProfile.AccountType.EMPLOYEE,
        role__isnull=False,
        role__rank__lt=profile.role.rank,
    )
    visible_ids.update(lower_rank_peers.values_list("user_id", flat=True))
    return visible_ids


def assignable_functions(user):
    queryset = AccessFunction.objects.filter(is_active=True)
    return queryset if user.is_superuser else queryset.filter(code__in=effective_permission_codes(user))


def assignable_roles(user):
    if user.is_superuser:
        return Role.objects.filter(is_active=True)
    permitted = effective_permission_codes(user)
    role_ids = []
    for role in Role.objects.filter(is_active=True).prefetch_related("function_assignments__function"):
        role_codes = {item.function.code for item in role.function_assignments.all() if item.allowed and item.function.is_active}
        if role_codes.issubset(permitted):
            role_ids.append(role.id)
    return Role.objects.filter(id__in=role_ids)


def can_manage_role(user, role) -> bool:
    return bool(user.is_superuser or (not role.is_system and role.created_by_id == user.id))


class HasFunctionPermission(BasePermission):
    message = "Your account does not have access to this function."

    def has_permission(self, request, view):
        resolver = getattr(view, "get_required_function_permission", None)
        codes = resolver() if resolver else getattr(view, "required_function_permission", None)
        if isinstance(codes, str):
            codes = (codes,)
        return bool(codes and any(has_function_access(request.user, code) for code in codes))
