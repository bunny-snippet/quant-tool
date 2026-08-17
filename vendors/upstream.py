"""Safe, allow-listed upstream API explorer used by the internal Swagger UI."""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

from surveys.integrations import InnovateMRAPIError, InnovateMRClient
from surveys.providers import ProviderError, get_provider

from .credentials import resolve_integration_token


INNOVATE_DOCS = "https://developer.innovatemr.com"
RFG_DOCS = "https://docs.researchforgood.com/RFGAPI/livealert/apidocs"
TOLUNA_DOCS = "https://docs.integratedpanel.toluna.com"
OPERATION_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
SECRET_FIELD_NAMES = frozenset({
    "authorization", "api_key", "apikey", "access_token", "secret", "signature", "hash",
})


class UpstreamExplorerError(RuntimeError):
    """A safe error that can be returned to an authenticated administrator."""


@dataclass(frozen=True)
class OperationSpec:
    code: str
    label: str
    description: str
    endpoint: str
    documentation_url: str
    required_parameters: tuple[str, ...] = ()
    query_parameters: tuple[str, ...] = ()
    body_parameters: tuple[str, ...] = ()
    upstream_method: str = "GET"
    mutating: bool = False
    response_description: str = "The provider's authenticated JSON response."


INNOVATE_OPERATIONS = {
    "inventory": OperationSpec(
        "inventory", "Allocated surveys", "All currently allocated live surveys.",
        "@inventory", f"{INNOVATE_DOCS}/get-allocated-surveys-all-live-surveys-21242389e0",
    ),
    "paged_inventory": OperationSpec(
        "paged_inventory", "Paged allocated surveys", "One upstream inventory page.",
        "@paged_inventory", f"{INNOVATE_DOCS}/get-allocated-surveys-with-pagination-live-surveys-only-21242392e0",
        query_parameters=("page_size", "cursor"),
    ),
    "survey": OperationSpec(
        "survey", "Survey by ID", "Live survey metadata for one survey ID.",
        "/supply/getAllocatedSurveysBySurveyId/{survey_id}", f"{INNOVATE_DOCS}/get-allocated-surveys-by-id-21242390e0",
        required_parameters=("survey_id",),
    ),
    "inventory_by_date": OperationSpec(
        "inventory_by_date", "Allocated surveys by date", "Live inventory changed since a provider date/time.",
        "/supply/getAllocatedSurveysByDate/{date_time}", f"{INNOVATE_DOCS}/get-allocated-surveys-by-date-live-surveys-only-21242391e0",
        required_parameters=("date_time",),
    ),
    "closed_by_date": OperationSpec(
        "closed_by_date", "Closed surveys by date", "Allocated surveys closed since a provider date/time.",
        "/supply/getClosedSurveyListByDate/{date_time}", f"{INNOVATE_DOCS}/get-closed-survey-list-allocated-to-supplier-only-21242396e0",
        required_parameters=("date_time",),
    ),
    "high_priority": OperationSpec(
        "high_priority", "High-priority surveys", "Allocated live high-priority inventory.",
        "/supply/getHighPrioritySurveys", f"{INNOVATE_DOCS}/get-allocated-high-priority-surveysall-live-surveys-21242415e0",
        query_parameters=("countryCode", "languageCode"),
    ),
    "quota": OperationSpec(
        "quota", "Survey quota", "Quota rows for one survey.",
        "@quota", f"{INNOVATE_DOCS}/get-quota-for-survey-21242417e0", required_parameters=("survey_id",),
    ),
    "targeting": OperationSpec(
        "targeting", "Survey targeting", "Pre-screening targeting questions and accepted values.",
        "@targeting", f"{INNOVATE_DOCS}/get-survey-targeting-21242416e0", required_parameters=("survey_id",),
    ),
    "transactions_by_pid": OperationSpec(
        "transactions_by_pid", "Transaction by survey and PID", "One respondent transaction lookup.",
        "@transaction", f"{INNOVATE_DOCS}/get-survey-transactions-data-by-pid-and-survey-number-21242406e0",
        required_parameters=("survey_id", "pid"),
    ),
    "transactions": OperationSpec(
        "transactions", "Survey transactions", "Transactions for one survey.",
        "/supply/getSurveyTransactions/{survey_id}", f"{INNOVATE_DOCS}/get-survey-transactions-data-21242397e0",
        required_parameters=("survey_id",), query_parameters=("startDate", "endDate", "status"),
    ),
    "transactions_by_date": OperationSpec(
        "transactions_by_date", "Transactions by date range", "Transactions across surveys in a date range.",
        "/supply/getSurveyTransactionsByDateRange", f"{INNOVATE_DOCS}/get-survey-transactions-data-by-date-range-21242398e0",
        query_parameters=("startDate", "endDate", "verifiedStartDate", "verifiedEndDate", "status"),
    ),
    "survey_status": OperationSpec(
        "survey_status", "Survey availability", "Current availability for one survey.",
        "/supply/getSurveyStatus/{survey_id}", f"{INNOVATE_DOCS}/survey-availability-endpoint-21242402e0",
        required_parameters=("survey_id",),
    ),
    "stats": OperationSpec(
        "stats", "Survey statistics", "Real-time traffic and revenue statistics for one survey.",
        "/supply/getSurveyStats/{survey_id}", f"{INNOVATE_DOCS}/get-survey-stats-21242408e0",
        required_parameters=("survey_id",),
    ),
    "stats_by_date": OperationSpec(
        "stats_by_date", "Statistics by date range", "Traffic and revenue statistics across a date range.",
        "/supply/getSurveyStatsByDateRange", f"{INNOVATE_DOCS}/get-survey-stats-data-by-date-range-21242403e0",
        query_parameters=("startDate", "endDate", "verifiedStartDate", "verifiedEndDate"),
    ),
    "redirect_method": OperationSpec(
        "redirect_method", "Survey redirect configuration", "Effective redirect URLs/pixels for one survey.",
        "/supply/surveySpecificRedirects/{survey_id}", f"{INNOVATE_DOCS}/get-redirect-method-for-survey-21242393e0",
        required_parameters=("survey_id",),
    ),
    "set_global_redirects": OperationSpec(
        "set_global_redirects", "Set global redirects", "Creates or replaces account-level success, failure, quota, terminate, postback and verification URLs.",
        "/supply/accountUrls", f"{INNOVATE_DOCS}/set-global-redirect-urls-and-pixels-for-suppliers-21242385e0",
        body_parameters=("payload",), upstream_method="PUT", mutating=True,
        response_description="Success status and InnovateMR confirmation message.",
    ),
    "delete_global_redirects": OperationSpec(
        "delete_global_redirects", "Delete global redirects", "Removes selected account-level redirect URLs or pixels while leaving unspecified values unchanged.",
        "/supply/accountUrls", f"{INNOVATE_DOCS}/delete-global-redirect-urls-and-pixels-for-suppliers-account-21242386e0",
        body_parameters=("payload",), upstream_method="DELETE", mutating=True,
        response_description="Success status and InnovateMR confirmation message.",
    ),
    "set_survey_redirects": OperationSpec(
        "set_survey_redirects", "Set survey redirects", "Creates or replaces redirect URLs and postbacks for one survey only.",
        "/supply/setRedirectionForSurvey/{survey_id}", f"{INNOVATE_DOCS}/set-redirect-method-for-survey-21242394e0",
        required_parameters=("survey_id",), body_parameters=("payload",),
        upstream_method="PUT", mutating=True,
        response_description="Success status and InnovateMR confirmation message.",
    ),
    "delete_survey_redirects": OperationSpec(
        "delete_survey_redirects", "Delete survey redirects", "Removes survey-level redirect overrides so account-level redirects can apply.",
        "/supply/surveySpecificRedirects/{survey_id}", f"{INNOVATE_DOCS}/delete-redirect-method-for-survey-21242395e0",
        required_parameters=("survey_id",), upstream_method="DELETE", mutating=True,
        response_description="Success status and InnovateMR confirmation message.",
    ),
    "panelist_profile": OperationSpec(
        "panelist_profile", "Panelist profile", "Stored InnovateMR qualifications for one PID.",
        "/respondent/getQualifications/{pid}", f"{INNOVATE_DOCS}/get-panellist-profiling-21242401e0",
        required_parameters=("pid",),
    ),
    "create_panelist_profile": OperationSpec(
        "create_panelist_profile", "Create panelist profile", "Stores country, language and qualification answers against one supplier PID.",
        "/respondent/setQualifications/{pid}", f"{INNOVATE_DOCS}/set-panellist-profiling-21242409e0",
        required_parameters=("pid",), body_parameters=("payload",),
        upstream_method="POST", mutating=True,
        response_description="Created/success status and provider message.",
    ),
    "update_panelist_profile": OperationSpec(
        "update_panelist_profile", "Update panelist profile", "Updates country, language and qualification answers for an existing supplier PID.",
        "/respondent/setQualifications/{pid}", f"{INNOVATE_DOCS}/update-panellist-profiling-21242410e0",
        required_parameters=("pid",), body_parameters=("payload",),
        upstream_method="PUT", mutating=True,
        response_description="Success status and provider message.",
    ),
    "question_library": OperationSpec(
        "question_library", "Question library by market", "All targeting questions for one country/language market.",
        "/supply/getQuestionsByCountryAndLanguage/{country}/{language}", f"{INNOVATE_DOCS}/lookup-question-library-21242384e0",
        required_parameters=("country", "language"),
    ),
    "core_metadata": OperationSpec(
        "core_metadata", "Core metadata", "Country, language, status, type and other requested platform mappings.",
        "/mapping/metadata/{metadata_fields}", f"{INNOVATE_DOCS}/core-metadata-fields-21242387e0",
        required_parameters=("metadata_fields",),
    ),
    "unique_ip_check": OperationSpec(
        "unique_ip_check", "Unique IP check", "Non-mutating provider eligibility check for a survey and IP.",
        "/supply/checkAllowUniqueIP", f"{INNOVATE_DOCS}/survey-allow-for-unique-ip-21242399e0",
        required_parameters=("survey_id", "ip"), body_parameters=("survey_id", "ip"), upstream_method="POST",
    ),
    "unique_pid_ip_check": OperationSpec(
        "unique_pid_ip_check", "Unique PID and IP check", "Non-mutating provider eligibility check for survey, PID and provider ID.",
        "/supply/allowUniquePIDAndIP", f"{INNOVATE_DOCS}/allow-unique-pid-ip-21242400e0",
        required_parameters=("survey_id", "pid", "external_id"),
        body_parameters=("survey_id", "pid", "external_id"), upstream_method="POST",
    ),
    "respondent_precheck": OperationSpec(
        "respondent_precheck", "Respondent pre-survey check", "Provider eligibility/disqualification check before redirect.",
        "/supply/respondentPreSurveyCheck", f"{INNOVATE_DOCS}/respondent-pre-survey-check-21242412e0",
        required_parameters=("survey_id", "pid", "ip", "device_type"),
        body_parameters=("survey_id", "pid", "ip", "device_type"), upstream_method="POST",
    ),
    "respondent_surveys": OperationSpec(
        "respondent_surveys", "Surveys for respondent", "Personalized inventory for a profiled respondent.",
        "/supply/respondent/{pid}/surveys", f"{INNOVATE_DOCS}/get-surveys-for-respondent-21242411e0",
        required_parameters=("pid", "ip", "device_type"), query_parameters=("num_surveys",),
        body_parameters=("ip", "device_type", "num_surveys"), upstream_method="POST",
    ),
    "recontact_pids": OperationSpec(
        "recontact_pids", "Recontact PIDs", "Included/excluded PIDs for one recontact survey.",
        "/supply/getPidsForRecontactSurvey/{survey_id}", f"{INNOVATE_DOCS}/get-pids-for-re-contact-surveys-studies-21242407e0",
        required_parameters=("survey_id",),
    ),
    "question_categories": OperationSpec(
        "question_categories", "Question categories", "Available targeting question categories.",
        "/supply/getQuestionsCategories", f"{INNOVATE_DOCS}/get-question-categories-21242388e0",
    ),
    "questions_by_category": OperationSpec(
        "questions_by_category", "Questions by category", "Question library rows in one category.",
        "/supply/getQuestionsByCategory/{category_key}", f"{INNOVATE_DOCS}/get-questions-by-category-21242404e0",
        required_parameters=("category_key",),
    ),
    "answers_by_question": OperationSpec(
        "answers_by_question", "Answers by question", "Answer options for one question key and market.",
        "/supply/getAnswersByQuesKey/{question_key}", f"{INNOVATE_DOCS}/answer-lookup-21242405e0",
        required_parameters=("question_key",), query_parameters=("country", "language"),
    ),
    "term_reasons": OperationSpec(
        "term_reasons", "Termination reason categories", "All provider termination categories.",
        "/resources/termReasonCategories", f"{INNOVATE_DOCS}/term-reason-category-21242414e0",
    ),
    "term_reason": OperationSpec(
        "term_reason", "Termination reason by code", "One provider termination category.",
        "/resources/termReasonCategories/{term_code}", f"{INNOVATE_DOCS}/single-term-reason-category-code-21242413e0",
        required_parameters=("term_code",),
    ),
}


