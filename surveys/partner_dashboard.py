"""Read-only client/supplier analytics, using the Overview visibility contract."""

from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import PermissionDenied
from django.db.models import Case, CharField, Count, F, IntegerField, Max, Min, Q, Sum, Value, When
from django.db.models.functions import Coalesce, Concat, NullIf
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from accounts.access import effective_permission_codes, function_permission_required
from .dashboard import COMPLETED, _visible_revenue, dashboard_attempts, dashboard_comparison_window, dashboard_range_window
from .partner_report_cache import cached_report_payload


UNIT = "platform_user__employee_profile__organization_unit"
METRICS = {
    "hits": "dashboard.card.hits", "completes": "dashboard.card.completes",
    "accepted": "dashboard.card.completes", "rejected": "dashboard.card.completes",
    "pending": "dashboard.card.completes", "share": "dashboard.card.completes",
    "conversion": "dashboard.card.conversion", "revenue": "dashboard.card.revenue",
    "rpc": "dashboard.card.rpc", "surveys": "dashboard.card.hits",
}


def component_access(user, section):
    codes = effective_permission_codes(user)
    allowed = lambda code: user.is_superuser or code in codes
    access = {key: allowed(code) for key, code in METRICS.items()}
    access.update(
        trend=allowed("dashboard.chart.performance"),
        review=allowed("dashboard.chart.status") and access["completes"],
        table=allowed("dashboard.chart.client_share" if section == "client" else "dashboard.chart.top_users"),
        date=allowed("dashboard.filter.date"),
        # Existing dashboard partner-selector grant applies on both dashboard pages.
        partner=allowed("dashboard.filter.client"),
        segment=allowed("dashboard.filter.country" if section == "client" else "dashboard.filter.branch"),
    )
    return access


def _dimensioned(queryset, section):
    if section == "client":
        return queryset.annotate(
            partner_id=Coalesce("survey__client_id", Value(0)),
            partner_name=Coalesce(NullIf("survey__client__name", Value("")), Value("Unassigned client")),
            segment_id=Coalesce(NullIf("survey__country_code", Value("")), Value("unknown")),
            segment_name=Coalesce(NullIf("survey__country", Value("")), NullIf("survey__country_code", Value("")), Value("Unassigned country")),
        )
    branches = {"branch": UNIT, "sub_branch": UNIT + "__parent", "shift": UNIT + "__parent__parent"}
    return queryset.annotate(
        partner_id=Coalesce("vendor_id", Value(0)),
        partner_name=Coalesce(
            NullIf("vendor__employee_profile__company_name", Value("")),
            NullIf(Concat("vendor__first_name", Value(" "), "vendor__last_name"), Value(" ")),
            NullIf("vendor__username", Value("")), Value("Direct traffic"),
        ),
        segment_id=Coalesce(Case(*[
            When(**{UNIT + "__unit_type": kind}, then=F(path + "__id"))
            for kind, path in branches.items()
        ], output_field=IntegerField()), Value(0)),
        segment_name=Coalesce(Case(*[
            When(**{UNIT + "__unit_type": kind}, then=F(path + "__name"))
            for kind, path in branches.items()
        ], output_field=CharField()), Value("Unassigned branch")),
    )


def _aggregates():
    completed = Q(status=COMPLETED)
    return {
        "hits": Count("id"), "completes": Count("id", filter=completed),
        "accepted": Count("id", filter=completed & Q(final_id_status__status="accepted")),
        "rejected": Count("id", filter=completed & Q(final_id_status__status="rejected")),
        "revenue": Sum("source_cpi_snapshot", filter=completed, default=Decimal("0.00")),
        "currency_min": Min("cpi_currency_snapshot", filter=completed),
        "currency_max": Max("cpi_currency_snapshot", filter=completed),
        "missing_cpi": Count("id", filter=completed & Q(source_cpi_snapshot__isnull=True)),
        "surveys": Count("survey_id", distinct=True),
    }


