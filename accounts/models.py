from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class AccessFunction(models.Model):
    code = models.CharField(max_length=120, unique=True)
    name = models.CharField(max_length=120)
    module = models.CharField(max_length=80, db_index=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["module", "name"]

    def __str__(self):
        return f"{self.module}: {self.name}"


class Role(models.Model):
    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=80, unique=True)
    description = models.TextField(blank=True)
    rank = models.PositiveSmallIntegerField(default=10)
    cpi_visibility_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=100,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Percentage of CPI visible to employee accounts assigned this role.",
    )
    is_system = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_roles"
    )
    functions = models.ManyToManyField(AccessFunction, through="RoleFunctionPermission", related_name="roles")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["rank", "name"]

    def __str__(self):
        return self.name


class RoleFunctionPermission(models.Model):
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="function_assignments")
    function = models.ForeignKey(AccessFunction, on_delete=models.CASCADE, related_name="role_assignments")
    allowed = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["role", "function"], name="unique_role_function")]
        ordering = ["function__module", "function__name"]

    def __str__(self):
        return f"{self.role} / {self.function.code}: {'allow' if self.allowed else 'deny'}"


class EmployeeProfile(models.Model):
    class AccountType(models.TextChoices):
        EMPLOYEE = "employee", "Employee"
        INTERNAL_VENDOR = "internal_vendor", "Internal vendor"
        EXTERNAL_VENDOR = "external_vendor", "External vendor"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="employee_profile")
    role = models.ForeignKey(Role, null=True, blank=True, on_delete=models.SET_NULL, related_name="employees")
    employee_id = models.CharField(max_length=60, unique=True, null=True, blank=True)
    department = models.CharField(max_length=120, blank=True)
    job_title = models.CharField(max_length=120, blank=True)
    account_type = models.CharField(max_length=20, choices=AccountType.choices, default=AccountType.EMPLOYEE, db_index=True)
    company_name = models.CharField(max_length=160, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_employee_profiles"
    )
    organization_unit = models.ForeignKey(
        "vendors.OrganizationUnit",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="members",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.user.get_full_name() or self.user.username


class UserFunctionOverride(models.Model):
    class Effect(models.TextChoices):
        ALLOW = "allow", "Allow"
        DENY = "deny", "Deny"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="function_overrides")
    function = models.ForeignKey(AccessFunction, on_delete=models.CASCADE, related_name="user_overrides")
    effect = models.CharField(max_length=8, choices=Effect.choices)
    reason = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "function"], name="unique_user_function_override")]
        ordering = ["function__module", "function__name"]

    def __str__(self):
        return f"{self.user} / {self.function.code}: {self.effect}"
