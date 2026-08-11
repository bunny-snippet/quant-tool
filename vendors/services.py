from datetime import timedelta
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Prefetch, Q
from django.utils import timezone

from accounts.models import EmployeeProfile
from surveys.models import Survey, SurveyAttempt

from .access import vendor_scope_user_id
from .models import (
    AllocationReservation,
    OrganizationClientAccess,
    OrganizationUnit,
    VendorClientAllocation,
    VendorSurveyAllocation,
)


MONEY_QUANTUM = Decimal("0.01")


class AllocationUnavailable(ValueError):
    """Raised when a supplier cannot reserve capacity for a survey."""


@dataclass(frozen=True)
class VendorSurveyContext:
    vendor_id: int
    client_allocation: VendorClientAllocation
    survey_allocation: VendorSurveyAllocation
    cpi_cut_percent: Decimal
    payable_cpi: Decimal | None


def organization_unit_rollup_counts(workspace_owner_ids) -> dict[str, dict[int, int]]:
    """Aggregate unique members and client grants from children into each parent."""

    units = list(
        OrganizationUnit.objects.filter(workspace_owner_id__in=workspace_owner_ids)
        .values("id", "parent_id")
    )
    unit_ids = {row["id"] for row in units}
    direct_members = {
        row["organization_unit_id"]: row["total"]
        for row in EmployeeProfile.objects.filter(organization_unit_id__in=unit_ids)
        .values("organization_unit_id")
        .annotate(total=Count("id"))
    }
    direct_clients: dict[int, set[int]] = {}
    for unit_id, client_id in OrganizationClientAccess.objects.filter(
        organization_unit_id__in=unit_ids,
        is_active=True,
        client__is_active=True,
    ).values_list("organization_unit_id", "client_id"):
        direct_clients.setdefault(unit_id, set()).add(client_id)

    children: dict[int, list[int]] = {}
    for row in units:
        if row["parent_id"] in unit_ids:
            children.setdefault(row["parent_id"], []).append(row["id"])

    member_rollups: dict[int, int] = {}
    client_rollups: dict[int, set[int]] = {}

    def visit(unit_id: int, trail: frozenset[int] = frozenset()) -> tuple[int, set[int]]:
        if unit_id in member_rollups:
            return member_rollups[unit_id], client_rollups[unit_id]
        if unit_id in trail:
            return direct_members.get(unit_id, 0), set(direct_clients.get(unit_id, set()))
        members = direct_members.get(unit_id, 0)
        clients = set(direct_clients.get(unit_id, set()))
        next_trail = trail | {unit_id}
        for child_id in children.get(unit_id, []):
            child_members, child_clients = visit(child_id, next_trail)
            members += child_members
            clients.update(child_clients)
        member_rollups[unit_id] = members
        client_rollups[unit_id] = clients
        return members, clients

    for unit_id in unit_ids:
        visit(unit_id)

    return {
        "members": member_rollups,
        "clients": {unit_id: len(client_ids) for unit_id, client_ids in client_rollups.items()},
        "direct_members": {unit_id: direct_members.get(unit_id, 0) for unit_id in unit_ids},
        "direct_clients": {unit_id: len(direct_clients.get(unit_id, set())) for unit_id in unit_ids},
    }


def payable_cpi(source_cpi, cut_percent) -> Decimal | None:
    if source_cpi is None:
        return None
    source = Decimal(source_cpi)
    cut = Decimal(cut_percent or 0)
    return (source * (Decimal("100") - cut) / Decimal("100")).quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def _is_active_now(allocation, now) -> bool:
    return bool(
        allocation.is_active
        and (allocation.starts_at is None or allocation.starts_at <= now)
        and (allocation.ends_at is None or allocation.ends_at > now)
    )


def _active_window_q(now, prefix="") -> Q:
    start = f"{prefix}starts_at"
    end = f"{prefix}ends_at"
    return (Q(**{f"{start}__isnull": True}) | Q(**{f"{start}__lte": now})) & (
        Q(**{f"{end}__isnull": True}) | Q(**{f"{end}__gt": now})
    )


def _available_quantity_q(prefix="") -> Q:
    return Q(**{
        f"{prefix}quantity_limit__gt": F(f"{prefix}consumed_quantity") + F(f"{prefix}reserved_quantity")
    })


