"""Changing a gown's pick-up, return and/or event date: the ONE set of rules and the ONE way of saving it.

Active Reservations (arabela_admin/views.py) calls it for a change staff save at once; reservations/date_requests.py calls the same two
functions again when a change that had to be paid first is paid. `check` refuses a change (DateChangeError) and says what it would be;
`apply` saves exactly that: the dates, the gown's rest block, the history lines. Both expect the caller to be inside a transaction with
the gown's row locked and the item freshly read."""
from datetime import timedelta

from django.utils import timezone

from gowns.models import COOLDOWN_DAYS, GownUnavailability
from reservations import ledger
from reservations.models import DateChangeRequest, Reservation, ReservationItem, ReservationStatusEvent

PICKUP_MAX_DAYS_AHEAD = 60        # how far from today a pick-up can be moved
RETURN_MAX_DAYS_AHEAD = 365       # a sanity limit for the return date (nobody keeps a gown longer than that)


class DateChangeError(Exception):
    """The change can't be made; nothing has been changed. `message` is written for STAFF (it may name another customer who holds the gown).
    `public` is what the booking's own customer may be told ('' when there is nothing safe to say beyond "it can't be saved")."""

    def __init__(self, message, status=400, *, public=""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.public = public


def plural_days(n):
    return f"{n} day{'s' if n != 1 else ''}"


# ---------------------------------------------------------------- what already holds a gown
def holding_items(gown_ids):
    """Bookings that really hold these physical gowns -- the same rule checkout's _find_available_unit and the customer calendar use: every
    booking except one already Returned, one the shop cancelled before pick-up, or whose reservation was Rejected/Cancelled (those never
    held stock). Pending ones count: they're awaiting payment approval, not released."""
    return (
        ReservationItem.objects.filter(gown_id__in=gown_ids)
        .exclude(stage__in=ReservationItem.FINISHED_STAGES)
        .exclude(reservation__status__in=[Reservation.Status.REJECTED, Reservation.Status.CANCELLED])
    )


def held_requests(gown_ids):
    """The date change requests still waiting for payment (and inside their time) for these gowns: the new days are held for them."""
    return DateChangeRequest.objects.filter(
        gown_id__in=gown_ids, status=DateChangeRequest.Status.WAITING, expires_at__gt=timezone.now())


def schedule_conflict(item, rental_date, return_date, rest_days=0):
    """What (if anything) already holds THIS physical gown on any day of rental_date..return_date, besides the booking being moved -- as a
    message naming it, or None. Inclusive on both ends, like checkout: a booking ending on the 12th collides with one starting on the 12th.

    rest_days: the days the gown must rest after it comes back (the booking's own cooldown block). When given, a LATER booking may not start
    inside those days either -- the same rule the website applies when a customer books (gowns/availability.plan_unit: the rental fits, but its
    rest days would run into a later booking). Another booking's rest block, or a staff block, inside the rest days is not a clash (as on the
    website), but any block inside the rental days still is. Days held for another booking's date change that is waiting for payment count as
    booked."""
    clash = (
        holding_items([item.gown_id])
        .filter(rental_date__lte=return_date + timedelta(days=rest_days), return_date__gte=rental_date)
        .exclude(id=item.id)
        .select_related("reservation__customer__profile")
        .order_by("rental_date")
        .first()
    )
    if clash and clash.rental_date > return_date:
        # It starts after the return, inside the rest days: the dates fit, but the gown would not get its rest.
        latest = clash.rental_date - timedelta(days=rest_days + 1)
        advice = (f" The latest return date is {latest:%b %d, %Y}." if latest > item.return_date else
                  " The return date can't be moved later without running into it.")
        return (
            f"{item.gown_name} needs {rest_days} rest days after it comes back, and {clash.reservation.display_customer_name} "
            f"({clash.reservation.reference_code}) has it from {clash.rental_date:%b %d, %Y}.{advice}"
        )
    if clash:
        return (
            f"{item.gown_name} is already booked {clash.rental_date:%b %d} – "
            f"{clash.return_date:%b %d, %Y} for {clash.reservation.display_customer_name} "
            f"({clash.reservation.reference_code}). Pick dates that don't overlap."
        )
    held = (
        held_requests([item.gown_id])
        .filter(hold_start__lte=return_date + timedelta(days=rest_days), hold_end__gte=rental_date)
        .exclude(item_id=item.id)
        .select_related("reservation__customer__profile")
        .order_by("hold_start")
        .first()
    )
    if held:
        return (
            f"{item.gown_name} is held {held.hold_start:%b %d} – {held.hold_end:%b %d, %Y} for "
            f"{held.reservation.display_customer_name} ({held.reservation.reference_code}) while their date change waits for payment. "
            "Pick dates that don't overlap."
        )
    block = (
        GownUnavailability.objects.filter(
            gown_id=item.gown_id, start_date__lte=return_date, end_date__gte=rental_date,
        )
        # This item's OWN trailing cooldown isn't a foreign obstacle -- it's about to
        # be moved to sit after whatever return_date is being saved right now (see
        # resync_cooldown_block below), so it must never block its own pick-up / return change.
        .exclude(auto_for_item=item)
        .order_by("start_date").first()
    )
    if block:
        return (
            f"{item.gown_name} is blocked for {block.get_reason_display().lower()} "
            f"{block.start_date:%b %d} – {block.end_date:%b %d, %Y}. Pick dates that "
            f"don't overlap."
        )
    return None


def return_floor(item):
    """The earliest day staff may move a gown's return to (besides today and its pick-up): a free-days booking -- the end of its free days (2 days after
    the event first booked: returning by then costs nothing, and the gown is kept until then anyway); an older booking -- the return date the
    customer first booked (so a later return saved by mistake can be undone, but nothing earlier than what was booked)."""
    if item.uses_free_days:
        return item.free_return_date
    return item.original_return_date or item.return_date


def extra_days_words(item):
    """A free-days gown's deduction in words for its history line ("Extra days now: 4 x P200 = P800, taken from the security deposit."), or ''."""
    if not item.uses_free_days:
        return ""
    early, later, change = ledger.booked_extra_days(item)
    days = early + later + change
    rate = int(item.reservation.late_fee_per_day or 0)
    if days <= 0 or rate <= 0:
        return " No extra days: nothing is taken from the security deposit for the dates."
    return f" Extra days now: {days} × ₱{rate:,} = ₱{days * rate:,}, taken from the security deposit."


def resync_cooldown_block(item, base_date):
    """Move this item's auto-created cooldown block (if it still has one) to sit right after base_date, matching reservation_submit's own
    [+1, +COOLDOWN_DAYS] window.

    If staff already released/deleted it, that choice is respected -- this never recreates one. getattr is safe here: Django raises
    RelatedObjectDoesNotExist (an AttributeError subclass) for a reverse one-to-one with nothing on the other end, exactly like a missing
    plain attribute."""
    block = getattr(item, "auto_cooldown_block", None)
    if block is None:
        return
    block.start_date = base_date + timedelta(days=1)
    block.end_date = base_date + timedelta(days=COOLDOWN_DAYS)
    block.save(update_fields=["start_date", "end_date"])


# ---------------------------------------------------------------- the rules
def check(item, want_pickup, want_return, want_event=None, *, today=None):
    """Whether this gown's dates may be changed this way (DateChangeError otherwise) -> what the change would be, for `apply`.

    Pick-up (want_pickup): the gown must still be waiting for pick-up; any day from today up to PICKUP_MAX_DAYS_AHEAD days ahead, earlier OR later than before.
    A pick-up that ends up after the current return date needs a new return date in the same request.
    Return (want_return): later, or earlier again (a date saved by mistake, or a customer who asks for fewer days) -- never a day that has passed, never
    before the pick-up, and never before the day the free days end (a free-days booking: 2 days after the event first booked) or, for an older booking,
    before the return date the customer first booked.
    Never: a day another booking, a block or a held date change holds this gown. When the RETURN moves, the gown's rest days after it must also stay clear of
    a later booking.
    Event (want_event): the customer's event day stays as they chose it, except when it is no longer between the pick-up and the return: a pick-up moved
    past it must come with the new event day, and a booking already like that can be given one on its own. The event day never changes any fee."""
    if item.stage in (ReservationItem.Stage.RETURNED, ReservationItem.Stage.CANCELLED):
        raise DateChangeError("This gown just changed (it may have been returned or cancelled). Please refresh the page.", 409)
    if want_pickup is None and want_return is None and want_event is None:
        raise DateChangeError("Choose a new pick-up date or a new return date.")

    today = today or timezone.localdate()
    old_pickup, old_return = item.rental_date, item.return_date
    old_event = item.effective_event_date        # worked out from the dates as they are NOW, before anything moves
    if want_pickup is not None:
        if item.stage != ReservationItem.Stage.PICKUP:
            raise DateChangeError("The gown has already been picked up, so its pick-up date can't change.")
        if want_pickup == old_pickup:
            raise DateChangeError(f"The pick-up is already on {want_pickup:%b %d, %Y}. Choose a different day.")
        if want_pickup < today:
            raise DateChangeError("The new pick-up date can't be a day that has already passed.")
        last_pickup = today + timedelta(days=PICKUP_MAX_DAYS_AHEAD)
        if want_pickup > last_pickup:
            raise DateChangeError(f"The pick-up can be moved up to {PICKUP_MAX_DAYS_AHEAD} days from today (until {last_pickup:%b %d, %Y}).")
    if want_return is not None:
        if want_return == old_return:
            raise DateChangeError(f"The return is already on {want_return:%b %d, %Y}. Choose a different day.")
        if want_return < today:
            raise DateChangeError("The new return date can't be a day that has already passed.")
        base = return_floor(item)
        if want_return < base:
            raise DateChangeError(
                f"The return can't be before {base:%b %d, %Y}: returning by then is free (2 days after the event the customer booked)."
                if item.uses_free_days else
                f"The return can't be moved before the date the customer first booked ({base:%b %d, %Y}).")
        if want_return > today + timedelta(days=RETURN_MAX_DAYS_AHEAD):
            raise DateChangeError("That return date is too far away. Please choose a nearer day.")

    final_pickup = want_pickup if want_pickup is not None else old_pickup
    final_return = want_return if want_return is not None else old_return
    if final_pickup > final_return:
        if want_return is None:
            raise DateChangeError(
                f"The new pick-up ({final_pickup:%b %d, %Y}) is after the return date ({old_return:%b %d, %Y}). Choose the new return date too.")
        raise DateChangeError("The return date can't be before the pick-up date.")

    if want_event is not None:
        if final_pickup <= old_event <= final_return:
            raise DateChangeError(f"The event date stays {old_event:%b %d, %Y}: it is already between the pick-up and the return.")
        if not final_pickup <= want_event <= final_return:
            raise DateChangeError(
                f"The new event date must be between the pick-up ({final_pickup:%b %d, %Y}) and the return ({final_return:%b %d, %Y}).")
    elif want_pickup is not None and final_pickup > old_event:
        raise DateChangeError(
            f"The new pick-up ({final_pickup:%b %d, %Y}) is after the event date ({old_event:%b %d, %Y}). Choose the new event date too.")
    elif want_return is not None and final_return < old_event:
        raise DateChangeError(
            f"The new return ({final_return:%b %d, %Y}) is before the event date ({old_event:%b %d, %Y}). Choose the new event date too.")

    rest_days = 0
    if item.gown_id and (want_pickup is not None or want_return is not None):
        # Rest days only matter when the return moves, and only while the gown still HAS its rest block (staff may have released it on purpose).
        rest_days = COOLDOWN_DAYS if (want_return is not None and getattr(item, "auto_cooldown_block", None) is not None) else 0
        conflict = schedule_conflict(item, final_pickup, final_return, rest_days=rest_days)
        if conflict:
            raise DateChangeError(conflict, 409, public="The new dates are no longer free for this gown.")
    return {
        "want_pickup": want_pickup, "want_return": want_return, "want_event": want_event,
        "old_pickup": old_pickup, "old_return": old_return, "old_event": old_event,
        "pickup": final_pickup, "return": final_return,
    }


def preview(item, plan):
    """Puts the checked change on an item object WITHOUT saving it -- what `apply` will save, so the money can be worked out first."""
    if item.event_date is None:
        item.event_date = plan["old_event"]
    if plan["want_event"] is not None:
        item.event_date = plan["want_event"]
    if plan["want_pickup"] is not None:
        if item.original_rental_date is None:
            item.original_rental_date = plan["old_pickup"]
        item.rental_date = plan["want_pickup"]
    if plan["want_return"] is not None:
        if item.original_return_date is None:
            item.original_return_date = plan["old_return"]
        item.return_date = plan["want_return"]
        item.overdue_date = plan["want_return"]
        # The day the customer once said they would come (1 day after the event) no longer means anything once staff set the return.
        item.planned_return_date = None


def apply(item, plan, *, by_name, actor=ReservationStatusEvent.Actor.STAFF):
    """Saves a change `check` allowed: the dates together, the gown's rest block moved with the return, and the history lines. Every saved
    return change is kept on the gown (return_changes): from the third one the Audit Log line is marked important for the owner
    (arabela_admin/audit_describe.py). Returns what the page is told."""
    want_pickup, want_return, want_event = plan["want_pickup"], plan["want_return"], plan["want_event"]
    old_pickup, old_return, old_event = plan["old_pickup"], plan["old_return"], plan["old_event"]
    final_pickup, final_return = plan["pickup"], plan["return"]

    # The event day is the customer's own and doesn't move with the dates -- but until it is saved it is *computed* from them, so freeze it first.
    preview(item, plan)
    fields = ["updated_at", "event_date"]
    if want_pickup is not None:
        fields += ["rental_date", "original_rental_date"]
    if want_return is not None:
        # Who moved the return, from what to what: the third change (and every one after) is shown to the owner as important.
        item.return_changes = list(item.return_changes or []) + [{
            "from": old_return.isoformat(), "to": want_return.isoformat(), "by": by_name,
            "at": timezone.now().isoformat(timespec="seconds"),
        }]
        fields += ["return_date", "overdue_date", "original_return_date", "planned_return_date", "return_changes"]
    item.save(update_fields=fields)
    if want_return is not None and item.gown_id:
        resync_cooldown_block(item, want_return)                 # the rest days move with the return

    days_early = ((item.original_rental_date or old_pickup) - final_pickup).days
    days_late = (final_return - (item.original_return_date or old_return)).days
    if want_pickup is not None:
        if days_early > 0:
            how = f"{plural_days(days_early)} earlier than originally booked"
        elif days_early == 0:
            how = "the day it was originally booked"
        else:
            how = f"{plural_days(-days_early)} later than originally booked"
        ReservationStatusEvent.record(
            item.reservation, f"{item.gown_name} pick-up date changed", item=item,
            detail=(f"Pick-up moved {'back ' if want_pickup > old_pickup else ''}from {old_pickup:%b %d} to {want_pickup:%b %d, %Y} at the customer's request ({how})."
                    + ("" if want_return is not None else extra_days_words(item))),
            actor=actor,
        )
    if want_return is not None:
        if want_return > old_return:
            how = f"{plural_days(max(days_late, 0))} later than originally booked"
        else:
            how = f"{plural_days((old_return - want_return).days)} earlier than before"
        ReservationStatusEvent.record(
            item.reservation, f"{item.gown_name} return date changed", item=item,
            detail=f"Return moved from {old_return:%b %d} to {want_return:%b %d, %Y} at the customer's request ({how})." + extra_days_words(item),
            actor=actor,
        )
    if want_event is not None:
        ReservationStatusEvent.record(
            item.reservation, f"{item.gown_name} event date changed", item=item,
            detail=f"Event date moved from {old_event:%b %d} to {want_event:%b %d, %Y} at the customer's request.",
            actor=actor,
        )
    return {
        "success": True, "rental_date": final_pickup.isoformat(), "return_date": final_return.isoformat(), "days_early": days_early, "days_late": days_late,
        "event_date": item.effective_event_date.isoformat(),
    }
