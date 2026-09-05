"""Permission-scoped dashboard queries and graph/KPI aggregation."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Avg, Count, Max, Min, Q, Sum
from django.utils import timezone

from accounts.access import activity_visible_user_ids
from accounts.models import EmployeeProfile

from .filters import SurveyAttemptFilter
from .models import SurveyAttempt


COMPLETED = SurveyAttempt.Status.COMPLETED
INITIATED = (SurveyAttempt.Status.INITIATED, SurveyAttempt.Status.REDIRECTED)
DASHBOARD_RANGE_LABELS = {
    "24h": "Last 24 hours",
    "48h": "Last 48 hours",
    "7d": "Last 7 days",
    "month": "Current month",
    "3m": "Last 3 months",
    "6m": "Last 6 months",
    "fy": "Financial year",
}


def dashboard_attempts(user, params, range_window=None):
    """Apply the same hierarchy and respondent scope used by Studies."""

    queryset = SurveyAttempt.objects.select_related(
        "survey", "survey__client", "platform_user", "platform_user__employee_profile__role"
    )
    if not user.is_superuser:
        queryset = queryset.filter(platform_user_id__in=activity_visible_user_ids(user))
    filterset = SurveyAttemptFilter(params, queryset=queryset)
    if not filterset.is_valid():
        message = next(iter(filterset.errors.values()))[0]
        raise ValueError(str(message))
    queryset = filterset.qs
    if range_window:
        queryset = queryset.filter(
            initiated_at__gte=range_window["start"],
            initiated_at__lte=range_window["end"],
        )
    return queryset


def dashboard_client_options(queryset):
    """Return only clients present inside the viewer's hierarchy-scoped traffic."""

    rows = queryset.filter(survey__client_id__isnull=False).values(
        "survey__client_id", "survey__client__name"
    ).distinct().order_by("survey__client__name", "survey__client_id")
    return [
        {"id": row["survey__client_id"], "name": row["survey__client__name"] or "Unnamed client"}
        for row in rows
    ]


def _visible_revenue(user, value):
    value = value or Decimal("0.00")
    profile = getattr(user, "employee_profile", None)
    role = getattr(profile, "role", None) if profile else None
    if (
        profile
        and profile.account_type == EmployeeProfile.AccountType.EMPLOYEE
        and role
        and not user.is_superuser
    ):
        value = value * role.cpi_visibility_percent / Decimal("100.00")
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _month_shift(value, offset):
    month_index = value.year * 12 + value.month - 1 + offset
    return value.replace(
        year=month_index // 12,
        month=month_index % 12 + 1,
        day=1,
    )


def _financial_year_start(value) -> int:
    local_value = timezone.localtime(value) if timezone.is_aware(value) else value
    return local_value.year if local_value.month >= 4 else local_value.year - 1


def dashboard_financial_year_options(queryset, now=None):
    """Return only financial years spanned by visible production traffic."""

    current = now or timezone.now()
    bounds = queryset.aggregate(first=Min("initiated_at"), last=Max("initiated_at"))
    first = bounds["first"]
    last = bounds["last"]
    if not first or not last:
        start_years = [_financial_year_start(current)]
    else:
        first_year = _financial_year_start(first)
        last_year = _financial_year_start(last)
        start_years = list(range(first_year, last_year + 1))
    return [
        {
            "start_year": year,
            "value": str(year),
            "label": f"{year}-{str(year + 1)[-2:]}",
        }
        for year in reversed(start_years)
    ]


