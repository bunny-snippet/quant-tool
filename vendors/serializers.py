from decimal import Decimal
import ipaddress
import re

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from accounts.models import EmployeeProfile
from accounts.access import has_function_access

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
from .credentials import set_integration_token


class VendorDirectorySerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    account_type = serializers.CharField(source="employee_profile.account_type", read_only=True)
    role_name = serializers.CharField(source="employee_profile.role.name", read_only=True, allow_null=True)
    created_by = serializers.CharField(source="employee_profile.created_by.username", read_only=True, allow_null=True)
    commercial_profile_id = serializers.IntegerField(source="vendor_commercial_profile.id", read_only=True, allow_null=True)
    default_cpi_cut_percent = serializers.DecimalField(
        source="vendor_commercial_profile.default_cpi_cut_percent",
        max_digits=5,
        decimal_places=2,
        read_only=True,
        allow_null=True,
    )
    currency = serializers.CharField(source="vendor_commercial_profile.currency", read_only=True, allow_null=True)
    delivery_mode = serializers.CharField(source="vendor_commercial_profile.delivery_mode", read_only=True, allow_null=True)
    api_key_count = serializers.IntegerField(read_only=True)
    allocation_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = get_user_model()
        fields = [
            "id", "username", "full_name", "email", "account_type", "role_name", "created_by",
            "commercial_profile_id", "default_cpi_cut_percent", "currency", "delivery_mode",
            "allocation_count", "api_key_count",
            "is_active", "date_joined",
        ]
        read_only_fields = fields

    def get_full_name(self, obj) -> str:
        return obj.get_full_name() or obj.username


class VendorManagementVendorOptionSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    full_name = serializers.CharField()
    username = serializers.CharField()
    account_type = serializers.ChoiceField(choices=EmployeeProfile.AccountType.choices)


class VendorManagementClientOptionSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    code = serializers.CharField()
    provider_code = serializers.CharField()


class VendorManagementOptionsSerializer(serializers.Serializer):
    vendors = VendorManagementVendorOptionSerializer(many=True)
    clients = VendorManagementClientOptionSerializer(many=True)


class OrganizationOwnerOptionSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    username = serializers.CharField()
    type = serializers.ChoiceField(choices=[("owner", "Main office"), ("internal_vendor", "Internal supplier")])


class OrganizationManagementOptionsSerializer(serializers.Serializer):
    owners = OrganizationOwnerOptionSerializer(many=True)
    clients = VendorManagementClientOptionSerializer(many=True)
    client_eligibility = serializers.DictField(child=serializers.ListField(child=serializers.IntegerField()))


