"""What a booking OWES, what it has PAID, its DATE CHANGE and OVERDUE FEES and the DEPOSIT it can give back -- all worked out here, never typed by anyone.

The one rule set the Payments page, the printed receipts, the customer's booking page and the walk-in form all read:

  * Total to pay = the gown rental + the security deposit (the deposit the booking was made with).
  * Money received covers the DEPOSIT first, then the rental. (The deposit is the shop's protection and it is never paid in halves, see
    reservations/payments.py: a deposit payment is exactly the deposit, or the booking is paid in full.)
  * The fee for days past the booked return date (`late_fee` in the code) = the booking's fee per day x those days, counted from the return
    date the customer ORIGINALLY booked (a gown that is still out counts up to today -- "so far"; a gown that is back counts up to the day
    it came back). It is not stored: it is worked out every time, so moving a return date later never makes it disappear.
    People read it in two parts, the same money: the DATE CHANGE FEE (the days the shop moved the return date) and the OVERDUE FEE (the days
    past the date agreed now, while the booking shows as Overdue).
  * The fees and any manual deductions come out of the deposit. Refundable = deposit paid - fees - deductions. If they are
    MORE than the deposit, the extra is what the customer still owes ("extra owed").
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

# The two names of the fee (the same money per day): see agreed_return() and overdue_days().
CHANGE_FEE, OVERDUE_FEE = "change", "overdue"
FEE_LABELS = {CHANGE_FEE: "Date change fee", OVERDUE_FEE: "Overdue fee"}


def long_day(value):
    """'Nov 29, 2026': a day the way a receipt writes it, or '' when there is none."""
    return f"{value:%b} {value.day}, {value.year}" if value else ""


def money(value):
    """A Decimal with exactly two places."""
    return Decimal(value or 0).quantize(CENT, rounding=ROUND_HALF_UP)


def peso(value, *, sign=True):
    """'₱3,500.00' -- how receipts and the Payments page write an amount."""
    return f"{'₱' if sign else ''}{money(value):,.2f}"


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
def late_days(item, today=None):
    """How many days this gown is late (or was). 0 = on time or not applicable.

    Counted from the return date the customer ORIGINALLY booked (original_return_date, else the current one). A gown that has not been
    picked up, or that the shop cancelled, can't be late. One that is out counts up to `today` ("late fee so far"); one that is back counts
    up to the day it came back (returned_on). Unlike the Overdue label on Active Reservations -- which waits for the CURRENT return date
    -- the fee starts the day after the original one, so a customer who asks for the gown longer pays for the extra days."""
    today = today or timezone.localdate()
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
    event date never change it). A cancelled gown, or one with no return date, has none."""
    today = today or timezone.localdate()
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
    than the one originally booked. The days between the two are the Date change fee; the days after it are overdue."""
    booked = item.original_return_date or item.return_date
    if booked is None or item.return_date is None:
        return booked
    return max(booked, item.return_date)


def overdue_days(item, today=None):
    """Of late_days(item), the days PAST the agreed return date: the gown is (or was) overdue -- the Overdue fee, and the "N days overdue" people read.
    The rest of late_days are days the return date was moved: the Date change fee."""
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
        rows = [{"direction": p.direction, "purpose": p.purpose, "amount": money(p.amount)} for p in stored if not p.is_void]
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
        days = late_days(item, today)
        if days > 0 and rate > 0:
            back = item.stage == ReservationItem.Stage.RETURNED
            overdue = overdue_days(item, today)
            booked, agreed = item.original_return_date or item.return_date, agreed_return(item)
            late_lines.append({
                "item_id": item.id, "gown": item.gown_name, "days": days, "rate": Decimal(rate), "amount": money(days * rate), "back": back,
                # the return date the fee is counted from (the ORIGINAL one) and the day it really came back: what a receipt says to explain the days
                "due": long_day(booked), "returned": long_day(item.returned_on), "change_days": days - overdue, "overdue_days": overdue,
            })
            # The same days under the names people read: moved (booked -> agreed) first, then overdue (past the agreed date). "growing": another day adds to it.
            for kind, count, due, growing in ((CHANGE_FEE, days - overdue, booked, not back and today < agreed), (OVERDUE_FEE, overdue, agreed, not back)):
                if count > 0:
                    fee_lines.append({
                        "kind": kind, "label": FEE_LABELS[kind], "item_id": item.id, "gown": item.gown_name, "days": count, "rate": Decimal(rate),
                        "amount": money(count * rate), "back": back, "growing": growing, "booked": long_day(booked), "agreed": long_day(agreed),
                        "due": long_day(due), "returned": long_day(item.returned_on),
                    })
    late_fee = sum((line["amount"] for line in late_lines), ZERO)
    change_fee = sum((line["amount"] for line in fee_lines if line["kind"] == CHANGE_FEE), ZERO)
    overdue_fee = sum((line["amount"] for line in fee_lines if line["kind"] == OVERDUE_FEE), ZERO)

    deductions = [d for d in reservation.deductions.all() if not d.is_void]
    deductions_total = sum((money(d.amount) for d in deductions), ZERO)

    against = late_fee + deductions_total
    overflow = max(against - deposit_paid, ZERO)
    extra_owed = max(overflow - extra_in, ZERO)
    refundable_total = max(deposit_paid - against, ZERO)
    all_returned = bool(live) and all(i.stage == ReservationItem.Stage.RETURNED for i in live)
    refund_available = max(refundable_total - refunded, ZERO) if (tracked and not cancelled and all_returned) else ZERO

    balance_core = deposit_unpaid + rental_unpaid
    balance_due = balance_core + extra_owed
    if not tracked or cancelled:
        balance_core = balance_due = ZERO                      # nothing more is asked of a booking that is not running (or was cancelled)
        extra_owed = ZERO

    late_now = any(not line["back"] for line in late_lines)          # a fee is still moving: a gown that has one is still out
    overdue_now = any(line["kind"] == OVERDUE_FEE and not line["back"] for line in fee_lines)          # a gown is out past its agreed return date
    if cancelled and tracked:
        state = CANCELLED
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
        change_days_total=sum(line["days"] for line in fee_lines if line["kind"] == CHANGE_FEE),
        overdue_days_total=sum(line["days"] for line in fee_lines if line["kind"] == OVERDUE_FEE),
        deductions_total=deductions_total, deposit_used=min(deposit_paid, against), overflow=overflow, extra_received=extra_in,
        extra_owed=extra_owed, refundable_total=refundable_total, refund_available=refund_available,
        all_returned=all_returned, balance_core=balance_core, balance_due=balance_due,
        state=state, state_label=STATE_LABELS[state],
    )
