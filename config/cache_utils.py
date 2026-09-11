"""Small fail-open helpers for non-authoritative Django cache data."""

import hashlib
import json
import logging
import secrets
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.core.cache import caches


logger = logging.getLogger(__name__)
_MISSING = object()


def jittered_ttl(base_seconds: int | None = None, jitter_seconds: int | None = None) -> int:
    """Return a positive TTL spread around the configured base.

    The random spread prevents a large group of related keys from expiring in
    the same second and stampeding the database.
    """

    base = max(1, int(base_seconds or settings.CACHE_DEFAULT_TTL_SECONDS))
    jitter = max(
        0,
        int(
            settings.CACHE_TTL_JITTER_SECONDS
            if jitter_seconds is None
            else jitter_seconds
        ),
    )
    if not jitter:
        return base
    return max(1, base - jitter + secrets.randbelow((jitter * 2) + 1))


def stable_cache_key(namespace: str, value: Any = None) -> str:
    """Build a bounded key without exposing filter values in Redis key names."""

    if value is None:
        return namespace
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:32]
    return f"{namespace}:{digest}"


def _cache(alias: str):
    return caches[alias]


def safe_cache_get(key: str, default: Any = None, *, alias: str = "default") -> Any:
    try:
        return _cache(alias).get(key, default)
    except Exception:
        logger.warning("Cache read failed for key namespace=%s", key.split(":", 1)[0], exc_info=True)
        return default


def safe_cache_set(
    key: str,
    value: Any,
    *,
    timeout: int | None = None,
    jitter_seconds: int | None = None,
    alias: str = "default",
) -> bool:
    try:
        _cache(alias).set(
            key,
            value,
            timeout=jittered_ttl(timeout, jitter_seconds),
        )
        return True
    except Exception:
        logger.warning("Cache write failed for key namespace=%s", key.split(":", 1)[0], exc_info=True)
        return False


def safe_cache_add(
    key: str,
    value: Any,
    *,
    timeout: int | None,
    alias: str = "default",
) -> bool | None:
    """Atomically add a coordination key, optionally without expiry.

    ``False`` means another process already owns the key. ``None`` means the
    cache is unavailable, allowing callers to fail open instead of making a
    non-authoritative Redis dependency block inventory updates.
    """

    try:
        normalized_timeout = None if timeout is None else max(1, int(timeout))
        return bool(_cache(alias).add(key, value, timeout=normalized_timeout))
    except Exception:
        logger.warning(
            "Cache add failed for key namespace=%s",
            key.split(":", 1)[0],
            exc_info=True,
        )
        return None


def safe_cache_generation(key: str, *, alias: str = "default") -> int:
    """Return a persistent, non-repeating cache namespace generation.

    A literal fallback such as ``1`` is unsafe for authorization-derived keys:
    evicting only the generation key could make an older payload stored in a
    separate cache database reachable again. Missing generations are therefore
    initialized atomically from a random 63-bit seed. During a cache outage the
    per-call random value intentionally prevents reuse of cached authorization
    payloads while callers continue to fail open to the database.
    """

    missing = object()
    cached = safe_cache_get(key, missing, alias=alias)
    if cached is not missing:
        try:
            return int(cached)
        except (TypeError, ValueError):
            pass

    seed = secrets.randbits(63) or 1
    created = safe_cache_add(key, seed, timeout=None, alias=alias)
    if created:
        return seed
    if created is False:
        winner = safe_cache_get(key, missing, alias=alias)
        if winner is not missing:
            try:
                return int(winner)
            except (TypeError, ValueError):
                pass
    return seed


def safe_cache_delete(key: str, *, alias: str = "default") -> bool:
    try:
        return bool(_cache(alias).delete(key))
    except Exception:
        logger.warning("Cache delete failed for key namespace=%s", key.split(":", 1)[0], exc_info=True)
        return False


def safe_cache_compare_delete(
    key: str,
    expected_value: Any,
    *,
    alias: str = "default",
) -> bool:
    """Delete a coordination key only while it still contains our token.

    Production uses Django's built-in Redis backend, where a tiny Lua script
    makes the comparison and deletion atomic. Other cache backends retain a
    best-effort compare/delete fallback for local development and tests.
    """

    try:
        backend = _cache(alias)
        redis_cache = getattr(backend, "_cache", None)
        get_client = getattr(redis_cache, "get_client", None)
        if callable(get_client):
            storage_key = backend.make_and_validate_key(key)
            client = get_client(storage_key, write=True)
            deleted = client.eval(
                """
                if redis.call('get', KEYS[1]) == ARGV[1] then
                    return redis.call('del', KEYS[1])
                end
                return 0
                """,
                1,
                storage_key,
                expected_value,
            )
            return bool(deleted)
        if backend.get(key, object()) != expected_value:
            return False
        return bool(backend.delete(key))
    except Exception:
        logger.warning(
            "Cache compare-delete failed for key namespace=%s",
            key.split(":", 1)[0],
            exc_info=True,
        )
        return False


def safe_cache_get_or_set(
    key: str,
    factory: Callable[[], Any],
    *,
    timeout: int | None = None,
    jitter_seconds: int | None = None,
    alias: str = "default",
) -> Any:
    cached = safe_cache_get(key, _MISSING, alias=alias)
    if cached is not _MISSING:
        return cached
    value = factory()
    safe_cache_set(
        key,
        value,
        timeout=timeout,
        jitter_seconds=jitter_seconds,
        alias=alias,
    )
    return value


def safe_cache_increment(key: str, *, default: int = 1, alias: str = "default") -> int:
    """Increment a namespace version without making cache availability critical."""

    try:
        backend = _cache(alias)
        backend.add(key, default, timeout=None)
        return int(backend.incr(key))
    except ValueError:
        try:
            _cache(alias).set(key, default + 1, timeout=None)
        except Exception:
            logger.warning(
                "Cache version reset failed for key namespace=%s",
                key.split(":", 1)[0],
                exc_info=True,
            )
        return default + 1
    except Exception:
        logger.warning("Cache increment failed for key namespace=%s", key.split(":", 1)[0], exc_info=True)
        return default
