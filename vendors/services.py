from datetime import timedelta
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.db.models import (
    Count,
    DecimalField,
    Exists,
    ExpressionWrapper,
    F,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Value,
)
from django.db.models.functions import Coalesce, Round
from django.utils import timezone

from accounts.models import EmployeeProfile
from surveys.models import Survey, SurveyAttempt

from .access import vendor_scope_user_id
from .models import (
    AllocationReservation,
    OrganizationClientAccess,
    OrganizationUnit,
    VendorClientAllocation,
    VendorAPIKey,
    VendorSurveyAllocation,
)


MONEY_QUANTUM = Decimal("0.01")


class AllocationUnavailable(ValueError):
    """Raised when a supplier cannot reserve capacity for a survey."""


@dataclass(frozen=True)
class VendorSurveyContext:
    vendor_id: int
    client_allocation: VendorClientAllocation
    survey_allocation: VendorSurveyAllocation | None
    cpi_cut_percent: Decimal
    payable_cpi: Decimal | None


@dataclass(frozen=True)
class OrganizationClientPolicy:
    """The closest explicit rule for one client in a user's unit ancestry."""
    client_id: int
    is_active: bool
    min_cpi: Decimal | None
    max_cpi: Decimal | None
    source_unit_id: int


def _cpi_in_range(value, minimum, maximum) -> bool:
    if value is None:
        return minimum is None and maximum is None
    return not ((minimum is not None and value < minimum) or (maximum is not None and value > maximum))


def _survey_policy_q(policies, *, cpi_field="cpi"):
    combined = Q(pk__in=[])
    for policy in policies:
        if not policy.is_active:
            continue
        clause = Q(client_id=policy.client_id)
        if policy.min_cpi is not None:
            clause &= Q(**{f"{cpi_field}__gte": policy.min_cpi})
        if policy.max_cpi is not None:
            clause &= Q(**{f"{cpi_field}__lte": policy.max_cpi})
        combined |= clause
    return combined


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
    direct_client_rules: dict[int, dict[int, bool]] = {}
    for unit_id, client_id, is_active in OrganizationClientAccess.objects.filter(
        organization_unit_id__in=unit_ids,
        client__is_active=True,
    ).values_list("organization_unit_id", "client_id", "is_active"):
        direct_client_rules.setdefault(unit_id, {})[client_id] = is_active

    children: dict[int, list[int]] = {}
    for row in units:
        if row["parent_id"] in unit_ids:
            children.setdefault(row["parent_id"], []).append(row["id"])

    member_rollups: dict[int, int] = {}
    client_rollups: dict[int, set[int]] = {}
    effective_clients: dict[int, set[int]] = {}

    def apply_inheritance(unit_id: int, inherited: dict[int, bool], trail=frozenset()):
        if unit_id in trail:
            return
        policies = dict(inherited)
        policies.update(direct_client_rules.get(unit_id, {}))
        effective_clients[unit_id] = {client_id for client_id, allowed in policies.items() if allowed}
        for child_id in children.get(unit_id, []):
            apply_inheritance(child_id, policies, trail | {unit_id})

    roots = [row["id"] for row in units if row["parent_id"] not in unit_ids]
    for root_id in roots:
        apply_inheritance(root_id, {})

    def visit(unit_id: int, trail: frozenset[int] = frozenset()) -> tuple[int, set[int]]:
        if unit_id in member_rollups:
            return member_rollups[unit_id], client_rollups[unit_id]
        if unit_id in trail:
            return direct_members.get(unit_id, 0), set(effective_clients.get(unit_id, set()))
        members = direct_members.get(unit_id, 0)
        clients = set(effective_clients.get(unit_id, set()))
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
        "direct_clients": {
            unit_id: sum(1 for allowed in direct_client_rules.get(unit_id, {}).values() if allowed)
            for unit_id in unit_ids
        },
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


