from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q


PERCENTAGE_VALIDATORS = [MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))]

PROFILE_REUSE_AGE_GROUPS = (
    "13-17", "18-24", "25-29", "30-34", "35-39", "40-44", "45-49", "50-54",
)


def default_profile_reuse_age_groups():
    return list(PROFILE_REUSE_AGE_GROUPS)


def default_profile_reuse_genders():
    return ["male", "female"]


class Client(models.Model):
    """A survey buyer/source account controlled by the platform owner."""

    code = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=160)
    provider_code = models.SlugField(max_length=80, default="innovatemr", db_index=True)
    company_name_match = models.CharField(
        max_length=160,
        blank=True,
        help_text="Current survey company label used while legacy inventory is mapped to this client.",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_clients",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class ClientIntegration(models.Model):
    """One independently scheduled and authenticated upstream client connection."""

    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="integrations")
    name = models.CharField(max_length=120)
    provider_code = models.SlugField(max_length=80, default="innovatemr", db_index=True)
    base_url = models.URLField(max_length=500)
    credential_env_key = models.CharField(
        max_length=120,
        blank=True,
        help_text="Environment-variable name containing the token. Secret values are never stored here.",
    )
    credential_env_keys = models.JSONField(
        default=dict,
        blank=True,
        help_text="Provider credential names mapped to environment-variable names; never secret values.",
    )
    config = models.JSONField(default=dict, blank=True, help_text="Non-secret provider configuration.")
    profile_reuse_enabled = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Allow this client to receive an eligible previously registered profile UID.",
    )
    profile_reuse_eligible_after_days = models.PositiveSmallIntegerField(
        default=60,
        validators=[MinValueValidator(1), MaxValueValidator(730)],
        help_text="Minimum age of a registered UID before it enters the reuse queue.",
    )
    profile_reuse_monthly_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("30.00"),
        validators=PERCENTAGE_VALIDATORS,
        help_text="Percentage of the previous calendar month's attempts available as this month's reuse budget.",
    )
    profile_reuse_country_codes = models.JSONField(
        default=list,
        blank=True,
        help_text="Allowed ISO country codes. Empty means every country served by this integration.",
    )
    profile_reuse_age_groups = models.JSONField(
        default=default_profile_reuse_age_groups,
        blank=True,
    )
    profile_reuse_genders = models.JSONField(
        default=default_profile_reuse_genders,
        blank=True,
    )
    profile_rereuse_enabled = models.BooleanField(
        default=False,
        help_text="Allow profiles that already completed one reuse round to enter a separate queue.",
    )
    profile_rereuse_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("50.00"),
        validators=PERCENTAGE_VALIDATORS,
        help_text="Share of the monthly reuse target reserved for already-reused profiles.",
    )
    profile_rereuse_cooldown_days = models.PositiveSmallIntegerField(
        default=30,
        validators=[MinValueValidator(1), MaxValueValidator(730)],
        help_text="Minimum cooldown after a profile reuse before it can be used again.",
    )
    profile_reuse_first_delay_minutes = models.PositiveIntegerField(
        default=86400,
        validators=[MinValueValidator(1), MaxValueValidator(1051200)],
        help_text=(
            "Minimum age of a newly registered profile before its first reuse, "
            "stored in minutes so the policy can be expressed in minutes, hours, or days."
        ),
    )
    profile_reuse_min_interval_minutes = models.PositiveIntegerField(
        default=43200,
        validators=[MinValueValidator(1), MaxValueValidator(1051200)],
        help_text="Minimum time between two reuses of the same UID.",
    )
    profile_reuse_max_uses_per_window = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(1000)],
        help_text="Maximum reuses allowed for one UID inside the configured rolling window.",
    )
    profile_reuse_window_minutes = models.PositiveIntegerField(
        default=1440,
        validators=[MinValueValidator(1), MaxValueValidator(1051200)],
        help_text="Rolling window used by the per-UID reuse limit.",
    )
    encrypted_api_token = models.TextField(blank=True, editable=False)
    credential_fingerprint = models.CharField(max_length=64, blank=True, editable=False)
    credential_last_four = models.CharField(max_length=4, blank=True, editable=False)
    credential_changed_at = models.DateTimeField(null=True, blank=True, editable=False)
    supplier_code = models.CharField(max_length=40, default="1000")
    inventory_endpoint = models.CharField(max_length=500, blank=True, help_text="Relative or absolute inventory endpoint. Blank calls Base URL exactly.")
    paged_inventory_endpoint = models.CharField(max_length=500, blank=True)
    quota_endpoint_template = models.CharField(max_length=500, blank=True, help_text="Optional endpoint containing {survey_id}.")
    targeting_endpoint_template = models.CharField(max_length=500, blank=True, help_text="Optional endpoint containing {survey_id}.")
    transaction_endpoint_template = models.CharField(max_length=500, blank=True, help_text="Optional endpoint containing {survey_id} and {pid}.")
    auth_header_name = models.CharField(max_length=120, default="x-access-token")
    auth_header_prefix = models.CharField(max_length=40, blank=True, help_text="For example: Bearer")
    inventory_result_key = models.CharField(max_length=120, default="result")
    quota_result_key = models.CharField(max_length=120, default="result")
    targeting_result_key = models.CharField(max_length=120, default="result")
    transaction_result_key = models.CharField(max_length=120, default="result")
    field_mapping = models.JSONField(default=dict, blank=True, help_text="Optional canonical-field to upstream-field mapping for custom providers.")
    scheduled_sync_enabled = models.BooleanField(default=False)
    sync_interval_seconds = models.PositiveIntegerField(
        default=60,
        validators=[MinValueValidator(60)],
        help_text="Minimum interval between inventory syncs for this integration.",
    )
    detail_refresh_batch = models.PositiveSmallIntegerField(
        default=3,
        validators=[MinValueValidator(0), MaxValueValidator(25)],
        help_text="Survey detail records refreshed after each inventory sync.",
    )
    last_tested_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_test_status = models.CharField(max_length=20, blank=True, editable=False)
    last_test_error = models.TextField(blank=True, editable=False)
    last_sync_started_at = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)
    last_sync_finished_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_sync_status = models.CharField(max_length=20, blank=True, editable=False)
    last_sync_error = models.TextField(blank=True, editable=False)
    last_sync_summary = models.JSONField(default=dict, blank=True, editable=False)
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_client_integrations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["client__name", "name"]
        constraints = [
            models.UniqueConstraint(fields=["client", "name"], name="unique_client_integration_name"),
        ]

    def __str__(self):
        return f"{self.client} · {self.name}"


    def clean(self):
        super().clean()
        minimum_interval = {
            "rfg": 600,
            "toluna": 60,
            "track_opinion": 300,
            "acuity": 300,
            "unimarket": 300,
        }.get(self.provider_code, 60)
        managed_providers = {"rfg", "toluna", "track_opinion", "acuity", "unimarket"}
        if self.provider_code in managed_providers and self.sync_interval_seconds < minimum_interval:
            raise ValidationError({
                "sync_interval_seconds": (
                    "RFG inventory cannot be polled more often than every 600 seconds."
                    if self.provider_code == "rfg"
                    else f"{self.provider_code.replace('_', ' ').title()} inventory cannot be polled more often than every {minimum_interval} seconds."
                )
            })
        if self.provider_code in managed_providers and self.scheduled_sync_enabled and self.last_test_status != "success":
            raise ValidationError({
                "scheduled_sync_enabled": "Test and verify the connection before enabling scheduled sync."
            })


