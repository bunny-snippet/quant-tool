from django.contrib.auth import get_user_model, login
from django.contrib.auth.views import LoginView, LogoutView
from django.db import transaction
from django.db.models import Count, Prefetch
from django.http import Http404
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import filters, viewsets

from .access import (
    EXTERNAL_VENDOR_FORBIDDEN_CODES, HasFunctionPermission, any_function_permission_required, assignable_functions, assignable_roles,
    can_manage_role, has_function_access, manageable_user_ids,
)
from .forms import FirstAdminSetupForm, WorkspaceAuthenticationForm
from .models import AccessFunction, EmployeeProfile, Role, RoleFunctionPermission, UserFunctionOverride
from .serializers import AccessFunctionSerializer, RoleSerializer, UserAccessSerializer
from vendors.access import organization_workspace_owner_ids
from vendors.models import OrganizationUnit


class WorkspaceLoginView(LoginView):
    template_name = "accounts/login.html"
    authentication_form = WorkspaceAuthenticationForm
    redirect_authenticated_user = True

    def form_valid(self, form):
        response = super().form_valid(form)
        if not form.cleaned_data.get("remember_me"):
            self.request.session.set_expiry(0)
        return response


class WorkspaceLogoutView(LogoutView):
    next_page = reverse_lazy("login")
    http_method_names = ["post", "options"]


def first_admin_setup(request):
    if get_user_model().objects.exists():
        raise Http404
    form = FirstAdminSetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = get_user_model().objects.create_superuser(
                username=form.cleaned_data["username"],
                email=form.cleaned_data["email"],
                password=form.cleaned_data["password1"],
                first_name=form.cleaned_data["first_name"],
                last_name=form.cleaned_data["last_name"],
            )
            profile, _ = EmployeeProfile.objects.get_or_create(user=user)
            profile.role = Role.objects.filter(slug="super-admin").first()
            profile.save(update_fields=["role", "updated_at"])
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        return redirect("home")
    return render(request, "accounts/setup.html", {"form": form})


@any_function_permission_required(
    "access.manage", "users.view", "roles.view", "users.create", "roles.create", "respondents.create"
)
def access_control_page(request):
    roles = assignable_roles(request.user).annotate(employee_count=Count("employees")).prefetch_related("function_assignments__function")
    functions = assignable_functions(request.user)
    user_ids = manageable_user_ids(request.user)
    if request.user.is_superuser:
        users = get_user_model().objects.all()
    else:
        users = get_user_model().objects.filter(id__in=user_ids)
    users = users.select_related(
        "employee_profile__role", "employee_profile__created_by",
        "employee_profile__organization_unit__workspace_owner",
        "employee_profile__organization_unit__parent__parent",
    ).prefetch_related("function_overrides__function")
    organization_units = OrganizationUnit.objects.filter(
        workspace_owner_id__in=organization_workspace_owner_ids(request.user),
        is_active=True,
    ).select_related("workspace_owner", "parent__parent").order_by("workspace_owner__username", "unit_type", "name")
    requester_type = getattr(getattr(request.user, "employee_profile", None), "account_type", "")
    return render(request, "accounts/access_control_v2.html", {
        "active_page": "access-control", "roles": roles, "functions": functions, "employees": users,
        "can_create_users": any(
            has_function_access(request.user, code) for code in ("users.create", "respondents.create")
        ),
        "can_create_vendor_accounts": has_function_access(request.user, "vendors.manage"),
        "is_internal_vendor": requester_type == EmployeeProfile.AccountType.INTERNAL_VENDOR,
        "external_vendor_forbidden_codes": sorted(EXTERNAL_VENDOR_FORBIDDEN_CODES),
        "create_user_label": (
            "Add respondent"
            if requester_type == EmployeeProfile.AccountType.INTERNAL_VENDOR
            else "Add user"
        ),
        "can_create_roles": has_function_access(request.user, "roles.create"),
        "organization_units": organization_units,
    })


@extend_schema_view(
    list=extend_schema(tags=["Access control"], summary="List configurable application functions"),
    create=extend_schema(tags=["Access control"], summary="Create an application function"),
    retrieve=extend_schema(tags=["Access control"], summary="Get an application function"),
    update=extend_schema(tags=["Access control"], summary="Replace an application function"),
    partial_update=extend_schema(tags=["Access control"], summary="Update an application function"),
    destroy=extend_schema(tags=["Access control"], summary="Delete an application function"),
)
class AccessFunctionViewSet(viewsets.ModelViewSet):
    queryset = AccessFunction.objects.all()
    serializer_class = AccessFunctionSerializer
    permission_classes = [HasFunctionPermission]
    def get_required_function_permission(self):
        return "permissions.view" if self.action in {"list", "retrieve"} else "access.manage"

    def get_queryset(self):
        return AccessFunction.objects.all() if self.request.user.is_superuser else assignable_functions(self.request.user)
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["code", "name", "module", "description"]
    ordering_fields = ["module", "name", "created_at"]
    ordering = ["module", "name"]


