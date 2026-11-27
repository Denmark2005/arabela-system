"""What a booking OWES, what it has PAID, its DATE CHANGE and OVERDUE FEES and the DEPOSIT it can give back -- all worked out here, never typed by anyone.

The one rule set the Payments page, the printed receipts, the customer's booking page and the walk-in form all read:

  * Total to pay = the gown rental + the security deposit (the deposit the booking was made with).
  * Money received covers the DEPOSIT first, then the rental. (The deposit is the shop's protection and it is never paid in halves, see
    reservations/payments.py: a deposit payment is exactly the deposit, or the booking is paid in full.)
  * The DATE FEES (`late_fee` in the code: all of them together) = the booking's fee per day x the days charged, per gown. Worked out every
    time from the dates, never typed or stored. Two rule sets, by when the gown was booked:
      - FREE-DAYS bookings (ReservationItem.uses_free_days: made since the customer chooses the event, pick-up and return): 2 days before
        the event and 2 days after it are free. Every BOOKED day outside them is charged, whether or not the customer uses it (the gown is
        kept for them): the EARLY PICK-UP FEE (pick-up before the free days), the LATER RETURN FEE (the return the customer booked, after
        them) and the DATE CHANGE FEE (staff moved the return past the booked one). Staff changing the dates moves these with the new dates,
        up or down. The OVERDUE FEE is the days a gown comes back (or is still out) after its return date.
      - older bookings: counted from the return date the customer ORIGINALLY booked (a gown that is still out counts up to today -- "so
        far"; a gown that is back counts up to the day it came back), read as the DATE CHANGE FEE (the days the shop moved the return date)
        and the OVERDUE FEE (the days past the date agreed now). Moving a return date later never makes it disappear.
  * The fees and any manual deductions come out of the deposit. Refundable = deposit paid - fees - deductions. If they are
    MORE than the deposit, the extra is what the customer still owes ("extra owed"). An extra charge already paid that the fees
    no longer ask for (staff moved the dates back, a deduction was removed) goes back with the deposit ("extra credit").
  * Bookings made since amounts over the deposit are paid right away (Reservation.pays_over_deposit): booked extra days that cost
    more than the deposit are paid together with it (Payment.extra_part, "over with deposit"), a date change that goes over it is
    paid before it happens (reservations/date_requests.py), and a CANCELLED booking gets what it paid over the deposit back (the
    deposit itself is kept).
  * Balance still to pay = what is unpaid of the deposit and the rental + any extra owed.

A booking made before the Payments page has no Payment rows. Until the one-time backfill (manage.py backfill_payments) gives it some, its
payments are derived from what it already carries (the online deposit, the shop's labelled receipt photos), so every page is right
either way -- the derived rows and the backfilled rows are the same rows (legacy_rows()).

Pure arithmetic over what the caller already loaded (use prefetch_related("items", "payments", "deductions", "receipt_records")):
asking for a statement costs no query per booking.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from reservations.models import Deduction, Payment, ReceiptRecord, Reservation, ReservationItem

ZERO = Decimal("0.00")
CENT = Decimal("0.01")

# A booking whose payments and balance are tracked: approved, in progress or finished. (Pending ones are still under review, Rejected
# ones never held anything, and a Cancelled one only matters when it WAS approved: its deposit is kept.)
LIVE_STATUSES = (
    Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE, Reservation.Status.OVERDUE, Reservation.Status.RETURNED,
)
OUT_STAGES = (ReservationItem.Stage.RESERVED, ReservationItem.Stage.RETURN, ReservationItem.Stage.OVERDUE)

# The five states a booking's money can be in (the Payments page filters and the status chip read these).
BALANCE = "balance"        # something is still to pay
REFUND = "refund"          # every gown is back and there is deposit to give back
PAID = "paid"              # nothing to pay, gowns not all back yet
SETTLED = "settled"        # every gown is back and nothing is owed either way
CANCELLED = "cancelled"    # cancelled after approval: the deposit is kept

STATE_LABELS = {
    BALANCE: "Balance due", REFUND: "Refund due", PAID: "Paid in full", SETTLED: "Settled", CANCELLED: "Cancelled - deposit kept",
}

# The names of the date fees (the same money per day): see fee_days(), agreed_return() and overdue_days(). EARLY and LATER exist only on
# free-days bookings; CHANGE and OVERDUE on every booking.
EARLY_FEE, LATER_FEE, CHANGE_FEE, OVERDUE_FEE = "early", "later", "change", "overdue"
FEE_KINDS = (EARLY_FEE, LATER_FEE, CHANGE_FEE, OVERDUE_FEE)
FEE_LABELS = {EARLY_FEE: "Early pick-up fee", LATER_FEE: "Later return fee", CHANGE_FEE: "Date change fee", OVERDUE_FEE: "Overdue fee"}

# Where an approved booking's security deposit stands (deposit_stage): All Bookings and Rental History say it with these words, and the
# Security Deposits page counts it the same way (arabela_admin.views._deposit_state).
DEPOSIT_NOT_PAID, DEPOSIT_HELD, DEPOSIT_RETURN_DUE, DEPOSIT_RETURNED = "Not paid yet", "Held", "Return due", "Returned"


def long_day(value):
    """'Nov 29, 2026': a day the way a receipt writes it, or '' when there is none."""
    return f"{value:%b} {value.day}, {value.year}" if value else ""


def money(value):
    """A Decimal with exactly two places."""
    return Decimal(value or 0).quantize(CENT, rounding=ROUND_HALF_UP)


def peso(value, *, sign=True):
    """'₱3,500.00' -- how receipts and the Payments page write an amount."""
    return f"{'₱' if sign else ''}{money(value):,.2f}"


def over_deposit(fees, deposit):
    """How much of `fees` the deposit does not cover (never below zero)."""
    return max(money(fees) - money(deposit), ZERO)


def booked_over_deposit(reservation, items=None):
    """What a booking's BOOKED extra days cost over its security deposit, for a booking that pays that right away; 0 otherwise."""
    if not reservation.pays_over_deposit:
        return ZERO
    items = list(reservation.items.all()) if items is None else items
    rate = int(reservation.late_fee_per_day or 0)
    days = sum(sum(booked_extra_days(item)) for item in items)
    return over_deposit(days * rate, reservation.security_deposit)


