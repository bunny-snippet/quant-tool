import csv
import ipaddress
import json
import logging
import re
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import quote, urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import DatabaseError, transaction
from django.db.models import Count, IntegerField, Max, Min, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse, HttpResponseRedirect, StreamingHttpResponse
from django.core.paginator import Paginator
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.access import (
    HasFunctionPermission,
    activity_visible_user_ids,
    effective_permission_codes,
    function_permission_required,
    has_function_access,
)
from vendors.services import (
    AllocationUnavailable,
    annotate_survey_pricing_for_user,
    finalize_attempt_capacity,
    reserve_attempt_capacity,
    resolve_vendor_survey_context,
    organization_client_ids_for_user,
    scope_surveys_for_api_key,
    scope_surveys_for_user,
)
from vendors.access import is_external_vendor_scope, vendor_scope_user_id
from vendors.credentials import decrypt_secret
from vendors.models import ClientIntegration, VendorAPIKey
from vendors.security import decode_delivery_token, sign_supplier_callback

from .filters import SurveyAttemptFilter, SurveyFilter
from .dashboard import (
    build_dashboard_payload,
    dashboard_attempts,
    dashboard_client_options,
    dashboard_range_window,
)
from .excel import ExcelSheet, build_excel_response
from .integrations import InnovateMRAPIError, InnovateMRClient
from .innovatemr_callbacks import verify_callback_request
from .models import Survey, SurveyAttempt, SyncRun, TolunaNotification
from .outcomes import describe_toluna_callback, provider_outcome
from .report_pricing import (
    apply_percentage,
    can_view_report_commercials,
    role_visibility_percent,
    supplier_cpi_for_admin,
    supplier_label_for_admin,
    viewer_attempt_cpi,
)
from .serializers import (
    SurveyDetailSerializer,
    DashboardResponseSerializer,
    SurveyListSerializer,
    SurveyAttemptSerializer,
    SurveyAttemptListResponseSerializer,
    SurveyQuotaSerializer,
    RFGCallbackResponseSerializer,
    SyncRunSerializer,
    SyncTriggerResponseSerializer,
    TargetingQuestionSerializer,
    UserHitsResponseSerializer,
)
from .status_context import verified_toluna_notification_summary
from .pagination import SurveyPagination
from prescreener_vault.services import (
    PrescreenerVaultError,
    answers_with_entry_postal_code,
    capture_prescreener_submission,
    operational_answer_value,
    wrong_target_country_answers,
)
from prescreener_vault.models import PrescreenerSubmission
from prescreener_vault.reuse import maybe_assign_reusable_profile
from .age_rules import OPEN_ENDED_AGE_MAX, normalize_age_range
from .providers import ProviderError, TolunaInviteRejected, get_provider
from .providers.rfg import RFG_TARGETING_ADAPTER_VERSION
from .providers.toluna import TOLUNA_ADAPTER_VERSION
from .geolocation import (
    geolocation_client_data,
    is_wrong_target_country,
    resolve_entry_geolocation,
    survey_target_country_code,
)
from .rfg_outcomes import RFG_STATUS_MAP, describe_rfg_outcome
from .rfg_text import clean_rfg_display_text
from .services import reconcile_attempt_status, replace_survey_quotas, replace_survey_targeting, sync_surveys
from .survey_flow import (
    backfill_attempt_entry_audit,
    build_biobrain_outbound_url,
    build_outbound_url,
    claim_project_entry_ip,
    create_attempt,
    ensure_attempt_prescreener_uid,
    get_request_client_data,
    get_request_ip,
    status_rid_from_request,
)
from .tasks import sync_innovatemr_surveys_task
from .user_hits import aggregate_user_hits, user_hit_filter_options


logger = logging.getLogger(__name__)


