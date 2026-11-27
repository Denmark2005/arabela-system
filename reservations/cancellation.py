"""Cancelling a booking, or just some of its gowns: what the admin's "Cancel booking" and "Cancel gown" do, and how a cancelled
booking is described on All Bookings.

Only a gown nobody has picked up yet can be cancelled. Two ways:
  * the WHOLE booking (cancel_by_staff) -- only while no gown in it has gone out, like the customer's own Cancel;
  * one or more GOWNS (cancel_gowns_by_staff) -- for a booking where some gowns went out or were returned and the rest never
    got collected. The cancelled gowns stop holding anything, and the booking carries on with the others. When the last gown
    left in a booking is cancelled, the whole booking becomes Cancelled, exactly as if it had been cancelled whole.
The refund rule (reservations/ledger.py): when a whole booking is cancelled the shop keeps the SECURITY DEPOSIT and everything else the
customer paid (rental, extra days) is to be given back from the Payments page (Reservation.refunds_rental); the owner may give the deposit
back too. When only some gowns are cancelled, their rental is no longer counted and any of it already paid goes back
(ReservationItem.rental_dropped); the deposit stays on the booking and is settled with the gowns that really went out
(Reservation.deposit_returned_on). Who cancelled, when and why are read back from the booking's own status log (ReservationStatusEvent)."""
from django.db import transaction
from django.utils import timezone
from django.utils.formats import date_format

from accounts.models import CustomerMessage
from accounts.services import sync_cancellation_flag
from gowns.models import Gown, GownUnavailability
from reservations import date_requests, ledger
from reservations.models import Reservation, ReservationItem, ReservationStatusEvent

# Written when the CUSTOMER cancels on the website (gowns.views.reservation_item_cancel).
CUSTOMER_CANCEL_LABEL = "Reservation cancelled"
# Written when STAFF cancel. The customer sees this one on their timeline, and it is what sends them the email.
SHOP_CANCEL_LABEL = "Reservation cancelled by Arabela"
# Staff-only twin of the line above: which staff member did it, and the reason in the shop's own words.
STAFF_NOTE_LABEL = "Booking cancelled by staff"
# Written when staff cancel only some gowns of a booking (the customer is emailed for it; the label decides the wording).
GOWN_CANCEL_LABEL = "Gown cancelled by Arabela"
GOWNS_CANCEL_LABEL = "Gowns cancelled by Arabela"
GOWN_STAFF_NOTE_LABEL = "Gown cancelled by staff"
DEPOSIT_SETTLED_LABEL = "Security deposit settled"

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


def _join_names(names):
    """'A', 'A and B', 'A, B and C'."""
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _compact_names(items, limit=3):
    """Short enough for a one-line timeline entry (300 characters): the first few gowns by name, then 'and N more'."""
    names = [item.gown_name for item in items]
    if len(names) <= limit:
        return _join_names(names)
    return f"{', '.join(names[:2])} and {len(names) - 2} more"


def _reason_or_error(reason_key, note):
    """(reason pair, trimmed note) for a valid reason, else CannotCancel -- the same two checks for both ways of cancelling."""
    reason = REASONS.get(reason_key)
    if reason is None:
        raise CannotCancel("Choose why this is being cancelled.")
    note = (note or "").strip()[:NOTE_MAX]
    if reason_key == "other" and not note:
        raise CannotCancel("Please write the reason in the note.")
    return reason, note


def can_cancel(reservation, items):
    """True when the admin may cancel this booking as a whole: approved, and every gown still part of it is waiting for pick-up.
    A gown the shop already cancelled is no longer part of the booking, so it counts neither for nor against."""
    live = [item for item in items if item.stage != ReservationItem.Stage.CANCELLED]
    return (
        reservation.status in (Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE)
        and bool(live)
        and all(item.stage == ReservationItem.Stage.PICKUP for item in live)
    )


def can_cancel_gowns(reservation):
    """True when this booking is one whose gowns staff may cancel one by one (approved, not cancelled/rejected/pending)."""
    return reservation.status in (
        Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE, Reservation.Status.OVERDUE,
    )


