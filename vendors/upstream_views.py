import json
import re

from django.db.models import Q
from django.utils.text import slugify
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound
from rest_framework.response import Response

from config.api_docs import IsDocumentationAdmin
from surveys.rfg_outcomes import RFG_STATUS_MAP, describe_rfg_outcome

from .models import ClientIntegration
from .upstream import (
    INNOVATE_OPERATIONS,
    RFG_OPERATIONS,
    TOLUNA_OPERATIONS,
    UpstreamExplorerError,
    execute_operation,
    integration_metadata,
    operation_response_description,
)
from .upstream_serializers import (
    RFGCallbackPreviewSerializer,
    UpstreamErrorSerializer,
    UpstreamExecutionResponseSerializer,
    UpstreamIntegrationMetadataSerializer,
    UpstreamMutationRequestSerializer,
)


CATALOG_TAG = "Client API catalog"
INNOVATE_TAG = "InnovateMR APIs"
RFG_TAG = "RFG APIs"
RFG_CALLBACK_TAG = "RFG Callbacks"
TOLUNA_TAG = "Toluna APIs"
PROVIDER_ALIASES = {
    "innovate": "innovatemr",
    "innovate-mr": "innovatemr",
    "innovatemr": "innovatemr",
    "rfg": "rfg",
    "research-for-good": "rfg",
    "toluna": "toluna",
    "toluna-integrated-panel": "toluna",
}

PARAMETER_HELP = {
    "survey_id": "Provider survey/project ID. InnovateMR example: 16003381; RFG example: RFG2300540746-001.",
    "survey_ids": "RFG project IDs as a comma-separated list or JSON array (maximum 100).",
    "pid": "Supplier respondent/PID value.",
    "external_id": "Provider transaction/check ID for InnovateMR's unique PID/IP check.",
    "rid": "Our platform-generated 10-character respondent RID.",
    "ip": "Respondent public IP address.",
    "fingerprint": "Optional RFG browser fingerprint; use 0 when unavailable.",
    "date_time": "Provider-formatted changed-since date/time.",
    "device_type": "Provider device value, for example desktop, mobile or tablet.",
    "num_surveys": "Number of personalized surveys requested (1-100; default 10).",
    "metadata_fields": "Comma-separated InnovateMR metadata fields.",
    "startDate": "InnovateMR start date/time filter.",
    "endDate": "InnovateMR end date/time filter.",
    "verifiedStartDate": "InnovateMR verified-start date/time filter.",
    "verifiedEndDate": "InnovateMR verified-end date/time filter.",
    "status": "Optional InnovateMR transaction status.",
    "page_size": "Upstream page size.",
    "cursor": "Upstream next-page cursor.",
    "country": "Two-letter country/market code, for example US.",
    "language": "Provider language name/code, for example English.",
    "countryCode": "InnovateMR country code.",
    "languageCode": "InnovateMR language code.",
    "category": "RFG B2B/B2C category.",
    "category_key": "InnovateMR question-category key.",
    "question_key": "InnovateMR question key.",
    "term_code": "InnovateMR termination category code.",
    "datapoint_name": "RFG datapoint property/name.",
    "modified_since": "RFG modifiedSince date in yyyy-MM-dd format.",
    "zips_only": "When true, RFG returns postal targeting only.",
    "allow_recontacts": "Include RFG recontact projects.",
    "inventory_type": "RFG inventory type: 0 all, 1 LiveAlert, 2 traditional.",
    "result": "Provider result code filter.",
    "start": "RFG log start date/time filter.",
    "end": "RFG log end date/time filter.",
    "zip": "Respondent postal/ZIP code.",
    "country_code": "Two-letter RFG country code.",
    "culture": "Configured Toluna culture, for example en-us, en-gb or en-in.",
    "cultures": "Comma-separated configured Toluna cultures (maximum five). Blank uses all configured cultures.",
    "include_routables": "When true, Toluna returns routable targeting questions in quota layers.",
}

