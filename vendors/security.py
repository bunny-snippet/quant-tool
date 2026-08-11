import hashlib
import hmac
import secrets

from django.conf import settings


API_KEY_PREFIX = "exh_"


def digest_api_key(raw_key: str) -> str:
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        raw_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def generate_api_key() -> tuple[str, str, str, str]:
    raw_key = f"{API_KEY_PREFIX}{secrets.token_urlsafe(36)}"
    return raw_key, raw_key[:12], raw_key[-4:], digest_api_key(raw_key)