def dashboard_range_window(range_key, now=None, financial_year=None):
    """Return one analytics window and its chart buckets in the active timezone."""

    key = str(range_key or "24h").strip().lower()
    if key not in DASHBOARD_RANGE_LABELS:
        raise ValueError("Range must be one of: 24h, 48h, 7d, month, 3m, 6m or fy.")
    end = now or timezone.now()
    if timezone.is_naive(end):
        end = timezone.make_aware(end, timezone.get_current_timezone())
    local_end = timezone.localtime(end)
    buckets = []
    bucket_label = ""

    if key in {"24h", "48h"}:
        hours = int(key[:-1])
        bucket_hours = hours // 12
        start = end - timedelta(hours=hours)
        for index in range(12):
            lower = start + timedelta(hours=index * bucket_hours)
            upper = min(end, lower + timedelta(hours=bucket_hours))
            buckets.append({
                "key": lower.isoformat(),
                "label": timezone.localtime(lower).strftime("%d %b %I %p"),
                "short_label": timezone.localtime(lower).strftime("%I %p").lstrip("0"),
                "lower": lower,
                "upper": upper,
            })
        bucket_label = f"{bucket_hours}-hour intervals"
    elif key == "7d":
        start = end - timedelta(days=7)
        for index in range(7):
            lower = start + timedelta(days=index)
            upper = min(end, lower + timedelta(days=1))
            buckets.append({
                "key": timezone.localtime(lower).date().isoformat(),
                "label": timezone.localtime(lower).strftime("%d %b %Y"),
                "short_label": timezone.localtime(lower).strftime("%d %b"),
                "lower": lower,
                "upper": upper,
            })
        bucket_label = "Daily intervals"
    elif key == "month":
        start = local_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        lower = start
        while lower < end:
            upper = min(end, lower + timedelta(days=1))
            buckets.append({
                "key": lower.date().isoformat(),
                "label": lower.strftime("%d %b %Y"),
                "short_label": lower.strftime("%d %b"),
                "lower": lower,
                "upper": upper,
            })
            lower += timedelta(days=1)
        bucket_label = "Daily intervals"
    elif key == "3m":
        start = end - timedelta(weeks=13)
        for index in range(13):
            lower = start + timedelta(weeks=index)
            upper = min(end, lower + timedelta(weeks=1))
            buckets.append({
                "key": lower.date().isoformat(),
                "label": timezone.localtime(lower).strftime("%d %b"),
                "short_label": timezone.localtime(lower).strftime("%d %b"),
                "lower": lower,
                "upper": upper,
            })
        bucket_label = "Weekly intervals"
    elif key == "6m":
        month_count = 6
        current_month = local_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = _month_shift(current_month, -(month_count - 1))
        for index in range(month_count):
            lower = _month_shift(start, index)
            upper = min(end, _month_shift(lower, 1))
            buckets.append({
                "key": lower.strftime("%Y-%m"),
                "label": lower.strftime("%b %Y"),
                "short_label": lower.strftime("%b"),
                "lower": lower,
                "upper": upper,
            })
        bucket_label = "Monthly intervals"
    else:
        current_financial_year = _financial_year_start(end)
        try:
            selected_year = int(financial_year or current_financial_year)
        except (TypeError, ValueError) as exc:
            raise ValueError("Financial year must be a four-digit starting year.") from exc
        if selected_year < 2020 or selected_year > current_financial_year:
            raise ValueError("Financial year is outside the supported reporting range.")
        start = local_end.replace(
            year=selected_year, month=4, day=1,
            hour=0, minute=0, second=0, microsecond=0,
        )
        financial_year_end = start.replace(year=selected_year + 1)
        end = min(end, financial_year_end)
        lower = start
        while lower < end:
            upper = min(end, _month_shift(lower, 1))
            buckets.append({
                "key": lower.strftime("%Y-%m"),
                "label": lower.strftime("%b %Y"),
                "short_label": lower.strftime("%b"),
                "lower": lower,
                "upper": upper,
            })
            lower = _month_shift(lower, 1)
        bucket_label = "Monthly intervals"

    return {
        "key": key,
        "label": (
            f"Financial year {selected_year}-{str(selected_year + 1)[-2:]}"
            if key == "fy" else DASHBOARD_RANGE_LABELS[key]
        ),
        "bucket_label": bucket_label,
        "start": start,
        "end": end,
        "buckets": buckets,
        "financial_year": selected_year if key == "fy" else None,
    }


def dashboard_comparison_window(range_window):
    """Return the fair baseline window for the selected dashboard range."""

    start = range_window["start"]
    end = range_window["end"]
    if range_window["key"] == "month":
        previous_start = _month_shift(start, -1)
        previous_month_end = start
        previous_end = min(previous_month_end, previous_start + (end - start))
        return {
            "start": previous_start,
            "end": previous_end,
            "label": "Previous month to date",
        }
    duration = end - start
    return {
        "start": start - duration,
        "end": start,
        "label": "Previous equivalent period",
    }


