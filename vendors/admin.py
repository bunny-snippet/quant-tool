from django.contrib import admin

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


@admin.register(OrganizationUnit)
class OrganizationUnitAdmin(admin.ModelAdmin):
    list_display = ["name", "unit_type", "workspace_owner", "parent", "is_active", "updated_at"]
    list_filter = ["unit_type", "workspace_owner", "is_active"]
    search_fields = ["name", "code", "workspace_owner__username", "workspace_owner__email"]


@admin.register(OrganizationClientAccess)
class OrganizationClientAccessAdmin(admin.ModelAdmin):
    list_display = ["organization_unit", "client", "is_active", "updated_at"]
    list_filter = ["organization_unit__workspace_owner", "client", "is_active"]
    search_fields = ["organization_unit__name", "organization_unit__code", "client__name", "client__code"]


class ClientIntegrationInline(admin.TabularInline):
    model = ClientIntegration
    extra = 0


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "provider_code", "is_active", "updated_at"]
    search_fields = ["name", "code", "company_name_match"]
    list_filter = ["provider_code", "is_active"]
    inlines = [ClientIntegrationInline]


@admin.register(VendorCommercialProfile)
class VendorCommercialProfileAdmin(admin.ModelAdmin):
    list_display = ["vendor", "default_cpi_cut_percent", "currency", "delivery_mode", "is_active", "updated_at"]
    search_fields = ["vendor__username", "vendor__first_name", "vendor__last_name", "vendor__email"]
    list_filter = ["is_active", "currency"]


@admin.register(VendorAPIKey)
class VendorAPIKeyAdmin(admin.ModelAdmin):
    list_display = ["vendor", "name", "masked_key", "survey_id_mode", "callback_signing_enabled", "is_active", "expires_at", "last_used_at", "created_at"]
    search_fields = ["vendor__username", "vendor__email", "name", "prefix"]
    list_filter = ["is_active", "created_at", "expires_at"]
    readonly_fields = ["prefix", "last_four", "key_hash", "encrypted_callback_secret", "callback_secret_last_four", "last_used_at", "revoked_at", "created_at", "updated_at"]


@admin.register(VendorClientAllocation)
class VendorClientAllocationAdmin(admin.ModelAdmin):
    list_display = [
        "vendor", "client", "cpi_cut_override_percent", "min_cpi", "max_cpi", "is_active",
    ]
    search_fields = ["vendor__username", "vendor__email", "client__name", "client__code"]
    list_filter = ["client", "is_active"]
    readonly_fields = ["created_at", "updated_at"]
    exclude = ["quantity_limit", "reserved_quantity", "consumed_quantity"]


@admin.register(VendorSurveyAllocation)
class VendorSurveyAllocationAdmin(admin.ModelAdmin):
    list_display = [
        "vendor_name", "survey", "cpi_cut_override_percent", "is_active",
    ]
    search_fields = [
        "client_allocation__vendor__username", "client_allocation__vendor__email",
        "survey__local_id", "survey__source_key", "survey__source_id", "survey__name",
    ]
    list_filter = ["client_allocation__client", "is_active"]
    readonly_fields = ["created_at", "updated_at"]
    exclude = ["quantity_limit", "reserved_quantity", "consumed_quantity"]

    @admin.display(description="Vendor")
    def vendor_name(self, obj):
        return obj.vendor

@admin.register(AllocationReservation)
class AllocationReservationAdmin(admin.ModelAdmin):
    list_display = ["attempt", "survey_allocation", "status", "quantity", "expires_at", "finalized_at"]
    search_fields = ["attempt__rid", "client_allocation__vendor__username", "survey_allocation__survey__local_id"]
    list_filter = ["status", "expires_at"]
    readonly_fields = [field.name for field in AllocationReservation._meta.fields]