RFG_OPERATIONS = {
    "test": OperationSpec(
        "test", "Connection test", "Signed echo request used to validate APID and secret.",
        "test/copy/1", f"{RFG_DOCS}/commands/test.html", upstream_method="POST",
    ),
    "inventory": OperationSpec(
        "inventory", "LiveAlert inventory", "Current Research For Good project inventory.",
        "livealert/inventory/1", f"{RFG_DOCS}/commands/inventory.html",
        query_parameters=("country", "category", "allow_recontacts", "inventory_type"), upstream_method="POST",
    ),
    "targeting": OperationSpec(
        "targeting", "Project targeting", "Targeting datapoints and quotas for one RFG project.",
        "livealert/targeting/1", f"{RFG_DOCS}/commands/targeting.html",
        required_parameters=("survey_id",), query_parameters=("zips_only",), upstream_method="POST",
    ),
    "quota": OperationSpec(
        "quota", "Project quotas", "Quotas extracted from the documented targeting response.",
        "livealert/targeting/1", f"{RFG_DOCS}/commands/targeting.html",
        required_parameters=("survey_id",), upstream_method="POST",
    ),
    "datapoints": OperationSpec(
        "datapoints", "List datapoints", "Available RFG profiling datapoints.",
        "livealert/listDatapoints/1", f"{RFG_DOCS}/commands/listDatapoints.html",
        query_parameters=("country", "modified_since"), upstream_method="POST",
    ),
    "datapoint": OperationSpec(
        "datapoint", "Datapoint details", "Question and answer metadata for one datapoint.",
        "livealert/datapoint/1", f"{RFG_DOCS}/commands/datapoint.html",
        required_parameters=("datapoint_name",), upstream_method="POST",
    ),
    "create_link": OperationSpec(
        "create_link", "Create live project link", "Returns the provider entry link for one project.",
        "livealert/createLink/1", f"{RFG_DOCS}/commands/createLink.html",
        required_parameters=("survey_id",), upstream_method="POST",
    ),
    "duplicate_check": OperationSpec(
        "duplicate_check", "Duplicate check", "Checks respondent eligibility using RFG duplicate rules.",
        "livealert/duplicateCheck/1", f"{RFG_DOCS}/commands/duplicateCheck.html",
        required_parameters=("survey_id", "rid", "ip"), query_parameters=("fingerprint",), upstream_method="POST",
    ),
    "duplicate_checks": OperationSpec(
        "duplicate_checks", "Bulk duplicate checks", "Checks one respondent against multiple RFG projects in one signed request.",
        "livealert/duplicateChecks/1", f"{RFG_DOCS}/commands/duplicateChecks.html",
        required_parameters=("survey_ids", "rid", "ip"), query_parameters=("fingerprint",), upstream_method="POST",
        response_description="A projects array containing each RFG project ID and its duplicate decision.",
    ),
    "log": OperationSpec(
        "log", "Project respondent log", "Returns RFG session/result log rows for one project and optional result/date filters.",
        "livealert/log/1", f"{RFG_DOCS}/commands/log.html",
        required_parameters=("survey_id",), query_parameters=("result", "start", "end"), upstream_method="POST",
        response_description="Session keys with start, end and result fields.",
    ),
    "stats": OperationSpec(
        "stats", "Project statistics", "Current statistics for one RFG project.",
        "livealert/stats/1", f"{RFG_DOCS}/commands/stats.html",
        required_parameters=("survey_id",), upstream_method="POST",
    ),
    "zip_to_geo": OperationSpec(
        "zip_to_geo", "Postal code to geography", "Converts a country/postal-code pair into RFG geographic region indexes.",
        "livealert/zipToGeo/1", f"{RFG_DOCS}/commands/zipToGeo.html",
        required_parameters=("zip", "country_code"), upstream_method="POST",
        response_description="Country and geographic region indexes resolved by RFG.",
    ),
}


