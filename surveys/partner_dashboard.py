"""Read-only client/supplier analytics, using the Overview visibility contract."""

from decimal import Decimal, ROUND_HALF_UP
from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.db.models import Case, CharField, Count, F, IntegerField, Max, Min, Q, Sum, Value, When
from django.db.models.functions import Coalesce, Concat, NullIf
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from accounts.access import effective_permission_codes, has_function_access
from django.contrib.auth.decorators import login_required
from .dashboard import COMPLETED, _visible_revenue, dashboard_attempts, dashboard_comparison_window, dashboard_range_window, dashboard_financial_year_options, _month_shift
from .report_cache import cached_report_payload


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
        segment=False,
        world_map=section == "client" and allowed("dashboard.chart.world_map") and access["completes"],
    )
    return access


def _dimensioned(queryset, section, include_segment=True):
    if section == "client":
        return queryset.annotate(
            partner_id=Coalesce("survey__client_id", Value(0)),
            partner_name=Coalesce(NullIf("survey__client__name", Value("")), Value("Unassigned client")),
            segment_id=Coalesce(NullIf("survey__country_code", Value("")), Value("unknown")),
            segment_name=Coalesce(NullIf("survey__country", Value("")), NullIf("survey__country_code", Value("")), Value("Unassigned country")),
        )
    branches = {"branch": UNIT, "sub_branch": UNIT + "__parent", "shift": UNIT + "__parent__parent"}
    queryset = queryset.annotate(
        partner_id=Coalesce("vendor_id", Value(0)),
        partner_name=Coalesce(
            NullIf("vendor__employee_profile__company_name", Value("")),
            NullIf(Concat("vendor__first_name", Value(" "), "vendor__last_name"), Value(" ")),
            NullIf("vendor__username", Value("")), Value("Direct traffic"),
        ),
    )
    if not include_segment:
        return queryset
    return queryset.annotate(
        segment_id=Coalesce(Case(*[
            When(**{UNIT + "__unit_type": kind}, then=F(path + "__id"))
            for kind, path in branches.items()
        ], output_field=IntegerField()), Value(0)),
        segment_name=Coalesce(Case(*[
            When(**{UNIT + "__unit_type": kind}, then=F(path + "__name"))
            for kind, path in branches.items()
        ], output_field=CharField()), Value("Unassigned branch")),
    )


def _aggregates(cohort=None):
    cohort = cohort if cohort is not None else Q()
    completed = cohort & Q(status=COMPLETED)
    return {
        "hits": Count("id", filter=cohort), "completes": Count("id", filter=completed),
        "accepted": Count("id", filter=completed & Q(final_id_status__status="accepted")),
        "rejected": Count("id", filter=completed & Q(final_id_status__status="rejected")),
        "revenue": Sum("source_cpi_snapshot", filter=completed, default=Decimal("0.00")),
        "currency_min": Min("cpi_currency_snapshot", filter=completed),
        "currency_max": Max("cpi_currency_snapshot", filter=completed),
        "missing_cpi": Count("id", filter=completed & Q(source_cpi_snapshot__isnull=True)),
        "surveys": Count("survey_id", distinct=True, filter=cohort),
    }


def main_table_access(user):
    """Keep existing partner-page and metric grants when embedding the tables."""
    if user.is_superuser:
        return {"client": True, "supplier": True}
    codes = effective_permission_codes(user)
    return {
        section: {
            "dashboard." + section + ".view", "dashboard.card.completes", chart,
        }.issubset(codes)
        for section, chart in (
            ("client", "dashboard.chart.client_share"),
            ("supplier", "dashboard.chart.top_users"),
        )
    }


