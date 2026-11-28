"""What the Dashboard says about money and today's work. Everything is READ from the payments ledger and the bookings (nothing is stored), so it can never
disagree with the pages it links to.

  latest_transactions()   the last few movements of money (received, extra charge, deposit handed back), each with how it was paid, who paid, the rental and the amount
  shop_today(today)       three small counts: bookings made today, pick-ups in the next 2 days that still owe money, and what needs a look (overdue gowns, online payments)
  alerts(shop_today)      the warning line under the greeting: only what needs a person (overdue gowns, pick-ups that still owe money, online payments)
  month_money(today)      Rental Income: the gown rental paid this month, and the fees and deductions the shop kept (never the security deposits)
  greeting(hour)          "Good morning" / "Good afternoon" / "Good evening" for an hour of the shop's clock
"""
from datetime import timedelta

from django.db.models import Prefetch
from django.urls import reverse
from django.utils import timezone

from reservations import cancellation, ledger
from reservations.models import Payment, PaymentAttempt, Reservation, ReservationItem, ReservationStatusEvent

LATEST = 5
PICKUP_DAYS = 2             # "pick-ups with a balance" looks at today and the next 2 days
LIVE_STATUSES = (Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE, Reservation.Status.OVERDUE, Reservation.Status.RETURNED)
CORE_PURPOSES = (Payment.Purpose.DEPOSIT, Payment.Purpose.FULL, Payment.Purpose.BALANCE)

M = Payment.Method
# "Money by method": every way a payment can be made, under the name people count it by ("Other": an old booking's money, or an unknown online way).
METHOD_GROUPS = ("Cash", "GCash", "Maya", "QR Ph", "Other")
_METHOD_GROUP = {M.CASH: "Cash", M.GCASH: "GCash", M.ONLINE_GCASH: "GCash", M.PM_GCASH: "GCash", M.PM_MAYA: "Maya", M.PM_QRPH: "QR Ph"}
CANCEL_LABELS = (cancellation.CUSTOMER_CANCEL_LABEL, cancellation.SHOP_CANCEL_LABEL)     # the history line a cancelled booking carries


def method_group(method):
    return _METHOD_GROUP.get(method, "Other")


def returned_split(statement):
    """(security deposit, extra money): of the money handed back besides gown rental, how much was the deposit and how much was an extra charge
    going back (counted first: an extra charge that is no longer owed is given back before the deposit is touched)."""
    back = max(statement.refunded - statement.rental_returned, ledger.ZERO)
    extra = min(back, statement.extra_credit)
    return back - extra, extra


def cancel_events():
    """The prefetch cancelled_on() reads."""
    return Prefetch("status_events", queryset=ReservationStatusEvent.objects.filter(label__in=CANCEL_LABELS).order_by("occurred_at", "id"),
                    to_attr="cancel_lines")


def cancelled_on(reservation):
    """The day a cancelled booking was cancelled (its history line; an old one without it: the day it last changed)."""
    lines = getattr(reservation, "cancel_lines", None)
    if lines is None:
        lines = list(reservation.status_events.filter(label__in=CANCEL_LABELS).order_by("occurred_at", "id"))
    return timezone.localtime(lines[0].occurred_at if lines else reservation.updated_at).date()


def payment_days(reservation):
    """The days money moved on this booking (the same payments money_between adds up), as sorted ISO days."""
    return sorted({timezone.localtime(row["paid_at"]).date().isoformat() for row in _money_rows(reservation)})


# How a payment was made, as a name and a colour class (the page's CSS knows gcash / maya / qrph / cash / other). Names only, no logos.
_METHOD_BADGE = {
    M.CASH: ("Cash", "cash"),
    M.GCASH: ("GCash", "gcash"), M.ONLINE_GCASH: ("GCash", "gcash"), M.PM_GCASH: ("GCash", "gcash"),
    M.PM_MAYA: ("Maya", "maya"), M.PM_QRPH: ("QR Ph", "qrph"), M.PM_OTHER: ("Online", "other"), M.OLD: ("Earlier", "other"),
}


def _kind(payment):
    """(what happened, the purpose in small words)."""
    if payment.direction == Payment.Direction.OUT:
        return "Refund" if payment.is_over_return else "Deposit returned", ""
    if payment.purpose == Payment.Purpose.EXTRA:
        return "Extra charge received", ""
    return "Payment received", payment.get_purpose_display()


