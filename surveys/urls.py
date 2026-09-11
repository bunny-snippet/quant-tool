from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .partner_dashboard import partner_dashboard

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
    UserDashboardAPIView,
    UserHitsAPIView,
    SyncRunViewSet,
    SyncTriggerView,
    SurveyViewSet,
    dashboard_page,
    export_job_create,
    export_job_download,
    export_job_status,
    final_ids_import,
    projects_page,
    reports_page,
    reconciliation_page,
    studies_page,
    prescreener_data_page,
    prescreener_data_export,
    termination_reasons_page,
    termination_reasons_export,
    toluna_notifications_page,
    toluna_notifications_export,
    user_hits_page,
    user_dashboard_page,
    user_dashboard_export,
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
    path("dashboard/client/", partner_dashboard, {"section": "client"}, name="client-dashboard"),
    path("dashboard/supplier/", partner_dashboard, {"section": "supplier"}, name="supplier-dashboard"),
    path("projects/", projects_page, name="projects"),
    path("studies/", studies_page, name="studies"),
    path("traffic-reports/", studies_page, name="traffic-reports"),
    path("reports/", reports_page, name="reports"),
    path("reports/traffic/", studies_page, name="reports-traffic"),
    path("reports/term/", termination_reasons_page, name="reports-term"),
    path("reports/reconciliation/", reconciliation_page, name="reports-reconciliation"),
    path("api/v1/export-jobs/<str:kind>/", export_job_create, name="export-job-create"),
    path("api/v1/export-jobs/status/<uuid:public_id>/", export_job_status, name="export-job-status"),
    path("api/v1/export-jobs/download/<uuid:public_id>/", export_job_download, name="export-job-download"),
    path("api/v1/final-ids/import/", final_ids_import, name="final-ids-import"),
    path("prescreened-data/", prescreener_data_page, name="prescreened-data"),
    path("prescreened-data/export/", prescreener_data_export, name="prescreened-data-export"),
    path("termination-reasons/", termination_reasons_page, name="termination-reasons"),
    path("termination-reasons/export/", termination_reasons_export, name="termination-reasons-export"),
    path("toluna-notifications/", toluna_notifications_page, name="toluna-notifications"),
    path("toluna-notifications/export/", toluna_notifications_export, name="toluna-notifications-export"),
    path("user-hits/", user_hits_page, name="user-hits"),
    path("user-dashboard/", user_dashboard_page, name="user-dashboard"),
    path("user-dashboard/export/", user_dashboard_export, name="user-dashboard-export"),
    path("api/v1/dashboard/", DashboardAPIView.as_view(), name="dashboard-api"),
    path("api/v1/user-hits/", UserHitsAPIView.as_view(), name="user-hits-api"),
    path("api/v1/user-dashboard/", UserDashboardAPIView.as_view(), name="user-dashboard-api"),
    path("api/v1/sync/", SyncTriggerView.as_view(), name="sync-trigger"),
    path("api/v1/", include(router.urls)),
]