def main_dashboard_tables(queryset, user, total_completes):
    tables = {"client": None, "supplier": None}
    table_access = main_table_access(user)
    if not any(table_access.values()):
        return tables
    money_allowed = user.is_superuser or has_function_access(user, "dashboard.card.revenue")
    for section, allowed in table_access.items():
        if not allowed:
            continue
        completed = Q(status=COMPLETED)
        rows = _dimensioned(queryset.select_related(None).order_by(), section, include_segment=False).values(
            "partner_id", "partner_name",
        ).annotate(
            completes=Count("id", filter=completed),
            accepted=Count("id", filter=completed & Q(final_id_status__status="accepted")),
            rejected=Count("id", filter=completed & Q(final_id_status__status="rejected")),
            revenue=Sum("source_cpi_snapshot", filter=completed, default=Decimal("0.00")),
            currency_min=Min("cpi_currency_snapshot", filter=completed),
            currency_max=Max("cpi_currency_snapshot", filter=completed),
            missing_cpi=Count("id", filter=completed & (Q(source_cpi_snapshot__isnull=True) | Q(cpi_currency_snapshot__isnull=True) | Q(cpi_currency_snapshot=""))),
        ).order_by("-completes", "partner_name", "partner_id")
        tables[section] = [{
            "id": str(row["partner_id"]), "name": row["partner_name"],
            "completes": row["completes"], "accepted": row["accepted"], "rejected": row["rejected"],
            "share": round(row["completes"] / total_completes * 100, 2) if total_completes else 0,
            "rejection_percentage": round(row["rejected"] / row["completes"] * 100, 2) if row["completes"] else 0,
            "revenue": _visible_revenue(user, row["revenue"]) if money_allowed and not row["missing_cpi"] and row["currency_min"] == row["currency_max"] else None,
            "currency": (row["currency_min"] or "USD") if money_allowed and not row["missing_cpi"] and row["currency_min"] == row["currency_max"] else None,
            "money_note": "Mixed currencies or missing CPI/currency snapshots" if money_allowed and (row["missing_cpi"] or row["currency_min"] != row["currency_max"]) else "",
        } for row in rows]
    return tables


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
    range_key = params.get("range") or "today"
    if range_key not in {"today", "24h", "48h", "7d", "15d", "21d", "28d", "month", "3m", "6m", "fy"}:
        raise ValueError("Invalid dashboard date range.")
    for key in ("partner", "segment"):
        if params.get(key) and not access[key]:
            raise PermissionDenied("You cannot use this dashboard filter.")
    if range_key != "today" and not access["date"]:
        raise PermissionDenied("You cannot change the dashboard date range.")
    partner = params.get("partner", "")
    segment = params.get("segment", "")
    if partner and (not partner.isascii() or not partner.isdigit() or len(partner) > 18):
        raise ValueError("Invalid partner selection.")
    if segment and (len(segment) > 12 or (section == "supplier" and (not segment.isascii() or not segment.isdigit()))):
        raise ValueError("Invalid country or branch selection.")
    visible = dashboard_attempts(user, {}).select_related(None).order_by()
    years = dashboard_financial_year_options(visible, now=now) if access["date"] else []
    year = params.get("financial_year")
    if year and (not access["date"] or str(year) not in {str(y["start_year"]) for y in years}):
        raise ValueError("Financial year is not available in your visible data.")
    window = dashboard_range_window(range_key, now=now, financial_year=year)
    if range_key == "today":
        comparison = {"start":window["start"]-timedelta(days=1), "end":window["start"], "label":"Yesterday · full calendar day"}
    elif range_key == "month":
        comparison = {"start":_month_shift(window["start"],-1), "end":window["start"], "label":"Previous month · full calendar month"}
    else:
        comparison = dashboard_comparison_window(window)
    # Metadata is bounded to the current/comparison cohort, never global users/clients.
    # Keep this queryset lean: annotating dimensions here retains their unused
    # joins even in aggregate()/DISTINCT and made production summaries very slow.
    bounded = visible.filter(initiated_at__gte=comparison["start"], initiated_at__lt=window["end"])
    options = {"partners": [], "segments": []}
    if access["partner"]:
        dimensions = _dimensioned(bounded, section, include_segment=False).values("partner_id", "partner_name").distinct()
        partners = {}
        for row in dimensions:
            partners[str(row["partner_id"])] = row["partner_name"]
        options["partners"] = [{"id": key_id, "name": name} for key_id, name in sorted(partners.items(), key=lambda pair: (pair[1].casefold(), pair[0]))]
    if partner:
        partner_field = "survey__client_id" if section == "client" else "vendor_id"
        bounded = bounded.filter(**{partner_field: int(partner) if int(partner) else None})
    if segment:
        bounded = _dimensioned(bounded,section).filter(segment_id=segment if section == "client" else int(segment))
    current = bounded.filter(initiated_at__gte=window["start"], initiated_at__lt=window["end"])
    aggregates = {}
    for prefix, period in (("current",window),("previous",comparison)):
        aggregates.update({prefix+"_"+key:value for key,value in _aggregates(Q(initiated_at__gte=period["start"],initiated_at__lt=period["end"])).items()})
    combined = bounded.aggregate(**aggregates)
    raw = {key:combined["current_"+key] for key in _aggregates()}
    summary = _metrics(raw, user, access)
    baseline = _metrics({key:combined["previous_"+key] for key in _aggregates()}, user, access)
    rows = []
    if access["table"]:
        fields = ["partner_id", "partner_name"]
        if section == "supplier":
            fields += ["segment_id", "segment_name"]
        for item in _dimensioned(current,section).values(*fields).annotate(**_aggregates()).order_by("partner_name", "partner_id"):
            metric = _metrics(item, user, access, raw["completes"])
            metric.update(id=str(item["partner_id"]), name=item["partner_name"])
            rows.append(metric)
    trend = []
    if access["trend"] and (access["hits"] or access["completes"]):
        # One grouped SQL query, not one query per bucket/day.
        cases = [When(initiated_at__gte=b["lower"], initiated_at__lt=b["upper"], then=Value(i)) for i,b in enumerate(window["buckets"])]
        values = {r["bucket"]:r for r in current.annotate(bucket=Case(*cases, output_field=IntegerField())).values("bucket").annotate(hits=Count("id"), completes=Count("id",filter=Q(status=COMPLETED)))} if cases else {}
        for index, bucket in enumerate(window["buckets"]):
            trend.append({"label": bucket["short_label"], **{
                key: values.get(index,{}).get(key,0) if access[key] else None for key in ("hits", "completes")
            }})
    countries = []
    if section == "client" and access.get("world_map") and access["completes"]:
        grouped = _dimensioned(current.filter(status=COMPLETED),section).values("segment_id", "segment_name", "partner_id", "partner_name").annotate(completes=Count("id")).order_by("segment_id", "-completes")
        by_country = {}
        for item in grouped:
            code = str(item["segment_id"]).strip().upper()
            code = "GB" if code == "UK" else code
            country = by_country.setdefault(code, {"code":code,"name":item["segment_name"],"completes":0,"clients":{}})
            country["completes"] += item["completes"]
            client = country["clients"].setdefault(item["partner_id"], {"name":item["partner_name"],"completes":0})
            client["completes"] += item["completes"]
        countries = sorted(by_country.values(), key=lambda row: (-row["completes"],row["name"]))
        for country in countries:
            country["clients"] = sorted(country["clients"].values(), key=lambda row: (-row["completes"],row["name"]))
    return {
        "section": section, "access": access, "options": options,
        "summary": summary, "previous": baseline, "rows": rows, "trend": trend, "countries":countries, "financial_years":years,
        "comparison_start":comparison["start"], "comparison_end":comparison["end"],
        "period": window["label"], "comparison_label": comparison["label"],
        "start": window["start"], "end": window["end"], "generated_at": timezone.now(),
        "definition": "Entry-period journeys; latest final decisions on completes. Revenue uses completed hit-time source CPI, not invoice revenue.",
    }


@never_cache
@require_GET
@login_required
def partner_dashboard(request, section):
    if section not in {"client", "supplier"}:
        raise Http404
    if not has_function_access(request.user, "dashboard." + section + ".view"):
        raise PermissionDenied("You do not have access to this dashboard.")
    access = component_access(request.user, section)
    if request.GET.get("format") == "json":
        try:
            payload = cached_report_payload(
                "partner-dashboard-v2", request,
                lambda: build_partner_payload(request.user, request.GET, section, access),
                timeout=60, extra_scope={"section": section, "access": access, "day":timezone.localdate().isoformat()},
            )
        except ValueError as exc:
            return JsonResponse({"detail": str(exc)}, status=400)
        return JsonResponse(payload)
    return render(request, "surveys/partner_dashboard.html", {
        "active_page": "dashboard", "dashboard_section": section,
        "partner_label": section.title(), "partner_access": access,
    })
