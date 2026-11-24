"""What the Dashboard says about money and today's work. Everything is READ from the payments ledger and the bookings (nothing is stored), so it can never
disagree with the pages it links to.

  latest_transactions()   the last few movements of money (received, late fee, deposit handed back), each with how it was paid, who paid, the rental and the amount
  shop_today(today)       three small counts: bookings made today, pick-ups in the next 2 days that still owe money, and what needs a look (late gowns, online payments)
"""
from datetime import timedelta

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
        return "Late fee received", ""
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
