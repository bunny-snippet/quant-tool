from __future__ import annotations

from datetime import datetime, time

from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time

from accounts.access import activity_visible_user_ids
from accounts.models import EmployeeProfile

from .models import SurveyAttempt


DEVICE_KEYS = ("desktop", "mobile", "tablet", "unclassified")


def _visible_user_ids(user) -> set[int]:
    return activity_visible_user_ids(user)


def _csv_values(value: str) -> set[str]:
    return {item.strip() for item in (value or "").split(",") if item.strip()}


def _device_key(value: str) -> str:
    normalized = (value or "").strip().lower()
    if "tablet" in normalized or normalized in {"tab", "t"}:
        return "tablet"
    if "mobile" in normalized or "phone" in normalized or normalized == "m":
        return "mobile"
    if "desktop" in normalized or "laptop" in normalized or normalized == "d":
        return "desktop"
    return "unclassified"


def _empty_counts() -> dict[str, int]:
    return {key: 0 for key in DEVICE_KEYS}


def _build_user_metadata(user_ids: set[int]) -> dict[int, dict]:
    users = list(
        get_user_model().objects.filter(pk__in=user_ids)
        .select_related(
            "employee_profile", "employee_profile__created_by",
            "employee_profile__organization_unit__parent__parent",
        )
        .order_by("first_name", "last_name", "username")
    )
    profiles = {}
    pending_ids = set(user_ids)
    while pending_ids:
        batch = list(
            EmployeeProfile.objects.filter(user_id__in=pending_ids)
            .select_related("user", "created_by", "organization_unit__parent__parent")
        )
        profiles.update({profile.user_id: profile for profile in batch})
        pending_ids = {
            profile.created_by_id for profile in batch
            if profile.created_by_id and profile.created_by_id not in profiles
        }

    def inherited_branch(user_id: int) -> str:
        current_id = user_id
        visited = set()
        while current_id and current_id not in visited:
            visited.add(current_id)
            profile = profiles.get(current_id)
            if not profile:
                break
            if profile.account_type == EmployeeProfile.AccountType.EXTERNAL_VENDOR:
                return ""
            if profile.company_name.strip():
                return profile.company_name.strip()
            current_id = profile.created_by_id
        return "Main branch"

    metadata = {}
    for platform_user in users:
        profile = profiles.get(platform_user.pk)
        unit = getattr(profile, "organization_unit", None) if profile else None
        if unit:
            unit_labels = {}
            unit_ids = {}
            current = unit
            while current:
                unit_labels[current.unit_type] = current.name
                unit_ids[current.unit_type] = current.pk
                current = current.parent
            branch = unit_labels.get("branch", "")
            sub_branch = unit_labels.get("sub_branch", "")
            shift = unit_labels.get("shift", "")
            branch_id = unit_ids.get("branch")
            sub_branch_id = unit_ids.get("sub_branch")
            shift_id = unit_ids.get("shift")
        else:
            branch = inherited_branch(platform_user.pk)
            sub_branch = (profile.department.strip() if profile and profile.department and branch else "") or branch
            shift = ""
            branch_id = sub_branch_id = shift_id = None
        metadata[platform_user.pk] = {
            "user_id": platform_user.pk,
            "user_name": platform_user.get_full_name() or platform_user.username,
            "username": platform_user.username,
            "user_email": platform_user.email,
            "branch": branch,
            "sub_branch": sub_branch,
            "shift": shift,
            "branch_id": branch_id,
            "sub_branch_id": sub_branch_id,
            "shift_id": shift_id,
        }
    return metadata


def user_hit_filter_options(user) -> dict:
    metadata = _build_user_metadata(_visible_user_ids(user))
    visible_users = list(metadata.values())
    visible_users.sort(key=lambda item: (item["user_name"].casefold(), item["user_id"]))

    def option_value(item, level):
        return str(item.get(f"{level}_id") or item.get(level) or "")

    def unique_options(level, parent_levels=()):
        options = {}
        for item in visible_users:
            name = item.get(level) or ""
            value = option_value(item, level)
            if not name or not value:
                continue
            option = {"value": value, "name": name}
            for parent_level in parent_levels:
                option[f"{parent_level}_value"] = option_value(item, parent_level)
            options[(value, *(option.get(f"{parent}_value", "") for parent in parent_levels))] = option
        return sorted(options.values(), key=lambda option: (option["name"].casefold(), option["value"]))

    for item in visible_users:
        item["branch_value"] = option_value(item, "branch")
        item["sub_branch_value"] = option_value(item, "sub_branch")
        item["shift_value"] = option_value(item, "shift")
    return {
        "users": visible_users,
        "branches": unique_options("branch"),
        "sub_branches": unique_options("sub_branch", ("branch",)),
        "shifts": unique_options("shift", ("branch", "sub_branch")),
    }


