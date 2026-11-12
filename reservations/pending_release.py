"""A booking the shop does not verify within 2 days frees its dates by itself (and the owner's 12-hour payment reminder rides the same sweep).

Why: a Pending booking holds its gowns the moment it is submitted, and nothing used to end that hold. Someone sending fake proofs could keep
every gown "taken" for as long as nobody opened them. Now an online booking that is still Pending 2 days after it was made is released:
  * it is closed as Rejected with a plain reason (a REJECTED booking is a shop decision, so it is not held against the customer the way a
    cancellation is), the gowns' automatic rest days go with it, and the customer gets a message in the bell and a line in the timeline;
  * a booking whose payment the OWNER already confirmed in her GCash is never released (the money is there; it only needs approving).

How it runs without a scheduler (this project has none, see reservations/reminders.py): a small middleware calls `maybe_release()` on page
visits, at most once a minute per server process, and everything it does is a conditional UPDATE, so two processes can never release the same
booking twice. Whatever goes wrong in here is logged and swallowed: a page must never fail because of this sweep.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from accounts.models import CustomerMessage
from gowns.models import GownUnavailability

from . import cancel_requests, owner_check
from .models import Reservation, ReservationStatusEvent

logger = logging.getLogger(__name__)

RELEASE_AFTER = timedelta(days=2)
RELEASE_NOTE = (
    "The shop did not verify the payment within 2 days, so this booking was released and its gowns are free again. "
    "If you already paid, please message the shop on Facebook with your receipt."
)
_THROTTLE_SECONDS = 60
_THROTTLE_KEY = "arabela:pending-release:last-run"


def _gown_names(reservation):
    names = [item.gown_name for item in reservation.items.all()]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def stale_pending(now=None):
    """The online bookings that have waited longer than RELEASE_AFTER for the shop (never one the owner already confirmed)."""
    now = now or timezone.now()
    return (
        Reservation.objects.filter(
            status=Reservation.Status.PENDING, channel=Reservation.Channel.ONLINE, created_at__lte=now - RELEASE_AFTER,
        )
        .exclude(owner_check=Reservation.OwnerCheck.RECEIVED)
    )


def release_one(pk, now=None):
    """Release one booking. True when this call released it (False when someone else decided it first, or it is no longer stale)."""
    now = now or timezone.now()
    with transaction.atomic():
        claimed = stale_pending(now).filter(pk=pk).update(
            status=Reservation.Status.REJECTED, reviewed_at=now, updated_at=now, notes=RELEASE_NOTE,
        )
        if not claimed:
            return False
        reservation = Reservation.objects.get(pk=pk)
        # The owner had it on her list to check: there is nothing left to check on a released booking.
        Reservation.objects.filter(pk=pk, owner_check=Reservation.OwnerCheck.PENDING).update(owner_check="")
        # These gowns never went out, so the automatic rest after them goes too (the same thing rejecting a booking does).
        GownUnavailability.objects.filter(auto_for_item__reservation=reservation).delete()
        ReservationStatusEvent.record(
            reservation, "Reservation released",
            detail="The shop did not verify the payment within 2 days, so the gowns were released.",
            actor=ReservationStatusEvent.Actor.SYSTEM,
        )
        if reservation.has_account:
            CustomerMessage.objects.create(
                recipient=reservation.customer,
                category=CustomerMessage.Category.GENERAL,
                body=(
                    f"Your reservation {reservation.reference_code} ({_gown_names(reservation)}) was released because the shop "
                    "did not verify the payment within 2 days, so its gowns are free again. If you already paid, please message "
                    "the shop on Facebook with your receipt."
                ),
            )
    return True


def release_stale_pending(now=None, limit=50):
    """Release every stale booking (at most `limit` per call). Returns how many were released."""
    now = now or timezone.now()
    released = 0
    for pk in list(stale_pending(now).order_by("created_at").values_list("pk", flat=True)[:limit]):
        if release_one(pk, now):
            released += 1
    return released


def maybe_release():
    """The throttled sweep the middleware calls: at most once a minute per process, and never raises."""
    try:
        if not cache.add(_THROTTLE_KEY, 1, _THROTTLE_SECONDS):
            return 0
        return release_stale_pending()
    except Exception:       # noqa: BLE001 -- a page must never fail because of this sweep
        logger.exception("Releasing stale Pending bookings failed (pages are unaffected)")
        return 0


class PendingReleaseMiddleware:
    """Runs the throttled sweep on ordinary page visits (not on static files, not while the test suite runs: settings.PENDING_RELEASE_ON_REQUEST)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            getattr(settings, "PENDING_RELEASE_ON_REQUEST", False)
            and request.method == "GET"
            and not request.path.startswith(("/static/", "/media/", "/healthz"))
        ):
            maybe_release()
            owner_check.maybe_send_reminders()
            cancel_requests.maybe_send_reminders()
        return self.get_response(request)