def counts_as_tracked(reservation):
    """Whether this booking's payments are tracked at all (see LIVE_STATUSES)."""
    if reservation.status in LIVE_STATUSES:
        return True
    return reservation.status == Reservation.Status.CANCELLED and reservation.reviewed_at is not None


def rental_due(reservation):
    """The gown rental of the booking: what was saved as the rental when it adds up with the deposit to the total, else the total less the
    deposit (an old or odd row), never below zero."""
    rental, deposit, total = money(reservation.rental_subtotal), money(reservation.security_deposit), money(reservation.total_amount)
    if rental + deposit != total:
        rental = max(total - deposit, ZERO)
    return rental


# ---------------------------------------------------------------- late days
def _uses_free_days(item):
    return getattr(item, "free_pickup_date", None) is not None and getattr(item, "free_return_date", None) is not None


def _days_past_return(item, today):
    """Days a gown came back (or is still out) after its return date: what a free-days booking's Overdue fee counts."""
    if item.return_date is None:
        return 0
    if item.stage == ReservationItem.Stage.RETURNED:
        return max(0, (item.returned_on - item.return_date).days) if item.returned_on else 0
    if item.stage in OUT_STAGES:
        return max(0, (today - item.return_date).days)
    return 0


def booked_extra_days(item):
    """(early, later, change): the BOOKED days of a free-days booking outside its free days -- picked up before them, returned after them as
    the customer booked it, and returned after that because staff moved the return later. Charged whether or not the days are used, and
    moving with every date change (up or down). (0, 0, 0) for an older booking, a gown the shop cancelled, or a booking that was cancelled
    or rejected (its gown is not kept for those days: a cancelled booking keeps the deposit and charges nothing on top, as the Terms say)."""
    if not _uses_free_days(item) or item.stage == ReservationItem.Stage.CANCELLED or item.rental_date is None or item.return_date is None:
        return 0, 0, 0
    if item.reservation.status in (Reservation.Status.CANCELLED, Reservation.Status.REJECTED):
        return 0, 0, 0
    early = max(0, (item.free_pickup_date - item.rental_date).days)
    booked = item.original_return_date or item.return_date
    later = max(0, (min(item.return_date, booked) - item.free_return_date).days)
    change = max(0, (item.return_date - max(booked, item.free_return_date)).days)
    return early, later, change