TOLUNA_OPERATIONS = {
    "connection_test": OperationSpec(
        "connection_test",
        "Connection and panel test",
        "Validates Toluna API/Reference credentials, configured cultures, panel GUIDs and panel settings.",
        "@toluna_test",
        f"{TOLUNA_DOCS}/externalsample/api/getquotas.html",
        response_description="Authentication state, configured cultures, reference-question count and settings-test result.",
    ),
    "cultures": OperationSpec(
        "cultures",
        "Reference cultures",
        "Returns Toluna's supported culture names and CultureIDs.",
        "/IPUtilityService/ReferenceData/Cultures",
        f"{TOLUNA_DOCS}/mapping/referencedataapi/cultures.html",
        response_description="Toluna culture rows used to map EN-US style configuration to numeric CultureIDs.",
    ),
    "reference_questions": OperationSpec(
        "reference_questions",
        "Question and answer library",
        "Returns Toluna profiling questions and answer IDs for one to five configured cultures.",
        "/IPUtilityService/ReferenceData/QuestionsAndAnswersData",
        f"{TOLUNA_DOCS}/mapping/referencedataapi/questionsandanswers.html",
        query_parameters=("cultures",),
        upstream_method="POST",
        response_description="Translated Toluna question definitions, answer IDs, answer labels and routing metadata.",
    ),
    "settings": OperationSpec(
        "settings",
        "Panel settings",
        "Returns the Integrated Panel settings for one configured culture panel.",
        "/IntegratedPanelService/api/Settings",
        f"{TOLUNA_DOCS}/externalsample/api/getsettings.html",
        required_parameters=("culture",),
        response_description="Toluna panel settings for the configured culture's PanelGUID.",
    ),
    "quotas": OperationSpec(
        "quotas",
        "Live surveys and quotas",
        "Returns live External Sample surveys, waves, quotas and targeting for one configured culture.",
        "/IPExternalSamplingService/ExternalSample/{PanelGUID}/Quotas",
        f"{TOLUNA_DOCS}/externalsample/api/getquotas.html",
        required_parameters=("culture",),
        query_parameters=("include_routables",),
        response_description="Country/cache metadata plus live surveys, waves, CPI, IR, LOI, quota capacity and targeting layers.",
    ),
}


