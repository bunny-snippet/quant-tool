from django.urls import path

from .views import WorkspaceLoginView, WorkspaceLogoutView, access_control_page, first_admin_setup

urlpatterns = [
    path("login/", WorkspaceLoginView.as_view(), name="login"),
    path("logout/", WorkspaceLogoutView.as_view(), name="logout"),
    path("setup/", first_admin_setup, name="first-admin-setup"),
    path("access-control/", access_control_page, name="access-control"),
]

