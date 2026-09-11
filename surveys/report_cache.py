"""Permission-scoped caches for expensive read-only workspace reports.

MySQL remains authoritative.  Short result TTLs keep active traffic close to
real time while preventing repeated filters, page navigation and dashboard
refreshes from recalculating the same aggregates.  Filter metadata has a longer
TTL because country/client/hierarchy choices change far less often.
"""

from collections.abc import Callable, Iterable
import hashlib
import secrets
import time
from typing import Any

from django.conf import settings

from accounts.access import (
    activity_visible_user_ids,
    activity_visibility_cache_generation,
    effective_permission_codes,
    permission_cache_generation,
)
from accounts.request_cache import request_cached
from config.cache_utils import (
    safe_cache_add,
    safe_cache_compare_delete,
    safe_cache_generation,
    safe_cache_get,
    safe_cache_increment,
    safe_cache_set,
    stable_cache_key,
)


CACHE_ALIAS = "reports"
_MISSING = object()
REPORT_METADATA_GENERATION_KEY = "reports:metadata:generation"
_REFRESH_LOCK_SECONDS = 30
_COLD_WAIT_SECONDS = 5


def report_metadata_generation() -> int:
    """Return the global version for filter/hierarchy selector payloads."""

    return safe_cache_generation(REPORT_METADATA_GENERATION_KEY, alias=CACHE_ALIAS)


def invalidate_report_metadata_cache() -> int:
    """Expire selector payloads after their underlying dimensions change."""

    return safe_cache_increment(
        REPORT_METADATA_GENERATION_KEY,
        default=report_metadata_generation(),
        alias=CACHE_ALIAS,
    )


def _cached_with_stale_revalidation(key: str, factory: Callable[[], Any], *, timeout: int) -> Any:
    """Prevent a popular report key from stampeding MySQL when it expires.

    Fresh results are returned normally.  One request refreshes an expired
    value while concurrent requests receive the last successful payload.  On a
    cold miss, peers briefly wait for the lock owner instead of launching the
    same full-table aggregate hundreds of times.
    """

    now = time.time()
    record = safe_cache_get(key, _MISSING, alias=CACHE_ALIAS)
    if isinstance(record, dict) and record.get("_report_cache_record") is True:
        if float(record.get("fresh_until") or 0) > now:
            return record.get("payload")
        stale_payload = record.get("payload")
    else:
        stale_payload = _MISSING

    lock_key = f"{key}:refresh-lock"
    lock_token = secrets.randbits(63) or 1
    owns_lock = safe_cache_add(
        lock_key,
        lock_token,
        timeout=_REFRESH_LOCK_SECONDS,
        alias=CACHE_ALIAS,
    )
    if owns_lock:
        try:
            value = factory()
            safe_cache_set(
                key,
                {
                    "_report_cache_record": True,
                    "fresh_until": time.time() + timeout,
                    "payload": value,
                },
                # Keep stale data long enough for fail-open background-style
                # refreshes, without making reporting permanently stale.
                timeout=timeout + max(60, timeout * 4),
                jitter_seconds=settings.REPORT_CACHE_TTL_JITTER_SECONDS,
                alias=CACHE_ALIAS,
            )
            return value
        finally:
            if owns_lock:
                # The database factory can outlive its lease. Never let an
                # older builder remove a replacement worker's lock.
                safe_cache_compare_delete(
                    lock_key,
                    lock_token,
                    alias=CACHE_ALIAS,
                )

    if stale_payload is not _MISSING:
        return stale_payload
    if owns_lock is None:
        return factory()

    # A cold cache has no stale result. Production metadata queries can take
    # longer than 400 ms, so wait for the owner through the observed slow-query
    # range before failing open to MySQL.
    deadline = time.monotonic() + _COLD_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(0.05)
        peer_record = safe_cache_get(key, _MISSING, alias=CACHE_ALIAS)
        if isinstance(peer_record, dict) and peer_record.get("_report_cache_record") is True:
            return peer_record.get("payload")
    return factory()


