import json

from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from config.api_docs import IsDocumentationAdmin

from .models import ClientIntegration
from .upstream import (
    INNOVATE_OPERATIONS,
    RFG_OPERATIONS,
    UpstreamExplorerError,
    execute_operation,
    integration_metadata,
)
from .upstream_serializers import (
    UpstreamErrorSerializer,
    UpstreamExecutionResponseSerializer,
    UpstreamIntegrationMetadataSerializer,
)


TAG = "Upstream client APIs"
COMMON_PARAMETERS = [
    OpenApiParameter(
        "operation", str, OpenApiParameter.PATH, required=True,
        description=(
            "Allow-listed provider operation. First call the integration metadata endpoint to see "
            "which operations this provider supports and its exact upstream URL. Built-in examples: "
            f"{', '.join(sorted(set(INNOVATE_OPERATIONS) | set(RFG_OPERATIONS)))}."
        ),
    ),
    OpenApiParameter(
        "parameters", str,
        description=(
            "Optional JSON object for parameters declared by a future configured read operation, "
            'for example {"market":"US"}. Built-in parameters have dedicated fields below.'
        ),
    ),
    OpenApiParameter("limit", int, description="Maximum list rows returned to Swagger (1-200; default 50)."),
    OpenApiParameter("survey_id", str, description="InnovateMR Survey ID or RFG project/rfg_id."),
    OpenApiParameter("pid", str, description="Supplier respondent/PID value."),
    OpenApiParameter("external_id", str, description="Provider transaction/check ID used by InnovateMR unique PID/IP check."),
    OpenApiParameter("rid", str, description="Platform-generated respondent RID."),
    OpenApiParameter("ip", str, description="Respondent public IP, required only for duplicate checks."),
    OpenApiParameter("fingerprint", str, description="Optional RFG duplicate-check fingerprint."),
    OpenApiParameter("date_time", str, description="Provider-formatted inventory/closed-survey date-time."),
    OpenApiParameter("device_type", str, description="Provider device value for respondent eligibility checks."),
    OpenApiParameter("num_surveys", int, description="Requested personalized inventory size (1-100)."),
    OpenApiParameter("metadata_fields", str, description="Comma-separated InnovateMR core metadata fields."),
    OpenApiParameter("startDate", str, description="InnovateMR start date/time filter."),
    OpenApiParameter("endDate", str, description="InnovateMR end date/time filter."),
    OpenApiParameter("verifiedStartDate", str, description="InnovateMR verified start date/time filter."),
    OpenApiParameter("verifiedEndDate", str, description="InnovateMR verified end date/time filter."),
    OpenApiParameter("status", str, description="Optional InnovateMR transaction status."),
    OpenApiParameter("page_size", int, description="Upstream page size for paged inventory."),
    OpenApiParameter("cursor", str, description="Upstream next cursor for paged inventory."),
    OpenApiParameter("country", str, description="Two-letter market code where supported."),
    OpenApiParameter("language", str, description="Provider language name/code where supported."),
    OpenApiParameter("countryCode", str, description="InnovateMR high-priority country code."),
    OpenApiParameter("languageCode", str, description="InnovateMR high-priority language code."),
    OpenApiParameter("category", str, description="Question category key or RFG B2B/B2C category."),
    OpenApiParameter("category_key", str, description="InnovateMR question category key."),
    OpenApiParameter("question_key", str, description="InnovateMR question key for answer lookup."),
    OpenApiParameter("term_code", str, description="InnovateMR termination category code."),
    OpenApiParameter("datapoint_name", str, description="RFG datapoint property/name."),
    OpenApiParameter("modified_since", str, description="RFG modifiedSince value."),
    OpenApiParameter("zips_only", bool, description="Return only RFG postal targeting where supported."),
    OpenApiParameter("allow_recontacts", bool, description="Include RFG recontacts in inventory."),
    OpenApiParameter("inventory_type", int, description="RFG inventory type (normally 1 for LiveAlert)."),
]


