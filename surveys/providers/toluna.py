import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote_plus, urlsplit, urlunsplit

import requests
from prescreener_vault.reuse import effective_profile_uid
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from surveys.age_rules import normalize_age_range
from surveys.models import (
    Survey,
    SurveyQuota,
    TargetingQuestion,
    TolunaMember,
    TolunaReferenceQuestion,
)

from .base import (
    NormalizedSurvey,
    ProviderConfigurationError,
    ProviderError,
    SurveyProvider,
    environment_value,
)


COUNTRY_NAMES = {
    "CA": "Canada",
    "GB": "United Kingdom",
    "IN": "India",
    "SG": "Singapore",
    "US": "United States",
}
LANGUAGE_NAMES = {"EN": "English", "FR": "French", "HI": "Hindi"}

# Toluna documents these as culture-independent common attributes. Age is a
# special case: it is intentionally absent from the Reference Data API, but it
# can still appear in quota requirements and a member DOB is required before an
# invite can be generated.
COMMON_AGE_QUESTION_ID = 1001538
COMMON_AGE_OPTIONS = (
    (2006351, "13-17", 13, 17),
    (2006352, "18-24", 18, 24),
    (2006353, "25-29", 25, 29),
    (2006354, "30-34", 30, 34),
    (2006355, "35-39", 35, 39),
    (2006356, "40-44", 40, 44),
    (2006357, "45-49", 45, 49),
    (2006358, "50-54", 50, 54),
    (2006359, "55-59", 55, 59),
    (2006360, "60-64", 60, 64),
    (2006361, "65 and older", 65, None),
)
TOLUNA_MAX_RESPONDENT_AGE = 99
COMMON_GENDER_QUESTION_ID = 1001007
COMMON_GENDER_OPTIONS = (
    (2000246, "Female"),
    (2000247, "Male"),
)
TOLUNA_ADAPTER_VERSION = 7


def _pick(payload, *names, default=None):
    if not isinstance(payload, dict):
        return default
    for name in names:
        if name in payload:
            return payload[name]
    lowered = {str(key).lower(): value for key, value in payload.items()}
    for name in names:
        if str(name).lower() in lowered:
            return lowered[str(name).lower()]
    return default


def _answer_values(payload):
    """Normalize Toluna's scalar/list and documented comma-separated values."""

    values = _pick(payload, "AnswerValues", default=[])
    if values is None:
        return []
    if isinstance(values, (str, int, float)):
        values = [values]
    normalized = []
    for value in values:
        for part in str(value).split(","):
            item = part.strip()
            if item and item not in normalized:
                normalized.append(item)
    return normalized


def _integer(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _decimal(value):
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=dt_timezone.utc) if parsed.tzinfo is None else parsed.astimezone(dt_timezone.utc)


class TolunaAPIError(ProviderError):
    """A structured Toluna HTTP error with its documented ResultCode."""

    def __init__(self, message, *, http_status=None, result_code=None, result=""):
        super().__init__(message)
        self.http_status = http_status
        self.result_code = result_code
        self.result = str(result or "")


class TolunaInviteRejected(ProviderError):
    """A terminal invite outcome that should be recorded instead of retried."""

    def __init__(self, message, *, status_code, result_code=None, result=""):
        super().__init__(message)
        self.status_code = str(status_code)
        self.result_code = result_code
        self.result = str(result or "")


# Toluna External Sample returns these documented business ResultCodes while
# generating an invite. They are terminal respondent/survey outcomes, not
# malformed prescreener answers and not application failures.
TOLUNA_INVITE_RESULT_STATUS = {
    # Survey/quota is unavailable for this partner or route.
    5: "7", 6: "7", 8: "7", 9: "7", 10: "7", 15: "7", 17: "7",
    23: "7", 24: "7", 25: "7", 27: "7", 32: "7", 33: "7", 37: "7",
    42: "7", 43: "7", 45: "7",
    # Capacity outcomes.
    28: "3", 29: "3", 41: "3", 46: "3", 47: "3",
    # Profile/device qualification outcomes.
    13: "11", 16: "11", 21: "11", 31: "11", 35: "11",
    # Security/quality outcomes.
    14: "4", 22: "4", 34: "4",
    # Repeat participation outcomes.
    26: "12", 30: "12", 38: "12",
    # Frequency caps.
    39: "10", 40: "10",
}

TOLUNA_INVITE_STATUS_MESSAGES = {
    "3": "The selected Toluna quota is full.",
    "4": "Toluna rejected this attempt for a security or quality rule.",
    "7": "This Toluna survey is not currently available for this respondent route.",
    "10": "This respondent has reached Toluna's survey frequency limit.",
    "11": "The respondent profile does not match an available Toluna route.",
    "12": "This respondent has already taken or been excluded from this Toluna survey.",
}


