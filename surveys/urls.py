from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .webhook_views import (
    TolunaEnhancedTerminationNotificationAPIView,
    TolunaMemberCompleteNotificationAPIView,
    TolunaMemberTerminateNotificationAPIView,
    TolunaQuotaStatusNotificationAPIView,
    TolunaReconciliationNotificationAPIView,
    TolunaSurveyClosedNotificationAPIView,
)

from .views import (
    DashboardAPIView,
    SurveyAttemptViewSet,
    UserHitsAPIView,
    SyncRunViewSet,
    SyncTriggerView,
    SurveyViewSet,
    dashboard_page,
    projects_page,
    studies_page,
    prescreener_data_page,
    prescreener_data_export,
    termination_reasons_page,
    termination_reasons_export,
    user_hits_page,
    survey_start,
    toluna_member_ready,
    RFGCallbackAPIView,
    rfg_result,
    survey_status,
    workspace_home,
)

router = DefaultRouter()
router.register("surveys", SurveyViewSet, basename="survey")
router.register("sync-runs", SyncRunViewSet, basename="sync-run")
router.register("survey-attempts", SurveyAttemptViewSet, basename="survey-attempt")

urlpatterns = [
    path(
        "api/toluna/notifications/member-complete",
        TolunaMemberCompleteNotificationAPIView.as_view(),
        name="toluna-notification-member-complete",
    ),
    path(
        "api/toluna/notifications/member-terminate",
        TolunaMemberTerminateNotificationAPIView.as_view(),
        name="toluna-notification-member-terminate",
    ),
    path(
        "api/toluna/notifications/survey-closed",
        TolunaSurveyClosedNotificationAPIView.as_view(),
        name="toluna-notification-survey-closed",
    ),
    path(
        "api/toluna/notifications/quota-status",
        TolunaQuotaStatusNotificationAPIView.as_view(),
        name="toluna-notification-quota-status",
    ),
    path(
        "api/toluna/notifications/enhanced-termination",
        TolunaEnhancedTerminationNotificationAPIView.as_view(),
        name="toluna-notification-enhanced-termination",
    ),
    path(
        "api/toluna/notifications/reconciliation",
        TolunaReconciliationNotificationAPIView.as_view(),
        name="toluna-notification-reconciliation",
    ),
    path("survey/start", survey_start, name="survey-start"),
    path("survey/toluna/member-ready", toluna_member_ready, name="toluna-member-ready"),
    path("survey/rfg/callback", RFGCallbackAPIView.as_view(), name="rfg-callback"),
    path("survey/rfg/result", rfg_result, name="rfg-result"),
    path("survey", survey_status, name="survey-status"),
    path("", workspace_home, name="home"),
    path("dashboard/", dashboard_page, name="dashboard"),
    path("projects/", projects_page, name="projects"),
    path("studies/", studies_page, name="studies"),
    path("traffic-reports/", studies_page, name="traffic-reports"),
    path("prescreened-data/", prescreener_data_page, name="prescreened-data"),
    path("prescreened-data/export/", prescreener_data_export, name="prescreened-data-export"),
    path("termination-reasons/", termination_reasons_page, name="termination-reasons"),
    path("termination-reasons/export/", termination_reasons_export, name="termination-reasons-export"),
    path("user-hits/", user_hits_page, name="user-hits"),
    path("api/v1/dashboard/", DashboardAPIView.as_view(), name="dashboard-api"),
    path("api/v1/user-hits/", UserHitsAPIView.as_view(), name="user-hits-api"),
    path("api/v1/sync/", SyncTriggerView.as_view(), name="sync-trigger"),
    path("api/v1/", include(router.urls)),
]
