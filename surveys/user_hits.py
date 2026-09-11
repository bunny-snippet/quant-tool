"""Hierarchy-scoped daily hit, completion and device aggregation."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone as datetime_timezone

from django.contrib.auth import get_user_model
from django.db import connections
from django.db.models import Count, DateField, Func, Q
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time

from accounts.access import activity_visible_user_ids, is_super_admin_account
from accounts.models import EmployeeProfile

from .models import SurveyAttempt


DEVICE_KEYS = ("desktop", "mobile", "tablet", "unclassified")
AGGREGATE_KEYS = (
    "hits_total", "hits_desktop", "hits_mobile", "hits_tablet",
    "completes_total", "completes_desktop", "completes_mobile",
    "completes_tablet", "survey_terminations",
)


class _MySQLISTDate(Func):
    """Truncate a UTC timestamp to its IST date without timezone tables.

    MySQL returns ``NULL`` from ``CONVERT_TZ`` when a named zone such as
    ``UTC`` has not been loaded into its timezone tables. Both offsets are
    therefore literal numeric offsets; this works on every supported MySQL
    installation and is correct for IST, which has no daylight-saving shift.
    """

    output_field = DateField()
    template = "DATE(CONVERT_TZ(%(expressions)s, '+00:00', '+05:30'))"


def _local_date_expression(queryset):
    if connections[queryset.db].vendor == "mysql":
        return _MySQLISTDate("initiated_at")
    return TruncDate(
        "initiated_at",
        tzinfo=datetime_timezone(timedelta(hours=5, minutes=30)),
    )


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
    # ``users`` already selected each visible profile. Seed the ancestry walk
    # from those objects instead of selecting the same rows a second time.
    profiles = {
        platform_user.pk: platform_user.employee_profile
        for platform_user in users
        if hasattr(platform_user, "employee_profile")
    }
    pending_ids = {
        profile.created_by_id
        for profile in profiles.values()
        if profile.created_by_id and profile.created_by_id not in profiles
    }
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
            "employee_id": (profile.employee_id or "").strip() if profile else "",
            "branch": branch,
            "sub_branch": sub_branch,
            "shift": shift,
            "branch_id": branch_id,
            "sub_branch_id": sub_branch_id,
            "shift_id": shift_id,
        }
    return metadata


def _user_metadata(user, visible_ids: set[int]) -> dict[int, dict]:
    """Reuse hierarchy metadata across the HTML filter page and API request."""

    # Import lazily because report_cache itself lazily imports this module for
    # traffic and termination-filter metadata.
    from .report_cache import cached_user_metadata

    return cached_user_metadata(
        "user-hits-users-v2",
        user,
        lambda: _build_user_metadata(visible_ids),
    )


def _legacy_identifier_user_map(metadata: dict[int, dict]) -> dict[str, int]:
    """Map historical attempt snapshots back to their platform users.

    Early respondent links stored the profile/API ``userId`` string before the
    explicit ``platform_user`` foreign key existed.  A user PK remains the most
    authoritative legacy identifier; employee ID, username and email provide
    safe exact-match fallbacks for older production records.
    """

    mapping = {str(user_id): user_id for user_id in metadata}
    for user_id, item in metadata.items():
        for value in (item.get("employee_id"), item.get("username"), item.get("user_email")):
            identifier = str(value or "").strip()
            if identifier:
                mapping.setdefault(identifier, user_id)
    return mapping


def user_hit_filter_options(user) -> dict:
    visible_ids = _visible_user_ids(user)
    metadata = _user_metadata(user, visible_ids)
    # The option-only value fields must not mutate shared cached metadata.
    visible_users = [dict(item) for item in metadata.values()]
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

    # Supplier choices must follow the same activity scope as the aggregate;
    # do not expose a supplier merely because it exists elsewhere in the tool.
    legacy_identifiers = tuple(_legacy_identifier_user_map(metadata))
    supplier_attempts = SurveyAttempt.objects.filter(vendor__isnull=False)
    if not is_super_admin_account(user):
        supplier_attempts = supplier_attempts.filter(
            Q(platform_user_id__in=visible_ids)
            | Q(platform_user_id__isnull=True, user_id__in=legacy_identifiers)
        )
    supplier_rows = supplier_attempts.values(
        "vendor_id", "vendor__first_name", "vendor__last_name",
        "vendor__username", "vendor__email",
    ).distinct().order_by(
        "vendor__first_name", "vendor__last_name", "vendor__username",
    )
    suppliers = []
    for row in supplier_rows:
        name = " ".join(
            part for part in (row["vendor__first_name"], row["vendor__last_name"])
            if part
        ).strip()
        suppliers.append({
            "value": str(row["vendor_id"]),
            "name": name or row["vendor__username"] or row["vendor__email"] or f"Supplier {row['vendor_id']}",
            "email": row["vendor__email"] or "",
        })
    return {
        "users": visible_users,
        "branches": unique_options("branch"),
        "sub_branches": unique_options("sub_branch", ("branch",)),
        "shifts": unique_options("shift", ("branch", "sub_branch")),
        "suppliers": suppliers,
    }


def _aggregate_user_hit_payload(user, params) -> dict:
    """Build a compact, page-neutral user-hit payload.

    Result caching intentionally ignores ``page`` and ``page_size``.  Keeping
    user metadata once and aggregate scalars per user/day avoids materializing
    the full public row schema for every matching day before the caller knows
    which page it needs.
    """

    visible_ids = _visible_user_ids(user)
    complete_visible_ids = set(visible_ids)
    metadata = _user_metadata(user, visible_ids)

    selected_user_values = _csv_values(params.get("user", ""))
    if any(not value.isdigit() for value in selected_user_values):
        raise ValueError("User filters must contain numeric IDs.")
    selected_user_ids = {int(value) for value in selected_user_values}
    if selected_user_ids:
        visible_ids &= selected_user_ids

    selected_supplier_values = _csv_values(params.get("supplier", ""))
    if any(not value.isdigit() for value in selected_supplier_values):
        raise ValueError("Supplier filters must contain numeric IDs.")
    selected_supplier_ids = {int(value) for value in selected_supplier_values}

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

    search = params.get("search", "").strip()
    if search:
        needle = search.casefold()
        visible_ids = {
            user_id for user_id in visible_ids
            if any(
                needle in str(metadata.get(user_id, {}).get(field, "")).casefold()
                for field in (
                    "user_name", "username", "user_email", "branch", "sub_branch", "shift"
                )
            )
        }

    # ``platform_user`` is authoritative for current rows. Older production
    # rows can contain only a profile/API employee ID in the string snapshot.
    # Resolve those exact identifiers without casting or exposing users outside
    # the requester's current hierarchy scope.
    visible_metadata = {
        user_id: metadata[user_id]
        for user_id in visible_ids
        if user_id in metadata
    }
    legacy_user_map = _legacy_identifier_user_map(visible_metadata)
    if is_super_admin_account(user) and visible_ids == complete_visible_ids:
        # A super-admin already owns the complete activity scope. Avoid a large
        # ``IN (...) OR legacy_user_id IN (...)`` predicate which prevents the
        # optimizer from choosing the cleanest aggregate plan. Once user,
        # hierarchy or search filters narrow that scope, use the indexed paths
        # below instead of grouping the entire attempt table and discarding
        # unrelated groups in Python.
        attempt_querysets = [SurveyAttempt.objects.all()]
    else:
        # Current FK-backed activity and the tiny legacy snapshot population
        # use different indexes. Two narrow aggregates are cheaper and more
        # predictable than one cross-column OR, and the groups are merged below.
        attempt_querysets = [
            SurveyAttempt.objects.filter(platform_user_id__in=visible_ids)
        ]
        if legacy_user_map:
            attempt_querysets.append(
                SurveyAttempt.objects.filter(
                    platform_user_id__isnull=True,
                    user_id__in=tuple(legacy_user_map),
                )
            )
    if lower:
        attempt_querysets = [
            queryset.filter(initiated_at__gte=lower)
            for queryset in attempt_querysets
        ]
    if upper:
        attempt_querysets = [
            queryset.filter(initiated_at__lte=upper)
            for queryset in attempt_querysets
        ]
    if selected_supplier_ids:
        attempt_querysets = [
            queryset.filter(vendor_id__in=selected_supplier_ids)
            for queryset in attempt_querysets
        ]

    # Group inside MySQL instead of transferring and looping over every hit in
    # Python. The MySQL expression uses numeric offsets at both ends so a host
    # without named timezone tables still returns the correct IST date.
    completed = Q(status=SurveyAttempt.Status.COMPLETED)
    tablet = Q(entry_device__icontains="tablet") | Q(entry_device__iexact="tab") | Q(entry_device__iexact="t")
    mobile = ~tablet & (
        Q(entry_device__icontains="mobile")
        | Q(entry_device__icontains="phone")
        | Q(entry_device__iexact="m")
    )
    desktop = ~tablet & ~mobile & (
        Q(entry_device__icontains="desktop")
        | Q(entry_device__icontains="laptop")
        | Q(entry_device__iexact="d")
    )
    # A user's current FK rows and historical snapshot-only rows are separate
    # SQL groups. Merge them into the same user/date bucket before presenting
    # the report.
    merged = {}
    for attempts in attempt_querysets:
        grouped = attempts.annotate(
            local_date=_local_date_expression(attempts)
        ).values("platform_user_id", "user_id", "local_date").annotate(
            hits_total=Count("id"),
            hits_desktop=Count("id", filter=desktop),
            hits_mobile=Count("id", filter=mobile),
            hits_tablet=Count("id", filter=tablet),
            completes_total=Count("id", filter=completed),
            completes_desktop=Count("id", filter=completed & desktop),
            completes_mobile=Count("id", filter=completed & mobile),
            completes_tablet=Count("id", filter=completed & tablet),
            survey_terminations=Count(
                "id",
                filter=Q(status=SurveyAttempt.Status.TERMINATED)
                & ~Q(status_source="local_prescreener"),
            ),
        )
        for aggregate in grouped.iterator(chunk_size=2000):
            report_user_id = aggregate["platform_user_id"]
            if report_user_id is None:
                report_user_id = legacy_user_map.get(
                    str(aggregate["user_id"] or "").strip()
                )
            local_date = aggregate["local_date"]
            if report_user_id not in visible_ids or not local_date:
                continue
            bucket = merged.setdefault(
                (report_user_id, local_date),
                {key: 0 for key in AGGREGATE_KEYS},
            )
            for key in bucket:
                bucket[key] += aggregate[key]

    compact_rows = []
    for (report_user_id, local_date), aggregate in merged.items():
        if report_user_id not in visible_metadata:
            continue
        compact_rows.append({
            "user_id": report_user_id,
            "date": local_date.isoformat(),
            **aggregate,
        })
    compact_rows.sort(key=lambda row: (
        visible_metadata[row["user_id"]]["user_name"].casefold(),
        row["user_id"],
    ))
    compact_rows.sort(key=lambda row: row["date"], reverse=True)

    summary = {
        "hits": {"total": 0, **_empty_counts()},
        "completes": {"total": 0, **_empty_counts()},
        "active_users": len({row["user_id"] for row in compact_rows}),
        "days": len({row["date"] for row in compact_rows}),
        "conversion_rate": 0,
        "incidence_rate": 0,
    }
    survey_terminations = 0
    for row in compact_rows:
        hits_classified = row["hits_desktop"] + row["hits_mobile"] + row["hits_tablet"]
        completes_classified = (
            row["completes_desktop"] + row["completes_mobile"] + row["completes_tablet"]
        )
        summary["hits"]["total"] += row["hits_total"]
        summary["hits"]["desktop"] += row["hits_desktop"]
        summary["hits"]["mobile"] += row["hits_mobile"]
        summary["hits"]["tablet"] += row["hits_tablet"]
        summary["hits"]["unclassified"] += max(0, row["hits_total"] - hits_classified)
        summary["completes"]["total"] += row["completes_total"]
        summary["completes"]["desktop"] += row["completes_desktop"]
        summary["completes"]["mobile"] += row["completes_mobile"]
        summary["completes"]["tablet"] += row["completes_tablet"]
        summary["completes"]["unclassified"] += max(
            0, row["completes_total"] - completes_classified
        )
        survey_terminations += row["survey_terminations"]
    if summary["hits"]["total"]:
        summary["conversion_rate"] = round(summary["completes"]["total"] / summary["hits"]["total"] * 100, 1)
    ir_denominator = summary["completes"]["total"] + survey_terminations
    if ir_denominator:
        summary["incidence_rate"] = round(summary["completes"]["total"] / ir_denominator * 100, 2)
    return {
        "rows": compact_rows,
        "metadata": visible_metadata,
        "summary": summary,
    }


def expand_user_hit_rows(compact_rows, metadata: dict[int, dict]) -> list[dict]:
    """Expand only the compact user/day aggregates selected for a page."""

    rows = []
    for aggregate in compact_rows:
        user_meta = metadata.get(aggregate["user_id"])
        if not user_meta:
            continue
        hits_classified = (
            aggregate["hits_desktop"]
            + aggregate["hits_mobile"]
            + aggregate["hits_tablet"]
        )
        completes_classified = (
            aggregate["completes_desktop"]
            + aggregate["completes_mobile"]
            + aggregate["completes_tablet"]
        )
        rows.append({
            **{field: user_meta[field] for field in (
                "user_id", "user_name", "username", "user_email",
                "branch", "sub_branch", "shift",
            )},
            "date": aggregate["date"],
            "hits": {
                "total": aggregate["hits_total"],
                "desktop": aggregate["hits_desktop"],
                "mobile": aggregate["hits_mobile"],
                "tablet": aggregate["hits_tablet"],
                "unclassified": max(0, aggregate["hits_total"] - hits_classified),
            },
            "completes": {
                "total": aggregate["completes_total"],
                "desktop": aggregate["completes_desktop"],
                "mobile": aggregate["completes_mobile"],
                "tablet": aggregate["completes_tablet"],
                "unclassified": max(0, aggregate["completes_total"] - completes_classified),
            },
        })
    return rows


def aggregate_user_hit_payload(user, params) -> dict:
    """Return compact aggregates for page-first API response construction."""

    return _aggregate_user_hit_payload(user, params)


def aggregate_user_hits(user, params) -> tuple[list[dict], dict]:
    """Compatibility wrapper returning the original fully expanded contract."""

    payload = _aggregate_user_hit_payload(user, params)
    return (
        expand_user_hit_rows(payload["rows"], payload["metadata"]),
        payload["summary"],
    )
