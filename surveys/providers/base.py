import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any


ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ProviderError(RuntimeError):
    """Safe upstream/provider error suitable for operational audit logs."""


class ProviderConfigurationError(ProviderError):
    """Raised when an integration references missing or invalid environment configuration."""


@dataclass(frozen=True)
class NormalizedSurvey:
    source_key: str
    numeric_source_id: int | None
    modified_at: datetime | None
    values: dict[str, Any]
    raw_data: dict[str, Any]


def environment_value(reference: str, label: str) -> str:
    reference = str(reference or "").strip()
    if not reference or not ENV_NAME_RE.fullmatch(reference):
        raise ProviderConfigurationError(f"Configure a valid environment-variable name for {label}.")
    value = os.getenv(reference, "")
    if not value:
        raise ProviderConfigurationError(f"Environment variable {reference} is not configured.")
    return value


class SurveyProvider:
    code = "base"
    label = "Survey provider"
    minimum_sync_interval_seconds = 60
    credential_fields: tuple[tuple[str, str], ...] = ()
    default_base_url = ""

    def __init__(self, integration, *, session=None):
        self.integration = integration
        self.session = session

    def test_connection(self) -> dict[str, Any]:
        raise NotImplementedError

    def inventory(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def normalize_inventory_item(self, payload: dict[str, Any], seen_at) -> NormalizedSurvey:
        raise NotImplementedError

    def refresh_details(self, survey) -> None:
        raise NotImplementedError

    def duplicate_check(self, survey, attempt, ip_address: str | None) -> bool:
        return False

    def build_outbound_url(self, survey, attempt, answers: dict[str, Any]) -> str:
        raise NotImplementedError
