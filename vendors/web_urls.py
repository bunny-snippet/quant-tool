from django.urls import path

from .views import client_integrations_page, organization_management_page, vendor_management_page


urlpatterns = [
    path("vendors/", vendor_management_page, name="vendor-management"),
    path("client-integrations/", client_integrations_page, name="client-integrations"),
    path("organization/", organization_management_page, name="organization-management"),
]
