"""Hierarchy-scoped monthly user reconciliation performance."""

from __future__ import annotations

from datetime import datetime

from django.db.models import Count, Q
from django.utils import timezone

from accounts.models import EmployeeProfile

from .models import FinalIDUpload, SurveyAttempt
from .user_hits import (
    _legacy_identifier_user_map,
    _user_metadata,
    _visible_user_ids,
)


def _csv_values(value: str) -> set[str]:
    return {item.strip() for item in (value or "").split(",") if item.strip()}


def _employee_metadata(user) -> dict[int, dict]:
    visible_ids = _visible_user_ids(user)
    employee_ids = set(
        EmployeeProfile.objects.filter(
            user_id__in=visible_ids,
            account_type=EmployeeProfile.AccountType.EMPLOYEE,
        ).values_list("user_id", flat=True)
    )
    metadata = _user_metadata(user, visible_ids)
    return {
        user_id: item
        for user_id, item in metadata.items()
        if user_id in employee_ids
    }


def user_dashboard_filter_options(user) -> dict:
    """Return deduplicated hierarchy options for employee accounts."""

    users = [dict(item) for item in _employee_metadata(user).values()]
    users.sort(key=lambda item: (item["user_name"].casefold(), item["user_id"]))

    def option_value(item, level):
        return str(item.get(f"{level}_id") or item.get(level) or "")

    def unique_options(level, parent_levels=()):
        options = {}
        for item in users:
            name = item.get(level) or ""
            value = option_value(item, level)
            if not name or not value:
                continue
            option = {"value": value, "name": name}
            for parent_level in parent_levels:
                option[f"{parent_level}_value"] = option_value(item, parent_level)
            key = (
                value,
                *(option.get(f"{parent}_value", "") for parent in parent_levels),
            )
            options[key] = option
        return sorted(
            options.values(),
            key=lambda option: (option["name"].casefold(), option["value"]),
        )

    for item in users:
        item["branch_value"] = option_value(item, "branch")
        item["sub_branch_value"] = option_value(item, "sub_branch")
        item["shift_value"] = option_value(item, "shift")

    return {
        "users": users,
        "branches": unique_options("branch"),
        "sub_branches": unique_options("sub_branch", ("branch",)),
        "shifts": unique_options("shift", ("branch", "sub_branch")),
    }


def _selected_period(params) -> tuple[int, int, datetime, datetime]:
    local_today = timezone.localdate()
    try:
        year = int(params.get("year") or local_today.year)
        month = int(params.get("month") or local_today.month)
    except (TypeError, ValueError) as exc:
        raise ValueError("Month and year must be numeric.") from exc
    if year < 2020 or year > local_today.year + 1:
        raise ValueError("Year is outside the supported reporting range.")
    if month < 1 or month > 12:
        raise ValueError("Month must be between 1 and 12.")

    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    current_timezone = timezone.get_current_timezone()
    lower = timezone.make_aware(datetime(year, month, 1), current_timezone)
    upper = timezone.make_aware(datetime(next_year, next_month, 1), current_timezone)
    return year, month, lower, upper


