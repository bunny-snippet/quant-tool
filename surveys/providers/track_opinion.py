from collections import defaultdict
from urllib.parse import urlsplit

import requests
from django.core.cache import cache

from prescreener_vault.reuse import effective_profile_uid
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


class TrackOpinionProvider(SurveyProvider):
    code = "track_opinion"
    label = "Track Opinion"
    default_base_url = "https://stagingsupply.opinionest.com"
    minimum_sync_interval_seconds = 300
    credential_fields = (("token", "Access-token environment key"),)

    def __init__(self, integration, *, session=None):
        super().__init__(integration, session=session or requests.Session())
        refs = integration.credential_env_keys or {}
        token_reference = refs.get("token") or integration.credential_env_key
        self.token = environment_value(token_reference, "Track Opinion access token")
        self.base_url = (integration.base_url or self.default_base_url).rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "stagingsupply.opinionest.com", "supply.opinionest.com"
        } or parsed.query or parsed.fragment:
            raise ProviderConfigurationError("Track Opinion base URL must use its official HTTPS supply host.")
        self.timeout = max(5, min(integer((integration.config or {}).get("timeout_seconds"), 30), 60))

    def _request(self, path, *, method="GET", payload=None, allow_not_found=False):
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                headers={"access-token": self.token, "Accept": "application/json"},
                json=payload,
                timeout=self.timeout,
            )
            if allow_not_found and response.status_code == 404:
                return None
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status else ""
            raise ProviderError(f"Track Opinion request failed{suffix}.") from exc
        except ValueError as exc:
            raise ProviderError("Track Opinion returned invalid JSON.") from exc
        if not isinstance(data, (dict, list)):
            raise ProviderError("Track Opinion returned an invalid response payload.")
        if isinstance(data, dict):
            result = value(data, "result", default={}) or {}
            success = value(result, "Success", default=value(data, "success", default=True))
            if success is False:
                raise ProviderError(str(value(result, "Message", default=value(data, "message", default="Track Opinion rejected the request."))))
        return data

    @staticmethod
    def _list(data, *keys):
        if isinstance(data, list):
            return data
        found = value(data, *keys, "data", default=[])
        if isinstance(found, list):
            return found
        return [found] if isinstance(found, dict) else []

    def test_connection(self):
        rows = self.inventory()
        redirect_result = {"requested": False, "configured": False}
        if (self.integration.config or {}).get("configure_redirects"):
            redirect_result["requested"] = True
            try:
                redirect_result.update({
                    "configured": True,
                    "survey_count": self.configure_redirects(),
                })
            except ProviderError:
                # Authentication/inventory verification must remain usable
                # when Track Opinion's separate redirect-update endpoint has
                # a provider-side outage. Surface a non-secret warning so an
                # operator can retry without disabling inventory sync.
                redirect_result["warning"] = (
                    "Track Opinion authenticated, but its redirect-update endpoint is temporarily unavailable."
                )
        return {
            "provider": self.code,
            "authenticated": True,
            "inventory_count": len(rows),
            "redirects": redirect_result,
        }

    def inventory(self):
        return self._list(self._request("/api/v1/survey/surveys"), "surveys")

    def _countries(self):
        key = f"track-opinion:countries:{self.integration.pk}"
        countries = cache.get(key)
        if countries is None:
            rows = self._list(self._request("/api/v1/support/country-list"), "data")
            countries = {str(value(row, "countryId")): row for row in rows if isinstance(row, dict)}
            cache.set(key, countries, 86400)
        return countries

    def normalize_inventory_item(self, payload, seen_at):
        source = str(value(payload, "SurveyId") or "").strip()
        if not source:
            raise ProviderError("Track Opinion inventory row has no SurveyId.")
        country = self._countries().get(str(value(payload, "CountryId")), {})
        localization = str(value(country, "localizationCode") or "").upper()
        language_code, _, country_code = localization.partition("_")
        devices = []
        if value(payload, "IsDesktop"):
            devices.append("Desktop")
        if value(payload, "IsMobile"):
            devices.append("Mobile")
        if value(payload, "IsTablet"):
            devices.append("Tablet")
        modified = datetime_value(value(payload, "UpdateTimeStamp"))
        raw_data = {**payload, "_country": country}
        return NormalizedSurvey(
            source_key=source,
            numeric_source_id=integer(source, None),
            modified_at=modified,
            raw_data=raw_data,
            values={
                "company_name": self.integration.client.name,
                "name": str(value(payload, "SurveyName") or f"Track Opinion survey {source}"),
                "status": Survey.Status.LIVE,
                "sample_size": 0,
                "completes": 0,
                "remaining": 0,
                "cpi": decimal_value(value(payload, "Cpi")),
                "loi": max(0, integer(value(payload, "Loi"))),
                "incidence_rate": decimal_value(value(payload, "Ir")),
                "country": str(value(country, "countryName") or country_code),
                "country_code": country_code,
                "language": language_code,
                "language_code": language_code,
                "buyer_id": str(value(payload, "ClientId") or ""),
                "device_type": ", ".join(devices),
                "entry_link": str(value(payload, "LiveLink") or ""),
                "test_entry_link": str(value(payload, "TestLink") or ""),
                "has_quota": True,
                "is_pii_required": bool(value(payload, "CollectPII")),
                "source_modified_at": modified,
                "last_seen_at": seen_at,
                "raw_data": raw_data,
            },
        )

    def detail_signature(self, raw_data):
        return tuple(
            str(value(raw_data or {}, field) or "")
            for field in ("Qual_UpdateTimeStamp", "Quota_UpdateTimeStamp", "Group_UpdateTimeStamp")
        )

    def _question_metadata(self, country_id):
        key = f"track-opinion:questions:{self.integration.pk}:{country_id}"
        rows = cache.get(key)
        if rows is None:
            payload = self._request(f"/api/v1/support/question/{country_id}")
            rows = {
                str(value(row, "QuestionId")): row
                for row in self._list(payload, "data") if isinstance(row, dict)
            }
            cache.set(key, rows, 86400)
        return rows

    def _answer_labels(self, country_id, question_id):
        key = f"track-opinion:answers:{self.integration.pk}:{country_id}:{question_id}"
        labels = cache.get(key)
        if labels is None:
            payload = self._request(
                f"/api/v1/support/question-answers/country/{country_id}/question/{question_id}"
            )
            rows = self._list(payload, "data")
            answer_rows = []
            for row in rows:
                answer_rows.extend(value(row, "QuestionAnswers", default=[]) or [])
            labels = {
                str(value(row, "AnswerId")): str(value(row, "Description") or value(row, "AnswerId"))
                for row in answer_rows if isinstance(row, dict)
            }
            cache.set(key, labels, 86400)
        return labels

    def refresh_details(self, survey):
        survey_id = survey.source_key
        qualifications_payload = self._request(f"/api/v1/survey/survey-Qualifications/{survey_id}")
        quota_payload = self._request(
            f"/api/v1/survey/survey-quotas/{survey_id}", allow_not_found=True
        )
        remaining_payload = self._request(f"/api/v1/survey/general-reserved-remaining/{survey_id}")
        qualifications = self._list(qualifications_payload, "surveyQualifications")
        quota_rows = self._list(quota_payload, "surveyQuotas") if quota_payload else []
        country_id = str(value(survey.raw_data or {}, "CountryId") or "")
        metadata = self._question_metadata(country_id)

        allowed_by_question = defaultdict(list)
        for row in qualifications:
            question_id = str(value(row, "qualificationId") or "")
            for answer in split_values(value(row, "answerIds", default=[])):
                if answer not in allowed_by_question[question_id]:
                    allowed_by_question[question_id].append(answer)
        grouped_quotas = {}
        for row in quota_rows:
            quota_id = str(value(row, "quotaId") or "").strip()
            group = grouped_quotas.setdefault(quota_id, {
                "quotaId": quota_id,
                "quotaName": str(value(row, "quotaName") or ""),
                "totalRemaining": integer(value(row, "totalRemaining")),
                "criteria": [],
                "source_rows": [],
            })
            group["totalRemaining"] = min(group["totalRemaining"], integer(value(row, "totalRemaining")))
            group["source_rows"].append(row)
            for criterion in value(row, "criteria", default=[]) or []:
                if isinstance(criterion, dict):
                    group["criteria"].append(criterion)
                    question_id = str(value(criterion, "qualificationId") or "")
                    for answer in split_values(value(criterion, "answerIds", default=[])):
                        if answer not in allowed_by_question[question_id]:
                            allowed_by_question[question_id].append(answer)

        questions = []
        label_maps = {}
        for question_id, allowed in allowed_by_question.items():
            meta = metadata.get(question_id, {})
            labels = self._answer_labels(country_id, question_id)
            label_maps[question_id] = labels
            dimension_hint = (
                "age" if question_id == "10558"
                else "gender" if question_id == "10560"
                else ""
            )
            questions.append(question_row(
                provider_code=self.code,
                survey=survey,
                question_id=question_id,
                text=value(meta, "Description") or f"Provider qualification {question_id}",
                question_type=str(value(meta, "QuestionTypeId") or ""),
                allowed_values=allowed,
                option_labels=labels,
                category="Track Opinion targeting",
                raw_data={"provider_question": meta},
                dimension_hint=dimension_hint,
            ))

        quotas = []
        for position, group in enumerate(grouped_quotas.values(), start=1):
            details = []
            for criterion in group["criteria"]:
                question_id = str(value(criterion, "qualificationId") or "")
                meta = metadata.get(question_id, {})
                selected = split_values(value(criterion, "answerIds", default=[]))
                labels = label_maps.get(question_id) or self._answer_labels(country_id, question_id)
                details.append({
                    "name": str(value(meta, "Description") or "Provider qualification"),
                    "values": [labels.get(item, item) for item in selected],
                })
            remaining = max(0, integer(group["totalRemaining"]))
            quotas.append(SurveyQuota(
                survey=survey,
                source_key=group["quotaId"] or f"quota-{position}",
                quota_id=integer(group["quotaId"], None),
                title=f"Quota {position}",
                name=group["quotaName"] or "Targeted respondent quota",
                remaining=remaining,
                status="Open" if remaining else "Full",
                targeting={"criteria": group["criteria"]},
                raw_data={
                    **group,
                    "targeting_details": details,
                    "_target_known": False,
                    "_completed_known": False,
                    "quotaLimitBy": "completes",
                },
            ))
        total_remaining = max(
            0,
            integer(
                value(
                    remaining_payload,
                    "totalRemaining", "generalReservedRemaining", "remaining",
                    default=value(
                        value(
                            remaining_payload,
                            # The live API currently exposes this misspelling;
                            # retain the documented spelling as a fallback.
                            "totalRemainaing", "surveyRemaining",
                            default={},
                        ),
                        "TotalRemaining",
                        default=value(value(remaining_payload, "result", default={}), "totalRemaining"),
                    ),
                )
            ),
        )
        if not quotas:
            quotas.append(SurveyQuota(
                survey=survey,
                source_key="overall",
                title="Overall quota",
                name="Overall survey quota",
                remaining=total_remaining,
                status="Open" if total_remaining else "Full",
                raw_data={"targeting_details": [], "_target_known": False, "_completed_known": False},
            ))
        persist_details(
            survey,
            questions,
            quotas,
            survey_updates={"remaining": total_remaining, "status": Survey.Status.LIVE if total_remaining else Survey.Status.CLOSED},
        )

    def configure_redirects(self):
        base = str((self.integration.config or {}).get("public_callback_base") or "https://exchange.api-grid.com").rstrip("/")
        redirects = {
            "completeRedirect": f"{base}/survey?status=1&rid=[transid]",
            "overQuotaRedirect": f"{base}/survey?status=3&rid=[transid]",
            "terminateRedirect": f"{base}/survey?status=2&rid=[transid]",
            "securityTerminateRedirect": f"{base}/survey?status=4&rid=[transid]",
            "completeS2SRedirect": f"{base}/survey?status=1&rid=[transid]",
            "overQuotaS2SRedirect": f"{base}/survey?status=3&rid=[transid]",
            "terminateS2SRedirect": f"{base}/survey?status=2&rid=[transid]",
            "securityTerminateS2SRedirect": f"{base}/survey?status=4&rid=[transid]",
        }
        rows = self.inventory()
        configured = 0
        for row in rows:
            survey_id = integer(value(row, "SurveyId"), None)
            if survey_id is None:
                continue
            self._request(
                "/api/v1/survey/survey-redirects",
                method="PUT",
                payload={"surveyId": survey_id, **redirects},
            )
            configured += 1
        return configured

    def build_outbound_url(self, survey, attempt, answers):
        return replace_placeholders(survey.entry_link, {
            "[transid]": attempt.rid,
            "[User_id]": effective_profile_uid(attempt) or attempt.rid,
            "[uid]": effective_profile_uid(attempt) or attempt.rid,
        })
