from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Avg, Count, Max, Q, Sum
from django.utils import timezone

from accounts.access import activity_visible_user_ids
from accounts.models import EmployeeProfile

from .filters import SurveyAttemptFilter
from .models import SurveyAttempt


COMPLETED = SurveyAttempt.Status.COMPLETED
INITIATED = (SurveyAttempt.Status.INITIATED, SurveyAttempt.Status.REDIRECTED)


def dashboard_attempts(user, params):
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
    return filterset.qs


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


def _aware_day_bounds(day):
    zone = timezone.get_current_timezone()
    return (
        timezone.make_aware(datetime.combine(day, time.min), zone),
        timezone.make_aware(datetime.combine(day, time.max), zone),
    )


def _performance_series(queryset):
    today = timezone.localdate()
    daily_days = [today - timedelta(days=offset) for offset in range(4, -1, -1)]
    daily_expressions = {}
    for index, day in enumerate(daily_days):
        lower, upper = _aware_day_bounds(day)
        window = Q(initiated_at__gte=lower, initiated_at__lte=upper)
        daily_expressions[f"daily_hits_{index}"] = Count("id", filter=window)
        daily_expressions[f"daily_completes_{index}"] = Count("id", filter=window & Q(status=COMPLETED))
    daily_totals = queryset.aggregate(**daily_expressions)
    daily = [
        {
            "key": day.isoformat(),
            "label": day.strftime("%d %b"),
            "hits": daily_totals[f"daily_hits_{index}"],
            "completes": daily_totals[f"daily_completes_{index}"],
        }
        for index, day in enumerate(daily_days)
    ]

    current_month = today.replace(day=1)
    month_starts = [_month_shift(current_month, offset) for offset in range(-5, 1)]
    monthly_expressions = {}
    for index, month_start in enumerate(month_starts):
        next_month = _month_shift(month_start, 1)
        lower, _unused = _aware_day_bounds(month_start)
        upper, _unused = _aware_day_bounds(next_month)
        window = Q(initiated_at__gte=lower, initiated_at__lt=upper)
        monthly_expressions[f"month_hits_{index}"] = Count("id", filter=window)
        monthly_expressions[f"month_completes_{index}"] = Count("id", filter=window & Q(status=COMPLETED))
    monthly_totals = queryset.aggregate(**monthly_expressions)
    monthly = [
        {
            "key": month_start.strftime("%Y-%m"),
            "label": month_start.strftime("%b %Y"),
            "hits": monthly_totals[f"month_hits_{index}"],
            "completes": monthly_totals[f"month_completes_{index}"],
        }
        for index, month_start in enumerate(month_starts)
    ]
    return {"daily": daily, "monthly": monthly}


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


def build_dashboard_payload(queryset, user, card_access, chart_access):
    completed_filter = Q(status=COMPLETED)
    totals = queryset.aggregate(
        hits=Count("id"),
        completes=Count("id", filter=completed_filter),
        initiated=Count("id", filter=Q(status__in=INITIATED)),
        terminated=Count("id", filter=Q(status=SurveyAttempt.Status.TERMINATED)),
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
    summary_values = {
        "hits": totals["hits"],
        "completes": totals["completes"],
        "conversion_rate": conversion,
        "active_users": totals["active_users"],
        "average_loi_seconds": round(totals["average_loi"] or 0),
        "revenue": _visible_revenue(user, totals["revenue"]),
        "revenue_currency": totals["currency"] or "USD",
    }
    summary = {
        key: value if card_access.get(key, False) else None
        for key, value in summary_values.items()
    }
    summary["revenue_currency"] = summary_values["revenue_currency"] if card_access.get("revenue") else None
    completed_classified = totals["desktop"] + totals["mobile"] + totals["tablet"]
    return {
        "summary": summary,
        "performance": _performance_series(queryset) if chart_access.get("performance") else None,
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
        "recent_activity": _recent_activity(queryset) if chart_access.get("recent") else None,
        "generated_at": timezone.now(),
    }
