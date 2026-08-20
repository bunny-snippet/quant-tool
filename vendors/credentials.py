"""Encryption and credential-change handling for client integrations."""

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import transaction
from django.utils import timezone


def _fernet() -> Fernet:
    raw = str(settings.INTEGRATION_CREDENTIAL_ENCRYPTION_KEY).encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    """Encrypt a supplier-facing shared secret with the deployment key."""

    value = str(value or "").strip()
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii") if value else ""


def decrypt_secret(value: str) -> str:
    """Decrypt a supplier-facing shared secret without exposing it in models/admin."""

    value = str(value or "").strip()
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Stored supplier callback secret cannot be decrypted. Check the encryption key.") from exc


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest() if token.strip() else ""


def resolve_integration_token(integration) -> str:
    if integration.encrypted_api_token:
        try:
            return _fernet().decrypt(integration.encrypted_api_token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Stored API credential cannot be decrypted. Check the encryption key.") from exc
    if integration.credential_env_key:
        return os.getenv(integration.credential_env_key, "").strip()
    return ""


def _clear_integration_data(integration) -> int:
    return integration.surveys.update(
        entry_link="",
        test_entry_link="",
        raw_data={},
        detail_synced_at=None,
        quota_synced_at=None,
        targeting_synced_at=None,
    )


@transaction.atomic
def set_integration_token(integration, token: str) -> tuple[bool, int]:
    """Store a secret and immediately invalidate only this integration if it changed."""
    token = token.strip()
    fingerprint = token_fingerprint(token)
    previous_fingerprint = integration.credential_fingerprint
    if not previous_fingerprint and integration.credential_env_key:
        previous_fingerprint = token_fingerprint(os.getenv(integration.credential_env_key, ""))
    changed = bool(previous_fingerprint and previous_fingerprint != fingerprint)
    cleared = _clear_integration_data(integration) if changed else 0
    integration.encrypted_api_token = _fernet().encrypt(token.encode("utf-8")).decode("ascii") if token else ""
    integration.credential_fingerprint = fingerprint
    integration.credential_last_four = token[-4:] if token else ""
    integration.credential_changed_at = timezone.now()
    integration.save(update_fields=[
        "encrypted_api_token", "credential_fingerprint", "credential_last_four",
        "credential_changed_at", "updated_at",
    ])
    return changed, cleared


def reconcile_all_integration_credentials() -> dict[str, int]:
    """Detect legacy environment-token changes without invalidating first-time baselines."""
    from .models import ClientIntegration

    checked = changed = cleared = 0
    for integration in ClientIntegration.objects.exclude(credential_env_key=""):
        token = os.getenv(integration.credential_env_key, "").strip()
        if not token:
            continue
        checked += 1
        fingerprint = token_fingerprint(token)
        if not integration.credential_fingerprint:
            integration.credential_fingerprint = fingerprint
            integration.credential_last_four = token[-4:]
            integration.save(update_fields=["credential_fingerprint", "credential_last_four", "updated_at"])
        elif integration.credential_fingerprint != fingerprint:
            was_changed, count = set_integration_token(integration, token)
            changed += int(was_changed)
            cleared += count
    return {"checked": checked, "changed": changed, "cleared": cleared}
