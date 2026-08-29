from django.contrib.auth import get_user_model
from django.conf import settings
from django.db.models.deletion import ProtectedError
from django.db.models import Count, Prefetch, Q
from django.shortcuts import render
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.access import (
    HasFunctionPermission,
    any_function_permission_required,
    effective_permission_codes,
    function_permission_required,
    has_function_access,
)
from accounts.models import EmployeeProfile
from surveys.models import Survey

from .access import (
    organization_workspace_owner_ids,
    vendor_scope_user_id,
)

from .models import (
    AllocationReservation,
    Client,
    ClientIntegration,
    OrganizationClientAccess,
    OrganizationUnit,
    VendorClientAllocation,
    VendorCommercialProfile,
    VendorAPIKey,
    VendorSurveyAllocation,
)
from .serializers import (
    AllocationReservationSerializer,
    ClientIntegrationSerializer,
    IntegrationActionResponseSerializer,
    IntegrationPreviewSerializer,
    ProviderCatalogSerializer,
    ClientSerializer,
    OrganizationClientAccessSerializer,
    OrganizationManagementOptionsSerializer,
    OrganizationUnitSerializer,
    VendorClientAllocationSerializer,
    VendorCommercialProfileSerializer,
    VendorAPIKeySerializer,
    VendorSurveyAllocationSerializer,
    VendorDirectorySerializer,
    VendorManagementOptionsSerializer,
)
from .services import organization_unit_rollup_counts


@any_function_permission_required("vendors.view", "vendors.manage", "allocations.view", "allocations.manage")
def vendor_management_page(request):
    owner_controlled = vendor_scope_user_id(request.user) is None
    codes = effective_permission_codes(request.user)
    vendor_columns = [
        name for name in ("name", "type", "cpi", "clients", "status", "actions")
        if f"vendors.column.vendor.{name}" in codes
    ]
    client_columns = [
        name for name in ("vendor", "client", "cpi", "window", "actions")
        if f"vendors.column.client.{name}" in codes
    ]
    project_columns = [
        name for name in ("vendor", "survey", "client", "cpi", "actions")
        if f"vendors.column.project.{name}" in codes
    ]
    api_columns = [
        name for name in ("vendor", "key", "created", "last_used", "expires", "actions")
        if f"vendors.column.api.{name}" in codes
    ]
    first_vendor_tab = next((name for name, code in (
        ("vendors", "vendors.tab.policies"), ("clients", "vendors.tab.clients"),
        ("surveys", "vendors.tab.projects"), ("api-keys", "vendors.tab.api_keys"),
    ) if code in codes), "")
    return render(request, "vendors/management.html", {
        "active_page": "vendors",
        "vendor_cards": [
            name for name in ("vendors", "client_grants", "projects")
            if f"vendors.card.{name}" in codes
        ],
        "can_view_vendors": "vendors.tab.policies" in codes,
        "can_view_client_allocations": "vendors.tab.clients" in codes,
        "can_view_project_allocations": "vendors.tab.projects" in codes,
        "can_view_api_keys": "vendors.tab.api_keys" in codes,
        "can_create_vendor": owner_controlled and "vendors.action.create" in codes,
        "can_edit_vendor_policy": owner_controlled and "vendors.action.edit_policy" in codes,
        "can_allocate_client": owner_controlled and "vendors.action.allocate_client" in codes,
        "can_allocate_project": owner_controlled and "vendors.action.allocate_project" in codes,
        "can_create_api_key": owner_controlled and "vendors.action.create_api_key" in codes,
        "can_revoke_api_key": owner_controlled and "vendors.action.revoke_api_key" in codes,
        "vendor_columns": vendor_columns, "vendor_column_count": max(1, len(vendor_columns)),
        "client_columns": client_columns, "client_column_count": max(1, len(client_columns)),
        "vendor_project_columns": project_columns, "vendor_project_column_count": max(1, len(project_columns)),
        "api_columns": api_columns, "api_column_count": max(1, len(api_columns)),
        "first_vendor_tab": first_vendor_tab,
    })


@function_permission_required("clients.integration.view")
def client_integrations_page(request):
    return render(request, "vendors/integrations.html", {
        "active_page": "client-integrations",
        "can_manage_integrations": has_function_access(request.user, "clients.integration.manage"),
    })