UPSTREAM_INPUT_NOTES = {
    "unique_ip_check": "We convert `survey_id` + `ip` to `{survNum, ip}`.",
    "unique_pid_ip_check": "We convert `survey_id`, `pid`, `external_id` to `{survNum, pid, id}`.",
    "respondent_precheck": "We convert the inputs to `{survNum, pid, ip, deviceType}`.",
    "respondent_surveys": "PID is placed in the URL; the upstream JSON body is `{ip, numSurveys, deviceType}`.",
    "duplicate_check": "We send `{rfg_id, rid, ip, fingerprint}` in RFG's signed JSON command.",
    "duplicate_checks": "We send `{rfg_ids, rid, ip, fingerprint}` in RFG's signed JSON command.",
    "targeting": "The local `survey_id` is sent upstream as RFG `rfg_id` for an RFG client.",
    "quota": "For RFG this calls targeting with `rfg_id` and returns only its `quotas` array.",
    "create_link": "The local `survey_id` is sent as RFG `rfg_id`; RFG returns a reusable link.",
    "log": "The local `survey_id` is sent as RFG `rfg_id` with optional result/start/end filters.",
    "stats": "For RFG the local `survey_id` is sent as `rfg_id`.",
    "zip_to_geo": "We send the local `zip` and uppercase `country_code` as RFG `{zip, countryCode}`.",
}

MUTATION_EXAMPLES = {
    "set_global_redirects": {
        "confirm_upstream_mutation": True,
        "payload": {
            "sUrl": "https://panel.example/survey?status=1&rid=[%%pid%%]",
            "fUrl": "https://panel.example/survey?status=2&rid=[%%pid%%]",
            "oUrl": "https://panel.example/survey?status=3&rid=[%%pid%%]",
            "qTUrl": "https://panel.example/survey?status=3&rid=[%%pid%%]",
            "tUrl": "https://panel.example/survey?status=4&rid=[%%pid%%]",
        },
    },
    "delete_global_redirects": {
        "confirm_upstream_mutation": True,
        "payload": {"oUrl": "", "qTUrl": "", "tUrl": ""},
    },
    "set_survey_redirects": {
        "confirm_upstream_mutation": True,
        "survey_id": "16003381",
        "payload": {
            "sUrl": "https://panel.example/survey?status=1&rid=[%%pid%%]",
            "fUrl": "https://panel.example/survey?status=2&rid=[%%pid%%]",
            "oUrl": "https://panel.example/survey?status=3&rid=[%%pid%%]",
            "tUrl": "https://panel.example/survey?status=4&rid=[%%pid%%]",
        },
    },
    "delete_survey_redirects": {
        "confirm_upstream_mutation": True,
        "survey_id": "16003381",
        "payload": {},
    },
    "create_panelist_profile": {
        "confirm_upstream_mutation": True,
        "pid": "respondent-123",
        "payload": {
            "Country": "US",
            "Language": "English",
            "Qualifications": [{
                "QuestiondId": 1,
                "QuestionKey": "AGE",
                "Options": [{"OptionId": 24, "OptionText": "24"}],
            }],
        },
    },
    "update_panelist_profile": {
        "confirm_upstream_mutation": True,
        "pid": "respondent-123",
        "payload": {
            "Country": "US",
            "Language": "English",
            "Qualifications": [],
        },
    },
}

DISPLAY_ENDPOINTS = {
    "@inventory": "/supply/getAllocatedSurveys",
    "@paged_inventory": "/supply/getAllocatedSurveysPaged",
    "@quota": "/supply/getQuotaForSurvey/{survey_id}",
    "@targeting": "/supply/getSurveyTargeting/{survey_id}",
    "@transaction": "/supply/getSurveyTransactionsByCond/{survey_id}/{pid}",
}