class ClientIntegrationSerializer(serializers.ModelSerializer):
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)
    client_name = serializers.CharField(source="client.name", read_only=True)
    api_token = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=False)
    has_credential = serializers.SerializerMethodField()
    masked_credential = serializers.SerializerMethodField()
    survey_count = serializers.IntegerField(source="surveys.count", read_only=True)
    config = serializers.JSONField(
        required=False,
        help_text=(
            "Provider-specific settings. RFG supports enforce_local_targeting: true for Strict "
            "local termination or false for Relaxed provider-side eligibility decisions."
        ),
    )

    class Meta:
        model = ClientIntegration
        fields = [
            "id", "client", "client_name", "name", "provider_code", "base_url", "credential_env_key",
            "credential_env_keys", "config",
            "api_token", "has_credential", "masked_credential", "supplier_code", "scheduled_sync_enabled",
            "inventory_endpoint", "paged_inventory_endpoint", "quota_endpoint_template",
            "targeting_endpoint_template", "transaction_endpoint_template", "auth_header_name",
            "auth_header_prefix", "inventory_result_key", "quota_result_key", "targeting_result_key",
            "transaction_result_key", "field_mapping",
            "sync_interval_seconds", "detail_refresh_batch", "is_active", "survey_count",
            "last_tested_at", "last_test_status", "last_test_error", "last_sync_started_at",
            "last_sync_finished_at", "last_sync_status", "last_sync_error", "last_sync_summary",
            "created_by", "created_at", "updated_at",
        ]
        read_only_fields = [
            "has_credential", "masked_credential", "last_tested_at", "last_test_status", "last_test_error",
            "last_sync_started_at", "last_sync_finished_at", "last_sync_status", "last_sync_error",
            "last_sync_summary", "created_at", "updated_at",
        ]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        provider = str(attrs.get("provider_code", getattr(self.instance, "provider_code", ""))).lower()
        provider_key = provider.replace("-", "").replace("_", "")
        base_url = str(attrs.get("base_url", getattr(self.instance, "base_url", ""))).rstrip("/")

        def set_default(field, value):
            if field not in attrs and not getattr(self.instance, field, ""):
                attrs[field] = value

        if provider_key == "rfg":
            attrs.setdefault("base_url", "https://api.researchforgood.com/API")
            credential_refs = attrs.get(
                "credential_env_keys", getattr(self.instance, "credential_env_keys", {})
            ) or {}
            if set(credential_refs) != {"apid", "secret"}:
                raise serializers.ValidationError({
                    "credential_env_keys": "RFG requires apid and secret environment-variable mappings."
                })
            env_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
            if any(
                not isinstance(value, str) or not env_pattern.fullmatch(value)
                for value in credential_refs.values()
            ):
                raise serializers.ValidationError({
                    "credential_env_keys": "Use environment-variable names here, never credential values."
                })
            config = attrs.get("config", getattr(self.instance, "config", {})) or {}
            allowed_config = {
                "country", "category", "allow_recontacts", "locale", "timeout_seconds",
                "detail_refresh_batch", "callback_security_mode", "callback_ip_allowlist",
                "enforce_local_targeting",
            }
            unexpected = set(config) - allowed_config
            if unexpected:
                raise serializers.ValidationError({
                    "config": f"Unsupported RFG settings: {', '.join(sorted(unexpected))}."
                })
            country = str(config.get("country") or "")
            if country and not re.fullmatch(r"[A-Za-z]{2}", country):
                raise serializers.ValidationError({"config": "Country must be a two-letter ISO code."})
            if config.get("category") not in {None, "", "B2B", "B2C"}:
                raise serializers.ValidationError({"config": "Category must be B2B or B2C."})
            if "enforce_local_targeting" in config and not isinstance(config["enforce_local_targeting"], bool):
                raise serializers.ValidationError({"config": "Strict targeting mode must be true or false."})
            if config.get("callback_security_mode", "ip") != "ip":
                raise serializers.ValidationError({"config": "Only RFG's documented server-IP callback mode is supported."})
            for address in config.get("callback_ip_allowlist") or []:
                try:
                    ipaddress.ip_address(address)
                except ValueError as exc:
                    raise serializers.ValidationError({"config": f"Invalid callback IP: {address}."}) from exc
            try:
                interval = int(attrs.get(
                    "sync_interval_seconds", getattr(self.instance, "sync_interval_seconds", 60)
                ))
            except (TypeError, ValueError) as exc:
                raise serializers.ValidationError({
                    "sync_interval_seconds": "Sync interval must be a whole number."
                }) from exc
            if interval < 60:
                raise serializers.ValidationError({
                    "sync_interval_seconds": "RFG inventory sync must be at least 60 seconds."
                })
            if attrs.get("scheduled_sync_enabled", False) and getattr(
                self.instance, "last_test_status", ""
            ) != "success":
                raise serializers.ValidationError({
                    "scheduled_sync_enabled": "Test and verify this connection before scheduling it."
                })
        elif provider_key in {"biobrain", "voqall"} or "voqall.com" in base_url.lower():
            api_root = base_url[:-8] if base_url.lower().endswith("/surveys") else base_url
            current_inventory = attrs.get(
                "inventory_endpoint", getattr(self.instance, "inventory_endpoint", "")
            )
            if not base_url.lower().endswith("/surveys") and not current_inventory:
                attrs["inventory_endpoint"] = "/surveys"
            set_default("auth_header_name", "EQ-PARTNER-ACCESS-KEY")
            set_default("inventory_result_key", "Surveys")
            set_default("quota_endpoint_template", f"{api_root}/survey-quotas/{{survey_id}}")
            set_default("targeting_endpoint_template", f"{api_root}/survey-qualifications/{{survey_id}}")
            set_default("quota_result_key", "Quotas")
            set_default("targeting_result_key", "Qualifications")
        elif provider_key == "innovatemr":
            set_default("inventory_endpoint", "/supply/getAllocatedSurveys")
            set_default("paged_inventory_endpoint", "/supply/getAllocatedSurveysPaged")
            set_default("quota_endpoint_template", "/supply/getQuotaForSurvey/{survey_id}")
            set_default("targeting_endpoint_template", "/supply/getSurveyTargeting/{survey_id}")
            set_default("transaction_endpoint_template", "/supply/getSurveyTransactionsByCond/{survey_id}/{pid}")
            set_default("auth_header_name", "x-access-token")
            set_default("inventory_result_key", "result")
        return attrs

    def get_has_credential(self, obj) -> bool:
        return bool(obj.encrypted_api_token or obj.credential_env_key or obj.credential_env_keys)

    def get_masked_credential(self, obj) -> str:
        return f"••••{obj.credential_last_four}" if obj.credential_last_four else ""

    def create(self, validated_data):
        token = validated_data.pop("api_token", None)
        instance = super().create(validated_data)
        if token is not None:
            set_integration_token(instance, token)
        return instance

    def update(self, instance, validated_data):
        token = validated_data.pop("api_token", None)
        connection_fields = {"provider_code", "base_url", "credential_env_keys", "config"}
        connection_changed = any(
            field in validated_data and validated_data[field] != getattr(instance, field)
            for field in connection_fields
        )
        instance = super().update(instance, validated_data)
        if token is not None:
            set_integration_token(instance, token)
        if connection_changed and instance.provider_code == "rfg":
            instance.last_test_status = ""
            instance.last_test_error = "Connection settings changed; test the connection again."
            instance.scheduled_sync_enabled = False
            instance.save(update_fields=[
                "last_test_status", "last_test_error", "scheduled_sync_enabled", "updated_at"
            ])
        return instance


class ProviderCredentialFieldSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()


class ProviderCatalogSerializer(serializers.Serializer):
    code = serializers.CharField()
    label = serializers.CharField()
    default_base_url = serializers.URLField(allow_blank=True)
    minimum_sync_interval_seconds = serializers.IntegerField(min_value=60)
    credential_fields = ProviderCredentialFieldSerializer(many=True)


class IntegrationActionResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()
    result = serializers.JSONField(required=False)
    task_id = serializers.CharField(required=False)


class IntegrationPreviewRowSerializer(serializers.Serializer):
    source_id = serializers.CharField()
    name = serializers.CharField(allow_blank=True)
    country = serializers.CharField(allow_blank=True)
    cpi = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    loi = serializers.IntegerField(allow_null=True)
    status = serializers.CharField()
    modified_at = serializers.DateTimeField(allow_null=True)


class IntegrationPreviewSerializer(serializers.Serializer):
    total_received = serializers.IntegerField(min_value=0)
    results = IntegrationPreviewRowSerializer(many=True)


class ClientSerializer(serializers.ModelSerializer):
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)
    integrations = serializers.SerializerMethodField()

    class Meta:
        model = Client
        fields = [
            "id", "code", "name", "provider_code", "company_name_match", "is_active",
            "created_by", "created_at", "updated_at", "integrations",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_integrations(self, obj) -> list[dict]:
        request = self.context.get("request")
        if not request or not has_function_access(request.user, "clients.integration.view"):
            return []
        return ClientIntegrationSerializer(obj.integrations.all(), many=True, context=self.context).data


class OrganizationUnitSerializer(serializers.ModelSerializer):
    workspace_owner_name = serializers.SerializerMethodField()
    workspace_owner_type = serializers.SerializerMethodField()
    parent_name = serializers.CharField(source="parent.name", read_only=True, allow_null=True)
    path = serializers.CharField(source="path_label", read_only=True)
    member_count = serializers.SerializerMethodField()
    client_count = serializers.SerializerMethodField()
    direct_member_count = serializers.SerializerMethodField()
    direct_client_count = serializers.SerializerMethodField()
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)

    class Meta:
        model = OrganizationUnit
        fields = [
            "id", "workspace_owner", "workspace_owner_name", "workspace_owner_type", "parent", "parent_name",
            "unit_type", "name", "code", "description", "path", "member_count", "client_count",
            "direct_member_count", "direct_client_count",
            "is_active", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_workspace_owner_name(self, obj) -> str:
        return obj.workspace_owner.get_full_name() or obj.workspace_owner.username

    def get_workspace_owner_type(self, obj) -> str:
        if obj.workspace_owner.is_superuser:
            return "owner"
        return getattr(getattr(obj.workspace_owner, "employee_profile", None), "account_type", "")

    def _rollup_value(self, obj, key: str) -> int:
        return int(self.context.get("organization_rollup_counts", {}).get(key, {}).get(obj.pk, 0))

    def get_member_count(self, obj) -> int:
        return self._rollup_value(obj, "members")

    def get_client_count(self, obj) -> int:
        return self._rollup_value(obj, "clients")

    def get_direct_member_count(self, obj) -> int:
        return self._rollup_value(obj, "direct_members")

    def get_direct_client_count(self, obj) -> int:
        return self._rollup_value(obj, "direct_clients")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context.get("request")
        owner = attrs.get("workspace_owner", getattr(self.instance, "workspace_owner", None))
        if request:
            from .access import organization_workspace_owner_ids
            if not owner or owner.pk not in organization_workspace_owner_ids(request.user):
                raise serializers.ValidationError({"workspace_owner": "You cannot manage this organization workspace."})
        if self.instance and "workspace_owner" in attrs and owner != self.instance.workspace_owner:
            raise serializers.ValidationError({"workspace_owner": "An organization unit cannot be moved to another workspace."})
        values = {
            "workspace_owner": owner,
            "parent": attrs.get("parent", getattr(self.instance, "parent", None)),
            "unit_type": attrs.get("unit_type", getattr(self.instance, "unit_type", None)),
            "name": attrs.get("name", getattr(self.instance, "name", "")),
            "code": attrs.get("code", getattr(self.instance, "code", "")),
            "description": attrs.get("description", getattr(self.instance, "description", "")),
            "is_active": attrs.get("is_active", getattr(self.instance, "is_active", True)),
        }
        candidate = OrganizationUnit(pk=getattr(self.instance, "pk", None), **values)
        if self.instance:
            candidate._state.adding = False
            candidate._state.db = self.instance._state.db
        try:
            candidate.full_clean(exclude=["created_by"])
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        return attrs


class OrganizationClientAccessSerializer(serializers.ModelSerializer):
    workspace_owner = serializers.IntegerField(source="organization_unit.workspace_owner_id", read_only=True)
    workspace_owner_name = serializers.SerializerMethodField()
    unit_name = serializers.CharField(source="organization_unit.name", read_only=True)
    unit_type = serializers.CharField(source="organization_unit.unit_type", read_only=True)
    unit_path = serializers.CharField(source="organization_unit.path_label", read_only=True)
    client_name = serializers.CharField(source="client.name", read_only=True)
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)

    class Meta:
        model = OrganizationClientAccess
        fields = [
            "id", "organization_unit", "workspace_owner", "workspace_owner_name", "unit_name", "unit_type",
            "unit_path", "client", "client_name", "is_active", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_workspace_owner_name(self, obj) -> str:
        owner = obj.organization_unit.workspace_owner
        return owner.get_full_name() or owner.username

    def validate(self, attrs):
        attrs = super().validate(attrs)
        unit = attrs.get("organization_unit", getattr(self.instance, "organization_unit", None))
        client = attrs.get("client", getattr(self.instance, "client", None))
        request = self.context.get("request")
        if request:
            from .access import organization_workspace_owner_ids
            if not unit or unit.workspace_owner_id not in organization_workspace_owner_ids(request.user):
                raise serializers.ValidationError({"organization_unit": "You cannot manage this organization workspace."})
        candidate = OrganizationClientAccess(
            pk=getattr(self.instance, "pk", None),
            organization_unit=unit,
            client=client,
            is_active=attrs.get("is_active", getattr(self.instance, "is_active", True)),
        )
        if self.instance:
            candidate._state.adding = False
            candidate._state.db = self.instance._state.db
        try:
            candidate.full_clean(exclude=["created_by"])
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        return attrs


class VendorCommercialProfileSerializer(serializers.ModelSerializer):
    vendor_name = serializers.SerializerMethodField()
    account_type = serializers.CharField(source="vendor.employee_profile.account_type", read_only=True)
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)

    class Meta:
        model = VendorCommercialProfile
        fields = [
            "id", "vendor", "vendor_name", "account_type", "default_cpi_cut_percent", "currency",
            "delivery_mode", "is_active", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_vendor_name(self, obj) -> str:
        return obj.vendor.get_full_name() or obj.vendor.username

    def validate(self, attrs):
        attrs = super().validate(attrs)
        vendor = attrs.get("vendor", getattr(self.instance, "vendor", None))
        cut = attrs.get("default_cpi_cut_percent", getattr(self.instance, "default_cpi_cut_percent", Decimal("0.00")))
        profile = EmployeeProfile.objects.filter(user=vendor).first()
        if not profile or profile.account_type not in {
            EmployeeProfile.AccountType.INTERNAL_VENDOR,
            EmployeeProfile.AccountType.EXTERNAL_VENDOR,
        }:
            raise serializers.ValidationError({"vendor": "Select an internal or external supplier account."})
        if profile.account_type == EmployeeProfile.AccountType.INTERNAL_VENDOR and cut != Decimal("0.00"):
            raise serializers.ValidationError({"default_cpi_cut_percent": "Internal supplier cut must be zero."})
        delivery_mode = attrs.get("delivery_mode", getattr(self.instance, "delivery_mode", VendorCommercialProfile.DeliveryMode.PANEL))
        if profile.account_type == EmployeeProfile.AccountType.INTERNAL_VENDOR and delivery_mode != VendorCommercialProfile.DeliveryMode.PANEL:
            raise serializers.ValidationError({"delivery_mode": "Internal suppliers use panel-only delivery."})
        return attrs


class VendorAPIKeySerializer(serializers.ModelSerializer):
    vendor_name = serializers.SerializerMethodField()
    account_type = serializers.CharField(source="vendor.employee_profile.account_type", read_only=True)
    masked_key = serializers.CharField(read_only=True)
    api_key = serializers.CharField(read_only=True, required=False)
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)

    class Meta:
        model = VendorAPIKey
        fields = [
            "id", "vendor", "vendor_name", "account_type", "name", "prefix", "last_four", "masked_key",
            "api_key", "is_active", "expires_at", "last_used_at", "revoked_at", "created_by",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "prefix", "last_four", "is_active", "last_used_at", "revoked_at", "created_at", "updated_at",
        ]

    def get_vendor_name(self, obj) -> str:
        return obj.vendor.get_full_name() or obj.vendor.username

    def validate(self, attrs):
        attrs = super().validate(attrs)
        vendor = attrs.get("vendor", getattr(self.instance, "vendor", None))
        if self.instance and "vendor" in attrs and attrs["vendor"] != self.instance.vendor:
            raise serializers.ValidationError({"vendor": "An issued API key cannot be transferred to another supplier."})
        profile = getattr(vendor, "employee_profile", None) if vendor else None
        if not profile or profile.account_type != EmployeeProfile.AccountType.EXTERNAL_VENDOR:
            raise serializers.ValidationError({"vendor": "API keys can only be issued to external suppliers."})
        commercial = getattr(vendor, "vendor_commercial_profile", None)
        if not commercial or not commercial.is_active or not commercial.api_access_enabled:
            raise serializers.ValidationError({"vendor": "Enable API or Panel + API delivery before issuing a key."})
        expires_at = attrs.get("expires_at", getattr(self.instance, "expires_at", None))
        if expires_at and expires_at <= timezone.now():
            raise serializers.ValidationError({"expires_at": "Expiration must be in the future."})
        return attrs

    def create(self, validated_data):
        from .security import generate_api_key

        raw_key, prefix, last_four, key_hash = generate_api_key()
        request = self.context.get("request")
        instance = VendorAPIKey.objects.create(
            **validated_data,
            prefix=prefix,
            last_four=last_four,
            key_hash=key_hash,
            created_by=request.user if request else None,
        )
        instance.api_key = raw_key
        return instance