def fee_days(item, today=None, planned=False):
    """{fee kind: days} for one gown -- EARLY_FEE, LATER_FEE, CHANGE_FEE, OVERDUE_FEE -- the ONE count every page, receipt and total uses.

    Free-days booking: the booked days outside the free days (booked_extra_days, from the moment it is booked) and the days it came back
    or is still out after its return date. Older booking: exactly the old rule -- late_days() (or planned_late_days() for the dates as they
    stand, `planned`) read as the date change part and the overdue part."""
    today = today or timezone.localdate()
    if _uses_free_days(item):
        early, later, change = booked_extra_days(item)
        overdue = _days_past_return(item, today) if item.stage != ReservationItem.Stage.CANCELLED else 0
        return {EARLY_FEE: early, LATER_FEE: later, CHANGE_FEE: change, OVERDUE_FEE: overdue}
    total = planned_late_days(item, today) if planned else late_days(item, today)
    overdue = overdue_days(item, today)
    return {EARLY_FEE: 0, LATER_FEE: 0, CHANGE_FEE: max(0, total - overdue), OVERDUE_FEE: overdue}


def late_days(item, today=None):
    """How many days this gown is late (or was). 0 = on time or not applicable.

    Free-days booking: the days charged after its free return day (the later return, the moved return and the overdue days; the early
    pick-up days are not "late"). Older booking: counted from the return date the customer ORIGINALLY booked (original_return_date, else the
    current one). A gown that has not been picked up, or that the shop cancelled, can't be late. One that is out counts up to `today` ("late
    fee so far"); one that is back counts up to the day it came back (returned_on). Unlike the Overdue label on Active Reservations -- which
    waits for the CURRENT return date -- the fee starts the day after the original one, so a customer who asks for the gown longer pays for
    the extra days."""
    today = today or timezone.localdate()
    if _uses_free_days(item):
        _early, later, change = booked_extra_days(item)
        return later + change + (_days_past_return(item, today) if item.stage != ReservationItem.Stage.CANCELLED else 0)
    scheduled = item.original_return_date or item.return_date
    if scheduled is None:
        return 0
    if item.stage == ReservationItem.Stage.RETURNED:
        return max(0, (item.returned_on - scheduled).days) if item.returned_on else 0
    if item.stage in OUT_STAGES:
        return max(0, (today - scheduled).days)
    return 0


def planned_late_days(item, today=None):
    """The late days of this gown for the dates as they stand NOW: what late_days() will say once the gown is back on its planned return date.

    A gown that is back counts exactly like late_days (up to the day it came back). One that has not come back yet counts up to its current return date --
    or up to today, when it is out and already past that date. So a return moved later shows the fee it will cost before the days have gone by, and
    the number never drops below what late_days() says today. Always counted from the return date the customer ORIGINALLY booked (the pick-up date and the
    event date never change it). A cancelled gown, or one with no return date, has none. A free-days booking: every day charged (fee_days)."""
    today = today or timezone.localdate()
    if _uses_free_days(item):
        return sum(fee_days(item, today, planned=True).values())
    scheduled = item.original_return_date or item.return_date
    if scheduled is None or item.return_date is None:
        return 0
    if item.stage == ReservationItem.Stage.CANCELLED:
        return 0
    if item.stage == ReservationItem.Stage.RETURNED:
        return max(0, (item.returned_on - scheduled).days) if item.returned_on else 0
    end = item.return_date
    if item.stage in OUT_STAGES and today > end:
        end = today
    return max(0, (end - scheduled).days)


