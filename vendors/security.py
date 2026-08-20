import hashlib
import hmac
import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing


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


def generate_callback_secret() -> str:
    """Return a high-entropy HMAC key shown only when created or rotated."""

    return secrets.token_urlsafe(32)


def sign_supplier_callback(parameters: dict, secret: str) -> str:
    """Sign a deterministic callback query for an external supplier."""

    canonical = urlencode(sorted((str(key), str(value)) for key, value in parameters.items()))
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


DELIVERY_TOKEN_SALT = "vendors.external-api-delivery.v1"


def generate_delivery_token(api_key_id: int, survey_id: int) -> str:
    """Bind an API inventory entry link to one API key and one local survey."""

    return signing.dumps(
        {"api_key_id": int(api_key_id), "survey_id": int(survey_id)},
        key=settings.SECRET_KEY,
        salt=DELIVERY_TOKEN_SALT,
        compress=True,
    )


def decode_delivery_token(token: str) -> dict:
    """Validate and decode an API delivery token."""

    value = signing.loads(
        str(token or ""), key=settings.SECRET_KEY, salt=DELIVERY_TOKEN_SALT
    )
    if not isinstance(value, dict):
        raise signing.BadSignature("Invalid supplier delivery token.")
    return value