def _performance_series(queryset, range_window):
    expressions = {}
    for index, bucket in enumerate(range_window["buckets"]):
        window = Q(initiated_at__gte=bucket["lower"], initiated_at__lt=bucket["upper"])
        completed = window & Q(status=COMPLETED)
        survey_terminated = window & Q(status=SurveyAttempt.Status.TERMINATED) & ~Q(
            status_source="local_prescreener"
        )
        expressions[f"hits_{index}"] = Count("id", filter=window)
        expressions[f"completes_{index}"] = Count("id", filter=completed)
        expressions[f"terminated_{index}"] = Count("id", filter=survey_terminated)
        expressions[f"revenue_{index}"] = Sum(
            "source_cpi_snapshot", filter=completed, default=Decimal("0.00")
        )
    totals = queryset.aggregate(**expressions)
    points = []
    for index, bucket in enumerate(range_window["buckets"]):
        hits = totals[f"hits_{index}"]
        completes = totals[f"completes_{index}"]
        terminated = totals[f"terminated_{index}"]
        revenue = totals[f"revenue_{index}"] or Decimal("0.00")
        ir_denominator = completes + terminated
        points.append({
            "key": bucket["key"],
            "label": bucket["label"],
            "short_label": bucket["short_label"],
            "hits": hits,
            "completes": completes,
            "conversion_rate": round(completes / hits * 100, 2) if hits else 0.0,
            "incidence_rate": round(completes / ir_denominator * 100, 2) if ir_denominator else 0.0,
            "revenue": revenue,
            "average_cpi": (revenue / completes).quantize(Decimal("0.01")) if completes else Decimal("0.00"),
            "rpc": (revenue / hits).quantize(Decimal("0.01")) if hits else Decimal("0.00"),
        })
    return points


def _client_distribution(queryset, user, card_access):
    completed_filter = Q(status=COMPLETED)
    grouped = queryset.values(
        "survey__client_id", "survey__client__name", "survey__company_name"
    ).annotate(
        hits=Count("id"),
        completes=Count("id", filter=completed_filter),
        revenue=Sum(
            "source_cpi_snapshot", filter=completed_filter, default=Decimal("0.00")
        ),
    ).order_by("-completes", "survey__client__name")
    merged = {}
    for row in grouped:
        name = row["survey__client__name"] or row["survey__company_name"] or "Unassigned client"
        key = str(row["survey__client_id"] or name)
        item = merged.setdefault(key, {
            "client_id": row["survey__client_id"], "name": name,
            "hits": 0, "completes": 0, "revenue": Decimal("0.00"),
        })
        item["hits"] += row["hits"]
        item["completes"] += row["completes"]
        item["revenue"] += row["revenue"] or Decimal("0.00")
    rows = sorted(merged.values(), key=lambda item: (-item["completes"], item["name"].casefold()))
    total = sum(item["completes"] for item in rows)
    for item in rows:
        item["share_percent"] = round(item["completes"] / total * 100, 1) if total else 0.0
        item["conversion_rate"] = (
            round(item["completes"] / item["hits"] * 100, 1) if item["hits"] else 0.0
        )
        item["revenue"] = (
            _visible_revenue(user, item["revenue"])
            if card_access.get("revenue") else None
        )
    if len(rows) > 8:
        other_hits = sum(item["hits"] for item in rows[7:])
        other_completes = sum(item["completes"] for item in rows[7:])
        other_revenue = (
            sum((item["revenue"] or Decimal("0.00")) for item in rows[7:])
            if card_access.get("revenue") else None
        )
        rows = rows[:7] + [{
            "client_id": None,
            "name": "Other clients",
            "hits": other_hits,
            "completes": other_completes,
            "share_percent": round(other_completes / total * 100, 1) if total else 0.0,
            "conversion_rate": round(other_completes / other_hits * 100, 1) if other_hits else 0.0,
            "revenue": other_revenue,
        }]
    return rows