def agreed_return(item):
    """The return date agreed with the customer now: the current one (staff move it later when the customer asks for more days), and never earlier
    than the one originally booked. The days between the two are the Date change fee; the days after it are overdue. A free-days booking: simply its
    return date (staff may also move it earlier, and the fee follows)."""
    if _uses_free_days(item):
        return item.return_date
    booked = item.original_return_date or item.return_date
    if booked is None or item.return_date is None:
        return booked
    return max(booked, item.return_date)


def overdue_days(item, today=None):
    """Of late_days(item), the days PAST the agreed return date: the gown is (or was) overdue -- the Overdue fee, and the "N days overdue" people read.
    The rest of late_days are days the return date was moved: the Date change fee."""
    if _uses_free_days(item):
        if item.stage == ReservationItem.Stage.CANCELLED:
            return 0
        return _days_past_return(item, today or timezone.localdate())
    total = late_days(item, today)
    if total <= 0:
        return 0
    ends = item.returned_on if item.stage == ReservationItem.Stage.RETURNED else (today or timezone.localdate())
    return min(total, max(0, (ends - agreed_return(item)).days))


# ---------------------------------------------------------------- old bookings
def legacy_rows(reservation):
    """The payments an OLD booking (one with no Payment rows) already carries, as plain dicts -- what the statement uses for it until the
    backfill saves them as real rows. An online booking that was approved paid its deposit online (GCash, checked by staff) and paid the
    rental at the shop if a shop receipt was attached to it; a walk-in's labelled receipt photos say what was paid at the counter."""
    if not counts_as_tracked(reservation):
        return []
    deposit, rental = money(reservation.security_deposit), rental_due(reservation)
    when = reservation.reviewed_at or reservation.created_at
    receipts = list(reservation.receipt_records.all())
    covers = ReceiptRecord.Covers
    labels = {r.covers for r in receipts}
    rows = []

    def row(purpose, method, amount, reference="", note=""):
        if amount > 0:
            rows.append({"direction": Payment.Direction.IN, "purpose": purpose, "method": method, "amount": amount,
                         "reference": reference, "paid_at": when, "note": note})

    if reservation.channel == Reservation.Channel.ONLINE:
        row(Payment.Purpose.DEPOSIT, Payment.Method.ONLINE_GCASH, deposit,
            reference=(reservation.proof_typed_reference or reservation.gcash_reference or "")[:20])
        # booked extra days over the deposit were paid in that same GCash payment
        row(Payment.Purpose.EXTRA, Payment.Method.ONLINE_GCASH, money(reservation.upfront_extra), note="Extra days over the deposit")
        if receipts:
            row(Payment.Purpose.BALANCE, Payment.Method.OLD, rental, note="Paid at the shop (receipt on file)")
    else:
        both = covers.BOTH in labels or {covers.RENTAL, covers.DEPOSIT} <= labels
        if both or covers.DEPOSIT in labels:
            row(Payment.Purpose.DEPOSIT, Payment.Method.OLD, deposit, note="Paid at the shop (receipt on file)")
        if both or covers.RENTAL in labels:
            row(Payment.Purpose.BALANCE, Payment.Method.OLD, rental, note="Paid at the shop (receipt on file)")
    return rows


# ---------------------------------------------------------------- the statement
def payment_rows(payment):
    """One stored payment as the rows it adds up as: its purpose, and -- when part of it paid extra days over the deposit
    (Payment.extra_part) -- that part as an extra charge of its own."""
    amount, part = money(payment.amount), money(payment.extra_part)
    if part <= 0 or payment.direction != Payment.Direction.IN or payment.purpose == Payment.Purpose.EXTRA:
        return [{"direction": payment.direction, "purpose": payment.purpose, "amount": amount}]
    part = min(part, amount)
    return [{"direction": payment.direction, "purpose": payment.purpose, "amount": amount - part},
            {"direction": payment.direction, "purpose": Payment.Purpose.EXTRA, "amount": part}]


