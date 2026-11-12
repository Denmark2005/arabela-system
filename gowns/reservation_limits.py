"""How many bookings and gowns one account may have at once -- the plan against someone booking every gown with fake proofs.

  * up to OPEN_BOOKINGS bookings waiting for the shop to verify (Pending) at a time -- OPEN_BOOKINGS_RETURNING once the customer has
    completed a rental (a gown of theirs came back);
  * up to GOWNS_PER_BOOKING gowns in one booking;
  * an account paused for refused proofs cannot book at all (accounts.services.record_rejected_proof).

Every number lives here, and the messages say them in plain words. Checked by the checkout (reservation_submit) -- the account's own row
is locked there so two quick submissions cannot both slip under the limit -- and, for the open-bookings limit and the pause, by the
"Proceed to Reservation" step too, so a customer is told before filling in the form.
"""
from accounts.services import proof_block_notice
from reservations.models import Reservation, ReservationItem

OPEN_BOOKINGS = 3
OPEN_BOOKINGS_RETURNING = 5
GOWNS_PER_BOOKING = 5


def is_returning_customer(user):
    """True when a gown of this customer has already come back (a completed rental)."""
    return ReservationItem.objects.filter(
        reservation__customer=user, stage=ReservationItem.Stage.RETURNED,
    ).exists()


def open_bookings(user):
    return Reservation.objects.filter(customer=user, status=Reservation.Status.PENDING).count()


def open_booking_cap(user):
    return OPEN_BOOKINGS_RETURNING if is_returning_customer(user) else OPEN_BOOKINGS


def open_bookings_message(user):
    """The refusal for a customer who already has the most bookings waiting for the shop, or '' when there is room for one more."""
    waiting, cap = open_bookings(user), open_booking_cap(user)
    if waiting < cap:
        return ""
    return (
        f"You already have {waiting} booking{'s' if waiting != 1 else ''} waiting for the shop to verify, which is the most one account can "
        "have at a time. Please wait until the shop approves or releases one before booking another."
    )


def gown_count_message(count):
    """The refusal for a selection with too many gowns, or ''."""
    if count <= GOWNS_PER_BOOKING:
        return ""
    extra = count - GOWNS_PER_BOOKING
    return (
        f"One booking can have up to {GOWNS_PER_BOOKING} gowns. Please remove {extra} gown{'s' if extra != 1 else ''} from your "
        "selection, or book them in a separate reservation later."
    )


def refusal_to_start(user):
    """What stops this account from starting a reservation at all (paused for refused proofs, or too many bookings waiting), or ''."""
    return proof_block_notice(user) or open_bookings_message(user)