@extend_schema_view(
    list=extend_schema(tags=["Access control"], summary="List roles and assigned functions"),
    create=extend_schema(tags=["Access control"], summary="Create a role with function permissions"),
    retrieve=extend_schema(tags=["Access control"], summary="Get a role"),
    update=extend_schema(tags=["Access control"], summary="Replace a role and its function permissions"),
    partial_update=extend_schema(tags=["Access control"], summary="Update a role or its function permissions"),
    destroy=extend_schema(tags=["Access control"], summary="Delete a role"),
)
class RoleViewSet(viewsets.ModelViewSet):
    queryset = Role.objects.annotate(employee_count=Count("employees")).prefetch_related("function_assignments__function")
    serializer_class = RoleSerializer
    permission_classes = [HasFunctionPermission]
    def get_required_function_permission(self):
        return {
            "list": "roles.view", "retrieve": "roles.view", "create": "roles.create",
            "update": "roles.update", "partial_update": "roles.update", "destroy": "roles.delete",
        }.get(self.action, "roles.view")

    def get_queryset(self):
        return assignable_roles(self.request.user).annotate(employee_count=Count("employees")).prefetch_related("function_assignments__function")

    def perform_update(self, serializer):
        if not can_manage_role(self.request.user, self.get_object()):
            raise PermissionDenied("You can only edit roles that you created.")
        serializer.save()

    def perform_destroy(self, instance):
        if not can_manage_role(self.request.user, instance):
            raise PermissionDenied("You can only delete roles that you created.")
        if instance.is_system:
            raise PermissionDenied("Default system roles cannot be deleted.")
        if instance.employees.exists():
            raise PermissionDenied("Move users to another role before deleting this role.")
        instance.delete()
    lookup_field = "slug"
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "slug", "description"]
    ordering_fields = ["rank", "name", "created_at"]
    ordering = ["rank", "name"]


@extend_schema_view(
    list=extend_schema(tags=["Access control"], summary="List users, roles, overrides and effective permissions"),
    create=extend_schema(tags=["Access control"], summary="Create an employee account"),
    retrieve=extend_schema(tags=["Access control"], summary="Get one user's effective access"),
    update=extend_schema(tags=["Access control"], summary="Replace a user and access overrides"),
    partial_update=extend_schema(tags=["Access control"], summary="Update role or per-user allow/deny overrides"),
    destroy=extend_schema(tags=["Access control"], summary="Delete an employee account"),
)
class UserAccessViewSet(viewsets.ModelViewSet):
    queryset = get_user_model().objects.select_related("employee_profile__role").prefetch_related(
        Prefetch(
            "employee_profile__role__function_assignments",
            queryset=RoleFunctionPermission.objects.select_related("function").order_by(
                "function__module", "function__name"
            ),
            to_attr="_access_function_assignments",
        ),
        Prefetch(
            "function_overrides",
            queryset=UserFunctionOverride.objects.select_related("function").order_by(
                "function__module", "function__name"
            ),
            to_attr="_access_function_overrides",
        ),
    )
    serializer_class = UserAccessSerializer
    permission_classes = [HasFunctionPermission]
    def get_required_function_permission(self):
        if self.action == "create":
            profile = getattr(self.request.user, "employee_profile", None)
            if getattr(profile, "account_type", "") in {
                EmployeeProfile.AccountType.INTERNAL_VENDOR,
                EmployeeProfile.AccountType.EXTERNAL_VENDOR,
            }:
                return "respondents.create"
        return {
            "list": "users.view", "retrieve": "users.view", "create": "users.create",
            "update": "users.update", "partial_update": "users.update", "destroy": "users.delete",
        }.get(self.action, "users.view")

    def get_queryset(self):
        queryset = get_user_model().objects.select_related(
            "employee_profile__role", "employee_profile__created_by",
            "employee_profile__organization_unit__workspace_owner",
            "employee_profile__organization_unit__parent__parent",
        ).prefetch_related(
            Prefetch(
                "employee_profile__role__function_assignments",
                queryset=RoleFunctionPermission.objects.select_related("function").order_by(
                    "function__module", "function__name"
                ),
                to_attr="_access_function_assignments",
            ),
            Prefetch(
                "function_overrides",
                queryset=UserFunctionOverride.objects.select_related("function").order_by(
                    "function__module", "function__name"
                ),
                to_attr="_access_function_overrides",
            ),
        )
        if self.request.user.is_superuser:
            return queryset
        return queryset.filter(id__in=manageable_user_ids(self.request.user), is_superuser=False)

    def perform_destroy(self, instance):
        if instance == self.request.user or instance.is_superuser:
            raise PermissionDenied("This account cannot be deleted here.")
        instance.delete()
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["username", "first_name", "last_name", "email", "employee_profile__employee_id"]
    ordering_fields = ["username", "first_name", "date_joined", "last_login"]
    ordering = ["first_name", "username"]
