"""Cached entry-IP geolocation for market validation and vault enrichment.

The resolver prefers trusted proxy metadata, then an optional local MaxMind
City database, and finally a configurable JSON endpoint. Lookup failures are
fail-open: traffic is rejected only when a reliable country code was resolved.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import requests
from django.conf import settings
from django.core.cache import caches

from .survey_flow import get_request_ip


logger = logging.getLogger(__name__)


def _clean_country_code(value) -> str:
    code = str(value or "").strip().upper()
    return code if len(code) == 2 and code.isalpha() else ""


def _clean_postal_code(value) -> str:
    return str(value or "").strip()[:40]


def _cache_key(ip_value: str) -> str:
    digest = hashlib.sha256(ip_value.encode("utf-8")).hexdigest()
    return f"entry-geo:v1:{digest}"


def _from_maxmind(ip_value: str) -> dict:
    database_path = str(settings.GEOIP_CITY_DB_PATH or "").strip()
    if not database_path or not Path(database_path).is_file():
        return {}
    try:
        import geoip2.database
        with geoip2.database.Reader(database_path) as reader:
            record = reader.city(ip_value)
        return {
            "country_code": _clean_country_code(record.country.iso_code),
            "country": str(record.country.name or "")[:120],
            "postal_code": _clean_postal_code(record.postal.code),
            "source": "maxmind",
        }
    except Exception:
        logger.warning("Local GeoIP lookup failed", exc_info=True)
        return {}


def _from_http(ip_value: str) -> dict:
    endpoint = str(settings.GEOIP_LOOKUP_URL or "").strip()
    if not endpoint:
        return {}
    try:
        response = requests.get(
            endpoint.format(ip=ip_value),
            timeout=settings.GEOIP_LOOKUP_TIMEOUT_SECONDS,
            headers={"Accept": "application/json", "User-Agent": "ExchangeHub/1.0"},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("success") is False:
            return {}
        return {
            "country_code": _clean_country_code(payload.get("country_code") or payload.get("countryCode")),
            "country": str(payload.get("country") or payload.get("country_name") or "")[:120],
            "postal_code": _clean_postal_code(payload.get("postal") or payload.get("postal_code") or payload.get("zip")),
            "source": "http",
        }
    except Exception:
        logger.warning("Remote GeoIP lookup failed for an entry request", exc_info=True)
        return {}


def resolve_entry_geolocation(request) -> dict:
    ip_value = get_request_ip(request)
    if not ip_value:
        return {}
    cache = caches["default"]
    key = _cache_key(ip_value)
    try:
        cached = cache.get(key)
    except Exception:
        cached = None
    if isinstance(cached, dict):
        return cached
    trusted_header_code = (
        _clean_country_code(request.META.get("HTTP_CF_IPCOUNTRY"))
        if settings.TRUST_X_FORWARDED_FOR else ""
    )
    result = _from_maxmind(ip_value)
    if not result or not result.get("country_code") or not result.get("postal_code"):
        remote = _from_http(ip_value)
        if remote:
            result = {
                "country_code": result.get("country_code") or remote.get("country_code", ""),
                "country": result.get("country") or remote.get("country", ""),
                "postal_code": result.get("postal_code") or remote.get("postal_code", ""),
                "source": result.get("source") or remote.get("source", ""),
            }
    if trusted_header_code:
        result["country_code"] = trusted_header_code
        prior_source = result.get("source")
        result["source"] = "trusted_proxy" if not prior_source else f"trusted_proxy+{prior_source}"
    result = {
        "ip": ip_value,
        "country_code": _clean_country_code(result.get("country_code")),
        "country": str(result.get("country") or "")[:120],
        "postal_code": _clean_postal_code(result.get("postal_code")),
        "source": str(result.get("source") or "unknown")[:40],
    }
    try:
        cache.set(key, result, timeout=settings.GEOIP_CACHE_TTL_SECONDS)
    except Exception:
        logger.warning("Could not cache entry GeoIP result", exc_info=True)
    return result


def survey_target_country_code(survey) -> str:
    return _clean_country_code(getattr(survey, "country_code", "")) or _clean_country_code(getattr(survey, "country", ""))


def is_wrong_target_country(survey, location: dict) -> bool:
    expected = survey_target_country_code(survey)
    actual = _clean_country_code((location or {}).get("country_code"))
    return bool(settings.ENFORCE_SURVEY_TARGET_COUNTRY and expected and actual and expected != actual)


def geolocation_client_data(location: dict) -> dict:
    if not location:
        return {}
    return {
        "geo_country_code": location.get("country_code", ""),
        "geo_country": location.get("country", ""),
        "geo_postal_code": location.get("postal_code", ""),
        "geo_source": location.get("source", ""),
    }