class OrganizationUnit(models.Model):
    """A strict Branch -> Sub-branch -> Shift hierarchy inside one workspace."""

    class UnitType(models.TextChoices):
        BRANCH = "branch", "Branch"
        SUB_BRANCH = "sub_branch", "Sub-branch"
        SHIFT = "shift", "Shift"

    workspace_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="organization_units",
        help_text="The super-admin workspace or internal supplier that owns this hierarchy.",
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
    )
    unit_type = models.CharField(max_length=16, choices=UnitType.choices, db_index=True)
    name = models.CharField(max_length=120)
    code = models.SlugField(max_length=80)
    description = models.CharField(max_length=240, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_organization_units",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["workspace_owner_id", "unit_type", "name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace_owner", "parent", "unit_type", "code"],
                name="unique_organization_unit_code_in_parent",
            ),
        ]
        indexes = [
            models.Index(fields=["workspace_owner", "unit_type", "is_active"]),
            models.Index(fields=["parent", "is_active"]),
        ]

    @property
    def path_label(self):
        labels = [self.name]
        current = self.parent
        visited = {self.pk}
        while current and current.pk not in visited:
            visited.add(current.pk)
            labels.append(current.name)
            current = current.parent
        return " / ".join(reversed(labels))

    def clean(self):
        super().clean()
        if not self.workspace_owner_id:
            raise ValidationError({"workspace_owner": "Select a workspace owner."})
        parent_rules = {
            self.UnitType.BRANCH: None,
            self.UnitType.SUB_BRANCH: self.UnitType.BRANCH,
            self.UnitType.SHIFT: self.UnitType.SUB_BRANCH,
        }
        if self.unit_type not in parent_rules:
            raise ValidationError({"unit_type": "Select a valid organization unit type."})
        profile = getattr(self.workspace_owner, "employee_profile", None)
        if not self.workspace_owner.is_superuser and getattr(profile, "account_type", "") != "internal_vendor":
            raise ValidationError({"workspace_owner": "Organization workspaces must belong to a super admin or internal supplier."})
        expected_parent_type = parent_rules[self.unit_type]
        if expected_parent_type is None and self.parent_id:
            raise ValidationError({"parent": "A branch cannot have a parent."})
        if expected_parent_type is not None:
            if not self.parent_id:
                raise ValidationError({"parent": f"A {self.get_unit_type_display().lower()} requires a parent."})
            if self.pk and self.parent_id == self.pk:
                raise ValidationError({"parent": "An organization unit cannot be its own parent."})
            if self.parent.unit_type != expected_parent_type:
                parent_name = dict(self.UnitType.choices)[expected_parent_type].lower()
                raise ValidationError({"parent": f"Parent must be a {parent_name}."})
            if self.parent.workspace_owner_id != self.workspace_owner_id:
                raise ValidationError({"parent": "Parent and child must belong to the same workspace."})
            visited = {self.pk} if self.pk else set()
            current = self.parent
            while current:
                if current.pk in visited:
                    raise ValidationError({"parent": "This parent would create a hierarchy cycle."})
                visited.add(current.pk)
                current = current.parent
        if self.pk:
            expected_child_type = {
                self.UnitType.BRANCH: self.UnitType.SUB_BRANCH,
                self.UnitType.SUB_BRANCH: self.UnitType.SHIFT,
                self.UnitType.SHIFT: None,
            }[self.unit_type]
            children = OrganizationUnit.objects.filter(parent_id=self.pk)
            invalid_children = children if expected_child_type is None else children.exclude(unit_type=expected_child_type)
            if invalid_children.exists():
                raise ValidationError({
                    "unit_type": "This type change would invalidate existing child units. Move or deactivate the children first."
                })
        duplicate = OrganizationUnit.objects.filter(
            workspace_owner_id=self.workspace_owner_id,
            parent_id=self.parent_id,
            unit_type=self.unit_type,
            code=self.code,
        )
        if self.pk:
            duplicate = duplicate.exclude(pk=self.pk)
        if duplicate.exists():
            raise ValidationError({"code": "This code is already used at the same hierarchy level."})

    def __str__(self):
        return self.path_label


