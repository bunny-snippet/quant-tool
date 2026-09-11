"""Durable, replay-safe result delivery to external survey suppliers."""

import http.client
import ipaddress
import logging
import socket
import ssl
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from vendors.credentials import decrypt_secret
from vendors.models import VendorAPIKey
from vendors.security import sign_supplier_callback

from .models import SurveyAttempt
from .outcomes import provider_outcome


logger = logging.getLogger(__name__)

DELIVERY_AUDIT_KEY = "supplier_callback_delivery"
TERMINAL_STATUS_CODES = {
    SurveyAttempt.Status.COMPLETED,
    SurveyAttempt.Status.TERMINATED,
    SurveyAttempt.Status.OVER_QUOTA,
    SurveyAttempt.Status.QUALITY_TERMINATED,
}


class SupplierCallbackRetryableError(RuntimeError):
    """Raised when a supplier callback can safely be retried by Celery."""


class UnsafeSupplierCallbackURL(ValueError):
    """Raised when a callback destination could reach a non-public network."""


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to a pre-validated IP with normal TLS SNI."""

    def __init__(self, hostname, resolved_address, port, *, connect_timeout, read_timeout):
        super().__init__(
            hostname,
            port=port,
            timeout=connect_timeout,
            context=ssl.create_default_context(),
        )
        self._resolved_address = resolved_address
        self._read_timeout = read_timeout

    def connect(self):
        raw_socket = socket.create_connection(
            (self._resolved_address, self.port),
            timeout=self.timeout,
        )
        try:
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
            self.sock.settimeout(self._read_timeout)
        except Exception:
            raw_socket.close()
            raise


def _delivery_event_id(attempt, status_code):
    return f"{attempt.pid}-{status_code}"


def build_supplier_result_url(attempt, status_code: str) -> str:
    """Build a signed supplier callback containing PID, never internal RID."""

    status_code = str(status_code)
    if status_code not in TERMINAL_STATUS_CODES or not attempt.supplier_api_key_id:
        return ""
    api_key = VendorAPIKey.objects.filter(pk=attempt.supplier_api_key_id).first()
    if not api_key:
        return ""
    callback_url = api_key.callback_url_for_status(status_code)
    if not callback_url:
        return ""
    callback_parts = urlsplit(callback_url)
    if (
        callback_parts.scheme != "https"
        or not callback_parts.hostname
        or callback_parts.username
        or callback_parts.password
    ):
        logger.error("Rejected unsafe supplier callback URL api_key=%s", api_key.pk)
        return ""
    outcome = provider_outcome(attempt)
    delivery = attempt.supplier_delivery_config or {}
    parameters = {
        "status": status_code,
        "surveyId": str(delivery.get("survey_id") or attempt.survey.local_id),
        "projectId": attempt.survey.local_id,
        "pid": attempt.pid,
        "eventId": _delivery_event_id(attempt, status_code),
        "timestamp": (
            attempt.callback_at or attempt.last_callback_at or timezone.now()
        ).isoformat(),
        "statusSource": attempt.status_source or "browser_callback",
        "termReason": outcome.get("reason", ""),
        "termCategory": outcome.get("category", ""),
    }
    existing_query = parse_qsl(callback_parts.query, keep_blank_values=True)
    reserved = {name.casefold() for name in parameters} | {"hash"}
    if any(str(name).casefold() in reserved for name, _value in existing_query):
        logger.error(
            "Rejected supplier callback URL with reserved query parameter api_key=%s",
            api_key.pk,
        )
        return ""
    if api_key.callback_signing_enabled:
        try:
            secret = decrypt_secret(api_key.encrypted_callback_secret)
        except ValueError:
            logger.error(
                "Could not decrypt supplier callback secret api_key=%s",
                api_key.pk,
            )
            return ""
        if not secret:
            logger.error(
                "Supplier callback signing is enabled without a secret api_key=%s",
                api_key.pk,
            )
            return ""
        parameters["hash"] = sign_supplier_callback(parameters, secret)
    return urlunsplit((
        callback_parts.scheme,
        callback_parts.netloc,
        callback_parts.path,
        urlencode([*existing_query, *parameters.items()]),
        callback_parts.fragment,
    ))


def _delivery_record(attempt):
    audit = attempt.upstream_transaction_data
    if not isinstance(audit, dict):
        audit = {}
    record = audit.get(DELIVERY_AUDIT_KEY)
    return audit, record if isinstance(record, dict) else {}


def _mark_queue_failure(attempt_id, event_id):
    with transaction.atomic():
        attempt = SurveyAttempt.objects.select_for_update().get(pk=attempt_id)
        audit, record = _delivery_record(attempt)
        if record.get("event_id") != event_id or record.get("state") != "queued":
            return
        audit[DELIVERY_AUDIT_KEY] = {
            **record,
            "state": "queue_failed",
            "last_error": "Callback worker queue unavailable.",
            "updated_at": timezone.now().isoformat(),
        }
        attempt.upstream_transaction_data = audit
        attempt.save(update_fields=["upstream_transaction_data", "updated_at"])


def _dispatch_supplier_callback(attempt_id, event_id):
    try:
        from .tasks import deliver_supplier_result_callback_task

        deliver_supplier_result_callback_task.delay(attempt_id, event_id)
    except Exception as exc:  # pragma: no cover - broker failures are environment-specific
        # Do not include the exception text: broker URLs may contain credentials.
        logger.error(
            "Could not queue supplier callback attempt_id=%s error_type=%s",
            attempt_id,
            type(exc).__name__,
        )
        _mark_queue_failure(attempt_id, event_id)


def queue_supplier_result_callback(attempt) -> bool:
    """Persist one terminal callback event, then enqueue it after commit.

    The marker is stored before dispatch, so provider replays and repeated local
    guard calls cannot create duplicate outbound jobs. ``eventId`` gives the
    receiving supplier an idempotency key for the unavoidable HTTP ambiguity
    where a connection can fail after the remote endpoint accepted the request.
    """

    status_code = str(attempt.status)
    if status_code not in TERMINAL_STATUS_CODES or not attempt.supplier_api_key_id:
        return False
    # Validate callback configuration before adding a delivery marker. The URL
    # is rebuilt by the worker so no signed URL or supplier query secret is
    # persisted in the operational database.
    if not build_supplier_result_url(attempt, status_code):
        return False
    event_id = _delivery_event_id(attempt, status_code)
    should_dispatch = False
    with transaction.atomic():
        locked = SurveyAttempt.objects.select_related("survey").select_for_update().get(
            pk=attempt.pk
        )
        if str(locked.status) != status_code:
            return False
        audit, existing = _delivery_record(locked)
        if existing.get("event_id") == event_id:
            # A broker outage means no HTTP request was issued, so a later
            # verified replay may safely recover that single queued event.
            if existing.get("state") != "queue_failed":
                return False
            audit[DELIVERY_AUDIT_KEY] = {
                **existing,
                "state": "queued",
                "last_error": "",
                "updated_at": timezone.now().isoformat(),
            }
        elif existing:
            logger.error(
                "Refused conflicting supplier callback event attempt_id=%s",
                locked.pk,
            )
            return False
        else:
            now = timezone.now().isoformat()
            audit[DELIVERY_AUDIT_KEY] = {
                "event_id": event_id,
                "status": status_code,
                "state": "queued",
                "attempt_count": 0,
                "queued_at": now,
                "updated_at": now,
                "last_error": "",
            }
        locked.upstream_transaction_data = audit
        locked.save(update_fields=["upstream_transaction_data", "updated_at"])
        transaction.on_commit(
            lambda: _dispatch_supplier_callback(locked.pk, event_id)
        )
        should_dispatch = True
    return should_dispatch


def resolve_public_callback_destination(callback_url):
    """Return URL parts and one validated IP to pin the TLS connection to."""

    parts = urlsplit(callback_url)
    hostname = (parts.hostname or "").rstrip(".").casefold()
    if (
        parts.scheme != "https"
        or not hostname
        or parts.username
        or parts.password
        or hostname == "localhost"
        or hostname.endswith(".localhost")
    ):
        raise UnsafeSupplierCallbackURL("Supplier callback destination is not allowed.")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise UnsafeSupplierCallbackURL(
            "Supplier callback destination has an invalid port."
        ) from exc
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            resolved = socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except (socket.gaierror, ValueError) as exc:
            raise SupplierCallbackRetryableError(
                "Supplier callback hostname could not be resolved."
            ) from exc
        addresses = []
        for row in resolved:
            try:
                addresses.append(ipaddress.ip_address(row[4][0]))
            except (IndexError, ValueError):
                continue
    if not addresses or any(not address.is_global for address in addresses):
        raise UnsafeSupplierCallbackURL("Supplier callback destination is not public.")
    return parts, hostname, port, str(addresses[0])


def validate_public_callback_destination(callback_url):
    """Reject loopback/private/link-local callback targets before server I/O."""

    resolve_public_callback_destination(callback_url)


def _send_pinned_supplier_callback(callback_url, event_id):
    """GET a callback using the same public IP that passed validation."""

    parts, hostname, port, resolved_address = resolve_public_callback_destination(
        callback_url
    )
    connection = _PinnedHTTPSConnection(
        hostname,
        resolved_address,
        port,
        connect_timeout=settings.SUPPLIER_CALLBACK_CONNECT_TIMEOUT_SECONDS,
        read_timeout=settings.SUPPLIER_CALLBACK_READ_TIMEOUT_SECONDS,
    )
    request_target = urlunsplit(("", "", parts.path or "/", parts.query, ""))
    try:
        connection.request(
            "GET",
            request_target,
            headers={
                "User-Agent": "Quest-Tool-Supplier-Callback/1.0",
                "X-Quest-Event-ID": event_id,
            },
        )
        return connection.getresponse().status
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise SupplierCallbackRetryableError(
            "Supplier callback request failed."
        ) from exc
    finally:
        connection.close()


def _finish_delivery(attempt_id, event_id, *, state, http_status=None, error=""):
    with transaction.atomic():
        attempt = SurveyAttempt.objects.select_for_update().get(pk=attempt_id)
        audit, record = _delivery_record(attempt)
        if record.get("event_id") != event_id:
            return False
        now = timezone.now()
        updated = {
            **record,
            "state": state,
            "http_status": http_status,
            "last_error": str(error or "")[:240],
            "updated_at": now.isoformat(),
        }
        if state == "delivered":
            updated["delivered_at"] = now.isoformat()
        audit[DELIVERY_AUDIT_KEY] = updated
        attempt.upstream_transaction_data = audit
        attempt.save(update_fields=["upstream_transaction_data", "updated_at"])
    return True


def deliver_supplier_result_callback(attempt_id, event_id):
    """Deliver one persisted callback and update its audit state."""

    with transaction.atomic():
        attempt = SurveyAttempt.objects.select_related("survey").select_for_update().get(
            pk=attempt_id
        )
        audit, record = _delivery_record(attempt)
        if record.get("event_id") != event_id:
            return {"status": "skipped", "reason": "event mismatch"}
        if record.get("state") == "delivered":
            return {"status": "skipped", "reason": "already delivered"}
        if record.get("state") == "delivering":
            last_attempt_at = parse_datetime(str(record.get("last_attempt_at") or ""))
            if (
                last_attempt_at
                and last_attempt_at > timezone.now() - timedelta(seconds=30)
            ):
                raise SupplierCallbackRetryableError(
                    "Supplier callback delivery is already running."
                )
            # A worker may have been killed after claiming the event. Celery's
            # late acknowledgement redelivers it and a stale claim is safe to
            # reclaim with the same remote idempotency key.
        if record.get("state") not in {
            "queued", "failed", "queue_failed", "delivering",
        }:
            return {"status": "skipped", "reason": "event is not deliverable"}
        record = {
            **record,
            "state": "delivering",
            "attempt_count": int(record.get("attempt_count") or 0) + 1,
            "last_attempt_at": timezone.now().isoformat(),
            "updated_at": timezone.now().isoformat(),
            "last_error": "",
        }
        audit[DELIVERY_AUDIT_KEY] = record
        attempt.upstream_transaction_data = audit
        attempt.save(update_fields=["upstream_transaction_data", "updated_at"])

    callback_url = build_supplier_result_url(attempt, record["status"])
    if not callback_url:
        _finish_delivery(
            attempt_id,
            event_id,
            state="failed",
            error="Callback configuration is unavailable.",
        )
        return {"status": "failed", "reason": "callback unavailable"}
    try:
        response_status = _send_pinned_supplier_callback(callback_url, event_id)
    except UnsafeSupplierCallbackURL:
        _finish_delivery(
            attempt_id,
            event_id,
            state="failed",
            error="Callback destination is not allowed.",
        )
        return {"status": "failed", "reason": "unsafe destination"}
    except SupplierCallbackRetryableError:
        _finish_delivery(
            attempt_id,
            event_id,
            state="failed",
            error="Supplier callback request failed.",
        )
        raise
    if 200 <= response_status < 300:
        _finish_delivery(
            attempt_id,
            event_id,
            state="delivered",
            http_status=response_status,
        )
        return {"status": "delivered", "http_status": response_status}
    _finish_delivery(
        attempt_id,
        event_id,
        state="failed",
        http_status=response_status,
        error=f"Supplier callback returned HTTP {response_status}.",
    )
    if response_status == 429 or response_status >= 500:
        raise SupplierCallbackRetryableError(
            f"Supplier callback returned HTTP {response_status}."
        )
    return {"status": "failed", "http_status": response_status}