def _with_money(reservation):
    """The booking with everything a statement reads: itself when a list page already loaded it, else read fresh (one booking)."""
    loaded = getattr(reservation, "_prefetched_objects_cache", {})
    if {"items", "payments", "deductions", "receipt_records"} <= set(loaded):
        return reservation
    return Reservation.objects.prefetch_related("items", "payments", "deductions", "receipt_records").get(pk=reservation.pk)


def money_back(reservation):
    """The money a CANCELLED booking is still to get back: everything paid besides the security deposit (rental, extra days), and the deposit
    too when the owner gives it back; an older cancelled booking only what it paid over the deposit. 0 for most."""
    if not (reservation.pays_over_deposit or reservation.refunds_rental):
        return ledger.ZERO
    booking = _with_money(reservation)
    if booking.status != Reservation.Status.CANCELLED:
        return ledger.ZERO
    statement = ledger.statement(booking)
    if statement.tracked:
        return statement.refund_available
    # Cancelled before the shop checked the payment (the screenshot checkout): what was said to be paid over the deposit.
    return booking.upfront_extra if booking.payment_proof_url else ledger.ZERO


def back_if_cancelled(reservation):
    """For a booking the customer or the shop may still cancel: (what would be returned, the deposit that would stay with the shop)."""
    booking = _with_money(reservation)
    statement = ledger.statement(booking)
    if statement.tracked:
        return max(statement.received - statement.deposit_paid - statement.refunded, ledger.ZERO), statement.deposit_paid
    if not booking.payment_proof_url:
        return ledger.ZERO, ledger.ZERO
    return booking.upfront_extra, booking.security_deposit          # a screenshot payment the shop has not checked yet


def money_story(reservation, statement, *, customer=False):
    """For a CANCELLED booking: one row per kind of money, in plain words -- what happened to the rental, the extra days and the deposit.
    `customer`: the words the customer reads. Each row: {"label", "amount", "note", "tone"} (tone: due, done, kept, muted)."""
    if not statement.cancelled:
        return []
    rows = []
    back_word = "Coming back to you" if customer else "To give back"
    done_word = "Returned to you" if customer else "Returned"
    kept_word = "Kept by the shop (not refunded)" if customer else "Kept by the shop"
    left = statement.refunded                       # handed back goes against the rental first, then the extra days, then the deposit

    def share(amount):
        nonlocal left
        part = min(left, amount)
        left -= part
        return part

    def row(label, amount, note, tone):
        rows.append({"label": label, "amount": f"{amount:,.2f}", "note": note, "tone": tone})

    def returning(label, amount, extra=""):
        done = share(amount)
        if done >= amount:
            row(label, amount, done_word + extra, "done")
        else:
            row(label, amount, back_word + extra + (f" ({ledger.peso(done)} returned)" if done > 0 else ""), "due")

    paid_rental = max(statement.received - statement.extra_received - statement.deposit_paid, ledger.ZERO)
    full_rental = ledger.rental_due(reservation)
    if paid_rental > 0 and statement.refunds_rental:
        returning("Gown rental paid", paid_rental)
    elif paid_rental > 0:
        row("Gown rental paid", paid_rental, "Please contact the shop" if customer else "Cancelled before refunds: handled at the shop", "muted")
    if full_rental > paid_rental:
        unpaid = full_rental - paid_rental
        row("Gown rental not paid", unpaid, "Nothing to pay" if customer else "Will not be collected", "muted")
    if statement.extra_received > 0:
        if statement.refunds_rental or reservation.pays_over_deposit:
            returning("Extra days paid", statement.extra_received)
        else:
            row("Extra days paid", statement.extra_received, kept_word, "kept")
    if statement.deposit_paid > 0:
        if statement.deposit_back_too:
            returning("Security deposit", statement.deposit_paid, " (the shop's fault)")
        else:
            row("Security deposit", statement.deposit_paid, kept_word, "kept")
    elif statement.deposit > 0:
        row("Security deposit", statement.deposit, "Not paid", "muted")
    return rows


