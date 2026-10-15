"""Cancelling a booking: what the admin's "Cancel booking" does, and how a cancelled booking is described on Reservation Records.

Only a booking nobody has picked up yet can be cancelled, and the whole booking goes at once (like the customer's own Cancel). The
security deposit is NOT refunded -- the booking stays on record as Cancelled with that written on it, so when the customer phones the
shop, staff can open the record and see that the deposit was already paid.

No new database columns: who cancelled, when and why are read back from the booking's own status log (ReservationStatusEvent)."""
from django.db import transaction
from django.utils import timezone
from django.utils.formats import date_format

from accounts.models import CustomerMessage
from accounts.services import sync_cancellation_flag
from gowns.models import Gown, GownUnavailability
from reservations.models import Reservation, ReservationItem, ReservationStatusEvent

# Written when the CUSTOMER cancels on the website (gowns.views.reservation_item_cancel).
CUSTOMER_CANCEL_LABEL = "Reservation cancelled"
# Written when STAFF cancel. The customer sees this one on their timeline, and it is what sends them the email.
SHOP_CANCEL_LABEL = "Reservation cancelled by Arabela"
# Staff-only twin of the line above: which staff member did it, and the reason in the shop's own words.
STAFF_NOTE_LABEL = "Booking cancelled by staff"

# key -> (what staff read in the shop's records, what the customer is told). "other" is told whatever staff wrote in the note.
REASONS = {
    "no_show": ("The gown was not picked up", "The gown was not picked up."),
    "customer_asked": ("The customer asked to cancel", "You asked us to cancel."),
    "other": ("Another reason", ""),
}
NOTE_MAX = 150


class CannotCancel(Exception):
    """The booking can't be cancelled right now. `message` is written for staff; `status` is the HTTP answer to give."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _peso(amount):
    return f"₱{amount:,.2f}"


def _day(moment):
    return date_format(timezone.localtime(moment), "M j, Y")


def can_cancel(reservation, items):
    """True when the admin may cancel this booking: approved, still waiting for pick-up, and nothing in it picked up yet."""
    return (
        reservation.status in (Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE)
        and bool(items)
        and all(item.stage == ReservationItem.Stage.PICKUP for item in items)
    )


def cancel_by_staff(reservation_id, *, staff_name, reason_key, note, typed_code):
    """Cancel the booking, release its gowns, tell the customer. Raises CannotCancel (nothing is changed) when it can't be done.

    Safe against a double click or two staff at once: the booking row is locked, so the second one finds it already Cancelled."""
    reason = REASONS.get(reason_key)
    if reason is None:
        raise CannotCancel("Choose why this booking is being cancelled.")
    note = (note or "").strip()[:NOTE_MAX]
    if reason_key == "other" and not note:
        raise CannotCancel("Please write the reason in the note.")

    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().select_related("customer").filter(id=reservation_id).first()
        if reservation is None:
            raise CannotCancel("Reservation not found.", 404)
        if reservation.status == Reservation.Status.CANCELLED:
            raise CannotCancel("This booking was already cancelled. Please refresh the page.", 409)
        if reservation.status == Reservation.Status.PENDING:
            raise CannotCancel("This booking is still waiting for approval. Use Reject in Pending Approval instead.")
        if reservation.status not in (Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE):
            raise CannotCancel(f"This booking can't be cancelled because its status is {reservation.status}.")
        items = list(reservation.items.select_related("gown"))
        if not can_cancel(reservation, items):
            raise CannotCancel(
                "A gown in this booking has already gone out, so the booking can't be cancelled. "
                "Mark the gown returned instead.")
        if (typed_code or "").strip().upper() != reservation.reference_code.upper():
            raise CannotCancel("The booking code you typed doesn't match. Type it exactly as shown.")

        reservation.status = Reservation.Status.CANCELLED
        reservation.save(update_fields=["status", "updated_at"])

        shop_reason = reason[0] + (f" - {note}" if note else "")
        customer_reason = reason[1] or note
        ReservationStatusEvent.record(
            reservation, SHOP_CANCEL_LABEL, detail=customer_reason, actor=ReservationStatusEvent.Actor.STAFF,
        )
        ReservationStatusEvent.record(
            reservation, STAFF_NOTE_LABEL, detail=f"{staff_name}: {shop_reason}",
            actor=ReservationStatusEvent.Actor.STAFF, staff_only=True,
        )

        # The gowns never went out, so they are free again -- unless another live booking still needs the same gown -- and the
        # automatic 5-day rest after a rental is dropped with them (there was no rental to rest from).
        for item in items:
            if item.gown_id and item.gown.status == Gown.Status.RESERVED and not item.gown_held_by_another_active_item:
                item.gown.status = Gown.Status.AVAILABLE
                item.gown.save(update_fields=["status", "updated_at"])
        GownUnavailability.objects.filter(auto_for_item__in=items).delete()

        gowns = ", ".join(item.gown_name for item in items)
        CustomerMessage.objects.create(
            recipient=reservation.customer,
            category=CustomerMessage.Category.GENERAL,
            body=(
                f"Your reservation {reservation.reference_code} ({gowns}) was cancelled by Arabela"
                + (f". {customer_reason}" if customer_reason else ".")
                + " Your security deposit is not refunded. If you would like to book again, please contact the shop."
            ),
        )

    # Counts toward the customer's cancellation limits like any cancelled reservation (see accounts.services).
    sync_cancellation_flag(reservation.customer)
    return reservation


def deposit_line(reservation):
    """The plain sentence staff read on a cancelled booking about the deposit."""
    if reservation.reviewed_at:
        return (f"Deposit {_peso(reservation.security_deposit)} was paid and verified on {_day(reservation.reviewed_at)}. "
                "It is not refunded.")
    return "Cancelled before the payment was verified."


def cancellation_facts(reservation, events):
    """{'by', 'when', 'why', 'deposit'} for a Cancelled booking. `events` = that booking's ReservationStatusEvent rows."""
    cancel = next((e for e in reversed(events) if e.label in (CUSTOMER_CANCEL_LABEL, SHOP_CANCEL_LABEL)), None)
    note = next((e for e in reversed(events) if e.label == STAFF_NOTE_LABEL), None)
    by, why = "Customer", "Cancelled by the customer on the website."
    if cancel is not None and cancel.label == SHOP_CANCEL_LABEL:
        name, _, shop_reason = (note.detail if note else "").partition(": ")
        by = f"Staff ({name})" if name and shop_reason else "Staff"
        why = shop_reason or cancel.detail or "No reason recorded."
    return {
        "by": by,
        "when": _day(cancel.occurred_at) if cancel else "",
        "why": why,
        "deposit": deposit_line(reservation),
    }
