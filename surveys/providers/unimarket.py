import gzip
import json
from pathlib import Path
from urllib.parse import urlsplit

import requests
from django.core.cache import cache

from prescreener_vault.reuse import effective_profile_uid
from surveys.models import Survey, SurveyAttempt, SurveyQuota

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


class UniMarketProvider(SurveyProvider):
    code = "unimarket"
    label = "UniMarket"
    default_base_url = "https://stg-api.supplier.unimrktresponse.net"
    minimum_sync_interval_seconds = 300
    credential_fields = (("token", "X-access-key environment key"),)
    supported_countries = ("US", "CA", "GB", "AU", "DE", "FR", "ES", "MX", "CN", "NL", "BR", "IT", "IN")
    _catalog = None

    def __init__(self, integration, *, session=None):
        super().__init__(integration, session=session or requests.Session())
        refs = integration.credential_env_keys or {}
        token_reference = refs.get("token") or integration.credential_env_key
        self.token = environment_value(token_reference, "UniMarket access key")
        self.base_url = (integration.base_url or self.default_base_url).rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or parsed.hostname != "stg-api.supplier.unimrktresponse.net" or parsed.query or parsed.fragment:
            raise ProviderConfigurationError("UniMarket base URL must use its official HTTPS supplier host.")
        self.timeout = max(5, min(integer((integration.config or {}).get("timeout_seconds"), 30), 60))

    def _request(self, path, *, allow_not_found=False):
        try:
            response = self.session.get(
                f"{self.base_url}{path}",
                headers={"x-access-key": self.token, "Accept": "application/json"},
                timeout=self.timeout,
            )
            if allow_not_found and response.status_code == 404:
                return None
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status else ""
            raise ProviderError(f"UniMarket request failed{suffix}.") from exc
        except ValueError as exc:
            raise ProviderError("UniMarket returned invalid JSON.") from exc
        if not isinstance(data, (dict, list)):
            raise ProviderError("UniMarket returned an invalid response payload.")
        if isinstance(data, dict):
            meta = value(data, "meta", default={}) or {}
            success = value(meta, "success", default=True)
            if success is False:
                raise ProviderError(str(value(meta, "message", default="UniMarket rejected the request.")))
        return data

    @staticmethod
    def _rows(payload, key):
        if isinstance(payload, list):
            return payload
        rows = value(payload, key, "data", default=[])
        return rows if isinstance(rows, list) else []

    @classmethod
    def _profile_catalog(cls):
        if cls._catalog is None:
            path = Path(__file__).resolve().parent.parent / "data" / "unimarket_profile_questions.json.gz"
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                cls._catalog = json.load(handle)
        return cls._catalog

    def test_connection(self):
        rows = self.inventory()
        return {"provider": self.code, "authenticated": True, "inventory_count": len(rows)}

    def inventory(self):
        country_codes = (self.integration.config or {}).get("country_codes") or self.supported_countries
        found = {}
        failures = []
        for country_code in country_codes:
            normalized_country = str(country_code).strip().upper()
            if normalized_country not in self.supported_countries:
                continue
            try:
                payload = self._request(f"/v1/surveys/{normalized_country}")
            except ProviderError as exc:
                failures.append(str(exc))
                continue
            for row in self._rows(payload, "surveys"):
                if not isinstance(row, dict) or value(row, "surveyId") in (None, ""):
                    continue
                source = str(value(row, "surveyId"))
                found[source] = {**row, "_country_code": normalized_country}
        if not found and failures:
            raise ProviderError("UniMarket country inventory calls failed.")
        return list(found.values())

    def normalize_inventory_item(self, payload, seen_at):
        source = str(value(payload, "surveyId") or "").strip()
        if not source:
            raise ProviderError("UniMarket inventory row has no surveyId.")
        country_code = str(value(payload, "_country_code") or "").upper()
        devices = []
        if value(payload, "deskTop"):
            devices.append("Desktop")
        if value(payload, "mobile"):
            devices.append("Mobile")
        if value(payload, "tablet"):
            devices.append("Tablet")
        modified = datetime_value(value(payload, "modifiedDateUTC"))
        remaining = max(0, integer(value(payload, "remaining")))
        return NormalizedSurvey(
            source_key=source,
            numeric_source_id=integer(source, None),
            modified_at=modified,
            raw_data=payload,
            values={
                "company_name": self.integration.client.name,
                "name": f"UniMarket survey {source}",
                "status": Survey.Status.LIVE if remaining else Survey.Status.CLOSED,
                "sample_size": remaining,
                "completes": 0,
                "remaining": remaining,
                "cpi": decimal_value(value(payload, "cpi")),
                "loi": max(0, integer(value(payload, "loi"))),
                "incidence_rate": decimal_value(value(payload, "ir")),
                "country": country_code,
                "country_code": country_code,
                "language": str(value(payload, "languageId") or ""),
                "language_code": "",
                "buyer_id": str(value(payload, "buyerId") or ""),
                "device_type": ", ".join(devices),
                "entry_link": str(value(payload, "entryLink") or ""),
                "has_quota": True,
                "is_pii_required": bool(value(payload, "isPII")),
                "group_type": "Survey group" if value(payload, "surveyGroup") else "",
                "source_modified_at": modified,
                "last_seen_at": seen_at,
                "raw_data": payload,
            },
        )

    def detail_signature(self, raw_data):
        return str(value(raw_data or {}, "modifiedDateUTC") or "")

    @staticmethod
    def _condition_options(condition):
        return split_values(value(condition, "options", default=[]))

    def refresh_details(self, survey):
        survey_id = survey.source_key
        questions_payload = self._request(f"/v1/surveys/{survey_id}/questions")
        quotas_payload = self._request(f"/v1/surveys/{survey_id}/quotas", allow_not_found=True)
        groups_payload = self._request(f"/v1/surveys/{survey_id}/groups", allow_not_found=True)
        stats_payload = self._request(f"/v1/surveys/{survey_id}/stats", allow_not_found=True)
        question_sources = self._rows(questions_payload, "questions")
        quota_sources = self._rows(quotas_payload, "quotas") if quotas_payload else []
        group_sources = self._rows(groups_payload, "groups") if groups_payload else []
        country_code = str((survey.raw_data or {}).get("_country_code") or survey.country_code).upper()
        catalog = self._profile_catalog().get(country_code, {})

        allowed_by_question = {}
        type_by_question = {}
        for source in question_sources:
            question_id = str(value(source, "questionId") or "")
            allowed_by_question.setdefault(question_id, [])
            type_by_question[question_id] = str(value(source, "typeId") or "")
            for selected in self._condition_options(source):
                if selected not in allowed_by_question[question_id]:
                    allowed_by_question[question_id].append(selected)
        for quota in quota_sources:
            for condition in value(quota, "conditions", default=[]) or []:
                if not isinstance(condition, dict):
                    continue
                question_id = str(value(condition, "questionId") or "")
                allowed_by_question.setdefault(question_id, [])
                type_by_question[question_id] = str(value(condition, "typeId") or type_by_question.get(question_id, ""))
                for selected in self._condition_options(condition):
                    if selected not in allowed_by_question[question_id]:
                        allowed_by_question[question_id].append(selected)

        questions = []
        for question_id, allowed in allowed_by_question.items():
            meta = catalog.get(question_id, {})
            metadata_label = f"{meta.get('title', '')} {meta.get('text', '')}".casefold()
            dimension_hint = (
                "postal" if any(term in metadata_label for term in ("zip", "postal", "post code"))
                else "gender" if "gender" in metadata_label
                else "age" if "age" in metadata_label
                else ""
            )
            questions.append(question_row(
                provider_code=self.code,
                survey=survey,
                question_id=question_id,
                text=meta.get("text") or meta.get("title") or f"Provider qualification {question_id}",
                question_type=meta.get("type") or type_by_question.get(question_id),
                allowed_values=allowed,
                option_labels=meta.get("options") or {},
                category="UniMarket targeting",
                raw_data={"provider_question": meta},
                dimension_hint=dimension_hint,
            ))

        quotas = []
        for position, quota in enumerate(quota_sources, start=1):
            details = []
            conditions = value(quota, "conditions", default=[]) or []
            for condition in conditions:
                question_id = str(value(condition, "questionId") or "")
                meta = catalog.get(question_id, {})
                labels = meta.get("options") or {}
                selected = self._condition_options(condition)
                details.append({
                    "name": meta.get("text") or meta.get("title") or "Provider qualification",
                    "values": [labels.get(item, item) for item in selected],
                })
            remaining = max(0, integer(value(quota, "remaining")))
            quota_id = str(value(quota, "quotaId") or f"quota-{position}")
            quotas.append(SurveyQuota(
                survey=survey,
                source_key=quota_id,
                quota_id=integer(quota_id, None),
                title=f"Quota {position}",
                name="Targeted respondent quota" if conditions else "Overall survey quota",
                remaining=remaining,
                status="Open" if remaining else "Full",
                targeting={"conditions": conditions},
                raw_data={
                    **quota,
                    "targeting_details": details,
                    "provider_groups": group_sources,
                    "_target_known": False,
                    "_completed_known": False,
                    "quotaLimitBy": "completes",
                },
            ))
        if not quotas:
            quotas.append(SurveyQuota(
                survey=survey,
                source_key="overall",
                title="Overall quota",
                name="Overall survey quota",
                remaining=survey.remaining,
                status="Open" if survey.remaining else "Full",
                raw_data={"targeting_details": [], "provider_groups": group_sources, "_target_known": False, "_completed_known": False},
            ))

        supplier_stats = value(stats_payload or {}, "supplierStats", default={}) or {}
        raw_data = dict(survey.raw_data or {})
        raw_data["_unimarket_groups"] = group_sources
        persist_details(survey, questions, quotas, survey_updates={
            "starts": max(0, integer(value(supplier_stats, "starts"), survey.starts)),
            "completes": max(0, integer(value(supplier_stats, "completes"), survey.completes)),
            "raw_data": raw_data,
        })

    def duplicate_check(self, survey, attempt, ip_address):
        group_rows = []
        for quota in survey.quotas.all():
            group_rows = (quota.raw_data or {}).get("provider_groups") or []
            if group_rows:
                break
        group_surveys = {
            str(item)
            for group in group_rows
            for item in (value(group, "groupSurveys", default=[]) or [])
        }
        if not group_surveys:
            return False
        uid = effective_profile_uid(attempt)
        if not uid:
            return False
        return SurveyAttempt.objects.filter(
            survey__integration=survey.integration,
            survey__source_key__in=group_surveys,
        ).exclude(pk=attempt.pk).filter(
            provider_profile_uid=uid,
            status__in={"redirected", "1", "2", "3", "4"},
        ).exists()

    def build_outbound_url(self, survey, attempt, answers):
        profile_uid = effective_profile_uid(attempt) or attempt.rid
        return replace_placeholders(survey.entry_link, {
            "{umid}": profile_uid,
            "{uid}": attempt.rid,
        })