def rental_back_if_gowns_cancelled(statement, items):
    """How much MORE rental would have to be returned if these gowns of a running booking were cancelled (their rental stops counting)."""
    price = sum((ledger.money(item.rental_price) for item in items), ledger.ZERO)
    paid = statement.received - statement.extra_received - statement.deposit
    before = max(paid - statement.rental, ledger.ZERO)
    after = max(paid - max(statement.rental - price, ledger.ZERO), ledger.ZERO)
    return after - before


def tell_money_back(reservation):
    """After the CUSTOMER cancelled: the message about what comes back to them, when there is anything."""
    amount = money_back(reservation)
    if amount <= 0 or not reservation.has_account:
        return
    how = "the shop will send it back" if reservation.reviewed_at else "please contact the shop"
    CustomerMessage.objects.create(
        recipient_id=reservation.customer_id, category=CustomerMessage.Category.PAYMENT,
        body=(f"You cancelled {reservation.reference_code}. The security deposit is not refunded. The {ledger.peso(amount)} you paid besides "
              f"it will be returned to you: {how}."))


def _release_gowns(items):
    """These booking lines never went out, so their physical gowns are free again -- unless another live booking still needs the
    same gown -- and the automatic 5-day rest after a rental is dropped with them (there was no rental to rest from)."""
    for item in items:
        if item.gown_id and item.gown.status == Gown.Status.RESERVED and not item.gown_held_by_another_active_item:
            item.gown.status = Gown.Status.AVAILABLE
            item.gown.save(update_fields=["status", "updated_at"])
    GownUnavailability.objects.filter(auto_for_item__in=items).delete()


def _cancel_whole(reservation, items, *, staff_name, reason_key, note):
    """Everything that makes a booking Cancelled. `items` = the gowns still part of it (never picked up). Must run inside the
    caller's transaction, with the booking row locked."""
    reason = REASONS[reason_key]
    reservation.status = Reservation.Status.CANCELLED
    reservation.refunds_rental = True          # the shop keeps the deposit; everything else paid goes back (reservations/ledger.py)
    reservation.save(update_fields=["status", "refunds_rental", "updated_at"])

    shop_reason = reason[0] + (f" - {note}" if note else "")
    customer_reason = reason[1] or note
    ReservationStatusEvent.record(
        reservation, SHOP_CANCEL_LABEL, detail=customer_reason, actor=ReservationStatusEvent.Actor.STAFF,
    )
    ReservationStatusEvent.record(
        reservation, STAFF_NOTE_LABEL, detail=f"{staff_name}: {shop_reason}",
        actor=ReservationStatusEvent.Actor.STAFF, staff_only=True,
    )

    _release_gowns(items)
    # A date change still waiting for payment is no longer needed.
    date_requests.close(reservation.pk, note="The booking was cancelled.", tell_customer=False)
    back = money_back(reservation)

    # A booking recorded for a walk-in with no website account has nobody to message: staff tell them in person or by phone.
    if reservation.has_account:
        gowns = ", ".join(item.gown_name for item in items)
        CustomerMessage.objects.create(
            recipient=reservation.customer,
            category=CustomerMessage.Category.GENERAL,
            body=(
                f"Your reservation {reservation.reference_code} ({gowns}) was cancelled by Arabela"
                + (f". {customer_reason}" if customer_reason else ".")
                + " Your security deposit is not refunded."
                + (f" The {ledger.peso(back)} you paid besides it will be returned to you." if back > 0 else "")
                + " If you would like to book again, please contact the shop."
            ),
        )