def _rental(reservation):
    """(the gown(s) in words, the rental dates in words) of one booking, from its prefetched gowns; a gown the shop cancelled is left out unless it is all there is."""
    items = list(reservation.items.all())
    live = [i for i in items if i.stage != ReservationItem.Stage.CANCELLED] or items
    if not live:
        return "", ""
    gown = live[0].gown_name + (f" + {len(live) - 1} more" if len(live) > 1 else "")
    first, last = min(i.rental_date for i in live), max(i.return_date for i in live)
    if first == last:
        dates = f"{first:%b} {first.day}, {first.year}"
    elif first.year == last.year:
        dates = f"{first:%b} {first.day} - {last:%b} {last.day}, {last.year}"
    else:
        dates = f"{first:%b} {first.day}, {first.year} - {last:%b} {last.day}, {last.year}"
    return gown, dates


def latest_transactions(limit=LATEST):
    """The last `limit` payments the shop recorded (a voided one stays in the list, marked Void), newest first. Rows from before the Payments page
    (is_legacy) are left out: they have no receipt and no real date of their own."""
    from django.utils import timezone

    rows = (
        Payment.objects.filter(is_legacy=False)
        .select_related("reservation__customer__profile")
        .prefetch_related("reservation__items")
        .order_by("-paid_at", "-id")[:limit]
    )
    out = []
    for p in rows:
        reservation = p.reservation
        when = timezone.localtime(p.paid_at)
        what, purpose = _kind(p)
        method, method_kind = _METHOD_BADGE.get(p.method, (p.get_method_display(), "other"))
        gown, dates = _rental(reservation)
        is_out = p.direction == Payment.Direction.OUT
        out.append({
            "id": p.pk, "booking_id": reservation.pk, "code": reservation.reference_code, "customer": reservation.display_customer_name,
            "day": f"{when:%b} {when.day}, {when.year}", "time": f"{when:%I:%M %p}".lstrip("0"),
            "what": what, "purpose": purpose, "method": method, "method_kind": method_kind,
            "gown": gown, "dates": dates,
            "amount": f"{ledger.money(p.amount):,.2f}", "sign": "-" if is_out else "+", "is_out": is_out,
            "void": p.is_void, "status": "Void" if p.is_void else "Success",
            # A practice or test payment never moved real money (the online payment writes it in the note); it is marked so nobody counts it.
            "test": "TEST PAYMENT" in (p.note or ""),
            "receipt": p.receipt_number or "",
        })
    return out


def shop_today(today):
    """{"new_bookings", "pickups_owing", "late_gowns", "online_attention", "needs_look"}."""
    new_bookings = Reservation.objects.filter(status__in=LIVE_STATUSES, created_at__date=today).count()

    # Bookings with a gown still waiting to go out in the next few days whose balance is not paid yet (collected at the counter).
    soon = today + timedelta(days=PICKUP_DAYS)
    waiting = (
        Reservation.objects.filter(
            status__in=(Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE, Reservation.Status.OVERDUE),
            items__stage=ReservationItem.Stage.PICKUP, items__rental_date__gte=today, items__rental_date__lte=soon,
        )
        .distinct()
        .prefetch_related("items", "payments", "deductions", "receipt_records")
    )
    pickups_owing = sum(1 for r in waiting if ledger.statement(r, today).balance_due > 0)

    late_gowns = ReservationItem.objects.filter(
        reservation__status__in=(Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE, Reservation.Status.OVERDUE),
        stage__in=ledger.OUT_STAGES, return_date__lt=today,
    ).count()
    online_attention = PaymentAttempt.objects.filter(PaymentAttempt.attention_filter()).count()
    return {
        "new_bookings": new_bookings, "pickups_owing": pickups_owing, "late_gowns": late_gowns, "online_attention": online_attention,
        "needs_look": late_gowns + online_attention,
    }


def alerts(shop_today):
    """The warning line under the greeting, as [{"text", "url"}]: one piece per thing that needs a person, nothing when nothing does. Each piece opens the
    page with the details (the same pages the old "Today at the shop" boxes opened)."""
    if not shop_today:
        return []
    out = []
    late = shop_today.get("late_gowns") or 0
    if late:
        out.append({"text": f"{late} gown{'s are' if late != 1 else ' is'} overdue", "url": reverse("arabela_admin:gown_monitoring") + "?tab=return"})
    owing = shop_today.get("pickups_owing") or 0
    if owing:
        out.append({"text": f"{owing} pick-up{'s' if owing != 1 else ''} in the next {PICKUP_DAYS} days still {'have' if owing != 1 else 'has'} a balance",
                    "url": reverse("arabela_admin:payments")})
    online = shop_today.get("online_attention") or 0
    if online:
        out.append({"text": f"{online} online payment{'s need' if online != 1 else ' needs'} a look", "url": reverse("arabela_admin:payment_online")})
    return out


