from rest_framework.routers import DefaultRouter

from .views import AccessFunctionViewSet, RoleViewSet, UserAccessViewSet

router = DefaultRouter()
router.register("functions", AccessFunctionViewSet, basename="access-function")
router.register("roles", RoleViewSet, basename="access-role")
router.register("users", UserAccessViewSet, basename="access-user")

urlpatterns = router.urls