INNOVATE_RESPONSE_DESCRIPTIONS = {
    "inventory": "Live survey rows allocated to this supplier account, including survey ID, market, CPI, IR, LOI, status and entry-link fields supplied by InnovateMR.",
    "paged_inventory": "One page of allocated live surveys plus the provider's next-page/cursor information.",
    "survey": "The current metadata record for the requested InnovateMR Survey ID.",
    "inventory_by_date": "Live survey records created or changed since the supplied provider date/time.",
    "closed_by_date": "Allocated survey IDs closed since the supplied provider date/time.",
    "high_priority": "High-priority live survey rows, optionally narrowed by country and language.",
    "quota": "Quota groups/rows for the survey, including targets, completes, remaining capacity, status and targeting conditions.",
    "targeting": "Pre-screening questions, question types and accepted answer/option IDs for the survey.",
    "transactions_by_pid": "The respondent transaction matching the supplied Survey ID and PID, including provider status and termination fields when available.",
    "transactions": "Respondent transactions for one survey, optionally filtered by date/status.",
    "transactions_by_date": "Respondent transactions across allocated surveys for the requested date/status window.",
    "survey_status": "The survey's current availability/status decision.",
    "stats": "Traffic, completion and revenue/statistics fields for one survey.",
    "stats_by_date": "Traffic, completion and revenue/statistics fields for the requested date window.",
    "redirect_method": "The effective survey-level redirect URL, pixel and postback configuration.",
    "panelist_profile": "Country, language and stored qualification answers for the supplied PID.",
    "question_library": "Targeting-question definitions available for the requested country/language market.",
    "core_metadata": "Requested InnovateMR mapping dictionaries such as countries, languages, statuses or survey types.",
    "unique_ip_check": "An allow/deny decision indicating whether the IP can enter the survey.",
    "unique_pid_ip_check": "An allow/deny decision for the supplied survey, PID, IP-associated provider ID combination.",
    "respondent_precheck": "The provider's pre-survey eligibility/status decision and reason fields.",
    "respondent_surveys": "Personalized survey inventory matching the supplied profiled PID, IP and device.",
    "recontact_pids": "Included/excluded PID collections for the requested recontact survey.",
    "question_categories": "All targeting question category keys and labels.",
    "questions_by_category": "Question definitions belonging to the requested category.",
    "answers_by_question": "Answer option IDs/text for the requested question and optional market.",
    "term_reasons": "All InnovateMR termination codes, categories and descriptions.",
    "term_reason": "The termination category/description for one provider term code.",
}