class UpstreamUnavailable(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "InnovateMR is temporarily unavailable and no cached survey detail exists."
    default_code = "upstream_unavailable"


PROJECT_COLUMN_PERMISSIONS = {
    "project_id": "projects.column.project_id", "survey": "projects.column.survey",
    "market": "projects.column.market", "completes": "projects.column.completes",
    "cpi": "projects.column.cpi", "loi_ir": "projects.column.loi_ir",
    "entry_link": "projects.column.entry_link", "modified": "projects.column.modified",
    "actions": "projects.column.actions",
}

PROJECT_FILTER_PERMISSIONS = {
    "search": "projects.filter.search", "country": "projects.filter.country",
    "status": "projects.filter.status", "client": "projects.filter.client",
    "buyer": "projects.filter.buyer", "survey_type": "projects.filter.survey_type",
    "cpi": "projects.filter.cpi", "date": "projects.filter.date",
    "clear": "projects.filters.clear",
}

STUDY_COLUMN_PERMISSIONS = {
    "project_id": "studies.column.project_id", "survey_id": "studies.column.survey_id",
    "country": "studies.column.country", "cpi": "studies.column.cpi",
    "respondent_id": "studies.column.respondent_id", "user": "studies.column.user",
    "device": "studies.column.device", "ip": "studies.column.ip", "loi": "studies.column.loi",
    "status": "studies.column.status", "start": "studies.column.start", "end": "studies.column.end",
}

STUDY_FILTER_PERMISSIONS = {
    "search": "studies.filter.search", "branch": "studies.filter.branch",
    "sub_branch": "studies.filter.sub_branch", "shift": "studies.filter.shift", "user": "studies.filter.user",
    "status": "studies.filter.status", "country": "studies.filter.country",
    "client": "studies.filter.client", "buyer": "studies.filter.buyer",
    "project": "studies.filter.project", "date": "studies.filter.date",
    "clear": "studies.filters.clear",
}

DASHBOARD_FILTER_PERMISSIONS = {
    "client": "dashboard.filter.client", "country": "dashboard.filter.country",
    "branch": "dashboard.filter.branch", "sub_branch": "dashboard.filter.sub_branch",
    "shift": "dashboard.filter.shift", "user": "dashboard.filter.user",
    "date": "dashboard.filter.date", "clear": "dashboard.filters.clear",
}

DASHBOARD_CARD_PERMISSIONS = {
    "hits": "dashboard.card.hits", "completes": "dashboard.card.completes",
    "conversion_rate": "dashboard.card.conversion", "active_users": "dashboard.card.active_users",
    "average_loi_seconds": "dashboard.card.average_loi", "revenue": "dashboard.card.revenue",
    "average_cpi": "dashboard.card.average_cpi", "rpc": "dashboard.card.rpc",
    "incidence_rate": "dashboard.card.ir",
}

DASHBOARD_CHART_PERMISSIONS = {
    "performance": "dashboard.chart.performance", "client_share": "dashboard.chart.client_share",
    "status": "dashboard.chart.status", "device": "dashboard.chart.device",
    "top_users": "dashboard.chart.top_users",
}

DASHBOARD_GRAPH_FILTER_PERMISSIONS = {
    "traffic": "dashboard.graph.traffic_filters",
    "finance": "dashboard.graph.finance_filters",
}

STUDY_CARD_PERMISSIONS = {
    "total": "studies.card.total", "initiated": "studies.card.initiated",
    "completed": "studies.card.completed", "terminated": "studies.card.terminated",
    "quota": "studies.card.quota", "security": "studies.card.security",
    "conversion": "studies.card.conversion", "desktop": "studies.card.desktop",
    "mobile": "studies.card.mobile", "tablet": "studies.card.tablet",
    "revenue": "studies.card.revenue",
    "ir": "studies.card.ir",
}

USER_HIT_COLUMN_PERMISSIONS = {
    "branch": "user_hits.column.branch", "sub_branch": "user_hits.column.sub_branch", "shift": "user_hits.column.shift",
    "user": "user_hits.column.user", "date": "user_hits.column.date",
    "hits": "user_hits.column.hits", "completes": "user_hits.column.completes",
}

USER_HIT_FILTER_PERMISSIONS = {
    "search": "user_hits.filter.search", "branch": "user_hits.filter.branch",
    "sub_branch": "user_hits.filter.sub_branch", "shift": "user_hits.filter.shift", "user": "user_hits.filter.user",
    "date": "user_hits.filter.date", "clear": "user_hits.filters.clear",
}

USER_HIT_CARD_PERMISSIONS = {
    "total_hits": "user_hits.card.total_hits", "completes": "user_hits.card.completes",
    "conversion": "user_hits.card.conversion", "active_users": "user_hits.card.active_users",
    "devices": "user_hits.card.devices", "ir": "user_hits.card.ir",
}

TERM_REASON_FIELD_PERMISSIONS = {
    "status": "termination_reasons.field.status",
    "reason": "termination_reasons.field.reason",
    "respondent": "termination_reasons.field.respondent",
    "survey": "termination_reasons.field.survey",
    "timing": "termination_reasons.field.timing",
    "audit": "termination_reasons.field.audit",
}

TERM_REASON_COLUMN_PERMISSIONS = {
    "rid": "termination_reasons.column.rid",
    "survey": "termination_reasons.column.survey",
    "client": "termination_reasons.column.client",
    "respondent": "termination_reasons.column.respondent",
    "status": "termination_reasons.column.status",
    "ended": "termination_reasons.column.ended",
    "actions": "termination_reasons.column.actions",
}

TERM_REASON_FILTER_PERMISSIONS = {
    "rid": "termination_reasons.filter.rid",
    "branch": "termination_reasons.filter.branch",
    "sub_branch": "termination_reasons.filter.sub_branch",
    "shift": "termination_reasons.filter.shift",
    "user": "termination_reasons.filter.user",
    "status": "termination_reasons.filter.status",
    "country": "termination_reasons.filter.country",
    "client": "termination_reasons.filter.client",
    "provider": "termination_reasons.filter.provider",
    "buyer": "termination_reasons.filter.buyer",
    "date": "termination_reasons.filter.date",
    "clear": "termination_reasons.filters.clear",
}

TOLUNA_NOTIFICATION_TABS = (
    (TolunaNotification.EventType.MEMBER_COMPLETE, "Member completion", "Completed respondents"),
    (TolunaNotification.EventType.MEMBER_TERMINATE, "Member termination", "Standard termination reasons"),
    (TolunaNotification.EventType.ENHANCED_TERMINATION, "Enhanced termination", "Quality and rejection details"),
    (TolunaNotification.EventType.QUOTA_STATUS, "Quota status", "Open and full quota changes"),
    (TolunaNotification.EventType.SURVEY_CLOSED, "Survey closed", "Closed survey notifications"),
    (TolunaNotification.EventType.RECONCILIATION, "Reconciliation", "Post-fieldwork adjustments"),
)

TERM_REASON_CARD_PERMISSIONS = {
    "total": "termination_reasons.card.total",
    "terminated": "termination_reasons.card.terminated",
    "quota": "termination_reasons.card.quota",
    "quality": "termination_reasons.card.quality",
}

PRESCREENER_DATA_FILTER_PERMISSIONS = {
    "search": "prescreener_data.filter.search",
    "country": "prescreener_data.filter.country",
    "language": "prescreener_data.filter.language",
    "age_group": "prescreener_data.filter.age_group",
    "gender": "prescreener_data.filter.gender",
    "clear": "prescreener_data.filters.clear",
}

PRESCREENER_DATA_COLUMN_PERMISSIONS = {
    "uid": "prescreener_data.column.uid",
    "market": "prescreener_data.column.market",
    "profile": "prescreener_data.column.profile",
    "captured": "prescreener_data.column.captured",
    "usage_count": "prescreener_data.column.usage_count",
    "answers": "prescreener_data.column.answers",
}

UNSUCCESSFUL_STATUS_LABELS = {
    SurveyAttempt.Status.TERMINATED: "Terminated",
    SurveyAttempt.Status.OVER_QUOTA: "Quota full",
    SurveyAttempt.Status.QUALITY_TERMINATED: "Quality / security",
    SurveyAttempt.Status.SURVEY_NOT_AVAILABLE: "Survey not available",
    SurveyAttempt.Status.NO_SURVEYS: "No surveys",
    SurveyAttempt.Status.NO_COOKIES: "No cookies",
    SurveyAttempt.Status.MAX_SURVEYS_REACHED: "Maximum surveys reached",
    SurveyAttempt.Status.NOT_QUALIFIED: "Not qualified",
    SurveyAttempt.Status.SURVEY_TAKEN: "Survey already taken",
}
UNSUCCESSFUL_ATTEMPT_STATUSES = set(UNSUCCESSFUL_STATUS_LABELS)


def _project_columns_for_user(user):
    codes = effective_permission_codes(user)
    columns = [name for name, code in PROJECT_COLUMN_PERMISSIONS.items() if code in codes]
    if "entry_link" in columns and "survey_links.copy" not in codes:
        columns.remove("entry_link")
    if "actions" in columns and "survey_details.view" not in codes:
        columns.remove("actions")
    return columns


def _component_access(codes, permissions):
    return {name: code in codes for name, code in permissions.items()}


def _permitted_columns(codes, permissions):
    return [name for name, code in permissions.items() if code in codes]


def _enforce_query_permissions(request, permission_parameters):
    for code, parameters in permission_parameters.items():
        if any(request.query_params.get(parameter) not in {None, ""} for parameter in parameters):
            if not has_function_access(request.user, code):
                raise PermissionDenied(f"Your account cannot use the {code} filter.")


@function_permission_required("dashboard.view")
def dashboard_page(request):
    codes = effective_permission_codes(request.user)
    return render(request, "surveys/dashboard.html", {
        "active_page": "dashboard",
        "dashboard_cards": _permitted_columns(codes, DASHBOARD_CARD_PERMISSIONS),
        "dashboard_charts": _permitted_columns(codes, DASHBOARD_CHART_PERMISSIONS),
        "dashboard_graph_filters": _permitted_columns(
            codes, DASHBOARD_GRAPH_FILTER_PERMISSIONS
        ),
    })


@function_permission_required("projects.view")
def projects_page(request):
    codes = effective_permission_codes(request.user)
    visible_surveys = scope_surveys_for_user(Survey.objects.all(), request.user)
    countries = visible_surveys.exclude(country_code="").values_list("country_code", "country").distinct().order_by("country_code")
    is_client_scoped_panel = bool(
        vendor_scope_user_id(request.user)
        or organization_client_ids_for_user(request.user) is not None
    )
    if is_client_scoped_panel:
        companies = visible_surveys.filter(client__isnull=False).values_list("client__name", flat=True).distinct().order_by("client__name")
    else:
        companies = visible_surveys.exclude(company_name="").values_list("company_name", flat=True).distinct().order_by("company_name")
    survey_types = list(
        visible_surveys.exclude(survey_type="").values_list("survey_type", flat=True).distinct().order_by("survey_type")
    )
    project_columns = _project_columns_for_user(request.user)
    project_filters = _component_access(codes, PROJECT_FILTER_PERMISSIONS)
    can_sort_cpi = project_filters["cpi"]
    cpi_min, cpi_max = 0, 100
    if can_sort_cpi:
        # Keep the exact viewer-visible slider bounds. The pricing expression is
        # isolated to this one aggregate so it no longer widens every country,
        # client and survey-type metadata query on the page.
        cpi_queryset = annotate_survey_pricing_for_user(
            visible_surveys, request.user
        )
        cpi_bounds = cpi_queryset.aggregate(
            minimum=Min("visible_cpi"),
            maximum=Max("visible_cpi"),
        )
        cpi_min = cpi_bounds["minimum"] or 0
        cpi_max = cpi_bounds["maximum"] or 100
        if cpi_max <= cpi_min:
            cpi_max = cpi_min + 1
    return render(request, "surveys/projects.html", {
        "active_page": "projects", "countries": countries, "companies": companies,
        # Buyer IDs are intentionally loaded only when the filter is opened.
        # Large inventories can contain tens of thousands of distinct values;
        # embedding them made the browser parse megabytes before projects.js
        # could issue the first survey-list request.
        "buyer_options": [], "survey_types": survey_types,
        "company_filter_label": "Client",
        "company_filter_param": "client_name" if is_client_scoped_panel else "company",
        "company_filter_default": "All clients",
        "project_columns": project_columns, "project_column_count": max(1, len(project_columns)),
        "can_view_project_client_name": "projects.column.client_name" in codes,
        "project_filters": project_filters,
        "can_sync": "sync.run" in codes,
        "can_export_projects": "projects.export" in codes,
        "can_change_project_page_size": "projects.control.page_size" in codes,
        "can_paginate_projects": "projects.control.pagination" in codes,
        "can_open_project_studies": "attempts.view" in codes and "studies.filter.project" in codes,
        "can_sort_cpi": can_sort_cpi, "cpi_min_bound": cpi_min, "cpi_max_bound": cpi_max,
    })


@function_permission_required("attempts.view")
def studies_page(request):
    codes = effective_permission_codes(request.user)
    user_ids = activity_visible_user_ids(request.user)
    hierarchy_options = user_hit_filter_options(request.user)
    visible_attempts = SurveyAttempt.objects.all()
    if not request.user.is_superuser:
        visible_attempts = visible_attempts.filter(platform_user_id__in=user_ids)
    visible_surveys = scope_surveys_for_user(Survey.objects.all(), request.user)
    countries = list(
        visible_surveys.exclude(country_code="")
        .values("country_code", "country")
        .distinct().order_by("country_code")
    )
    study_clients = list(
        visible_attempts.filter(survey__client__isnull=False)
        .values("survey__client_id", "survey__client__name")
        .distinct().order_by("survey__client__name")
    )
    study_buyers = list(
        visible_attempts.exclude(survey__buyer_id="")
        .values("survey__buyer_id", "survey__client_id")
        .distinct().order_by("survey__buyer_id")
    )
    return render(request, "surveys/studies.html", {
        "active_page": "studies",
        "tracked_users": hierarchy_options["users"],
        "study_branches": hierarchy_options["branches"],
        "study_sub_branches": hierarchy_options["sub_branches"],
        "study_shifts": hierarchy_options["shifts"],
        "study_countries": countries,
        "study_clients": study_clients,
        "study_buyers": study_buyers,
        "attempt_statuses": [
            ("initiated,redirected", "Initiated"),
            (SurveyAttempt.Status.COMPLETED, "Completed"),
            (SurveyAttempt.Status.TERMINATED, "Terminated"),
            (SurveyAttempt.Status.OVER_QUOTA, "Over quota"),
            (SurveyAttempt.Status.QUALITY_TERMINATED, "Quality terminated"),
            (SurveyAttempt.Status.SURVEY_NOT_AVAILABLE, "Survey not available"),
            (SurveyAttempt.Status.NO_SURVEYS, "No surveys"),
            (SurveyAttempt.Status.NO_COOKIES, "No cookies"),
            (SurveyAttempt.Status.MAX_SURVEYS_REACHED, "Maximum surveys reached"),
            (SurveyAttempt.Status.NOT_QUALIFIED, "Not qualified"),
            (SurveyAttempt.Status.SURVEY_TAKEN, "Survey already taken"),
        ],
        "study_filters": _component_access(codes, STUDY_FILTER_PERMISSIONS),
        "study_columns": _permitted_columns(codes, STUDY_COLUMN_PERMISSIONS),
        "study_column_count": max(1, len(_permitted_columns(codes, STUDY_COLUMN_PERMISSIONS))),
        "study_cards": _permitted_columns(codes, STUDY_CARD_PERMISSIONS),
        "can_export": "attempts.export" in codes,
        "can_change_study_page_size": "studies.control.page_size" in codes,
        "can_paginate_studies": "studies.control.pagination" in codes,
    })


@function_permission_required("user_hits.view")
def user_hits_page(request):
    codes = effective_permission_codes(request.user)
    return render(request, "surveys/user_hits.html", {
        "active_page": "user-hits",
        "hit_filters": _component_access(codes, USER_HIT_FILTER_PERMISSIONS),
        "hit_columns": _permitted_columns(codes, USER_HIT_COLUMN_PERMISSIONS),
        "hit_column_count": max(1, len(_permitted_columns(codes, USER_HIT_COLUMN_PERMISSIONS))),
        "hit_cards": _permitted_columns(codes, USER_HIT_CARD_PERMISSIONS),
        "can_change_hit_page_size": "user_hits.control.page_size" in codes,
        "can_paginate_hits": "user_hits.control.pagination" in codes,
        **user_hit_filter_options(request.user),
    })


@function_permission_required("prescreener_data.view")
def prescreener_data_page(request):
    """Read-only, permission-scoped Panelist Data browser for the isolated vault."""

    codes = effective_permission_codes(request.user)
    filters_access = _component_access(codes, PRESCREENER_DATA_FILTER_PERMISSIONS)
    columns = _permitted_columns(codes, PRESCREENER_DATA_COLUMN_PERMISSIONS)
    selected = {
        "search": request.GET.get("search", "").strip(),
        "country": request.GET.get("country", "").strip(),
        "language": request.GET.get("language", "").strip(),
        "age_group": request.GET.get("age_group", "").strip(),
        "gender": request.GET.get("gender", "").strip(),
    }
    for name, value in selected.items():
        if value and not filters_access[name]:
            raise PermissionDenied(f"Your account cannot use the {name.replace('_', ' ')} filter.")

    page_obj = None
    summary = {"total": 0, "countries": 0, "age_groups": 0, "genders": 0}
    options = {"countries": [], "languages": [], "age_groups": [], "genders": []}
    vault_error = ""
    if not getattr(settings, "PRESCREENER_VAULT_ENABLED", False):
        vault_error = "The pre-screener vault is not enabled on this environment."
    else:
        try:
            base = PrescreenerSubmission.objects.using("prescreener_vault").all()
            options = {
                "countries": list(base.exclude(country_code="").values("country_code", "country").distinct().order_by("country_code")),
                "languages": list(base.exclude(language_code="").values("language_code", "language").distinct().order_by("language_code")),
                "age_groups": list(base.exclude(respondent_age_group="").values_list("respondent_age_group", flat=True).distinct().order_by("respondent_age_group")),
                "genders": list(base.exclude(respondent_gender="").values_list("respondent_gender", flat=True).distinct().order_by("respondent_gender")),
            }
            queryset = base.prefetch_related("question_answers")
            if selected["search"]:
                queryset = queryset.filter(uid__icontains=selected["search"])
            if selected["country"]:
                queryset = queryset.filter(country_code__iexact=selected["country"])
            if selected["language"]:
                queryset = queryset.filter(language_code__iexact=selected["language"])
            if selected["age_group"]:
                queryset = queryset.filter(respondent_age_group__iexact=selected["age_group"])
            if selected["gender"]:
                queryset = queryset.filter(respondent_gender__iexact=selected["gender"])
            summary = queryset.aggregate(
                total=Count("uid"),
                countries=Count("country_code", distinct=True),
                age_groups=Count("respondent_age_group", distinct=True),
                genders=Count("respondent_gender", distinct=True),
            )
            page_obj = Paginator(queryset.order_by("-submitted_at"), 20).get_page(request.GET.get("page", 1))
        except (DatabaseError, PrescreenerVaultError) as exc:
            logger.exception("Unable to read the pre-screener vault")
            vault_error = f"Vault data is temporarily unavailable: {exc}"

    query_without_page = request.GET.copy()
    query_without_page.pop("page", None)
    return render(request, "surveys/prescreened_data.html", {
        "active_page": "prescreened-data",
        "vault_error": vault_error,
        "page_obj": page_obj,
        "summary": summary,
        "options": options,
        "selected": selected,
        "vault_filters": filters_access,
        "vault_columns": columns,
        "vault_column_count": max(1, len(columns)),
        "can_export_vault": "prescreener_data.export" in codes,
        "can_paginate_vault": "prescreener_data.control.pagination" in codes,
        "page_query": query_without_page.urlencode(),
    })


@function_permission_required("prescreener_data.export")
def prescreener_data_export(request):
    """Export the filtered vault as analysis-friendly submission and answer sheets."""

    if not getattr(settings, "PRESCREENER_VAULT_ENABLED", False):
        return HttpResponse("The pre-screener vault is not enabled.", status=503)

    codes = effective_permission_codes(request.user)
    filters_access = _component_access(codes, PRESCREENER_DATA_FILTER_PERMISSIONS)
    selected = {
        "search": request.GET.get("search", "").strip(),
        "country": request.GET.get("country", "").strip(),
        "language": request.GET.get("language", "").strip(),
        "age_group": request.GET.get("age_group", "").strip(),
        "gender": request.GET.get("gender", "").strip(),
    }
    for name, value in selected.items():
        if value and not filters_access[name]:
            raise PermissionDenied(f"Your account cannot use the {name.replace('_', ' ')} filter.")

    queryset = PrescreenerSubmission.objects.using("prescreener_vault").all()
    if selected["search"]:
        queryset = queryset.filter(uid__icontains=selected["search"])
    if selected["country"]:
        queryset = queryset.filter(country_code__iexact=selected["country"])
    if selected["language"]:
        queryset = queryset.filter(language_code__iexact=selected["language"])
    if selected["age_group"]:
        queryset = queryset.filter(respondent_age_group__iexact=selected["age_group"])
    if selected["gender"]:
        queryset = queryset.filter(respondent_gender__iexact=selected["gender"])
    queryset = queryset.prefetch_related("question_answers").order_by("-submitted_at")

    def submission_rows():
        for submission in queryset.iterator(chunk_size=500):
            yield [
                submission.uid, submission.country, submission.country_code,
                submission.language, submission.language_code, submission.respondent_age,
                submission.respondent_age_group, submission.respondent_gender,
                submission.respondent_ethnicity, submission.respondent_postal_code,
                submission.usage_count, _excel_datetime(submission.submitted_at),
                _excel_datetime(submission.captured_at),
            ]

    def answer_rows():
        for submission in queryset.iterator(chunk_size=250):
            for answer in submission.question_answers.all():
                yield [
                    submission.uid, answer.position, answer.question_id,
                    answer.question_key, answer.question_text, answer.question_type,
                    answer.question_category, answer.canonical_attribute,
                    ", ".join(str(value) for value in answer.answer_values),
                    ", ".join(str(value) for value in answer.answer_labels),
                    ", ".join(str(value) for value in answer.upstream_values),
                ]

    local_now = timezone.localtime()
    return build_excel_response(
        f"panelist-data-{local_now:%Y%m%d-%H%M%S}-IST.xlsx",
        [
            ExcelSheet(
                "Submissions",
                ["UID", "Country", "Country code", "Language", "Language code", "Age", "Age group", "Gender", "Ethnicity", "ZIP / postal code", "Visits", "Registered at (IST)", "Captured at (IST)"],
                submission_rows(),
                [22, 20, 13, 17, 14, 9, 13, 14, 24, 18, 13, 22, 22],
            ),
            ExcelSheet(
                "Answers",
                ["UID", "Position", "Question ID", "Question key", "Question", "Question type", "Category", "Reusable attribute", "Answer values", "Answer labels", "Upstream values"],
                answer_rows(),
                [22, 10, 16, 22, 48, 18, 18, 20, 28, 34, 25],
            ),
        ],
    )


def _refresh_provider_outcome(attempt, integration):
    """Fetch one provider transaction without coupling custom clients to Innovate status rules."""

    provider_code = (integration.provider_code if integration else "innovatemr").lower()
    client = InnovateMRClient(integration=integration)
    if provider_code == "innovatemr":
        reconcile_attempt_status(client, attempt)
        attempt.refresh_from_db()
        return

    survey_identifier = attempt.survey.source_id or attempt.survey.source_key
    transactions = client.get_survey_transactions_by_pid(survey_identifier, attempt.rid)
    if not transactions:
        attempt.upstream_checked_at = timezone.now()
        attempt.save(update_fields=["upstream_checked_at", "updated_at"])
        return

    respondent_keys = ("PID", "pid", "trackId", "rid", "RID", "respondentId")
    transaction_row = next(
        (
            row for row in transactions
            if any(str(row.get(key) or "") == attempt.rid for key in respondent_keys)
        ),
        transactions[0],
    )
    attempt.upstream_transaction_data = transaction_row
    attempt.upstream_checked_at = timezone.now()
    attempt.save(update_fields=["upstream_transaction_data", "upstream_checked_at", "updated_at"])


def _term_report_values(request, name):
    """Return stable, de-duplicated values from repeated or CSV query params."""
    values = []
    for raw_value in request.GET.getlist(name):
        for value in str(raw_value or "").split(","):
            value = value.strip()
            if value and value not in values:
                values.append(value)
    return values


def _term_report_filter_state(request, filters_access):
    selected = {
        "search": request.GET.get("search", "").strip(),
        "branch": _term_report_values(request, "branch"),
        "sub_branch": _term_report_values(request, "sub_branch"),
        "shift": _term_report_values(request, "shift"),
        "user": _term_report_values(request, "user"),
        "status": _term_report_values(request, "status"),
        "country": _term_report_values(request, "country"),
        "client": _term_report_values(request, "client"),
        "provider": _term_report_values(request, "provider"),
        "buyer_id": _term_report_values(request, "buyer_id"),
        "date_field": request.GET.get("date_field", "callback").strip() or "callback",
        "date_from": request.GET.get("date_from", "").strip(),
        "date_to": request.GET.get("date_to", "").strip(),
    }
    supplied_by_permission = {
        "rid": selected["search"], "branch": selected["branch"],
        "sub_branch": selected["sub_branch"], "shift": selected["shift"],
        "user": selected["user"], "status": selected["status"],
        "country": selected["country"], "client": selected["client"],
        "provider": selected["provider"],
        "buyer": selected["buyer_id"], "date": selected["date_from"] or selected["date_to"],
    }
    for filter_name, value in supplied_by_permission.items():
        if value and not filters_access.get(filter_name, False):
            raise PermissionDenied(f"Your account cannot use the {filter_name.replace('_', ' ')} filter.")
    if selected["date_field"] not in {"initiated", "callback"}:
        selected["date_field"] = "callback"
    return selected


def _term_report_base_queryset():
    return SurveyAttempt.objects.select_related(
        "survey__integration__client", "survey__client",
        "platform_user__employee_profile__organization_unit__parent__parent",
    ).filter(status__in=UNSUCCESSFUL_ATTEMPT_STATUSES)


def _term_report_datetime(value, label):
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        raise PermissionDenied(f"{label} must use a valid date and time.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _filtered_term_report_queryset(request, filters_access):
    selected = _term_report_filter_state(request, filters_access)
    queryset = _term_report_base_queryset()
    search = selected["search"]
    if search:
        queryset = queryset.filter(
            Q(rid__icontains=search)
            | Q(prescreener_uid__icontains=search) | Q(provider_profile_uid__icontains=search)
            | Q(survey__local_id__icontains=search) | Q(survey__source_key__icontains=search)
            | Q(survey__buyer_id__icontains=search) | Q(survey__client__name__icontains=search)
            | Q(platform_user__username__icontains=search) | Q(platform_user__first_name__icontains=search)
            | Q(platform_user__last_name__icontains=search) | Q(platform_user__email__icontains=search)
            | Q(initiation_ip__icontains=search) | Q(callback_ip__icontains=search)
        )
    filter_data = {
        name: ",".join(selected[name])
        for name in ("branch", "sub_branch", "shift", "user", "status", "country", "client", "buyer_id")
        if selected[name]
    }
    queryset = SurveyAttemptFilter(filter_data, queryset=queryset).qs
    if selected["provider"]:
        queryset = queryset.filter(survey__integration__provider_code__in=selected["provider"])
    lower = _term_report_datetime(selected["date_from"], "From date and time")
    upper = _term_report_datetime(selected["date_to"], "To date and time")
    if lower and upper and lower > upper:
        raise PermissionDenied("From date and time cannot be after To date and time.")
    date_column = "initiated_at" if selected["date_field"] == "initiated" else "callback_at"
    if lower:
        queryset = queryset.filter(**{f"{date_column}__gte": lower})
    if upper:
        queryset = queryset.filter(**{f"{date_column}__lte": upper})
    return queryset, selected


def _term_report_options(base_queryset, user):
    hierarchy = user_hit_filter_options(user)
    provider_labels = {
        "innovatemr": "InnovateMR",
        "rfg": "Research For Good",
        "cint": "Cint Exchange",
        "toluna": "Toluna",
        "biobrain": "BioBrain / Voqall",
        "custom": "Custom REST API",
    }
    provider_codes = list(
        ClientIntegration.objects.filter(is_active=True).exclude(provider_code="")
        .values_list("provider_code", flat=True)
        .distinct().order_by("provider_code")
    )
    return {
        **hierarchy,
        "providers": [
            {"value": code, "name": provider_labels.get(code, code.replace("-", " ").title())}
            for code in provider_codes
        ],
        "countries": list(base_queryset.exclude(survey__country_code="").values(
            "survey__country_code", "survey__country"
        ).distinct().order_by("survey__country_code")),
        "clients": list(base_queryset.filter(survey__client__isnull=False).values(
            "survey__client_id", "survey__client__name"
        ).distinct().order_by("survey__client__name")),
        "buyers": list(base_queryset.exclude(survey__buyer_id="").values(
            "survey__client_id", "survey__buyer_id"
        ).distinct().order_by("survey__buyer_id")),
    }


def _filtered_toluna_notifications(request, selected):
    """Apply the Term Reports filters to Toluna's operational notification audit."""

    queryset = TolunaNotification.objects.select_related(
        "integration__client", "survey__client", "attempt__platform_user",
    ).defer("raw_payload")
    search = selected["search"]
    if search:
        queryset = queryset.filter(
            Q(attempt__rid__icontains=search)
            | Q(unique_code__icontains=search)
            | Q(survey__local_id__icontains=search)
            | Q(survey__source_key__icontains=search)
            | Q(provider_survey_id__icontains=search)
            | Q(survey_ref__icontains=search)
            | Q(reason__icontains=search)
            | Q(rejection_name__icontains=search)
            | Q(attempt__platform_user__username__icontains=search)
            | Q(attempt__platform_user__email__icontains=search)
        )
    if selected["country"]:
        queryset = queryset.filter(survey__country_code__in=selected["country"])
    if selected["client"]:
        queryset = queryset.filter(survey__client_id__in=selected["client"])
    if selected["buyer_id"]:
        queryset = queryset.filter(survey__buyer_id__in=selected["buyer_id"])

    attempt_filters = {
        name: ",".join(selected[name])
        for name in ("branch", "sub_branch", "shift", "user", "status")
        if selected[name]
    }
    if attempt_filters:
        attempts = SurveyAttemptFilter(
            attempt_filters,
            queryset=SurveyAttempt.objects.filter(survey__integration__provider_code="toluna"),
        ).qs
        queryset = queryset.filter(attempt_id__in=attempts.values("id"))

    lower = _term_report_datetime(selected["date_from"], "From date and time")
    upper = _term_report_datetime(selected["date_to"], "To date and time")
    if lower and upper and lower > upper:
        raise PermissionDenied("From date and time cannot be after To date and time.")
    queryset = queryset.annotate(report_time=Coalesce("occurred_at", "received_at"))
    if lower:
        queryset = queryset.filter(report_time__gte=lower)
    if upper:
        queryset = queryset.filter(report_time__lte=upper)
    return queryset


@function_permission_required("termination_reasons.view")
def termination_reasons_page(request):
    codes = effective_permission_codes(request.user)
    filters_access = _component_access(codes, TERM_REASON_FILTER_PERMISSIONS)
    columns = _permitted_columns(codes, TERM_REASON_COLUMN_PERMISSIONS)
    queryset, selected = _filtered_term_report_queryset(request, filters_access)
    detail_rid = (request.GET.get("detail") or request.GET.get("rid") or "").strip()
    detail_attempt = None
    detail_outcome = None
    lookup_error = ""
    show_toluna_notifications = "toluna" in selected["provider"]
    toluna_event = (request.GET.get("toluna_event") or TolunaNotification.EventType.MEMBER_TERMINATE).strip()
    valid_toluna_events = {value for value, _label, _description in TOLUNA_NOTIFICATION_TABS}
    if toluna_event not in valid_toluna_events:
        toluna_event = TolunaNotification.EventType.MEMBER_TERMINATE
    toluna_page_obj = None
    toluna_tabs = []
    toluna_detail = None

    if detail_rid and "termination_reasons.action.details" not in codes:
        raise PermissionDenied("Your account cannot open outcome details.")

    base_queryset = _term_report_base_queryset()
    filter_options = _term_report_options(base_queryset, request.user)

    if show_toluna_notifications:
        toluna_queryset = _filtered_toluna_notifications(request, selected)
        toluna_counts = {
            row["event_type"]: row["total"]
            for row in toluna_queryset.values("event_type").annotate(total=Count("id"))
        }
        tab_params = request.GET.copy()
        for parameter in ("toluna_event", "toluna_page", "toluna_detail", "detail", "rid", "page"):
            tab_params.pop(parameter, None)
        for value, label, description in TOLUNA_NOTIFICATION_TABS:
            query = tab_params.copy()
            query["toluna_event"] = value
            toluna_tabs.append({
                "value": value,
                "label": label,
                "description": description,
                "count": toluna_counts.get(value, 0),
                "active": value == toluna_event,
                "query": query.urlencode(),
            })
        selected_notifications = toluna_queryset.filter(event_type=toluna_event).order_by(
            "-report_time", "-received_at"
        )
        toluna_page_obj = Paginator(selected_notifications, 20).get_page(
            request.GET.get("toluna_page", 1)
        )
        toluna_detail_id = (request.GET.get("toluna_detail") or "").strip()
        if toluna_detail_id:
            if "termination_reasons.action.details" not in codes:
                raise PermissionDenied("Your account cannot open notification details.")
            if toluna_detail_id.isdigit():
                toluna_detail = toluna_queryset.filter(pk=toluna_detail_id).first()

    summary = queryset.aggregate(
        total=Count("id"),
        terminated=Count("id", filter=Q(status=SurveyAttempt.Status.TERMINATED)),
        quota=Count("id", filter=Q(status=SurveyAttempt.Status.OVER_QUOTA)),
        quality=Count("id", filter=Q(status=SurveyAttempt.Status.QUALITY_TERMINATED)),
    )
    page_obj = Paginator(queryset.order_by("-callback_at", "-initiated_at"), 20).get_page(
        request.GET.get("page", 1)
    )
    for row in page_obj.object_list:
        row.reason_outcome = provider_outcome(row)
        row.reason_status_label = UNSUCCESSFUL_STATUS_LABELS.get(row.status, row.get_status_display())

    if detail_rid:
        if len(detail_rid) != 10 or not detail_rid.isalnum():
            lookup_error = "The requested RID must contain exactly 10 letters and numbers."
        else:
            detail_attempt = base_queryset.filter(rid=detail_rid).first()
            if detail_attempt is None:
                non_terminal_attempt = SurveyAttempt.objects.select_related(
                    "survey__integration__client", "survey__client", "platform_user"
                ).filter(rid=detail_rid).first()
                if non_terminal_attempt:
                    lookup_error = (
                        f"This RID is currently {non_terminal_attempt.get_status_display().lower()}; "
                        "provider outcome details become available after a final unsuccessful status."
                    )
        if not lookup_error and detail_attempt is None:
            lookup_error = "No survey attempt was found for this RID."
        elif detail_attempt:
            detail_attempt.reason_status_label = UNSUCCESSFUL_STATUS_LABELS.get(
                detail_attempt.status, detail_attempt.get_status_display()
            )
            detail_outcome = provider_outcome(detail_attempt)
            integration = detail_attempt.survey.integration if detail_attempt.survey.integration_id else None
            provider_code = (integration.provider_code if integration else "innovatemr").lower()
            supports_lookup = provider_code == "innovatemr" or bool(
                integration and integration.transaction_endpoint_template
            )
            if (
                supports_lookup
                and "termination_reasons.action.refresh" in codes
                and (not detail_outcome["status"] or not detail_outcome["reason"])
            ):
                try:
                    _refresh_provider_outcome(detail_attempt, integration)
                    detail_outcome = provider_outcome(detail_attempt)
                except (InnovateMRAPIError, ValueError) as exc:
                    provider_label = integration.client.name if integration else "InnovateMR"
                    lookup_error = (
                        f"The attempt was found, but {provider_label} could not return its detailed "
                        f"transaction yet: {exc}"
                    )

    link_params = request.GET.copy()
    for parameter in ("detail", "rid"):
        link_params.pop(parameter, None)
    detail_query = link_params.urlencode()
    page_params = link_params.copy()
    page_params.pop("page", None)
    page_query = page_params.urlencode()
    toluna_link_params = request.GET.copy()
    for parameter in ("toluna_detail", "detail", "rid"):
        toluna_link_params.pop(parameter, None)
    toluna_detail_query = toluna_link_params.urlencode()
    toluna_page_params = toluna_link_params.copy()
    toluna_page_params.pop("toluna_page", None)
    toluna_page_query = toluna_page_params.urlencode()

    return render(request, "surveys/termination_reasons.html", {
        "active_page": "termination-reasons",
        "selected": selected,
        "search_query": selected["search"],
        "client_options": filter_options["clients"],
        "term_reason_clients": filter_options["clients"],
        "term_branches": filter_options["branches"],
        "term_sub_branches": filter_options["sub_branches"],
        "term_shifts": filter_options["shifts"],
        "term_users": filter_options["users"],
        "term_countries": filter_options["countries"],
        "term_buyers": filter_options["buyers"],
        "term_providers": filter_options["providers"],
        "attempt_statuses": list(UNSUCCESSFUL_STATUS_LABELS.items()),
        "summary": summary,
        "page_obj": page_obj,
        "reason_columns": columns,
        "reason_column_count": max(1, len(columns)),
        "reason_filters": filters_access,
        "reason_cards": _permitted_columns(codes, TERM_REASON_CARD_PERMISSIONS),
        "can_paginate_reasons": "termination_reasons.control.pagination" in codes,
        "can_view_reason_details": "termination_reasons.action.details" in codes,
        "detail_attempt": detail_attempt,
        "detail_outcome": detail_outcome,
        "detail_query": detail_query,
        "page_query": page_query,
        "lookup_error": lookup_error,
        "can_refresh_reasons": "termination_reasons.action.refresh" in codes,
        "can_export_reasons": "termination_reasons.export" in codes,
        "reason_fields": _component_access(codes, TERM_REASON_FIELD_PERMISSIONS),
        "show_toluna_notifications": show_toluna_notifications,
        "toluna_tabs": toluna_tabs,
        "toluna_event": toluna_event,
        "toluna_page_obj": toluna_page_obj,
        "toluna_detail": toluna_detail,
        "toluna_detail_query": toluna_detail_query,
        "toluna_page_query": toluna_page_query,
    })


@function_permission_required("termination_reasons.export")
def termination_reasons_export(request):
    """Export the exact filtered Term Reports result set with both status layers."""
    codes = effective_permission_codes(request.user)
    filters_access = _component_access(codes, TERM_REASON_FILTER_PERMISSIONS)
    queryset, selected = _filtered_term_report_queryset(request, filters_access)
    queryset = queryset.order_by("-callback_at", "-initiated_at")
    headers = [
        "RID", "PID", "UID", "Project ID", "Client survey ID", "Client", "Provider",
        "Buyer ID", "Country", "Respondent", "Email", "Entry IP", "Exit IP",
        "Platform status", "Provider status", "Term reason", "Term category",
        "Status source", "Started at", "Ended at", "LOI (minutes)",
    ]
    widths = [15, 13, 22, 19, 20, 22, 18, 17, 13, 22, 30, 17, 17, 20, 27, 44, 22, 20, 24, 24, 15]

    def rows():
        for attempt in queryset.iterator(chunk_size=500):
            outcome = provider_outcome(attempt)
            survey = attempt.survey
            client = survey.client or (survey.integration.client if survey.integration_id else None)
            provider = survey.integration.provider_code if survey.integration_id else "innovatemr"
            if attempt.platform_user_id:
                respondent = attempt.platform_user.get_full_name() or attempt.platform_user.username
                email = attempt.platform_user.email
            else:
                respondent = ""
                email = ""
            ended_at = attempt.callback_at or attempt.last_callback_at or attempt.initiated_at
            yield [
                attempt.rid, getattr(attempt, "pid", ""), attempt.provider_profile_uid or attempt.prescreener_uid or "",
                survey.local_id, survey.source_key, client.name if client else survey.company_name,
                provider, survey.buyer_id, survey.country_code or survey.country, respondent, email,
                attempt.initiation_ip or "", attempt.callback_ip or "",
                UNSUCCESSFUL_STATUS_LABELS.get(attempt.status, attempt.get_status_display()),
                outcome.get("status") or "Not supplied", outcome.get("reason") or "",
                outcome.get("category") or "", attempt.status_source,
                _excel_datetime(attempt.initiated_at), _excel_datetime(ended_at),
                round(attempt.loi_seconds / 60, 2) if attempt.loi_seconds is not None else "",
            ]
    local_now = timezone.localtime()
    sheets = [ExcelSheet("Term Reports", headers, rows(), widths)]
    if "toluna" in selected["provider"]:
        event_type = (request.GET.get("toluna_event") or TolunaNotification.EventType.MEMBER_TERMINATE).strip()
        valid_events = {value for value, _label, _description in TOLUNA_NOTIFICATION_TABS}
        if event_type not in valid_events:
            event_type = TolunaNotification.EventType.MEMBER_TERMINATE
        notifications = _filtered_toluna_notifications(request, selected).filter(
            event_type=event_type
        ).order_by("-report_time", "-received_at")
        notification_headers = [
            "Notification", "RID", "Unique code", "Project ID", "Survey ID", "Survey reference",
            "Wave ID", "Quota ID", "Provider status", "Reason", "Rejection ID", "Rejection",
            "Reconciliation ID", "Revenue", "Applied", "Processing result", "Occurred at",
            "Received at", "Duplicate deliveries",
        ]
        notification_widths = [
            24, 15, 22, 19, 18, 28, 12, 12, 22, 28, 14, 30, 18, 13, 11, 38, 24, 24, 18,
        ]

        def notification_rows():
            for notification in notifications.iterator(chunk_size=500):
                yield [
                    notification.get_event_type_display(),
                    notification.attempt.rid if notification.attempt_id else "",
                    notification.unique_code,
                    notification.survey.local_id if notification.survey_id else "",
                    notification.provider_survey_id or "",
                    notification.survey_ref,
                    notification.wave_id or "",
                    notification.quota_id or "",
                    notification.provider_status,
                    notification.reason,
                    notification.rejection_id or "",
                    notification.rejection_name,
                    notification.reconciliation_id or "",
                    round(notification.revenue_cents / 100, 2) if notification.revenue_cents is not None else "",
                    "Yes" if notification.applied else "No",
                    notification.processing_message,
                    _excel_datetime(notification.occurred_at),
                    _excel_datetime(notification.received_at),
                    notification.duplicate_count,
                ]

        sheets.append(ExcelSheet("Toluna Notifications", notification_headers, notification_rows(), notification_widths))
    return build_excel_response(
        f"term-reports-{local_now:%Y%m%d-%H%M%S}-IST.xlsx",
        sheets,
    )


def workspace_home(request):
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login
        return redirect_to_login(request.get_full_path())
    if has_function_access(request.user, "projects.view"):
        return HttpResponseRedirect(reverse("projects"))
    if has_function_access(request.user, "dashboard.view"):
        return HttpResponseRedirect(reverse("dashboard"))
    if has_function_access(request.user, "attempts.view"):
        return HttpResponseRedirect(reverse("traffic-reports"))
    if has_function_access(request.user, "termination_reasons.view"):
        return HttpResponseRedirect(reverse("termination-reasons"))
    if has_function_access(request.user, "user_hits.view"):
        return HttpResponseRedirect(reverse("user-hits"))
    if has_function_access(request.user, "prescreener_data.view"):
        return HttpResponseRedirect(reverse("prescreened-data"))
    if any(has_function_access(request.user, code) for code in ("vendors.view", "vendors.manage", "allocations.view", "allocations.manage")):
        return HttpResponseRedirect(reverse("vendor-management"))
    if any(has_function_access(request.user, code) for code in ("access.manage", "users.view", "users.create", "roles.view", "roles.create")):
        return HttpResponseRedirect(reverse("access-control"))
    from django.core.exceptions import PermissionDenied
    raise PermissionDenied("No workspace page is assigned to this account.")


def _qualifying_option_values(question):
    """Return provider-approved option IDs from normalized targeting data."""

    raw = question.raw_data or {}
    if "targeting_choices" not in raw:
        return None
    allowed = {str(value) for value in raw.get("targeting_choices") or []}
    if not allowed:
        return None
    if question.key == "RFG_GENDER":
        return {
            "M" if value == "1" else "F" if value == "2" else value
            for value in allowed
        }
    return allowed


def _toluna_required_option_values(question):
    raw = question.raw_data or {}
    if not raw.get("required_by_provider"):
        return None
    if raw.get("toluna_kind") == "postal" or "open" in str(
        raw.get("reference_answer_type") or ""
    ).lower():
        # Open-ended AnswerIDs describe Toluna's answer envelope; they are not
        # respondent-facing values.  Use concrete AnswerValues when supplied,
        # otherwise accept any non-empty text and let the provider contract
        # carry the synthetic ID during member registration.
        return {
            str(value).strip()
            for value in raw.get("allowed_answer_values") or []
            if str(value).strip()
        } or None
    allowed = {str(value) for value in raw.get("allowed_answer_ids") or []}
    if allowed:
        return allowed
    # The Toluna adapter has already reduced value-backed choice questions to
    # the provider-required options. Preserve that closed list in the UI and
    # in POST validation even when the quota supplied values instead of IDs.
    return {
        str(option.get("OptionId"))
        for option in question.options or []
        if isinstance(option, dict) and option.get("OptionId") is not None
    } or None


def _toluna_required_value_note(question, *, is_postal=False):
    raw = question.raw_data or {}
    if not raw.get("required_by_provider"):
        return ""
    values = []
    for value in raw.get("allowed_answer_values") or []:
        normalized = str(value).strip()
        if normalized and normalized not in values:
            values.append(normalized)
    if not values:
        return ""
    if is_postal:
        label = "Required postal code or prefix" if len(values) == 1 else "Required postal codes or prefixes"
    else:
        label = "Required value" if len(values) == 1 else "Required values"
    if is_postal and len(values) > 20:
        return f"{label}: all {len(values):,} accepted values are listed below."
    return f"{label}: {', '.join(values)}"


def _toluna_required_value_groups(question, *, is_postal=False):
    """Return the complete merged value list without exposing quota layers."""

    raw = question.raw_data or {}
    if not is_postal or not raw.get("required_by_provider"):
        return []
    values = []
    seen = set()
    for value in raw.get("allowed_answer_values") or []:
        normalized = str(value).strip()
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            values.append(normalized)
    if len(values) <= 20:
        return []
    return [values[index:index + 25] for index in range(0, len(values), 25)]


def _is_postal_targeting_question(key, text):
    """Recognize ZIP/postal/PIN qualifications across provider naming variants."""

    normalized = re.sub(
        r"[^a-z0-9]+",
        " ",
        f"{key or ''} {text or ''}".lower(),
    ).strip()
    return bool(re.search(
        r"\b(?:zip\s*codes?|postal\s*codes?|post\s*codes?|pin\s*codes?|pincodes?)\b",
        normalized,
    ))


def _innovatemr_postal_targeting_values(question):
    """Return InnovateMR OptionText postal values, never sequence OptionIds."""

    values = []
    seen = set()
    for option in question.options or []:
        if isinstance(option, dict):
            value = option.get("OptionText")
            if value in (None, ""):
                value = (
                    option.get("OptionCode")
                    or option.get("OptionValue")
                    or option.get("Value")
                )
        else:
            value = option
        value = clean_rfg_display_text(str(value or "")).strip()
        normalized = value.casefold()
        if value and normalized not in seen:
            seen.add(normalized)
            values.append(value)
    return values


def _normalized_postal_targeting_value(value):
    return re.sub(r"[\s-]+", "", str(value or "")).casefold()


def _innovatemr_postal_targeting_note(values):
    if not values:
        return ""
    visible_limit = 12
    shown = ", ".join(values[:visible_limit])
    if len(values) <= visible_limit:
        return f"Required ZIP/postal codes: {shown}"
    return (
        f"Required ZIP/postal codes: {len(values):,} provider-approved codes "
        f"(examples: {shown})"
    )


def _rfg_profile_dimension(question):
    """Return the mandatory respondent-profile dimension for one RFG row."""

    key = re.sub(r"[^a-z0-9]+", " ", str(question.key or "").lower()).strip()
    text = re.sub(
        r"[^a-z0-9]+",
        " ",
        clean_rfg_display_text(question.text or "").lower(),
    ).strip()
    combined = f"{key} {text}"
    if re.search(r"\b(gender|sex)\b", combined):
        return "gender"
    if re.search(r"\b(date of birth|birthday|dob|age)\b", combined):
        return "age"
    if re.search(r"\b(postal code|postcode|zip code|zipcode|zip)\b", combined):
        return "postal"
    return ""


def _rfg_alias_allowed_values(question, dimension):
    choices = {
        str(value) for value in (question.raw_data or {}).get("targeting_choices") or []
    }
    if dimension == "gender":
        return {
            "M" if value == "1" else "F" if value == "2" else value
            for value in choices
        }
    return choices


def _rfg_alias_upstream_values(alias, dimension, values):
    """Translate a displayed mandatory-profile answer to an alias answer ID."""

    if dimension != "gender" or not values:
        return list(values)
    selected = str(values[0]).upper()
    wanted_label = "male" if selected in {"M", "1"} else "female"
    for option in alias.options or []:
        if not isinstance(option, dict):
            continue
        label = clean_rfg_display_text(option.get("OptionText") or "").lower().strip()
        if label == wanted_label and option.get("OptionId") not in (None, ""):
            return [str(option["OptionId"])]
    return ["1" if wanted_label == "male" else "2"]


def _prescreener_questions(survey, submitted_data=None, *, qualifying_options_only=True):
    prepared = []
    provider_code = str(
        survey.integration.provider_code
        if survey.integration_id else "innovatemr"
    ).lower()
    is_rfg = provider_code == "rfg"
    is_toluna = provider_code == "toluna"
    question_rows = list(survey.targeting_questions.all())
    if provider_code == "cint" and not question_rows:
        # No-qualification Cint opportunities still collect a minimal reusable
        # profile. Empty question IDs and upstream values keep these controls
        # out of the signed provider entry URL.
        question_rows = [
            SimpleNamespace(
                pk="platform_profile_age",
                question_id="",
                key="AGE",
                text="What is your age?",
                question_type="Numeric",
                category="Required profile",
                options=[],
                raw_data={
                    "platform_only": True,
                    "targeting_age_ranges": [{"min": 13, "max": OPEN_ENDED_AGE_MAX}],
                },
            ),
            SimpleNamespace(
                pk="platform_profile_gender",
                question_id="",
                key="GENDER",
                text="What is your gender?",
                question_type="Single Punch",
                category="Required profile",
                options=[
                    {"OptionId": "male", "OptionText": "Male"},
                    {"OptionId": "female", "OptionText": "Female"},
                ],
                raw_data={"platform_only": True},
            ),
        ]

    profile_aliases = {}
    aliased_question_ids = set()
    if is_rfg:
        required = {}
        for question in question_rows:
            dimension = _rfg_profile_dimension(question)
            is_required = (
                str(question.category or "").strip().lower() == "required profile"
                or str(question.key or "").upper()
                in {"RFG_BIRTHDAY", "RFG_GENDER", "RFG_POSTAL_CODE"}
            )
            if dimension and is_required:
                required[dimension] = question
        for question in question_rows:
            dimension = _rfg_profile_dimension(question)
            primary = required.get(dimension)
            if primary and primary.pk != question.pk:
                profile_aliases.setdefault(primary.pk, []).append(question)
                aliased_question_ids.add(question.pk)

    for question in question_rows:
        if question.pk in aliased_question_ids:
            continue
        display_text = clean_rfg_display_text(question.text or question.key)
        lowered_type = question.question_type.lower()
        normalized_key = str(question.key or "").upper()
        normalized_text = display_text.lower()
        is_dob_question = (
            normalized_key in {"DOB", "BIRTHDAY", "RFG_BIRTHDAY"}
            or "date of birth" in normalized_text
            or "birthday" in normalized_text
        )
        is_age_question = (
            normalized_key == "AGE"
            or ("your age" in normalized_text and not is_dob_question)
        )
        is_postal_question = _is_postal_targeting_question(
            normalized_key,
            normalized_text,
        )
        options = []
        age_ranges = []
        allowed_values = _qualifying_option_values(question)
        if is_toluna:
            toluna_required_values = _toluna_required_option_values(question)
            if toluna_required_values is not None:
                allowed_values = (
                    set(toluna_required_values)
                    if allowed_values is None
                    else set(allowed_values).intersection(toluna_required_values)
                )
        toluna_postal_prefixes = []
        toluna_postal_constraints_present = False
        if is_toluna and is_postal_question:
            # Toluna open-ended postal questions usually carry a synthetic
            # AnswerID plus the actual qualifying ZIP/postal prefixes in
            # AnswerValues.  The synthetic ID is registration metadata, not a
            # value a respondent can type, so validate against AnswerValues.
            raw_toluna_postal_values = [
                str(value).strip()
                for value in (question.raw_data or {}).get("allowed_answer_values") or []
                if str(value).strip()
            ]
            toluna_postal_constraints_present = bool(raw_toluna_postal_values)
            toluna_postal_prefixes = [
                value
                for value in raw_toluna_postal_values
                if _normalized_postal_targeting_value(value)
            ]
            if toluna_postal_constraints_present:
                allowed_values = set(toluna_postal_prefixes)
        dimension = _rfg_profile_dimension(question) if is_rfg else ""
        alias_allowed_sets = [
            _rfg_alias_allowed_values(alias, dimension)
            for alias in profile_aliases.get(question.pk, [])
            if (alias.raw_data or {}).get("targeting_choices")
        ]
        for alias_allowed in alias_allowed_sets:
            allowed_values = (
                set(alias_allowed)
                if allowed_values is None
                else set(allowed_values).intersection(alias_allowed)
            )
        postal_targeting_values = (
            _innovatemr_postal_targeting_values(question)
            if provider_code == "innovatemr" and is_postal_question else []
        )
        if postal_targeting_values:
            # InnovateMR ZIP OptionIds are sequence numbers; OptionText holds
            # the canonical provider value which the respondent must submit.
            allowed_values = set(postal_targeting_values)
        rendered_option_rows = (
            [] if postal_targeting_values else question.options or []
        )
        for option in rendered_option_rows:
            if not isinstance(option, dict):
                option = {"OptionId": option, "OptionText": str(option)}
            option_id = option.get("OptionId")
            parsed_age_range = (
                normalize_age_range(option)
                if is_age_question or is_dob_question else None
            )
            if parsed_age_range is not None:
                label = f"{parsed_age_range[0]}–{parsed_age_range[1]}"
            else:
                label = clean_rfg_display_text(
                    option.get("OptionText") or str(option_id or "Option")
                )
            value = str(option_id if option_id is not None else label)
            option_is_qualified = not allowed_values or value in allowed_values
            if parsed_age_range is not None and option_is_qualified:
                age_ranges.append({
                    "ageStart": parsed_age_range[0],
                    "ageEnd": parsed_age_range[1],
                })
            if qualifying_options_only and not option_is_qualified:
                continue
            options.append({"value": value, "label": label})
        if is_dob_question or is_age_question:
            age_constraints_present = bool(
                question.options
                or (question.raw_data or {}).get("targeting_age_ranges")
                or allowed_values
            )
            for item in (question.raw_data or {}).get("targeting_age_ranges") or []:
                parsed_age_range = normalize_age_range(item)
                if parsed_age_range is not None:
                    age_ranges.append({
                        "ageStart": parsed_age_range[0],
                        "ageEnd": parsed_age_range[1],
                    })
            has_effective_age_ranges = bool(age_ranges)
            for alias in profile_aliases.get(question.pk, []):
                age_constraints_present = age_constraints_present or bool(
                    alias.options
                    or (alias.raw_data or {}).get("targeting_age_ranges")
                    or (alias.raw_data or {}).get("targeting_choices")
                )
                alias_age_ranges = []
                alias_allowed = {
                    str(value)
                    for value in (alias.raw_data or {}).get("targeting_choices") or []
                }
                for option in alias.options or []:
                    option_id = (
                        option.get("OptionId")
                        if isinstance(option, dict) else option
                    )
                    if alias_allowed and str(option_id) not in alias_allowed:
                        continue
                    parsed_age_range = normalize_age_range(option)
                    if parsed_age_range is not None:
                        alias_age_ranges.append({
                            "ageStart": parsed_age_range[0],
                            "ageEnd": parsed_age_range[1],
                        })
                for item in (alias.raw_data or {}).get("targeting_age_ranges") or []:
                    parsed_age_range = normalize_age_range(item)
                    if parsed_age_range is not None:
                        alias_age_ranges.append({
                            "ageStart": parsed_age_range[0],
                            "ageEnd": parsed_age_range[1],
                        })
                if alias_age_ranges and has_effective_age_ranges:
                    age_ranges = [
                        {
                            "ageStart": max(
                                int(primary["ageStart"]),
                                int(alias_range["ageStart"]),
                            ),
                            "ageEnd": min(
                                int(primary["ageEnd"]),
                                int(alias_range["ageEnd"]),
                            ),
                        }
                        for primary in age_ranges
                        for alias_range in alias_age_ranges
                        if max(
                            int(primary["ageStart"]),
                            int(alias_range["ageStart"]),
                        ) <= min(
                            int(primary["ageEnd"]),
                            int(alias_range["ageEnd"]),
                        )
                    ]
                elif alias_age_ranges:
                    age_ranges = alias_age_ranges
                    has_effective_age_ranges = True
        else:
            age_constraints_present = False
        if is_dob_question:
            input_kind = "date_mask"
            display_text = "What is your date of birth?"
        elif is_age_question:
            input_kind = "number"
            display_text = "What is your age?"
        elif is_postal_question:
            input_kind = "text"
        elif is_toluna and "open" in str(
            (question.raw_data or {}).get("reference_answer_type") or ""
        ).lower():
            input_kind = "text"
        elif "date" in lowered_type:
            input_kind = "date_mask"
        elif "multi" in lowered_type:
            input_kind = "checkbox"
        elif "single" in lowered_type and options:
            input_kind = "radio"
        elif options:
            # A few providers return a closed choice list with a generic or
            # ``Dummy`` type. Options are authoritative, so render a safe
            # selectable control instead of asking for arbitrary free text.
            input_kind = "radio"
        elif question.key.upper() == "AGE" or "numeric" in lowered_type:
            input_kind = "number"
        else:
            input_kind = "text"
        field_name = f"question_{question.pk}"
        selected_values = submitted_data.getlist(field_name) if submitted_data is not None else []
        current_value = selected_values[0] if selected_values else ""
        if input_kind == "date_mask" and current_value:
            try:
                current_value = date.fromisoformat(current_value).strftime("%d-%m-%Y")
            except ValueError:
                pass
        for option in options:
            option["selected"] = option["value"] in selected_values
        unique_age_ranges = sorted({
            (int(item["ageStart"]), int(item["ageEnd"])) for item in age_ranges
        })
        age_ranges = []
        for minimum, maximum in unique_age_ranges:
            if age_ranges and minimum <= age_ranges[-1][1] + 1:
                age_ranges[-1] = (
                    age_ranges[-1][0],
                    max(age_ranges[-1][1], maximum),
                )
            else:
                age_ranges.append((minimum, maximum))
        min_value = min((item[0] for item in age_ranges), default=None)
        max_value = max((item[1] for item in age_ranges), default=None)
        age_range_labels = [
            f"{minimum}\u2013{maximum}"
            for minimum, maximum in age_ranges
        ]
        required_value_note = (
            _toluna_required_value_note(question, is_postal=is_postal_question)
            if is_toluna and input_kind not in {"radio", "checkbox"}
            else ""
        )
        required_value_groups = (
            _toluna_required_value_groups(question, is_postal=is_postal_question)
            if is_toluna and input_kind not in {"radio", "checkbox"}
            else []
        )
        provider_targeting_note = clean_rfg_display_text(
            (question.raw_data or {}).get("targeting_note") or ""
        )
        prepared.append({
            "model": question,
            "profile_dimension": dimension,
            "aliases": profile_aliases.get(question.pk, []),
            "display_text": display_text,
            "field_name": field_name,
            "input_kind": input_kind,
            "type_label": (
                "Date of birth" if is_dob_question
                else "Age" if is_age_question
                else "Postal code" if is_postal_question
                else "Date" if input_kind == "date_mask"
                else (question.question_type or "Question")
            ),
            "options": options,
            "current_value": current_value,
            "min_value": min_value,
            "max_value": max_value,
            "input_label": (
                "Age" if is_age_question
                else "ZIP / postal code" if is_postal_question
                else "Your answer"
            ),
            "placeholder": (
                "Enter your age" if is_age_question
                else "Enter your ZIP / postal code" if is_postal_question
                else "Type your answer"
            ),
            "is_dob_question": is_dob_question,
            "is_age_question": is_age_question,
            "is_postal_question": is_postal_question,
            "postal_allowed_values": postal_targeting_values,
            "postal_prefix_match": toluna_postal_constraints_present,
            "allowed_values": sorted(allowed_values or []),
            # RFG deliberately accepts a visible non-qualifying answer and
            # records a local early-termination reason. Other providers use
            # strict prescreener validation because no equivalent outcome
            # contract exists for a disallowed value.
            "enforce_allowed_values": not is_rfg,
            "age_ranges": age_ranges,
            "age_constraints_present": age_constraints_present,
            "qualifying_options_only": bool(
                qualifying_options_only and allowed_values
            ),
            "targeting_note": (
                provider_targeting_note
                or (
                    f"Qualifying age: {', '.join(age_range_labels)}"
                    if (is_age_question or is_dob_question) and age_range_labels
                    else required_value_note
                    if required_value_note
                    else _innovatemr_postal_targeting_note(postal_targeting_values)
                    if postal_targeting_values
                    else "Only answers accepted by this survey are shown."
                    if qualifying_options_only and (
                        allowed_values or (is_toluna and (question.raw_data or {}).get("required_by_provider") and options)
                    ) else ""
                )
            ),
            "targeting_value_groups": required_value_groups,
            "targeting_value_count": sum(
                len(group) for group in required_value_groups
            ),
        })
    return prepared


def _collect_prescreener_answers(request, survey):
    answers = {}
    errors = []
    for prepared in _prescreener_questions(
        survey, qualifying_options_only=False
    ):
        question = prepared["model"]
        respondent_age = None
        if prepared["input_kind"] == "date_mask":
            raw_date = request.POST.get(prepared["field_name"], "").strip()
            try:
                parts = raw_date.split("-")
                if len(parts) != 3:
                    raise ValueError
                if len(parts[0]) == 4:
                    year, month, day = parts
                else:
                    day, month, year = parts
                born = date(int(year), int(month), int(day))
                normalized_date = born.isoformat()
                if prepared["is_dob_question"]:
                    today = date.today()
                    respondent_age = today.year - born.year - (
                        (today.month, today.day) < (born.month, born.day)
                    )
            except (TypeError, ValueError):
                errors.append(
                    f"Enter a valid date in DD-MM-YYYY format for: {prepared['display_text']}"
                )
                continue
            values = [normalized_date]
        else:
            values = [value.strip() for value in request.POST.getlist(prepared["field_name"]) if value.strip()]
        if not values:
            errors.append(f"Please answer: {prepared['display_text']}")
            continue

        valid_options = {item["value"] for item in prepared["options"]}
        allowed_values = set(prepared.get("allowed_values") or [])
        enforced_allowed_values = (
            allowed_values if prepared.get("enforce_allowed_values", True) else set()
        )
        upstream_values = values.copy()
        if prepared["input_kind"] in {"radio", "checkbox"}:
            invalid = [
                value for value in values
                if value not in valid_options
                or (
                    enforced_allowed_values
                    and value not in enforced_allowed_values
                )
            ]
            if invalid:
                errors.append(f"Invalid answer for: {prepared['display_text']}")
                continue
        elif prepared["input_kind"] == "number":
            try:
                numeric_value = int(values[0])
            except ValueError:
                errors.append(f"Enter a valid number for: {prepared['display_text']}")
                continue
            # AGE and other numeric-open-ended qualifications must carry the
            # respondent's actual answer. Targeting OptionIds identify the
            # provider's accepted range, not the respondent's age.
            upstream_values = [str(numeric_value)]
            if prepared["is_age_question"]:
                respondent_age = numeric_value
        elif prepared.get("is_postal_question") and (
            enforced_allowed_values or prepared.get("postal_prefix_match")
        ):
            accepted = {}
            for value in enforced_allowed_values:
                normalized_allowed_value = _normalized_postal_targeting_value(value)
                if normalized_allowed_value:
                    accepted.setdefault(normalized_allowed_value, str(value))
            normalized_value = _normalized_postal_targeting_value(values[0])
            if prepared.get("postal_prefix_match"):
                postal_is_accepted = any(
                    normalized_value.startswith(normalized)
                    for normalized in accepted
                )
                canonical_value = values[0] if postal_is_accepted else None
            else:
                canonical_value = accepted.get(normalized_value)
            if canonical_value is None:
                errors.append(
                    f"Enter a ZIP/postal code accepted by this survey for: {prepared['display_text']}"
                )
                continue
            if not prepared.get("postal_prefix_match"):
                values = [canonical_value]
                upstream_values = [canonical_value]

        if respondent_age is not None and (
            not 1 <= respondent_age <= OPEN_ENDED_AGE_MAX
            or bool(prepared["age_ranges"]) and not any(
                minimum <= respondent_age <= maximum
                for minimum, maximum in prepared["age_ranges"]
            )
            or prepared.get("age_constraints_present") and not prepared["age_ranges"]
        ):
            errors.append(f"Enter an age within the accepted range for: {prepared['display_text']}")
            continue

        platform_only = bool((question.raw_data or {}).get("platform_only"))
        if platform_only:
            upstream_values = []

        answers[str(question.pk)] = {
            "question_id": question.question_id,
            "question_key": question.key,
            "question_text": prepared["display_text"],
            "question_type": question.question_type,
            "question_category": question.category,
            "values": values,
            "upstream_values": upstream_values,
            "platform_only": platform_only,
        }
        for alias in prepared.get("aliases", []):
            alias_upstream_values = _rfg_alias_upstream_values(
                alias,
                prepared.get("profile_dimension", ""),
                upstream_values,
            )
            answers[str(alias.pk)] = {
                "question_id": alias.question_id,
                "question_key": alias.key,
                "question_text": clean_rfg_display_text(alias.text or alias.key),
                "values": values,
                "upstream_values": alias_upstream_values,
                "profile_alias": question.key,
            }
    return answers, errors


def _invalid_survey_link(request, message="This link is invalid or is no longer available.", status_code=400):
    return render(request, "surveys/flow_error.html", {
        "title": "Invalid survey link",
        "message": message,
    }, status=status_code)


def _has_exact_query(request, expected_names):
    """Reject duplicated or client-injected start-link parameters."""
    return set(request.GET.keys()) == set(expected_names) and all(
        len(request.GET.getlist(name)) == 1 for name in expected_names
    )


def _rfg_result_url(rid, result):
    return f"{reverse('rfg-result')}?{urlencode({'rid': rid, 'result': result})}"


def _release_prepared_toluna_invite(attempt):
    """Atomically consume one prepared invite and redirect without exposing profile data."""

    with transaction.atomic():
        locked = SurveyAttempt.objects.select_for_update().get(pk=attempt.pk)
        if (
            locked.status != SurveyAttempt.Status.INITIATED
            or not locked.submitted_at
            or not locked.outbound_url
        ):
            return None
        outbound_url = locked.outbound_url
        locked.redirected_at = timezone.now()
        locked.status = SurveyAttempt.Status.REDIRECTED
        locked.save(update_fields=["redirected_at", "status", "updated_at"])
    return HttpResponseRedirect(outbound_url)


def _finish_local_rfg_attempt(attempt, answers, request, *, result, reason):
    now = timezone.now()
    with transaction.atomic():
        locked = SurveyAttempt.objects.select_for_update().get(pk=attempt.pk)
        if locked.status != SurveyAttempt.Status.INITIATED:
            return locked
        client_data = get_request_client_data(request)
        locked.answers = operational_answer_value(answers)
        locked.submitted_at = now
        locked.callback_at = now
        locked.last_callback_at = now
        locked.callback_ip = get_request_ip(request) or locked.initiation_ip
        locked.exit_user_agent = client_data.get("user_agent", "")
        locked.exit_browser = client_data.get("browser", "")
        locked.exit_device = client_data.get("device", "")
        locked.exit_os = client_data.get("os", "")
        locked.exit_client_data = client_data
        locked.status = RFG_STATUS_MAP[result]
        locked.status_source = "local_prescreener"
        locked.loi_seconds = locked.calculate_loi_seconds(now)
        locked.upstream_transaction_data = {
            **(locked.upstream_transaction_data or {}),
            "rfg_local_outcome": {"result": result, "local_reason": reason},
        }
        locked.save(update_fields=[
            "answers", "submitted_at", "callback_at", "last_callback_at", "callback_ip",
            "exit_user_agent", "exit_browser", "exit_device", "exit_os", "exit_client_data",
            "status", "status_source", "loi_seconds", "upstream_transaction_data", "updated_at",
        ])
        finalize_attempt_capacity(locked)
    return locked


def _finish_toluna_invite_rejection(attempt, answers, request, rejection):
    """Persist a documented Toluna invite ResultCode as a final local status."""

    now = timezone.now()
    with transaction.atomic():
        locked = SurveyAttempt.objects.select_for_update().get(pk=attempt.pk)
        if locked.status != SurveyAttempt.Status.INITIATED:
            return locked
        client_data = get_request_client_data(request)
        locked.answers = operational_answer_value(answers)
        locked.submitted_at = now
        locked.callback_at = now
        locked.last_callback_at = now
        locked.callback_ip = get_request_ip(request) or locked.initiation_ip
        locked.exit_user_agent = client_data.get("user_agent", "")
        locked.exit_browser = client_data.get("browser", "")
        locked.exit_device = client_data.get("device", "")
        locked.exit_os = client_data.get("os", "")
        locked.exit_client_data = client_data
        locked.status = rejection.status_code
        locked.status_source = "toluna_invite_rejection"
        locked.loi_seconds = locked.calculate_loi_seconds(now)
        locked.upstream_transaction_data = {
            **(locked.upstream_transaction_data or {}),
            "toluna_invite_rejection": {
                "result": rejection.result,
                "result_code": rejection.result_code,
                "reason": str(rejection),
            },
        }
        locked.save(update_fields=[
            "answers", "submitted_at", "callback_at", "last_callback_at", "callback_ip",
            "exit_user_agent", "exit_browser", "exit_device", "exit_os", "exit_client_data",
            "status", "status_source", "loi_seconds", "upstream_transaction_data", "updated_at",
        ])
        finalize_attempt_capacity(locked)
    return locked


def _finish_wrong_target_country_attempt(attempt, request, location):
    """Record a local S4 before any prescreener question or provider redirect."""
    now = timezone.now()
    expected = survey_target_country_code(attempt.survey)
    actual = str((location or {}).get("country_code") or "").upper()
    vault_answers = wrong_target_country_answers(attempt, location)
    if settings.PRESCREENER_VAULT_ENABLED:
        try:
            capture_prescreener_submission(attempt, vault_answers, submitted_at=now)
        except PrescreenerVaultError:
            logger.exception("Wrong-target-country vault capture failed for rid=%s", attempt.rid)
    with transaction.atomic():
        locked = SurveyAttempt.objects.select_for_update().get(pk=attempt.pk)
        if locked.status != SurveyAttempt.Status.INITIATED:
            return locked
        client_data = {**get_request_client_data(request), **geolocation_client_data(location)}
        locked.answers = operational_answer_value(vault_answers)
        locked.submitted_at = now
        locked.callback_at = now
        locked.last_callback_at = now
        locked.callback_ip = get_request_ip(request) or locked.initiation_ip
        locked.exit_user_agent = client_data.get("user_agent", "")
        locked.exit_browser = client_data.get("browser", "")
        locked.exit_device = client_data.get("device", "")
        locked.exit_os = client_data.get("os", "")
        locked.exit_client_data = client_data
        locked.status = SurveyAttempt.Status.QUALITY_TERMINATED
        locked.status_source = "local_country_guard"
        locked.loi_seconds = locked.calculate_loi_seconds(now)
        locked.upstream_transaction_data = {
            **(locked.upstream_transaction_data or {}),
            "local_country_guard": {
                "status": "Wrong target country", "reason": "Wrong target country",
                "expected_country": expected, "detected_country": actual,
                "geo_source": str((location or {}).get("source") or ""),
            },
        }
        locked.save(update_fields=[
            "answers", "submitted_at", "callback_at", "last_callback_at", "callback_ip",
            "exit_user_agent", "exit_browser", "exit_device", "exit_os", "exit_client_data",
            "status", "status_source", "loi_seconds", "upstream_transaction_data", "updated_at",
        ])
        finalize_attempt_capacity(locked)
    return locked


def _finish_duplicate_ip_attempt(attempt, request, prior_attempt=None):
    """Record a same-project duplicate entry IP as an immediate local S4."""

    now = timezone.now()
    with transaction.atomic():
        locked = SurveyAttempt.objects.select_for_update().get(pk=attempt.pk)
        if locked.status != SurveyAttempt.Status.INITIATED:
            return locked
        client_data = get_request_client_data(request)
        locked.submitted_at = now
        locked.callback_at = now
        locked.last_callback_at = now
        locked.callback_ip = get_request_ip(request) or locked.initiation_ip
        locked.exit_user_agent = client_data.get("user_agent", "")
        locked.exit_browser = client_data.get("browser", "")
        locked.exit_device = client_data.get("device", "")
        locked.exit_os = client_data.get("os", "")
        locked.exit_client_data = client_data
        locked.status = SurveyAttempt.Status.QUALITY_TERMINATED
        locked.status_source = "local_duplicate_ip_guard"
        locked.loi_seconds = locked.calculate_loi_seconds(now)
        locked.upstream_transaction_data = {
            **(locked.upstream_transaction_data or {}),
            "local_ip_guard": {
                "status": "Security terminated",
                "reason": "Duplicate IP address",
                "first_attempt_rid": getattr(prior_attempt, "rid", ""),
            },
        }
        locked.save(update_fields=[
            "submitted_at", "callback_at", "last_callback_at", "callback_ip",
            "exit_user_agent", "exit_browser", "exit_device", "exit_os", "exit_client_data",
            "status", "status_source", "loi_seconds", "upstream_transaction_data", "updated_at",
        ])
        finalize_attempt_capacity(locked)
    return locked


def _recorded_status_url(attempt, status_code):
    """Build a trusted local result URL without invoking provider callback checks."""

    return f"{reverse('survey-status')}?{urlencode({'status': str(status_code), 'rid': attempt.rid})}"


@require_http_methods(["GET", "POST"])
def survey_start(request):
    if request.method == "GET" and not request.GET.get("rid"):
        required_params = {"surveyId", "supplierCode", "userId", "code"}
        has_delivery_parameter = "delivery" in request.GET
        if has_delivery_parameter:
            required_params.add("delivery")
        if not _has_exact_query(request, required_params):
            return _invalid_survey_link(request)

        survey_id = request.GET.get("surveyId", "").strip()
        supplier_code = request.GET.get("supplierCode", "").strip()
        internal_code = request.GET.get("code", "").strip()
        user_id = request.GET.get("userId", "").strip()
        delivery_token = request.GET.get("delivery", "").strip()
        if (
            not survey_id
            or len(survey_id) > 160
            or not user_id.isdigit()
            or not internal_code.isdigit()
            or len(internal_code) != 14
        ):
            return _invalid_survey_link(request)

        delivery_api_key = None
        delivery_survey_id = None
        if has_delivery_parameter:
            try:
                delivery = decode_delivery_token(delivery_token)
                delivery_api_key = VendorAPIKey.objects.select_related(
                    "vendor", "vendor__employee_profile", "vendor__vendor_commercial_profile"
                ).get(pk=int(delivery["api_key_id"]))
                delivery_survey_id = int(delivery["survey_id"])
            except (KeyError, TypeError, ValueError, signing.BadSignature, VendorAPIKey.DoesNotExist):
                return _invalid_survey_link(request)
            if (
                not delivery_api_key.is_active
                or delivery_api_key.revoked_at
                or (delivery_api_key.expires_at and delivery_api_key.expires_at <= timezone.now())
                or str(delivery_api_key.vendor_id) != user_id
            ):
                return _invalid_survey_link(request)

        platform_user = get_user_model().objects.filter(pk=int(user_id), is_active=True).first()
        if (
            platform_user is None
            or not has_function_access(platform_user, "projects.view")
            or not has_function_access(platform_user, "survey_links.copy")
        ):
            return _invalid_survey_link(request)

        survey_queryset = scope_surveys_for_user(
            Survey.objects.select_related("integration", "client"), platform_user
        )
        if delivery_api_key:
            survey_queryset = scope_surveys_for_api_key(survey_queryset, delivery_api_key)
        survey = survey_queryset.filter(
            local_id=internal_code,
            status=Survey.Status.LIVE,
            **({"pk": delivery_survey_id} if delivery_survey_id else {}),
        ).first()
        if survey is None:
            return _invalid_survey_link(request)
        expected_survey_id = (
            survey.local_id
            if delivery_api_key and delivery_api_key.survey_id_mode == VendorAPIKey.SurveyIdMode.PROJECT_ID
            else str(survey.source_identifier)
        )
        if survey_id != expected_survey_id:
            return _invalid_survey_link(request)
        is_rfg = bool(
            survey.integration_id and survey.integration.provider_code == "rfg"
        )
        is_dynamic_provider = bool(
            survey.integration_id and survey.integration.provider_code in {"rfg", "toluna"}
        )
        if not survey.entry_link and not is_dynamic_provider:
            return _invalid_survey_link(request)
        expected_supplier_code = settings.PUBLIC_SUPPLIER_CODE
        if supplier_code != expected_supplier_code:
            return _invalid_survey_link(request)

        stale = survey.detail_synced_at is None or survey.targeting_synced_at is None or (
            survey.source_modified_at and survey.targeting_synced_at < survey.source_modified_at
        )
        if is_dynamic_provider:
            stale = stale or not survey.targeting_questions.filter(
                raw_data__adapter_version__in=[1, 2, 3, TOLUNA_ADAPTER_VERSION]
            ).exists()
            if is_rfg:
                stale = stale or not survey.entry_link or not survey.targeting_questions.filter(
                    key="RFG_BIRTHDAY",
                    raw_data__adapter_version=RFG_TARGETING_ADAPTER_VERSION,
                ).exists()
            elif survey.integration.provider_code == "toluna":
                stale = stale or not survey.targeting_questions.filter(
                    raw_data__adapter_version=TOLUNA_ADAPTER_VERSION
                ).exists()
        if survey.integration_id and survey.integration.provider_code == "biobrain":
            stale = stale or any(
                not question.text
                or str(question.text).startswith("Qualification ")
                or bool(re.fullmatch(r"Q\d+", str(question.key or ""), re.IGNORECASE))
                or (question.raw_data or {}).get("metadata_hydrated") is not True
                or any(not isinstance(option, dict) for option in (question.options or []))
                for question in survey.targeting_questions.all()
            )
        targeting_warning = ""
        if stale:
            try:
                if is_dynamic_provider:
                    get_provider(survey.integration).refresh_details(survey)
                else:
                    replace_survey_targeting(InnovateMRClient(integration=survey.integration), survey)
            except Exception:
                logger.exception(
                    "Provider detail hydration failed for survey=%s integration=%s",
                    survey.pk,
                    survey.integration_id,
                )
                if not survey.entry_link and not is_dynamic_provider:
                    return _invalid_survey_link(
                        request,
                        "The provider entry link is temporarily unavailable. Please try again shortly.",
                        status_code=503,
                    )
                if not survey.targeting_questions.exists():
                    targeting_warning = "Pre-screening criteria are temporarily unavailable. You can still continue."
        if not survey.entry_link and not is_dynamic_provider:
            return _invalid_survey_link(
                request,
                "The provider entry link is temporarily unavailable. Please try again shortly.",
                status_code=503,
            )
        entry_ip = get_request_ip(request)
        entry_location = resolve_entry_geolocation(request)
        entry_client_data = {
            **get_request_client_data(request),
            **geolocation_client_data(entry_location),
        }
        try:
            with transaction.atomic():
                allocation_context = resolve_vendor_survey_context(
                    platform_user,
                    survey,
                    require_capacity=True,
                    for_update=True,
                )
                prior_ip_attempt, duplicate_ip = claim_project_entry_ip(survey, entry_ip)
                attempt = create_attempt(
                    survey,
                    platform_user,
                    entry_ip,
                    client_data=entry_client_data,
                )
                if allocation_context:
                    reserve_attempt_capacity(
                        attempt,
                        allocation_context.survey_allocation,
                        client_allocation=allocation_context.client_allocation,
                    )
                if delivery_api_key:
                    delivery_config = {
                        "survey_id_mode": delivery_api_key.survey_id_mode,
                        "survey_id": expected_survey_id,
                        "project_id": survey.local_id,
                        "supplier_id": delivery_api_key.vendor_id,
                    }
                    SurveyAttempt.objects.filter(pk=attempt.pk).update(
                        supplier_api_key_id=delivery_api_key.pk,
                        supplier_delivery_config=delivery_config,
                    )
                    attempt.supplier_api_key_id = delivery_api_key.pk
                    attempt.supplier_delivery_config = delivery_config
        except AllocationUnavailable as exc:
            return _invalid_survey_link(request, str(exc), status_code=409)
        if duplicate_ip:
            attempt = _finish_duplicate_ip_attempt(attempt, request, prior_ip_attempt)
            return HttpResponseRedirect(_recorded_status_url(attempt, "4"))
        if targeting_warning:
            request.session[f"attempt_warning_{attempt.rid}"] = targeting_warning
        return HttpResponseRedirect(f"{reverse('survey-start')}?rid={quote(attempt.rid)}")

    if request.method == "GET" and not _has_exact_query(request, {"rid"}):
        return _invalid_survey_link(request)

    rid = (request.GET.get("rid", "") if request.method == "GET" else request.POST.get("rid", "")).strip()
    if len(rid) != 10 or not rid.isalnum():
        return _invalid_survey_link(request)
    attempt = SurveyAttempt.objects.select_related(
        "survey", "survey__integration", "platform_user"
    ).filter(rid=rid).first()
    if attempt is None or attempt.platform_user is None or not attempt.platform_user.is_active:
        return _invalid_survey_link(request, status_code=404)
    attempt = backfill_attempt_entry_audit(attempt, request)

    entry_location = {
        "ip": attempt.initiation_ip or "",
        "country_code": (attempt.entry_client_data or {}).get("geo_country_code", ""),
        "country": (attempt.entry_client_data or {}).get("geo_country", ""),
        "postal_code": (attempt.entry_client_data or {}).get("geo_postal_code", ""),
        "source": (attempt.entry_client_data or {}).get("geo_source", ""),
    }
    if not entry_location["country_code"] and request.method == "GET":
        entry_location = resolve_entry_geolocation(request)
        geo_updates = geolocation_client_data(entry_location)
        if geo_updates:
            merged_client_data = {**(attempt.entry_client_data or {}), **geo_updates}
            SurveyAttempt.objects.filter(pk=attempt.pk).update(entry_client_data=merged_client_data)
            attempt.entry_client_data = merged_client_data
    if attempt.status == SurveyAttempt.Status.INITIATED and is_wrong_target_country(attempt.survey, entry_location):
        attempt = _finish_wrong_target_country_attempt(attempt, request, entry_location)
        return HttpResponseRedirect(_recorded_status_url(attempt, "4"))

    provider_code = (
        attempt.survey.integration.provider_code
        if attempt.survey.integration_id else ""
    )
    # A Toluna invite may already have been prepared by the previous release.
    # Consume legacy in-flight rows server-side without exposing MemberCode or
    # date of birth on an intermediate page.
    if (
        provider_code == "toluna"
        and attempt.status == SurveyAttempt.Status.INITIATED
        and attempt.submitted_at
        and attempt.outbound_url
    ):
        prepared_redirect = _release_prepared_toluna_invite(attempt)
        if prepared_redirect is not None:
            request.session.pop(f"toluna_member_ready_{attempt.rid}", None)
            return prepared_redirect

    toluna_refresh_message = ""
    if (
        request.method == "POST"
        and provider_code == "toluna"
        and attempt.survey.detail_synced_at is None
    ):
        try:
            get_provider(attempt.survey.integration).refresh_details(attempt.survey)
            attempt.survey.refresh_from_db()
            toluna_refresh_message = (
                "Toluna targeting changed while this page was open. "
                "Please review the updated questions and submit again."
            )
        except Exception:
            logger.exception(
                "Toluna pre-submit targeting refresh failed for survey=%s rid=%s",
                attempt.survey_id,
                attempt.rid,
            )
            toluna_refresh_message = (
                "Toluna targeting is being updated. Please wait a moment and submit again."
            )

    if request.method == "POST":
        answers, errors = ({}, [toluna_refresh_message]) if toluna_refresh_message else (
            _collect_prescreener_answers(request, attempt.survey)
        )
        if not errors:
            try:
                if provider_code == "biobrain":
                    ensure_attempt_prescreener_uid(attempt)
                provider = None
                if provider_code in {"rfg", "toluna"}:
                    provider = get_provider(attempt.survey.integration)
                if provider_code == "rfg":
                    eligible, reason = provider.validate_prescreener(attempt.survey, answers)
                    if not eligible:
                        if settings.PRESCREENER_VAULT_ENABLED:
                            capture_prescreener_submission(
                                attempt,
                                answers_with_entry_postal_code(attempt, answers),
                                allow_draft_replace=True,
                            )
                        _finish_local_rfg_attempt(
                            attempt, answers, request, result="7", reason=reason
                        )
                        return HttpResponseRedirect(_rfg_result_url(attempt.rid, "7"))

                # Reuse is decided before vault capture. When an older matching
                # profile is selected, its original RID + UID row remains the
                # only Panelist record and only its Visits counter increments.
                # The attempt RID remains unique for callback/status tracking.
                reuse_event = maybe_assign_reusable_profile(attempt, answers)
                if settings.PRESCREENER_VAULT_ENABLED and reuse_event is None:
                    capture_prescreener_submission(
                        attempt,
                        answers_with_entry_postal_code(attempt, answers),
                        allow_draft_replace=(
                            attempt.status == SurveyAttempt.Status.INITIATED
                            and not attempt.redirected_at
                            and not attempt.outbound_url
                        ),
                    )

                if provider_code == "rfg":
                    if provider.duplicate_check(
                        attempt.survey,
                        attempt,
                        get_request_ip(request) or attempt.initiation_ip,
                        request.POST.get("rfg_fingerprint", "0"),
                    ):
                        _finish_local_rfg_attempt(
                            attempt,
                            answers,
                            request,
                            result="8",
                            reason="This respondent has already attempted this survey or survey group.",
                        )
                        return HttpResponseRedirect(_rfg_result_url(attempt.rid, "8"))
                if not errors:
                    with transaction.atomic():
                        locked = SurveyAttempt.objects.select_for_update().select_related(
                            "survey", "survey__integration"
                        ).get(pk=attempt.pk)
                        if locked.status != SurveyAttempt.Status.INITIATED:
                            return HttpResponseRedirect(f"{reverse('survey-start')}?rid={quote(locked.rid)}")
                        if provider_code == "toluna" and locked.submitted_at and locked.outbound_url:
                            # A concurrent submit may have prepared this exact
                            # invite while this request waited for the lock.
                            outbound_url = locked.outbound_url
                            locked.redirected_at = timezone.now()
                            locked.status = SurveyAttempt.Status.REDIRECTED
                            locked.save(update_fields=["redirected_at", "status", "updated_at"])
                            return HttpResponseRedirect(outbound_url)
                        if provider_code == "toluna" and locked.survey.detail_synced_at is None:
                            raise ProviderError(
                                "Toluna targeting changed while this page was open. "
                                "Please review the updated questions and submit again."
                            )
                        if provider:
                            outbound_url = provider.build_outbound_url(
                                locked.survey, locked, answers
                            )
                        elif provider_code == "biobrain":
                            outbound_url = build_biobrain_outbound_url(
                                locked.survey.entry_link,
                                locked.rid,
                                locked.provider_profile_uid or locked.prescreener_uid,
                                answers,
                            )
                        else:
                            outbound_url = build_outbound_url(
                                locked.survey.entry_link, locked.rid, answers
                            )
                        now = timezone.now()
                        locked.answers = operational_answer_value(answers)
                        locked.submitted_at = now
                        locked.outbound_url = outbound_url
                        locked.redirected_at = now
                        locked.status = SurveyAttempt.Status.REDIRECTED
                        locked.save(update_fields=[
                            "answers", "submitted_at", "redirected_at", "outbound_url", "status",
                            "source_cpi_snapshot", "payable_cpi_snapshot", "cpi_snapshot_source",
                            "upstream_transaction_data", "updated_at"
                        ])
                    return HttpResponseRedirect(outbound_url)
            except TolunaInviteRejected as exc:
                _finish_toluna_invite_rejection(attempt, answers, request, exc)
                return HttpResponseRedirect(_recorded_status_url(attempt, exc.status_code))
            except Exception as exc:
                if isinstance(exc, PrescreenerVaultError):
                    logger.exception("Prescreener vault capture failed for rid=%s", attempt.rid)
                    detail = "Secure prescreener storage is temporarily unavailable. Please submit again shortly."
                else:
                    detail = str(exc) if isinstance(exc, ProviderError) else "The upstream provider is temporarily unavailable."
                errors.append(f"Survey provider could not continue: {detail}")
    else:
        errors = []

    if attempt.status != SurveyAttempt.Status.INITIATED:
        return render(request, "surveys/status.html", {
            "title": "Survey already initiated",
            "message": "This RID has already been used to enter the survey.",
            "tone": "info",
            "status_label": attempt.get_status_display(),
            "rid": attempt.rid,
            "ip_address": attempt.callback_ip or attempt.initiation_ip,
            "loi_seconds": attempt.loi_seconds,
            "attempt_found": True,
        })

    return render(request, "surveys/prescreener.html", {
        "attempt": attempt,
        "survey": attempt.survey,
        "questions": _prescreener_questions(attempt.survey, request.POST if request.method == "POST" else None),
        "errors": errors,
        "warning": request.session.pop(f"attempt_warning_{attempt.rid}", ""),
        "is_rfg": bool(
            attempt.survey.integration_id
            and attempt.survey.integration.provider_code == "rfg"
        ),
    })


@require_http_methods(["GET", "POST"])
def toluna_member_ready(request):
    """Release legacy prepared Toluna invites without rendering member PII."""
    if request.method == "GET" and not _has_exact_query(request, {"rid"}):
        return _invalid_survey_link(request)

    rid = (
        request.GET.get("rid", "")
        if request.method == "GET"
        else request.POST.get("rid", "")
    ).strip()
    if len(rid) != 10 or not rid.isalnum():
        return _invalid_survey_link(request)

    attempt = SurveyAttempt.objects.select_related("survey__integration").filter(
        rid=rid,
        survey__integration__provider_code="toluna",
    ).first()
    if attempt is None:
        return _invalid_survey_link(request, status_code=404)

    redirect = _release_prepared_toluna_invite(attempt)
    if redirect is None:
        return _invalid_survey_link(
            request,
            "This Toluna survey entry has already been used or is no longer available.",
            status_code=409,
        )
    request.session.pop(f"toluna_member_ready_{rid}", None)
    return redirect


STATUS_PAGES = {
    "1": {"title": "Thank you for participating!", "message": "Your survey response has been completed successfully.", "tone": "success"},
    "2": {"title": "Survey ended", "message": "The survey provider ended this attempt before it could be completed.", "tone": "neutral"},
    "3": {"title": "Quota already filled", "message": "The required quota was filled before your response could be completed.", "tone": "warning"},
    "4": {"title": "Quality check unsuccessful", "message": "This response did not pass the survey's quality checks.", "tone": "danger"},
}


# Toluna has a wider end-page contract than the platform-neutral S1-S4 set.
# The database retains each distinct Toluna result so Traffic/Term Reports can
# show the real outcome instead of collapsing every unsuccessful return to S2.
TOLUNA_STATUS_PAGES = {
    "1": {"title": "Survey qualified", "message": "Your survey response qualified and was completed successfully.", "tone": "success", "status_label": "Qualified"},
    "2": {"title": "Survey terminated", "message": "The survey provider ended this attempt before completion.", "tone": "neutral", "status_label": "Terminated"},
    "3": {"title": "Quota already filled", "message": "The required quota was filled before your response could be completed.", "tone": "warning", "status_label": "Quota full"},
    "4": {"title": "Fraud check unsuccessful", "message": "This response did not pass the survey provider's fraud checks.", "tone": "danger", "status_label": "Fraud terminated"},
    "7": {"title": "Survey not available", "message": "This survey is no longer available for this respondent.", "tone": "warning", "status_label": "Survey not available"},
    "8": {"title": "No surveys available", "message": "There are currently no suitable surveys available for this respondent.", "tone": "neutral", "status_label": "No surveys"},
    "9": {"title": "Cookies are required", "message": "The survey could not continue because browser cookies were unavailable.", "tone": "warning", "status_label": "No cookies"},
    "10": {"title": "Survey limit reached", "message": "The maximum number of surveys allowed for this respondent has been reached.", "tone": "warning", "status_label": "Maximum surveys reached"},
    "11": {"title": "Not qualified", "message": "The respondent did not meet this survey's qualification requirements.", "tone": "neutral", "status_label": "Not qualified"},
    "12": {"title": "Survey already taken", "message": "This respondent has already participated in this survey.", "tone": "neutral", "status_label": "Survey already taken"},
}


def _status_presentation_for_attempt(attempt, page, status_label):
    """Explain locally enforced outcomes instead of showing a generic S4."""

    if not attempt:
        return page, status_label
    audit = attempt.upstream_transaction_data or {}
    if attempt.status_source == "local_country_guard":
        guard = audit.get("local_country_guard") or {}
        expected = str(guard.get("expected_country") or "").upper()
        actual = str(guard.get("detected_country") or "").upper()
        message = "Your location does not match this survey's target country."
        if expected and actual:
            message = f"Your detected country ({actual}) does not match this survey's target country ({expected})."
        return {
            "title": "Location not eligible",
            "message": message,
            "tone": "danger",
        }, "Wrong target country"
    if attempt.status_source == "local_duplicate_ip_guard":
        return {
            "title": "Duplicate entry blocked",
            "message": "This IP address has already entered this project, so another entry from the same IP is not allowed.",
            "tone": "danger",
        }, "Duplicate IP blocked"
    recorded_toluna_outcome = audit.get("toluna_outcome") or {}
    if not isinstance(recorded_toluna_outcome, dict):
        recorded_toluna_outcome = {}
    callback_toluna_outcome = describe_toluna_callback(audit.get("toluna_callback") or {})
    toluna_rejection_id = str(
        recorded_toluna_outcome.get("rejection_id")
        or callback_toluna_outcome.get("rejection_id")
        or ""
    )
    if (
        attempt.status_source == "toluna_callback"
        and attempt.is_verified
        and toluna_rejection_id == "73"
    ):
        return {
            "title": "Survey already attempted",
            "message": (
                "Toluna rejected this attempt because the same internet identity "
                "has already attempted this survey."
            ),
            "tone": "neutral",
        }, status_label
    return page, status_label


RFG_CALLBACK_IPS = {
    "15.222.163.99", "3.97.223.177", "3.97.28.227", "3.230.105.121",
    "52.21.20.32", "52.45.41.61",
}


@require_http_methods(["GET"])
def rfg_result(request):
    rid = status_rid_from_request(request)
    attempt = SurveyAttempt.objects.select_related("survey__integration").filter(
        rid=rid,
        survey__integration__provider_code="rfg",
    ).first()
    if not attempt:
        return _invalid_survey_link(
            request, "This RFG result link is invalid.", status_code=404
        )

    browser_parameters = dict(request.GET.items())
    now = timezone.now()
    client_data = get_request_client_data(request)
    with transaction.atomic():
        locked = SurveyAttempt.objects.select_for_update().get(pk=attempt.pk)
        locked.last_callback_at = now
        locked.exit_user_agent = client_data.get("user_agent", "")
        locked.exit_browser = client_data.get("browser", "")
        locked.exit_device = client_data.get("device", "")
        locked.exit_os = client_data.get("os", "")
        locked.exit_client_data = client_data
        locked.upstream_transaction_data = {
            **(locked.upstream_transaction_data or {}),
            "rfg_browser_return": browser_parameters,
        }
        locked.save(update_fields=[
            "last_callback_at", "exit_user_agent", "exit_browser", "exit_device", "exit_os",
            "exit_client_data", "upstream_transaction_data", "updated_at",
        ])
        attempt = locked

    stored = attempt.upstream_transaction_data or {}
    local_parameters = stored.get("rfg_local_outcome") or {}
    callback_parameters = stored.get("rfg_callback") or {}
    outcome_parameters = (
        callback_parameters if attempt.is_verified else local_parameters or browser_parameters
    )
    outcome = describe_rfg_outcome(outcome_parameters, attempt=attempt)
    return render(request, "surveys/rfg_result.html", {
        "attempt": attempt,
        "outcome": outcome,
        "verified": bool(attempt.is_verified or attempt.status_source == "local_prescreener"),
        "verification_pending": bool(
            attempt.status_source != "local_prescreener" and not attempt.is_verified
        ),
    })


class RFGCallbackAPIView(APIView):
    """Receive RFG's server callback from documented RFG callback addresses."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    @extend_schema(
        tags=["RFG Callbacks"],
        summary="Receive a verified Research For Good result callback",
        description=(
            "Called by RFG after a respondent outcome. It updates the RID attempt, exit IP/time, "
            "LOI and allocation state. This is not a normal admin test endpoint: Swagger calls will "
            "normally receive 403 because only RFG's configured server IPs are trusted. Use the "
            "RFG callback preview endpoint to safely understand result/live codes without writing data."
        ),
        parameters=[
            OpenApiParameter("rid", OpenApiTypes.STR, required=True, description="Platform respondent ID"),
            OpenApiParameter("result", OpenApiTypes.STR, required=True, description="RFG result code"),
            OpenApiParameter("ruledOutBy", OpenApiTypes.STR, required=False, description="RFG termination reason"),
            OpenApiParameter("sesskey", OpenApiTypes.STR, required=False, description="RFG session identifier"),
            OpenApiParameter("liveP", OpenApiTypes.STR, required=False, description="RFG respondent journey bit field"),
            OpenApiParameter("liveS", OpenApiTypes.STR, required=False, description="RFG security detail code"),
            OpenApiParameter("liveI", OpenApiTypes.STR, required=False, description="RFG invalid-profile detail code"),
            OpenApiParameter("quotaThrottle", OpenApiTypes.STR, required=False, description="RFG quota throttle flag"),
        ],
        responses={200: RFGCallbackResponseSerializer},
    )
    def get(self, request):
        rid = status_rid_from_request(request)
        result = request.GET.get("result", "").strip()
        attempt = SurveyAttempt.objects.select_related("survey__integration").filter(
            rid=rid,
            survey__integration__provider_code="rfg",
        ).first()
        if not attempt or result not in RFG_STATUS_MAP:
            return Response({"detail": "Unknown callback."}, status=status.HTTP_400_BAD_REQUEST)

        integration = attempt.survey.integration
        config = integration.config or {}
        if config.get("callback_security_mode", "ip") != "ip":
            return Response(
                {"detail": "Unsupported callback security mode."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        callback_ip = get_request_ip(request)
        allowed = set(config.get("callback_ip_allowlist") or RFG_CALLBACK_IPS)
        try:
            verified_ip = bool(callback_ip and str(ipaddress.ip_address(callback_ip)) in allowed)
        except ValueError:
            verified_ip = False
        if not verified_ip:
            return Response({"detail": "Callback source is not trusted."}, status=status.HTTP_403_FORBIDDEN)

        now = timezone.now()
        with transaction.atomic():
            locked = SurveyAttempt.objects.select_for_update().get(pk=attempt.pk)
            locked.status = RFG_STATUS_MAP[result]
            locked.callback_at = locked.callback_at or now
            locked.last_callback_at = now
            locked.callback_ip = callback_ip
            locked.callback_count += 1
            locked.status_source = "rfg_callback"
            locked.is_verified = True
            locked.loi_seconds = locked.calculate_loi_seconds(now)
            locked.upstream_transaction_data = {
                **(locked.upstream_transaction_data or {}),
                "rfg_callback": dict(request.GET.items()),
                "rfg_outcome": describe_rfg_outcome(dict(request.GET.items())),
            }
            locked.save(update_fields=[
                "status", "callback_at", "last_callback_at", "callback_ip", "callback_count",
                "status_source", "is_verified", "loi_seconds", "upstream_transaction_data", "updated_at",
            ])
            finalize_attempt_capacity(locked)
        return Response({"ok": True, "rid": rid, "status": locked.status})


def _external_supplier_result_url(attempt, status_code: str) -> str:
    """Forward a recorded result to the issuing external supplier."""

    if not attempt.supplier_api_key_id:
        return ""
    api_key = VendorAPIKey.objects.filter(pk=attempt.supplier_api_key_id).first()
    if not api_key:
        return ""
    callback_url = api_key.callback_url_for_status(status_code)
    if not callback_url:
        return ""
    outcome = provider_outcome(attempt)
    delivery = attempt.supplier_delivery_config or {}
    parameters = {
        "status": str(status_code),
        "surveyId": str(delivery.get("survey_id") or attempt.survey.local_id),
        "projectId": attempt.survey.local_id,
        "rid": attempt.rid,
        "pid": getattr(attempt, "pid", "") or attempt.rid,
        "statusSource": attempt.status_source or "browser_callback",
        "termReason": outcome.get("reason", ""),
        "termCategory": outcome.get("category", ""),
    }
    if api_key.callback_signing_enabled:
        try:
            secret = decrypt_secret(api_key.encrypted_callback_secret)
        except ValueError:
            logger.exception("Could not decrypt supplier callback secret api_key=%s", api_key.pk)
            return ""
        if not secret:
            logger.error("Supplier callback signing is enabled without a secret api_key=%s", api_key.pk)
            return ""
        parameters["hash"] = sign_supplier_callback(parameters, secret)
    separator = "&" if "?" in callback_url else "?"
    return f"{callback_url}{separator}{urlencode(parameters)}"


@require_http_methods(["GET"])
def survey_status(request):
    status_code = request.GET.get("status", "").strip()
    rid = status_rid_from_request(request)
    page = STATUS_PAGES.get(status_code) or TOLUNA_STATUS_PAGES.get(status_code)
    if page is None or not rid:
        return render(request, "surveys/flow_error.html", {
            "title": "Invalid survey status",
            "message": "A supported survey status and RID are required.",
        }, status=400)

    # Keep the platform RID canonical even when a provider echoes the reusable
    # profile UID (Toluna MemberCode) in its redirect. Reused UIDs are allowed
    # to appear on multiple historical journeys, so the newest matching
    # journey is the safe fallback when the provider did not return our RID.
    attempt = SurveyAttempt.objects.filter(rid=rid).first()
    if attempt is None:
        attempt = SurveyAttempt.objects.filter(prescreener_uid=rid).first()
    if attempt is None:
        attempt = (
            SurveyAttempt.objects.filter(provider_profile_uid=rid)
            .order_by("-initiated_at")
            .first()
        )
    canonical_rid = attempt.rid if attempt else rid
    ip_address = get_request_ip(request)
    toluna_notification = verified_toluna_notification_summary(attempt)
    if attempt:
        provider_code = (
            attempt.survey.integration.provider_code
            if attempt.survey.integration_id
            else "innovatemr"
        )
        trusted_recorded_source = (
            attempt.status_source in {
                "local_country_guard",
                "local_duplicate_ip_guard",
                "toluna_invite_rejection",
            }
            or (
                provider_code == "toluna"
                and attempt.status_source == "toluna_callback"
                and attempt.is_verified
            )
            or (provider_code == "toluna" and toluna_notification is not None)
        )
        canonical_query = (
            _has_exact_query(request, {"status", "rid"})
            and request.GET.get("rid", "").strip() == attempt.rid
            and trusted_recorded_source
            and str(attempt.status) == status_code
        )
        if status_code not in STATUS_PAGES and provider_code != "toluna":
            return render(request, "surveys/flow_error.html", {
                "title": "Invalid survey status",
                "message": "This extended result status is only configured for Toluna attempts.",
            }, status=400)

        callback_verified = False
        innovate_callback_verified = False
        if provider_code == "toluna":
            page = TOLUNA_STATUS_PAGES[status_code]
            if not canonical_query:
                try:
                    callback_verified = get_provider(attempt.survey.integration).verify_callback(request)
                except ProviderError as exc:
                    return render(request, "surveys/flow_error.html", {
                        "title": "Invalid Toluna callback",
                        "message": str(exc),
                    }, status=403)
                if not callback_verified:
                    return render(request, "surveys/flow_error.html", {
                        "title": "Invalid Toluna callback",
                        "message": "Toluna callback verification is not enabled for this integration.",
                    }, status=403)
        elif (
            provider_code == "innovatemr"
            and not canonical_query
            and settings.INNOVATEMR_CALLBACK_HASH_REQUIRED
        ):
            verification = verify_callback_request(request)
            if not verification.valid:
                logger.warning(
                    "Rejected InnovateMR callback rid=%s reason=%s ip=%s",
                    attempt.rid,
                    verification.error,
                    ip_address or "unknown",
                )
                return render(request, "surveys/flow_error.html", {
                    "title": "Invalid survey callback",
                    "message": "This survey result could not be verified and was not recorded.",
                }, status=403)
            innovate_callback_verified = True
        callback_transition_applied = False
        if not canonical_query:
            with transaction.atomic():
                attempt = SurveyAttempt.objects.select_for_update().select_related(
                    "survey__integration"
                ).get(pk=attempt.pk)
                now = timezone.now()
                toluna_already_finalized = bool(
                    provider_code == "toluna"
                    and attempt.is_verified
                    and (
                        attempt.status_source == "toluna_callback"
                        or attempt.status_source.startswith("toluna_notification_")
                    )
                )
                callback_transition_applied = not toluna_already_finalized
                exit_client_data = get_request_client_data(request)
                if innovate_callback_verified:
                    exit_client_data["innovatemr_callback"] = {
                        "status": status_code,
                        "termReason": str(
                            request.GET.get("termReason")
                            or request.GET.get("term_reason")
                            or request.GET.get("reason")
                            or ""
                        ).strip()[:1000],
                        "closeQuotaId": str(request.GET.get("closeQuotaId") or "").strip()[:160],
                        "surveyId": str(request.GET.get("surveyId") or "").strip()[:160],
                        "verifiedAt": now.isoformat(),
                    }
                if callback_transition_applied and (
                    attempt.callback_at is None or provider_code == "toluna"
                ):
                    attempt.callback_at = now
                    attempt.callback_ip = ip_address
                    attempt.loi_seconds = attempt.calculate_loi_seconds(now)
                    attempt.status = status_code
                    attempt.exit_user_agent = exit_client_data.get("user_agent", "")
                    attempt.exit_browser = exit_client_data.get("browser", "")
                    attempt.exit_device = exit_client_data.get("device", "")
                    attempt.exit_os = exit_client_data.get("os", "")
                    attempt.exit_client_data = exit_client_data
                    attempt.status_source = (
                        "toluna_callback"
                        if provider_code == "toluna"
                        else "innovatemr_signed_redirect"
                        if innovate_callback_verified
                        else "browser_callback"
                    )
                    if provider_code == "toluna":
                        callback_data = dict(request.GET.items())
                        for callback_key in list(callback_data):
                            if callback_key.casefold() == "hash":
                                callback_data[callback_key] = "[redacted]"
                        attempt.is_verified = callback_verified
                        toluna_outcome = describe_toluna_callback(
                            callback_data,
                            code=status_code,
                            status=page["status_label"],
                            title=page["title"],
                        )
                        attempt.upstream_transaction_data = {
                            **(attempt.upstream_transaction_data or {}),
                            "toluna_callback": callback_data,
                            "toluna_outcome": toluna_outcome,
                        }
                if callback_transition_applied and innovate_callback_verified:
                    callback_data = dict(request.GET.items())
                    for callback_key in list(callback_data):
                        if callback_key.casefold() in {"hash", "hashdata"}:
                            callback_data[callback_key] = "[redacted]"
                    attempt.upstream_transaction_data = {
                        **(attempt.upstream_transaction_data or {}),
                        "innovatemr_browser_return": callback_data,
                    }
                    attempt.exit_client_data = exit_client_data
                    attempt.is_verified = True
                if callback_transition_applied:
                    attempt.last_callback_at = now
                    attempt.callback_count += 1
                    attempt.save(update_fields=[
                        "callback_at", "callback_ip", "loi_seconds", "status", "exit_user_agent", "exit_browser",
                        "exit_device", "exit_os", "exit_client_data", "status_source", "last_callback_at",
                        "callback_count", "is_verified", "upstream_transaction_data", "updated_at"
                    ])
                    finalize_attempt_capacity(attempt)
        if not canonical_query and callback_transition_applied:
            supplier_callback_url = _external_supplier_result_url(attempt, status_code)
            if supplier_callback_url:
                return HttpResponseRedirect(supplier_callback_url)
        if provider_code == "toluna" and not canonical_query:
            # Provider redirects are signed, state-changing requests. Convert
            # the first verified callback (and any replay of it) into a clean,
            # read-only local result URL so browser refresh/prefetch cannot
            # increment counters or finalize capacity twice.
            return HttpResponseRedirect(_recorded_status_url(attempt, attempt.status))
        if provider_code == "toluna":
            page = TOLUNA_STATUS_PAGES.get(attempt.status, page)
        else:
            page = STATUS_PAGES.get(attempt.status, page)
        status_label = page.get("status_label") or attempt.get_status_display()
    else:
        status_label = "Unknown attempt"

    page, status_label = _status_presentation_for_attempt(attempt, page, status_label)

    outcome = provider_outcome(attempt) if attempt else {}
    return render(request, "surveys/status.html", {
        **page,
        "status_label": status_label,
        "rid": canonical_rid,
        "ip_address": ip_address,
        "loi_seconds": attempt.loi_seconds if attempt else None,
        "attempt_found": bool(attempt),
        "toluna_notification": toluna_notification,
        "provider_reason": outcome.get("reason", ""),
    }, status=200 if attempt else 404)


@extend_schema_view(
    list=extend_schema(
        tags=["Surveys"],
        summary="List synchronized surveys",
        description=(
            "Returns locally stored surveys using page-number pagination. Search matches project ID, upstream survey ID, "
            "survey name, country and category. Date filters accept ISO-8601 timestamps."
        ),
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, description="Free-text search across survey identifiers and descriptive fields."),
            OpenApiParameter("ordering", OpenApiTypes.STR, description="One of source_modified_at, source_created_at, cpi, sample_size, completes, created_at; prefix '-' for descending."),
            OpenApiParameter("page", OpenApiTypes.INT, description="1-based result page."),
            OpenApiParameter("page_size", OpenApiTypes.INT, description="Rows per page (1–100, default 20)."),
            OpenApiParameter("buyer_id", OpenApiTypes.STR, description="Comma-separated buyer/sub-client IDs."),
            OpenApiParameter("survey_type", OpenApiTypes.STR, description="Comma-separated normalized audience types, for example B2B,B2C."),
        ],
    ),
    retrieve=extend_schema(
        tags=["Surveys"],
        summary="Get one survey",
        description="Looks up a survey by the platform's immutable 14-digit local_id and embeds current quotas and targeting questions.",
    ),
)
class SurveyViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Survey.objects.all()
    lookup_field = "local_id"
    filterset_class = SurveyFilter
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    # Some providers (notably Toluna) use a composite stable key such as
    # ``SurveyID:WaveID`` while the project table intentionally displays the
    # SurveyID portion. Prefix search keeps that displayed identifier
    # searchable without discarding the WaveID needed for row uniqueness.
    search_fields = ["local_id", "^source_key", "=source_id", "name", "company_name", "buyer_id", "survey_type", "country", "country_code", "job_category"]
    ordering_fields = ["source_modified_at", "source_created_at", "cpi", "sample_size", "completes", "created_at"]
    ordering = ["-source_modified_at", "-created_at"]
    permission_classes = [HasFunctionPermission]

    def get_queryset(self):
        queryset = scope_surveys_for_user(super().get_queryset(), self.request.user)
        queryset = scope_surveys_for_api_key(queryset, self.request.auth)

        # Visible CPI needs SQL expressions only when the database must filter,
        # order or export by it. Ordinary project-list rows are priced by the
        # serializer from the already scoped/prefetched allocation context.
        cpi_ordering = self.request.query_params.get("ordering", "").lstrip("-") == "cpi"
        cpi_filtering = any(
            self.request.query_params.get(name) not in {None, ""}
            for name in ("min_cpi", "max_cpi")
        )
        if self.action in {"retrieve", "export"} or cpi_ordering or cpi_filtering:
            queryset = annotate_survey_pricing_for_user(queryset, self.request.user)

        # MySQL is markedly faster at resolving the two nullable foreign keys
        # for a page in separate bounded queries than through one wide LEFT
        # JOIN. Detail/export paths still use joins because they resolve one
        # project or intentionally stream the full export queryset.
        if self.action == "list":
            queryset = queryset.prefetch_related("client", "integration")
        elif self.action in {"retrieve", "quotas", "targeting", "export"}:
            queryset = queryset.select_related("client", "integration")
        if self.action in {"retrieve", "quotas", "targeting"}:
            queryset = queryset.prefetch_related("quotas", "targeting_questions")

        # Export is unpaginated, so retain the correlated completes annotation
        # there. List pages attach the same totals with one grouped query for
        # only the 20-100 surveys actually returned.
        if self.action in {"retrieve", "export"}:
            completed_attempts = (
                SurveyAttempt.objects.filter(
                    survey_id=OuterRef("pk"),
                    status=SurveyAttempt.Status.COMPLETED,
                )
                .values("survey_id")
                .annotate(total=Count("pk"))
                .values("total")[:1]
            )
            queryset = queryset.annotate(
                platform_completes=Coalesce(
                    Subquery(completed_attempts, output_field=IntegerField()),
                    Value(0),
                )
            )
        return queryset

    @staticmethod
    def _attach_page_platform_completes(surveys):
        """Attach exact platform completes with one query for this page only."""

        survey_ids = [survey.pk for survey in surveys]
        if not survey_ids:
            return
        totals = dict(
            SurveyAttempt.objects.filter(
                survey_id__in=survey_ids,
                status=SurveyAttempt.Status.COMPLETED,
            )
            .values("survey_id")
            .annotate(total=Count("pk"))
            .values_list("survey_id", "total")
        )
        for survey in surveys:
            survey.platform_completes = totals.get(survey.pk, 0)

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            self._attach_page_platform_completes(page)
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        rows = list(queryset)
        self._attach_page_platform_completes(rows)
        serializer = self.get_serializer(rows, many=True)
        return Response(serializer.data)

    def get_required_function_permission(self):
        if self.action == "export":
            return "projects.export"
        return "survey_details.view" if self.action in {"retrieve", "quotas", "targeting"} else "projects.view"

    def filter_queryset(self, queryset):
        _enforce_query_permissions(self.request, {
            "projects.filter.search": ("search",),
            "projects.filter.country": ("country",),
            "projects.filter.status": ("status",),
            "projects.filter.client": ("company", "client_name"),
            "projects.filter.buyer": ("buyer_id",),
            "projects.filter.survey_type": ("survey_type",),
            "projects.filter.date": ("created_from", "created_to", "modified_from", "modified_to"),
        })
        cpi_ordering = self.request.query_params.get("ordering", "").lstrip("-") == "cpi"
        cpi_filtering = any(self.request.query_params.get(name) not in {None, ""} for name in ("min_cpi", "max_cpi"))
        if (cpi_ordering or cpi_filtering) and not has_function_access(self.request.user, "projects.filter.cpi"):
            raise PermissionDenied("Your account cannot filter or sort projects by CPI.")
        queryset = super().filter_queryset(queryset)
        if cpi_ordering:
            direction = "-" if self.request.query_params.get("ordering", "").startswith("-") else ""
            queryset = queryset.order_by(
                f"{direction}visible_cpi",
                "-source_modified_at",
                "-created_at",
            )
        return queryset

    def get_serializer_class(self):
        return SurveyDetailSerializer if self.action == "retrieve" else SurveyListSerializer

    @extend_schema(
        tags=["Surveys"],
        summary="Search buyer IDs visible in Projects",
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, description="Buyer ID substring."),
            OpenApiParameter("company", OpenApiTypes.STR, description="Comma-separated client/company names."),
            OpenApiParameter("client_name", OpenApiTypes.STR, description="Comma-separated allocated client names."),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    @action(detail=False, methods=["get"], url_path="buyer-options")
    def buyer_options(self, request):
        """Return a bounded, scoped buyer-ID search instead of embedding all IDs."""

        if not has_function_access(request.user, "projects.filter.buyer"):
            raise PermissionDenied("Your account cannot use the projects buyer filter.")

        queryset = self.get_queryset().exclude(buyer_id="")
        client_scoped = bool(
            vendor_scope_user_id(request.user)
            or organization_client_ids_for_user(request.user) is not None
        )
        client_parameter = "client_name" if client_scoped else "company"
        selected_clients = [
            value.strip()
            for value in str(request.query_params.get(client_parameter) or "").split(",")
            if value.strip()
        ]
        if selected_clients:
            if not has_function_access(request.user, "projects.filter.client"):
                raise PermissionDenied("Your account cannot use the projects client filter.")
            queryset = queryset.filter(**{
                "client__name__in" if client_scoped else "company_name__in": selected_clients
            })

        search = str(request.query_params.get("search") or "").strip()[:160]
        if search:
            queryset = queryset.filter(buyer_id__icontains=search)

        limit = 200
        buyer_values = list(
            queryset.values_list("buyer_id", flat=True)
            .distinct()
            .order_by("buyer_id")[: limit + 1]
        )
        selected_client = selected_clients[0] if len(selected_clients) == 1 else ""
        return Response({
            "results": [
                {
                    "value": buyer_id,
                    "client_value": selected_client,
                }
                for buyer_id in buyer_values[:limit]
            ],
            "has_more": len(buyer_values) > limit,
        })

    @extend_schema(
        tags=["Surveys"],
        summary="Export all filtered projects",
        description=(
            "Downloads an Excel workbook containing every survey matching the current Projects filters and "
            "ordering. Pagination is ignored and columns follow the requesting user's project permissions."
        ),
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, description="Search project ID, survey ID, name, country or category."),
            OpenApiParameter("country", OpenApiTypes.STR, description="Comma-separated country codes."),
            OpenApiParameter("status", OpenApiTypes.STR, description="Comma-separated survey statuses."),
            OpenApiParameter("company", OpenApiTypes.STR, description="Comma-separated client/company names."),
            OpenApiParameter("buyer_id", OpenApiTypes.STR, description="Comma-separated buyer/sub-client IDs."),
            OpenApiParameter("survey_type", OpenApiTypes.STR, description="Comma-separated normalized audience types, for example B2B,B2C."),
            OpenApiParameter("created_from", OpenApiTypes.DATETIME, description="Source-created timestamp lower bound."),
            OpenApiParameter("created_to", OpenApiTypes.DATETIME, description="Source-created timestamp upper bound."),
            OpenApiParameter("modified_from", OpenApiTypes.DATETIME, description="Source-modified timestamp lower bound."),
            OpenApiParameter("modified_to", OpenApiTypes.DATETIME, description="Source-modified timestamp upper bound."),
            OpenApiParameter("min_cpi", OpenApiTypes.NUMBER, description="Minimum viewer-visible CPI after configured cuts, inclusive."),
            OpenApiParameter("max_cpi", OpenApiTypes.NUMBER, description="Maximum viewer-visible CPI after configured cuts, inclusive."),
            OpenApiParameter("ordering", OpenApiTypes.STR, description="Current Projects ordering, including viewer-visible cpi or -cpi."),
        ],
        responses={(200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): OpenApiTypes.BINARY},
    )
    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request, *args, **kwargs):
        if not has_function_access(request.user, "projects.view"):
            raise PermissionDenied("Project visibility is required before projects can be exported.")
        queryset = self.filter_queryset(self.get_queryset())
        columns = [column for column in _project_columns_for_user(request.user) if column != "actions"]
        local_now = timezone.localtime()
        headers, rows, widths = _survey_excel_rows(queryset, request, columns)
        return build_excel_response(
            f"projects-{local_now:%Y%m%d-%H%M%S}-IST.xlsx",
            [ExcelSheet("Projects", headers, rows, widths)],
        )

    @staticmethod
    def _refresh_if_stale(survey, detail_type):
        synced_at = survey.quota_synced_at if detail_type == "quotas" else survey.targeting_synced_at
        stale = synced_at is None or (
            survey.source_modified_at is not None and synced_at < survey.source_modified_at
        )
        if survey.integration_id and survey.integration.provider_code == "biobrain":
            if detail_type == "targeting":
                stale = stale or any(
                    not question.text
                    or str(question.text).startswith("Qualification ")
                    or bool(re.fullmatch(r"Q\d+", str(question.key or ""), re.IGNORECASE))
                    or (question.raw_data or {}).get("metadata_hydrated") is not True
                    or any(not isinstance(option, dict) for option in (question.options or []))
                    for question in survey.targeting_questions.all()
                )
            else:
                stale = stale or any(
                    not isinstance((quota.raw_data or {}).get("targeting_details"), list)
                    or (quota.raw_data or {}).get("metadata_hydrated") is not True
                    for quota in survey.quotas.all()
                )
        if (
            detail_type == "targeting"
            and survey.integration_id
            and survey.integration.provider_code == "toluna"
        ):
            stale = stale or not survey.targeting_questions.filter(
                raw_data__adapter_version=TOLUNA_ADAPTER_VERSION
            ).exists()
        if (
            detail_type == "targeting"
            and survey.integration_id
            and survey.integration.provider_code == "rfg"
        ):
            stale = stale or not survey.targeting_questions.filter(
                key="RFG_BIRTHDAY",
                raw_data__adapter_version=RFG_TARGETING_ADAPTER_VERSION,
            ).exists()
        if stale:
            if survey.integration_id and survey.integration.provider_code in {"rfg", "toluna"}:
                get_provider(survey.integration).refresh_details(survey)
            else:
                refresh = replace_survey_quotas if detail_type == "quotas" else replace_survey_targeting
                refresh(InnovateMRClient(integration=survey.integration), survey)
            # get_object() prefetched both related collections before the
            # provider atomically replaced them. Drop those snapshots so this
            # same response serializes the newly-created quota/question rows.
            prefetched = getattr(survey, "_prefetched_objects_cache", {})
            prefetched.pop("quotas", None)
            prefetched.pop("targeting_questions", None)
        return stale

    @extend_schema(
        tags=["Survey details"],
        summary="List a survey's quotas",
        description="Returns the most recently synchronized, provider-normalized quota data for this survey.",
        responses={200: SurveyQuotaSerializer(many=True)},
    )
    @action(detail=True, methods=["get"])
    def quotas(self, request, local_id=None):
        survey = self.get_object()
        try:
            self._refresh_if_stale(survey, "quotas")
        except (InnovateMRAPIError, ProviderError) as exc:
            if survey.quota_synced_at is None and not survey.quotas.exists():
                raise UpstreamUnavailable(str(exc)) from exc
        return Response(SurveyQuotaSerializer(survey.quotas.all(), many=True).data)

    @extend_schema(
        tags=["Survey details"],
        summary="List pre-screening questions and accepted answers",
        description="Returns provider-normalized pre-screening questions. Answer codes preserve the upstream provider mapping.",
        responses={200: TargetingQuestionSerializer(many=True)},
    )
    @action(detail=True, methods=["get"], url_path="targeting")
    def targeting(self, request, local_id=None):
        survey = self.get_object()
        try:
            self._refresh_if_stale(survey, "targeting")
        except (InnovateMRAPIError, ProviderError) as exc:
            if (
                survey.targeting_synced_at is None
                and not survey.targeting_questions.exists()
            ):
                raise UpstreamUnavailable(str(exc)) from exc
        return Response(TargetingQuestionSerializer(survey.targeting_questions.all(), many=True).data)


class SyncTriggerView(APIView):
    permission_classes = [HasFunctionPermission]
    required_function_permission = "sync.run"
    @extend_schema(
        tags=["Synchronization"],
        summary="Start an InnovateMR inventory synchronization",
        description=(
            "By default queues the same Celery task that beat runs every minute. Use wait=true for operational testing to run in the HTTP process "
            "and receive counters immediately. The sync fetches both full and cursor-paged inventory, deduplicates by surveyId using modifiedDate, "
            "and refreshes quota/targeting only for new or changed surveys."
        ),
        parameters=[OpenApiParameter("wait", OpenApiTypes.BOOL, description="Run synchronously and return the completed run summary.")],
        request=None,
        responses={200: SyncTriggerResponseSerializer, 202: OpenApiTypes.OBJECT},
        examples=[OpenApiExample("Synchronous result", value={"run_id": 42, "status": "success", "created": 3, "updated": 8, "unchanged": 110, "closed": 2, "detail_failures": 0}, response_only=True)],
    )
    def post(self, request):
        wait = str(request.query_params.get("wait", "false")).lower() in {"1", "true", "yes"}
        if wait:
            try:
                summary = sync_surveys()
            except InnovateMRAPIError as exc:
                raise UpstreamUnavailable(str(exc)) from exc
            return Response(SyncTriggerResponseSerializer(summary.__dict__).data)
        task = sync_innovatemr_surveys_task.delay()
        return Response({"task_id": task.id, "status": "queued"}, status=status.HTTP_202_ACCEPTED)


class SyncRunViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = SyncRun.objects.all()
    serializer_class = SyncRunSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["status"]
    ordering_fields = ["started_at", "finished_at", "created", "updated", "detail_failures"]
    ordering = ["-started_at"]
    permission_classes = [HasFunctionPermission]
    required_function_permission = "sync.view"

    @extend_schema(tags=["Synchronization"], summary="List synchronization audit runs")
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=["Synchronization"], summary="Get one synchronization audit run")
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)


@extend_schema_view(
    list=extend_schema(
        tags=["Survey attempts"],
        summary="List respondent survey attempts",
        description=(
            "Staff-only audit data for initiated pre-screeners, redirects, callbacks, IPs, measured LOI, "
            "survey country and the CPI snapshot frozen when the respondent entered."
        ),
    ),
    retrieve=extend_schema(
        tags=["Survey attempts"],
        summary="Get one respondent attempt by RID",
        description="Staff-only detail including captured answers and outbound supplier URL.",
    ),
)
class SurveyAttemptViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = SurveyAttemptSerializer
    permission_classes = [HasFunctionPermission]
    lookup_field = "rid"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = SurveyAttemptFilter
    search_fields = [
        "rid", "prescreener_uid", "user_id", "survey__local_id", "^survey__source_key", "=survey__source_id", "survey__buyer_id", "survey__name", "survey__company_name",
        "platform_user__username", "platform_user__first_name", "platform_user__last_name", "platform_user__email",
        "initiation_ip", "callback_ip", "entry_browser", "entry_device", "entry_os",
    ]
    ordering_fields = ["initiated_at", "callback_at", "loi_seconds", "status"]
    ordering = ["-initiated_at"]

    def _filtered_summary(self, queryset):
        completed_filter = Q(status=SurveyAttempt.Status.COMPLETED)
        survey_termination_filter = Q(status=SurveyAttempt.Status.TERMINATED) & ~Q(
            status_source="local_prescreener"
        )
        summary = queryset.aggregate(
            total=Count("id"),
            initiated=Count("id", filter=Q(status__in=[SurveyAttempt.Status.INITIATED, SurveyAttempt.Status.REDIRECTED])),
            completed=Count("id", filter=completed_filter),
            terminated=Count("id", filter=Q(status=SurveyAttempt.Status.TERMINATED)),
            survey_terminated=Count("id", filter=survey_termination_filter),
            over_quota=Count("id", filter=Q(status=SurveyAttempt.Status.OVER_QUOTA)),
            security_terminated=Count("id", filter=Q(status=SurveyAttempt.Status.QUALITY_TERMINATED)),
            desktop=Count("id", filter=completed_filter & Q(entry_device__icontains="desktop")),
            mobile=Count("id", filter=completed_filter & (Q(entry_device__icontains="mobile") | Q(entry_device__icontains="phone"))),
            tablet=Count("id", filter=completed_filter & (Q(entry_device__icontains="tablet") | Q(entry_device__iexact="tab"))),
            total_revenue=Sum("source_cpi_snapshot", filter=completed_filter, default=Decimal("0.00")),
            supplier_revenue=Sum(
                Coalesce("payable_cpi_snapshot", "source_cpi_snapshot"),
                filter=completed_filter,
                default=Decimal("0.00"),
            ),
            revenue_currency=Max("cpi_currency_snapshot", filter=completed_filter),
        )
        completed = summary["completed"]
        ir_denominator = completed + summary["survey_terminated"]
        classified = summary["desktop"] + summary["mobile"] + summary["tablet"]
        if not can_view_report_commercials(self.request.user):
            revenue = (
                summary["supplier_revenue"]
                if is_external_vendor_scope(self.request.user)
                else summary["total_revenue"]
            )
            summary["total_revenue"] = apply_percentage(
                revenue,
                role_visibility_percent(self.request.user),
            )
        card_access = _component_access(
            effective_permission_codes(self.request.user), STUDY_CARD_PERMISSIONS
        )
        visible = lambda card, value: value if card_access[card] else None
        return {
            "total": visible("total", summary["total"]),
            "initiated": visible("initiated", summary["initiated"]),
            "completed": visible("completed", completed),
            "terminated": visible("terminated", summary["terminated"]),
            "over_quota": visible("quota", summary["over_quota"]),
            "security_terminated": visible("security", summary["security_terminated"]),
            "conversion_rate": visible(
                "conversion",
                round((completed / summary["total"] * 100), 2) if summary["total"] else 0.0,
            ),
            "incidence_rate": visible(
                "ir", round((completed / ir_denominator * 100), 2) if ir_denominator else 0.0,
            ),
            "total_revenue": visible("revenue", summary["total_revenue"]),
            "revenue_currency": visible(
                "revenue", summary["revenue_currency"] or "USD"
            ),
            "completed_devices": {
                "desktop": visible("desktop", summary["desktop"]),
                "mobile": visible("mobile", summary["mobile"]),
                "tablet": visible("tablet", summary["tablet"]),
                "unclassified": max(0, completed - classified),
            },
        }

    @extend_schema(tags=["Survey attempts"], summary="List visible survey attempts with filter-aware totals", responses={200: SurveyAttemptListResponseSerializer})
    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        summary = self._filtered_summary(queryset)
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            response = self.get_paginated_response(serializer.data)
            response.data["summary"] = summary
            return response
        return Response({"count": queryset.count(), "next": None, "previous": None, "results": self.get_serializer(queryset, many=True).data, "summary": summary})

    def get_required_function_permission(self):
        return "attempts.export" if self.action == "export" else "attempts.view"

    def get_queryset(self):
        queryset = SurveyAttempt.objects.select_related(
            "survey", "survey__client", "survey__integration", "platform_user", "platform_user__employee_profile", "platform_user__employee_profile__role",
            "platform_user__employee_profile__organization_unit", "platform_user__employee_profile__organization_unit__parent",
            "platform_user__employee_profile__organization_unit__parent__parent",
            "vendor", "vendor__employee_profile", "client", "client_allocation", "survey_allocation",
        ).all()
        if self.request.user.is_superuser:
            return queryset
        visible_user_ids = activity_visible_user_ids(self.request.user)
        return queryset.filter(platform_user_id__in=visible_user_ids)

    def filter_queryset(self, queryset):
        _enforce_query_permissions(self.request, {
            "studies.filter.search": ("search",),
            "studies.filter.branch": ("branch",),
            "studies.filter.sub_branch": ("sub_branch",),
            "studies.filter.shift": ("shift",),
            "studies.filter.user": ("user",),
            "studies.filter.status": ("status",),
            "studies.filter.country": ("country",),
            "studies.filter.client": ("client",),
            "studies.filter.buyer": ("buyer_id",),
            "studies.filter.project": ("internal_id",),
            "studies.filter.date": ("initiated_from", "initiated_to", "callback_from", "callback_to"),
        })
        return super().filter_queryset(queryset)

    @extend_schema(
        tags=["Survey attempts"],
        summary="Export all filtered survey attempt data",
        description=(
            "Downloads the agreed Traffic Reports Excel columns for every filtered attempt, including immutable "
            "hit-time CPI, supplier CPI, respondent device/network audit and lifecycle timestamps."
        ),
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, description="Search RID, user, survey, IP or client metadata."),
            OpenApiParameter("user", OpenApiTypes.STR, description="Comma-separated platform user IDs."),
            OpenApiParameter("branch", OpenApiTypes.STR, description="Comma-separated organization Branch IDs or legacy labels."),
            OpenApiParameter("sub_branch", OpenApiTypes.STR, description="Comma-separated organization Sub-branch IDs or legacy labels."),
            OpenApiParameter("shift", OpenApiTypes.STR, description="Comma-separated organization Shift IDs or legacy labels."),
            OpenApiParameter("status", OpenApiTypes.STR, description="Comma-separated attempt status codes."),
            OpenApiParameter("country", OpenApiTypes.STR, description="Comma-separated survey country codes."),
            OpenApiParameter("company", OpenApiTypes.STR, description="Comma-separated survey company names."),
            OpenApiParameter("client", OpenApiTypes.STR, description="Comma-separated internal client IDs."),
            OpenApiParameter("buyer_id", OpenApiTypes.STR, description="Comma-separated buyer/sub-client IDs."),
            OpenApiParameter(
                "survey_id",
                OpenApiTypes.STR,
                description="Exact provider key; Toluna also accepts its SurveyID or WaveID.",
            ),
            OpenApiParameter("internal_id", OpenApiTypes.STR, description="Exact internal 14-digit project ID."),
            OpenApiParameter("entry_ip", OpenApiTypes.STR, description="Exact entry IP address."),
            OpenApiParameter("exit_ip", OpenApiTypes.STR, description="Exact exit IP address."),
            OpenApiParameter("initiated_from", OpenApiTypes.DATETIME, description="Entry timestamp lower bound (ISO 8601)."),
            OpenApiParameter("initiated_to", OpenApiTypes.DATETIME, description="Entry timestamp upper bound (ISO 8601)."),
            OpenApiParameter("callback_from", OpenApiTypes.DATETIME, description="Exit timestamp lower bound (ISO 8601)."),
            OpenApiParameter("callback_to", OpenApiTypes.DATETIME, description="Exit timestamp upper bound (ISO 8601)."),
            OpenApiParameter("ordering", OpenApiTypes.STR, description="Sort by initiated_at, callback_at, loi_seconds or status; prefix - for descending."),
        ],
        responses={(200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): OpenApiTypes.BINARY},
    )
    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        local_now = timezone.localtime()
        headers, rows, widths = _attempt_excel_rows(queryset, request.user)
        if not headers:
            raise PermissionDenied("No Traffic Report columns are assigned to your account.")
        return build_excel_response(
            f"traffic-reports-{local_now:%Y%m%d-%H%M%S}-IST.xlsx",
            [ExcelSheet("Traffic Reports", headers, rows, widths)],
        )


class DashboardAPIView(APIView):
    permission_classes = [HasFunctionPermission]
    required_function_permission = "dashboard.view"

    @extend_schema(
        tags=["Dashboard"],
        summary="Get permission-scoped dashboard analytics",
        description=(
            "Returns permission-scoped KPI totals, incidence rate, immutable hit-time CPI revenue, "
            "client completion share, performance, outcome/device breakdowns and top users."
        ),
        parameters=[
            OpenApiParameter(
                "range", OpenApiTypes.STR,
                description="Global analytics window: 24h, 48h, 72h, 3m, 6m or 1y. Defaults to 24h.",
                enum=["24h", "48h", "72h", "3m", "6m", "1y"],
            ),
            OpenApiParameter(
                "traffic_range", OpenApiTypes.STR,
                description="Independent Traffic graph window; does not change dashboard cards.",
                enum=["24h", "48h", "72h", "3m", "6m", "1y"],
            ),
            OpenApiParameter(
                "traffic_client", OpenApiTypes.INT,
                description="Visible internal client ID for the Traffic graph only.",
            ),
            OpenApiParameter(
                "finance_range", OpenApiTypes.STR,
                description="Independent Revenue/RPC graph window; does not change dashboard cards.",
                enum=["24h", "48h", "72h", "3m", "6m", "1y"],
            ),
            OpenApiParameter(
                "finance_client", OpenApiTypes.INT,
                description="Visible internal client ID for the Revenue/RPC graph only.",
            ),
        ],
        responses={200: DashboardResponseSerializer},
    )
    def get(self, request):
        codes = effective_permission_codes(request.user)
        if any(request.query_params.get(key) not in {None, ""} for key in (
            "traffic_range", "traffic_client"
        )) and DASHBOARD_GRAPH_FILTER_PERMISSIONS["traffic"] not in codes:
            raise PermissionDenied("Your account cannot filter the Traffic dashboard graph.")
        if any(request.query_params.get(key) not in {None, ""} for key in (
            "finance_range", "finance_client"
        )) and DASHBOARD_GRAPH_FILTER_PERMISSIONS["finance"] not in codes:
            raise PermissionDenied("Your account cannot filter the Finance dashboard graph.")
        try:
            range_window = dashboard_range_window(request.query_params.get("range", "24h"))
            traffic_window = dashboard_range_window(
                request.query_params.get("traffic_range") or range_window["key"]
            )
            finance_window = dashboard_range_window(
                request.query_params.get("finance_range") or range_window["key"]
            )
            visible_queryset = dashboard_attempts(request.user, {})
            client_options = dashboard_client_options(visible_queryset)
            visible_client_ids = {item["id"] for item in client_options}

            def selected_client(parameter):
                raw_value = str(request.query_params.get(parameter) or "").strip()
                if not raw_value:
                    return None
                try:
                    client_id = int(raw_value)
                except ValueError as exc:
                    raise ValueError("Graph client must be a numeric client ID.") from exc
                if client_id not in visible_client_ids:
                    raise ValueError("The selected graph client is not visible to this account.")
                return client_id

            traffic_client_id = selected_client("traffic_client")
            finance_client_id = selected_client("finance_client")

            def graph_queryset(window, client_id=None):
                scoped = visible_queryset.filter(
                    initiated_at__gte=window["start"], initiated_at__lte=window["end"]
                )
                return scoped.filter(survey__client_id=client_id) if client_id else scoped

            queryset = graph_queryset(range_window)
            traffic_queryset = graph_queryset(traffic_window, traffic_client_id)
            finance_queryset = graph_queryset(finance_window, finance_client_id)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(build_dashboard_payload(
            queryset,
            request.user,
            _component_access(codes, DASHBOARD_CARD_PERMISSIONS),
            _component_access(codes, DASHBOARD_CHART_PERMISSIONS),
            range_window,
            traffic_queryset=traffic_queryset,
            traffic_range_window=traffic_window,
            traffic_client_id=traffic_client_id,
            finance_queryset=finance_queryset,
            finance_range_window=finance_window,
            finance_client_id=finance_client_id,
            client_options=client_options,
        ))


class UserHitsAPIView(APIView):
    permission_classes = [HasFunctionPermission]
    required_function_permission = "user_hits.view"

    @extend_schema(
        tags=["User hits"],
        summary="Aggregate user survey hits and completes by IST date and device",
        description=(
            "Returns one row per visible user and IST calendar date. Hits count initiated survey attempts; "
            "completes count status 1 within those attempts. Device splits use entry-device audit data."
        ),
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, description="Search user, email, branch, sub-branch or shift."),
            OpenApiParameter("user", OpenApiTypes.STR, description="Comma-separated platform user IDs."),
            OpenApiParameter("branch", OpenApiTypes.STR, description="Comma-separated branch/company labels."),
            OpenApiParameter("sub_branch", OpenApiTypes.STR, description="Comma-separated sub-branch/department labels."),
            OpenApiParameter("shift", OpenApiTypes.STR, description="Comma-separated organization shift labels."),
            OpenApiParameter("from_date", OpenApiTypes.DATE, description="Inclusive IST entry date."),
            OpenApiParameter("to_date", OpenApiTypes.DATE, description="Inclusive IST entry date."),
            OpenApiParameter("from_time", OpenApiTypes.TIME, description="Optional inclusive IST start time; requires from_date."),
            OpenApiParameter("to_time", OpenApiTypes.TIME, description="Optional inclusive IST end time; requires to_date."),
            OpenApiParameter("page", OpenApiTypes.INT, description="1-based aggregate result page."),
            OpenApiParameter("page_size", OpenApiTypes.INT, description="Rows per page, 1–100."),
        ],
        responses={200: UserHitsResponseSerializer},
    )
    def get(self, request):
        _enforce_query_permissions(request, {
            "user_hits.filter.search": ("search",),
            "user_hits.filter.user": ("user",),
            "user_hits.filter.branch": ("branch",),
            "user_hits.filter.sub_branch": ("sub_branch",),
            "user_hits.filter.shift": ("shift",),
            "user_hits.filter.date": ("from_date", "from_time", "to_date", "to_time"),
        })
        try:
            rows, summary = aggregate_user_hits(request.user, request.query_params)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        codes = effective_permission_codes(request.user)
        if USER_HIT_CARD_PERMISSIONS["total_hits"] not in codes:
            summary["hits"]["total"] = None
        if USER_HIT_CARD_PERMISSIONS["completes"] not in codes:
            summary["completes"]["total"] = None
        if USER_HIT_CARD_PERMISSIONS["conversion"] not in codes:
            summary["conversion_rate"] = None
        if USER_HIT_CARD_PERMISSIONS["active_users"] not in codes:
            summary["active_users"] = None
        if USER_HIT_CARD_PERMISSIONS["devices"] not in codes:
            for device in ("desktop", "mobile", "tablet", "unclassified"):
                summary["completes"][device] = None
        if USER_HIT_CARD_PERMISSIONS["ir"] not in codes:
            summary["incidence_rate"] = None
        paginator = SurveyPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        response = paginator.get_paginated_response(page)
        response.data["summary"] = summary
        return response


class _CsvEcho:
    def write(self, value):
        return value


def _csv_safe(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    elif hasattr(value, "isoformat"):
        value = timezone.localtime(value).isoformat() if timezone.is_aware(value) else value.isoformat()
    else:
        value = str(value)
    return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value


def _excel_datetime(value):
    if not value:
        return ""
    local_value = timezone.localtime(value) if timezone.is_aware(value) else value
    return local_value.strftime("%d %b %Y %I:%M:%S %p IST")


def _survey_excel_rows(queryset, request, columns):
    can_view_client_name = has_function_access(request.user, "projects.column.client_name")
    survey_headers = ["Survey ID", "Survey name"]
    survey_widths = [16, 32]
    if can_view_client_name:
        survey_headers.append("Client")
        survey_widths.append(21)
    survey_headers.append("Buyer ID")
    survey_widths.append(15)
    headers_by_column = {
        "project_id": ["Project ID"],
        "survey": survey_headers,
        "market": ["Country code", "Country", "Language code", "Language"],
        "completes": ["Sample size", "Completes", "Remaining", "Progress (%)"],
        "cpi": ["CPI"],
        "loi_ir": ["LOI (minutes)", "Incidence rate (%)", "Survey type"],
        "entry_link": ["Entry link"],
        "modified": ["Status", "Source created at", "Source modified at", "Record created at", "Record updated at"],
    }
    widths_by_column = {
        "project_id": [19], "survey": survey_widths, "market": [13, 20, 14, 18],
        "completes": [13, 12, 12, 14], "cpi": [11], "loi_ir": [15, 18, 14],
        "entry_link": [48], "modified": [14, 22, 22, 22, 22],
    }
    export_columns = [column for column in columns if column in headers_by_column]
    headers = [header for column in export_columns for header in headers_by_column[column]]
    widths = [width for column in export_columns for width in widths_by_column[column]]

    def rows():
        serializer_context = {"request": request}
        for survey in queryset.iterator(chunk_size=500):
            data = SurveyListSerializer(survey, context=serializer_context).data
            values_by_column = {
                "project_id": [data.get("local_id")],
                "survey": (
                    [data.get("source_id"), data.get("name")]
                    + ([data.get("client_name") or data.get("display_company_name") or data.get("company_name")] if can_view_client_name else [])
                    + [data.get("buyer_id")]
                ),
                "market": [data.get("country_code"), data.get("country"), data.get("language_code"), data.get("language")],
                "completes": [data.get("sample_size"), data.get("completes"), data.get("remaining"), data.get("progress_percent")],
                "cpi": [data.get("cpi")],
                "loi_ir": [data.get("loi"), data.get("incidence_rate"), data.get("survey_type") or data.get("group_type")],
                "entry_link": [data.get("start_link")],
                "modified": [
                    data.get("status"), data.get("source_created_at"), data.get("source_modified_at"),
                    data.get("created_at"), data.get("updated_at"),
                ],
            }
            yield [value for column in export_columns for value in values_by_column[column]]

    return headers, rows(), widths


def _attempt_excel_rows(queryset, requesting_user=None):
    """Build Traffic Report rows without leaking upstream commercial data."""

    commercial_admin = can_view_report_commercials(requesting_user)
    permitted = set(_permitted_columns(
        effective_permission_codes(requesting_user), STUDY_COLUMN_PERMISSIONS
    ))
    specs = {
        "project_id": (["Project id", "Client name"], [19, 21]),
        "survey_id": (["Cleint survey id"], [18]),
        "respondent_id": (["RID"], [14]),
        "status": (["Status", "Status source"], [19, 18]),
        "country": (["Country"], [18]),
        "cpi": (
            ["Current Client CPI", "Client entry link CPI"] + (["Vendor CPI", "Vendor name"] if commercial_admin else []),
            [18, 20] + ([14, 20] if commercial_admin else []),
        ),
        "user": (["User name"], [22]),
        "device": (["Device", "OS", "Browser", "User agent"], [13, 16, 18, 42]),
        "ip": (["Entry IP", "Exit IP"], [16, 16]),
        "loi": (["Actual LOI (minutes)"], [19]),
        "start": (["Inisitate at", "Presecreent at", "Redirect at", "entry date time"], [22, 22, 22, 22]),
        "end": (["Exit date time"], [22]),
    }
    ordered_columns = [column for column in STUDY_COLUMN_PERMISSIONS if column in permitted]
    headers = [header for column in ordered_columns for header in specs[column][0]]
    widths = [width for column in ordered_columns for width in specs[column][1]]

    def rows():
        for attempt in queryset.iterator(chunk_size=1000):
            survey = attempt.survey
            user = attempt.platform_user
            client = attempt.client or survey.client
            status_label = (
                "Initiated"
                if attempt.status in {SurveyAttempt.Status.INITIATED, SurveyAttempt.Status.REDIRECTED}
                else attempt.get_status_display()
            )
            values_by_column = {
                "project_id": [survey.local_id, client.name if client else survey.company_name],
                "survey_id": [survey.source_identifier],
                "respondent_id": [attempt.rid],
                "status": [status_label, attempt.status_source],
                "country": [survey.country or survey.country_code],
                "cpi": [
                    viewer_attempt_cpi(attempt, requesting_user, current=True),
                    viewer_attempt_cpi(attempt, requesting_user),
                    *([supplier_cpi_for_admin(attempt), supplier_label_for_admin(attempt)] if commercial_admin else []),
                ],
                "user": [(user.get_full_name() or user.username) if user else "Deleted user"],
                "device": [attempt.entry_device, attempt.entry_os, attempt.entry_browser, attempt.entry_user_agent],
                "ip": [attempt.initiation_ip, attempt.callback_ip],
                "loi": [round((attempt.loi_seconds or 0) / 60, 2)],
                "start": [
                    _excel_datetime(attempt.initiated_at), _excel_datetime(attempt.submitted_at),
                    _excel_datetime(attempt.redirected_at), _excel_datetime(attempt.created_at),
                ],
                "end": [_excel_datetime(attempt.callback_at or attempt.last_callback_at)],
            }
            yield [value for column in ordered_columns for value in values_by_column[column]]

    return headers, rows(), widths


def _survey_csv_rows(queryset, request, columns):
    headers_by_column = {
        "project_id": ["Project ID"],
        "survey": ["Survey ID", "Survey name", "Client", "Buyer ID"],
        "market": ["Country code", "Country", "Language code", "Language"],
        "completes": ["Sample size", "Completes", "Remaining", "Progress (%)"],
        "cpi": ["CPI"],
        "loi_ir": ["LOI (minutes)", "Incidence rate (%)", "Survey type"],
        "entry_link": ["Entry link"],
        "modified": ["Status", "Source created at", "Source modified at", "Record created at", "Record updated at"],
    }
    export_columns = [column for column in columns if column in headers_by_column]
    headers = [header for column in export_columns for header in headers_by_column[column]]
    writer = csv.writer(_CsvEcho())
    yield "\ufeff" + writer.writerow(headers)
    serializer_context = {"request": request}
    for survey in queryset.iterator(chunk_size=500):
        data = SurveyListSerializer(survey, context=serializer_context).data
        values_by_column = {
            "project_id": [data.get("local_id")],
            "survey": [
                data.get("source_id"), data.get("name"),
                data.get("client_name") or data.get("display_company_name") or data.get("company_name"),
                data.get("buyer_id"),
            ],
            "market": [data.get("country_code"), data.get("country"), data.get("language_code"), data.get("language")],
            "completes": [data.get("sample_size"), data.get("completes"), data.get("remaining"), data.get("progress_percent")],
            "cpi": [data.get("cpi")],
            "loi_ir": [data.get("loi"), data.get("incidence_rate"), data.get("survey_type") or data.get("group_type")],
            "entry_link": [data.get("start_link")],
            "modified": [
                data.get("status"), data.get("source_created_at"), data.get("source_modified_at"),
                data.get("created_at"), data.get("updated_at"),
            ],
        }
        values = [value for column in export_columns for value in values_by_column[column]]
        yield writer.writerow([_csv_safe(value) for value in values])


def _attempt_csv_rows(queryset, requesting_user=None):
    headers = [
        "Respondent ID (RID)", "Status code", "Status", "Termination reason", "Termination category", "Status source", "Platform user ID", "Username", "Employee name",
        "Email", "Employee ID", "Account type", "Role", "Supplier ID", "Supplier name", "Supplier account type",
        "Client ID", "Client name", "Client allocation ID", "Survey allocation ID",
        "Internal project ID", "Survey ID", "Survey name", "Company", "Buyer ID", "Survey type", "Country", "Language", "Supplier code",
        "Current survey CPI", "Source CPI snapshot", "CPI snapshot source", "CPI cut snapshot (%)", "Payable CPI snapshot",
        "CPI currency snapshot", "Expected LOI (minutes)",
        "Actual LOI (seconds)", "Entry IP", "Exit IP", "Entry browser", "Exit browser", "Entry device",
        "Exit device", "Entry OS", "Exit OS", "Entry user agent", "Exit user agent", "Entry referrer",
        "Entry accept language", "Initiated at (IST)", "Pre-screener submitted at (IST)",
        "Redirected at (IST)", "First callback at (IST)", "Last callback at (IST)", "Callback count",
        "Verified", "Last upstream check (IST)", "Upstream transaction", "Pre-screener answers",
        "Outbound supplier URL", "Entry client metadata", "Exit client metadata", "Record created at (IST)",
        "Record updated at (IST)",
    ]
    writer = csv.writer(_CsvEcho())
    yield "\ufeff" + writer.writerow(headers)
    hide_source_cpi = is_external_vendor_scope(requesting_user)
    requesting_profile = getattr(requesting_user, "employee_profile", None) if requesting_user else None
    requesting_role = getattr(requesting_profile, "role", None) if requesting_profile else None
    visible_percent = (
        requesting_role.cpi_visibility_percent
        if requesting_profile and requesting_profile.account_type == "employee" and requesting_role and not requesting_user.is_superuser
        else Decimal("100.00")
    )

    def visible_cpi(value):
        if hide_source_cpi or value is None:
            return ""
        return (Decimal(value) * visible_percent / Decimal("100.00")).quantize(Decimal("0.01"))

    for attempt in queryset.iterator(chunk_size=1000):
        outcome = provider_outcome(attempt) if attempt.status in {
            SurveyAttempt.Status.TERMINATED,
            SurveyAttempt.Status.OVER_QUOTA,
            SurveyAttempt.Status.QUALITY_TERMINATED,
        } else {"reason": "", "category": ""}
        user = attempt.platform_user
        profile = getattr(user, "employee_profile", None) if user else None
        role = getattr(profile, "role", None) if profile else None
        vendor = attempt.vendor
        vendor_profile = getattr(vendor, "employee_profile", None) if vendor else None
        survey = attempt.survey
        values = [
            attempt.rid, attempt.status,
            "Initiated" if attempt.status in {SurveyAttempt.Status.INITIATED, SurveyAttempt.Status.REDIRECTED} else attempt.get_status_display(),
            outcome["reason"], outcome["category"], attempt.status_source, user.pk if user else attempt.user_id,
            user.username if user else "", (user.get_full_name() or user.username) if user else "Deleted user",
            user.email if user else "", getattr(profile, "employee_id", ""),
            profile.get_account_type_display() if profile else "", role.name if role else "",
            vendor.pk if vendor else "", (vendor.get_full_name() or vendor.username) if vendor else "",
            vendor_profile.get_account_type_display() if vendor_profile else "",
            attempt.client_id, attempt.client.name if attempt.client else "", attempt.client_allocation_id,
            attempt.survey_allocation_id,
            survey.local_id, survey.source_identifier, survey.name, survey.company_name, survey.buyer_id, survey.survey_type or survey.group_type, survey.country_code,
            survey.language_code, attempt.supplier_code,
            visible_cpi(survey.cpi),
            visible_cpi(attempt.source_cpi_snapshot),
            attempt.cpi_snapshot_source, attempt.cpi_cut_percent_snapshot, attempt.payable_cpi_snapshot, attempt.cpi_currency_snapshot,
            survey.loi, attempt.loi_seconds,
            attempt.initiation_ip, attempt.callback_ip, attempt.entry_browser, attempt.exit_browser,
            attempt.entry_device, attempt.exit_device, attempt.entry_os, attempt.exit_os,
            attempt.entry_user_agent, attempt.exit_user_agent, attempt.entry_referrer,
            attempt.entry_accept_language, attempt.initiated_at, attempt.submitted_at, attempt.redirected_at,
            attempt.callback_at, attempt.last_callback_at, attempt.callback_count, attempt.is_verified,
            attempt.upstream_checked_at, attempt.upstream_transaction_data, attempt.answers, attempt.outbound_url,
            attempt.entry_client_data, attempt.exit_client_data,
            attempt.created_at, attempt.updated_at,
        ]
        yield writer.writerow([_csv_safe(value) for value in values])