class AmbiguousClientIntegration(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "ambiguous_client_integration"


def _provider_key(value):
    return re.sub(r"[-_]", "", str(value or "").lower())


def _lookup_aliases(integration):
    provider = _provider_key(integration.provider_code)
    aliases = {
        integration.client.code.lower(),
        slugify(integration.client.name),
        slugify(integration.name),
    }
    if provider == "innovatemr":
        aliases.update({"innovate", "innovate-mr", "innovatemr"})
    elif provider == "rfg":
        aliases.update({"rfg", "research-for-good"})
    elif provider == "toluna":
        aliases.update({"toluna", "toluna-integrated-panel"})
    return aliases


@extend_schema_view(
    list=extend_schema(
        tags=[CATALOG_TAG],
        summary="Search configured clients and their runnable provider APIs",
        description=(
            "Use `search=innovate` or `search=rfg` instead of finding a numeric database ID. "
            "The response shows stable client codes, accepted aliases, exact upstream URLs, official "
            "documentation and whether server-side credentials are configured. Secret values are never returned."
        ),
        parameters=[OpenApiParameter("search", str, description="Client name, client code, integration name or provider.")],
        responses=UpstreamIntegrationMetadataSerializer(many=True),
    ),
    retrieve=extend_schema(
        tags=[CATALOG_TAG],
        summary="Inspect one client API catalog by name/code",
        description=(
            "Examples: `/upstream-explorer/innovate/` and `/upstream-explorer/rfg/`. "
            "If one client has multiple active connections, pass `?integration=<integration-name>`."
        ),
        responses={
            200: UpstreamIntegrationMetadataSerializer,
            404: UpstreamErrorSerializer,
            409: OpenApiResponse(UpstreamErrorSerializer, description="Alias matches multiple active integrations."),
        },
    ),
)
class UpstreamExplorerViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Admin-only proxy for explicit provider operations."""

    queryset = ClientIntegration.objects.select_related("client").filter(
        is_active=True, client__is_active=True
    ).order_by("client__name", "name", "id")
    serializer_class = UpstreamIntegrationMetadataSerializer
    permission_classes = [IsDocumentationAdmin]
    pagination_class = None
    lookup_url_kwarg = "client_code"
    lookup_value_regex = r"[A-Za-z0-9][A-Za-z0-9_-]*"

    def get_queryset(self):
        queryset = super().get_queryset()
        search = str(self.request.query_params.get("search") or "").strip()
        if search and self.action == "list":
            queryset = queryset.filter(
                Q(client__code__icontains=search)
                | Q(client__name__icontains=search)
                | Q(name__icontains=search)
                | Q(provider_code__icontains=search)
            )
        return queryset

    def get_object(self):
        lookup = str(self.kwargs.get(self.lookup_url_kwarg) or "").lower()
        queryset = self.queryset.all()
        exact_client_matches = [item for item in queryset if item.client.code.lower() == lookup]
        candidates = exact_client_matches or [item for item in queryset if lookup in _lookup_aliases(item)]
        requested_provider = PROVIDER_ALIASES.get(lookup)
        if requested_provider and not exact_client_matches:
            provider_matches = [
                item for item in queryset if _provider_key(item.provider_code) == _provider_key(requested_provider)
            ]
            if provider_matches:
                candidates = provider_matches
        integration_name = str(self.request.query_params.get("integration") or "").strip()
        if integration_name:
            normalized_name = slugify(integration_name)
            candidates = [
                item for item in candidates
                if slugify(item.name) == normalized_name or str(item.pk) == integration_name
            ]
        if not candidates:
            raise NotFound(
                "No active client integration matches this name/code. Use the catalog search endpoint first."
            )
        if len(candidates) > 1:
            choices = ", ".join(f"{item.client.code}:{slugify(item.name)}" for item in candidates)
            raise AmbiguousClientIntegration(
                f"This alias matches multiple integrations. Add ?integration=<name>. Available: {choices}."
            )
        self.check_object_permissions(self.request, candidates[0])
        return candidates[0]

    def list(self, request, *args, **kwargs):
        return Response([integration_metadata(item) for item in self.get_queryset()])

    def retrieve(self, request, *args, **kwargs):
        return Response(integration_metadata(self.get_object()))

    def _execute(self, integration, operation):
        parameters = self.request.query_params.dict()
        if isinstance(self.request.data, dict):
            parameters.update(self.request.data)
        generic_parameters = parameters.pop("parameters", "")
        if generic_parameters:
            try:
                decoded = json.loads(generic_parameters)
            except (TypeError, ValueError):
                return Response(
                    {"detail": "The parameters field must contain a valid JSON object."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not isinstance(decoded, dict):
                return Response(
                    {"detail": "The parameters field must contain a JSON object."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            parameters.update(decoded)
        try:
            result = execute_operation(integration, operation, parameters)
        except UpstreamExplorerError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)

    @extend_schema(
        tags=[CATALOG_TAG],
        summary="Execute a configured future-provider read operation",
        description=(
            "Generic extension point for future clients configured in `read_api_operations`. "
            "InnovateMR and RFG users should use their named endpoints below; live mutations are rejected here."
        ),
        parameters=[
            OpenApiParameter("operation", str, OpenApiParameter.PATH, required=True),
            OpenApiParameter("parameters", str, description='JSON object, for example {"market":"US"}.'),
            OpenApiParameter("limit", int, description="Maximum list rows returned (1-200)."),
        ],
        responses={200: UpstreamExecutionResponseSerializer, 400: UpstreamErrorSerializer},
    )
    @action(detail=True, methods=["get"], url_path=r"execute/(?P<operation>[a-z][a-z0-9_]+)")
    def execute(self, request, client_code=None, operation=None):
        integration = self.get_object()
        spec = {**INNOVATE_OPERATIONS, **RFG_OPERATIONS, **TOLUNA_OPERATIONS}.get(operation)
        if spec and spec.mutating:
            return Response(
                {"detail": "Use the provider-specific POST endpoint for live mutations."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return self._execute(integration, operation)

    @extend_schema(
        tags=[RFG_CALLBACK_TAG],
        summary="Preview how an RFG callback result will be interpreted",
        description=(
            "Safe, non-writing helper for administrators. Enter RFG result/live fields and receive the "
            "human-readable platform status and reason. It does not update an RID. The real callback is "
            "`GET /survey/rfg/callback` and remains restricted to RFG's documented server IP addresses."
        ),
        request=RFGCallbackPreviewSerializer,
        responses={200: OpenApiResponse(description="Mapped platform status, title and reason."), 400: UpstreamErrorSerializer},
    )
    @action(detail=True, methods=["post"], url_path="rfg/callback-preview")
    def rfg_callback_preview(self, request, client_code=None):
        integration = self.get_object()
        if _provider_key(integration.provider_code) != "rfg":
            return Response({"detail": "This client is not an RFG integration."}, status=400)
        serializer = RFGCallbackPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        outcome = describe_rfg_outcome(serializer.validated_data)
        return Response({
            "known_result_code": str(serializer.validated_data["result"]) in RFG_STATUS_MAP,
            **outcome,
        })

    @extend_schema(
        tags=[RFG_CALLBACK_TAG],
        summary="Understand the three documented RFG callback methods",
        description=(
            "Non-writing callback setup guide. It explains RFG page redirects, browser pixels and "
            "server callbacks; which one this platform accepts; required placeholders; and why an "
            "ordinary Swagger request cannot impersonate an RFG production callback."
        ),
        responses={200: OpenApiResponse(description="Callback methods, platform URL and security requirements.")},
    )
    @action(detail=True, methods=["get"], url_path="rfg/callback-guide")
    def rfg_callback_guide(self, request, client_code=None):
        integration = self.get_object()
        if _provider_key(integration.provider_code) != "rfg":
            return Response({"detail": "This client is not an RFG integration."}, status=400)
        callback_url = request.build_absolute_uri("/survey/rfg/callback")
        return Response({
            "configured_platform_mode": "server callback with source-IP allowlist",
            "production_callback_url": callback_url,
            "methods_in_official_specification": [
                {
                    "method": "Page redirect",
                    "purpose": "RFG redirects the respondent browser to a configured outcome URL.",
                    "security": "RFG HMAC-MD5 redirect verification described by RFG.",
                    "platform_use": "Not used for the verified server-to-server status update.",
                },
                {
                    "method": "Browser pixel callback",
                    "purpose": "The result page loads a tracking pixel URL in the respondent browser.",
                    "security": "RFG HMAC-MD5 pixel verification described by RFG.",
                    "platform_use": "Not trusted as the final verified callback because it originates in the browser.",
                },
                {
                    "method": "Server callback",
                    "purpose": "RFG calls the platform directly with RID/result and optional outcome fields.",
                    "security": "Only configured RFG server IP addresses are accepted.",
                    "platform_use": "Active verified integration mode.",
                },
            ],
            "required_query_fields": ["rid", "result"],
            "optional_query_fields": [
                "ruledOutBy", "sesskey", "liveP", "liveS", "liveI", "quotaThrottle"
            ],
            "important": (
                "Use callback-preview to learn result meanings. Calling the production callback from "
                "Swagger normally returns 403 because Swagger is not an RFG callback server."
            ),
            "official_documentation": (
                "https://docs.researchforgood.com/RFGAPI/api-pub-callback-spec/apidocs/index.html"
            ),
        })


def _operation_parameters(spec):
    names = list(dict.fromkeys((*spec.required_parameters, *spec.query_parameters)))
    parameters = [
        OpenApiParameter(
            name,
            bool if name in {"zips_only", "allow_recontacts", "include_routables"} else int if name in {"num_surveys", "page_size", "inventory_type"} else str,
            required=name in spec.required_parameters,
            description=PARAMETER_HELP.get(name, name.replace("_", " ").title()),
        )
        for name in names
    ]
    parameters.append(OpenApiParameter("limit", int, description="Maximum list rows shown in this response (1-200)."))
    return parameters


def _operation_description(provider, spec):
    required = ", ".join(spec.required_parameters) or "nothing required"
    optional = ", ".join(spec.query_parameters) or "none"
    safety = (
        "\n\n**Safety:** This changes live provider data. Send `confirm_upstream_mutation: true`; "
        "without it the call is rejected before contacting InnovateMR."
        if spec.mutating else ""
    )
    mapping_note = UPSTREAM_INPUT_NOTES.get(spec.code)
    mapping_text = f" {mapping_note}" if mapping_note else ""
    displayed_endpoint = DISPLAY_ENDPOINTS.get(spec.endpoint, spec.endpoint)
    return (
        f"**Purpose:** {spec.description}\n\n"
        f"**You send:** required: {required}; optional: {optional}. Credentials are never entered here."
        f"{mapping_text}\n\n"
        f"**Upstream request:** `{spec.upstream_method} {displayed_endpoint}` using server-side authentication.\n\n"
        f"**You receive:** {operation_response_description(provider, spec)}\n\n"
        f"**Official documentation:** {spec.documentation_url}{safety}"
    )


def _build_operation_action(provider, operation, spec):
    local_method = "post" if spec.mutating else "get"
    tag = INNOVATE_TAG if provider == "innovatemr" else TOLUNA_TAG if provider == "toluna" else RFG_TAG

    def operation_action(self, request, client_code=None):
        integration = self.get_object()
        if _provider_key(integration.provider_code) != _provider_key(provider):
            return Response(
                {"detail": f"This endpoint requires a {provider} client integration."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return self._execute(integration, operation)

    operation_action.__name__ = f"{provider}_{operation}"
    operation_action.__doc__ = spec.description
    schema_kwargs = {
        "tags": [tag],
        "summary": spec.label,
        "description": _operation_description(provider, spec),
        "responses": {
            200: UpstreamExecutionResponseSerializer,
            400: OpenApiResponse(UpstreamErrorSerializer, description="Missing/invalid input or safe upstream error."),
            403: OpenApiResponse(description="Admin or super-admin session required."),
            409: OpenApiResponse(UpstreamErrorSerializer, description="Client alias is ambiguous."),
        },
        "operation_id": f"{provider}_{operation}",
    }
    if spec.mutating:
        schema_kwargs["request"] = UpstreamMutationRequestSerializer
        schema_kwargs["examples"] = [OpenApiExample(
            "Confirmed live change",
            value=MUTATION_EXAMPLES.get(
                operation, {"confirm_upstream_mutation": True, "payload": {}}
            ),
            request_only=True,
        )]
    else:
        schema_kwargs["parameters"] = _operation_parameters(spec)
    operation_action = action(
        detail=True,
        methods=[local_method],
        url_path=f"{provider}/{operation.replace('_', '-')}",
        url_name=f"{provider}-{operation.replace('_', '-')}",
    )(operation_action)
    return extend_schema(**schema_kwargs)(operation_action)


for _operation, _spec in INNOVATE_OPERATIONS.items():
    setattr(
        UpstreamExplorerViewSet,
        f"innovatemr_{_operation}",
        _build_operation_action("innovatemr", _operation, _spec),
    )

for _operation, _spec in RFG_OPERATIONS.items():
    setattr(
        UpstreamExplorerViewSet,
        f"rfg_{_operation}",
        _build_operation_action("rfg", _operation, _spec),
    )

for _operation, _spec in TOLUNA_OPERATIONS.items():
    setattr(
        UpstreamExplorerViewSet,
        f"toluna_{_operation}",
        _build_operation_action("toluna", _operation, _spec),
    )
