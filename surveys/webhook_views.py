"""Public server-to-server notification endpoints."""

import hmac

from django.conf import settings
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import TolunaNotification
from .toluna_notifications import TolunaNotificationError, ingest_toluna_notification


class TolunaNotificationAPIView(APIView):
    """Authenticate and ingest one documented Toluna JSON notification."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    event_type = ""

    @extend_schema(
        tags=["Toluna Notifications"],
        summary="Receive a Toluna server notification",
        description=(
            "Toluna calls this server-to-server endpoint with JSON. Requests must include the "
            "configured X-Toluna-Token header. Exact duplicate deliveries are acknowledged but "
            "are not applied twice."
        ),
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        expected = str(getattr(settings, "TOLUNA_NOTIFICATION_TOKEN", "") or "")
        if not expected:
            return Response(
                {"detail": "Toluna notifications are not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        supplied = str(request.headers.get("X-Toluna-Token") or "")
        if not supplied or not hmac.compare_digest(supplied, expected):
            return Response(
                {"detail": "Invalid notification token."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        content_type = str(request.content_type or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return Response(
                {"detail": "Content-Type must be application/json."},
                status=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            )
        try:
            result = ingest_toluna_notification(self.event_type, request.data)
        except TolunaNotificationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            "accepted": True,
            "duplicate": result.duplicate,
            "event_id": result.notification.pk,
            "applied": result.notification.applied,
        })


class TolunaMemberCompleteNotificationAPIView(TolunaNotificationAPIView):
    event_type = TolunaNotification.EventType.MEMBER_COMPLETE


class TolunaMemberTerminateNotificationAPIView(TolunaNotificationAPIView):
    event_type = TolunaNotification.EventType.MEMBER_TERMINATE


class TolunaSurveyClosedNotificationAPIView(TolunaNotificationAPIView):
    event_type = TolunaNotification.EventType.SURVEY_CLOSED


class TolunaQuotaStatusNotificationAPIView(TolunaNotificationAPIView):
    event_type = TolunaNotification.EventType.QUOTA_STATUS


class TolunaEnhancedTerminationNotificationAPIView(TolunaNotificationAPIView):
    event_type = TolunaNotification.EventType.ENHANCED_TERMINATION


class TolunaReconciliationNotificationAPIView(TolunaNotificationAPIView):
    event_type = TolunaNotification.EventType.RECONCILIATION