class Statement:
    """Everything the money side of one booking adds up to. Plain attributes (Decimals); as_dict() gives the same as text for JSON."""

    def __init__(self, **values):
        self.__dict__.update(values)

    def as_dict(self):
        out = {}
        for key, value in self.__dict__.items():
            if isinstance(value, Decimal):
                out[key] = f"{value:.2f}"
            elif isinstance(value, list):
                out[key] = [
                    {k: (f"{v:.2f}" if isinstance(v, Decimal) else v) for k, v in entry.items()} if isinstance(entry, dict) else entry
                    for entry in value
                ]
            else:
                out[key] = value
        return out


def statement(reservation, today=None):
    """The Statement of one booking. Reads reservation.items / payments / deductions / receipt_records (prefetch them for a list)."""
    today = today or timezone.localdate()
    tracked = counts_as_tracked(reservation)
    cancelled = reservation.status == Reservation.Status.CANCELLED
    deposit_due, rental = money(reservation.security_deposit), rental_due(reservation)
    rate = int(reservation.late_fee_per_day or 0)

    items = list(reservation.items.all())
    live = [i for i in items if i.stage != ReservationItem.Stage.CANCELLED]

    stored = list(reservation.payments.all())
    legacy = False
    if stored:
        rows = [row for p in stored if not p.is_void for row in payment_rows(p)]
    else:
        rows = [{"direction": r["direction"], "purpose": r["purpose"], "amount": r["amount"]} for r in legacy_rows(reservation)]
        legacy = bool(rows)

    core_purposes = (Payment.Purpose.DEPOSIT, Payment.Purpose.FULL, Payment.Purpose.BALANCE)
    core_in = sum((r["amount"] for r in rows if r["direction"] == Payment.Direction.IN and r["purpose"] in core_purposes), ZERO)
    extra_in = sum((r["amount"] for r in rows if r["direction"] == Payment.Direction.IN and r["purpose"] == Payment.Purpose.EXTRA), ZERO)
    refunded = sum((r["amount"] for r in rows if r["direction"] == Payment.Direction.OUT), ZERO)

    # Money covers the deposit first, then the rental.
    deposit_paid = min(core_in, deposit_due)
    rental_paid = min(max(core_in - deposit_due, ZERO), rental)
    deposit_unpaid = deposit_due - deposit_paid
    rental_unpaid = rental - rental_paid

    late_lines, fee_lines = [], []
    for item in live:
        counts = fee_days(item, today)
        days = sum(counts.values())
        if days > 0 and rate > 0:
            back = item.stage == ReservationItem.Stage.RETURNED
            free_days_rule = _uses_free_days(item)
            overdue = counts[OVERDUE_FEE]
            booked, agreed = item.original_return_date or item.return_date, agreed_return(item)
            late_lines.append({
                "item_id": item.id, "gown": item.gown_name, "days": days, "rate": Decimal(rate), "amount": money(days * rate), "back": back,
                # the return date the fee is counted from (the ORIGINAL one) and the day it really came back: what a receipt says to explain the days
                "due": long_day(booked), "returned": long_day(item.returned_on), "change_days": days - overdue, "overdue_days": overdue,
                "free_days": free_days_rule,
            })
            # The same days under the names people read: an early pick-up and the later return as booked (free-days bookings), then moved
            # (booked -> agreed), then overdue (past the agreed date). "growing": another day adds to it (only an overdue gown, or an older
            # booking's moved return while it is out).
            for kind in FEE_KINDS:
                count = counts[kind]
                if count <= 0:
                    continue
                if kind == OVERDUE_FEE:
                    due, growing = agreed, not back
                elif kind == CHANGE_FEE:
                    due, growing = booked, (not back and today < agreed and not free_days_rule)
                else:
                    due, growing = (item.free_pickup_date if kind == EARLY_FEE else item.free_return_date), False
                fee_lines.append({
                    "kind": kind, "label": FEE_LABELS[kind], "item_id": item.id, "gown": item.gown_name, "days": count, "rate": Decimal(rate),
                    "amount": money(count * rate), "back": back, "growing": growing, "booked": long_day(booked), "agreed": long_day(agreed),
                    "due": long_day(due), "returned": long_day(item.returned_on),
                    # what a free-days line explains: the booked pick-up / return and the free days they are counted from
                    "free_days": free_days_rule,
                    "pickup": long_day(item.rental_date), "return": long_day(item.return_date),
                    "free_from": long_day(getattr(item, "free_pickup_date", None)), "free_until": long_day(getattr(item, "free_return_date", None)),
                    # a later return fee's days run up to the return date, or the day first booked when staff moved it further (that part is a change)
                    "until": long_day(min(item.return_date, booked)) if kind == LATER_FEE else "",
                })
    late_fee = sum((line["amount"] for line in late_lines), ZERO)
    early_fee = sum((line["amount"] for line in fee_lines if line["kind"] == EARLY_FEE), ZERO)
    later_fee = sum((line["amount"] for line in fee_lines if line["kind"] == LATER_FEE), ZERO)
    change_fee = sum((line["amount"] for line in fee_lines if line["kind"] == CHANGE_FEE), ZERO)
    overdue_fee = sum((line["amount"] for line in fee_lines if line["kind"] == OVERDUE_FEE), ZERO)

    deductions = [d for d in reservation.deductions.all() if not d.is_void]
    deductions_total = sum((money(d.amount) for d in deductions), ZERO)

    against = late_fee + deductions_total
    # A free-days booking's BOOKED days are charged from the day it is booked, so they can be there while the deposit is not paid yet: they
    # come out of the deposit once it is paid, so that unpaid part is not asked for a second time as "extra" (only what no deposit covers is).
    booked_fee = sum((line["amount"] for line in fee_lines if line["free_days"] and line["kind"] != OVERDUE_FEE), ZERO)
    covered_when_paid = min(booked_fee, deposit_unpaid)
    overflow = max(against - deposit_paid - covered_when_paid, ZERO)
    extra_owed = max(overflow - extra_in, ZERO)
    all_returned = bool(live) and all(i.stage == ReservationItem.Stage.RETURNED for i in live)
    if cancelled and reservation.pays_over_deposit:
        # A cancelled booking keeps the DEPOSIT; what was paid over it (for days it now will not use) is the customer's and goes back.
        extra_credit = extra_in
        refundable_total = extra_credit
        refund_available = max(refundable_total - refunded, ZERO) if tracked else ZERO
    else:
        # An extra charge paid that is no longer needed is the customer's money: it goes back with the deposit. (An older booking that was
        # cancelled keeps what it has.)
        extra_credit = ZERO if cancelled else max(extra_in - overflow, ZERO)
        refundable_total = max(deposit_paid - against, ZERO) + extra_credit
        refund_available = max(refundable_total - refunded, ZERO) if (tracked and not cancelled and all_returned) else ZERO

    balance_core = deposit_unpaid + rental_unpaid
    balance_due = balance_core + extra_owed
    if not tracked or cancelled:
        balance_core = balance_due = ZERO                      # nothing more is asked of a booking that is not running (or was cancelled)
        extra_owed = ZERO

    # A fee is still moving: an older booking's gown that has one is still out; a free-days booking's only while its gown is out past its return date.
    late_now = any(not line["back"] and (not line["free_days"] or line["overdue_days"] > 0) for line in late_lines)
    overdue_now = any(line["kind"] == OVERDUE_FEE and not line["back"] for line in fee_lines)          # a gown is out past its agreed return date
    # Booked extra days over the deposit that have to come WITH the deposit (a booking that pays them right away, deposit not paid yet). Only
    # the BOOKED days: an overdue fee is asked for as the extra charge it is.
    over_with_deposit = (min(extra_owed, over_deposit(booked_fee, deposit_due))
                         if (reservation.pays_over_deposit and deposit_unpaid > 0) else ZERO)
    if cancelled and tracked:
        state = REFUND if refund_available > 0 else CANCELLED
    elif balance_due > 0:
        state = BALANCE
    elif refund_available > 0:
        state = REFUND
    elif all_returned:
        state = SETTLED
    else:
        state = PAID

    return Statement(
        tracked=tracked, legacy=legacy, cancelled=cancelled,
        rental=rental, deposit=deposit_due, total=rental + deposit_due,
        received=core_in + extra_in, refunded=refunded,
        deposit_paid=deposit_paid, rental_paid=rental_paid, deposit_unpaid=deposit_unpaid, rental_unpaid=rental_unpaid,
        late_rate=Decimal(rate), late_lines=late_lines, late_days_total=sum(line["days"] for line in late_lines), late_fee=late_fee,
        late_now=late_now, overdue_now=overdue_now, fee_lines=fee_lines, change_fee=change_fee, overdue_fee=overdue_fee,
        early_fee=early_fee, later_fee=later_fee, extra_days_fee=early_fee + later_fee,
        early_days_total=sum(line["days"] for line in fee_lines if line["kind"] == EARLY_FEE),
        later_days_total=sum(line["days"] for line in fee_lines if line["kind"] == LATER_FEE),
        change_days_total=sum(line["days"] for line in fee_lines if line["kind"] == CHANGE_FEE),
        overdue_days_total=sum(line["days"] for line in fee_lines if line["kind"] == OVERDUE_FEE),
        total_deduction=late_fee + deductions_total,                 # everything that comes out of the deposit: the date fees and the deductions
        deductions_total=deductions_total, deposit_used=min(deposit_paid, against), overflow=overflow, extra_received=extra_in, extra_credit=extra_credit,
        extra_owed=extra_owed, over_with_deposit=over_with_deposit, refundable_total=refundable_total, refund_available=refund_available,
        pays_over_deposit=reservation.pays_over_deposit,
        all_returned=all_returned, balance_core=balance_core, balance_due=balance_due,
        state=state, state_label=STATE_LABELS[state],
    )


