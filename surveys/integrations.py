import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit

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

BIOBRAIN_DETAIL_ADAPTER_VERSION = 3

BIOBRAIN_STANDARD_QUALIFICATIONS = {
    "GENDER": {
        "question": "What is your gender?",
        "options": {"1": "Male", "2": "Female", "3": "Other"},
    },
    "AGE": {"question": "What is your age?", "options": {}},
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
        self._biobrain_language_cache: dict[str, dict[str, Any]] | None = None
        self._biobrain_qualification_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._biobrain_qualification_catalog_cache: dict[str, dict[str, Any]] | None = None
        self._biobrain_survey_language_cache: dict[str, Any] = {}

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

    def _biobrain_url(self, endpoint: str) -> str:
        """Resolve BioBrain collection URLs beside the configured `/surveys` URL."""
        endpoint = str(endpoint or "").strip()
        if urlparse(endpoint).scheme in {"http", "https"}:
            return endpoint
        parsed = urlsplit(self.base_url)
        path = parsed.path.rstrip("/")
        if path.lower().endswith("/surveys"):
            path = path[:-len("/surveys")]
        return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/{endpoint.lstrip('/')}", "", ""))

    def _biobrain_collection_urls(self, endpoint: str) -> list[str]:
        """Return current and API-v2 collection URLs without changing inventory."""
        primary = self._biobrain_url(endpoint)
        parsed = urlsplit(primary)
        hosts = [parsed.netloc]
        if "partner-api2." not in parsed.netloc and "partner-api." in parsed.netloc:
            hosts.append(parsed.netloc.replace("partner-api.", "partner-api2.", 1))
        return [
            urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
            for host in dict.fromkeys(hosts)
        ]

    def _biobrain_languages(self) -> dict[str, dict[str, Any]]:
        if self._biobrain_language_cache is None:
            payload = self._get(self._biobrain_url("collection/languages"))
            rows = self._result_list(payload, "Languages")
            self._biobrain_language_cache = {
                str(row.get("Id")): row for row in rows if row.get("Id") is not None
            }
        return self._biobrain_language_cache

    @staticmethod
    def _biobrain_value(payload: Any, *names: str, default=None):
        if not isinstance(payload, dict):
            return default
        lowered = {str(key).lower(): value for key, value in payload.items()}
        for name in names:
            if name in payload:
                return payload[name]
            value = lowered.get(str(name).lower())
            if value is not None:
                return value
        return default

    @classmethod
    def _biobrain_qualification_payload(cls, payload: Any, qualification_id) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        candidates: list[Any] = []
        direct = cls._biobrain_value(payload, "Qualification")
        if direct is not None:
            candidates.append(direct)
        data = cls._biobrain_value(payload, "data", "result")
        if isinstance(data, dict):
            candidates.append(cls._biobrain_value(data, "Qualification", default=data))
        rows = cls._biobrain_value(payload, "Qualifications")
        if isinstance(rows, list):
            candidates.extend(rows)
        if cls._biobrain_value(payload, "Id", "QualificationId") is not None:
            candidates.append(payload)
        expected_id = str(qualification_id or "")
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            candidate_id = cls._biobrain_value(candidate, "Id", "QualificationId")
            if expected_id and candidate_id not in (None, "") and str(candidate_id) != expected_id:
                continue
            return candidate
        return {}

    def _biobrain_qualification_catalog(self) -> dict[str, dict[str, Any]]:
        if self._biobrain_qualification_catalog_cache is None:
            try:
                payload = self._get(self._biobrain_url("collection/qualifications"))
                rows = self._result_list(payload, "Qualifications")
            except InnovateMRAPIError:
                logger.warning("BioBrain qualification catalog could not be loaded", exc_info=True)
                rows = []
            self._biobrain_qualification_catalog_cache = {
                str(self._biobrain_value(row, "Id", "QualificationId")): row
                for row in rows
                if self._biobrain_value(row, "Id", "QualificationId") is not None
            }
        return self._biobrain_qualification_catalog_cache

    def _biobrain_language_id(self, survey_id, language_id=None, response_payload=None):
        candidate = language_id or self._biobrain_value(
            response_payload, "LanguageId", "LanguageID", "language_id"
        )
        if candidate not in (None, ""):
            self._biobrain_survey_language_cache[str(survey_id)] = candidate
            return candidate
        cached = self._biobrain_survey_language_cache.get(str(survey_id))
        if cached not in (None, ""):
            return cached
        try:
            endpoint = self._endpoint("inventory_endpoint", "", "")
            key = str(self._config("inventory_result_key", "") or "Surveys")
            rows = self._result_list(self._get(endpoint), key)
        except InnovateMRAPIError:
            logger.warning("BioBrain survey language lookup failed survey=%s", survey_id, exc_info=True)
            return None
        for row in rows:
            row_id = self._biobrain_value(row, "SurveyId", "surveyId", "Id")
            row_language = self._biobrain_value(row, "LanguageId", "LanguageID", "language_id")
            if row_id is not None and row_language not in (None, ""):
                self._biobrain_survey_language_cache[str(row_id)] = row_language
        return self._biobrain_survey_language_cache.get(str(survey_id))

    def _biobrain_qualification(self, language_id, qualification_id) -> dict[str, Any]:
        cache_key = (str(language_id or ""), str(qualification_id or ""))
        if not cache_key[1]:
            return {}
        if cache_key not in self._biobrain_qualification_cache:
            detail: dict[str, Any] = {}
            if cache_key[0]:
                endpoint = f"collection/languages/{cache_key[0]}/qualifications/{cache_key[1]}"
                for detail_url in self._biobrain_collection_urls(endpoint):
                    try:
                        payload = self._get(detail_url)
                        candidate = self._biobrain_qualification_payload(payload, qualification_id)
                        if candidate:
                            detail = candidate
                        if self._biobrain_value(detail, "QuestionText", "Question", "Text") and self._biobrain_value(
                            detail, "Options", "Answers", "QualificationOptions"
                        ):
                            break
                    except InnovateMRAPIError:
                        logger.warning(
                            "BioBrain localized qualification lookup failed url=%s language=%s qualification=%s",
                            detail_url, cache_key[0], cache_key[1], exc_info=True,
                        )
            needs_generic = not self._biobrain_value(detail, "Code", "TypeName")
            generic = self._biobrain_qualification_catalog().get(cache_key[1], {}) if needs_generic else {}
            self._biobrain_qualification_cache[cache_key] = {**generic, **detail}
        return self._biobrain_qualification_cache[cache_key]

    @staticmethod
    def _biobrain_options(item: dict[str, Any], detail: dict[str, Any]) -> list[dict[str, Any]]:
        option_ids = InnovateMRClient._biobrain_value(item, "OptionIds", default=[])
        option_codes = InnovateMRClient._biobrain_value(item, "OptionCodes", default=[])
        option_ids = option_ids if isinstance(option_ids, list) else []
        option_codes = option_codes if isinstance(option_codes, list) else []
        detail_options = InnovateMRClient._biobrain_value(
            detail, "Options", "Answers", "QualificationOptions", default=[]
        )
        labels: dict[str, str] = {}
        if isinstance(detail_options, list):
            for option in detail_options:
                if not isinstance(option, dict):
                    continue
                option_code = InnovateMRClient._biobrain_value(option, "OptionCode", "Code", "Value")
                option_id = InnovateMRClient._biobrain_value(option, "OptionId", "Id")
                option_text = InnovateMRClient._biobrain_value(
                    option, "OptionText", "Text", "Label", "Name", default=option_code or option_id
                )
                for lookup in (option_code, option_id):
                    if lookup not in (None, ""):
                        labels[str(lookup)] = str(option_text)
        qualification_code = str(
            InnovateMRClient._biobrain_value(detail, "Code", "Key", default="") or ""
        ).strip().upper()
        standard_labels = BIOBRAIN_STANDARD_QUALIFICATIONS.get(qualification_code, {}).get("options", {})
        options = []
        for index, option_id in enumerate(option_ids):
            option_code = option_codes[index] if index < len(option_codes) else option_id
            option_text = labels.get(str(option_code)) or labels.get(str(option_id))
            if not option_text or option_text.strip() in {str(option_code), str(option_id)}:
                option_text = standard_labels.get(str(option_code), str(option_code))
            options.append({
                "OptionId": option_id,
                "OptionCode": option_code,
                "OptionText": option_text,
                "Qualifies": True,
            })
        return options

    def _normalize_biobrain_qualification(self, item, language_id) -> dict[str, Any]:
        qualification_id = self._biobrain_value(item, "QualificationId", "Id")
        detail = self._biobrain_qualification(language_id, qualification_id)
        qualification_code = str(self._biobrain_value(detail, "Code", "Key", default="") or "").strip()
        standard = BIOBRAIN_STANDARD_QUALIFICATIONS.get(qualification_code.upper(), {})
        localized_question = str(
            self._biobrain_value(
                detail, "QuestionText", "Question", "Text", "Label", "Name", default=""
            )
            or ""
        )
        question_text = localized_question or standard.get("question") or qualification_code or f"Qualification {qualification_id}"
        question_type = str(
            self._biobrain_value(detail, "TypeName", "QuestionType", default="")
            or self._biobrain_value(item, "QualificationTypeId", default="")
        )
        options = self._biobrain_options(item, detail)
        readable_options = all(
            str(option.get("OptionText") or "").strip()
            and str(option.get("OptionText")).strip() not in {
                str(option.get("OptionCode")), str(option.get("OptionId"))
            }
            for option in options
        )
        metadata_hydrated = bool(
            (localized_question or standard.get("question"))
            and (not options or readable_options)
        )
        return {
            **item,
            "QuestionId": qualification_id,
            "QuestionKey": qualification_code or f"BIOBRAIN_Q_{qualification_id}",
            "QuestionText": question_text,
            "QuestionType": question_type,
            "QuestionCategory": "BioBrain targeting",
            "Options": options,
            "targeting_choices": [str(option["OptionId"]) for option in options],
            "qualification_code": qualification_code,
            "adapter_version": BIOBRAIN_DETAIL_ADAPTER_VERSION,
            "metadata_hydrated": metadata_hydrated,
        }

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

    def write_json(self, method: str, endpoint: str, body: dict[str, Any] | None = None) -> Any:
        """Execute an explicitly confirmed provider configuration/profile mutation.

        This method is intentionally separate from the inventory helpers so a
        caller cannot turn an arbitrary Swagger request into an upstream write.
        The explorer allow-list and confirmation gate are enforced before this
        method is reached.
        """
        method = str(method or "").upper()
        if method not in {"POST", "PUT", "DELETE"}:
            raise InnovateMRAPIError("Unsupported upstream write method")
        url = self._url(endpoint)
        try:
            response = self.session.request(
                method,
                url,
                json=body or {},
                headers=self._headers(),
                timeout=self.timeout,
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
        if (
            isinstance(payload, dict)
            and self.provider_key == "innovatemr"
            and payload.get("apiStatus") not in {None, "success"}
        ):
            raise InnovateMRAPIError(
                f"InnovateMR rejected the request: {payload.get('msg', 'Unexpected response')}"
            )
        return payload

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
            normalized["N"] = max(0, int(item.get("Completes") or 0))
            normalized["supCmps"] = max(0, int(item.get("SupplierCompletes") or item.get("Completed") or 0))
            normalized["remainingN"] = max(0, normalized["N"] - normalized["supCmps"])
            language_id = item.get("LanguageId")
            if language_id not in (None, "") and item.get("SurveyId") is not None:
                self._biobrain_survey_language_cache[str(item.get("SurveyId"))] = language_id
            language = self._biobrain_languages().get(str(language_id), {}) if language_id is not None else {}
            country_code = str(language.get("CountryCode") or "").upper()
            normalized["Country"] = country_code
            normalized["CountryCode"] = country_code
            normalized["Language"] = str(language.get("Name") or "")
            normalized["LanguageCode"] = str(language.get("Name") or "").upper()[:8]
            normalized["deviceType"] = ", ".join(name for name, field in (("desktop", "DesktopAllowed"), ("mobile", "MobileAllowed"), ("tablet", "TabletAllowed")) if item.get(field))
            normalized["_biobrain_detail_adapter_version"] = BIOBRAIN_DETAIL_ADAPTER_VERSION
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

    def get_quota_for_survey(self, survey_id: int, *, language_id=None) -> list[dict[str, Any]]:
        endpoint=self._endpoint("quota_endpoint_template", "/supply/getQuotaForSurvey/{survey_id}", "")
        if not endpoint: return []
        key=str(self._config("quota_result_key", "") or ("Quotas" if self.is_biobrain else "result")); payload=self._get(endpoint.format(survey_id=survey_id)); items=self._result_list(payload, key)
        if not self.is_biobrain:
            return items
        if any(isinstance(item.get("Conditions"), list) and item.get("Conditions") for item in items):
            language_id = self._biobrain_language_id(survey_id, language_id, payload)
        normalized = []
        for item in items:
            targeting_details = []
            metadata_hydrated = True
            for condition in item.get("Conditions", []) if isinstance(item.get("Conditions"), list) else []:
                if not isinstance(condition, dict):
                    continue
                question = self._normalize_biobrain_qualification(condition, language_id)
                metadata_hydrated = metadata_hydrated and question["metadata_hydrated"]
                targeting_details.append({
                    "name": question["QuestionText"],
                    "values": [option["OptionText"] for option in question["Options"]] or ["Provider-defined segment"],
                })
            normalized.append({
                **item,
                "id": item.get("QuotaId"),
                "title": "Targeted respondent quota" if targeting_details else "Overall survey quota",
                "targeting": {"Conditions": item.get("Conditions", [])},
                "targeting_details": targeting_details,
                "metadata_hydrated": metadata_hydrated,
            })
        return normalized

    def get_survey_targeting(self, survey_id: int, *, language_id=None) -> list[dict[str, Any]]:
        endpoint=self._endpoint("targeting_endpoint_template", "/supply/getSurveyTargeting/{survey_id}", "")
        if not endpoint: return []
        key=str(self._config("targeting_result_key", "") or ("Qualifications" if self.is_biobrain else "result")); payload=self._get(endpoint.format(survey_id=survey_id)); items=self._result_list(payload, key)
        if self.is_biobrain:
            language_id = self._biobrain_language_id(survey_id, language_id, payload)
        return [self._normalize_biobrain_qualification(item, language_id) for item in items] if self.is_biobrain else items

    def get_survey_transactions_by_pid(self, survey_id: int, pid: str) -> list[dict[str, Any]]:
        endpoint=self._endpoint("transaction_endpoint_template", "/supply/getSurveyTransactionsByCond/{survey_id}/{pid}", "")
        if not endpoint: return []
        return self._result_list(self._get(endpoint.format(survey_id=survey_id, pid=pid)), str(self._config("transaction_result_key", "") or "result"))