def cancel_by_staff(reservation_id, *, staff_name, reason_key, note, typed_code):
    """Cancel the booking, release its gowns, tell the customer. Raises CannotCancel (nothing is changed) when it can't be done.

    Safe against a double click or two staff at once: the booking row is locked, so the second one finds it already Cancelled."""
    _reason_or_error(reason_key, note)
    note = (note or "").strip()[:NOTE_MAX]

    with transaction.atomic():
        # of=("self",): lock only the booking row. The customer is an outer join now (a walk-in booking may have no
        # account), and PostgreSQL refuses FOR UPDATE on the nullable side of an outer join.
        reservation = Reservation.objects.select_for_update(of=("self",)).select_related("customer").filter(id=reservation_id).first()
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
                "A gown in this booking has already gone out, so the booking can't be cancelled as a whole. "
                "Cancel the gowns nobody picked up one by one instead, or mark the gown returned.")
        if (typed_code or "").strip().upper() != reservation.reference_code.upper():
            raise CannotCancel("The booking code you typed doesn't match. Type it exactly as shown.")

        live = [item for item in items if item.stage != ReservationItem.Stage.CANCELLED]
        _cancel_whole(reservation, live, staff_name=staff_name, reason_key=reason_key, note=note)

    # Counts toward the customer's cancellation limits like any cancelled reservation (see accounts.services).
    sync_cancellation_flag(reservation.customer)
    return reservation


class GownCancelResult:
    """What cancel_gowns_by_staff did: the gowns cancelled, and whether that ended the whole booking (every gown cancelled) or
    finished it (every gown left is back)."""

    def __init__(self, reservation, cancelled, booking_cancelled, booking_complete):
        self.reservation = reservation
        self.cancelled = cancelled
        self.booking_cancelled = booking_cancelled
        self.booking_complete = booking_complete


def _gowns_to_cancel(all_items, item_ids):
    """The booking lines to cancel: exactly `item_ids`, each of which must belong to the booking and still be waiting for pick-up."""
    wanted = list(dict.fromkeys(item_ids))
    if not wanted:
        raise CannotCancel("Choose which gown to cancel.")
    by_id = {item.id: item for item in all_items}
    chosen = []
    for item_id in wanted:
        item = by_id.get(item_id)
        if item is None:
            raise CannotCancel("That gown is not part of this booking.", 404)
        if item.stage == ReservationItem.Stage.CANCELLED:
            raise CannotCancel(f"{item.gown_name} was already cancelled. Please refresh the page.", 409)
        if item.stage == ReservationItem.Stage.RETURNED:
            raise CannotCancel(f"{item.gown_name} was already returned, so it can't be cancelled.", 409)
        if item.stage != ReservationItem.Stage.PICKUP:
            raise CannotCancel(f"{item.gown_name} has already gone out, so it can't be cancelled. Mark it returned instead.")
        chosen.append(item)
    return chosen