# ---------------------------------------------------------------- the security deposit, in words
def deposit_stage(reservation, statement):
    """Not paid yet / Held / Return due / Returned, from the booking's own money. "Returned" means nothing is left to hand back: it was handed back, or
    the fees and deductions used it up (or staff marked it returned by hand before the Payments page existed)."""
    if reservation.deposit_returned_at:
        return DEPOSIT_RETURNED
    if statement.deposit > 0 and statement.deposit_paid <= 0:
        return DEPOSIT_NOT_PAID
    if not statement.all_returned:
        return DEPOSIT_HELD
    return DEPOSIT_RETURN_DUE if statement.refund_available > 0 else DEPOSIT_RETURNED


def deposit_returned_day(reservation, statement):
    """The day a Returned deposit was returned: the day it was handed back, else the day the last gown came back (it was used up). None otherwise."""
    if deposit_stage(reservation, statement) != DEPOSIT_RETURNED:
        return None
    if reservation.deposit_returned_at:
        return timezone.localtime(reservation.deposit_returned_at).date()
    back = [p.paid_at for p in reservation.payments.all()
            if p.direction == Payment.Direction.OUT and p.purpose == Payment.Purpose.DEPOSIT_RETURN and not p.is_void]
    return timezone.localtime(max(back)).date() if back else reservation.deposit_returned_on


def is_settled(reservation, statement):
    """Nothing is left to move on this booking: every gown back, nothing to pay and nothing to hand back."""
    if reservation.status == Reservation.Status.CANCELLED or not statement.tracked or not statement.all_returned:
        return False
    if reservation.deposit_returned_at:
        return True
    return statement.balance_due <= 0 and statement.refund_available <= 0