def organization_client_policies_for_user(user) -> dict[int, OrganizationClientPolicy] | None:
    """Resolve per-client Branch -> Sub-branch -> Shift inheritance."""
    if hasattr(user, "_organization_client_policies_cache"):
        return user._organization_client_policies_cache
    profile = EmployeeProfile.objects.select_related(
        "organization_unit__parent__parent"
    ).filter(user=user).first()
    if not profile or not profile.organization_unit_id:
        user._organization_client_policies_cache = None
        return None
    current_unit = profile.organization_unit
    ancestry = []
    visited_units = set()
    while current_unit and current_unit.pk not in visited_units:
        visited_units.add(current_unit.pk)
        if not current_unit.is_active:
            user._organization_client_policies_cache = {}
            return {}
        ancestry.append(current_unit.pk)
        current_unit = current_unit.parent
    rules = OrganizationClientAccess.objects.filter(
        organization_unit_id__in=ancestry, client__is_active=True,
    ).values("organization_unit_id", "client_id", "is_active", "min_cpi", "max_cpi")
    rules_by_unit = {}
    for rule in rules:
        rules_by_unit.setdefault(rule["organization_unit_id"], []).append(rule)
    policies = {}
    for unit_id in reversed(ancestry):
        for rule in rules_by_unit.get(unit_id, []):
            policies[rule["client_id"]] = OrganizationClientPolicy(
                client_id=rule["client_id"], is_active=rule["is_active"],
                min_cpi=rule["min_cpi"], max_cpi=rule["max_cpi"], source_unit_id=unit_id,
            )
    user._organization_client_policies_cache = policies
    return policies


def organization_client_ids_for_user(user) -> set[int] | None:
    policies = organization_client_policies_for_user(user)
    if policies is None:
        return None
    return {client_id for client_id, policy in policies.items() if policy.is_active}


