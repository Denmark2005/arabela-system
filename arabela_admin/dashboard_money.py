"""What the Dashboard says about money and today's work. Everything is READ from the payments ledger and the bookings (nothing is stored), so it can never
disagree with the pages it links to.

  latest_transactions()   the last few movements of money (received, extra charge, deposit handed back), each with how it was paid, who paid, the rental and the amount
  shop_today(today)       three small counts: bookings made today, pick-ups in the next 2 days that still owe money, and what needs a look (overdue gowns, online payments)
  alerts(shop_today)      the warning line under the greeting: only what needs a person (overdue gowns, pick-ups that still owe money, online payments)
  month_money(today)      Rental Income: the gown rental paid this month, and the fees and deductions the shop kept (never the security deposits)
  greeting(hour)          "Good morning" / "Good afternoon" / "Good evening" for an hour of the shop's clock
"""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from reservations import ledger
from reservations.models import Payment, PaymentAttempt, Reservation, ReservationItem

LATEST = 5
PICKUP_DAYS = 2             # "pick-ups with a balance" looks at today and the next 2 days
LIVE_STATUSES = (Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE, Reservation.Status.OVERDUE, Reservation.Status.RETURNED)

M = Payment.Method
# How a payment was made, as a name and a colour class (the page's CSS knows gcash / maya / qrph / cash / other). Names only, no logos.
_METHOD_BADGE = {
    M.CASH: ("Cash", "cash"),
    M.GCASH: ("GCash", "gcash"), M.ONLINE_GCASH: ("GCash", "gcash"), M.PM_GCASH: ("GCash", "gcash"),
    M.PM_MAYA: ("Maya", "maya"), M.PM_QRPH: ("QR Ph", "qrph"), M.PM_OTHER: ("Online", "other"), M.OLD: ("Earlier", "other"),
}


def _kind(payment):
    """(what happened, the purpose in small words)."""
    if payment.direction == Payment.Direction.OUT:
        return "Deposit returned", ""
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


CORE_PURPOSES = (Payment.Purpose.DEPOSIT, Payment.Purpose.FULL, Payment.Purpose.BALANCE)


def _money_rows(reservation):
    """The booking's payments as (direction, purpose, amount, paid_at), oldest first: the same rows ledger.statement adds up (the recorded payments that
    are not voided; for an old booking that has none, the ones ledger.legacy_rows derives from what it carries)."""
    stored = sorted(reservation.payments.all(), key=lambda p: (p.paid_at, p.pk))
    if stored:
        return [(p.direction, p.purpose, ledger.money(p.amount), p.paid_at) for p in stored if not p.is_void]
    rows = ledger.legacy_rows(reservation)
    return [(r["direction"], r["purpose"], ledger.money(r["amount"]), r["paid_at"]) for r in sorted(rows, key=lambda r: r["paid_at"])]


def month_money(today):
    """What the shop EARNED in the calendar month of `today`. The security deposits are never in it: a deposit is the customer's money and goes back to
    them, until a fee or a deduction keeps part of it.

      income     the gown-rental part of every payment received this month. A payment covers the deposit first and then the rental (the rule of the
                 Payments page, reservations/ledger.py), so a "Paid in full" counts only its rental part and a deposit-only payment counts nothing.
      bookings   how many bookings that income came from.
      kept       the fees and deductions the shop kept this month: what they took out of the deposit of a booking whose last gown came back this month
                 (only then are the fees final), plus an extra charge paid on top of a deposit this month.

    Only bookings whose money is tracked count (approved, running, finished, or cancelled after approval), like everywhere else. Amounts are text ("84,500.00")."""
    first = today.replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)

    def in_month(when):
        return when is not None and first <= timezone.localtime(when).date() < following

    income = kept = ledger.ZERO
    bookings = 0
    candidates = (
        Reservation.objects.filter(status__in=LIVE_STATUSES + (Reservation.Status.CANCELLED,))
        .prefetch_related("items", "payments", "deductions", "receipt_records")
    )
    for reservation in candidates:
        if not ledger.counts_as_tracked(reservation):
            continue
        deposit_due, rental_due = ledger.money(reservation.security_deposit), ledger.rental_due(reservation)
        received = earned = ledger.ZERO
        for direction, purpose, amount, when in _money_rows(reservation):
            if direction != Payment.Direction.IN:
                continue
            if purpose == Payment.Purpose.EXTRA:
                if in_month(when):
                    kept += amount
                continue
            if purpose not in CORE_PURPOSES:
                continue
            rental_before = min(max(received - deposit_due, ledger.ZERO), rental_due)
            received += amount
            rental_after = min(max(received - deposit_due, ledger.ZERO), rental_due)
            if in_month(when):
                earned += rental_after - rental_before
        if earned > 0:
            income += earned
            bookings += 1
        statement = ledger.statement(reservation, today)
        if statement.all_returned and statement.deposit_used > 0:
            back = [i.returned_on for i in reservation.items.all() if i.stage == ReservationItem.Stage.RETURNED and i.returned_on]
            if back and first <= max(back) < following:
                kept += statement.deposit_used
    return {"income": f"{income:,.2f}", "bookings": bookings, "kept": f"{kept:,.2f}"}