def cancel_gowns_by_staff(reservation_id, item_ids, *, staff_name, reason_key, note):
    """Cancel some gowns of an approved booking -- the ones nobody picked up -- and leave the rest of the booking alone.

    `item_ids` = the booking lines to cancel (the exact gowns the staff member confirmed). Each cancelled gown is freed on the
    calendar (and its automatic rest block dropped), the customer is told which gowns and why, and nothing else about the booking
    changes. Raises CannotCancel (nothing is changed) when it can't be done. Safe against a double click or two staff at once: the
    booking row is locked and each gown is claimed with one conditional UPDATE, so a gown can only be cancelled once -- and never
    once somebody has marked it picked up."""
    reason, note = _reason_or_error(reason_key, note)

    with transaction.atomic():
        reservation = Reservation.objects.select_for_update(of=("self",)).select_related("customer").filter(id=reservation_id).first()
        if reservation is None:
            raise CannotCancel("Reservation not found.", 404)
        if reservation.status == Reservation.Status.CANCELLED:
            raise CannotCancel("This booking was already cancelled. Please refresh the page.", 409)
        if reservation.status == Reservation.Status.PENDING:
            raise CannotCancel("This booking is still waiting for approval. Use Reject in Pending Approval instead.")
        if not can_cancel_gowns(reservation):
            raise CannotCancel(f"This booking can't be changed because its status is {reservation.status}.")

        all_items = list(reservation.items.select_related("gown").order_by("rental_date", "id"))
        chosen = _gowns_to_cancel(all_items, item_ids)

        before = ledger.statement(_with_money(reservation))
        rental_back = rental_back_if_gowns_cancelled(before, chosen)
        claimed = ReservationItem.objects.filter(
            id__in=[item.id for item in chosen], stage=ReservationItem.Stage.PICKUP,
        ).update(stage=ReservationItem.Stage.CANCELLED, rental_dropped=True, updated_at=timezone.now())
        if claimed != len(chosen):
            raise CannotCancel("A gown in this booking changed while you were working. Please refresh the page.", 409)
        for item in chosen:
            item.stage = ReservationItem.Stage.CANCELLED
            item.rental_dropped = True

        remaining = [item for item in all_items if item.stage != ReservationItem.Stage.CANCELLED]
        if not remaining:
            # Nothing is left of the booking: it is cancelled as a whole, written up exactly like a whole-booking cancel.
            _cancel_whole(reservation, chosen, staff_name=staff_name, reason_key=reason_key, note=note)
            result = GownCancelResult(reservation, chosen, booking_cancelled=True, booking_complete=False)
        else:
            customer_reason = (
                ("The gown was not picked up." if len(chosen) == 1 else "The gowns were not picked up.")
                if reason_key == "no_show" else (reason[1] or note)
            )
            shop_reason = reason[0] + (f" - {note}" if note else "")
            ReservationStatusEvent.record(
                reservation, GOWN_CANCEL_LABEL if len(chosen) == 1 else GOWNS_CANCEL_LABEL,
                detail=f"{_compact_names(chosen)}: {customer_reason}", actor=ReservationStatusEvent.Actor.STAFF,
            )
            ReservationStatusEvent.record(
                reservation, GOWN_STAFF_NOTE_LABEL, detail=f"{staff_name}: {_compact_names(chosen)} - {shop_reason}",
                actor=ReservationStatusEvent.Actor.STAFF, staff_only=True,
            )

            _release_gowns(chosen)
            date_requests.close(reservation.pk, note="That gown was cancelled.", item_ids=[item.id for item in chosen])

            if reservation.has_account:
                price = sum((ledger.money(item.rental_price) for item in chosen), ledger.ZERO)
                if rental_back > 0:
                    about_money = f"The {ledger.peso(rental_back)} rental you paid for it will be returned to you. "
                elif price > 0 and before.tracked:
                    about_money = f"You no longer pay its {ledger.peso(price)} rental. "
                else:
                    about_money = ""
                CustomerMessage.objects.create(
                    recipient=reservation.customer,
                    category=CustomerMessage.Category.GENERAL,
                    body=(
                        f"Arabela cancelled {_join_names(item.gown_name for item in chosen)} from your reservation "
                        f"{reservation.reference_code}. {customer_reason} The rest of your reservation is unchanged. {about_money}"
                        "If you have any questions about your payment, please contact the shop."
                    ),
                )

            # Every gown left is already back: the booking is finished. The deposit is "settled" only once nothing is left to hand back or to pay --
            # the same line (and the same single email) the last gown's Mark Returned writes (reservations.payments.sync_settled).
            complete = all(item.stage == ReservationItem.Stage.RETURNED for item in remaining)
            if complete:
                from reservations import payments

                payments.sync_settled(reservation.pk)
            result = GownCancelResult(reservation, chosen, booking_cancelled=False, booking_complete=complete)

    if result.booking_cancelled:
        # Counts toward the customer's cancellation limits like any cancelled reservation (see accounts.services).
        sync_cancellation_flag(reservation.customer)
    return result


def deposit_line(reservation):
    """The plain sentence staff read on a cancelled booking about the deposit."""
    if reservation.reviewed_at:
        back = money_back(reservation)
        fate = (f"The owner gives it back: {reservation.deposit_refund_reason}." if reservation.deposit_refund_reason else "It is not refunded.")
        return (f"Deposit {_peso(reservation.security_deposit)} was paid and verified on {_day(reservation.reviewed_at)}. "
                f"{fate}" + (f" {_peso(back)} still has to be given back (Payments)." if back > 0 else ""))
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