RFG_RESPONSE_DESCRIPTIONS = {
    "test": "Authentication status and confirmation that RFG returned the signed echo marker.",
    "inventory": "RFG project rows including rfg_id, title, country, CPI, estimated IR/LOI, desired/current completes, state, category and targeting hints.",
    "targeting": "Targeting datapoints and quota definitions for the requested RFG project.",
    "quota": "Only the quota array extracted from RFG's targeting response.",
    "datapoints": "Names of RFG profiling datapoints available for the requested market/date.",
    "datapoint": "The datapoint property, question translations, answer choices, modes/types and supported countries.",
    "create_link": "RFG's reusable project entry link. Respondent RID, country, postal code, gender, birthday and datapoint values are added later for a live attempt.",
    "duplicate_check": "An isDuplicate decision for one respondent/project combination.",
    "duplicate_checks": "A projects array with an isDuplicate decision for each supplied RFG project.",
    "log": "RFG session log rows containing sesskey, start, end and result.",
    "stats": "Project starts, completes, terminates, quotas, CR, EPC, project/trailing CR and EPC.",
    "zip_to_geo": "RFG country/geography region indexes resolved from the postal code.",
}


def operation_response_description(provider: str, spec: OperationSpec) -> str:
    provider = re.sub(r"[-_]", "", str(provider or "").lower())
    if provider == "rfg":
        mapping = RFG_RESPONSE_DESCRIPTIONS
    elif provider == "toluna":
        mapping = {}
    else:
        mapping = INNOVATE_RESPONSE_DESCRIPTIONS
    return mapping.get(spec.code, spec.response_description)


def _provider_key(integration) -> str:
    return str(integration.provider_code or "").lower().replace("-", "").replace("_", "")


