from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

from accounts.access import HasFunctionPermission


class ProtectedSchemaView(SpectacularAPIView):
    permission_classes = [HasFunctionPermission]
    required_function_permission = "api_docs.view"


class ProtectedSwaggerView(SpectacularSwaggerView):
    permission_classes = [HasFunctionPermission]
    required_function_permission = "api_docs.view"


class ProtectedRedocView(SpectacularRedocView):
    permission_classes = [HasFunctionPermission]
    required_function_permission = "api_docs.view"


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/schema/", ProtectedSchemaView.as_view(), name="api-schema"),
    path("api/docs/", ProtectedSwaggerView.as_view(url_name="api-schema"), name="swagger-ui"),
    path("api/redoc/", ProtectedRedocView.as_view(url_name="api-schema"), name="redoc"),
    path("api/v1/access/", include("accounts.api_urls")),
    path("api/v1/vendors/", include("vendors.urls")),
    path("", include("vendors.web_urls")),
    path("", include("accounts.urls")),
    path("", include("surveys.urls")),
]
