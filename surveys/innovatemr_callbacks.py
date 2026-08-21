"""Verification helpers for InnovateMR browser redirects.

InnovateMR signs the fully hydrated redirect URL with the configured shared
secret.  The URL used as the HMAC message must still contain the hash query
parameter, but its value must be empty.  Query order and percent-encoding are
therefore intentionally preserved from the raw request instead of rebuilding
the URL from ``request.GET``.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from django.conf import settings


SUPPORTED_ALGORITHMS = {
    "sha256": hashlib.sha256,
    "sha1": hashlib.sha1,
    "md5": hashlib.md5,
}
HASH_PARAMETER_NAMES = {"hash", "hashdata"}


@dataclass(frozen=True)
class CallbackVerification:
    valid: bool
    error: str = ""


def sign_callback_url(unsigned_url: str, secret: str, algorithm: str = "sha256") -> str:
    """Return the lowercase InnovateMR HMAC digest for an unsigned URL."""

    digest = SUPPORTED_ALGORITHMS.get(str(algorithm or "").strip().lower())
    if digest is None:
        raise ValueError("Unsupported InnovateMR callback hash algorithm.")
    return hmac.new(
        str(secret).encode("utf-8"),
        str(unsigned_url).encode("utf-8"),
        digest,
    ).hexdigest()


def _unsigned_query_and_hash(raw_query: str) -> tuple[str, str] | tuple[None, None]:
    segments = str(raw_query or "").split("&") if raw_query else []
    matches: list[tuple[int, str]] = []
    for index, segment in enumerate(segments):
        raw_name, separator, raw_value = segment.partition("=")
        if unquote_plus(raw_name).strip().casefold() in HASH_PARAMETER_NAMES:
            if not separator:
                raw_value = ""
            matches.append((index, unquote_plus(raw_value).strip()))

    # Multiple signature fields are ambiguous and could let a proxy/app verify
    # a different value from the one used by the provider.
    if len(matches) != 1:
        return None, None

    index, received_hash = matches[0]
    raw_name = segments[index].partition("=")[0]
    segments[index] = f"{raw_name}="
    return "&".join(segments), received_hash


def _candidate_unsigned_urls(request, unsigned_query: str) -> list[str]:
    request_url = request.build_absolute_uri(request.path)
    request_parts = urlsplit(request_url)
    urls = [
        urlunsplit((
            request_parts.scheme,
            request_parts.netloc,
            request_parts.path,
            unsigned_query,
            "",
        ))
    ]

    # Behind a reverse proxy Django may see an internal host/scheme.  The
    # public origin is the URL InnovateMR actually signed, so verify that exact
    # deployment URL as a second, explicit candidate.
    public_base = str(getattr(settings, "PUBLIC_APP_BASE_URL", "") or "").strip().rstrip("/")
    if public_base:
        public_parts = urlsplit(public_base)
        public_path = f"{public_parts.path.rstrip('/')}{request.path}"
        public_url = urlunsplit((
            public_parts.scheme,
            public_parts.netloc,
            public_path,
            unsigned_query,
            "",
        ))
        if public_url not in urls:
            urls.append(public_url)
    return urls


def verify_callback_request(request) -> CallbackVerification:
    """Verify one InnovateMR redirect without exposing the shared secret."""

    secret = str(getattr(settings, "INNOVATEMR_CALLBACK_HASH_KEY", "") or "")
    if not secret:
        return CallbackVerification(False, "not_configured")

    algorithm = str(
        getattr(settings, "INNOVATEMR_CALLBACK_HASH_ALGORITHM", "sha256") or "sha256"
    ).strip().lower()
    if algorithm not in SUPPORTED_ALGORITHMS:
        return CallbackVerification(False, "unsupported_algorithm")

    unsigned_query, received_hash = _unsigned_query_and_hash(
        request.META.get("QUERY_STRING", "")
    )
    if unsigned_query is None:
        return CallbackVerification(False, "missing_or_duplicate_hash")
    if not received_hash:
        return CallbackVerification(False, "empty_hash")

    for unsigned_url in _candidate_unsigned_urls(request, unsigned_query):
        expected_hash = sign_callback_url(unsigned_url, secret, algorithm)
        if hmac.compare_digest(received_hash.casefold(), expected_hash.casefold()):
            return CallbackVerification(True)
    return CallbackVerification(False, "hash_mismatch")