@extend_schema_view(
    list=extend_schema(
        tags=[TAG],
        summary="List configured upstream integrations and every runnable API",
        description=(
            "Shows client/provider base URLs, exact API endpoints, official documentation links and "
            "credential environment-variable names. Credential values are never returned."
        ),
        responses=UpstreamIntegrationMetadataSerializer(many=True),
    ),
    retrieve=extend_schema(
        tags=[TAG],
        summary="Inspect one integration's upstream API catalog",
        responses=UpstreamIntegrationMetadataSerializer,
    ),
)
class UpstreamExplorerViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Admin-only proxy for explicit, read-oriented provider operations."""

    queryset = ClientIntegration.objects.select_related("client").filter(
        is_active=True, client__is_active=True
    ).order_by("client__name", "name", "id")
    serializer_class = UpstreamIntegrationMetadataSerializer
    permission_classes = [IsDocumentationAdmin]
    pagination_class = None

    def list(self, request, *args, **kwargs):
        return Response([integration_metadata(item) for item in self.get_queryset()])

    def retrieve(self, request, *args, **kwargs):
        return Response(integration_metadata(self.get_object()))

    def _execute(self, integration, operation):
        parameters = self.request.query_params.dict()
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
        tags=[TAG],
        summary="Execute any allow-listed upstream client API",
        description=(
            "Runs the selected provider API using credentials resolved on the server. For future "
            "providers, additional GET operations configured in integration.config.read_api_operations "
            "are accepted here without exposing an arbitrary URL parameter. This endpoint is local GET "
            "even when a provider such as RFG requires a signed POST upstream."
        ),
        parameters=COMMON_PARAMETERS,
        responses={
            200: UpstreamExecutionResponseSerializer,
            400: OpenApiResponse(UpstreamErrorSerializer, description="Unsupported operation, missing input, or safe upstream error."),
            403: OpenApiResponse(description="Admin or super-admin session required."),
        },
    )
    @action(detail=True, methods=["get"], url_path=r"execute/(?P<operation>[a-z][a-z0-9_]+)")
    def execute(self, request, pk=None, operation=None):
        return self._execute(self.get_object(), operation)

    @extend_schema(
        tags=[TAG], summary="Run this client's inventory API",
        parameters=[OpenApiParameter("limit", int, description="Maximum returned rows (1-200).")],
        responses={200: UpstreamExecutionResponseSerializer, 400: UpstreamErrorSerializer},
    )
    @action(detail=True, methods=["get"], url_path="inventory")
    def inventory(self, request, pk=None):
        return self._execute(self.get_object(), "inventory")

    @extend_schema(
        tags=[TAG], summary="Run this client's survey quota API",
        description="For RFG, quotas are returned by its targeting command and extracted here.",
        parameters=[
            OpenApiParameter("survey_id", str, required=True, description="Survey ID or RFG rfg_id."),
            OpenApiParameter("limit", int, description="Maximum returned rows (1-200)."),
        ],
        responses={200: UpstreamExecutionResponseSerializer, 400: UpstreamErrorSerializer},
    )
    @action(detail=True, methods=["get"], url_path="quota")
    def quota(self, request, pk=None):
        return self._execute(self.get_object(), "quota")

    @extend_schema(
        tags=[TAG], summary="Run this client's survey targeting API",
        parameters=[
            OpenApiParameter("survey_id", str, required=True, description="Survey ID or RFG rfg_id."),
            OpenApiParameter("zips_only", bool, description="RFG-only postal targeting switch."),
            OpenApiParameter("limit", int, description="Maximum returned rows (1-200)."),
        ],
        responses={200: UpstreamExecutionResponseSerializer, 400: UpstreamErrorSerializer},
    )
    @action(detail=True, methods=["get"], url_path="targeting")
    def targeting(self, request, pk=None):
        return self._execute(self.get_object(), "targeting")