class OrganizationClientAccess(models.Model):
    """Explicit client visibility with nearest-unit override inheritance."""

    organization_unit = models.ForeignKey(
        OrganizationUnit,
        on_delete=models.PROTECT,
        related_name="client_access_rules",
    )
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="organization_access_rules")
    min_cpi = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Optional source-CPI floor inherited by child units.",
    )
    max_cpi = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Optional source-CPI ceiling inherited by child units.",
    )
    inherit_cpi_range = models.BooleanField(
        default=True,
        help_text="When enabled, a child rule keeps the closest parent rule's CPI range.",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_organization_client_access_rules",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["organization_unit__workspace_owner_id", "organization_unit__name", "client__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization_unit", "client"],
                name="unique_client_access_per_organization_unit",
            ),
            models.CheckConstraint(
                condition=Q(min_cpi__isnull=True) | Q(max_cpi__isnull=True) | Q(min_cpi__lte=F("max_cpi")),
                name="organization_client_cpi_range_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["organization_unit", "is_active"]),
            models.Index(fields=["client", "is_active"]),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if not self.organization_unit_id:
            errors["organization_unit"] = "Select an organization unit."
        if not self.client_id:
            errors["client"] = "Select a client."
        if errors:
            raise ValidationError(errors)
        if self.min_cpi is not None and self.max_cpi is not None and self.min_cpi > self.max_cpi:
            raise ValidationError({"max_cpi": "Maximum CPI must be greater than or equal to minimum CPI."})
        owner = self.organization_unit.workspace_owner
        profile = getattr(owner, "employee_profile", None)
        if getattr(profile, "account_type", "") == "internal_vendor":
            if not VendorClientAllocation.objects.filter(vendor=owner, client=self.client, is_active=True).exists():
                raise ValidationError({"client": "Allocate this client to the internal supplier before assigning it to a branch."})

    def __str__(self):
        return f"{self.organization_unit.path_label} · {self.client.name}"


class VendorCommercialProfile(models.Model):
    """Commercial defaults for a user marked as an internal or external supplier."""

    class DeliveryMode(models.TextChoices):
        PANEL = "panel", "Panel only"
        API = "api", "API only"
        BOTH = "both", "Panel and API"

    vendor = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="vendor_commercial_profile",
    )
    default_cpi_cut_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=PERCENTAGE_VALIDATORS,
    )
    currency = models.CharField(max_length=3, default="USD")
    delivery_mode = models.CharField(
        max_length=8,
        choices=DeliveryMode.choices,
        default=DeliveryMode.PANEL,
        db_index=True,
        help_text="Controls whether an external supplier can sign in to the panel, use API keys, or both.",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_vendor_commercial_profiles",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["vendor__first_name", "vendor__username"]

    @property
    def panel_access_enabled(self):
        return self.delivery_mode in {self.DeliveryMode.PANEL, self.DeliveryMode.BOTH}

    @property
    def api_access_enabled(self):
        return self.delivery_mode in {self.DeliveryMode.API, self.DeliveryMode.BOTH}

    def clean(self):
        super().clean()
        employee_profile = getattr(self.vendor, "employee_profile", None)
        account_type = getattr(employee_profile, "account_type", "")
        if account_type not in {"internal_vendor", "external_vendor"}:
            raise ValidationError({"vendor": "Commercial profiles can only be assigned to supplier accounts."})
        if account_type == "internal_vendor" and self.default_cpi_cut_percent != Decimal("0.00"):
            raise ValidationError({"default_cpi_cut_percent": "Internal suppliers must receive the full source CPI."})
        if account_type == "internal_vendor" and self.delivery_mode != self.DeliveryMode.PANEL:
            raise ValidationError({"delivery_mode": "Internal suppliers use the panel delivery mode."})

    def __str__(self):
        return self.vendor.get_full_name() or self.vendor.username


class VendorAPIKey(models.Model):
    """Revocable, hashed API credential for one external supplier account."""

    class SurveyIdMode(models.TextChoices):
        PROJECT_ID = "project_id", "Project ID (recommended)"
        PROVIDER_SURVEY_ID = "provider_survey_id", "Provider survey ID"

    vendor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="vendor_api_keys",
    )
    client_allocations = models.ManyToManyField(
        "VendorClientAllocation",
        blank=True,
        related_name="api_keys",
        help_text="Client grants this key may expose. Per-project access exclusions still follow the live allocation rules.",
    )
    name = models.CharField(max_length=120)
    prefix = models.CharField(max_length=16, db_index=True)
    last_four = models.CharField(max_length=4)
    key_hash = models.CharField(max_length=64, unique=True, editable=False)
    survey_id_mode = models.CharField(
        max_length=24,
        choices=SurveyIdMode.choices,
        default=SurveyIdMode.PROJECT_ID,
        help_text="Controls which identifier is exposed as survey_id in this key's inventory.",
    )
    complete_callback_url = models.URLField(max_length=1000, blank=True)
    terminate_callback_url = models.URLField(max_length=1000, blank=True)
    quota_callback_url = models.URLField(max_length=1000, blank=True)
    quality_callback_url = models.URLField(max_length=1000, blank=True)
    callback_signing_enabled = models.BooleanField(
        default=False,
        help_text="Append an HMAC-SHA256 hash to callbacks sent to this supplier.",
    )
    encrypted_callback_secret = models.TextField(blank=True, editable=False)
    callback_secret_last_four = models.CharField(max_length=4, blank=True, editable=False)
    is_active = models.BooleanField(default=True, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_vendor_api_keys",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["vendor", "name"], name="unique_vendor_api_key_name"),
        ]
        indexes = [models.Index(fields=["vendor", "is_active"])]

    @property
    def masked_key(self):
        return f"{self.prefix}••••{self.last_four}"

    @property
    def callback_configured(self):
        return any((
            self.complete_callback_url,
            self.terminate_callback_url,
            self.quota_callback_url,
            self.quality_callback_url,
        ))

    def callback_url_for_status(self, status_code: str) -> str:
        status_code = str(status_code)
        return {
            "1": self.complete_callback_url,
            "2": self.terminate_callback_url,
            "3": self.quota_callback_url,
            "4": self.quality_callback_url,
        }.get(status_code, self.terminate_callback_url)

    def clean(self):
        super().clean()
        account_type = getattr(getattr(self.vendor, "employee_profile", None), "account_type", "")
        if account_type != "external_vendor":
            raise ValidationError({"vendor": "API keys can only be issued to external suppliers."})

    def __str__(self):
        return f"{self.vendor} · {self.name} · {self.masked_key}"


