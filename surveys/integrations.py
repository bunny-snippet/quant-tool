import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class InnovateMRAPIError(RuntimeError):
    """Raised when a configured survey provider returns an invalid response."""


class InnovateMRNotFound(InnovateMRAPIError):
    """Raised when a survey-provider resource does not exist."""


@dataclass
class PagedSurveyResult:
    surveys: list[dict[str, Any]]
    pages: int


BIOBRAIN_FIELD_MAP = {
    "surveyId": "SurveyId", "surveyName": "Name", "CPI": "Revenue", "IR": "IncidentRate",
    "LOI": "LengthOfInterview", "supCmps": "Completes", "entryLink": "SurveyUrl",
    "isQuota": "Has_Quotas", "isPIIRequired": "CollectPii", "createdDate": "StartDate",
    "modifiedDate": "LastUpdatedOnUTC", "Language": "LanguageId",
}


def _path_value(payload: Any, path: str, default=None):
    value = payload
    for part in str(path or "").split("."):
        if not part:
            continue
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


class InnovateMRClient:
    """Configurable survey-provider client; class name is retained for API compatibility."""

    def __init__(self, token: str | None = None, session: requests.Session | None = None, integration=None):
        self.integration = integration
        if integration is not None:
            if token is None:
                from vendors.credentials import resolve_integration_token
                token = resolve_integration_token(integration)
            # Never borrow the global InnovateMR key for another client.
            self.token = token or ""
        else:
            self.token = token if token is not None else settings.INNOVATEMR_API_TOKEN
        self.base_url = (integration.base_url if integration is not None else settings.INNOVATEMR_BASE_URL).rstrip("/")
        self.provider_code = (getattr(integration, "provider_code", "innovatemr") or "innovatemr").lower()
        self.provider_key = self.provider_code.replace("-", "").replace("_", "")
        self.is_biobrain = self.provider_key in {"biobrain", "voqall"} or "voqall.com" in self.base_url.lower()
        self.timeout = settings.INNOVATEMR_TIMEOUT_SECONDS
        self.page_size = settings.INNOVATEMR_PAGE_SIZE
        self.max_pages = settings.INNOVATEMR_MAX_PAGES
        self.session = session or requests.Session()

    def _config(self, name: str, default=""):
        return getattr(self.integration, name, default) if self.integration is not None else default

    def _endpoint(self, name: str, innovate_default: str = "", biobrain_default: str = "") -> str:
        configured = self._config(name, "")
        if configured:
            return configured
        if self.is_biobrain:
            return biobrain_default
        if self.provider_key == "innovatemr":
            return innovate_default
        return ""

    def _url(self, endpoint: str) -> str:
        endpoint = str(endpoint or "").strip()
        if not endpoint:
            return self.base_url
        if urlparse(endpoint).scheme in {"http", "https"}:
            return endpoint
        return f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise InnovateMRAPIError(f"API token is not configured for {self.provider_code}")
        default_header = "EQ-PARTNER-ACCESS-KEY" if self.is_biobrain else "x-access-token"
        header_name = str(self._config("auth_header_name", "") or default_header).strip()
        prefix = str(self._config("auth_header_prefix", "") or "").strip()
        return {header_name: f"{prefix} {self.token}" if prefix else self.token, "Accept": "application/json"}

    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        url = self._url(endpoint)
        try:
            response = self.session.get(url, params=params, headers=self._headers(), timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                raise InnovateMRNotFound(f"{self.provider_code} returned no data for {url}") from exc
            raise InnovateMRAPIError(f"{self.provider_code} request failed for {url}: {exc}") from exc
        except (requests.RequestException, ValueError) as exc:
            raise InnovateMRAPIError(f"{self.provider_code} request failed for {url}: {exc}") from exc
        if not isinstance(payload, (dict, list)):
            raise InnovateMRAPIError(f"{self.provider_code} returned an invalid JSON payload")
        if isinstance(payload, dict) and self.provider_key == "innovatemr" and payload.get("apiStatus") not in {None, "success"}:
            raise InnovateMRAPIError(f"InnovateMR rejected the request: {payload.get('msg', 'Unexpected response')}")
        if isinstance(payload, dict) and self.is_biobrain and payload.get("hasError") is True:
            messages = payload.get("messages") or []
            raise InnovateMRAPIError(f"Bio Brain rejected the request: {'; '.join(str(item) for item in messages) or str(payload.get('error') or 'Unexpected response')}")
        return payload

    def _post(self, endpoint: str, body: dict[str, Any]) -> Any:
        url = self._url(endpoint)
        try:
            response = self.session.post(
                url, json=body, headers=self._headers(), timeout=self.timeout
            )
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                raise InnovateMRNotFound(f"{self.provider_code} returned no data for {url}") from exc
            raise InnovateMRAPIError(f"{self.provider_code} request failed for {url}: {exc}") from exc
        except (requests.RequestException, ValueError) as exc:
            raise InnovateMRAPIError(f"{self.provider_code} request failed for {url}: {exc}") from exc
        if not isinstance(payload, (dict, list)):
            raise InnovateMRAPIError(f"{self.provider_code} returned an invalid JSON payload")
        if isinstance(payload, dict) and self.provider_key == "innovatemr" and payload.get("apiStatus") not in {None, "success"}:
            raise InnovateMRAPIError(f"InnovateMR rejected the request: {payload.get('msg', 'Unexpected response')}")
        return payload

    def request_json(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        """Execute one server-configured read request for the admin API explorer.

        Authentication is still resolved internally. Callers receive only the
        provider JSON payload, never request headers or credential values.
        """
        return self._get(endpoint, params=params)

    def post_json(self, endpoint: str, body: dict[str, Any]) -> Any:
        """Execute one allow-listed, non-mutating provider check via POST."""
        return self._post(endpoint, body)

    def endpoint_url(self, endpoint: str) -> str:
        """Return the non-secret effective URL used for documentation metadata."""
        return self._url(endpoint)

    def _result_list(self, payload: Any, key: str) -> list[dict[str, Any]]:
        result = _path_value(payload, key, []) if isinstance(payload, dict) else payload
        if not isinstance(result, list):
            raise InnovateMRAPIError(f"{self.provider_code} response field '{key or '<root>'}' must be a list")
        return [item for item in result if isinstance(item, dict)]

    def _normalize_survey(self, item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        mapping = dict(BIOBRAIN_FIELD_MAP if self.is_biobrain else {})
        custom_mapping = self._config("field_mapping", {}) or {}
        if isinstance(custom_mapping, dict):
            mapping.update({str(key): str(value) for key, value in custom_mapping.items() if value})
        for canonical, upstream in mapping.items():
            value = _path_value(item, upstream)
            if value is not None:
                normalized[canonical] = value
        if self.is_biobrain:
            normalized.setdefault("CPI", item.get("Cpi")); normalized.setdefault("IR", item.get("Ir")); normalized.setdefault("LOI", item.get("Loi"))
            normalized["deviceType"] = ", ".join(name for name, field in (("desktop", "DesktopAllowed"), ("mobile", "MobileAllowed"), ("tablet", "TabletAllowed")) if item.get(field))
        normalized["_provider_name"] = getattr(getattr(self.integration, "client", None), "name", self.provider_code)
        return normalized

    def get_allocated_surveys(self) -> list[dict[str, Any]]:
        endpoint = self._endpoint("inventory_endpoint", "/supply/getAllocatedSurveys", "")
        key = str(self._config("inventory_result_key", "") or ("Surveys" if self.is_biobrain else "result"))
        return [self._normalize_survey(item) for item in self._result_list(self._get(endpoint), key)]

    def test_connection(self) -> dict[str, Any]:
        surveys = self.get_allocated_surveys()
        return {"ok": True, "provider": self.provider_code, "endpoint": self._url(self._endpoint("inventory_endpoint", "/supply/getAllocatedSurveys", "")), "records_visible": len(surveys)}

    def get_allocated_surveys_paged(self) -> PagedSurveyResult:
        endpoint = self._endpoint("paged_inventory_endpoint", "/supply/getAllocatedSurveysPaged", "")
        if not endpoint:
            return PagedSurveyResult(surveys=[], pages=0)
        surveys=[]; next_cursor=None; seen_cursors=set(); key=str(self._config("inventory_result_key", "") or "result")
        for page_number in range(1, self.max_pages + 1):
            params={"limit": self.page_size}
            if next_cursor: params["next"] = next_cursor
            payload=self._get(endpoint, params=params); surveys.extend(self._normalize_survey(item) for item in self._result_list(payload, key))
            paging=payload.get("paging") or {} if isinstance(payload, dict) else {}; candidate=paging.get("next") if isinstance(paging, dict) else None
            if not candidate or candidate in seen_cursors: return PagedSurveyResult(surveys=surveys, pages=page_number)
            seen_cursors.add(candidate); next_cursor=candidate
        raise InnovateMRAPIError(f"Pagination exceeded max pages ({self.max_pages})")

    def get_quota_for_survey(self, survey_id: int) -> list[dict[str, Any]]:
        endpoint=self._endpoint("quota_endpoint_template", "/supply/getQuotaForSurvey/{survey_id}", "")
        if not endpoint: return []
        key=str(self._config("quota_result_key", "") or ("Quotas" if self.is_biobrain else "result")); items=self._result_list(self._get(endpoint.format(survey_id=survey_id)), key)
        return [{**item, "id": item.get("QuotaId"), "targeting": {"Conditions": item.get("Conditions", [])}} for item in items] if self.is_biobrain else items

    def get_survey_targeting(self, survey_id: int) -> list[dict[str, Any]]:
        endpoint=self._endpoint("targeting_endpoint_template", "/supply/getSurveyTargeting/{survey_id}", "")
        if not endpoint: return []
        key=str(self._config("targeting_result_key", "") or ("Qualifications" if self.is_biobrain else "result")); items=self._result_list(self._get(endpoint.format(survey_id=survey_id)), key)
        return [{**item, "QuestionId": item.get("QualificationId"), "QuestionKey": str(item.get("QualificationId") or ""), "QuestionType": str(item.get("QualificationTypeId") or ""), "Options": item.get("OptionIds", [])} for item in items] if self.is_biobrain else items

    def get_survey_transactions_by_pid(self, survey_id: int, pid: str) -> list[dict[str, Any]]:
        endpoint=self._endpoint("transaction_endpoint_template", "/supply/getSurveyTransactionsByCond/{survey_id}/{pid}", "")
        if not endpoint: return []
        return self._result_list(self._get(endpoint.format(survey_id=survey_id, pid=pid)), str(self._config("transaction_result_key", "") or "result"))