def organization_client_ids_for_user(user) -> set[int] | None:
    """Return the nearest explicit unit grant set for an assigned employee.

    An exact Shift grant overrides broader Sub-branch/Branch grants. When the
    Shift has no direct grants, the closest ancestor with grants is inherited.
    Root and unassigned accounts remain unscoped for backward compatibility.
    """

    if hasattr(user, "_organization_client_ids_cache"):
        return user._organization_client_ids_cache
    profile = EmployeeProfile.objects.select_related(
        "organization_unit__parent__parent"
    ).filter(user=user).first()
    if not profile or not profile.organization_unit_id:
        user._organization_client_ids_cache = None
        return None
    current_unit = profile.organization_unit
    visited_units = set()
    while current_unit and current_unit.pk not in visited_units:
        visited_units.add(current_unit.pk)
        if not current_unit.is_active:
            user._organization_client_ids_cache = set()
            return set()
        direct_client_ids = set(
            OrganizationClientAccess.objects.filter(
                organization_unit=current_unit,
                client__is_active=True,
                is_active=True,
            ).values_list("client_id", flat=True)
        )
        if direct_client_ids:
            user._organization_client_ids_cache = direct_client_ids
            return direct_client_ids
        current_unit = current_unit.parent
    user._organization_client_ids_cache = set()
    return set()


def scope_surveys_for_user(queryset, user):
    """Expose only explicitly allocated projects with client and project capacity."""

    vendor_id = vendor_scope_user_id(user)
    organization_client_ids = organization_client_ids_for_user(user)
    if not vendor_id:
        if organization_client_ids is None:
            return queryset
        return queryset.filter(client_id__in=organization_client_ids).distinct()
    now = timezone.now()
    client_allocations = (
        VendorClientAllocation.objects.filter(
            vendor_id=vendor_id,
            vendor__is_active=True,
            vendor__vendor_commercial_profile__is_active=True,
            client__is_active=True,
            is_active=True,
        )
        .filter(_active_window_q(now))
        .filter(_available_quantity_q())
        .select_related("vendor", "vendor__employee_profile", "vendor__vendor_commercial_profile", "client")
    )
    available_rules = (
        VendorSurveyAllocation.objects.filter(client_allocation__in=client_allocations, is_active=True)
        .filter(_active_window_q(now))
        .filter(_available_quantity_q())
        .select_related("client_allocation", "client_allocation__vendor", "client_allocation__vendor__employee_profile")
    )
    scoped = (
        queryset.filter(vendor_allocations__in=available_rules, remaining__gt=0)
        .prefetch_related(
            Prefetch("client__vendor_allocations", queryset=client_allocations, to_attr="request_vendor_allocations"),
            Prefetch("vendor_allocations", queryset=available_rules, to_attr="request_vendor_survey_allocations"),
        )
        .distinct()
    )
    if organization_client_ids is not None:
        scoped = scoped.filter(client_id__in=organization_client_ids)
    return scoped


def resolve_vendor_survey_context(user, survey: Survey, *, require_capacity=True, for_update=False):
    """Resolve the supplier's active client grant and mandatory project allocation."""

    organization_client_ids = organization_client_ids_for_user(user)
    if organization_client_ids is not None and survey.client_id not in organization_client_ids:
        raise AllocationUnavailable("This client is not assigned to the user's organization unit.")
    vendor_id = vendor_scope_user_id(user)
    if not vendor_id:
        return None
    now = timezone.now()
    client_queryset = VendorClientAllocation.objects.select_related(
        "vendor", "vendor__employee_profile", "vendor__vendor_commercial_profile", "client"
    )
    if for_update:
        client_queryset = client_queryset.select_for_update()
    client_allocation = client_queryset.filter(
        vendor_id=vendor_id,
        vendor__is_active=True,
        vendor__vendor_commercial_profile__is_active=True,
        client__is_active=True,
        client_id=survey.client_id,
        is_active=True,
    ).first()
    if not client_allocation or not _is_active_now(client_allocation, now):
        raise AllocationUnavailable("This client is not allocated to the supplier.")
    if require_capacity and client_allocation.remaining_quantity < 1:
        raise AllocationUnavailable("Client quantity is exhausted.")

    survey_queryset = VendorSurveyAllocation.objects.select_related("client_allocation")
    if for_update:
        survey_queryset = survey_queryset.select_for_update()
    survey_allocation = survey_queryset.filter(
        client_allocation=client_allocation,
        survey=survey,
    ).first()
    if not survey_allocation:
        raise AllocationUnavailable("This project is not allocated to the supplier.")
    if not _is_active_now(survey_allocation, now):
        raise AllocationUnavailable("This project is disabled or outside its allocation dates.")
    if require_capacity and survey_allocation.remaining_quantity < 1:
        raise AllocationUnavailable("Project complete cap is exhausted.")
    if require_capacity and survey.remaining < 1:
        raise AllocationUnavailable("Upstream survey quantity is exhausted.")

    account_type = client_allocation.vendor.employee_profile.account_type
    cut = survey_allocation.effective_cpi_cut_percent
    if account_type == EmployeeProfile.AccountType.INTERNAL_VENDOR:
        cut = Decimal("0.00")
    return VendorSurveyContext(
        vendor_id=vendor_id,
        client_allocation=client_allocation,
        survey_allocation=survey_allocation,
        cpi_cut_percent=cut,
        payable_cpi=payable_cpi(survey.cpi, cut),
    )


