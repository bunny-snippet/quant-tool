from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import serializers

from .access import (
    EXTERNAL_VENDOR_FORBIDDEN_CODES,
    assignable_functions,
    assignable_roles,
    has_function_access,
)
from .models import AccessFunction, EmployeeProfile, Role, RoleFunctionPermission, UserFunctionOverride
from vendors.models import OrganizationUnit


class AccessFunctionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AccessFunction
        fields = ["id", "code", "name", "module", "description", "is_active", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]


class RoleSerializer(serializers.ModelSerializer):
    permission_codes = serializers.ListField(child=serializers.CharField(max_length=120), required=False, write_only=True)
    effective_permission_codes = serializers.SerializerMethodField()
    employee_count = serializers.IntegerField(read_only=True, default=0)
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)

    class Meta:
        model = Role
        fields = [
            "id", "name", "slug", "description", "rank", "cpi_visibility_percent", "is_system", "is_active", "employee_count",
            "permission_codes", "effective_permission_codes", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = ["is_system", "created_at", "updated_at"]

    def get_effective_permission_codes(self, obj) -> list[str]:
        assignments = getattr(obj, "_access_function_assignments", None)
        if assignments is None:
            assignments = getattr(obj, "_prefetched_objects_cache", {}).get(
                "function_assignments"
            )
        if assignments is None:
            return list(
                obj.function_assignments.filter(
                    allowed=True,
                    function__is_active=True,
                ).values_list("function__code", flat=True)
            )
        return [
            assignment.function.code
            for assignment in assignments
            if assignment.allowed and assignment.function.is_active
        ]

    def validate_permission_codes(self, codes):
        codes = list(dict.fromkeys(codes))
        found = set(AccessFunction.objects.filter(code__in=codes).values_list("code", flat=True))
        missing = sorted(set(codes) - found)
        if missing:
            raise serializers.ValidationError(f"Unknown function codes: {', '.join(missing)}")
        request = self.context.get("request")
        if request and not request.user.is_superuser:
            grantable = set(assignable_functions(request.user).values_list("code", flat=True))
            forbidden = sorted(set(codes) - grantable)
            if forbidden:
                raise serializers.ValidationError(f"You cannot delegate functions you do not have: {', '.join(forbidden)}")
        return codes

    def _set_permissions(self, role, codes):
        functions = AccessFunction.objects.filter(code__in=codes)
        role.function_assignments.exclude(function__in=functions).delete()
        for function in functions:
            RoleFunctionPermission.objects.update_or_create(role=role, function=function, defaults={"allowed": True})
        getattr(role, "_prefetched_objects_cache", {}).pop("function_assignments", None)
        if hasattr(role, "_access_function_assignments"):
            delattr(role, "_access_function_assignments")

    @transaction.atomic
    def create(self, validated_data):
        codes = validated_data.pop("permission_codes", [])
        request = self.context.get("request")
        if request:
            validated_data["created_by"] = request.user
        role = super().create(validated_data)
        self._set_permissions(role, codes)
        return role

    @transaction.atomic
    def update(self, instance, validated_data):
        codes = validated_data.pop("permission_codes", None)
        role = super().update(instance, validated_data)
        if codes is not None:
            self._set_permissions(role, codes)
        return role


class UserAccessSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    role = serializers.SlugField(write_only=True, required=False, allow_null=True)
    role_details = serializers.SerializerMethodField()
    allow_codes = serializers.ListField(child=serializers.CharField(max_length=120), required=False, write_only=True)
    deny_codes = serializers.ListField(child=serializers.CharField(max_length=120), required=False, write_only=True)
    allowed_overrides = serializers.SerializerMethodField()
    denied_overrides = serializers.SerializerMethodField()
    effective_permissions = serializers.SerializerMethodField()
    password = serializers.CharField(write_only=True, required=False, min_length=8)
    account_type = serializers.ChoiceField(choices=EmployeeProfile.AccountType.choices, write_only=True, required=False)
    account_type_details = serializers.SerializerMethodField()
    company_name = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=160)
    company = serializers.CharField(source="employee_profile.company_name", read_only=True)
    department = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=120)
    sub_branch = serializers.CharField(source="employee_profile.department", read_only=True)
    created_by = serializers.CharField(source="employee_profile.created_by.username", read_only=True, allow_null=True)
    organization_unit = serializers.PrimaryKeyRelatedField(
        queryset=OrganizationUnit.objects.filter(is_active=True),
        required=False,
        allow_null=True,
        write_only=True,
    )
    organization_unit_details = serializers.SerializerMethodField()

    class Meta:
        model = get_user_model()
        fields = [
            "id", "username", "first_name", "last_name", "full_name", "email", "is_active", "last_login",
            "role", "role_details", "allow_codes", "deny_codes", "allowed_overrides", "denied_overrides",
            "effective_permissions", "password", "account_type", "account_type_details", "company_name", "company",
            "department", "sub_branch", "created_by",
            "organization_unit", "organization_unit_details",
        ]
        extra_kwargs = {"username": {"required": False}}
        read_only_fields = ["last_login"]

    def get_full_name(self, obj) -> str:
        return obj.get_full_name() or obj.username

    def get_role_details(self, obj) -> dict | None:
        profile = getattr(obj, "employee_profile", None)
        return {"slug": profile.role.slug, "name": profile.role.name} if profile and profile.role else None

    def get_account_type_details(self, obj) -> dict:
        profile = getattr(obj, "employee_profile", None)
        return {"value": profile.account_type, "label": profile.get_account_type_display()} if profile else {"value": "employee", "label": "Employee"}

    def get_organization_unit_details(self, obj) -> dict | None:
        profile = getattr(obj, "employee_profile", None)
        unit = getattr(profile, "organization_unit", None) if profile else None
        if not unit:
            return None
        owner = unit.workspace_owner
        return {
            "id": unit.pk,
            "name": unit.name,
            "type": unit.unit_type,
            "type_label": unit.get_unit_type_display(),
            "path": unit.path_label,
            "workspace_owner": owner.pk,
            "workspace_owner_name": owner.get_full_name() or owner.username,
        }

    def _serialized_access(self, obj) -> tuple[list[str], list[str], list[str]]:
        cached = getattr(obj, "_serialized_access_permissions", None)
        if cached is not None:
            return cached

        overrides = getattr(obj, "_access_function_overrides", None)
        if overrides is None:
            overrides = list(obj.function_overrides.select_related("function").all())

        allowed_overrides = [
            override.function.code
            for override in overrides
            if override.effect == UserFunctionOverride.Effect.ALLOW
        ]
        denied_overrides = [
            override.function.code
            for override in overrides
            if override.effect == UserFunctionOverride.Effect.DENY
        ]

        if not obj.is_active:
            effective = set()
        elif obj.is_superuser:
            active_codes = getattr(self, "_active_function_codes", None)
            if active_codes is None:
                active_codes = list(
                    AccessFunction.objects.filter(is_active=True).values_list("code", flat=True)
                )
                self._active_function_codes = active_codes
            effective = set(active_codes)
        else:
            profile = getattr(obj, "employee_profile", None)
            role = profile.role if profile else None
            effective = set()
            if role and role.is_active:
                assignments = getattr(role, "_access_function_assignments", None)
                if assignments is None:
                    assignments = list(role.function_assignments.select_related("function").all())
                effective.update(
                    assignment.function.code
                    for assignment in assignments
                    if assignment.allowed and assignment.function.is_active
                )
            for override in overrides:
                if not override.function.is_active:
                    continue
                if override.effect == UserFunctionOverride.Effect.ALLOW:
                    effective.add(override.function.code)
                else:
                    effective.discard(override.function.code)
            if profile and profile.account_type == EmployeeProfile.AccountType.EXTERNAL_VENDOR:
                effective.difference_update(EXTERNAL_VENDOR_FORBIDDEN_CODES)
                effective = {
                    code for code in effective if not code.startswith("organization.")
                }

        cached = (allowed_overrides, denied_overrides, sorted(effective))
        obj._serialized_access_permissions = cached
        return cached

    def get_allowed_overrides(self, obj) -> list[str]:
        return self._serialized_access(obj)[0]

    def get_denied_overrides(self, obj) -> list[str]:
        return self._serialized_access(obj)[1]

    def get_effective_permissions(self, obj) -> list[str]:
        return self._serialized_access(obj)[2]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context.get("request")
        if not request:
            return attrs
        requested_type = attrs.get(
            "account_type",
            getattr(getattr(self.instance, "employee_profile", None), "account_type", EmployeeProfile.AccountType.EMPLOYEE),
        )
        requested_role = attrs.get(
            "role",
            getattr(getattr(getattr(self.instance, "employee_profile", None), "role", None), "slug", None),
        )
        organization_unit = attrs.get(
            "organization_unit",
            getattr(getattr(self.instance, "employee_profile", None), "organization_unit", None),
        )
        forbidden_allows = sorted(set(attrs.get("allow_codes", [])) & EXTERNAL_VENDOR_FORBIDDEN_CODES)
        if requested_type == EmployeeProfile.AccountType.EXTERNAL_VENDOR and forbidden_allows:
            raise serializers.ValidationError({
                "allow_codes": f"External suppliers cannot receive management functions: {', '.join(forbidden_allows)}"
            })
        if requested_type in {
            EmployeeProfile.AccountType.INTERNAL_VENDOR,
            EmployeeProfile.AccountType.EXTERNAL_VENDOR,
        } and organization_unit:
            raise serializers.ValidationError({"organization_unit": "Supplier accounts are workspace roots and cannot belong to a shift."})
        if organization_unit:
            from vendors.access import organization_workspace_owner_ids
            if organization_unit.workspace_owner_id not in organization_workspace_owner_ids(request.user):
                raise serializers.ValidationError({"organization_unit": "You cannot assign users to this organization workspace."})
            current_unit = organization_unit
            visited_units = set()
            while current_unit and current_unit.pk not in visited_units:
                visited_units.add(current_unit.pk)
                if not current_unit.is_active:
                    raise serializers.ValidationError({
                        "organization_unit": "Users cannot be assigned below an inactive organization unit."
                    })
                current_unit = current_unit.parent
            if requested_role in {"employee", "team-lead"} and organization_unit.unit_type != OrganizationUnit.UnitType.SHIFT:
                raise serializers.ValidationError({"organization_unit": "Employees and Team Leads must be assigned to a Shift."})
        creator_profile = getattr(request.user, "employee_profile", None)
        creator_type = getattr(creator_profile, "account_type", EmployeeProfile.AccountType.EMPLOYEE)
        if creator_type == EmployeeProfile.AccountType.EXTERNAL_VENDOR:
            raise serializers.ValidationError("External suppliers cannot create subordinate accounts.")
        if creator_type == EmployeeProfile.AccountType.INTERNAL_VENDOR:
            if not has_function_access(request.user, "respondents.create"):
                raise serializers.ValidationError("This internal supplier cannot create respondents.")
            if requested_type != EmployeeProfile.AccountType.EMPLOYEE:
                raise serializers.ValidationError({"account_type": "Internal suppliers can only create respondent employees."})
        elif requested_type in {
            EmployeeProfile.AccountType.INTERNAL_VENDOR,
            EmployeeProfile.AccountType.EXTERNAL_VENDOR,
        } and not has_function_access(request.user, "vendors.manage"):
            raise serializers.ValidationError({"account_type": "Supplier accounts require Manage supplier policies access."})
        return attrs

    @staticmethod
    def _forced_role(account_type, requested_role):
        if account_type == EmployeeProfile.AccountType.INTERNAL_VENDOR:
            return "admin"
        if account_type == EmployeeProfile.AccountType.EXTERNAL_VENDOR:
            return "external-vendor"
        return requested_role

    @staticmethod
    def _ensure_vendor_policy(user, account_type, created_by):
        if account_type not in {
            EmployeeProfile.AccountType.INTERNAL_VENDOR,
            EmployeeProfile.AccountType.EXTERNAL_VENDOR,
        }:
            return
        from vendors.models import VendorCommercialProfile

        commercial, _ = VendorCommercialProfile.objects.get_or_create(
            vendor=user,
            defaults={"created_by": created_by},
        )
        changed = []
        if account_type == EmployeeProfile.AccountType.INTERNAL_VENDOR:
            if commercial.default_cpi_cut_percent:
                commercial.default_cpi_cut_percent = 0
                changed.append("default_cpi_cut_percent")
            if commercial.delivery_mode != VendorCommercialProfile.DeliveryMode.PANEL:
                commercial.delivery_mode = VendorCommercialProfile.DeliveryMode.PANEL
                changed.append("delivery_mode")
        if changed:
            changed.append("updated_at")
            commercial.save(update_fields=changed)

    def validate_role(self, slug):
        request = self.context.get("request")
        queryset = assignable_roles(request.user) if request else Role.objects.filter(is_active=True)
        if slug is not None and not queryset.filter(slug=slug).exists():
            raise serializers.ValidationError("Unknown or inactive role.")
        return slug

    def validate_email(self, value):
        queryset = get_user_model().objects.filter(email__iexact=value) | get_user_model().objects.filter(username__iexact=value)
        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value.lower()

    def _validate_codes(self, codes):
        found = set(AccessFunction.objects.filter(code__in=codes).values_list("code", flat=True))
        missing = sorted(set(codes) - found)
        if missing:
            raise serializers.ValidationError(f"Unknown function codes: {', '.join(missing)}")
        request = self.context.get("request")
        if request and not request.user.is_superuser:
            grantable = set(assignable_functions(request.user).values_list("code", flat=True))
            forbidden = sorted(set(codes) - grantable)
            if forbidden:
                raise serializers.ValidationError(f"You cannot delegate functions you do not have: {', '.join(forbidden)}")
        return list(dict.fromkeys(codes))

    def validate_allow_codes(self, codes):
        return self._validate_codes(codes)

    def validate_deny_codes(self, codes):
        return self._validate_codes(codes)

    def _update_access(
        self, user, role_slug, allow_codes, deny_codes, account_type=serializers.empty,
        company_name=serializers.empty, department=serializers.empty, organization_unit=serializers.empty,
    ):
        profile, _ = EmployeeProfile.objects.get_or_create(user=user)
        if role_slug is not serializers.empty:
            profile.role = Role.objects.filter(slug=role_slug).first() if role_slug else None
        if account_type is not serializers.empty:
            profile.account_type = account_type
        if company_name is not serializers.empty:
            profile.company_name = company_name
        if department is not serializers.empty:
            profile.department = department
        if organization_unit is not serializers.empty:
            profile.organization_unit = organization_unit
        profile.save()
        user.employee_profile = profile
        if profile.account_type == EmployeeProfile.AccountType.EXTERNAL_VENDOR:
            user.function_overrides.filter(function__code__in=EXTERNAL_VENDOR_FORBIDDEN_CODES).delete()
        if allow_codes is not serializers.empty or deny_codes is not serializers.empty:
            allow_codes = [] if allow_codes is serializers.empty else allow_codes
            deny_codes = [] if deny_codes is serializers.empty else deny_codes
            selected = set(allow_codes) | set(deny_codes)
            user.function_overrides.exclude(function__code__in=selected).delete()
            effects = [(allow_codes, UserFunctionOverride.Effect.ALLOW), (deny_codes, UserFunctionOverride.Effect.DENY)]
            for codes, effect in effects:
                for function in AccessFunction.objects.filter(code__in=codes):
                    UserFunctionOverride.objects.update_or_create(user=user, function=function, defaults={"effect": effect})
        for attr in ("_access_function_overrides", "_serialized_access_permissions"):
            if hasattr(user, attr):
                delattr(user, attr)

    @transaction.atomic
    def create(self, validated_data):
        requested_role = validated_data.pop("role", "employee")
        allow_codes = validated_data.pop("allow_codes", [])
        deny_codes = validated_data.pop("deny_codes", [])
        password = validated_data.pop("password", None)
        account_type = validated_data.pop("account_type", EmployeeProfile.AccountType.EMPLOYEE)
        role_slug = self._forced_role(account_type, requested_role)
        company_name = validated_data.pop("company_name", "")
        department = validated_data.pop("department", "")
        organization_unit = validated_data.pop("organization_unit", None)
        if account_type == EmployeeProfile.AccountType.EXTERNAL_VENDOR:
            company_name = ""
            department = ""
            organization_unit = None
        if not password:
            raise serializers.ValidationError({"password": "Password is required when creating a user."})
        if not validated_data.get("email"):
            raise serializers.ValidationError({"email": "Email is required."})
        validated_data.setdefault("username", validated_data["email"].lower())
        request = self.context.get("request")
        if request and not assignable_roles(request.user).filter(slug=role_slug).exists():
            raise serializers.ValidationError({"role": "You cannot assign this role."})
        user = get_user_model()(**validated_data)
        user.set_password(password) if password else user.set_unusable_password()
        user.save()
        profile, _ = EmployeeProfile.objects.get_or_create(user=user)
        profile.created_by = request.user if request else None
        profile.save(update_fields=["created_by", "updated_at"])
        self._update_access(
            user, role_slug, allow_codes, deny_codes, account_type, company_name, department, organization_unit
        )
        self._ensure_vendor_policy(user, account_type, request.user if request else None)
        return user

    @transaction.atomic
    def update(self, instance, validated_data):
        requested_role = validated_data.pop("role", serializers.empty)
        allow_codes = validated_data.pop("allow_codes", serializers.empty)
        deny_codes = validated_data.pop("deny_codes", serializers.empty)
        password = validated_data.pop("password", None)
        account_type = validated_data.pop("account_type", serializers.empty)
        company_name = validated_data.pop("company_name", serializers.empty)
        department = validated_data.pop("department", serializers.empty)
        organization_unit = validated_data.pop("organization_unit", serializers.empty)
        final_account_type = (
            account_type
            if account_type is not serializers.empty
            else instance.employee_profile.account_type
        )
        role_slug = self._forced_role(final_account_type, requested_role)
        if final_account_type == EmployeeProfile.AccountType.EXTERNAL_VENDOR:
            company_name = ""
            department = ""
            organization_unit = None
        previous_email = instance.email
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if "email" in validated_data and (instance.username == previous_email or "@" in instance.username):
            instance.username = validated_data["email"]
        if password:
            instance.set_password(password)
        instance.save()
        self._update_access(
            instance, role_slug, allow_codes, deny_codes, account_type, company_name, department, organization_unit
        )
        request = self.context.get("request")
        self._ensure_vendor_policy(instance, final_account_type, request.user if request else None)
        return instance