@function_permission_required("organization.view")
def organization_management_page(request):
    codes = effective_permission_codes(request.user)
    owner_controlled = vendor_scope_user_id(request.user) is None
    can_view_structure = "organization.tab.structure" in codes
    can_view_client_access = "organization.tab.client_access" in codes
    can_view_clients = owner_controlled and "organization.tab.clients" in codes
    unit_columns = [
        name for name in ("path", "type", "members", "clients", "status", "actions")
        if f"organization.column.unit.{name}" in codes
    ]
    access_columns = [
        name for name in ("unit", "client", "cpi", "status", "actions")
        if f"organization.column.access.{name}" in codes
    ]
    first_tab = next((name for name, available in (
        ("structure", can_view_structure),
        ("client-access", can_view_client_access),
        ("clients", can_view_clients),
    ) if available), "")
    return render(request, "vendors/organization.html", {
        "active_page": "organization",
        "organization_cards": [
            name for name in ("branches", "shifts", "members", "client_grants")
            if f"organization.card.{name}" in codes
        ],
        "can_view_structure": can_view_structure,
        "can_view_client_access": can_view_client_access,
        "can_view_clients": can_view_clients,
        "can_manage_units": any(
            code in codes for code in (
                "organization.action.create_unit", "organization.action.edit_unit", "organization.action.delete_unit"
            )
        ),
        "can_create_units": "organization.action.create_unit" in codes,
        "can_edit_units": "organization.action.edit_unit" in codes,
        "can_delete_units": "organization.action.delete_unit" in codes,
        "can_manage_unit_clients": "organization.action.assign_client" in codes,
        "can_remove_unit_clients": "organization.action.remove_client" in codes,
        "can_manage_clients": owner_controlled and "clients.manage" in codes,
        "can_view_integrations": owner_controlled and "clients.integration.view" in codes,
        "can_manage_integrations": owner_controlled and "clients.integration.manage" in codes,
        "can_test_integrations": owner_controlled and "clients.integration.test" in codes,
        "can_preview_integrations": owner_controlled and "clients.integration.preview" in codes,
        "can_sync_integrations": owner_controlled and "clients.integration.sync" in codes,
        "organization_unit_columns": unit_columns,
        "organization_access_columns": access_columns,
        "first_organization_tab": first_tab,
    })


class OrganizationManagementOptionsView(APIView):
    permission_classes = [HasFunctionPermission]
    required_function_permission = "organization.view"

    @extend_schema(
        tags=["Organization hierarchy"],
        summary="List organization workspace and client selector options",
        responses={200: OrganizationManagementOptionsSerializer},
    )
    def get(self, request):
        owner_ids = organization_workspace_owner_ids(request.user)
        owners = get_user_model().objects.filter(pk__in=owner_ids).select_related("employee_profile").order_by(
            "first_name", "last_name", "username"
        )
        clients = Client.objects.filter(is_active=True).order_by("name")
        eligibility = {}
        visible_client_ids = set()
        for owner in owners:
            profile = getattr(owner, "employee_profile", None)
            if owner.is_superuser:
                eligible = list(clients.values_list("id", flat=True))
                owner_type = "owner"
            else:
                eligible = list(
                    VendorClientAllocation.objects.filter(vendor=owner, client__is_active=True, is_active=True)
                    .values_list("client_id", flat=True)
                )
                owner_type = getattr(profile, "account_type", "")
            eligibility[str(owner.pk)] = eligible
            visible_client_ids.update(eligible)
            owner._organization_owner_type = owner_type
        visible_clients = clients.filter(pk__in=visible_client_ids)
        return Response({
            "owners": [
                {
                    "id": owner.pk,
                    "name": owner.get_full_name() or owner.username,
                    "username": owner.username,
                    "type": owner._organization_owner_type,
                }
                for owner in owners
            ],
            "clients": [
                {"id": client.pk, "name": client.name, "code": client.code, "provider_code": client.provider_code}
                for client in visible_clients
            ],
            "client_eligibility": eligibility,
        })