def survey_pricing_for_user(user, survey: Survey) -> tuple[Decimal | None, Decimal | None]:
    """Return request-visible CPI and applied cut without exposing source CPI to external suppliers."""

    def apply_employee_role_percentage(price, existing_cut):
        profile = getattr(user, "employee_profile", None)
        role = getattr(profile, "role", None) if profile else None
        if (
            not getattr(user, "is_superuser", False)
            and profile
            and profile.account_type == EmployeeProfile.AccountType.EMPLOYEE
            and role
        ):
            visible_percent = min(Decimal("100.00"), max(Decimal("0.00"), role.cpi_visibility_percent))
            role_cut = Decimal("100.00") - visible_percent
            base_cut = existing_cut or Decimal("0.00")
            combined_cut = Decimal("100.00") - (
                (Decimal("100.00") - base_cut) * visible_percent / Decimal("100.00")
            )
            return payable_cpi(price, role_cut), combined_cut.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        return price, existing_cut

    if not vendor_scope_user_id(user):
        return apply_employee_role_percentage(survey.cpi, None)
    client_allocations = getattr(getattr(survey, "client", None), "request_vendor_allocations", None)
    survey_allocations = getattr(survey, "request_vendor_survey_allocations", None)
    if client_allocations:
        client_allocation = client_allocations[0]
        survey_allocation = survey_allocations[0] if survey_allocations else None
        cut = survey_allocation.effective_cpi_cut_percent if survey_allocation else client_allocation.effective_cpi_cut_percent
        if client_allocation.vendor.employee_profile.account_type == EmployeeProfile.AccountType.INTERNAL_VENDOR:
            cut = Decimal("0.00")
        return apply_employee_role_percentage(payable_cpi(survey.cpi, cut), cut)
    context = resolve_vendor_survey_context(user, survey, require_capacity=False)
    return apply_employee_role_percentage(context.payable_cpi, context.cpi_cut_percent)


@transaction.atomic
def reserve_attempt_capacity(
    attempt: SurveyAttempt,
    survey_allocation: VendorSurveyAllocation | None = None,
    *,
    client_allocation: VendorClientAllocation | None = None,
    expires_at=None,
) -> AllocationReservation:
    """Reserve one unit and freeze the attempt's supplier/client/CPI context.

    The caller may wrap attempt creation and this function in an outer atomic
    transaction when enforcement is connected to the respondent start flow.
    """

    attempt = SurveyAttempt.objects.select_for_update().select_related("survey").get(pk=attempt.pk)
    existing = AllocationReservation.objects.filter(attempt=attempt).first()
    if existing:
        return existing

    if survey_allocation is None:
        raise AllocationUnavailable("An explicit project allocation is required.")
    if client_allocation is None:
        client_allocation = survey_allocation.client_allocation
    if client_allocation is None:
        raise AllocationUnavailable("A client allocation is required.")
    client_allocation = (
        VendorClientAllocation.objects.select_for_update()
        .select_related("vendor", "vendor__employee_profile", "vendor__vendor_commercial_profile", "client")
        .get(pk=client_allocation.pk)
    )
    locked_survey_allocation = None
    if survey_allocation is not None:
        locked_survey_allocation = (
            VendorSurveyAllocation.objects.select_for_update()
            .select_related("survey", "client_allocation")
            .get(pk=survey_allocation.pk)
        )
    now = timezone.now()

    commercial_profile = getattr(client_allocation.vendor, "vendor_commercial_profile", None)
    if not client_allocation.vendor.is_active or not client_allocation.client.is_active:
        raise AllocationUnavailable("Supplier or client access is inactive.")
    if not commercial_profile or not commercial_profile.is_active:
        raise AllocationUnavailable("Supplier commercial access is inactive.")
    if attempt.survey_id != locked_survey_allocation.survey_id:
        raise AllocationUnavailable("Attempt survey does not match the assigned survey.")
    if attempt.survey.client_id != client_allocation.client_id:
        raise AllocationUnavailable("Survey is not mapped to the allocation's client.")
    if not _is_active_now(client_allocation, now):
        raise AllocationUnavailable("Client allocation is inactive or outside its active dates.")
    if not _is_active_now(locked_survey_allocation, now):
        raise AllocationUnavailable("Project allocation is inactive or outside its active dates.")
    if client_allocation.remaining_quantity < 1:
        raise AllocationUnavailable("Client quantity is exhausted.")
    if locked_survey_allocation.remaining_quantity < 1:
        raise AllocationUnavailable("Project complete cap is exhausted.")
    if attempt.survey.remaining < 1:
        raise AllocationUnavailable("Upstream survey quantity is exhausted.")

    vendor_profile = client_allocation.vendor.employee_profile
    cut = locked_survey_allocation.effective_cpi_cut_percent
    if vendor_profile.account_type == EmployeeProfile.AccountType.INTERNAL_VENDOR:
        cut = Decimal("0.00")
    source_cpi = attempt.survey.cpi
    final_cpi = payable_cpi(source_cpi, cut)

    client_allocation.reserved_quantity += 1
    client_allocation.save(update_fields=["reserved_quantity", "updated_at"])
    locked_survey_allocation.reserved_quantity += 1
    locked_survey_allocation.save(update_fields=["reserved_quantity", "updated_at"])

    SurveyAttempt.objects.filter(pk=attempt.pk).update(
        vendor=client_allocation.vendor,
        client=client_allocation.client,
        client_allocation=client_allocation,
        survey_allocation=locked_survey_allocation,
        source_cpi_snapshot=source_cpi,
        cpi_snapshot_source="captured",
        cpi_cut_percent_snapshot=cut,
        payable_cpi_snapshot=final_cpi,
        cpi_currency_snapshot=(
            commercial_profile.currency
            if commercial_profile
            else "USD"
        ),
    )
    attempt.refresh_from_db()
    return AllocationReservation.objects.create(
        attempt=attempt,
        client_allocation=client_allocation,
        survey_allocation=locked_survey_allocation,
        quantity=1,
        expires_at=expires_at or now + timedelta(minutes=settings.VENDOR_RESERVATION_TTL_MINUTES),
    )