def _top_suppliers(queryset, user, card_access, total_completes):
    rows = queryset.values(
        "vendor_id", "vendor__first_name", "vendor__last_name", "vendor__username",
        "vendor__employee_profile__company_name",
        "platform_user__employee_profile__organization_unit__unit_type",
        "platform_user__employee_profile__organization_unit__name",
        "platform_user__employee_profile__organization_unit__parent__name",
        "platform_user__employee_profile__organization_unit__parent__parent__name",
    ).annotate(
        hits=Count("id"),
        completes=Count("id", filter=Q(status=COMPLETED)),
        revenue=Sum(
            "source_cpi_snapshot",
            filter=Q(status=COMPLETED),
            default=Decimal("0.00"),
        ),
    )
    merged = {}
    for row in rows:
        supplier_name = row["vendor__employee_profile__company_name"] or " ".join(filter(None, [
            row["vendor__first_name"], row["vendor__last_name"],
        ])).strip() or row["vendor__username"] or "Direct traffic"
        unit_type = row["platform_user__employee_profile__organization_unit__unit_type"]
        if unit_type == "branch":
            branch_name = row["platform_user__employee_profile__organization_unit__name"]
        elif unit_type == "sub_branch":
            branch_name = row["platform_user__employee_profile__organization_unit__parent__name"]
        elif unit_type == "shift":
            branch_name = row["platform_user__employee_profile__organization_unit__parent__parent__name"]
        else:
            branch_name = None
        branch_name = branch_name or "Unassigned branch"
        key = (row["vendor_id"], supplier_name, branch_name)
        item = merged.setdefault(key, {
            "supplier_id": row["vendor_id"],
            "name": supplier_name,
            "branch_name": branch_name,
            "hits": 0,
            "completes": 0,
            "revenue": Decimal("0.00"),
        })
        item["hits"] += row["hits"]
        item["completes"] += row["completes"]
        item["revenue"] += row["revenue"] or Decimal("0.00")
    result = sorted(
        merged.values(),
        key=lambda item: (-item["completes"], -item["hits"], item["name"].casefold()),
    )[:8]
    for item in result:
        item["conversion_rate"] = (
            round(item["completes"] / item["hits"] * 100, 1) if item["hits"] else 0.0
        )
        item["contribution_percent"] = (
            round(item["completes"] / total_completes * 100, 1)
            if total_completes else 0.0
        )
        item["revenue"] = (
            _visible_revenue(user, item["revenue"])
            if card_access.get("revenue") else None
        )
    return result


def _recent_activity(queryset):
    rows = queryset.order_by("-initiated_at")[:7]
    result = []
    for attempt in rows:
        user_name = "Deleted user"
        if attempt.platform_user:
            user_name = attempt.platform_user.get_full_name() or attempt.platform_user.username
        result.append({
            "rid": attempt.rid,
            "user_name": user_name,
            "project_id": attempt.survey.local_id,
            "client_name": (
                attempt.survey.client.name if attempt.survey.client_id else attempt.survey.company_name
            ) or "Unassigned client",
            "status": attempt.status,
            "status_label": "Initiated" if attempt.status in INITIATED else attempt.get_status_display(),
            "initiated_at": attempt.initiated_at,
        })
    return result


def _range_payload(range_window):
    return {
        "key": range_window["key"],
        "label": range_window["label"],
        "bucket_label": range_window["bucket_label"],
        "start": range_window["start"],
        "end": range_window["end"],
        "financial_year": range_window.get("financial_year"),
    }


def _permission_scoped_performance(queryset, range_window, user, card_access):
    points = _performance_series(queryset, range_window)
    for point in points:
        point_revenue = _visible_revenue(user, point["revenue"])
        point["revenue"] = point_revenue if card_access.get("revenue") else None
        point["average_cpi"] = (
            (point_revenue / point["completes"]).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ) if point["completes"] else Decimal("0.00")
        ) if card_access.get("average_cpi") else None
        point["rpc"] = (
            (point_revenue / point["hits"]).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ) if point["hits"] else Decimal("0.00")
        ) if card_access.get("rpc") else None
    return points


def _percent_change(current, previous):
    if current is None or previous is None:
        return None
    current_value = float(current)
    previous_value = float(previous)
    if previous_value == 0:
        return 0.0 if current_value == 0 else 100.0
    return round((current_value - previous_value) / abs(previous_value) * 100, 1)