def greeting(hour):
    """For an hour (0-23) of the shop's own clock: "Good morning" from 5:00, "Good afternoon" from 12:00, "Good evening" from 18:00 (and through the night).
    The page's own script uses the same three steps, so the words change by themselves while the page stays open."""
    if 5 <= hour < 12:
        return "Good morning"
    if 12 <= hour < 18:
        return "Good afternoon"
    return "Good evening"


def _money_rows(reservation):
    """The booking's payments, oldest first: the same money ledger.statement adds up (the recorded payments that are not voided; for an old booking that
    has none, the ones ledger.legacy_rows derives from what it carries). Each is {"direction", "purpose", "amount", "paid_at", "method",
    "payment" (the stored Payment, or None for an old one), "over" (the part of a deposit payment that paid booked days over the deposit),
    "rental_back" (money handed back: the part of it that is gown rental)}."""
    stored = sorted(reservation.payments.all(), key=lambda p: (p.paid_at, p.pk))
    if stored:
        return [{"direction": p.direction, "purpose": p.purpose, "amount": ledger.money(p.amount), "paid_at": p.paid_at, "method": p.method, "payment": p,
                 "over": ledger.money(p.extra_part) if p.direction == Payment.Direction.IN and p.purpose in CORE_PURPOSES else ledger.ZERO,
                 "rental_back": ledger.money(p.rental_part) if p.direction == Payment.Direction.OUT else ledger.ZERO}
                for p in stored if not p.is_void]
    rows = ledger.legacy_rows(reservation)
    return [{"direction": r["direction"], "purpose": r["purpose"], "amount": ledger.money(r["amount"]), "paid_at": r["paid_at"], "method": r["method"], "payment": None,
             "over": ledger.ZERO, "rental_back": ledger.ZERO}
            for r in sorted(rows, key=lambda r: r["paid_at"])]