def scope_surveys_for_user(queryset, user):
    """Expose every project under a client grant; project rules are overrides."""

    vendor_id = vendor_scope_user_id(user)
    organization_policies = organization_client_policies_for_user(user)
    if not vendor_id:
        if organization_policies is None:
            return queryset
        return queryset.filter(_survey_policy_q(organization_policies.values())).distinct()
    now = timezone.now()
    client_allocations = list(
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
    if not client_allocations:
        return queryset.none()
    allocation_ids = [row.pk for row in client_allocations]
    supplier_policies = [OrganizationClientPolicy(
        client_id=row.client_id, is_active=True, min_cpi=row.min_cpi,
        max_cpi=row.max_cpi, source_unit_id=0,
    ) for row in client_allocations]
    all_project_rules = VendorSurveyAllocation.objects.filter(
        client_allocation_id__in=allocation_ids, survey_id=OuterRef("pk")
    )
    available_rules = (
        VendorSurveyAllocation.objects.filter(client_allocation_id__in=allocation_ids, is_active=True)
        .filter(_active_window_q(now))
        .filter(_available_quantity_q())
        .select_related("client_allocation", "client_allocation__vendor", "client_allocation__vendor__employee_profile")
    )
    scoped = (
        queryset.filter(_survey_policy_q(supplier_policies), remaining__gt=0)
        .annotate(
            request_has_project_rule=Exists(all_project_rules),
            request_has_available_project_rule=Exists(available_rules.filter(survey_id=OuterRef("pk"))),
        )
        .filter(Q(request_has_project_rule=False) | Q(request_has_available_project_rule=True))
        .prefetch_related(
            Prefetch(
                "client__vendor_allocations",
                queryset=VendorClientAllocation.objects.filter(pk__in=allocation_ids).select_related(
                    "vendor", "vendor__employee_profile", "vendor__vendor_commercial_profile", "client"
                ),
                to_attr="request_vendor_allocations",
            ),
            Prefetch("vendor_allocations", queryset=available_rules, to_attr="request_vendor_survey_allocations"),
        )
        .distinct()
    )
    if organization_policies is not None:
        scoped = scoped.filter(_survey_policy_q(organization_policies.values()))
    return scoped


def scope_surveys_for_api_key(queryset, api_key):
    """Apply the client grants explicitly selected when an API key was issued."""

    if not isinstance(api_key, VendorAPIKey):
        return queryset
    client_ids = api_key.client_allocations.filter(
        vendor_id=api_key.vendor_id,
        is_active=True,
        client__is_active=True,
    ).values("client_id")
    return queryset.filter(client_id__in=Subquery(client_ids)).distinct()


def resolve_vendor_survey_context(user, survey: Survey, *, require_capacity=True, for_update=False):
    """Resolve the supplier client grant and optional per-project override."""

    organization_policies = organization_client_policies_for_user(user)
    if organization_policies is not None:
        organization_policy = organization_policies.get(survey.client_id)
        if not organization_policy or not organization_policy.is_active:
            raise AllocationUnavailable("This client is not assigned to the user's organization unit.")
        if not _cpi_in_range(survey.cpi, organization_policy.min_cpi, organization_policy.max_cpi):
            raise AllocationUnavailable("This project's CPI is outside the organization unit's allowed range.")
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
    if not _cpi_in_range(survey.cpi, client_allocation.min_cpi, client_allocation.max_cpi):
        raise AllocationUnavailable("This project's CPI is outside the supplier's client policy.")
    if require_capacity and client_allocation.remaining_quantity < 1:
        raise AllocationUnavailable("Client quantity is exhausted.")

    survey_queryset = VendorSurveyAllocation.objects.select_related("client_allocation")
    if for_update:
        survey_queryset = survey_queryset.select_for_update()
    survey_allocation = survey_queryset.filter(
        client_allocation=client_allocation,
        survey=survey,
    ).first()
    if survey_allocation and not _is_active_now(survey_allocation, now):
        raise AllocationUnavailable("This project is disabled or outside its allocation dates.")
    if require_capacity and survey_allocation and survey_allocation.remaining_quantity < 1:
        raise AllocationUnavailable("Project complete cap is exhausted.")
    if require_capacity and survey.remaining < 1:
        raise AllocationUnavailable("Upstream survey quantity is exhausted.")

    account_type = client_allocation.vendor.employee_profile.account_type
    cut = survey_allocation.effective_cpi_cut_percent if survey_allocation else client_allocation.effective_cpi_cut_percent
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


def annotate_survey_pricing_for_user(queryset, user, *, alias="visible_cpi"):
    """Annotate the exact rounded CPI displayed/exported for this viewer."""

    money_field = DecimalField(max_digits=18, decimal_places=2)
    calculation_field = DecimalField(max_digits=20, decimal_places=8)
    cut_field = DecimalField(max_digits=7, decimal_places=4)
    hundred = Value(Decimal("100.00"), output_field=cut_field)
    zero_cut = Value(Decimal("0.00"), output_field=cut_field)

    profile = EmployeeProfile.objects.select_related("role").filter(user=user).first()
    role_percent = Decimal("100.00")
    if (
        not getattr(user, "is_superuser", False)
        and profile
        and profile.account_type == EmployeeProfile.AccountType.EMPLOYEE
        and profile.role_id
    ):
        role_percent = min(
            Decimal("100.00"),
            max(Decimal("0.00"), Decimal(profile.role.cpi_visibility_percent)),
        )

    vendor_id = vendor_scope_user_id(user)
    cut_expression = zero_cut
    if vendor_id:
        vendor_profile = EmployeeProfile.objects.filter(user_id=vendor_id).first()
        if vendor_profile and vendor_profile.account_type != EmployeeProfile.AccountType.INTERNAL_VENDOR:
            now = timezone.now()
            client_cuts = (
                VendorClientAllocation.objects.filter(
                    vendor_id=vendor_id,
                    vendor__is_active=True,
                    vendor__vendor_commercial_profile__is_active=True,
                    client_id=OuterRef("client_id"),
                    client__is_active=True,
                    is_active=True,
                )
                .filter(_active_window_q(now))
                .filter(_available_quantity_q())
                .annotate(
                    resolved_cut=Coalesce(
                        "cpi_cut_override_percent",
                        "vendor__vendor_commercial_profile__default_cpi_cut_percent",
                        zero_cut,
                        output_field=cut_field,
                    )
                )
                .values("resolved_cut")[:1]
            )
            project_cuts = (
                VendorSurveyAllocation.objects.filter(
                    client_allocation__vendor_id=vendor_id,
                    client_allocation__vendor__is_active=True,
                    client_allocation__vendor__vendor_commercial_profile__is_active=True,
                    client_allocation__client_id=OuterRef("client_id"),
                    client_allocation__client__is_active=True,
                    client_allocation__is_active=True,
                    survey_id=OuterRef("pk"),
                    is_active=True,
                )
                .filter(_active_window_q(now))
                .filter(_available_quantity_q())
                .filter(_active_window_q(now, "client_allocation__"))
                .filter(_available_quantity_q("client_allocation__"))
                .annotate(
                    resolved_cut=Coalesce(
                        "cpi_cut_override_percent",
                        "client_allocation__cpi_cut_override_percent",
                        "client_allocation__vendor__vendor_commercial_profile__default_cpi_cut_percent",
                        zero_cut,
                        output_field=cut_field,
                    )
                )
                .values("resolved_cut")[:1]
            )
            cut_expression = Coalesce(
                Subquery(project_cuts, output_field=cut_field),
                Subquery(client_cuts, output_field=cut_field),
                zero_cut,
                output_field=cut_field,
            )

    after_allocation_cut = ExpressionWrapper(
        F("cpi") * (hundred - cut_expression) / hundred,
        output_field=calculation_field,
    )
    visible_expression = ExpressionWrapper(
        after_allocation_cut * Value(role_percent, output_field=cut_field) / hundred,
        output_field=calculation_field,
    )
    return queryset.annotate(
        **{
            alias: Round(
                visible_expression,
                precision=2,
                output_field=money_field,
            )
        }
    )


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

    if client_allocation is None and survey_allocation is not None:
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
    if locked_survey_allocation and attempt.survey_id != locked_survey_allocation.survey_id:
        raise AllocationUnavailable("Attempt survey does not match the assigned survey.")
    if attempt.survey.client_id != client_allocation.client_id:
        raise AllocationUnavailable("Survey is not mapped to the allocation's client.")
    if not _is_active_now(client_allocation, now):
        raise AllocationUnavailable("Client allocation is inactive or outside its active dates.")
    if locked_survey_allocation and not _is_active_now(locked_survey_allocation, now):
        raise AllocationUnavailable("Project allocation is inactive or outside its active dates.")
    if client_allocation.remaining_quantity < 1:
        raise AllocationUnavailable("Client quantity is exhausted.")
    if locked_survey_allocation and locked_survey_allocation.remaining_quantity < 1:
        raise AllocationUnavailable("Project complete cap is exhausted.")
    if attempt.survey.remaining < 1:
        raise AllocationUnavailable("Upstream survey quantity is exhausted.")

    vendor_profile = client_allocation.vendor.employee_profile
    cut = locked_survey_allocation.effective_cpi_cut_percent if locked_survey_allocation else client_allocation.effective_cpi_cut_percent
    if vendor_profile.account_type == EmployeeProfile.AccountType.INTERNAL_VENDOR:
        cut = Decimal("0.00")
    source_cpi = attempt.survey.cpi
    final_cpi = payable_cpi(source_cpi, cut)

    client_allocation.reserved_quantity += 1
    client_allocation.save(update_fields=["reserved_quantity", "updated_at"])
    if locked_survey_allocation:
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
