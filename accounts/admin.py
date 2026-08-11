from django.contrib import admin

from .models import AccessFunction, EmployeeProfile, Role, RoleFunctionPermission, UserFunctionOverride


class RoleFunctionPermissionInline(admin.TabularInline):
    model = RoleFunctionPermission
    extra = 0


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "rank", "is_active", "is_system"]
    list_filter = ["is_active", "is_system"]
    search_fields = ["name", "slug", "description"]
    prepopulated_fields = {"slug": ["name"]}
    inlines = [RoleFunctionPermissionInline]


@admin.register(AccessFunction)
class AccessFunctionAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "module", "is_active"]
    list_filter = ["module", "is_active"]
    search_fields = ["code", "name", "description"]


@admin.register(EmployeeProfile)
class EmployeeProfileAdmin(admin.ModelAdmin):
    list_display = ["user", "employee_id", "role", "organization_unit", "department", "job_title"]
    list_filter = ["role", "organization_unit__unit_type", "department"]
    search_fields = ["user__username", "user__first_name", "user__last_name", "employee_id"]


@admin.register(UserFunctionOverride)
class UserFunctionOverrideAdmin(admin.ModelAdmin):
    list_display = ["user", "function", "effect", "reason"]
    list_filter = ["effect", "function__module"]
    search_fields = ["user__username", "function__code", "reason"]