def money_between(start=None, end=None, today=None):
    """The money of every tracked booking between two days (both counted; None = no limit), by the day it was PAID. The Dashboard's month and the
    Reservation Report both come from here, so they can never disagree. The security deposits are never income: a deposit is the customer's money and
    goes back to them, until a fee or a deduction keeps part of it.

      income      the gown-rental part of every payment received. A payment covers the deposit first and then the rental (the rule of the Payments
                  page, reservations/ledger.py), so a "Paid in full" counts only its rental part and a deposit-only payment counts nothing.
                  Rental given back (a cancelled booking or gown) comes off on the day it is returned.
      by_booking  {booking id: its income}, for the bookings that income came from (or was given back to).
      rental_back the rental given back in these days.
      kept        the fees and deductions the shop kept: what they took out of the deposit of a booking whose last gown came back in these days (only
                  then are the fees final), plus an extra charge paid on top of a deposit in these days (less any part the fees no longer ask for,
                  which goes back to the customer: ledger.extra_credit).
      collected   the security-deposit part of the payments received.
      returned    the money handed back besides rental, split into returned_deposit and returned_extra (returned_split).
      kept_cancelled  the security deposits kept from bookings cancelled in these days (not given back by the owner).
      by_method   {method group: money received less money handed back, deposits included}: what to count in the cash drawer and the e-wallets.
      payments    every payment in these days, oldest first: {"reservation", "row" (see _money_rows), "day", "rental", "deposit", "extra", "other"}
                  ("other" = money paid beyond what the booking asks, e.g. for a gown cancelled afterwards).

    Only bookings whose money is tracked count (approved, running, finished, or cancelled after approval), like everywhere else. Amounts are Decimals."""
    today = today or timezone.localdate()

    def inside(day):
        return day is not None and (start is None or day >= start) and (end is None or day <= end)

    zero = ledger.ZERO
    income = kept = collected = returned = rental_back = zero
    returned_deposit = returned_extra = kept_cancelled = zero
    by_method = {name: zero for name in METHOD_GROUPS}
    by_booking, payments = {}, []
    candidates = (
        Reservation.objects.filter(status__in=LIVE_STATUSES + (Reservation.Status.CANCELLED,))
        .select_related("customer__profile")
        .prefetch_related("items", "payments", "deductions", "receipt_records", cancel_events())
    )
    for reservation in candidates:
        if not ledger.counts_as_tracked(reservation):
            continue
        deposit_due, rental_due = ledger.money(reservation.security_deposit), ledger.rental_due(reservation)
        received = earned = zero
        rows = _money_rows(reservation)
        statement = ledger.statement(reservation, today)
        # The part of the extra charges that goes back with the deposit (dates moved back after it was paid), taken off the latest ones first.
        give_back, not_kept = statement.extra_credit, {}
        for index in reversed(range(len(rows))):
            paid_extra = rows[index]["amount"] if rows[index]["purpose"] == Payment.Purpose.EXTRA else rows[index]["over"]
            if give_back > 0 and rows[index]["direction"] == Payment.Direction.IN and paid_extra > 0:
                not_kept[index] = min(paid_extra, give_back)
                give_back -= not_kept[index]
        extra_to_return = statement.extra_credit          # an extra charge going back is counted before the deposit (returned_split)
        for index, row in enumerate(rows):
            day = timezone.localtime(row["paid_at"]).date()
            amount = row["amount"]
            parts = {"rental": zero, "deposit": zero, "extra": zero, "other": zero}
            if inside(day):
                sign = 1 if row["direction"] == Payment.Direction.IN else -1
                by_method[method_group(row["method"])] += sign * amount
            if row["direction"] != Payment.Direction.IN:
                back = row["rental_back"]
                parts["rental"] = -back
                extra_part = min(amount - back, extra_to_return)
                extra_to_return -= extra_part
                if inside(day):
                    returned += amount - back
                    returned_extra += extra_part
                    returned_deposit += amount - back - extra_part
                    rental_back += back
                    earned -= back
            elif row["purpose"] == Payment.Purpose.EXTRA:
                parts["extra"] = amount
                if inside(day):
                    kept += amount - not_kept.get(index, zero)
            elif row["purpose"] in CORE_PURPOSES:
                deposit_before = min(received, deposit_due)
                rental_before = min(max(received - deposit_due, zero), rental_due)
                core = amount - row["over"]                  # without the booked days over the deposit paid in the same payment
                received += core
                parts["deposit"] = min(received, deposit_due) - deposit_before
                parts["rental"] = min(max(received - deposit_due, zero), rental_due) - rental_before
                parts["extra"] = row["over"]
                parts["other"] = core - parts["deposit"] - parts["rental"]
                if inside(day):
                    earned += parts["rental"]
                    collected += parts["deposit"]
                    kept += row["over"] - not_kept.get(index, zero)
            else:
                parts["other"] = amount
            if inside(day):
                payments.append({"reservation": reservation, "row": row, "day": day, **parts})
        if earned != 0:
            income += earned
            by_booking[reservation.pk] = earned
        if statement.all_returned and statement.deposit_used > 0:
            back = [i.returned_on for i in reservation.items.all() if i.stage == ReservationItem.Stage.RETURNED and i.returned_on]
            if back and inside(max(back)):
                kept += statement.deposit_used
        # A cancelled booking keeps its security deposit (unless the owner gives it back): the shop's money from the day it was cancelled.
        if statement.cancelled and not statement.deposit_back_too and inside(cancelled_on(reservation)):
            kept_cancelled += max(statement.deposit_paid - returned_split(statement)[0], zero)
    payments.sort(key=lambda p: (p["row"]["paid_at"], p["row"]["payment"].pk if p["row"]["payment"] is not None else 0, p["reservation"].pk))
    return {"income": income, "by_booking": by_booking, "kept": kept, "collected": collected, "returned": returned, "rental_back": rental_back,
            "returned_deposit": returned_deposit, "returned_extra": returned_extra, "kept_cancelled": kept_cancelled, "by_method": by_method,
            "payments": payments}


def method_lines(by_method):
    """[(name, "1,500.00")] for the boxes: Cash and GCash always, the others when money moved that way."""
    return [(name, f"{by_method[name]:,.2f}") for name in METHOD_GROUPS if name in ("Cash", "GCash", "Maya", "QR Ph") or by_method[name]]


def today_money(today):
    """"Today's Money" (what staff without "See rental income" see instead): all money in today less money handed back, by method."""
    money = money_between(today, today, today)
    net = sum(money["by_method"].values(), ledger.ZERO)
    return {"net": f"{net:,.2f}", "methods": method_lines(money["by_method"])}


def month_money(today):
    """The Dashboard's Rental Income: money_between over the calendar month of `today`, as text ("84,500.00")."""
    first = today.replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    money = money_between(first, last, today)
    paid = sum(1 for value in money["by_booking"].values() if value > 0)
    return {"income": f"{money['income']:,.2f}", "bookings": paid, "kept": f"{money['kept']:,.2f}",
            "kept_cancelled": f"{money['kept_cancelled']:,.2f}"}
