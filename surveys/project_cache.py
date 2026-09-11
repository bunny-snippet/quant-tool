"""Non-authoritative Projects caches isolated from respondent/profile data."""

import secrets
import time

from django.conf import settings
from django.db.models import Max, Min

from .country_dimensions import country_dimension_rows

from config.cache_utils import (
    safe_cache_add,
    safe_cache_compare_delete,
    safe_cache_generation,
    safe_cache_get,
    safe_cache_increment,
    safe_cache_set,
    stable_cache_key,
)


CACHE_ALIAS = "projects"
_FILTER_VERSION_KEY = "projects:filters-version"
_COUNT_VERSION_KEY = "projects:count-version"
_INVALIDATION_THROTTLE_KEY = "projects:invalidate-throttle"
_MISSING = object()
_SINGLEFLIGHT_LOCK_SECONDS = 30
_SINGLEFLIGHT_WAIT_SECONDS = 2


def _singleflight_get_or_set(key, factory, *, timeout, jitter_seconds):
    """Build one cold Projects cache key without multiplying its SQL queries.

    Inventory versions intentionally make old values unreachable immediately.
    When several web workers encounter the new version together, exactly one
    worker rebuilds it while peers wait for that authoritative result. Cache
    outages still fail open, preserving MySQL as the source of truth.
    """

    cached = safe_cache_get(key, _MISSING, alias=CACHE_ALIAS)
    if cached is not _MISSING:
        return cached

    lock_key = f"{key}:build-lock"
    lock_token = secrets.randbits(63) or 1
    owns_lock = safe_cache_add(
        lock_key,
        lock_token,
        timeout=_SINGLEFLIGHT_LOCK_SECONDS,
        alias=CACHE_ALIAS,
    )
    if owns_lock is not False:
        try:
            value = factory()
            safe_cache_set(
                key,
                value,
                timeout=timeout,
                jitter_seconds=jitter_seconds,
                alias=CACHE_ALIAS,
            )
            return value
        finally:
            if owns_lock:
                # A slow builder can outlive its lease. Compare-and-delete
                # prevents it from removing a replacement owner's lock.
                safe_cache_compare_delete(
                    lock_key,
                    lock_token,
                    alias=CACHE_ALIAS,
                )

    # The indexed rebuild should normally complete well inside this bound. A
    # bounded wait avoids a cache miss stampede without leaving a request stuck
    # behind a failed worker indefinitely.
    deadline = time.monotonic() + _SINGLEFLIGHT_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(0.05)
        cached = safe_cache_get(key, _MISSING, alias=CACHE_ALIAS)
        if cached is not _MISSING:
            return cached

    # The lock owner may have died or Redis may have dropped the value. Falling
    # back to the database keeps this cache strictly non-authoritative.
    return factory()


def _version(key: str) -> int:
    return safe_cache_generation(key, alias=CACHE_ALIAS)


def invalidate_project_cache(
    *,
    throttle_seconds: int = 0,
    filters: bool = True,
    counts: bool = True,
) -> bool:
    """Invalidate metadata/counts without scanning or flushing Redis DB 3.

    High-frequency feeds can request a small throttle window. The first
    process increments the shared version while later callbacks in that window
    reuse it, preventing every five-second Cint delivery from invalidating all
    user-scoped project metadata. Filter metadata and filtered counts use
    independent versions so a busy inventory feed can keep pagination totals
    fresh without forcing expensive filter-option rebuilds at the same rate.
    Cache outages fail open and still invalidate.
    """

    if not filters and not counts:
        return False
    throttle_seconds = max(0, int(throttle_seconds or 0))
    if throttle_seconds:
        throttle_scope = f"{int(filters)}:{int(counts)}"
        acquired = safe_cache_add(
            f"{_INVALIDATION_THROTTLE_KEY}:{throttle_scope}",
            1,
            timeout=throttle_seconds,
            alias=CACHE_ALIAS,
        )
        if acquired is False:
            return False

    if filters:
        safe_cache_increment(
            _FILTER_VERSION_KEY,
            default=_version(_FILTER_VERSION_KEY),
            alias=CACHE_ALIAS,
        )
    if counts:
        safe_cache_increment(
            _COUNT_VERSION_KEY,
            default=_version(_COUNT_VERSION_KEY),
            alias=CACHE_ALIAS,
        )
    return True


