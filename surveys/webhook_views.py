"""Public server-to-server notification endpoints."""

import hashlib
import hmac
from ipaddress import ip_address, ip_network

from django.conf import settings
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import TolunaNotification
from .toluna_notifications import TolunaNotificationError, ingest_toluna_notification


def _configured_networks(setting_name: str):
    """Return configured IP networks, or ``None`` for invalid configuration."""

    configured = getattr(settings, setting_name, ()) or ()
    if isinstance(configured, str):
        configured = configured.split(",")
    networks = []
    try:
        for value in configured:
            normalized = str(value or "").strip()
            if normalized:
                networks.append(ip_network(normalized, strict=False))
    except ValueError:
        return None
    return tuple(networks)


def _parsed_ip(value):
    try:
        return ip_address(str(value or "").strip())
    except ValueError:
        return None


def _is_in_networks(address, networks) -> bool:
    return bool(address and any(address in network for network in networks))


def _notification_source_ip(request, trusted_proxies):
    """Resolve the client IP without trusting a caller-supplied leftmost XFF.

    Forwarding headers are ignored unless the TCP peer is explicitly trusted.
    When it is trusted, proxy hops are removed from the right so values prepended
    by an untrusted caller can never impersonate an allowlisted Toluna address.
    """

    peer = _parsed_ip(request.META.get("REMOTE_ADDR"))
    if peer is None:
        return None
    if not _is_in_networks(peer, trusted_proxies):
        return peer

    forwarded = [
        value.strip()
        for value in str(request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")
        if value.strip()
    ]
    if not forwarded:
        real_ip = str(request.META.get("HTTP_X_REAL_IP") or "").strip()
        forwarded = [real_ip] if real_ip else []
    for value in reversed(forwarded):
        candidate = _parsed_ip(value)
        if candidate is None:
            return None
        if not _is_in_networks(candidate, trusted_proxies):
            return candidate
    return None


def _payload_value(payload, *names):
    if not isinstance(payload, dict):
        return None
    for name in names:
        if name in payload:
            return payload[name]
    lowered = {str(key).lower(): value for key, value in payload.items()}
    for name in names:
        if str(name).lower() in lowered:
            return lowered[str(name).lower()]
    return None


def _valid_member_status_hmac(payload) -> bool | None:
    """Verify Toluna Standard Encryption for JSON member-status payloads.

    ``None`` means the server-side HMAC key is not configured. Toluna defines
    the signed value as SurveyID + WaveID + MemberCode with no separators.
    """

    key = str(getattr(settings, "TOLUNA_NOTIFICATION_HMAC_KEY", "") or "")
    if not key:
        return None
    survey_id = _payload_value(payload, "SurveyId", "SurveyID")
    wave_id = _payload_value(payload, "WaveId", "WaveID")
    member_code = _payload_value(payload, "UniqueCode")
    supplied = str(_payload_value(payload, "EncryptedValue") or "").strip().lower()
    if any(value in (None, "") for value in (survey_id, wave_id, member_code)):
        return False
    try:
        supplied_digest = bytes.fromhex(supplied)
    except ValueError:
        return False
    if len(supplied_digest) != hashlib.sha256().digest_size:
        return False
    signed_value = f"{survey_id}{wave_id}{member_code}".encode("utf-8")
    calculated = hmac.new(
        key.encode("utf-8"),
        signed_value,
        hashlib.sha256,
    ).digest()
    return hmac.compare_digest(supplied_digest, calculated)


class TolunaNotificationAPIView(APIView):
    """Authenticate and ingest one documented Toluna JSON notification."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    event_type = ""

    @extend_schema(
        tags=["Toluna Notifications"],
        summary="Receive a Toluna server notification",
        description=(
            "Toluna calls this server-to-server endpoint with JSON. Requests must originate from "
            "a configured Toluna source IP. Member completion and termination payloads must also "
            "contain Toluna's valid HMAC-SHA256 EncryptedValue. Exact duplicate deliveries are "
            "acknowledged but are not applied twice."
        ),
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            401: OpenApiTypes.OBJECT,
            403: OpenApiTypes.OBJECT,
            503: OpenApiTypes.OBJECT,
        },
    )
    def post(self, request):
        allowed_networks = _configured_networks("TOLUNA_NOTIFICATION_IP_ALLOWLIST")
        trusted_proxies = _configured_networks("TOLUNA_NOTIFICATION_TRUSTED_PROXY_IPS")
        if allowed_networks is None or trusted_proxies is None or not allowed_networks:
            return Response(
                {"detail": "Toluna notifications are not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        source_ip = _notification_source_ip(request, trusted_proxies)
        if not _is_in_networks(source_ip, allowed_networks):
            return Response(
                {"detail": "Notification source is not allowed."},
                status=status.HTTP_403_FORBIDDEN,
            )
        content_type = str(request.content_type or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return Response(
                {"detail": "Content-Type must be application/json."},
                status=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            )
        if self.event_type in {
            TolunaNotification.EventType.MEMBER_COMPLETE,
            TolunaNotification.EventType.MEMBER_TERMINATE,
        }:
            require_hmac = bool(
                getattr(settings, "TOLUNA_NOTIFICATION_REQUIRE_HMAC", False)
            )
            if require_hmac:
                valid_hmac = _valid_member_status_hmac(request.data)
                if valid_hmac is None:
                    return Response(
                        {"detail": "Toluna member-status verification is not configured."},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE,
                    )
                if not valid_hmac:
                    return Response(
                        {"detail": "Invalid member-status signature."},
                        status=status.HTTP_401_UNAUTHORIZED,
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