def _custom_operations(integration) -> dict[str, OperationSpec]:
    configured = (integration.config or {}).get("read_api_operations") or []
    if not isinstance(configured, list):
        return {}
    operations: dict[str, OperationSpec] = {}
    base = urlsplit(integration.base_url)
    for item in configured:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip().lower()
        endpoint = str(item.get("endpoint") or "").strip()
        if not OPERATION_CODE_RE.fullmatch(code) or not endpoint:
            continue
        parsed = urlsplit(endpoint)
        if parsed.scheme and (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
            continue
        required = tuple(str(value) for value in item.get("required_parameters") or [] if str(value))
        query = tuple(str(value) for value in item.get("query_parameters") or [] if str(value))
        operations[code] = OperationSpec(
            code=code,
            label=str(item.get("label") or code.replace("_", " ").title()),
            description=str(item.get("description") or "Configured read-only provider operation."),
            endpoint=endpoint,
            documentation_url=str(item.get("documentation_url") or ""),
            required_parameters=required,
            query_parameters=query,
        )
    return operations


def operation_specs(integration) -> dict[str, OperationSpec]:
    provider = _provider_key(integration)
    if provider == "rfg":
        return dict(RFG_OPERATIONS)
    if provider == "toluna":
        return dict(TOLUNA_OPERATIONS)
    if provider == "innovatemr":
        operations = dict(INNOVATE_OPERATIONS)
    else:
        operations = {
            "inventory": INNOVATE_OPERATIONS["inventory"],
            "paged_inventory": INNOVATE_OPERATIONS["paged_inventory"],
            "quota": INNOVATE_OPERATIONS["quota"],
            "targeting": INNOVATE_OPERATIONS["targeting"],
            "transactions_by_pid": INNOVATE_OPERATIONS["transactions_by_pid"],
        }
    operations.update(_custom_operations(integration))
    return operations


def credential_metadata(integration) -> dict[str, Any]:
    provider = _provider_key(integration)
    if provider in {"rfg", "toluna"}:
        references = integration.credential_env_keys or {}
        env_names = [str(value) for value in references.values() if value]
        configured = bool(env_names) and all(bool(os.getenv(name, "")) for name in env_names)
        return {
            "source": "environment",
            "environment_variables": env_names,
            "configured": configured,
            "authentication": (
                "RFG signed HMAC request (APID/time/hash generated server-side)"
                if provider == "rfg"
                else "Toluna API_AUTH_KEY / PARTNER_AUTH_KEY headers injected server-side"
            ),
        }
    try:
        configured = bool(resolve_integration_token(integration))
    except ValueError:
        configured = False
    source = "encrypted database credential" if integration.encrypted_api_token else "environment"
    env_names = [integration.credential_env_key] if integration.credential_env_key else []
    return {
        "source": source,
        "environment_variables": env_names,
        "configured": configured,
        "authentication": f"{integration.auth_header_name or 'provider header'} (injected server-side)",
    }


def _configured_endpoint(integration, spec: OperationSpec) -> str:
    if spec.endpoint == "@inventory":
        return integration.inventory_endpoint or (
            "/supply/getAllocatedSurveys" if _provider_key(integration) == "innovatemr" else ""
        )
    if spec.endpoint == "@paged_inventory":
        return integration.paged_inventory_endpoint or (
            "/supply/getAllocatedSurveysPaged" if _provider_key(integration) == "innovatemr" else ""
        )
    if spec.endpoint == "@quota":
        return integration.quota_endpoint_template or (
            "/supply/getQuotaForSurvey/{survey_id}" if _provider_key(integration) == "innovatemr" else ""
        )
    if spec.endpoint == "@targeting":
        return integration.targeting_endpoint_template or (
            "/supply/getSurveyTargeting/{survey_id}" if _provider_key(integration) == "innovatemr" else ""
        )
    if spec.endpoint == "@transaction":
        return integration.transaction_endpoint_template or (
            "/supply/getSurveyTransactionsByCond/{survey_id}/{pid}" if _provider_key(integration) == "innovatemr" else ""
        )
    return spec.endpoint


def _effective_url(integration, spec: OperationSpec) -> str:
    if _provider_key(integration) == "rfg":
        return f"{integration.base_url.rstrip('/')} (command: {spec.endpoint})"
    if _provider_key(integration) == "toluna":
        config = integration.config or {}
        environment = str(config.get("environment") or "production").lower()
        es_base_url = str(
            config.get("external_sample_base_url")
            or ("https://training.ups.toluna.com" if environment == "sandbox" else integration.base_url)
            or "https://tws.toluna.com"
        ).rstrip("/")
        reference_base_url = str(
            config.get("reference_base_url") or "https://tws.toluna.com"
        ).rstrip("/")
        if spec.code == "connection_test":
            return "Toluna Reference Data + Integrated Panel settings"
        if spec.code in {"cultures", "reference_questions"}:
            return f"{reference_base_url}{spec.endpoint}"
        return f"{es_base_url}{spec.endpoint}"
    endpoint = _configured_endpoint(integration, spec)
    if not endpoint:
        return "Not configured"
    return InnovateMRClient(integration=integration).endpoint_url(endpoint)


def integration_metadata(integration) -> dict[str, Any]:
    provider = _provider_key(integration)
    aliases = {integration.client.code, re.sub(r"[^a-z0-9]+", "-", integration.client.name.lower()).strip("-")}
    if provider == "innovatemr":
        aliases.update({"innovate", "innovatemr", "innovate-mr"})
    elif provider == "rfg":
        aliases.update({"rfg", "research-for-good"})
    elif provider == "toluna":
        aliases.update({"toluna", "toluna-integrated-panel"})
    return {
        "client_code": integration.client.code,
        "lookup_aliases": sorted(alias for alias in aliases if alias),
        "client": integration.client.name,
        "integration": integration.name,
        "provider": integration.provider_code,
        "base_url": integration.base_url,
        "active": integration.is_active,
        "credential": credential_metadata(integration),
        "operations": [
            {
                "code": spec.code,
                "label": spec.label,
                "description": spec.description,
                "upstream_method": spec.upstream_method,
                "api_url": _effective_url(integration, spec),
                "documentation_url": spec.documentation_url,
                "required_parameters": list(spec.required_parameters),
                "query_parameters": list(spec.query_parameters),
                "body_parameters": list(spec.body_parameters),
                "mutating": spec.mutating,
                "response_description": operation_response_description(provider, spec),
            }
            for spec in operation_specs(integration).values()
        ],
    }


def _required_values(spec: OperationSpec, parameters: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    missing = []
    for name in spec.required_parameters:
        value = str(parameters.get(name) or "").strip()
        if not value:
            missing.append(name)
        elif len(value) > 240:
            raise UpstreamExplorerError(f"Parameter '{name}' is too long.")
        else:
            values[name] = value
    if missing:
        raise UpstreamExplorerError(f"Missing required parameter(s): {', '.join(missing)}.")
    return values


def _redact(value: Any, credential_values: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if str(key).lower() in SECRET_FIELD_NAMES else _redact(item, credential_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, credential_values) for item in value]
    if isinstance(value, str) and value in credential_values:
        return "[REDACTED]"
    return value


def _limit_payload(payload: Any, limit: int) -> tuple[Any, int | None, bool]:
    if isinstance(payload, list):
        return payload[:limit], len(payload), len(payload) > limit
    if not isinstance(payload, dict):
        return payload, None, False
    limited = deepcopy(payload)
    for key in ("result", "projects", "Surveys", "items"):
        rows = limited.get(key)
        if isinstance(rows, list):
            limited[key] = rows[:limit]
            return limited, len(rows), len(rows) > limit
    return limited, None, False


def _credential_values(integration) -> set[str]:
    values = set()
    if _provider_key(integration) in {"rfg", "toluna"}:
        for env_name in (integration.credential_env_keys or {}).values():
            if env_name and os.getenv(str(env_name), ""):
                values.add(os.getenv(str(env_name), ""))
    else:
        try:
            token = resolve_integration_token(integration)
        except ValueError:
            token = ""
        if token:
            values.add(token)
    return values


def _execute_rfg(integration, spec: OperationSpec, parameters: dict[str, Any]) -> Any:
    provider = get_provider(integration)
    required = _required_values(spec, parameters)
    if spec.code == "test":
        return provider.test_connection()
    command: dict[str, Any] = {}
    if spec.code == "inventory":
        config = integration.config or {}
        country = str(parameters.get("country") or config.get("country") or "").upper()
        category = str(parameters.get("category") or config.get("category") or "").upper()
        command = {
            "allowRecontacts": str(parameters.get("allow_recontacts") or "").lower() in {"1", "true", "yes"}
            if parameters.get("allow_recontacts") not in {None, ""}
            else bool(config.get("allow_recontacts", False)),
            "type": int(parameters.get("inventory_type") or 1),
        }
        if country:
            command["country"] = country
        if category in {"B2B", "B2C"}:
            command["category"] = category
    elif spec.code in {"targeting", "quota"}:
        command = {
            "rfg_id": required["survey_id"],
            "zipsOnly": str(parameters.get("zips_only") or "").lower() in {"1", "true", "yes"},
        }
    elif spec.code == "datapoints":
        if parameters.get("country"):
            command["country"] = str(parameters["country"]).upper()
        if parameters.get("modified_since"):
            command["modifiedSince"] = str(parameters["modified_since"])
    elif spec.code == "datapoint":
        command = {"name": required["datapoint_name"]}
    elif spec.code in {"create_link", "stats"}:
        command = {"rfg_id": required["survey_id"]}
    elif spec.code == "duplicate_check":
        command = {
            "rfg_id": required["survey_id"], "rid": required["rid"], "ip": required["ip"],
            "fingerprint": parameters.get("fingerprint") or 0,
        }
    elif spec.code == "duplicate_checks":
        raw_ids = parameters.get("survey_ids")
        if isinstance(raw_ids, list):
            survey_ids = raw_ids
        else:
            text = str(raw_ids or "").strip()
            if text.startswith("["):
                try:
                    survey_ids = json.loads(text)
                except ValueError as exc:
                    raise UpstreamExplorerError("survey_ids must be a JSON array or comma-separated IDs.") from exc
            else:
                survey_ids = [value.strip() for value in text.split(",") if value.strip()]
        if not isinstance(survey_ids, list) or not survey_ids or len(survey_ids) > 100:
            raise UpstreamExplorerError("survey_ids must contain between 1 and 100 project IDs.")
        command = {
            "rfg_ids": [str(value) for value in survey_ids],
            "rid": required["rid"],
            "ip": required["ip"],
            "fingerprint": parameters.get("fingerprint") or 0,
        }
    elif spec.code == "log":
        command = {"rfg_id": required["survey_id"]}
        for local_name in ("result", "start", "end"):
            if parameters.get(local_name) not in {None, ""}:
                command[local_name] = parameters[local_name]
    elif spec.code == "zip_to_geo":
        command = {"zip": required["zip"], "countryCode": required["country_code"].upper()}
    result = provider.explorer_read(spec.endpoint, **command)
    if spec.code == "quota":
        return {"quotas": result.get("quotas") or [], "source": "livealert/targeting/1"}
    return result


def _execute_toluna(integration, spec: OperationSpec, parameters: dict[str, Any]) -> Any:
    """Execute the documented read-only Toluna operations with server-side secrets."""
    provider = get_provider(integration)
    required = _required_values(spec, parameters)
    if spec.code == "connection_test":
        return provider.test_connection()
    if spec.code == "cultures":
        payload, _ = provider._request(
            "GET",
            f"{provider.reference_base_url}/IPUtilityService/ReferenceData/Cultures",
            headers=provider.reference_headers,
        )
        return payload

    configured_panels = dict(provider._panels())
    if spec.code == "reference_questions":
        cultures = [
            value.strip().lower().replace("_", "-")
            for value in str(parameters.get("cultures") or "").split(",")
            if value.strip()
        ] or list(configured_panels)
        cultures = list(dict.fromkeys(cultures))
        if len(cultures) > 5:
            raise UpstreamExplorerError("Toluna accepts a maximum of five cultures per Reference Data request.")
        unsupported = [value for value in cultures if value not in configured_panels]
        if unsupported:
            raise UpstreamExplorerError(
                f"Culture(s) not configured for this integration: {', '.join(unsupported)}."
            )
        culture_rows = provider._cultures()
        missing = [value for value in cultures if value not in culture_rows]
        if missing:
            raise UpstreamExplorerError(f"Toluna did not return culture(s): {', '.join(missing)}.")
        payload, _ = provider._request(
            "POST",
            f"{provider.reference_base_url}/IPUtilityService/ReferenceData/QuestionsAndAnswersData",
            headers=provider.reference_headers,
            json={
                "CultureIDs": [int(culture_rows[value]["CultureID"]) for value in cultures],
                "CategoryIDs": [],
                "IncludeComputed": False,
                "IncludeRoutables": True,
                "IncludeDemographics": True,
            },
        )
        return payload

    culture = required["culture"].lower().replace("_", "-")
    panel_guid = configured_panels.get(culture)
    if not panel_guid:
        raise UpstreamExplorerError(f"Culture '{culture}' is not configured for this integration.")
    if spec.code == "settings":
        payload, _ = provider._request(
            "GET",
            f"{provider.es_base_url}/IntegratedPanelService/api/Settings",
            headers=provider.api_headers,
            params={"partnerGUID": panel_guid},
        )
        return payload
    include_routables = str(parameters.get("include_routables") or "true").lower() in {
        "1", "true", "yes", "on",
    }
    payload, _ = provider._request(
        "GET",
        f"{provider.es_base_url}/IPExternalSamplingService/ExternalSample/{quote(panel_guid, safe='')}/Quotas",
        headers=provider.api_headers,
        params={"includeRoutables": str(include_routables).lower()},
    )
    return payload


def _execute_rest(integration, spec: OperationSpec, parameters: dict[str, Any]) -> Any:
    client = InnovateMRClient(integration=integration)
    required = _required_values(spec, parameters)
    endpoint = _configured_endpoint(integration, spec)
    if not endpoint:
        raise UpstreamExplorerError(f"Operation '{spec.code}' is not configured for this integration.")
    for name, value in required.items():
        endpoint = endpoint.replace("{" + name + "}", quote(value, safe=""))
    unresolved = re.findall(r"\{([^{}]+)\}", endpoint)
    if unresolved:
        raise UpstreamExplorerError(f"Missing endpoint value(s): {', '.join(unresolved)}.")
    query = {
        name: str(parameters[name]).strip()
        for name in spec.query_parameters
        if parameters.get(name) not in {None, ""}
    }
    if spec.code == "paged_inventory":
        if "page_size" in query:
            query["limit"] = query.pop("page_size")
        if "cursor" in query:
            query["next"] = query.pop("cursor")
    if spec.mutating:
        confirmed = parameters.get("confirm_upstream_mutation") is True
        if not confirmed:
            raise UpstreamExplorerError(
                "This changes live provider data. Set confirm_upstream_mutation=true in the request body."
            )
        body = parameters.get("payload") or {}
        if not isinstance(body, dict):
            raise UpstreamExplorerError("payload must be a JSON object.")
        return client.write_json(spec.upstream_method, endpoint, body)
    if spec.upstream_method == "POST":
        if spec.code == "unique_ip_check":
            body = {"ip": required["ip"], "survNum": required["survey_id"]}
        elif spec.code == "unique_pid_ip_check":
            body = {
                "id": required["external_id"], "pid": required["pid"],
                "survNum": required["survey_id"],
            }
        elif spec.code == "respondent_precheck":
            body = {
                "pid": required["pid"], "ip": required["ip"],
                "survNum": required["survey_id"], "deviceType": required["device_type"],
            }
        elif spec.code == "respondent_surveys":
            try:
                num_surveys = int(parameters.get("num_surveys") or 10)
            except (TypeError, ValueError) as exc:
                raise UpstreamExplorerError("num_surveys must be a whole number.") from exc
            body = {
                "ip": required["ip"], "numSurveys": max(1, min(num_surveys, 100)),
                "deviceType": required["device_type"],
            }
        else:
            raise UpstreamExplorerError("This configured POST operation is not allow-listed.")
        return client.post_json(endpoint, body)
    return client.request_json(endpoint, params=query or None)


def execute_operation(integration, operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
    specs = operation_specs(integration)
    spec = specs.get(operation)
    if spec is None:
        raise UpstreamExplorerError(
            f"Unsupported operation '{operation}'. Available: {', '.join(sorted(specs)) or 'none'}."
        )
    try:
        limit = max(1, min(int(parameters.get("limit") or 50), 200))
    except (TypeError, ValueError) as exc:
        raise UpstreamExplorerError("Limit must be a whole number from 1 to 200.") from exc
    try:
        provider = _provider_key(integration)
        if provider == "rfg":
            payload = _execute_rfg(integration, spec, parameters)
        elif provider == "toluna":
            payload = _execute_toluna(integration, spec, parameters)
        else:
            payload = _execute_rest(integration, spec, parameters)
    except (ProviderError, InnovateMRAPIError, ValueError) as exc:
        raise UpstreamExplorerError(str(exc)) from exc
    payload = _redact(payload, _credential_values(integration))
    payload, total_count, truncated = _limit_payload(payload, limit)
    return {
        "integration": {
            "client_code": integration.client.code,
            "client": integration.client.name,
            "name": integration.name,
            "provider": integration.provider_code,
        },
        "operation": {
            "code": spec.code,
            "label": spec.label,
            "upstream_method": spec.upstream_method,
            "api_url": _effective_url(integration, spec),
            "documentation_url": spec.documentation_url,
            "description": spec.description,
            "required_parameters": list(spec.required_parameters),
            "query_parameters": list(spec.query_parameters),
            "body_parameters": list(spec.body_parameters),
            "mutating": spec.mutating,
            "response_description": operation_response_description(integration.provider_code, spec),
        },
        "credential": credential_metadata(integration),
        "result": payload,
        "total_rows_in_response": total_count,
        "response_truncated": truncated,
        "response_limit": limit,
    }