class VendorClientAllocationSerializer(serializers.ModelSerializer):
    vendor_name = serializers.SerializerMethodField()
    client_name = serializers.CharField(source="client.name", read_only=True)
    account_type = serializers.CharField(source="vendor.employee_profile.account_type", read_only=True)
    remaining_quantity = serializers.IntegerField(read_only=True)
    effective_cpi_cut_percent = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True)
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)

    class Meta:
        model = VendorClientAllocation
        fields = [
            "id", "vendor", "vendor_name", "account_type", "client", "client_name", "quantity_limit",
            "reserved_quantity", "consumed_quantity", "remaining_quantity", "cpi_cut_override_percent",
            "effective_cpi_cut_percent", "starts_at", "ends_at", "is_active", "created_by",
            "created_at", "updated_at",
        ]
        read_only_fields = ["reserved_quantity", "consumed_quantity", "created_at", "updated_at"]

    def get_vendor_name(self, obj) -> str:
        return obj.vendor.get_full_name() or obj.vendor.username

    def validate(self, attrs):
        attrs = super().validate(attrs)
        vendor = attrs.get("vendor", getattr(self.instance, "vendor", None))
        quantity_limit = attrs.get("quantity_limit", getattr(self.instance, "quantity_limit", 0))
        cut = attrs.get("cpi_cut_override_percent", getattr(self.instance, "cpi_cut_override_percent", None))
        profile = EmployeeProfile.objects.filter(user=vendor).first()
        if not profile or profile.account_type not in {
            EmployeeProfile.AccountType.INTERNAL_VENDOR,
            EmployeeProfile.AccountType.EXTERNAL_VENDOR,
        }:
            raise serializers.ValidationError({"vendor": "Select an internal or external supplier account."})
        if profile.account_type == EmployeeProfile.AccountType.INTERNAL_VENDOR and cut not in {None, Decimal("0.00")}:
            raise serializers.ValidationError({"cpi_cut_override_percent": "Internal supplier cut must be zero."})
        instance = self.instance
        used = (instance.consumed_quantity + instance.reserved_quantity) if instance else 0
        if quantity_limit < used:
            raise serializers.ValidationError({"quantity_limit": "Limit cannot be below consumed plus reserved quantity."})
        starts_at = attrs.get("starts_at", getattr(instance, "starts_at", None))
        ends_at = attrs.get("ends_at", getattr(instance, "ends_at", None))
        if starts_at and ends_at and ends_at <= starts_at:
            raise serializers.ValidationError({"ends_at": "End time must be after start time."})
        return attrs