def _parameter_lists(request, neutral_parameters: Iterable[str]) -> list:
    source = getattr(request, "query_params", None) or request.GET
    neutral = set(neutral_parameters)
    return sorted(
        (key, tuple(values))
        for key, values in source.lists()
        if key not in neutral
    )


def report_viewer_scope(user) -> dict:
    """Build a constant-size, permission-safe viewer fingerprint for cache keys.

    The generation values are incremented synchronously whenever role grants or
    hierarchy visibility changes. This avoids enumerating and hashing every
    visible user on each report cache hit while still making old scoped payloads
    unreachable immediately after an access change.
    """

    def fingerprint(values) -> str:
        digest = hashlib.sha256()
        for value in sorted(values, key=str):
            encoded = str(value).encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
        return digest.hexdigest()[:32]

    def build():
        # The concrete fingerprints bind the cache entry to the same
        # request-local permission and hierarchy snapshots used to build the
        # queryset/payload. Generations alone are insufficient if a revocation
        # commits between queryset construction and cache-key construction.
        permission_snapshot = effective_permission_codes(user)
        visibility_snapshot = activity_visible_user_ids(user)
        return {
            "user_id": user.pk,
            "superuser": bool(user.is_superuser),
            "permission_generation": permission_cache_generation(),
            "visibility_generation": activity_visibility_cache_generation(),
            "permission_fingerprint": fingerprint(permission_snapshot),
            "visibility_fingerprint": fingerprint(visibility_snapshot),
        }

    return request_cached(("report-viewer-scope", user.pk), build)


def cached_report_payload(
    namespace: str,
    request,
    factory: Callable[[], Any],
    *,
    timeout: int | None = None,
    neutral_parameters: Iterable[str] = (
        "page", "page_size", "format", "ordering", "include_summary",
    ),
    extra_scope: Any = None,
) -> Any:
    """Cache a filtered aggregate/payload without exposing query values in keys."""

    key = stable_cache_key(
        f"reports:{namespace}",
        {
            "viewer": report_viewer_scope(request.user),
            "query": _parameter_lists(request, neutral_parameters),
            "extra": extra_scope,
        },
    )
    return _cached_with_stale_revalidation(
        key,
        factory,
        timeout=timeout or settings.REPORT_CACHE_RESULT_TTL_SECONDS,
    )


def cached_user_metadata(
    namespace: str,
    user,
    factory: Callable[[], Any],
    *,
    extra_scope: Any = None,
    timeout: int | None = None,
) -> Any:
    """Cache hierarchy/filter selector metadata for one effective viewer scope."""

    key = stable_cache_key(
        f"reports:{namespace}:metadata",
        {
            "viewer": report_viewer_scope(user),
            "extra": extra_scope,
            "metadata_generation": report_metadata_generation(),
        },
    )
    return _cached_with_stale_revalidation(
        key,
        factory,
        timeout=timeout or settings.REPORT_CACHE_METADATA_TTL_SECONDS,
    )


def cached_integration_metadata(
    namespace: str,
    integration,
    factory: Callable[[], Any],
    *,
    timeout: int | None = None,
) -> Any:
    """Cache expensive provider-card counters shared by authorized viewers."""

    key = stable_cache_key(
        f"reports:integration:{namespace}",
        {
            "integration_id": integration.pk,
            "integration_updated_at": integration.updated_at,
        },
    )
    return _cached_with_stale_revalidation(
        key,
        factory,
        timeout=timeout or settings.REPORT_CACHE_RESULT_TTL_SECONDS,
    )


def cached_integration_metadata_batch(
    namespace: str,
    integrations,
    factory: Callable[[], Any],
    *,
    timeout: int | None = None,
) -> Any:
    """Cache one set-based card payload for an exact integration page."""

    integration_scope = sorted(
        (integration.pk, integration.updated_at)
        for integration in integrations
    )
    key = stable_cache_key(
        f"reports:integration-batch:{namespace}",
        integration_scope,
    )
    return _cached_with_stale_revalidation(
        key,
        factory,
        timeout=timeout or settings.REPORT_CACHE_RESULT_TTL_SECONDS,
    )