def aggregate_user_hits(user, params) -> tuple[list[dict], dict]:
    visible_ids = _visible_user_ids(user)
    metadata = _build_user_metadata(visible_ids)

    selected_user_values = _csv_values(params.get("user", ""))
    if any(not value.isdigit() for value in selected_user_values):
        raise ValueError("User filters must contain numeric IDs.")
    selected_user_ids = {int(value) for value in selected_user_values}
    if selected_user_ids:
        visible_ids &= selected_user_ids

    selected_branches = _csv_values(params.get("branch", ""))
    selected_sub_branches = _csv_values(params.get("sub_branch", ""))
    selected_shifts = _csv_values(params.get("shift", ""))
    def hierarchy_match(item, level, selected):
        return bool(
            str(item.get(f"{level}_id") or "") in selected
            or (item.get(level) or "") in selected
        )

    if selected_branches:
        visible_ids = {user_id for user_id in visible_ids if hierarchy_match(metadata.get(user_id, {}), "branch", selected_branches)}
    if selected_sub_branches:
        visible_ids = {
            user_id for user_id in visible_ids
            if hierarchy_match(metadata.get(user_id, {}), "sub_branch", selected_sub_branches)
        }
    if selected_shifts:
        visible_ids = {user_id for user_id in visible_ids if hierarchy_match(metadata.get(user_id, {}), "shift", selected_shifts)}

    from_date = parse_date(params.get("from_date", "")) if params.get("from_date") else None
    to_date = parse_date(params.get("to_date", "")) if params.get("to_date") else None
    from_clock = parse_time(params.get("from_time", "")) if params.get("from_time") else None
    to_clock = parse_time(params.get("to_time", "")) if params.get("to_time") else None
    if params.get("from_date") and from_date is None:
        raise ValueError("from_date must use YYYY-MM-DD format.")
    if params.get("to_date") and to_date is None:
        raise ValueError("to_date must use YYYY-MM-DD format.")
    if params.get("from_time") and from_clock is None:
        raise ValueError("from_time must use HH:MM or HH:MM:SS format.")
    if params.get("to_time") and to_clock is None:
        raise ValueError("to_time must use HH:MM or HH:MM:SS format.")
    if from_clock and not from_date:
        raise ValueError("from_time requires from_date.")
    if to_clock and not to_date:
        raise ValueError("to_time requires to_date.")
    if from_date and to_date and from_date > to_date:
        raise ValueError("from_date cannot be after to_date.")

    current_timezone = timezone.get_current_timezone()
    lower = (
        timezone.make_aware(datetime.combine(from_date, from_clock or time.min), current_timezone)
        if from_date else None
    )
    upper = (
        timezone.make_aware(datetime.combine(to_date, to_clock or time.max), current_timezone)
        if to_date else None
    )
    if lower and upper and lower > upper:
        raise ValueError("from date/time cannot be after to date/time.")

    attempts = SurveyAttempt.objects.filter(platform_user_id__in=visible_ids).only(
        "id", "platform_user_id", "status", "status_source", "entry_device", "initiated_at"
    )
    if lower:
        attempts = attempts.filter(initiated_at__gte=lower)
    if upper:
        attempts = attempts.filter(initiated_at__lte=upper)

    search = params.get("search", "").strip()

    grouped: dict[tuple[int, object], dict] = {}
    for attempt in attempts.iterator(chunk_size=2000):
        user_meta = metadata.get(attempt.platform_user_id)
        if not user_meta:
            continue
        local_date = timezone.localtime(attempt.initiated_at, current_timezone).date()
        key = (attempt.platform_user_id, local_date)
        row = grouped.setdefault(key, {
            **{field: user_meta[field] for field in (
                "user_id", "user_name", "username", "user_email", "branch", "sub_branch", "shift"
            )},
            "date": local_date.isoformat(),
            "hits": {"total": 0, **_empty_counts()},
            "completes": {"total": 0, **_empty_counts()},
            "_survey_terminations": 0,
        })
        device = _device_key(attempt.entry_device)
        row["hits"]["total"] += 1
        row["hits"][device] += 1
        if attempt.status == SurveyAttempt.Status.COMPLETED:
            row["completes"]["total"] += 1
            row["completes"][device] += 1
        elif attempt.status == SurveyAttempt.Status.TERMINATED and attempt.status_source != "local_prescreener":
            row["_survey_terminations"] += 1

    rows = list(grouped.values())
    if search:
        needle = search.casefold()
        rows = [
            row for row in rows
            if any(needle in str(row[field]).casefold() for field in (
                "user_name", "username", "user_email", "branch", "sub_branch", "shift"
            ))
        ]
    rows.sort(key=lambda row: (row["user_name"].casefold(), row["user_id"]))
    rows.sort(key=lambda row: row["date"], reverse=True)

    summary = {
        "hits": {"total": 0, **_empty_counts()},
        "completes": {"total": 0, **_empty_counts()},
        "active_users": len({row["user_id"] for row in rows}),
        "days": len({row["date"] for row in rows}),
        "conversion_rate": 0,
        "incidence_rate": 0,
    }
    for row in rows:
        for metric in ("hits", "completes"):
            for key in ("total", *DEVICE_KEYS):
                summary[metric][key] += row[metric][key]
    if summary["hits"]["total"]:
        summary["conversion_rate"] = round(summary["completes"]["total"] / summary["hits"]["total"] * 100, 1)
    survey_terminations = sum(row.pop("_survey_terminations", 0) for row in rows)
    ir_denominator = summary["completes"]["total"] + survey_terminations
    if ir_denominator:
        summary["incidence_rate"] = round(summary["completes"]["total"] / ir_denominator * 100, 2)
    return rows, summary