class TolunaProvider(SurveyProvider):
    """Toluna External Sample adapter for inventory, members and invites."""

    code = "toluna"
    label = "Toluna Integrated Panel"
    default_base_url = "https://tws.toluna.com"
    minimum_sync_interval_seconds = 60
    credential_fields = (
        ("api_auth_key", "External Sample API key environment variable"),
        ("partner_auth_key", "Reference Data API key environment variable"),
        ("hmac_key", "Callback HMAC key environment variable"),
    )

    def __init__(self, integration, *, session=None):
        super().__init__(integration, session=session or requests.Session())
        config = integration.config or {}
        environment = str(config.get("environment") or "production").lower()
        if environment not in {"production", "sandbox"}:
            raise ProviderConfigurationError("Toluna environment must be production or sandbox.")
        self.environment = environment
        if environment == "sandbox":
            self.member_base_url = str(config.get("member_base_url") or "https://training.ups.toluna.com").rstrip("/")
            self.es_base_url = str(config.get("external_sample_base_url") or "https://training.ups.toluna.com").rstrip("/")
        else:
            self.member_base_url = str(config.get("member_base_url") or "https://ip.surveyrouter.com").rstrip("/")
            self.es_base_url = str(config.get("external_sample_base_url") or integration.base_url or self.default_base_url).rstrip("/")
        self.reference_base_url = str(config.get("reference_base_url") or "https://tws.toluna.com").rstrip("/")
        self.timeout = max(5, _integer(config.get("timeout_seconds"), 30))
        refs = integration.credential_env_keys or {}
        self.api_auth_key = environment_value(refs.get("api_auth_key"), "Toluna API_AUTH_KEY")
        self.partner_auth_key = environment_value(refs.get("partner_auth_key"), "Toluna PARTNER_AUTH_KEY")
        self._credential_refs = refs
        self.inventory_cache_expires_at = None

    @property
    def api_headers(self):
        return {"Accept": "application/json", "API_AUTH_KEY": self.api_auth_key}

    @property
    def reference_headers(self):
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "PARTNER_AUTH_KEY": self.partner_auth_key,
        }

    def _request(self, method, url, *, expected=(200,), allow_empty=False, **kwargs):
        try:
            response = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise ProviderError("Toluna request could not reach the upstream service.") from exc
        if response.status_code not in expected:
            # Toluna normally returns a small Result/ResultCode or validation
            # message for 400 responses. Surface only known diagnostic fields:
            # never echo the complete response because it may contain member
            # profile data.
            detail = self._safe_error_detail(response)
            suffix = f": {detail}" if detail else "."
            try:
                error_payload = response.json()
            except (TypeError, ValueError):
                error_payload = {}
            result = _pick(error_payload, "Result", default="")
            result_code = _integer(_pick(error_payload, "ResultCode"), None)
            raise TolunaAPIError(
                f"Toluna request failed (HTTP {response.status_code}){suffix}",
                http_status=response.status_code,
                result_code=result_code,
                result=result,
            )
        if response.status_code in {201, 204}:
            return None, response.status_code
        # Toluna's member PUT endpoint documents a successful 200 response but
        # returns no JSON body. Only callers that explicitly opt in may accept
        # an empty response; inventory and invite endpoints still require JSON.
        if allow_empty and not bytes(getattr(response, "content", b"") or b""):
            return None, response.status_code
        try:
            return response.json(), response.status_code
        except ValueError as exc:
            raise ProviderError("Toluna returned invalid JSON.") from exc

    @staticmethod
    def _safe_error_detail(response):
        allowed = {
            "result", "resultcode", "message", "errormessage", "error",
            "errors", "modelstate", "title", "detail",
        }
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            selected = {
                str(key): value
                for key, value in payload.items()
                if str(key).lower() in allowed and value not in (None, "", [], {})
            }
            if selected:
                rendered = json.dumps(selected, ensure_ascii=True, separators=(",", ":"))
                # Toluna sometimes embeds the complete rejected member body
                # inside Message. Never send MemberCode, GUID, DOB or profile
                # answers back to the browser/log-facing exception.
                if "Cannot Register MemberCode" in rendered:
                    question_ids = sorted(set(re.findall(r"QuestionID\\?[\"']?\s*:\s*(\d+)", rendered)))
                    suffix = f" (question IDs: {', '.join(question_ids)})" if question_ids else ""
                    return f"Toluna rejected one or more member profile attributes{suffix}."
                return rendered[:800]
        text = str(getattr(response, "text", "") or "").strip()
        if text and "<html" not in text.lower():
            return re.sub(r"\s+", " ", text)[:500]
        return ""

    def _panels(self):
        panels = []
        for key, env_name in sorted(self._credential_refs.items()):
            if not str(key).startswith("panel_"):
                continue
            culture = str(key)[6:].replace("_", "-").lower()
            if not re.fullmatch(r"[a-z]{2}-[a-z]{2}", culture):
                raise ProviderConfigurationError(f"Invalid Toluna panel mapping key: {key}.")
            panels.append((culture, environment_value(env_name, f"Toluna {culture} PanelGUID")))
        if not panels:
            raise ProviderConfigurationError("Configure at least one Toluna panel_<culture> environment mapping.")
        return panels

    def _cultures(self):
        payload, _ = self._request(
            "GET",
            f"{self.reference_base_url}/IPUtilityService/ReferenceData/Cultures",
            headers=self.reference_headers,
        )
        if not isinstance(payload, list):
            raise ProviderError("Toluna cultures response must be a list.")
        return {
            str(_pick(row, "Name", default="")).lower(): row
            for row in payload
            if isinstance(row, dict) and _pick(row, "Name") and _pick(row, "CultureID") is not None
        }

    def _reference_data_is_fresh(self, cultures):
        cutoff = timezone.now() - timedelta(
            hours=max(1, _integer((self.integration.config or {}).get("reference_refresh_hours"), 24))
        )
        return all(
            TolunaReferenceQuestion.objects.filter(
                integration=self.integration,
                culture_code=culture,
                updated_at__gte=cutoff,
            ).exists()
            for culture in cultures
        )

    def _sync_reference_data(self, culture_rows):
        cultures = list(culture_rows)
        if self._reference_data_is_fresh(cultures):
            return TolunaReferenceQuestion.objects.filter(
                integration=self.integration, culture_code__in=cultures
            ).count()
        culture_ids = [_integer(culture_rows[culture].get("CultureID")) for culture in cultures]
        payload, _ = self._request(
            "POST",
            f"{self.reference_base_url}/IPUtilityService/ReferenceData/QuestionsAndAnswersData",
            headers=self.reference_headers,
            json={
                "CultureIDs": culture_ids,
                "CategoryIDs": [],
                "IncludeComputed": False,
                "IncludeRoutables": True,
                "IncludeDemographics": True,
            },
        )
        if not isinstance(payload, list):
            raise ProviderError("Toluna question-and-answer response must be a list.")
        culture_by_id = {value: key for key, value in zip(cultures, culture_ids)}
        rows = []
        for item in payload:
            translated = _pick(item, "TranslatedQuestion", default={}) or {}
            question_id = _integer(_pick(translated, "QuestionID"), -1)
            culture_id = _integer(_pick(translated, "CultureID"), -1)
            culture_code = culture_by_id.get(culture_id)
            if not culture_code or question_id < 0:
                continue
            options = []
            for answer in _pick(item, "TranslatedAnswers", default=[]) or []:
                answer_id = _integer(_pick(answer, "AnswerID"), -1)
                if answer_id < 0:
                    continue
                options.append({
                    "OptionId": answer_id,
                    "OptionText": str(_pick(answer, "Translation", "AnswerInternalName", default=answer_id)),
                    "AnswerInternalName": str(_pick(answer, "AnswerInternalName", default="")),
                })
            rows.append(TolunaReferenceQuestion(
                integration=self.integration,
                culture_code=culture_code,
                culture_id=culture_id,
                question_id=question_id,
                internal_name=str(_pick(item, "InternalName", default="")),
                display_name=str(_pick(translated, "DisplayNameTranslation", default="")),
                answer_type=str(_pick(item, "AnswerType", default="")),
                is_routable=bool(_pick(item, "IsRoutable", default=False)),
                options=options,
                raw_data=item,
            ))
        existing_by_culture = {
            (row.culture_code, row.question_id) for row in rows
        }
        for culture_code, culture_row in culture_rows.items():
            culture_id = _integer(culture_row.get("CultureID"), -1)
            if (culture_code, COMMON_AGE_QUESTION_ID) not in existing_by_culture:
                rows.append(TolunaReferenceQuestion(
                    integration=self.integration,
                    culture_code=culture_code,
                    culture_id=culture_id,
                    question_id=COMMON_AGE_QUESTION_ID,
                    internal_name="Age",
                    display_name="What is your age?",
                    answer_type="SingleSelect",
                    is_routable=False,
                    options=[
                        {
                            "OptionId": answer_id,
                            "OptionText": label,
                            "AnswerInternalName": label,
                            "ageStart": minimum,
                            "ageEnd": maximum,
                        }
                        for answer_id, label, minimum, maximum in COMMON_AGE_OPTIONS
                    ],
                    raw_data={"source": "toluna_common_attributes"},
                ))
            if (culture_code, COMMON_GENDER_QUESTION_ID) not in existing_by_culture:
                rows.append(TolunaReferenceQuestion(
                    integration=self.integration,
                    culture_code=culture_code,
                    culture_id=culture_id,
                    question_id=COMMON_GENDER_QUESTION_ID,
                    internal_name="Gender",
                    display_name="What is your gender?",
                    answer_type="SingleSelect",
                    is_routable=False,
                    options=[
                        {
                            "OptionId": answer_id,
                            "OptionText": label,
                            "AnswerInternalName": label,
                        }
                        for answer_id, label in COMMON_GENDER_OPTIONS
                    ],
                    raw_data={"source": "toluna_common_attributes"},
                ))
        with transaction.atomic():
            TolunaReferenceQuestion.objects.filter(
                integration=self.integration, culture_code__in=cultures
            ).delete()
            TolunaReferenceQuestion.objects.bulk_create(rows, batch_size=500)
        return len(rows)

    def test_connection(self):
        panels = self._panels()
        cultures = self._cultures()
        missing = [culture for culture, _ in panels if culture not in cultures]
        if missing:
            raise ProviderConfigurationError(f"Toluna cultures not available: {', '.join(missing)}.")
        culture_rows = {culture: cultures[culture] for culture, _ in panels}
        reference_count = self._sync_reference_data(culture_rows)
        first_culture, first_panel = panels[0]
        settings_payload, _ = self._request(
            "GET",
            f"{self.es_base_url}/IntegratedPanelService/api/Settings",
            headers=self.api_headers,
            params={"partnerGUID": first_panel},
        )
        return {
            "provider": self.code,
            "authenticated": True,
            "environment": self.environment,
            "configured_cultures": [culture for culture, _ in panels],
            "reference_questions": reference_count,
            "settings_received": len(settings_payload or []),
            "tested_culture": first_culture,
        }

    def inventory(self):
        panels = self._panels()
        cultures = self._cultures()
        missing = [culture for culture, _ in panels if culture not in cultures]
        if missing:
            raise ProviderConfigurationError(f"Toluna cultures not available: {', '.join(missing)}.")
        self._sync_reference_data({culture: cultures[culture] for culture, _ in panels})
        inventory = []
        cache_expiries = []
        for culture, panel_guid in panels:
            payload, _ = self._request(
                "GET",
                f"{self.es_base_url}/IPExternalSamplingService/ExternalSample/{panel_guid}/Quotas",
                headers=self.api_headers,
                params={"includeRoutables": "true"},
            )
            if not isinstance(payload, dict) or not isinstance(_pick(payload, "Surveys", default=[]), list):
                raise ProviderError(f"Toluna quota response for {culture} is invalid.")
            metadata = {
                "culture_code": culture,
                "culture_id": _integer(cultures[culture].get("CultureID")),
                "country_id": _integer(_pick(payload, "CountryID")),
                "cache_expires": _pick(payload, "CacheExpires"),
            }
            cache_expires_at = _datetime(metadata["cache_expires"])
            if cache_expires_at is not None:
                cache_expiries.append(cache_expires_at)
            for survey in _pick(payload, "Surveys", default=[]):
                if isinstance(survey, dict) and _pick(survey, "SurveyID") is not None and _pick(survey, "WaveID") is not None:
                    inventory.append({"survey": survey, "toluna": metadata})
        self.inventory_cache_expires_at = min(cache_expiries, default=None)
        return inventory

    def normalize_inventory_item(self, payload, seen_at):
        survey = payload.get("survey") or {}
        metadata = payload.get("toluna") or {}
        survey_id = _integer(_pick(survey, "SurveyID"), -1)
        wave_id = _integer(_pick(survey, "WaveID"), -1)
        if survey_id < 0 or wave_id < 0:
            raise ProviderError("Toluna inventory row is missing SurveyID or WaveID.")
        culture = str(metadata.get("culture_code") or "").lower()
        language_code, country_code = (culture.split("-", 1) + [""])[:2]
        required = max(0, _integer(_pick(survey, "CompletesRequired")))
        remaining = max(0, _integer(_pick(survey, "EstimatedCompletesRemaining")))
        price = _pick(survey, "Price", default={}) or {}
        created = _datetime(_pick(
            survey,
            "CreatedDate", "CreationDate", "CreatedAt", "LaunchDate", "StartDate",
        ))
        modified = _datetime(_pick(
            survey,
            "LastUpdated", "ModifiedDate", "LastModified", "UpdatedAt", "UpdateDate",
        )) or created
        devices = [str(item) for item in (_pick(survey, "DeviceTypeIDs", default=[]) or [])]
        source_key = f"{survey_id}:{wave_id}"
        raw_data = {**survey, "_toluna": metadata}
        return NormalizedSurvey(
            source_key=source_key,
            numeric_source_id=None,
            modified_at=modified,
            raw_data=raw_data,
            values={
                "company_name": self.integration.client.name,
                "name": str(_pick(survey, "SurveyName", default=f"Toluna survey {survey_id}")),
                "status": Survey.Status.LIVE if remaining > 0 else Survey.Status.CLOSED,
                "sample_size": required,
                "completes": max(0, required - remaining),
                "remaining": remaining,
                "cpi": _decimal(_pick(price, "Amount")),
                "loi": max(0, _integer(_pick(survey, "LOI"))),
                "incidence_rate": _decimal(_pick(survey, "IR")),
                "country": COUNTRY_NAMES.get(country_code.upper(), country_code.upper()),
                "country_code": country_code.upper(),
                "language": LANGUAGE_NAMES.get(language_code.upper(), language_code.upper()),
                "language_code": language_code.upper(),
                "group_type": str(_pick(survey, "StudyTypeID", default="")),
                "buyer_id": str(wave_id),
                "survey_type": "Recontact" if bool(_pick(survey, "IsSurveyRecontact", default=False)) else "Standard",
                "device_type": ", ".join(devices),
                "has_quota": bool(_pick(survey, "Quotas", default=[])),
                "is_recontact": bool(_pick(survey, "IsSurveyRecontact", default=False)),
                "source_created_at": created,
                "source_modified_at": modified,
                "last_seen_at": seen_at,
                "entry_link": "",
                "raw_data": raw_data,
            },
        )

    @staticmethod
    def _quota_question_rows(quotas):
        rows = {}
        for quota in quotas:
            remaining_raw = _pick(
                quota, "EstimatedCompletesRemaining", default=None
            )
            if remaining_raw is not None and _integer(remaining_raw, -1) <= 0:
                continue
            layers = _pick(quota, "Layers", default=[]) or []

            def subquota_has_capacity(subquota):
                current_raw = _pick(subquota, "CurrentCompletes", default=None)
                maximum_raw = _pick(
                    subquota, "MaxTargetCompletes", default=None
                )
                if current_raw is None or maximum_raw is None:
                    return True
                current = _integer(current_raw, -1)
                maximum = _integer(maximum_raw, -1)
                return maximum < 0 or current < maximum

            # Layers are AND. If any layer has no open SubQuota, this entire
            # top-level quota cannot route and none of its values should be
            # advertised by the pre-screener.
            if any(
                not any(subquota_has_capacity(item) for item in (
                    _pick(layer, "SubQuotas", default=[]) or []
                ))
                for layer in layers
            ):
                continue
            for layer in layers:
                for subquota in _pick(layer, "SubQuotas", default=[]) or []:
                    if not subquota_has_capacity(subquota):
                        continue
                    for item in _pick(subquota, "QuestionsAndAnswers", default=[]) or []:
                        question_id = _integer(_pick(item, "QuestionID"), -1)
                        if question_id < 0:
                            continue
                        current = rows.setdefault(question_id, {
                            "answer_ids": set(), "answer_values": [], "raw": [],
                            "all_routable": True,
                        })
                        current["answer_ids"].update(
                            _integer(value, -1) for value in (_pick(item, "AnswerIDs", default=[]) or [])
                        )
                        current["answer_ids"].discard(-1)
                        for normalized in _answer_values(item):
                            if normalized not in current["answer_values"]:
                                current["answer_values"].append(normalized)
                        current["raw"].append(item)
                        current["all_routable"] = current["all_routable"] and bool(
                            _pick(item, "IsRoutable", default=False)
                        )
        return rows

    @staticmethod
    def _question_kind(reference):
        text = f"{reference.internal_name} {reference.display_name}".lower()
        if "gender" in text or text.strip() in {"sex", "member sex"}:
            return "gender"
        if "postal" in text or "zip" in text:
            return "postal"
        if re.search(r"\bage\b|birth", text):
            return "birth_date"
        return "profile"

    @staticmethod
    def _age_range(value):
        return normalize_age_range(value)

    @classmethod
    def _age_option_range(cls, option):
        return normalize_age_range(option)

    @classmethod
    def _targeting_age_ranges(cls, requirement, reference):
        ranges = {
            parsed
            for value in requirement.get("answer_values") or []
            if (parsed := cls._age_range(value)) is not None
        }
        allowed_ids = {str(value) for value in requirement.get("answer_ids") or []}
        for option in reference.options or []:
            if str(option.get("OptionId")) not in allowed_ids:
                continue
            parsed = cls._age_option_range(option)
            if parsed is not None:
                ranges.add(parsed)
        return [
            {"min": minimum, "max": maximum}
            for minimum, maximum in sorted(
                ranges,
                key=lambda item: (
                    item[0],
                    TOLUNA_MAX_RESPONDENT_AGE + 1 if item[1] is None else item[1],
                ),
            )
        ]

    def refresh_details(self, survey):
        # Inventory sync and on-demand prescreener hydration can run at the
        # same time. Re-read the row under a lock before deriving questions so
        # a newer raw quota contract can never be overwritten by rows built
        # from an older in-memory Survey instance.
        with transaction.atomic():
            locked = Survey.objects.select_for_update().get(pk=survey.pk)
            result = self._refresh_details_locked(locked)
        # Notification reconciliation acquires its own row locks. Run it only
        # after releasing the Survey lock to keep a consistent lock order with
        # the webhook path and avoid Survey/Notification deadlocks.
        from ..toluna_notifications import reconcile_toluna_operational_notifications

        reconcile_toluna_operational_notifications(
            locked,
            replay_applied=True,
            applied_since=locked.last_seen_at,
        )
        return result

    def _refresh_details_locked(self, survey):
        raw = survey.raw_data or {}
        quotas = _pick(raw, "Quotas", default=[]) or []
        culture = str((raw.get("_toluna") or {}).get("culture_code") or "").lower()
        references = {
            row.question_id: row
            for row in TolunaReferenceQuestion.objects.filter(
                integration=self.integration, culture_code=culture
            )
        }
        requirements = self._quota_question_rows(quotas)
        mandatory = {}
        for row in references.values():
            kind = self._question_kind(row)
            if kind in {"birth_date", "gender"} and kind not in mandatory:
                mandatory[kind] = row
        # Ask every quota attribute locally, including attributes Toluna marks
        # routable. When the respondent supplies a mapped routable answer we
        # also register it with Toluna; leaving a known answer out forces a
        # second provider prescreener and can make the member fail the exact
        # quota that was already matched here.
        question_ids = set(requirements)
        question_ids.update(row.question_id for row in mandatory.values())
        questions = []
        for question_id in sorted(question_ids):
            reference = references.get(question_id)
            if reference is None:
                raise ProviderError(f"Toluna reference mapping is missing question {question_id} for {culture}.")
            kind = self._question_kind(reference)
            requirement = requirements.get(question_id, {})
            allowed_ids = sorted(requirement.get("answer_ids") or [])
            allowed_values = list(requirement.get("answer_values") or [])
            required_by_provider = bool(requirement)
            reference_options = list(reference.options or [])
            answer_type = reference.answer_type.lower()
            # Age is collected as the respondent's exact numeric value. Keep
            # Toluna's answer-ID mapping in raw data for quota evaluation, while
            # exposing the exact union of textual and ID-backed quota ranges to
            # the pre-screener. Choice options would incorrectly widen partial
            # ranges such as 21-29 to Toluna's common 18-24 bucket.
            normalized_allowed_values = {
                str(value).strip().casefold()
                for value in allowed_values
                if str(value).strip()
            }

            def option_is_required(option):
                if not required_by_provider or (not allowed_ids and not normalized_allowed_values):
                    return True
                option_labels = {
                    str(option.get("OptionText") or "").strip().casefold(),
                    str(option.get("AnswerInternalName") or "").strip().casefold(),
                }
                return (
                    _integer(option.get("OptionId"), -1) in allowed_ids
                    or bool(normalized_allowed_values.intersection(option_labels - {""}))
                )

            if kind == "birth_date":
                options = []
            elif kind == "postal" or "open" in answer_type:
                # Open-ended questions can carry a synthetic AnswerID that is
                # required alongside AnswerValue during member registration.
                # Keep that mapping internally; the prescreener still renders
                # a text input and shows the quota values as guidance.
                options = reference_options
            else:
                options = [
                    option for option in reference_options if option_is_required(option)
                ]
            targeting_age_ranges = (
                self._targeting_age_ranges(requirement, reference)
                if kind == "birth_date" else []
            )
            question_type = (
                "numeric" if kind == "birth_date"
                else "text" if kind == "postal" or "open" in answer_type
                else "multi" if "multi" in answer_type
                else "single"
            )
            text = "What is your age?" if kind == "birth_date" else (
                "What is your postal code?" if kind == "postal" else reference.display_name or reference.internal_name
            )
            questions.append(TargetingQuestion(
                survey=survey,
                question_id=question_id,
                key=f"TOLUNA_{question_id}",
                text=text,
                question_type=question_type,
                category=(
                    "Required profile"
                    if kind in {"birth_date", "gender"} or required_by_provider
                    else "Toluna targeting"
                ),
                options=options,
                raw_data={
                    "adapter_version": TOLUNA_ADAPTER_VERSION,
                    "toluna_kind": kind,
                    "toluna_is_routable": bool(
                        requirement.get("all_routable")
                        if requirement
                        else reference.is_routable
                    ),
                    "allowed_answer_ids": allowed_ids,
                    "allowed_answer_values": allowed_values,
                    "required_by_provider": required_by_provider,
                    "required_for_member": (
                        kind in {"birth_date", "gender"}
                        or (
                            required_by_provider
                            and not requirement.get("all_routable")
                            and "computed" not in answer_type
                        )
                    ),
                    "reference_answer_type": reference.answer_type,
                    **({
                        "targeting_age_ranges": targeting_age_ranges,
                        "toluna_age_options": reference_options,
                    } if kind == "birth_date" else {}),
                },
            ))
        quota_rows = []
        for quota in quotas:
            quota_id = _integer(_pick(quota, "QuotaID"), -1)
            if quota_id < 0:
                continue
            target = max(0, _integer(_pick(quota, "CompletesRequired")))
            remaining = max(0, _integer(_pick(quota, "EstimatedCompletesRemaining")))
            quota_rows.append(SurveyQuota(
                survey=survey,
                source_key=str(quota_id),
                quota_id=quota_id,
                title=f"Toluna quota {quota_id}",
                name=f"Quota {quota_id}",
                sample_size=target,
                completes=max(0, target - remaining),
                remaining=remaining,
                status="Open" if remaining > 0 else "Full",
                targeting={"layers": _pick(quota, "Layers", default=[]) or []},
                raw_data=quota,
            ))
        now = timezone.now()
        with transaction.atomic():
            survey.targeting_questions.all().delete()
            survey.quotas.all().delete()
            TargetingQuestion.objects.bulk_create(questions)
            SurveyQuota.objects.bulk_create(quota_rows)
            survey.has_quota = bool(quota_rows)
            survey.targeting_synced_at = now
            survey.quota_synced_at = now
            survey.detail_synced_at = now
            survey.save(update_fields=[
                "has_quota", "targeting_synced_at", "quota_synced_at", "detail_synced_at", "updated_at"
            ])
    @staticmethod
    def _answers_by_question(answers):
        return {
            _integer(answer.get("question_id"), -1): answer
            for answer in answers.values()
            if _integer(answer.get("question_id"), -1) >= 0
        }

    @staticmethod
    def _age(value):
        raw = str(value).strip()
        if raw.isdigit():
            return int(raw)
        born = date.fromisoformat(raw)
        today = date.today()
        return today.year - born.year - ((today.month, today.day) < (born.month, born.day))

    @staticmethod
    def _birth_date(value, member_code=""):
        """Return a stable, random-looking API DOB for a respondent-entered age."""
        raw = str(value).strip()
        if raw.isdigit():
            age = int(raw)
            if not 1 <= age <= TOLUNA_MAX_RESPONDENT_AGE:
                raise ProviderError("Enter a valid age before Toluna member registration.")
            today = date.today()
            # Month/day are spread across the calendar but remain stable for
            # the same platform UID. The birth year is then chosen so the age
            # is exact on today's date, including respondents whose birthday
            # has not occurred yet this year.
            digest = hashlib.sha256(str(member_code or raw).encode("utf-8")).digest()
            month = digest[0] % 12 + 1
            day = digest[1] % 28 + 1
            year = today.year - age - ((month, day) > (today.month, today.day))
            born = date(year, month, day)
        else:
            try:
                born = date.fromisoformat(raw)
            except ValueError as exc:
                raise ProviderError("Enter a valid age before Toluna member registration.") from exc
            age = TolunaProvider._age(born.isoformat())
            if not 1 <= age <= TOLUNA_MAX_RESPONDENT_AGE:
                raise ProviderError("Enter a valid age before Toluna member registration.")
        return born.strftime("%m/%d/%Y")

    @classmethod
    def _answer_matches(cls, requirement, answer, question):
        values = {str(value) for value in (answer.get("upstream_values") or answer.get("values") or [])}
        allowed_ids = {str(value) for value in (_pick(requirement, "AnswerIDs", default=[]) or [])}
        allowed_values = _answer_values(requirement)
        kind = str((question.raw_data or {}).get("toluna_kind") or "profile")
        if kind == "birth_date":
            try:
                age = cls._age(next(iter(answer.get("values") or [])))
            except (StopIteration, TypeError, ValueError):
                return False
            if not 1 <= age <= TOLUNA_MAX_RESPONDENT_AGE:
                return False
            if allowed_values:
                for item in allowed_values:
                    parsed = cls._age_range(item)
                    if parsed and parsed[0] <= age and (
                        parsed[1] is None or age <= parsed[1]
                    ):
                        return True
            if allowed_ids:
                age_options = (
                    (question.raw_data or {}).get("toluna_age_options")
                    or question.options
                    or []
                )
                for option in age_options:
                    parsed = cls._age_option_range(option)
                    if (
                        str(option.get("OptionId")) in allowed_ids
                        and parsed
                        and parsed[0] <= age
                        and (parsed[1] is None or age <= parsed[1])
                    ):
                        return True
                return False
        if allowed_ids and values.intersection(allowed_ids):
            return True
        if allowed_values:
            raw_value = str(next(iter(answer.get("values") or []), "")).strip().lower()
            if kind == "postal":
                normalized_value = re.sub(r"[\s-]+", "", raw_value)
                normalized_candidates = [
                    re.sub(r"[\s-]+", "", candidate.casefold())
                    for candidate in allowed_values
                ]
                normalized_candidates = [
                    candidate for candidate in normalized_candidates if candidate
                ]
                return bool(normalized_value) and any(
                    normalized_value.startswith(candidate)
                    for candidate in normalized_candidates
                )
            return raw_value in {candidate.lower() for candidate in allowed_values}
        if allowed_ids and "text" in question.question_type.lower():
            option_ids = {str(option.get("OptionId")) for option in question.options}
            if allowed_ids.intersection(option_ids):
                return bool(next(iter(answer.get("values") or []), "").strip())
        return not allowed_ids

    def _matching_quota(self, survey, answers):
        answers_by_question = self._answers_by_question(answers)
        questions = {row.question_id: row for row in survey.targeting_questions.all()}
        for quota in survey.quotas.filter(remaining__gt=0).order_by("pk"):
            layers = _pick(quota.raw_data, "Layers", default=[]) or []
            matched_all_layers = True
            for layer in layers:
                matched_subquota = False
                for subquota in _pick(layer, "SubQuotas", default=[]) or []:
                    current_raw = _pick(subquota, "CurrentCompletes", default=None)
                    maximum_raw = _pick(subquota, "MaxTargetCompletes", default=None)
                    if current_raw is not None and maximum_raw is not None:
                        current = _integer(current_raw, -1)
                        maximum = _integer(maximum_raw, -1)
                        if maximum >= 0 and current >= maximum:
                            continue
                    conditions = _pick(subquota, "QuestionsAndAnswers", default=[]) or []

                    def condition_matches(condition):
                        question_id = _integer(_pick(condition, "QuestionID"), -1)
                        # A legacy row without local Reference Data can still
                        # fall back to Toluna's router. Once we have rendered a
                        # routable question locally, its answer is mandatory
                        # and must qualify just like every non-routable answer.
                        if bool(_pick(condition, "IsRoutable", default=False)) and (
                            question_id not in questions
                        ):
                            return True
                        return (
                            question_id in answers_by_question
                            and question_id in questions
                            and self._answer_matches(
                                condition, answers_by_question[question_id], questions[question_id]
                            )
                        )

                    # Toluna's sampling contract is OR between answer rows for
                    # the same QuestionID, but AND between different
                    # QuestionIDs in one SubQuota. Treating every row as AND
                    # makes range questions impossible to satisfy (for
                    # example an age cannot be both 18-24 and 25-29).
                    conditions_by_question = {}
                    for condition in conditions:
                        question_id = _integer(_pick(condition, "QuestionID"), -1)
                        conditions_by_question.setdefault(question_id, []).append(condition)
                    if all(
                        any(condition_matches(condition) for condition in question_conditions)
                        for question_conditions in conditions_by_question.values()
                    ):
                        matched_subquota = True
                        break
                if not matched_subquota:
                    matched_all_layers = False
                    break
            if matched_all_layers:
                return quota
        raise ProviderError("The respondent profile does not match an open Toluna quota.")

    def _member_payload(self, survey, attempt, answers):
        member_code = effective_profile_uid(attempt)
        refs = self.integration.credential_env_keys or {}
        culture = str((survey.raw_data.get("_toluna") or {}).get("culture_code") or "").replace("-", "_")
        # Toluna's FAQ defines Unique Partner Code, PartnerGUID and PanelGUID
        # as equivalent terms and instructs integrations to use the
        # culture-specific PanelGUID. Keep the old partner_guid mapping only
        # as a backward-compatible fallback for existing configurations.
        partner_guid = environment_value(
            refs.get(f"panel_{culture}") or refs.get("partner_guid"),
            f"Toluna {culture.replace('_', '-')} PanelGUID",
        )
        questions = {row.question_id: row for row in survey.targeting_questions.all()}
        reference_questions = {
            row.question_id: row
            for row in TolunaReferenceQuestion.objects.filter(
                integration=self.integration,
                culture_code=culture.replace("_", "-"),
                question_id__in=questions,
            )
        }
        registration_answers = []
        birth_date = postal_code = ""
        answers_by_question = self._answers_by_question(answers)
        for question_id, question in questions.items():
            answer = answers_by_question.get(question_id) or {}
            values = [
                str(value).strip()
                for value in answer.get("values") or []
                if str(value).strip()
            ]
            upstream_values = [
                str(value).strip()
                for value in answer.get("upstream_values") or values
                if str(value).strip()
            ]
            kind = str((question.raw_data or {}).get("toluna_kind") or "profile")
            if kind == "birth_date":
                if values:
                    birth_date = self._birth_date(values[0], member_code)
                continue
            if kind == "postal":
                question_raw = question.raw_data or {}
                reference = reference_questions.get(question_id)
                reference_type = str(
                    (reference.answer_type if reference else "")
                    or question_raw.get("reference_answer_type")
                    or ""
                ).lower()
                is_routable = bool(
                    question_raw.get("toluna_is_routable")
                    if "toluna_is_routable" in question_raw
                    else (reference.is_routable if reference else False)
                )
                required_for_member = bool(
                    question_raw.get("required_for_member")
                    if "required_for_member" in question_raw
                    else (
                        question_raw.get("required_by_provider")
                        and not is_routable
                        and "computed" not in reference_type
                    )
                )
                if required_for_member and not values:
                    raise ProviderError(
                        f"Toluna member profile is missing required question {question_id}."
                    )
                # PostalCode is a first-class Toluna member property rather
                # than a RegistrationAnswer. Send an entered value even when
                # the corresponding quota attribute is routable/computed;
                # Toluna uses this core field to derive geographic profiles.
                if values:
                    postal_code = values[0]
                continue
            reference = reference_questions.get(question_id)
            reference_type = str(
                (reference.answer_type if reference else "")
                or (question.raw_data or {}).get("reference_answer_type")
                or ""
            ).lower()
            question_raw = question.raw_data or {}
            is_routable = bool(
                question_raw.get("toluna_is_routable")
                if "toluna_is_routable" in question_raw
                else (reference.is_routable if reference else False)
            )
            if "computed" in reference_type:
                continue
            # Adapter-v6 questions carry an explicit member contract. Preserve
            # the old required_by_provider fallback for already-synced rows
            # that predate that flag.
            required_for_member = bool(
                question_raw.get("required_for_member")
                if "required_for_member" in question_raw
                else (
                    kind == "gender"
                    or bool(question_raw.get("required_by_provider"))
                )
            )
            if not values or not upstream_values:
                if required_for_member:
                    raise ProviderError(
                        f"Toluna member profile is missing required question {question_id}."
                    )
                continue
            # Preserve the explicit member-registration contract for ordinary
            # optional questions. Routable answers are the only optional
            # quota answers we forward: when the respondent already supplied
            # one, Toluna does not need to ask it again in its preliminary
            # screener.
            if not required_for_member and not is_routable:
                continue

            option_rows = [
                option for option in question.options or []
                if isinstance(option, dict) and option.get("OptionId") not in (None, "")
            ]
            option_ids = {
                _integer(option.get("OptionId"), -1)
                for option in option_rows
            } - {-1}
            allowed_ids = {
                _integer(value, -1)
                for value in question_raw.get("allowed_answer_ids") or []
            } - {-1}
            mapped = []
            if "open" in reference_type or "text" in question.question_type.lower():
                # Toluna requires every open AnswerValue to be paired with its
                # synthetic/envelope AnswerID. Numeric-looking respondent text
                # is still an AnswerValue, never an AnswerID.
                envelope_ids = (
                    allowed_ids.intersection(option_ids)
                    if allowed_ids and option_ids
                    else allowed_ids or option_ids
                )
                if len(envelope_ids) != 1:
                    if is_routable and not required_for_member:
                        continue
                    raise ProviderError(
                        f"Toluna member question {question_id} has no unambiguous open-answer mapping."
                    )
                envelope_id = next(iter(envelope_ids))
                mapped = [
                    {"AnswerID": envelope_id, "AnswerValue": value}
                    for value in values
                ]
            else:
                valid_ids = allowed_ids or option_ids
                if not valid_ids:
                    if is_routable and not required_for_member:
                        continue
                    raise ProviderError(
                        f"Toluna member question {question_id} has no trusted answer mapping."
                    )
                label_ids = {}
                for option in option_rows:
                    option_id = _integer(option.get("OptionId"), -1)
                    for label in (
                        option.get("OptionText"),
                        option.get("AnswerInternalName"),
                    ):
                        normalized = str(label or "").strip().casefold()
                        if normalized:
                            label_ids.setdefault(normalized, set()).add(option_id)
                for value in upstream_values:
                    answer_id = _integer(value, -1)
                    if answer_id < 0:
                        matches = label_ids.get(value.casefold(), set())
                        answer_id = next(iter(matches)) if len(matches) == 1 else -1
                    if answer_id < 0 or (valid_ids and answer_id not in valid_ids):
                        if is_routable and not required_for_member:
                            mapped = []
                            break
                        raise ProviderError(
                            f"Toluna member question {question_id} has an invalid answer mapping."
                        )
                    mapped.append({"AnswerID": answer_id})
            if len(mapped) != len(values):
                if is_routable and not required_for_member:
                    continue
                raise ProviderError(
                    f"Toluna member question {question_id} could not serialize every required answer."
                )
            registration_answers.append({"QuestionID": question_id, "Answers": mapped})
        if not birth_date:
            raise ProviderError("Date of birth is required before Toluna member registration.")
        if not any(
            (questions.get(_integer(item["QuestionID"])).raw_data or {}).get("toluna_kind") == "gender"
            for item in registration_answers if questions.get(_integer(item["QuestionID"]))
        ):
            raise ProviderError("Gender is required before Toluna member registration.")
        payload = {
            "PartnerGUID": partner_guid,
            "MemberCode": member_code,
            "IsActive": True,
            "BirthDate": birth_date,
            "IsTest": bool((self.integration.config or {}).get("is_test_member", False)),
            "RegistrationAnswers": registration_answers,
        }
        # PostalCode is optional in Toluna's contract. An empty string is not
        # equivalent to omitting an optional value and can be rejected as an
        # invalid property for surveys that did not ask for postal code.
        if postal_code:
            payload["PostalCode"] = postal_code
        return payload

    def _register_member(self, survey, attempt, answers):
        payload = self._member_payload(survey, attempt, answers)
        member_code = str(payload["MemberCode"])
        member_summary = {
            "member_id": str(payload["MemberCode"]),
            "birth_date": str(payload["BirthDate"]),
        }
        profile_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        # Toluna explicitly rejects near-simultaneous calls for the same
        # MemberCode.  A cache single-flight lock serializes one respondent
        # across all Gunicorn/Celery processes without holding a database
        # transaction open while the upstream HTTP request is running.
        identity = hashlib.sha256(
            f"{self.integration.pk}:{member_code}".encode("utf-8")
        ).hexdigest()
        lock_key = f"toluna:member-lock:{identity}"
        throttle_key = f"toluna:member-throttle:{identity}"
        lock_token = secrets.token_hex(16)
        deadline = time.monotonic() + 2.0
        while not cache.add(lock_key, lock_token, timeout=45):
            if time.monotonic() >= deadline:
                raise ProviderError(
                    "This Toluna member profile is already being synchronized. Please submit again shortly."
                )
            time.sleep(0.05)
        try:
            member, _ = TolunaMember.objects.get_or_create(
                integration=self.integration,
                member_code=member_code,
                defaults={
                    "culture_code": str(
                        (survey.raw_data.get("_toluna") or {}).get("culture_code") or ""
                    )
                },
            )
            if member.is_registered and member.profile_hash == profile_hash:
                return member_summary

            # The marker starts when a Toluna member request starts.  Waiting
            # for it to expire enforces the documented one-second minimum
            # between calls for the same MemberCode, including a POST->PUT
            # retry after an upstream conflict.
            while cache.get(throttle_key):
                time.sleep(0.05)
            cache.set(throttle_key, True, timeout=2)

            url = f"{self.member_base_url}/IntegratedPanelService/api/Respondent"
            method = "PUT" if member.is_registered else "POST"
            try:
                _, status_code = self._request(
                    method,
                    url,
                    expected=(200, 201, 409),
                    allow_empty=(method == "PUT"),
                    headers={"Accept": "application/json;version=2.0", "Content-Type": "application/json"},
                    json=payload,
                )
                if status_code == 409:
                    while cache.get(throttle_key):
                        time.sleep(0.05)
                    cache.set(throttle_key, True, timeout=2)
                    self._request(
                        "PUT",
                        url,
                        expected=(200,),
                        allow_empty=True,
                        headers={"Accept": "application/json;version=2.0", "Content-Type": "application/json"},
                        json=payload,
                    )
            except ProviderError as exc:
                member.last_error = str(exc)
                member.save(update_fields=["last_error", "updated_at"])
                raise
            member.profile_hash = profile_hash
            member.is_registered = True
            member.last_synced_at = timezone.now()
            member.last_error = ""
            member.save(update_fields=[
                "profile_hash", "is_registered", "last_synced_at", "last_error", "updated_at"
            ])
            return member_summary
        finally:
            # Do not delete a newer owner's lock if this request ever outlives
            # the safety timeout and another process has already acquired it.
            if cache.get(lock_key) == lock_token:
                cache.delete(lock_key)

    def build_outbound_url(self, survey, attempt, answers):
        member_code = effective_profile_uid(attempt)
        if not member_code:
            raise ProviderError("The Toluna member identity is missing.")
        quota = self._matching_quota(survey, answers)
        # Register/update the member before requesting an invite. Keep this
        # summary process-local for diagnostics; it is never rendered in the
        # public redirect flow.
        self.last_member_summary = self._register_member(survey, attempt, answers)
        culture = str((survey.raw_data.get("_toluna") or {}).get("culture_code") or "").replace("-", "_")
        panel_guid = environment_value(
            (self.integration.credential_env_keys or {}).get(f"panel_{culture}"),
            f"Toluna {culture.replace('_', '-')} PanelGUID",
        )
        try:
            invite, _ = self._request(
                "GET",
                f"{self.es_base_url}/IPExternalSamplingService/ExternalSample/{panel_guid}/{member_code}/Invite/{quota.quota_id}",
                headers=self.api_headers,
            )
        except TolunaAPIError as exc:
            local_status = TOLUNA_INVITE_RESULT_STATUS.get(exc.result_code)
            if not local_status:
                # Configuration errors and Toluna technical failures remain
                # retryable operational errors instead of being miscredited as
                # respondent outcomes.
                raise
            raise TolunaInviteRejected(
                TOLUNA_INVITE_STATUS_MESSAGES[local_status],
                status_code=local_status,
                result_code=exc.result_code,
                result=exc.result,
            ) from exc
        if not isinstance(invite, dict) or not _pick(invite, "URL"):
            raise ProviderError("Toluna did not return a survey invite URL.")
        expected_survey = _integer(_pick(survey.raw_data, "SurveyID"), -1)
        expected_wave = _integer(_pick(survey.raw_data, "WaveID"), -1)
        expected_quota = _integer(quota.quota_id, -1)
        if (
            _integer(_pick(invite, "SurveyID", "SurveyId"), -2) != expected_survey
            or _integer(_pick(invite, "WaveID"), -2) != expected_wave
            or _integer(_pick(invite, "QuotaID", "QuotaId"), -2) != expected_quota
        ):
            raise ProviderError("Toluna invite did not match the selected survey, wave, and quota.")
        invite_url = str(_pick(invite, "URL")).strip()
        parts = urlsplit(invite_url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            raise ProviderError("Toluna returned an invalid survey invite URL.")
        existing_rid = ""
        for segment in parts.query.split("&"):
            raw_key, separator, raw_value = segment.partition("=")
            if separator and unquote_plus(raw_key).casefold() == "rid":
                existing_rid = raw_value
                break
        if existing_rid and unquote_plus(existing_rid) != attempt.rid:
            raise ProviderError("Toluna invite contained an unexpected respondent RID.")
        if existing_rid:
            outbound_url = invite_url
        else:
            # The provider query is opaque and may be signed. Preserve its
            # exact byte representation and append only our alphanumeric RID;
            # parsing/re-encoding can silently change spaces, ordering or
            # duplicate parameters and invalidate an otherwise valid invite.
            base_url, fragment_marker, fragment = invite_url.partition("#")
            separator = "&" if "?" in base_url else "?"
            outbound_url = f"{base_url}{separator}rid={attempt.rid}"
            if fragment_marker:
                outbound_url = f"{outbound_url}#{fragment}"
        attempt.source_cpi_snapshot = _decimal(_pick(invite, "PartnerAmount")) or attempt.source_cpi_snapshot
        attempt.payable_cpi_snapshot = attempt.source_cpi_snapshot
        attempt.cpi_snapshot_source = "toluna_invite"
        attempt.upstream_transaction_data = {
            **(attempt.upstream_transaction_data or {}),
            "toluna_invite": {
                "survey_id": _pick(invite, "SurveyID", "SurveyId"),
                "wave_id": _pick(invite, "WaveID"),
                "quota_id": _pick(invite, "QuotaID"),
                "partner_amount": str(_pick(invite, "PartnerAmount", default="")),
                "member_amount": str(_pick(invite, "MemberAmount", default="")),
                "loi": _pick(invite, "LOI"),
                "ir": _pick(invite, "IR"),
            },
        }
        return outbound_url

    def verify_callback(self, request):
        config = self.integration.config or {}
        if not bool(config.get("callback_hash_required", True)):
            return False
        supplied = str(request.GET.get("hash") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", supplied):
            raise ProviderError("Toluna callback hash is missing or invalid.")
        hmac_key = environment_value(
            (self.integration.credential_env_keys or {}).get("hmac_key"),
            "Toluna callback HMAC key",
        )
        parts = urlsplit(request.build_absolute_uri())
        # Toluna signs the exact redirect URL before appending the hash. Keep
        # the original query encoding and ordering instead of parsing and
        # re-encoding it (which could change %20 to '+' and invalidate a valid
        # signature).
        unsigned_segments = []
        for segment in parts.query.split("&"):
            raw_key = segment.partition("=")[0]
            if unquote_plus(raw_key).lower() != "hash":
                unsigned_segments.append(segment)
        unsigned_query = "&".join(segment for segment in unsigned_segments if segment)
        unsigned_url = urlunsplit((parts.scheme, parts.netloc, parts.path, unsigned_query, "")) + "&"
        calculated = hmac.new(hmac_key.encode("utf-8"), unsigned_url.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, calculated):
            raise ProviderError("Toluna callback hash verification failed.")
        return True
