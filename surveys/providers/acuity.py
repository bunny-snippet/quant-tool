from collections import defaultdict
from urllib.parse import urlsplit

import requests
from django.core.cache import cache

from surveys.models import Survey, SurveyQuota

from .base import (
    NormalizedSurvey,
    ProviderConfigurationError,
    ProviderError,
    SurveyProvider,
    environment_value,
)
from .supply_common import (
    datetime_value,
    decimal_value,
    integer,
    persist_details,
    question_row,
    replace_placeholders,
    split_values,
    value,
)


class AcuityAnalyticsProvider(SurveyProvider):
    code = "acuity"
    label = "Acuity Analytics"
    default_base_url = "https://api.acuitykp.online"
    minimum_sync_interval_seconds = 300
    credential_fields = (
        ("supplier_id", "Supplier ID environment key"),
        ("token", "Token environment key"),
    )

    def __init__(self, integration, *, session=None):
        super().__init__(integration, session=session or requests.Session())
        refs = integration.credential_env_keys or {}
        self.supplier_id = environment_value(refs.get("supplier_id"), "Acuity Supplier ID")
        self.token = environment_value(refs.get("token"), "Acuity token")
        self.base_url = (integration.base_url or self.default_base_url).rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or parsed.hostname != "api.acuitykp.online" or parsed.query or parsed.fragment:
            raise ProviderConfigurationError("Acuity base URL must use its official HTTPS API host.")
        self.timeout = max(5, min(integer((integration.config or {}).get("timeout_seconds"), 45), 90))
        self._question_catalogs = {}

    def _request(self, path, *, supplier=False, payload=None):
        headers = {"token": self.token, "Accept": "application/json"}
        if supplier:
            headers["Supplierid"] = self.supplier_id
        try:
            response = self.session.get(
                f"{self.base_url}{path}",
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status else ""
            raise ProviderError(f"Acuity request failed{suffix}.") from exc
        except ValueError as exc:
            raise ProviderError("Acuity returned invalid JSON.") from exc
        if not isinstance(data, (dict, list)):
            raise ProviderError("Acuity returned an invalid response payload.")
        if isinstance(data, dict):
            response_code = value(data, "ResponseCode", "response_code")
            if response_code not in (None, "", 1, "1", 200, "200"):
                raise ProviderError(str(value(data, "ResponseMessage", "message", default="Acuity rejected the request.")))
        return data

    @staticmethod
    def _rows(payload):
        if isinstance(payload, list):
            return payload
        rows = value(payload, "Data", "data", "result", default=[])
        return rows if isinstance(rows, list) else []

    def test_connection(self):
        rows = self.inventory()
        return {"provider": self.code, "authenticated": True, "inventory_count": len(rows)}

    def inventory(self):
        return self._rows(self._request("/webservices/survey/send_supplier_data", supplier=True))

    def _master_rows(self, name):
        key = f"acuity:master:{self.integration.pk}:{name}"
        rows = cache.get(key)
        if rows is None:
            rows = self._rows(self._request(f"/webservices/survey/get_master_data/{name}"))
            cache.set(key, rows, 86400)
        return rows

    def _country(self, name):
        wanted = str(name or "").strip().casefold()
        return next((row for row in self._master_rows("country") if str(value(row, "name") or "").strip().casefold() == wanted), {})

    def _language(self, name):
        wanted = str(name or "").strip().casefold()
        return next((row for row in self._master_rows("language") if str(value(row, "name") or "").strip().casefold() == wanted), {})

    def normalize_inventory_item(self, payload, seen_at):
        source = str(value(payload, "project_id") or "").strip()
        if not source:
            raise ProviderError("Acuity inventory row has no project_id.")
        country = self._country(value(payload, "country"))
        language = self._language(value(payload, "language"))
        country_code = str(value(country, "short_name") or value(payload, "country") or "").upper()
        language_code = str(value(language, "short_name") or value(payload, "language") or "").upper()
        target = max(0, integer(value(payload, "quota")))
        completed = max(0, integer(value(payload, "live_quota")))
        modified = datetime_value(value(payload, "project_start_date"))
        audience = str(value(payload, "audience_type") or "").upper()
        return NormalizedSurvey(
            source_key=source,
            numeric_source_id=integer(source, None),
            modified_at=modified,
            raw_data=payload,
            values={
                "company_name": self.integration.client.name,
                "name": str(value(payload, "project_name") or value(payload, "project_code") or f"Acuity survey {source}"),
                "status": Survey.Status.LIVE,
                "sample_size": target,
                "completes": min(completed, target) if target else completed,
                "remaining": max(0, target - completed) if target else 0,
                "cpi": decimal_value(value(payload, "cpi")),
                "loi": max(0, integer(value(payload, "project_loi"))),
                "incidence_rate": decimal_value(value(payload, "project_ir")),
                "country": str(value(payload, "country") or country_code),
                "country_code": country_code,
                "language": str(value(payload, "language") or language_code),
                "language_code": language_code,
                "buyer_id": str(value(payload, "project_code") or ""),
                "survey_type": audience if audience in {"B2B", "B2C"} else "",
                "device_type": str(value(payload, "device_type") or ""),
                "entry_link": str(value(payload, "live_url") or ""),
                "test_entry_link": str(value(payload, "test_url") or ""),
                "job_category": str(value(payload, "category") or ""),
                "has_quota": True,
                "is_pii_required": bool(integer(value(payload, "pii_collection"))),
                "source_created_at": modified,
                "source_modified_at": modified,
                "last_seen_at": seen_at,
                "raw_data": payload,
            },
        )

    def detail_signature(self, raw_data):
        return value(raw_data or {}, "qualifications", default=[])

    def _question_catalog(self, survey):
        country = self._country(value(survey.raw_data or {}, "country"))
        language = self._language(value(survey.raw_data or {}, "language"))
        country_id = integer(value(country, "country_id", "id"), None)
        language_id = integer(value(language, "language_id", "id"), None)
        if country_id is None or language_id is None:
            raise ProviderError("Acuity country/language mapping is unavailable for this survey.")
        cache_key = (country_id, language_id)
        if cache_key not in self._question_catalogs:
            shared_key = f"acuity:questions:{self.integration.pk}:{country_id}:{language_id}"
            rows = cache.get(shared_key)
            if rows is None:
                rows = self._rows(self._request(
                    "/webservices/survey/get_master_data/question",
                    payload={"country_id": country_id, "language_id": language_id},
                ))
                cache.set(shared_key, rows, 86400)
            self._question_catalogs[cache_key] = {
                str(value(row, "question_id", "id")): row for row in rows if isinstance(row, dict)
            }
        return self._question_catalogs[cache_key]

    @staticmethod
    def _mapped_options(meta):
        result = {}
        mapped = value(meta, "mapped_option", default={}) or {}
        if isinstance(mapped, dict):
            for raw_id, label in mapped.items():
                option_id = str(raw_id).rsplit("-", 1)[-1]
                result[option_id] = str(label)
        elif isinstance(mapped, list):
            for row in mapped:
                if isinstance(row, dict):
                    option_id = str(value(row, "id", "option_id") or "")
                    if option_id:
                        result[option_id] = str(value(row, "name", "label", default=option_id))
        return result

    @staticmethod
    def _qualification_values(qualification):
        selected = []
        for field in ("option", "range"):
            for item in value(qualification, field, default=[]) or []:
                raw = value(item, field, "value", default=item) if isinstance(item, dict) else item
                for candidate in split_values(raw):
                    if candidate not in selected:
                        selected.append(candidate)
        return selected

    def refresh_details(self, survey):
        qualifications = value(survey.raw_data or {}, "qualifications", default=[]) or []
        catalog = self._question_catalog(survey)
        questions = []
        details = []
        for qualification in qualifications:
            if not isinstance(qualification, dict):
                continue
            question_id = str(value(qualification, "question_id") or "")
            meta = catalog.get(question_id, {})
            allowed = self._qualification_values(qualification)
            labels = self._mapped_options(meta)
            text = value(meta, "question", "title") or f"Provider qualification {question_id}"
            title = str(value(meta, "title") or "")
            dimension_hint = "age" if value(qualification, "range", default=[]) else (
                "postal" if any(term in title.casefold() for term in ("zip", "postal", "post code"))
                else "gender" if "gender" in title.casefold()
                else "age" if "age" in title.casefold()
                else ""
            )
            questions.append(question_row(
                provider_code=self.code,
                survey=survey,
                question_id=question_id,
                text=text,
                question_type=str(value(meta, "control_type") or value(qualification, "control_type") or ""),
                allowed_values=allowed,
                option_labels=labels,
                category="Acuity Analytics targeting",
                raw_data={"provider_question": meta},
                dimension_hint=dimension_hint,
            ))
            details.append({
                "name": str(text),
                "values": [labels.get(item, item) for item in allowed],
            })

        target = max(0, integer(value(survey.raw_data or {}, "quota"), survey.sample_size))
        completed = max(0, integer(value(survey.raw_data or {}, "live_quota"), survey.completes))
        remaining = max(0, target - completed) if target else survey.remaining
        quota = SurveyQuota(
            survey=survey,
            source_key="overall-targeted",
            title="Survey quota",
            name="Targeted respondent quota" if details else "Overall survey quota",
            sample_size=target,
            completes=min(completed, target) if target else completed,
            remaining=remaining,
            status="Open" if remaining else "Full",
            targeting={"qualifications": qualifications},
            raw_data={
                "targeting_details": details,
                "_target_known": bool(target),
                "_completed_known": True,
                "quotaLimitBy": "completes",
            },
        )
        persist_details(survey, questions, [quota], survey_updates={
            "sample_size": target,
            "completes": min(completed, target) if target else completed,
            "remaining": remaining,
        })

    def build_outbound_url(self, survey, attempt, answers):
        return replace_placeholders(survey.entry_link, {"[identifier]": attempt.rid})
