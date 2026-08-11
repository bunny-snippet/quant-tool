from celery import shared_task
from django.utils import timezone

from .models import AllocationReservation
from .services import expire_reservation


@shared_task(name="vendors.expire_allocation_reservations")
def expire_allocation_reservations_task(batch_size=500):
    reservation_ids = list(
        AllocationReservation.objects.filter(
            status=AllocationReservation.Status.RESERVED,
            expires_at__lte=timezone.now(),
        )
        .order_by("expires_at")
        .values_list("id", flat=True)[:batch_size]
    )
    expired = 0
    for reservation_id in reservation_ids:
        reservation = AllocationReservation.objects.filter(pk=reservation_id).first()
        if reservation and reservation.status == AllocationReservation.Status.RESERVED:
            expire_reservation(reservation)
            expired += 1
    return {"expired": expired, "examined": len(reservation_ids)}