class VendorClientAllocation(models.Model):
    """Client eligibility and client-level CPI policy.

    Legacy quantity columns remain only for historical compatibility and are
    deliberately excluded from current supplier delivery decisions.
    """

    vendor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="client_allocations",
    )
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="vendor_allocations")
    quantity_limit = models.PositiveBigIntegerField(default=0)
    reserved_quantity = models.PositiveBigIntegerField(default=0, editable=False)
    consumed_quantity = models.PositiveBigIntegerField(default=0, editable=False)
    cpi_cut_override_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=PERCENTAGE_VALIDATORS,
        help_text="Optional client-specific cut. Blank uses the supplier commercial default.",
    )
    min_cpi = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Optional source-CPI floor for this supplier/client grant.",
    )
    max_cpi = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Optional source-CPI ceiling for this supplier/client grant.",
    )
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_vendor_client_allocations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["client__name", "vendor__first_name", "vendor__username"]
        constraints = [
            models.UniqueConstraint(fields=["vendor", "client"], name="unique_vendor_client_allocation"),
            models.CheckConstraint(
                condition=Q(consumed_quantity__lte=F("quantity_limit")),
                name="client_consumed_not_above_limit",
            ),
            models.CheckConstraint(
                condition=Q(reserved_quantity__lte=F("quantity_limit") - F("consumed_quantity")),
                name="client_reserved_within_remaining",
            ),
            models.CheckConstraint(
                condition=Q(min_cpi__isnull=True) | Q(max_cpi__isnull=True) | Q(min_cpi__lte=F("max_cpi")),
                name="vendor_client_cpi_range_valid",
            ),
        ]
        indexes = [models.Index(fields=["vendor", "is_active"]), models.Index(fields=["client", "is_active"])]

    @property
    def remaining_quantity(self):
        return max(0, self.quantity_limit - self.consumed_quantity - self.reserved_quantity)

    @property
    def effective_cpi_cut_percent(self):
        if self.cpi_cut_override_percent is not None:
            return self.cpi_cut_override_percent
        commercial = getattr(self.vendor, "vendor_commercial_profile", None)
        return commercial.default_cpi_cut_percent if commercial else Decimal("0.00")

    def clean(self):
        super().clean()
        employee_profile = getattr(self.vendor, "employee_profile", None)
        account_type = getattr(employee_profile, "account_type", "")
        if account_type not in {"internal_vendor", "external_vendor"}:
            raise ValidationError({"vendor": "Client allocations can only be assigned to supplier accounts."})
        if account_type == "internal_vendor" and self.cpi_cut_override_percent not in {None, Decimal("0.00")}:
            raise ValidationError({"cpi_cut_override_percent": "Internal suppliers cannot have a CPI cut."})
        if self.min_cpi is not None and self.max_cpi is not None and self.min_cpi > self.max_cpi:
            raise ValidationError({"max_cpi": "Maximum CPI must be greater than or equal to minimum CPI."})
        if self.ends_at and self.starts_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": "End time must be after start time."})
        if self.consumed_quantity + self.reserved_quantity > self.quantity_limit:
            raise ValidationError({"quantity_limit": "Limit cannot be below consumed plus reserved quantity."})

    def __str__(self):
        return f"{self.vendor} · {self.client}"