def build_user_dashboard_payload(user, params) -> dict:
    """Aggregate one selected completion month into one row per employee."""

    metadata = _employee_metadata(user)
    visible_ids = set(metadata)

    selected_users = _csv_values(params.get("user", ""))
    if any(not value.isdigit() for value in selected_users):
        raise ValueError("User filters must contain numeric IDs.")
    if selected_users:
        visible_ids &= {int(value) for value in selected_users}

    def hierarchy_match(item, level, selected):
        return bool(
            str(item.get(f"{level}_id") or "") in selected
            or str(item.get(level) or "") in selected
        )

    for parameter, level in (
        ("branch", "branch"),
        ("sub_branch", "sub_branch"),
        ("shift", "shift"),
    ):
        selected = _csv_values(params.get(parameter, ""))
        if selected:
            visible_ids = {
                user_id
                for user_id in visible_ids
                if hierarchy_match(metadata.get(user_id, {}), level, selected)
            }

    search = str(params.get("search") or "").strip().casefold()
    if search:
        visible_ids = {
            user_id
            for user_id in visible_ids
            if any(
                search in str(metadata.get(user_id, {}).get(field, "")).casefold()
                for field in (
                    "user_name", "username", "user_email", "employee_id",
                    "branch", "sub_branch", "shift",
                )
            )
        }

    year, month, lower, upper = _selected_period(params)
    visible_metadata = {
        user_id: metadata[user_id]
        for user_id in visible_ids
        if user_id in metadata
    }
    legacy_user_map = _legacy_identifier_user_map(visible_metadata)

    aggregates = {
        user_id: {"completes": 0, "accepted": 0, "rejected": 0}
        for user_id in visible_ids
    }

    def merge_rows(queryset, identity_field, resolver):
        rows = queryset.values(identity_field).annotate(
            completes=Count("id"),
            accepted=Count(
                "id",
                filter=Q(final_id_status__status=FinalIDUpload.Decision.ACCEPTED),
            ),
            rejected=Count(
                "id",
                filter=Q(final_id_status__status=FinalIDUpload.Decision.REJECTED),
            ),
        )
        for aggregate in rows.iterator(chunk_size=1000):
            report_user_id = resolver(aggregate[identity_field])
            if report_user_id not in aggregates:
                continue
            bucket = aggregates[report_user_id]
            bucket["completes"] += aggregate["completes"]
            bucket["accepted"] += aggregate["accepted"]
            bucket["rejected"] += aggregate["rejected"]

    current_attempts = SurveyAttempt.objects.filter(
        platform_user_id__in=visible_ids,
        status=SurveyAttempt.Status.COMPLETED,
        initiated_at__gte=lower,
        initiated_at__lt=upper,
    )
    merge_rows(current_attempts, "platform_user_id", lambda value: value)

    if legacy_user_map:
        legacy_attempts = SurveyAttempt.objects.filter(
            platform_user_id__isnull=True,
            user_id__in=tuple(legacy_user_map),
            status=SurveyAttempt.Status.COMPLETED,
            initiated_at__gte=lower,
            initiated_at__lt=upper,
        )
        merge_rows(
            legacy_attempts,
            "user_id",
            lambda value: legacy_user_map.get(str(value or "").strip()),
        )

    rows = []
    for user_id in visible_ids:
        counts = aggregates[user_id]
        reviewed = counts["accepted"] + counts["rejected"]
        pending = max(0, counts["completes"] - reviewed)
        rows.append({
            **visible_metadata[user_id],
            **counts,
            "pending": pending,
            "acceptance_rate": (
                round(counts["accepted"] / counts["completes"] * 100, 1)
                if counts["completes"] else 0.0
            ),
            "rejection_rate": (
                round(counts["rejected"] / counts["completes"] * 100, 1)
                if counts["completes"] else 0.0
            ),
            "pending_rate": (
                round(pending / counts["completes"] * 100, 1)
                if counts["completes"] else 0.0
            ),
            "reviewed_rate": (
                round(reviewed / counts["completes"] * 100, 1)
                if counts["completes"] else 0.0
            ),
        })
    rows.sort(
        key=lambda row: (
            -row["accepted"],
            -row["completes"],
            row["user_name"].casefold(),
            row["user_id"],
        )
    )

    summary = {
        "users": len(rows),
        "active_users": sum(1 for row in rows if row["completes"]),
        "completes": sum(row["completes"] for row in rows),
        "accepted": sum(row["accepted"] for row in rows),
        "rejected": sum(row["rejected"] for row in rows),
        "pending": sum(row["pending"] for row in rows),
    }
    reviewed_total = summary["accepted"] + summary["rejected"]
    summary["acceptance_rate"] = (
        round(summary["accepted"] / summary["completes"] * 100, 1)
        if summary["completes"] else 0.0
    )
    summary["rejection_rate"] = (
        round(summary["rejected"] / summary["completes"] * 100, 1)
        if summary["completes"] else 0.0
    )
    summary["pending_rate"] = (
        round(summary["pending"] / summary["completes"] * 100, 1)
        if summary["completes"] else 0.0
    )
    summary["reviewed_rate"] = (
        round(reviewed_total / summary["completes"] * 100, 1)
        if summary["completes"] else 0.0
    )
    return {
        "rows": rows,
        "summary": summary,
        "period": {
            "year": year,
            "month": month,
            "label": lower.strftime("%B %Y"),
        },
    }