def traffic_filter_metadata(user, visible_attempts, visible_surveys) -> dict:
    """Return cached Traffic filter choices without caching attempt rows."""

    def load_hierarchy():
        from .user_hits import user_hit_filter_options

        return user_hit_filter_options(user)

    def load_survey_dimensions():
        from .country_dimensions import country_dimension_rows

        return {
            "countries": [
                {"country_code": code, "country": label}
                for code, label in country_dimension_rows(visible_surveys)
            ],
        }

    def load_attempt_dimensions():
        supplier_rows = list(
            visible_attempts.filter(vendor__isnull=False)
            .values(
                "vendor_id",
                "vendor__first_name",
                "vendor__last_name",
                "vendor__username",
                "vendor__email",
            )
            .distinct()
            .order_by("vendor__first_name", "vendor__last_name", "vendor__username")
        )
        suppliers = []
        for row in supplier_rows:
            full_name = " ".join(
                part for part in (row["vendor__first_name"], row["vendor__last_name"]) if part
            ).strip()
            suppliers.append({
                "value": str(row["vendor_id"]),
                "name": full_name or row["vendor__username"] or row["vendor__email"] or f"Supplier {row['vendor_id']}",
                "email": row["vendor__email"] or "",
            })
        return {
            "suppliers": suppliers,
            "clients": list(
                visible_attempts.filter(survey__client__isnull=False)
                .values("survey__client_id", "survey__client__name")
                .distinct()
                .order_by("survey__client__name")
            ),
            "buyers": list(
                visible_attempts.exclude(survey__buyer_id="")
                .values("survey__buyer_id", "survey__client_id")
                .distinct()
                .order_by("survey__buyer_id")
            ),
        }

    # Access/hierarchy changes invalidate every scoped key synchronously. Both
    # inventory- and attempt-derived dimensions use a short bounded TTL: live
    # survey feeds and respondent starts are too frequent to rotate every
    # viewer's metadata generation on each row write.
    return {
        **cached_user_metadata("report-hierarchy-v1", user, load_hierarchy),
        **cached_user_metadata(
            "traffic-survey-dimensions-v1",
            user,
            load_survey_dimensions,
            timeout=settings.REPORT_CACHE_DYNAMIC_METADATA_TTL_SECONDS,
        ),
        **cached_user_metadata(
            "traffic-attempt-dimensions-v1",
            user,
            load_attempt_dimensions,
            timeout=settings.REPORT_CACHE_DYNAMIC_METADATA_TTL_SECONDS,
        ),
    }


def term_filter_metadata(user, base_queryset) -> dict:
    """Return cached Term Report selector values and hierarchy choices."""

    def load_hierarchy():
        from .user_hits import user_hit_filter_options

        return user_hit_filter_options(user)

    def load_attempt_dimensions():
        supplier_rows = list(
            base_queryset.filter(vendor__isnull=False)
            .values(
                "vendor_id", "vendor__first_name", "vendor__last_name",
                "vendor__username", "vendor__email",
            )
            .distinct()
            .order_by("vendor__first_name", "vendor__last_name", "vendor__username")
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
            "suppliers": suppliers,
            "countries": list(
                base_queryset.exclude(survey__country_code="")
                .values("survey__country_code", "survey__country")
                .distinct()
                .order_by("survey__country_code")
            ),
            "clients": list(
                base_queryset.filter(survey__client__isnull=False)
                .values("survey__client_id", "survey__client__name")
                .distinct()
                .order_by("survey__client__name")
            ),
            "buyers": list(
                base_queryset.exclude(survey__buyer_id="")
                .values("survey__client_id", "survey__buyer_id")
                .distinct()
                .order_by("survey__buyer_id")
            ),
        }

    # Keep namespaces versioned whenever visibility/scoping rules change. The
    # short attempt-dimension TTL bounds freshness without allowing each live
    # respondent callback to stampede the selector DISTINCT queries.
    return {
        **cached_user_metadata("report-hierarchy-v1", user, load_hierarchy),
        **cached_user_metadata(
            "term-attempt-dimensions-v2",
            user,
            load_attempt_dimensions,
            timeout=settings.REPORT_CACHE_DYNAMIC_METADATA_TTL_SECONDS,
        ),
    }
