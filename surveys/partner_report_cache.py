"""Small isolated report cache; no dependency on Quest's caching stack."""

import hashlib
import json
from threading import RLock

from django.core.cache import cache
from accounts.access import activity_visible_user_ids, effective_permission_codes
from accounts.profile_context import employee_profile_for_user

_LOCKS = [RLock() for _ in range(32)]


def cached_report_payload(namespace, request, factory, *, timeout=30, extra_scope=None):
    user = request.user
    profile = employee_profile_for_user(user)
    role = getattr(profile, "role", None)
    scope = {
        "user": user.pk, "superuser": user.is_superuser,
        "permissions": sorted(effective_permission_codes(user)),
        "visible": sorted(activity_visible_user_ids(user)) if not user.is_superuser else [],
        "cpi_percent": str(getattr(role, "cpi_visibility_percent", "")),
        "account_type": getattr(profile, "account_type", ""),
        "params": sorted((k, v) for k, v in request.GET.lists() if k != "format"),
        "extra": extra_scope,
    }
    digest = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    key = "quant:" + namespace + ":" + digest
    # Bounded local locks avoid simultaneous duplicate queries in a web process.
    with _LOCKS[int(digest[:8], 16) % len(_LOCKS)]:
        try:
            existing = cache.get(key)
        except Exception:
            existing = None
        if existing is not None:
            return existing
        result = factory()
        try:
            cache.set(key, result, timeout=timeout)
        except Exception:
            pass  # Cache outages must not make the report unavailable.
        return result