@transaction.atomic
def finalize_attempt_capacity(attempt: SurveyAttempt) -> AllocationReservation | None:
    """Consume a completion or release every other terminal outcome, idempotently."""

    reservation = (
        AllocationReservation.objects.select_for_update()
        .filter(attempt=attempt)
        .first()
    )
    if not reservation or reservation.status != AllocationReservation.Status.RESERVED:
        return reservation

    client_allocation = VendorClientAllocation.objects.select_for_update().get(pk=reservation.client_allocation_id)
    survey_allocation = (
        VendorSurveyAllocation.objects.select_for_update().get(pk=reservation.survey_allocation_id)
        if reservation.survey_allocation_id
        else None
    )
    quantity = reservation.quantity
    if client_allocation.reserved_quantity < quantity or (
        survey_allocation and survey_allocation.reserved_quantity < quantity
    ):
        raise RuntimeError("Allocation counters are inconsistent with the reservation.")

    client_allocation.reserved_quantity -= quantity
    if survey_allocation:
        survey_allocation.reserved_quantity -= quantity
    if attempt.status == SurveyAttempt.Status.COMPLETED:
        client_allocation.consumed_quantity += quantity
        if survey_allocation:
            survey_allocation.consumed_quantity += quantity
        reservation.status = AllocationReservation.Status.CONSUMED
        reservation.reason = "Completed survey"
    else:
        reservation.status = AllocationReservation.Status.RELEASED
        reservation.reason = f"Released for attempt status {attempt.status}"

    client_allocation.save(update_fields=["reserved_quantity", "consumed_quantity", "updated_at"])
    if survey_allocation:
        survey_allocation.save(update_fields=["reserved_quantity", "consumed_quantity", "updated_at"])
    reservation.finalized_at = timezone.now()
    reservation.save(update_fields=["status", "reason", "finalized_at", "updated_at"])
    return reservation


@transaction.atomic
def expire_reservation(reservation: AllocationReservation) -> AllocationReservation:
    locked = AllocationReservation.objects.select_for_update().get(pk=reservation.pk)
    if locked.status != AllocationReservation.Status.RESERVED:
        return locked
    if locked.expires_at > timezone.now():
        raise AllocationUnavailable("Reservation has not expired yet.")

    client_allocation = VendorClientAllocation.objects.select_for_update().get(pk=locked.client_allocation_id)
    survey_allocation = (
        VendorSurveyAllocation.objects.select_for_update().get(pk=locked.survey_allocation_id)
        if locked.survey_allocation_id
        else None
    )
    quantity = locked.quantity
    if client_allocation.reserved_quantity < quantity or (
        survey_allocation and survey_allocation.reserved_quantity < quantity
    ):
        raise RuntimeError("Allocation counters are inconsistent with the reservation.")
    client_allocation.reserved_quantity -= quantity
    if survey_allocation:
        survey_allocation.reserved_quantity -= quantity
    client_allocation.save(update_fields=["reserved_quantity", "updated_at"])
    if survey_allocation:
        survey_allocation.save(update_fields=["reserved_quantity", "updated_at"])
    locked.status = AllocationReservation.Status.EXPIRED
    locked.reason = "Reservation expired before a terminal callback"
    locked.finalized_at = timezone.now()
    locked.save(update_fields=["status", "reason", "finalized_at", "updated_at"])
    return locked
