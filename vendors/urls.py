from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AllocationReservationViewSet,
    ClientIntegrationViewSet,
    ClientViewSet,
    VendorClientAllocationViewSet,
    VendorCommercialProfileViewSet,
    VendorSurveyAllocationViewSet,
    VendorDirectoryViewSet,
    VendorManagementOptionsView,
    VendorAPIKeyViewSet,
    OrganizationClientAccessViewSet,
    OrganizationManagementOptionsView,
    OrganizationUnitViewSet,
)
from .upstream_views import UpstreamExplorerViewSet


router = DefaultRouter()
router.register("directory", VendorDirectoryViewSet, basename="vendor-directory")
router.register("clients", ClientViewSet, basename="vendor-client")
router.register("integrations", ClientIntegrationViewSet, basename="client-integration")
router.register("commercial-profiles", VendorCommercialProfileViewSet, basename="vendor-commercial-profile")
router.register("api-keys", VendorAPIKeyViewSet, basename="vendor-api-key")
router.register("client-allocations", VendorClientAllocationViewSet, basename="vendor-client-allocation")
router.register("survey-allocations", VendorSurveyAllocationViewSet, basename="vendor-survey-allocation")
router.register("reservations", AllocationReservationViewSet, basename="allocation-reservation")
router.register("organization-units", OrganizationUnitViewSet, basename="organization-unit")
router.register("organization-client-access", OrganizationClientAccessViewSet, basename="organization-client-access")
router.register("upstream-explorer", UpstreamExplorerViewSet, basename="upstream-explorer")

urlpatterns = router.urls
urlpatterns += [path("management-options/", VendorManagementOptionsView.as_view(), name="vendor-management-options")]
urlpatterns += [path("organization-options/", OrganizationManagementOptionsView.as_view(), name="organization-options")]