def project_filter_metadata(
    queryset,
    *,
    user_id: int,
    client_scoped: bool,
    include_cpi: bool,
    cpi_field: str = "cpi",
    cpi_queryset=None,
) -> dict:
    key = stable_cache_key(
        f"projects:v{_version(_FILTER_VERSION_KEY)}:filters",
        {
            "user_id": user_id,
            "client_scoped": client_scoped,
            "include_cpi": include_cpi,
            "cpi_field": cpi_field,
        },
    )

    def load():
        # Providers can spell the same market label differently (or leave it
        # blank) while using the same canonical country code.  The filter is
        # keyed by country_code, so publish exactly one option per code.  This
        # also prevents the browser from submitting values such as ``FR,FR``.
        country_rows = country_dimension_rows(queryset)
        countries_by_code = {}
        for code, name in country_rows:
            current = countries_by_code.get(code)
            # Prefer the most descriptive non-empty label while retaining the
            # old index-friendly DISTINCT query. MySQL otherwise performs a
            # full grouped MAX scan across the entire inventory on cache miss.
            if current is None or (bool(name), len(name or ""), name or "") > (
                bool(current), len(current or ""), current or ""
            ):
                countries_by_code[code] = name
        countries = sorted(countries_by_code.items())
        company_field = "client__name" if client_scoped else "company_name"
        companies = list(
            queryset.exclude(**{company_field: ""})
            .values_list(company_field, flat=True)
            .distinct()
            .order_by(company_field)
        )
        buyer_rows = list(
            queryset.exclude(buyer_id="")
            .values("buyer_id", "client__name", "company_name")
            .distinct()
            .order_by("buyer_id")
        )
        survey_types = list(
            queryset.exclude(survey_type="")
            .values_list("survey_type", flat=True)
            .distinct()
            .order_by("survey_type")
        )
        cpi_source = cpi_queryset if cpi_queryset is not None else queryset
        cpi_bounds = (
            cpi_source.aggregate(
                minimum=Min(cpi_field),
                maximum=Max(cpi_field),
            )
            if include_cpi
            else {"minimum": None, "maximum": None}
        )
        return {
            "countries": countries,
            "companies": companies,
            "buyer_options": [
                {
                    "value": row["buyer_id"],
                    "client_value": (
                        row["client__name"] if client_scoped else row["company_name"]
                    ) or "",
                }
                for row in buyer_rows
            ],
            "survey_types": survey_types,
            "cpi_min": cpi_bounds["minimum"],
            "cpi_max": cpi_bounds["maximum"],
        }

    return _singleflight_get_or_set(
        key,
        load,
        timeout=settings.PROJECT_CACHE_FILTERS_TTL_SECONDS,
        jitter_seconds=settings.PROJECT_CACHE_TTL_JITTER_SECONDS,
    )


def project_filtered_count(request, queryset) -> int:
    count_neutral_parameters = {"page", "page_size", "ordering", "format"}
    # DRF's public ``request.auth`` property performs lazy authentication when
    # accessed. Pagination runs after authentication, so its private snapshot
    # is already populated; reading it directly also keeps this cache helper
    # side-effect free for RequestFactory callers and tests.
    request_auth = getattr(request, "_auth", None)
    key = stable_cache_key(
        f"projects:v{_version(_COUNT_VERSION_KEY)}:count",
        {
            "user_id": request.user.pk,
            "api_key_id": getattr(request_auth, "pk", None),
            "query": sorted(
                (key, tuple(values))
                for key, values in request.query_params.lists()
                if key not in count_neutral_parameters
            ),
        },
    )

    def load_count():
        """Count only unique project IDs, not every annotated list column.

        The Projects list queryset can carry viewer-pricing and completes
        annotations plus joins used by organization/supplier visibility. A
        direct ``queryset.count()`` makes MySQL materialize those wide rows in
        a DISTINCT subquery. Pagination only needs unique survey primary keys,
        so clear ordering and narrow the selected columns before counting.
        Filters and permission predicates remain attached to the queryset.
        """

        count_queryset = queryset.order_by()
        # Scope querysets that already require DISTINCT must keep it for
        # correctness, but narrow the subquery back to the primary key so list
        # annotations and wide model columns are not materialized. Ordinary
        # scalar/Exists querysets take the cheaper direct COUNT(*) path.
        if count_queryset.query.distinct:
            count_queryset = count_queryset.values("pk")
        return count_queryset.count()

    return int(_singleflight_get_or_set(
        key,
        load_count,
        timeout=settings.PROJECT_CACHE_COUNT_TTL_SECONDS,
        jitter_seconds=settings.PROJECT_CACHE_TTL_JITTER_SECONDS,
    ))