class OrganizationScopedMixin:
    def organization_owner_ids(self):
        return organization_workspace_owner_ids(self.request.user)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save()

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    list=extend_schema(tags=["Organization hierarchy"], summary="List Branch, Sub-branch and Shift units"),
    create=extend_schema(tags=["Organization hierarchy"], summary="Create an organization unit"),
    retrieve=extend_schema(tags=["Organization hierarchy"], summary="Get an organization unit"),
    update=extend_schema(tags=["Organization hierarchy"], summary="Replace an organization unit"),
    partial_update=extend_schema(tags=["Organization hierarchy"], summary="Update an organization unit"),
    destroy=extend_schema(tags=["Organization hierarchy"], summary="Delete an unused organization unit"),
)
class OrganizationUnitViewSet(OrganizationScopedMixin, viewsets.ModelViewSet):
    serializer_class = OrganizationUnitSerializer
    permission_classes = [HasFunctionPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["workspace_owner", "parent", "unit_type", "is_active"]
    search_fields = ["name", "code", "description", "workspace_owner__username"]
    ordering_fields = ["unit_type", "name", "created_at", "updated_at"]
    ordering = ["workspace_owner_id", "unit_type", "name"]

    def get_required_function_permission(self):
        if self.action in {"list", "retrieve"}:
            return "organization.view"
        if self.action == "create":
            return "organization.action.create_unit"
        if self.action == "destroy":
            return "organization.action.delete_unit"
        return "organization.action.edit_unit"

    def get_queryset(self):
        return OrganizationUnit.objects.filter(
            workspace_owner_id__in=self.organization_owner_ids()
        ).select_related(
            "workspace_owner", "workspace_owner__employee_profile", "parent", "created_by"
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["organization_rollup_counts"] = organization_unit_rollup_counts(
            self.organization_owner_ids()
        )
        return context

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        try:
            instance.delete()
        except ProtectedError:
            return Response(
                {
                    "detail": (
                        "This unit is still used by child units or client assignments. "
                        "Move or delete those records first."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    list=extend_schema(tags=["Organization hierarchy"], summary="List unit-level client visibility grants"),
    create=extend_schema(tags=["Organization hierarchy"], summary="Assign a client to an organization unit"),
    retrieve=extend_schema(tags=["Organization hierarchy"], summary="Get a unit client grant"),
    update=extend_schema(tags=["Organization hierarchy"], summary="Replace a unit client grant"),
    partial_update=extend_schema(tags=["Organization hierarchy"], summary="Update a unit client grant"),
    destroy=extend_schema(tags=["Organization hierarchy"], summary="Remove a unit client grant"),
)
class OrganizationClientAccessViewSet(OrganizationScopedMixin, viewsets.ModelViewSet):
    serializer_class = OrganizationClientAccessSerializer
    permission_classes = [HasFunctionPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["organization_unit", "organization_unit__workspace_owner", "client", "is_active"]
    search_fields = ["organization_unit__name", "organization_unit__code", "client__name", "client__code"]
    ordering_fields = ["created_at", "updated_at", "organization_unit__name", "client__name"]
    ordering = ["organization_unit__workspace_owner_id", "organization_unit__name", "client__name"]

    def get_required_function_permission(self):
        if self.action in {"list", "retrieve"}:
            return "organization.view"
        if self.action == "destroy":
            return "organization.action.remove_client"
        return "organization.action.assign_client"

    def get_queryset(self):
        return OrganizationClientAccess.objects.filter(
            organization_unit__workspace_owner_id__in=self.organization_owner_ids()
        ).select_related(
            "organization_unit", "organization_unit__workspace_owner", "organization_unit__parent__parent",
            "client", "created_by",
        )

    def destroy(self, request, *args, **kwargs):
        self.get_object().delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class VendorManagementOptionsView(APIView):
    """Small non-secret lookup lists used by the vendor allocation workspace."""

    permission_classes = [HasFunctionPermission]
    required_function_permission = (
        "vendors.tab.policies", "vendors.tab.clients", "vendors.tab.projects", "vendors.tab.api_keys",
        "vendors.action.edit_policy", "vendors.action.allocate_client", "vendors.action.allocate_project",
        "vendors.action.create_api_key", "vendors.action.revoke_api_key",
    )

    @extend_schema(
        tags=["Suppliers & allocations"],
        summary="List safe supplier-management selector options",
        description="Returns non-secret active vendor and client labels for allocation modals, scoped to the current vendor hierarchy when applicable.",
        responses={200: VendorManagementOptionsSerializer},
    )
    def get(self, request):
        vendor_id = vendor_scope_user_id(request.user)
        vendors = get_user_model().objects.filter(
            employee_profile__account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR,
            employee_profile__role__slug="external-vendor",
            is_active=True,
        ).select_related("employee_profile", "employee_profile__role").order_by(
            "first_name", "last_name", "username"
        )
        clients = Client.objects.filter(is_active=True).order_by("name")
        if vendor_id:
            vendors = vendors.filter(pk=vendor_id)
            clients = clients.filter(vendor_allocations__vendor_id=vendor_id).distinct()
        return Response({
            "vendors": [
                {
                    "id": vendor.pk,
                    "full_name": vendor.get_full_name() or vendor.username,
                    "username": vendor.username,
                    "account_type": vendor.employee_profile.account_type,
                }
                for vendor in vendors
            ],
            "clients": [
                {"id": client.pk, "name": client.name, "code": client.code, "provider_code": client.provider_code}
                for client in clients
            ],
        })


class VendorScopedQuerysetMixin:
    vendor_scope_filter = None

    def get_queryset(self):
        queryset = super().get_queryset()
        vendor_id = vendor_scope_user_id(self.request.user)
        if vendor_id and self.vendor_scope_filter:
            queryset = queryset.filter(**{self.vendor_scope_filter: vendor_id}).distinct()
        return queryset


class PermissionByActionMixin(VendorScopedQuerysetMixin):
    view_permission = None
    manage_permission = None

    def get_required_function_permission(self):
        return (self.view_permission, self.manage_permission) if self.action in {"list", "retrieve"} else self.manage_permission

    def perform_create(self, serializer):
        if vendor_scope_user_id(self.request.user):
            raise PermissionDenied("Vendor-scoped accounts cannot change owner-controlled commercial data.")
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        if vendor_scope_user_id(self.request.user):
            raise PermissionDenied("Vendor-scoped accounts cannot change owner-controlled commercial data.")
        serializer.save()

    def destroy(self, request, *args, **kwargs):
        if vendor_scope_user_id(request.user):
            raise PermissionDenied("Vendor-scoped accounts cannot change owner-controlled commercial data.")
        instance = self.get_object()
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    list=extend_schema(tags=["Suppliers & allocations"], summary="List internal and external supplier accounts"),
    retrieve=extend_schema(tags=["Suppliers & allocations"], summary="Get a supplier account and commercial summary"),
)
class VendorDirectoryViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = VendorDirectorySerializer
    permission_classes = [HasFunctionPermission]
    required_function_permission = "vendors.tab.policies"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["employee_profile__account_type", "is_active"]
    search_fields = ["username", "first_name", "last_name", "email"]
    ordering_fields = ["first_name", "last_name", "date_joined"]
    ordering = ["first_name", "last_name", "username"]

    def get_queryset(self):
        queryset = get_user_model().objects.filter(
            employee_profile__account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR,
            employee_profile__role__slug="external-vendor",
        ).select_related(
            "employee_profile", "employee_profile__role", "employee_profile__created_by",
            "vendor_commercial_profile",
        ).prefetch_related(
            Prefetch(
                "client_allocations",
                queryset=VendorClientAllocation.objects.filter(is_active=True).select_related(
                    "vendor__vendor_commercial_profile", "client"
                ),
                to_attr="active_client_allocations",
            )
        ).annotate(
            allocation_count=Count("client_allocations", distinct=True),
            api_key_count=Count("vendor_api_keys", filter=Q(vendor_api_keys__is_active=True), distinct=True),
        )
        vendor_id = vendor_scope_user_id(self.request.user)
        return queryset.filter(pk=vendor_id) if vendor_id else queryset


@extend_schema_view(
    list=extend_schema(tags=["Suppliers & allocations"], summary="List survey clients"),
    create=extend_schema(tags=["Suppliers & allocations"], summary="Create a survey client"),
    retrieve=extend_schema(tags=["Suppliers & allocations"], summary="Get a survey client"),
    update=extend_schema(tags=["Suppliers & allocations"], summary="Replace a survey client"),
    partial_update=extend_schema(tags=["Suppliers & allocations"], summary="Update a survey client"),
    destroy=extend_schema(tags=["Suppliers & allocations"], summary="Deactivate a survey client"),
)
class ClientViewSet(PermissionByActionMixin, viewsets.ModelViewSet):
    queryset = Client.objects.select_related("created_by").prefetch_related(
        Prefetch(
            "integrations",
            queryset=ClientIntegration.objects.select_related("client", "created_by"),
        )
    ).exclude(
        is_active=False, provider_code__in=("biobrain", "voqall")
    )
    serializer_class = ClientSerializer
    permission_classes = [HasFunctionPermission]
    view_permission = "clients.view"
    manage_permission = "clients.manage"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["provider_code", "is_active"]
    search_fields = ["name", "code", "company_name_match"]
    ordering_fields = ["name", "created_at", "updated_at"]
    ordering = ["name"]
    vendor_scope_filter = "vendor_allocations__vendor_id"

    def _can_view_client_integrations(self):
        if not hasattr(self, "_can_view_client_integrations_cache"):
            user = self.request.user
            self._can_view_client_integrations_cache = bool(
                user.is_superuser
                or "clients.integration.view" in effective_permission_codes(user)
            )
        return self._can_view_client_integrations_cache

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["can_view_client_integrations"] = self._can_view_client_integrations()
        return context

    @staticmethod
    def _attach_page_integration_metadata(clients):
        clients = list(clients)
        integrations = [
            integration
            for client in clients
            for integration in client.integrations.all()
        ]
        ClientIntegrationViewSet._attach_list_metadata(integrations)
        return clients

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            if self._can_view_client_integrations():
                page = self._attach_page_integration_metadata(page)
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        rows = list(queryset)
        if self._can_view_client_integrations():
            rows = self._attach_page_integration_metadata(rows)
        return Response(self.get_serializer(rows, many=True).data)


@extend_schema_view(
    list=extend_schema(tags=["Suppliers & allocations"], summary="List non-secret client integration metadata"),
    create=extend_schema(tags=["Suppliers & allocations"], summary="Create client integration metadata"),
    retrieve=extend_schema(tags=["Suppliers & allocations"], summary="Get client integration metadata"),
    update=extend_schema(tags=["Suppliers & allocations"], summary="Replace client integration metadata"),
    partial_update=extend_schema(tags=["Suppliers & allocations"], summary="Update client integration metadata"),
    destroy=extend_schema(tags=["Suppliers & allocations"], summary="Deactivate client integration metadata"),
)
class ClientIntegrationViewSet(PermissionByActionMixin, viewsets.ModelViewSet):
    queryset = ClientIntegration.objects.select_related("client", "created_by").exclude(
        client__is_active=False, provider_code__in=("biobrain", "voqall")
    )
    serializer_class = ClientIntegrationSerializer
    permission_classes = [HasFunctionPermission]
    view_permission = "clients.integration.view"
    manage_permission = "clients.integration.manage"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["client", "provider_code", "scheduled_sync_enabled", "is_active"]
    search_fields = ["name", "client__name", "client__code"]
    vendor_scope_filter = "client__vendor_allocations__vendor_id"

    @staticmethod
    def _attach_list_metadata(integrations):
        """Batch integration-card counters that otherwise cost five queries per row."""

        integrations = list(integrations)
        integration_ids = [integration.pk for integration in integrations]
        if not integration_ids:
            return integrations

        survey_counts = {integration_id: 0 for integration_id in integration_ids}
        country_codes = {integration_id: [] for integration_id in integration_ids}
        for row in (
            Survey.objects.filter(integration_id__in=integration_ids)
            .values("integration_id", "country_code")
            .annotate(total=Count("id"))
            .order_by("integration_id", "country_code")
        ):
            integration_id = row["integration_id"]
            survey_counts[integration_id] += row["total"]
            if row["country_code"] and len(country_codes[integration_id]) < 250:
                country_codes[integration_id].append(row["country_code"])

        from prescreener_vault.reuse import profile_reuse_month_statuses

        reuse_statuses = profile_reuse_month_statuses(integrations)
        for integration in integrations:
            integration._survey_count = survey_counts[integration.pk]
            integration._profile_reuse_available_country_codes = country_codes[
                integration.pk
            ]
            integration._profile_reuse_status = reuse_statuses[integration.pk]
        return integrations

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            page = self._attach_list_metadata(page)
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        rows = self._attach_list_metadata(queryset)
        return Response(self.get_serializer(rows, many=True).data)

    def get_required_function_permission(self):
        if self.action in {"list", "retrieve", "providers"}:
            return "clients.integration.view"
        return {
            "test_connection": "clients.integration.test",
            "preview": "clients.integration.preview",
            "sync_now": "clients.integration.sync",
        }.get(self.action, "clients.integration.manage")

    @extend_schema(
        tags=["Client integrations"],
        summary="List installed upstream provider adapters",
        responses=ProviderCatalogSerializer(many=True),
    )
    @action(detail=False, methods=["get"], url_path="providers")
    def providers(self, request):
        from surveys.providers import provider_catalog

        providers = provider_catalog()
        biobrain_is_published = Client.objects.filter(
            is_active=True, provider_code__in=("biobrain", "voqall")
        ).exists()
        if not biobrain_is_published:
            providers = [item for item in providers if item.get("code") not in {"biobrain", "voqall"}]
        return Response(providers)

    @action(detail=True, methods=["post"], url_path="test-connection")
    def test_connection(self, request, pk=None):
        integration = self.get_object()
        if integration.provider_code in {"rfg", "toluna", "track_opinion", "acuity", "unimarket"}:
            from surveys.provider_services import test_provider_connection
            from surveys.providers import ProviderError

            try:
                result = test_provider_connection(integration)
            except ProviderError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            return Response({"detail": "Connection verified.", "result": result})

        from surveys.integrations import InnovateMRClient
        try:
            result = InnovateMRClient(integration=integration).test_connection()
            integration.last_test_status = "success"
            integration.last_test_error = ""
            integration.scheduled_sync_enabled = True
            integration.sync_interval_seconds = settings.CLIENT_INTEGRATION_INNOVATEMR_SYNC_INTERVAL_SECONDS
            response_status = status.HTTP_200_OK
        except Exception as exc:
            integration.last_test_status = "failed"
            integration.last_test_error = str(exc)[:10000]
            result = {"ok": False, "error": integration.last_test_error}
            response_status = status.HTTP_400_BAD_REQUEST
        integration.last_tested_at = timezone.now()
        integration.save(update_fields=[
            "last_tested_at", "last_test_status", "last_test_error",
            "scheduled_sync_enabled", "sync_interval_seconds", "updated_at",
        ])
        return Response(result, status=response_status)

    @extend_schema(
        tags=["Client integrations"],
        summary="Preview provider inventory without storing it",
        responses=IntegrationPreviewSerializer,
    )
    @action(detail=True, methods=["get"], url_path="preview")
    def preview(self, request, pk=None):
        from surveys.provider_services import provider_preview
        from surveys.providers import ProviderError

        try:
            data = provider_preview(self.get_object(), request.query_params.get("limit", 10))
        except (ProviderError, ValueError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(data)

    @action(detail=True, methods=["post"], url_path="sync-now")
    def sync_now(self, request, pk=None):
        from surveys.tasks import sync_client_integration_task

        integration = self.get_object()
        if integration.provider_code in {"rfg", "toluna", "track_opinion", "acuity", "unimarket"} and integration.last_test_status != "success":
            return Response(
                {"detail": "Test and verify the connection first."},
                status=status.HTTP_409_CONFLICT,
            )
        task = sync_client_integration_task.delay(integration.pk)
        return Response({"status": "queued", "task_id": task.id}, status=status.HTTP_202_ACCEPTED)


@extend_schema_view(
    list=extend_schema(tags=["Suppliers & allocations"], summary="List supplier CPI policies"),
    create=extend_schema(tags=["Suppliers & allocations"], summary="Create a supplier CPI policy"),
    retrieve=extend_schema(tags=["Suppliers & allocations"], summary="Get a supplier CPI policy"),
    update=extend_schema(tags=["Suppliers & allocations"], summary="Replace a supplier CPI policy"),
    partial_update=extend_schema(tags=["Suppliers & allocations"], summary="Update a supplier CPI policy"),
    destroy=extend_schema(tags=["Suppliers & allocations"], summary="Deactivate a supplier CPI policy"),
)
class VendorCommercialProfileViewSet(PermissionByActionMixin, viewsets.ModelViewSet):
    queryset = VendorCommercialProfile.objects.select_related(
        "vendor", "vendor__employee_profile", "created_by"
    ).filter(vendor__employee_profile__account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR)
    serializer_class = VendorCommercialProfileSerializer
    permission_classes = [HasFunctionPermission]
    view_permission = "vendors.tab.policies"
    manage_permission = "vendors.action.edit_policy"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["vendor", "vendor__employee_profile__account_type", "is_active"]
    search_fields = ["vendor__username", "vendor__first_name", "vendor__last_name", "vendor__email"]
    vendor_scope_filter = "vendor_id"


@extend_schema_view(
    list=extend_schema(tags=["Suppliers & allocations"], summary="List external-supplier API keys (masked)"),
    create=extend_schema(
        tags=["Suppliers & allocations"],
        summary="Issue an external-supplier API key",
        description="Returns the plaintext api_key once. Store it securely; later responses contain only the masked identifier.",
    ),
    retrieve=extend_schema(tags=["Suppliers & allocations"], summary="Get masked API-key metadata"),
    update=extend_schema(tags=["Suppliers & allocations"], summary="Replace API-key label or expiration"),
    partial_update=extend_schema(tags=["Suppliers & allocations"], summary="Update API-key label or expiration"),
    destroy=extend_schema(tags=["Suppliers & allocations"], summary="Revoke an external-supplier API key"),
)
class VendorAPIKeyViewSet(viewsets.ModelViewSet):
    queryset = VendorAPIKey.objects.select_related(
        "vendor", "vendor__employee_profile", "vendor__vendor_commercial_profile", "created_by"
    ).prefetch_related("client_allocations__client").filter(
        vendor__employee_profile__account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR
    )
    serializer_class = VendorAPIKeySerializer
    permission_classes = [HasFunctionPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["vendor", "is_active"]
    search_fields = ["name", "prefix", "vendor__username", "vendor__first_name", "vendor__last_name"]
    ordering_fields = ["created_at", "last_used_at", "expires_at", "name"]
    ordering = ["-created_at"]

    def get_required_function_permission(self):
        if self.action in {"list", "retrieve"}:
            return "vendors.tab.api_keys"
        if self.action == "destroy":
            return "vendors.action.revoke_api_key"
        return "vendors.action.create_api_key"

    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.none() if vendor_scope_user_id(self.request.user) else queryset

    def create(self, request, *args, **kwargs):
        if vendor_scope_user_id(request.user):
            raise PermissionDenied("Only the owner workspace can issue supplier API keys.")
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        if vendor_scope_user_id(self.request.user):
            raise PermissionDenied("Only the owner workspace can issue supplier API keys.")
        serializer.save()

    def perform_update(self, serializer):
        if vendor_scope_user_id(self.request.user):
            raise PermissionDenied("Only the owner workspace can change supplier API keys.")
        serializer.save()

    def destroy(self, request, *args, **kwargs):
        if vendor_scope_user_id(request.user):
            raise PermissionDenied("Only the owner workspace can revoke supplier API keys.")
        instance = self.get_object()
        if instance.is_active:
            instance.is_active = False
            instance.revoked_at = timezone.now()
            instance.save(update_fields=["is_active", "revoked_at", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    list=extend_schema(tags=["Suppliers & allocations"], summary="List supplier client grants"),
    create=extend_schema(tags=["Suppliers & allocations"], summary="Allocate a client to a supplier"),
    retrieve=extend_schema(tags=["Suppliers & allocations"], summary="Get a supplier client allocation"),
    update=extend_schema(tags=["Suppliers & allocations"], summary="Replace a supplier client allocation"),
    partial_update=extend_schema(tags=["Suppliers & allocations"], summary="Update a supplier client allocation"),
    destroy=extend_schema(tags=["Suppliers & allocations"], summary="Deactivate a supplier client allocation"),
)
class VendorClientAllocationViewSet(PermissionByActionMixin, viewsets.ModelViewSet):
    queryset = VendorClientAllocation.objects.select_related(
        "vendor", "vendor__employee_profile", "vendor__vendor_commercial_profile", "client", "created_by"
    ).prefetch_related("api_keys").filter(
        vendor__employee_profile__account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR
    )
    serializer_class = VendorClientAllocationSerializer
    permission_classes = [HasFunctionPermission]
    view_permission = "vendors.tab.clients"
    manage_permission = "vendors.action.allocate_client"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["vendor", "client", "vendor__employee_profile__account_type", "is_active"]
    search_fields = ["vendor__username", "vendor__first_name", "vendor__last_name", "client__name", "client__code"]
    ordering_fields = ["created_at", "updated_at", "client__name"]
    ordering = ["client__name", "vendor__username"]
    vendor_scope_filter = "vendor_id"


@extend_schema_view(
    list=extend_schema(tags=["Suppliers & allocations"], summary="List supplier project access overrides"),
    create=extend_schema(tags=["Suppliers & allocations"], summary="Create a project access override"),
    retrieve=extend_schema(tags=["Vendors & allocations"], summary="Get a project allocation"),
    update=extend_schema(tags=["Vendors & allocations"], summary="Replace a project allocation"),
    partial_update=extend_schema(tags=["Suppliers & allocations"], summary="Update a project access override"),
    destroy=extend_schema(tags=["Vendors & allocations"], summary="Deactivate a project allocation"),
)
class VendorSurveyAllocationViewSet(PermissionByActionMixin, viewsets.ModelViewSet):
    queryset = VendorSurveyAllocation.objects.select_related(
        "client_allocation", "client_allocation__vendor", "client_allocation__vendor__employee_profile",
        "client_allocation__vendor__vendor_commercial_profile", "client_allocation__client", "survey", "created_by",
    ).filter(
        client_allocation__vendor__employee_profile__account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR
    )
    serializer_class = VendorSurveyAllocationSerializer
    permission_classes = [HasFunctionPermission]
    view_permission = "vendors.tab.projects"
    manage_permission = "vendors.action.allocate_project"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["client_allocation", "client_allocation__vendor", "client_allocation__client", "survey", "is_active"]
    search_fields = [
        "client_allocation__vendor__username", "client_allocation__vendor__first_name",
        "client_allocation__vendor__last_name", "survey__local_id", "survey__source_id", "survey__name",
    ]
    ordering_fields = ["created_at", "updated_at", "survey__local_id", "survey__source_key"]
    ordering = ["survey__source_id", "client_allocation__vendor__username"]
    vendor_scope_filter = "client_allocation__vendor_id"

    @action(detail=False, methods=["get"], url_path="catalog")
    def catalog(self, request):
        """Return every client project; missing overrides mean access is on."""

        if vendor_scope_user_id(request.user):
            raise PermissionDenied("Only the owner workspace can manage supplier project access.")
        try:
            allocation_id = int(request.query_params.get("client_allocation", ""))
        except (TypeError, ValueError):
            return Response({"detail": "client_allocation is required."}, status=status.HTTP_400_BAD_REQUEST)
        allocation = VendorClientAllocation.objects.select_related("client", "vendor").filter(
            pk=allocation_id,
            vendor__employee_profile__account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR,
        ).first()
        if not allocation:
            return Response({"detail": "Client allocation was not found."}, status=status.HTTP_404_NOT_FOUND)
        surveys = Survey.objects.filter(client_id=allocation.client_id).order_by("local_id")
        search = str(request.query_params.get("search") or "").strip()
        if search:
            surveys = surveys.filter(
                Q(local_id__icontains=search)
                | Q(source_key__icontains=search)
                | Q(name__icontains=search)
            )
        try:
            page = max(1, int(request.query_params.get("page", 1)))
            page_size = min(100, max(10, int(request.query_params.get("page_size", 30))))
        except (TypeError, ValueError):
            page, page_size = 1, 30
        total = surveys.count()
        rows = list(surveys[(page - 1) * page_size:page * page_size])
        rules = {
            rule.survey_id: rule
            for rule in VendorSurveyAllocation.objects.filter(
                client_allocation=allocation,
                survey_id__in=[row.pk for row in rows],
            )
        }
        return Response({
            "count": total,
            "page": page,
            "page_size": page_size,
            "results": [
                {
                    "id": row.pk,
                    "project_id": row.local_id,
                    "survey_id": str(row.source_identifier),
                    "name": row.name,
                    "country": row.country_code or row.country,
                    "status": row.status,
                    "assigned": bool(rules[row.pk].is_active) if row.pk in rules else True,
                    "override_id": rules[row.pk].pk if row.pk in rules else None,
                }
                for row in rows
            ],
        })

    @action(detail=False, methods=["post"], url_path="set-access")
    def set_access(self, request):
        """Enable or exclude one project beneath an existing client grant."""

        if vendor_scope_user_id(request.user):
            raise PermissionDenied("Only the owner workspace can manage supplier project access.")
        try:
            allocation_id = int(request.data.get("client_allocation"))
            survey_id = int(request.data.get("survey"))
        except (TypeError, ValueError):
            return Response(
                {"detail": "client_allocation and survey are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        assigned = request.data.get("assigned")
        if not isinstance(assigned, bool):
            return Response({"detail": "assigned must be true or false."}, status=status.HTTP_400_BAD_REQUEST)
        allocation = VendorClientAllocation.objects.filter(
            pk=allocation_id,
            vendor__employee_profile__account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR,
        ).first()
        survey = Survey.objects.filter(pk=survey_id).first()
        if not allocation or not survey or survey.client_id != allocation.client_id:
            return Response({"detail": "Project does not belong to this client allocation."}, status=status.HTTP_400_BAD_REQUEST)
        rule = VendorSurveyAllocation.objects.filter(
            client_allocation=allocation,
            survey=survey,
        ).first()
        if assigned:
            if rule:
                rule.is_active = True
                rule.save(update_fields=["is_active", "updated_at"])
        elif rule:
            rule.is_active = False
            rule.save(update_fields=["is_active", "updated_at"])
        else:
            rule = VendorSurveyAllocation.objects.create(
                client_allocation=allocation,
                survey=survey,
                is_active=False,
                created_by=request.user,
            )
        return Response({
            "client_allocation": allocation.pk,
            "survey": survey.pk,
            "assigned": assigned,
            "override_id": rule.pk if rule else None,
        })


@extend_schema_view(
    list=extend_schema(tags=["Suppliers & allocations"], summary="List allocation reservation audit records"),
    retrieve=extend_schema(tags=["Suppliers & allocations"], summary="Get an allocation reservation audit record"),
)
class AllocationReservationViewSet(VendorScopedQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    queryset = AllocationReservation.objects.select_related(
        "attempt", "client_allocation", "client_allocation__vendor", "survey_allocation", "survey_allocation__survey",
    ).all()
    serializer_class = AllocationReservationSerializer
    permission_classes = [HasFunctionPermission]
    required_function_permission = ("allocations.view", "allocations.manage")
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["status", "client_allocation", "client_allocation__vendor", "survey_allocation"]
    search_fields = ["attempt__rid", "client_allocation__vendor__username", "survey_allocation__survey__local_id"]
    ordering_fields = ["created_at", "expires_at", "finalized_at", "status"]
    ordering = ["-created_at"]
    vendor_scope_filter = "client_allocation__vendor_id"