class VendorSurveyAllocation(models.Model):
    """Explicit project visibility and complete cap under a client allocation."""

    client_allocation = models.ForeignKey(
        VendorClientAllocation,
        on_delete=models.PROTECT,
        related_name="survey_allocations",
    )
    survey = models.ForeignKey("surveys.Survey", on_delete=models.PROTECT, related_name="vendor_allocations")
    quantity_limit = models.PositiveBigIntegerField(default=0)
    reserved_quantity = models.PositiveBigIntegerField(default=0, editable=False)
    consumed_quantity = models.PositiveBigIntegerField(default=0, editable=False)
    cpi_cut_override_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=PERCENTAGE_VALIDATORS,
        help_text="Optional survey-specific cut. Blank uses the client/supplier policy.",
    )
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_vendor_survey_allocations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["survey__source_id", "client_allocation__vendor__username"]
        constraints = [
            models.UniqueConstraint(
                fields=["client_allocation", "survey"],
                name="unique_vendor_survey_allocation",
            ),
            models.CheckConstraint(
                condition=Q(consumed_quantity__lte=F("quantity_limit")),
                name="survey_consumed_not_above_limit",
            ),
            models.CheckConstraint(
                condition=Q(reserved_quantity__lte=F("quantity_limit") - F("consumed_quantity")),
                name="survey_reserved_within_remaining",
            ),
        ]
        indexes = [
            models.Index(fields=["survey", "is_active"]),
            models.Index(fields=["client_allocation", "is_active"]),
        ]

    @property
    def vendor(self):
        return self.client_allocation.vendor

    @property
    def client(self):
        return self.client_allocation.client

    @property
    def remaining_quantity(self):
        return max(0, self.quantity_limit - self.consumed_quantity - self.reserved_quantity)

    @property
    def effective_cpi_cut_percent(self):
        if self.cpi_cut_override_percent is not None:
            return self.cpi_cut_override_percent
        return self.client_allocation.effective_cpi_cut_percent

    def clean(self):
        super().clean()
        if self.survey.client_id and self.survey.client_id != self.client_allocation.client_id:
            raise ValidationError({"survey": "Survey client must match the parent client allocation."})
        if not self.survey.client_id:
            raise ValidationError({"survey": "Survey must be mapped to a client before it can be allocated."})
        account_type = getattr(getattr(self.vendor, "employee_profile", None), "account_type", "")
        if account_type == "internal_vendor" and self.cpi_cut_override_percent not in {None, Decimal("0.00")}:
            raise ValidationError({"cpi_cut_override_percent": "Internal suppliers cannot have a CPI cut."})
        if self.ends_at and self.starts_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": "End time must be after start time."})
        if self.consumed_quantity + self.reserved_quantity > self.quantity_limit:
            raise ValidationError({"quantity_limit": "Limit cannot be below consumed plus reserved quantity."})

    def __str__(self):
        return f"{self.vendor} · {self.survey}"


class AllocationReservation(models.Model):
    """Auditable one-unit reservation lifecycle for a survey attempt."""

    class Status(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        CONSUMED = "consumed", "Consumed"
        RELEASED = "released", "Released"
        EXPIRED = "expired", "Expired"

    attempt = models.OneToOneField(
        "surveys.SurveyAttempt",
        on_delete=models.PROTECT,
        related_name="allocation_reservation",
    )
    client_allocation = models.ForeignKey(
        VendorClientAllocation,
        on_delete=models.PROTECT,
        related_name="reservations",
    )
    survey_allocation = models.ForeignKey(
        VendorSurveyAllocation,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reservations",
    )
    quantity = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RESERVED, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    finalized_at = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "expires_at"])]

    def clean(self):
        super().clean()
        if self.survey_allocation and self.survey_allocation.client_allocation_id != self.client_allocation_id:
            raise ValidationError({"survey_allocation": "Survey and client allocations must belong to the same supplier scope."})
        if self.survey_allocation and self.attempt.survey_id != self.survey_allocation.survey_id:
            raise ValidationError({"attempt": "Attempt survey must match the survey allocation."})

    def __str__(self):
        return f"{self.attempt.rid} · {self.status}"
