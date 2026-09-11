import hashlib
import hmac
from urllib.parse import urlencode, urlsplit

import requests
from django.core.cache import cache
from django.utils import timezone

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
    split_values,
    value,
)


class ZampliaProvider(SurveyProvider):
    code = "zamplia"
    label = "Zamplia"
    default_base_url = "https://surveysupply.zamplia.com/api/v1"
    minimum_sync_interval_seconds = 300
    credential_fields = (
        ("client_id", "Client ID environment key"),
        ("token", "ZAMP-KEY environment key"),
        ("hmac_key", "Optional HMAC secret environment key"),
    )

    def __init__(self, integration, *, session=None):
        super().__init__(integration, session=session or requests.Session())
        refs = integration.credential_env_keys or {}
        self.client_id = environment_value(
            refs.get("client_id"), "Zamplia client ID"
        )
        self.token = environment_value(
            refs.get("token") or integration.credential_env_key,
            "Zamplia ZAMP-KEY",
        )
        self.hmac_reference = str(refs.get("hmac_key") or "").strip()
        self.base_url = (integration.base_url or self.default_base_url).rstrip("/")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {
                "surveysupply.zamplia.com",
                "surveysupplysandbox.zamplia.com",
            }
            or parsed.path.rstrip("/") != "/api/v1"
            or parsed.query
            or parsed.fragment
        ):
            raise ProviderConfigurationError(
                "Zamplia base URL must use its official HTTPS Supply API host and /api/v1 path."
            )
        self.timeout = max(
            5,
            min(
                integer((integration.config or {}).get("timeout_seconds"), 30),
                60,
            ),
        )

    def _request(self, path, *, params=None, allow_not_found=False):
        try:
            response = self.session.get(
                f"{self.base_url}{path}",
                params=params,
                headers={"ZAMP-KEY": self.token, "Accept": "application/json"},
                timeout=self.timeout,
            )
            if allow_not_found and response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status else ""
            raise ProviderError(f"Zamplia request failed{suffix}.") from exc
        except ValueError as exc:
            raise ProviderError("Zamplia returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise ProviderError("Zamplia returned an invalid response payload.")
        if value(payload, "success", default=True) is False:
            raise ProviderError(
                str(value(payload, "message", default="Zamplia rejected the request."))
            )
        result = value(payload, "result", default={}) or {}
        if not isinstance(result, dict):
            raise ProviderError("Zamplia response result must be an object.")
        if value(result, "success", default=True) is False:
            message = str(
                value(result, "message", default="Zamplia rejected the request.")
            )
            code = str(value(result, "Code", "code", default="")).strip()
            raise ProviderError(f"{message}{f' ({code})' if code else ''}")
        return result

    @staticmethod
    def _rows(result):
        rows = value(result or {}, "data", default=[])
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        return [rows] if isinstance(rows, dict) else []

    def test_connection(self):
        rows = self.inventory()
        return {
            "provider": self.code,
            "authenticated": True,
            "environment": (
                "sandbox" if "sandbox" in self.base_url else "production"
            ),
            "inventory_count": len(rows),
        }

    def inventory(self):
        return self._rows(self._request("/Surveys/GetAllocatedSurveys"))

    def _languages(self):
        key = f"zamplia:languages:{self.integration.pk}"
        rows = cache.get(key)
        if rows is None:
            rows = self._rows(self._request("/Attributes/GetLanguages"))
            cache.set(key, rows, 86400)
        by_id = {
            str(value(row, "LanguageId")): row
            for row in rows
            if value(row, "LanguageId") not in (None, "")
        }
        by_code = {
            str(value(row, "LanguageCode") or "").casefold(): row
            for row in rows
            if value(row, "LanguageCode")
        }
        return by_id, by_code

    def _language(self, payload):
        by_id, by_code = self._languages()
        language_id = str(value(payload, "LanguageId") or "")
        language_code = str(value(payload, "LanguageCode") or "").casefold()
        return by_id.get(language_id) or by_code.get(language_code) or {}

    @staticmethod
    def _is_live(payload, now):
        raw_status = value(payload, "status", "SurveyStatus")
        normalized = str(
            raw_status if raw_status is not None else "Live"
        ).strip().casefold()
        provider_live = normalized in {"1", "live", "open", "active"}
        end_at = datetime_value(value(payload, "SurveyEndDate"))
        return provider_live and (end_at is None or end_at > now), end_at

    def normalize_inventory_item(self, payload, seen_at):
        source = str(value(payload, "SurveyId") or "").strip()
        if not source:
            raise ProviderError("Zamplia inventory row has no SurveyId.")
        language = self._language(payload)
        modified = datetime_value(value(payload, "LastUpdateTimeStamp"))
        created = datetime_value(value(payload, "SurveyCreatedAt"))
        is_live, end_at = self._is_live(payload, seen_at)
        target = max(0, integer(value(payload, "TotalCompleteRequired")))
        language_code = str(
            value(
                language,
                "LanguageCode",
                default=value(payload, "LanguageCode", default=""),
            )
            or ""
        ).strip()
        country_code = str(value(language, "CountryCode") or "").strip().upper()
        if not country_code and "-" in language_code:
            country_code = language_code.rsplit("-", 1)[-1].upper()
        return NormalizedSurvey(
            source_key=source,
            numeric_source_id=integer(source, None),
            modified_at=modified,
            raw_data=payload,
            values={
                "company_name": self.integration.client.name,
                "name": str(
                    value(payload, "Name") or f"Zamplia survey {source}"
                ),
                "status": (
                    Survey.Status.LIVE if is_live else Survey.Status.CLOSED
                ),
                "sample_size": target,
                "completes": 0,
                "remaining": target,
                "cpi": decimal_value(value(payload, "CPI")),
                "loi": max(0, integer(value(payload, "LOI"))),
                "incidence_rate": decimal_value(value(payload, "IR")),
                "country": str(
                    value(language, "Country") or country_code
                ).strip(),
                "country_code": country_code,
                "language": language_code.split("-", 1)[0],
                "language_code": language_code,
                "buyer_id": str(value(payload, "ApiClientId") or ""),
                "survey_type": str(value(payload, "StudyTypes") or "")[:20],
                "group_type": (
                    "Survey group"
                    if integer(value(payload, "IsSurveyGroupExist"))
                    else ""
                ),
                "device_type": str(value(payload, "Device") or ""),
                "job_category": str(value(payload, "IndustryId") or ""),
                "has_quota": True,
                "is_pii_required": bool(
                    integer(value(payload, "CollectPII"))
                ),
                "is_recontact": bool(
                    integer(value(payload, "IsRecontactSurvey"))
                ),
                "source_created_at": created,
                "source_modified_at": modified,
                "source_end_at": end_at,
                "last_seen_at": seen_at,
                "raw_data": payload,
            },
        )

    def detail_signature(self, raw_data):
        return (
            str(value(raw_data or {}, "LastUpdateTimeStamp") or ""),
            str(value(raw_data or {}, "SurveyEndDate") or ""),
            str(value(raw_data or {}, "status", "SurveyStatus") or ""),
        )

    def _demographics(self, language_id):
        key = f"zamplia:demographics:{self.integration.pk}:{language_id}"
        rows = cache.get(key)
        if rows is None:
            rows = self._rows(
                self._request(
                    "/Attributes/GetDemoGraphics",
                    params={"LanguageId": language_id},
                )
            )
            cache.set(key, rows, 86400)
        return {
            str(value(row, "QuestionID")): row
            for row in rows
            if value(row, "QuestionID") not in (None, "")
        }

    def refresh_details(self, survey):
        survey_id = survey.source_key
        detail_rows = self._rows(
            self._request(
                "/Surveys/GetSurveyById",
                params={"SurveyId": survey_id},
            )
        )
        detail = detail_rows[0] if detail_rows else dict(survey.raw_data or {})
        qualification_rows = self._rows(
            self._request(
                "/Surveys/GetSurveyQualifications",
                params={"SurveyId": survey_id},
            )
        )
        quota_result = self._request(
            "/Surveys/GetSurveyQuotas",
            params={"SurveyId": survey_id},
            allow_not_found=True,
        )
        quota_rows = self._rows(quota_result or {})
        stats = self._request(
            "/Surveys/getProjectGlobalstats",
            params={"SurveyId": survey_id},
            allow_not_found=True,
        ) or {}
        stats_data = value(stats, "data", default={}) or {}
        language_id = integer(
            value(
                detail,
                "LanguageId",
                default=value(survey.raw_data or {}, "LanguageId"),
            ),
            None,
        )
        catalog = (
            self._demographics(language_id)
            if language_id is not None
            else {}
        )

        questions = []
        for qualification in qualification_rows:
            question_id = str(
                value(qualification, "QuestionID") or ""
            ).strip()
            if not question_id:
                continue
            metadata = catalog.get(question_id, {})
            selected = split_values(
                value(qualification, "AnswerCodes", default=[]) or []
            )
            labels = {
                str(value(answer, "AnswerCode")): str(
                    value(
                        answer,
                        "AnswerText",
                        default=value(answer, "AnswerCode", default=""),
                    )
                )
                for answer in value(
                    metadata, "AnswerCodes", default=[]
                )
                or []
                if isinstance(answer, dict)
                and value(answer, "AnswerCode") not in (None, "")
            }
            demographic_name = str(
                value(metadata, "DemographicName") or ""
            )
            normalized_name = demographic_name.casefold()
            hint = (
                "postal"
                if any(token in normalized_name for token in ("zip", "postal"))
                else "gender"
                if "gender" in normalized_name
                else "age"
                if "age" in normalized_name or question_id == "29"
                else ""
            )
            questions.append(
                question_row(
                    provider_code=self.code,
                    survey=survey,
                    question_id=question_id,
                    text=(
                        value(metadata, "QuestionText")
                        or f"Provider qualification {question_id}"
                    ),
                    question_type=str(
                        value(
                            qualification,
                            "QuestionType",
                            default=value(
                                metadata, "QuestionType", default=""
                            ),
                        )
                    ),
                    allowed_values=selected,
                    option_labels=labels,
                    category="Zamplia targeting",
                    raw_data={"provider_question": metadata},
                    dimension_hint=hint,
                )
            )

        target = max(
            0,
            integer(
                value(detail, "TotalCompleteRequired"),
                survey.sample_size,
            ),
        )
        completed = max(
            0,
            integer(value(stats_data, "completes"), survey.completes),
        )
        overall_remaining = max(0, target - completed)
        quotas = []
        for position, quota in enumerate(quota_rows, start=1):
            quota_id = str(
                value(quota, "QuotaId", "QuotaID", "Id")
                or f"quota-{position}"
            )
            quota_target = max(
                0,
                integer(
                    value(
                        quota,
                        "TotalCompleteRequired",
                        "CompletesRequired",
                        "TotalQuotaCount",
                        "Target",
                    ),
                    0,
                ),
            )
            quota_completed = max(
                0,
                integer(
                    value(quota, "Completes", "TotalCompletes"),
                    0,
                ),
            )
            quota_remaining = max(
                0,
                integer(
                    value(quota, "Remaining", "TotalRemaining"),
                    max(0, quota_target - quota_completed),
                ),
            )
            quota_qualifications = value(
                quota,
                "QuotaQualifications",
                "Qualifications",
                "Criteria",
                default=[],
            ) or []
            if isinstance(quota_qualifications, dict):
                quota_qualifications = [quota_qualifications]
            if not isinstance(quota_qualifications, list):
                quota_qualifications = []
            quota_datapoints = []
            quota_targeting_details = []
            for qualification in quota_qualifications:
                if not isinstance(qualification, dict):
                    continue
                question_id = str(
                    value(qualification, "QuestionId", "QuestionID") or ""
                ).strip()
                answer_codes = split_values(
                    value(qualification, "AnswerCodes", default=[])
                )
                metadata = catalog.get(question_id, {})
                answer_labels = {
                    str(value(answer, "AnswerCode")): str(
                        value(
                            answer,
                            "AnswerText",
                            default=value(answer, "AnswerCode", default=""),
                        )
                    )
                    for answer in value(metadata, "AnswerCodes", default=[]) or []
                    if isinstance(answer, dict)
                    and value(answer, "AnswerCode") not in (None, "")
                }
                question_text = str(
                    value(metadata, "QuestionText")
                    or f"Provider qualification {question_id}"
                )
                readable_answers = [
                    answer_labels.get(code, code) for code in answer_codes
                ]
                quota_datapoints.append(
                    {
                        "question_id": question_id,
                        "name": question_text,
                        "values": answer_codes,
                    }
                )
                quota_targeting_details.append(
                    {"name": question_text, "values": readable_answers}
                )
            quotas.append(
                SurveyQuota(
                    survey=survey,
                    source_key=quota_id,
                    quota_id=integer(quota_id, None),
                    title=f"Quota {position}",
                    name=str(
                        value(quota, "Name", "QuotaName")
                        or "Targeted respondent quota"
                    ),
                    sample_size=quota_target,
                    completes=quota_completed,
                    remaining=quota_remaining,
                    status=str(
                        value(quota, "Status")
                        or ("Open" if quota_remaining else "Full")
                    ),
                    targeting={
                        "qualifications": quota_qualifications,
                        "datapoints": quota_datapoints,
                    },
                    raw_data={
                        **quota,
                        "targeting_details": quota_targeting_details,
                        "_target_known": bool(quota_target),
                        "_completed_known": True,
                        "quotaLimitBy": "completes",
                    },
                )
            )
        if not quotas:
            quotas.append(
                SurveyQuota(
                    survey=survey,
                    source_key="overall",
                    title="Overall quota",
                    name="Overall survey quota",
                    sample_size=target,
                    completes=completed,
                    remaining=overall_remaining,
                    status="Open" if overall_remaining else "Full",
                    raw_data={
                        "_target_known": bool(target),
                        "_completed_known": True,
                        "quotaLimitBy": "completes",
                    },
                )
            )

        is_live, end_at = self._is_live(detail, timezone.now())
        merged_raw = {
            **(survey.raw_data or {}),
            **detail,
            "_global_stats": stats_data,
        }
        persist_details(
            survey,
            questions,
            quotas,
            survey_updates={
                "sample_size": target,
                "completes": completed,
                "remaining": overall_remaining,
                "status": (
                    Survey.Status.LIVE
                    if is_live
                    else Survey.Status.CLOSED
                ),
                "source_end_at": end_at,
                "buyer_id": str(
                    value(detail, "ApiClientId") or survey.buyer_id
                ),
                "raw_data": merged_raw,
            },
        )

    def build_outbound_url(self, survey, attempt, answers):
        detail = survey.raw_data or {}
        api_client_id = str(
            value(detail, "ApiClientId") or survey.buyer_id or ""
        ).strip()
        if not api_client_id:
            raise ProviderError(
                "Refresh Zamplia survey details before generating an entry link."
            )
        profile_uid = effective_profile_uid(attempt) or attempt.rid
        params = [
            ("vid", self.client_id),
            ("isMap", "1"),
            ("sid", survey.source_key),
            ("umid", profile_uid),
            ("UID", attempt.rid),
            ("cid", api_client_id),
        ]
        for answer in answers.values():
            question_id = str(answer.get("question_id") or "").strip()
            selected = [
                str(item).strip()
                for item in answer.get("upstream_values") or []
                if str(item).strip()
            ]
            if question_id and selected:
                params.append((f"q{question_id}", ",".join(selected)))
        participant_base = str(
            (self.integration.config or {}).get("participant_base_url")
            or "https://zampparticipant.zamplia.com/"
        ).rstrip("/") + "/"
        parsed = urlsplit(participant_base)
        if (
            parsed.scheme != "https"
            or parsed.hostname
            not in {"zampparticipant.zamplia.com", "res.zamplia.com"}
            or parsed.query
            or parsed.fragment
        ):
            raise ProviderConfigurationError(
                "Use Zamplia's official HTTPS participant host."
            )
        unsigned_url = f"{participant_base}?{urlencode(params)}"
        if self.hmac_reference:
            secret = environment_value(
                self.hmac_reference, "Zamplia HMAC secret"
            )
            signature = hmac.new(
                secret.encode("utf-8"),
                unsigned_url.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            return f"{unsigned_url}&hash={signature}"
        return unsigned_url
