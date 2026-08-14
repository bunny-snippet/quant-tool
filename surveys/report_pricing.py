"""Permission-aware CPI and supplier labels for Traffic Reports.

The project inventory and respondent reports must show the same commercial
price to a scoped user. Source/client CPI is reserved for platform admins;
other viewers see only the price after their supplier allocation and/or role
visibility percentage has been applied.
"""

from decimal import Decimal, ROUND_HALF_UP

from accounts.models import EmployeeProfile
from vendors.access import is_external_vendor_scope


MONEY_QUANTUM = Decimal("0.01")
ADMIN_ROLE_SLUGS = {"admin", "super-admin"}


def _money(value):
    """Normalize a nullable CPI value without introducing float rounding."""

    if value is None:
        return None
    return Decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _percentage(value):
    """Clamp a configured visibility percentage to the supported 0-100 range."""

    return min(Decimal("100.00"), max(Decimal("0.00"), Decimal(value)))


def apply_percentage(value, percent):
    """Apply a CPI visibility percentage and return a currency-safe value."""

    value = _money(value)
    if value is None:
        return None
    return (value * _percentage(percent) / Decimal("100.00")).quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def role_visibility_percent(user):
    """Return the role percentage that applies to an employee viewer or actor."""

    profile = getattr(user, "employee_profile", None) if user else None
    role = getattr(profile, "role", None) if profile else None
    if profile and profile.account_type == EmployeeProfile.AccountType.EMPLOYEE and role:
        return _percentage(role.cpi_visibility_percent)
    return Decimal("100.00")


def can_view_report_commercials(user):
    """Allow source/supplier CPI columns only to platform admin scopes."""

    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if is_external_vendor_scope(user):
        return False
    profile = getattr(user, "employee_profile", None)
    role = getattr(profile, "role", None) if profile else None
    return bool(role and role.slug in ADMIN_ROLE_SLUGS)


def viewer_attempt_cpi(attempt, viewer, *, current=False):
    """Return the only CPI a non-admin viewer may see for one attempt."""

    source = attempt.survey.cpi if current else attempt.source_cpi_snapshot
    if can_view_report_commercials(viewer):
        return _money(source)

    base = source
    if is_external_vendor_scope(viewer):
        if current:
            cut = Decimal(attempt.cpi_cut_percent_snapshot or 0)
            base = apply_percentage(source, Decimal("100.00") - cut)
        else:
            base = attempt.payable_cpi_snapshot
            if base is None:
                base = source
    return apply_percentage(base, role_visibility_percent(viewer))


def supplier_cpi_for_admin(attempt):
    """Return the supplier/organization payout CPI visible in an admin export."""

    base = attempt.payable_cpi_snapshot
    if base is None:
        base = attempt.source_cpi_snapshot
    return apply_percentage(base, role_visibility_percent(attempt.platform_user))


def supplier_label_for_admin(attempt):
    """Resolve external supplier name or an organization member's Sub-branch."""

    supplier = attempt.vendor
    supplier_profile = getattr(supplier, "employee_profile", None) if supplier else None
    if supplier_profile and supplier_profile.account_type == EmployeeProfile.AccountType.EXTERNAL_VENDOR:
        return supplier.get_full_name() or supplier.username

    actor = attempt.platform_user
    actor_profile = getattr(actor, "employee_profile", None) if actor else None
    if actor_profile and actor_profile.account_type == EmployeeProfile.AccountType.EXTERNAL_VENDOR:
        return actor.get_full_name() or actor.username

    unit = getattr(actor_profile, "organization_unit", None) if actor_profile else None
    visited = set()
    while unit and unit.pk not in visited:
        visited.add(unit.pk)
        if unit.unit_type == "sub_branch":
            return unit.name
        unit = unit.parent

    if supplier:
        return supplier.get_full_name() or supplier.username
    return ""