def _comparison_payload(queryset, user, card_access, current_values, label):
    completed_filter = Q(status=COMPLETED)
    survey_termination_filter = Q(status=SurveyAttempt.Status.TERMINATED) & ~Q(
        status_source="local_prescreener"
    )
    totals = queryset.aggregate(
        hits=Count("id"),
        completes=Count("id", filter=completed_filter),
        survey_terminated=Count("id", filter=survey_termination_filter),
        active_users=Count("platform_user_id", distinct=True),
        average_loi=Avg("loi_seconds"),
        revenue=Sum(
            "source_cpi_snapshot", filter=completed_filter, default=Decimal("0.00")
        ),
    )
    visible_revenue = _visible_revenue(user, totals["revenue"])
    ir_denominator = totals["completes"] + totals["survey_terminated"]
    values = {
        "hits": totals["hits"],
        "completes": totals["completes"],
        "conversion_rate": (
            round(totals["completes"] / totals["hits"] * 100, 2)
            if totals["hits"] else 0.0
        ),
        "incidence_rate": (
            round(totals["completes"] / ir_denominator * 100, 2)
            if ir_denominator else 0.0
        ),
        "active_users": totals["active_users"],
        "average_loi_seconds": round(totals["average_loi"] or 0),
        "revenue": visible_revenue,
        "average_cpi": (
            visible_revenue / totals["completes"]
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if totals["completes"] else Decimal("0.00"),
        "rpc": (
            visible_revenue / totals["hits"]
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if totals["hits"] else Decimal("0.00"),
    }
    visible_values = {
        key: value if card_access.get(key, False) else None
        for key, value in values.items()
    }
    return {
        "label": label,
        "values": visible_values,
        "deltas": {
            key: _percent_change(current_values.get(key), value)
            if card_access.get(key, False) else None
            for key, value in values.items()
        },
    }


def build_dashboard_payload(
    queryset,
    user,
    card_access,
    chart_access,
    range_window=None,
    *,
    traffic_queryset=None,
    traffic_range_window=None,
    traffic_client_id=None,
    finance_queryset=None,
    finance_range_window=None,
    finance_client_id=None,
    client_options=None,
    comparison_queryset=None,
    comparison_label="Previous equivalent period",
    financial_years=None,
):
    range_window = range_window or dashboard_range_window("24h")
    completed_filter = Q(status=COMPLETED)
    survey_termination_filter = Q(status=SurveyAttempt.Status.TERMINATED) & ~Q(
        status_source="local_prescreener"
    )
    desktop_hit_filter = Q(entry_device__icontains="desktop") | Q(entry_device__icontains="laptop")
    mobile_hit_filter = Q(entry_device__icontains="mobile") | Q(entry_device__icontains="phone")
    tablet_hit_filter = Q(entry_device__icontains="tablet") | Q(entry_device__iexact="tab")
    last_hour_start = range_window["end"] - timedelta(hours=1)
    totals = queryset.aggregate(
        hits=Count("id"),
        completes=Count("id", filter=completed_filter),
        last_hour_completes=Count(
            "id",
            filter=completed_filter & Q(initiated_at__gte=last_hour_start),
        ),
        initiated=Count("id", filter=Q(status__in=INITIATED)),
        terminated=Count("id", filter=Q(status=SurveyAttempt.Status.TERMINATED)),
        survey_terminated=Count("id", filter=survey_termination_filter),
        quota=Count("id", filter=Q(status=SurveyAttempt.Status.OVER_QUOTA)),
        security=Count("id", filter=Q(status=SurveyAttempt.Status.QUALITY_TERMINATED)),
        active_users=Count("platform_user_id", distinct=True),
        average_loi=Avg("loi_seconds"),
        revenue=Sum("source_cpi_snapshot", filter=completed_filter, default=Decimal("0.00")),
        currency=Max("cpi_currency_snapshot", filter=completed_filter),
        desktop_hits=Count("id", filter=desktop_hit_filter),
        mobile_hits=Count("id", filter=mobile_hit_filter),
        tablet_hits=Count("id", filter=tablet_hit_filter),
        desktop=Count("id", filter=completed_filter & desktop_hit_filter),
        mobile=Count("id", filter=completed_filter & mobile_hit_filter),
        tablet=Count("id", filter=completed_filter & tablet_hit_filter),
    )
    conversion = round(totals["completes"] / totals["hits"] * 100, 2) if totals["hits"] else 0.0
    ir_denominator = totals["completes"] + totals["survey_terminated"]
    incidence_rate = round(totals["completes"] / ir_denominator * 100, 2) if ir_denominator else 0.0
    visible_revenue = _visible_revenue(user, totals["revenue"])
    summary_values = {
        "hits": totals["hits"],
        "completes": totals["completes"],
        "conversion_rate": conversion,
        "incidence_rate": incidence_rate,
        "active_users": totals["active_users"],
        "average_loi_seconds": round(totals["average_loi"] or 0),
        "revenue": visible_revenue,
        "average_cpi": (
            visible_revenue / totals["completes"]
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if totals["completes"] else Decimal("0.00"),
        "rpc": (
            visible_revenue / totals["hits"]
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if totals["hits"] else Decimal("0.00"),
        "revenue_currency": totals["currency"] or "USD",
    }
    summary = {
        key: value if card_access.get(key, False) else None
        for key, value in summary_values.items()
    }
    summary["last_hour_completes"] = (
        totals["last_hour_completes"] if card_access.get("completes") else None
    )
    summary["revenue_currency"] = (
        summary_values["revenue_currency"]
        if any(card_access.get(key) for key in ("revenue", "average_cpi", "rpc"))
        else None
    )
    completed_classified = totals["desktop"] + totals["mobile"] + totals["tablet"]
    hit_classified = totals["desktop_hits"] + totals["mobile_hits"] + totals["tablet_hits"]
    traffic_range_window = traffic_range_window or range_window
    finance_range_window = finance_range_window or range_window
    traffic_queryset = traffic_queryset if traffic_queryset is not None else queryset
    finance_queryset = finance_queryset if finance_queryset is not None else queryset
    traffic_chart = None
    finance_chart = None
    if chart_access.get("performance"):
        traffic_points = _permission_scoped_performance(
            traffic_queryset, traffic_range_window, user, card_access
        )
        traffic_chart = {
            "range": _range_payload(traffic_range_window),
            "client_id": traffic_client_id,
            "points": traffic_points,
        }
        if any(card_access.get(key) for key in ("revenue", "average_cpi", "rpc")):
            same_scope = (
                traffic_client_id == finance_client_id
                and traffic_range_window["start"] == finance_range_window["start"]
                and traffic_range_window["end"] == finance_range_window["end"]
                and traffic_range_window["buckets"] == finance_range_window["buckets"]
                and traffic_queryset.query.sql_with_params()
                == finance_queryset.query.sql_with_params()
            )
            finance_chart = {
                "range": _range_payload(finance_range_window),
                "client_id": finance_client_id,
                "points": (
                    [dict(point) for point in traffic_points]
                    if same_scope
                    else _permission_scoped_performance(
                        finance_queryset, finance_range_window, user, card_access
                    )
                ),
            }
    return {
        "range": _range_payload(range_window),
        "summary": summary,
        "comparison": (
            _comparison_payload(
                comparison_queryset, user, card_access, summary_values, comparison_label
            )
            if comparison_queryset is not None else None
        ),
        "financial_years": financial_years or [],
        "traffic_chart": traffic_chart,
        "finance_chart": finance_chart,
        "graph_clients": client_options or [],
        "client_distribution": (
            _client_distribution(queryset, user, card_access)
            if chart_access.get("client_share") else None
        ),
        "status_breakdown": {
            "initiated": totals["initiated"], "completed": totals["completes"],
            "terminated": totals["terminated"], "quota": totals["quota"], "security": totals["security"],
        } if chart_access.get("status") else None,
        "device_breakdown": {
            "desktop": totals["desktop"], "mobile": totals["mobile"], "tablet": totals["tablet"],
            "unclassified": max(0, totals["completes"] - completed_classified),
        } if chart_access.get("device") else None,
        "device_performance": {
            key: {
                "hits": hits,
                "completes": completes,
                "conversion_rate": round(completes / hits * 100, 1) if hits else 0.0,
            }
            for key, hits, completes in (
                ("desktop", totals["desktop_hits"], totals["desktop"]),
                ("mobile", totals["mobile_hits"], totals["mobile"]),
                ("tablet", totals["tablet_hits"], totals["tablet"]),
                (
                    "unclassified",
                    max(0, totals["hits"] - hit_classified),
                    max(0, totals["completes"] - completed_classified),
                ),
            )
        } if chart_access.get("device") else None,
        "top_suppliers": (
            _top_suppliers(queryset, user, card_access, totals["completes"])
            if chart_access.get("top_users") else None
        ),
        "generated_at": timezone.now(),
    }