class VendorSurveyAllocationSerializer(serializers.ModelSerializer):
    vendor = serializers.IntegerField(source="client_allocation.vendor_id", read_only=True)
    vendor_name = serializers.SerializerMethodField()
    client = serializers.IntegerField(source="client_allocation.client_id", read_only=True)
    client_name = serializers.CharField(source="client_allocation.client.name", read_only=True)
    survey_local_id = serializers.CharField(source="survey.local_id", read_only=True)
    survey_source_id = serializers.SerializerMethodField()
    survey_name = serializers.CharField(source="survey.name", read_only=True)
    remaining_quantity = serializers.IntegerField(read_only=True)
    effective_cpi_cut_percent = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True)
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)

    class Meta:
        model = VendorSurveyAllocation
        fields = [
            "id", "client_allocation", "vendor", "vendor_name", "client", "client_name", "survey",
            "survey_local_id", "survey_source_id", "survey_name", "quantity_limit", "reserved_quantity",
            "consumed_quantity", "remaining_quantity", "cpi_cut_override_percent",
            "effective_cpi_cut_percent", "starts_at", "ends_at", "is_active", "created_by",
            "created_at", "updated_at",
        ]
        read_only_fields = ["reserved_quantity", "consumed_quantity", "created_at", "updated_at"]

    def get_vendor_name(self, obj) -> str:
        return obj.vendor.get_full_name() or obj.vendor.username

    @extend_schema_field({"oneOf": [{"type": "integer"}, {"type": "string"}]})
    def get_survey_source_id(self, obj):
        return obj.survey.source_identifier

    def validate(self, attrs):
        attrs = super().validate(attrs)
        parent = attrs.get("client_allocation", getattr(self.instance, "client_allocation", None))
        survey = attrs.get("survey", getattr(self.instance, "survey", None))
        quantity_limit = attrs.get("quantity_limit", getattr(self.instance, "quantity_limit", 0))
        cut = attrs.get("cpi_cut_override_percent", getattr(self.instance, "cpi_cut_override_percent", None))
        if parent and survey and survey.client_id != parent.client_id:
            raise serializers.ValidationError({"survey": "Survey must belong to the parent allocation's client."})
        account_type = getattr(getattr(parent.vendor, "employee_profile", None), "account_type", "") if parent else ""
        if account_type == EmployeeProfile.AccountType.INTERNAL_VENDOR and cut not in {None, Decimal("0.00")}:
            raise serializers.ValidationError({"cpi_cut_override_percent": "Internal supplier cut must be zero."})
        instance = self.instance
        used = (instance.consumed_quantity + instance.reserved_quantity) if instance else 0
        if quantity_limit < used:
            raise serializers.ValidationError({"quantity_limit": "Limit cannot be below consumed plus reserved quantity."})
        starts_at = attrs.get("starts_at", getattr(instance, "starts_at", None))
        ends_at = attrs.get("ends_at", getattr(instance, "ends_at", None))
        if starts_at and ends_at and ends_at <= starts_at:
            raise serializers.ValidationError({"ends_at": "End time must be after start time."})
        return attrs


class AllocationReservationSerializer(serializers.ModelSerializer):
    rid = serializers.CharField(source="attempt.rid", read_only=True)
    vendor = serializers.IntegerField(source="client_allocation.vendor_id", read_only=True)
    survey = serializers.IntegerField(source="survey_allocation.survey_id", read_only=True, allow_null=True)

    class Meta:
        model = AllocationReservation
        fields = [
            "id", "attempt", "rid", "vendor", "client_allocation", "survey_allocation", "survey",
            "quantity", "status", "expires_at", "finalized_at", "reason", "created_at", "updated_at",
        ]
        read_only_fields = fields
