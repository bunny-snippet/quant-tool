from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Avg, Count, Max, Q, Sum
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
    "72h": "Last 72 hours",
    "3m": "Last 3 months",
    "6m": "Last 6 months",
    "1y": "Last 1 year",
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


def dashboard_range_window(range_key, now=None):
    """Return one analytics window and its chart buckets in the active timezone."""

    key = str(range_key or "24h").strip().lower()
    if key not in DASHBOARD_RANGE_LABELS:
        raise ValueError("Range must be one of: 24h, 48h, 72h, 3m, 6m, 1y.")
    end = now or timezone.now()
    if timezone.is_naive(end):
        end = timezone.make_aware(end, timezone.get_current_timezone())
    local_end = timezone.localtime(end)
    buckets = []
    bucket_label = ""

    if key in {"24h", "48h", "72h"}:
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
    else:
        month_count = 6 if key == "6m" else 12
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

    return {
        "key": key,
        "label": DASHBOARD_RANGE_LABELS[key],
        "bucket_label": bucket_label,
        "start": start,
        "end": end,
        "buckets": buckets,
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


def _client_distribution(queryset):
    grouped = queryset.filter(status=COMPLETED).values(
        "survey__client_id", "survey__client__name", "survey__company_name"
    ).annotate(completes=Count("id")).order_by("-completes", "survey__client__name")
    merged = {}
    for row in grouped:
        name = row["survey__client__name"] or row["survey__company_name"] or "Unassigned client"
        key = str(row["survey__client_id"] or name)
        item = merged.setdefault(key, {
            "client_id": row["survey__client_id"], "name": name, "completes": 0,
        })
        item["completes"] += row["completes"]
    rows = sorted(merged.values(), key=lambda item: (-item["completes"], item["name"].casefold()))
    total = sum(item["completes"] for item in rows)
    for item in rows:
        item["share_percent"] = round(item["completes"] / total * 100, 1) if total else 0.0
    if len(rows) > 8:
        other_completes = sum(item["completes"] for item in rows[7:])
        rows = rows[:7] + [{
            "client_id": None,
            "name": "Other clients",
            "completes": other_completes,
            "share_percent": round(other_completes / total * 100, 1) if total else 0.0,
        }]
    return rows


def _top_users(queryset):
    rows = queryset.exclude(platform_user_id=None).values(
        "platform_user_id", "platform_user__first_name", "platform_user__last_name",
        "platform_user__username",
    ).annotate(
        hits=Count("id"),
        completes=Count("id", filter=Q(status=COMPLETED)),
    ).order_by("-completes", "-hits", "platform_user__first_name")[:8]
    result = []
    for row in rows:
        name = " ".join(filter(None, [row["platform_user__first_name"], row["platform_user__last_name"]])).strip()
        result.append({
            "user_id": row["platform_user_id"],
            "name": name or row["platform_user__username"] or "Deleted user",
            "hits": row["hits"],
            "completes": row["completes"],
            "conversion_rate": round(row["completes"] / row["hits"] * 100, 1) if row["hits"] else 0.0,
        })
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
):
    range_window = range_window or dashboard_range_window("24h")
    completed_filter = Q(status=COMPLETED)
    survey_termination_filter = Q(status=SurveyAttempt.Status.TERMINATED) & ~Q(
        status_source="local_prescreener"
    )
    totals = queryset.aggregate(
        hits=Count("id"),
        completes=Count("id", filter=completed_filter),
        initiated=Count("id", filter=Q(status__in=INITIATED)),
        terminated=Count("id", filter=Q(status=SurveyAttempt.Status.TERMINATED)),
        survey_terminated=Count("id", filter=survey_termination_filter),
        quota=Count("id", filter=Q(status=SurveyAttempt.Status.OVER_QUOTA)),
        security=Count("id", filter=Q(status=SurveyAttempt.Status.QUALITY_TERMINATED)),
        active_users=Count("platform_user_id", distinct=True),
        average_loi=Avg("loi_seconds"),
        revenue=Sum("source_cpi_snapshot", filter=completed_filter, default=Decimal("0.00")),
        currency=Max("cpi_currency_snapshot", filter=completed_filter),
        desktop=Count("id", filter=completed_filter & (Q(entry_device__icontains="desktop") | Q(entry_device__icontains="laptop"))),
        mobile=Count("id", filter=completed_filter & (Q(entry_device__icontains="mobile") | Q(entry_device__icontains="phone"))),
        tablet=Count("id", filter=completed_filter & (Q(entry_device__icontains="tablet") | Q(entry_device__iexact="tab"))),
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
    summary["revenue_currency"] = (
        summary_values["revenue_currency"]
        if any(card_access.get(key) for key in ("revenue", "average_cpi", "rpc"))
        else None
    )
    completed_classified = totals["desktop"] + totals["mobile"] + totals["tablet"]
    traffic_range_window = traffic_range_window or range_window
    finance_range_window = finance_range_window or range_window
    traffic_queryset = traffic_queryset if traffic_queryset is not None else queryset
    finance_queryset = finance_queryset if finance_queryset is not None else queryset
    traffic_chart = None
    finance_chart = None
    if chart_access.get("performance"):
        traffic_chart = {
            "range": _range_payload(traffic_range_window),
            "client_id": traffic_client_id,
            "points": _permission_scoped_performance(
                traffic_queryset, traffic_range_window, user, card_access
            ),
        }
        if any(card_access.get(key) for key in ("revenue", "average_cpi", "rpc")):
            finance_chart = {
                "range": _range_payload(finance_range_window),
                "client_id": finance_client_id,
                "points": _permission_scoped_performance(
                    finance_queryset, finance_range_window, user, card_access
                ),
            }
    return {
        "range": _range_payload(range_window),
        "summary": summary,
        "traffic_chart": traffic_chart,
        "finance_chart": finance_chart,
        "graph_clients": client_options or [],
        "client_distribution": _client_distribution(queryset) if chart_access.get("client_share") else None,
        "status_breakdown": {
            "initiated": totals["initiated"], "completed": totals["completes"],
            "terminated": totals["terminated"], "quota": totals["quota"], "security": totals["security"],
        } if chart_access.get("status") else None,
        "device_breakdown": {
            "desktop": totals["desktop"], "mobile": totals["mobile"], "tablet": totals["tablet"],
            "unclassified": max(0, totals["completes"] - completed_classified),
        } if chart_access.get("device") else None,
        "top_users": _top_users(queryset) if chart_access.get("top_users") else None,
        "generated_at": timezone.now(),
    }