def _metrics(row, user, access, total_completes=None):
    row = dict(row)
    row["pending"] = row["completes"] - row["accepted"] - row["rejected"]
    row["conversion"] = round(row["completes"] / row["hits"] * 100, 2) if row["hits"] else 0
    row["share"] = round(row["completes"] / total_completes * 100, 2) if total_completes else 0
    currency = row.pop("currency_min")
    mixed = currency != row.pop("currency_max")
    missing = row.pop("missing_cpi")
    # Never silently sum different currencies, missing snapshots, or unknown currency.
    money_available = not mixed and not missing and (not row["completes"] or bool(currency))
    revenue = _visible_revenue(user, row["revenue"]) if money_available else None
    row["revenue"] = revenue
    row["rpc"] = (revenue / row["hits"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if revenue is not None and row["hits"] else (Decimal("0.00") if revenue is not None else None)
    row["currency"] = currency if money_available else None
    row["money_note"] = "Mixed currencies or missing CPI/currency snapshots" if not money_available else ""
    if not (access["revenue"] or access["rpc"]):
        row["currency"], row["money_note"] = None, ""
    for key in METRICS:
        if not access[key]:
            row[key] = None
    return row


def build_partner_payload(user, params, section, access, now=None):
    range_key = params.get("range") or "7d"
    if range_key not in {"24h", "7d", "month"}:
        raise ValueError("Select 24h, 7d or month.")
    for key in ("partner", "segment"):
        if params.get(key) and not access[key]:
            raise PermissionDenied("You cannot use this dashboard filter.")
    if range_key != "7d" and not access["date"]:
        raise PermissionDenied("You cannot change the dashboard date range.")
    partner = params.get("partner", "")
    segment = params.get("segment", "")
    if partner and (not partner.isascii() or not partner.isdigit() or len(partner) > 18):
        raise ValueError("Invalid partner selection.")
    if segment and (len(segment) > 12 or (section == "supplier" and (not segment.isascii() or not segment.isdigit()))):
        raise ValueError("Invalid country or branch selection.")
    window = dashboard_range_window(range_key, now=now)
    comparison = dashboard_comparison_window(window)
    base = _dimensioned(dashboard_attempts(user, {}).select_related(None).order_by(), section)
    # Metadata is bounded to the current/comparison cohort, never global users/clients.
    bounded = base.filter(initiated_at__gte=comparison["start"], initiated_at__lt=window["end"])
    options = {"partners": [], "segments": []}
    if access["partner"] or access["segment"]:
        dimensions = bounded.values("partner_id", "partner_name", "segment_id", "segment_name").distinct()
        partners, segments = {}, {}
        for row in dimensions:
            partners[str(row["partner_id"])] = row["partner_name"]
            segments[str(row["segment_id"])] = row["segment_name"]
        for key, values, permitted in [("partners", partners, access["partner"]), ("segments", segments, access["segment"])]:
            if permitted:
                options[key] = [{"id": key_id, "name": name} for key_id, name in sorted(values.items(), key=lambda pair: (pair[1].casefold(), pair[0]))]
    if partner:
        bounded = bounded.filter(partner_id=int(partner))
    if segment:
        bounded = bounded.filter(segment_id=segment if section == "client" else int(segment))
    current = bounded.filter(initiated_at__gte=window["start"], initiated_at__lt=window["end"])
    previous = bounded.filter(initiated_at__gte=comparison["start"], initiated_at__lt=comparison["end"])
    raw = current.aggregate(**_aggregates())
    summary = _metrics(raw, user, access)
    baseline = _metrics(previous.aggregate(**_aggregates()), user, access)
    rows = []
    if access["table"]:
        fields = ["partner_id", "partner_name"]
        if section == "supplier":
            fields += ["segment_id", "segment_name"]
        for item in current.values(*fields).annotate(**_aggregates()).order_by("partner_name", "partner_id"):
            metric = _metrics(item, user, access, raw["completes"])
            metric.update(id=str(item["partner_id"]), name=item["partner_name"])
            rows.append(metric)
    trend = []
    if access["trend"] and (access["hits"] or access["completes"]):
        # One grouped SQL query, not one query per bucket/day.
        expressions = {}
        for index, bucket in enumerate(window["buckets"]):
            bucket_q = Q(initiated_at__gte=bucket["lower"], initiated_at__lt=bucket["upper"])
            expressions[f"hits_{index}"] = Count("id", filter=bucket_q)
            expressions[f"completes_{index}"] = Count("id", filter=bucket_q & Q(status=COMPLETED))
        values = current.aggregate(**expressions) if expressions else {}
        for index, bucket in enumerate(window["buckets"]):
            trend.append({"label": bucket["short_label"], **{
                key: values[f"{key}_{index}"] if access[key] else None for key in ("hits", "completes")
            }})
    return {
        "section": section, "access": access, "options": options,
        "summary": summary, "previous": baseline, "rows": rows, "trend": trend,
        "period": window["label"], "comparison_label": comparison["label"],
        "start": window["start"], "end": window["end"], "generated_at": timezone.now(),
        "definition": "Entry-period journeys; latest final decisions on completes. Revenue uses completed hit-time source CPI, not invoice revenue.",
    }


@never_cache
@require_GET
@function_permission_required("dashboard.view")
def partner_dashboard(request, section):
    if section not in {"client", "supplier"}:
        raise Http404
    access = component_access(request.user, section)
    if request.GET.get("format") == "json":
        try:
            payload = cached_report_payload(
                "partner-dashboard-v1", request,
                lambda: build_partner_payload(request.user, request.GET, section, access),
                timeout=30, extra_scope={"section": section, "access": access},
            )
        except ValueError as exc:
            return JsonResponse({"detail": str(exc)}, status=400)
        return JsonResponse(payload)
    return render(request, "surveys/partner_dashboard.html", {
        "active_page": "dashboard", "dashboard_section": section,
        "partner_label": section.title(), "partner_access": access,
    })
