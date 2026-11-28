"""The money boxes on Payments and Security Deposits (asked for 2026-11-28): Rental Income (or Today's Money), Deposits Held and Still to Collect.

ONE calculation for both pages and made here on the server, so the numbers can't differ between pages and a staff member without "See
rental income" is never even sent the earnings:
  Rental Income, kept money, money by method   arabela_admin/dashboard_money.money_between, by the day the money was paid (the same sums
                                               as the Dashboard and Rental History)
  Deposits Held                                now, by the Security Deposits page's own states (views._deposit_totals)
  To give back now                             every refund not handed back yet (what the Payments page's "Refund due" lists)
  Still to Collect                             the unpaid balances of the bookings the chosen dates list (the same days the lists filter by)
"""
from datetime import date, timedelta

from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from reservations import ledger
from reservations.models import Reservation

from . import dashboard_money, rental_report
from .views import _can, _deposit_totals, _focus_dates, _is_admin_staff

# What a chosen date can mean: the day a gown is picked up, comes back or is worn, or the day money was paid or handed back.
KINDS = ("pickup", "return", "event", "paid")
WRONG_ORDER = "The To date can't be before the From date."


def tracked_bookings():
    """Every booking whose money is tracked (see ledger.counts_as_tracked), with everything the statement reads loaded in a few queries."""
    return (
        Reservation.objects.filter(Q(status__in=ledger.LIVE_STATUSES) | Q(status=Reservation.Status.CANCELLED, reviewed_at__isnull=False))
        .select_related("customer__profile")
        .prefetch_related("items", "payments", "deductions", "receipt_records")
    )


def read_dates(params):
    """(from, to, kind, error) of ?from=&to=&by= (?date= : one exact day). A missing or unusable date means "no limit" on that side."""
    def day(name):
        try:
            found = date.fromisoformat((params.get(name) or "").strip())
        except ValueError:
            return None
        return found if 2000 <= found.year <= 2100 else None

    start, end, single = day("from"), day("to"), day("date")
    if single and not start and not end:
        start = end = single
    kind = params.get("by") or params.get("kind") or "return"
    if kind not in KINDS:
        kind = "return"
    return start, end, kind, (WRONG_ORDER if start and end and end < start else "")


def period_words(start, end, today):
    """'All dates', 'Today', 'Nov 20, 2026', 'November 2026', 'Nov 1 to Nov 15, 2026', 'From Nov 1, 2026'."""
    if not start and not end:
        return "All dates"
    if start and end:
        if start == end:
            return "Today" if start == today else rental_report.short_day(start)
        month_end = (start.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        if start.day == 1 and end == month_end:
            return f"{start:%B %Y}"
        return rental_report.span_text(start, end)
    return f"From {rental_report.short_day(start)}" if start else f"Until {rental_report.short_day(end)}"


def booking_days(reservation):
    """{kind: sorted ISO days} the lists and "Still to Collect" find a booking by."""
    marks = _focus_dates(reservation, list(reservation.items.all()))
    return {"pickup": marks["datesPickup"], "return": marks["datesReturn"], "event": marks["datesEvent"],
            "paid": dashboard_money.payment_days(reservation)}


def in_dates(days, start, end):
    """Whether any of these ISO days is inside the chosen dates (no dates chosen: every booking is)."""
    if not start and not end:
        return True
    low, high = start.isoformat() if start else "", end.isoformat() if end else "9999"
    return any(low <= day <= high for day in days)


def owing_and_giveback(start, end, kind, today):
    """(still to collect, from how many bookings, to give back now, to how many bookings)."""
    owing = give = ledger.ZERO
    owing_bookings = give_bookings = 0
    for reservation in tracked_bookings():
        statement = ledger.statement(reservation, today)
        if not statement.tracked:
            continue
        if statement.refund_available > 0:
            give += statement.refund_available
            give_bookings += 1
        if statement.balance_due > 0 and in_dates(booking_days(reservation)[kind], start, end):
            owing += statement.balance_due
            owing_bookings += 1
    return owing, owing_bookings, give, give_bookings


def _text(value):
    return f"{ledger.money(value):,.2f}"


def boxes_context(request):
    """Everything the three boxes show for the dates in the address."""
    today = timezone.localdate()
    start, end, kind, error = read_dates(request.GET)
    box = {"error": error, "see_income": _can(request, "see_income"), "tag": period_words(start, end, today), "dated": bool(start or end)}
    if error:
        return box
    money = dashboard_money.money_between(start, end, today)
    held = _deposit_totals(today)
    owing, owing_bookings, give, give_bookings = owing_and_giveback(start, end, kind, today)
    box.update({
        "held": _text(held["held"]), "held_bookings": held["held_bookings"], "give_back": _text(give), "give_back_bookings": give_bookings,
        "collected": _text(money["collected"]), "returned_deposit": _text(money["returned_deposit"]),
        "returned_extra": _text(money["returned_extra"]), "has_returned_extra": money["returned_extra"] != 0,
        "owing": _text(owing), "owing_bookings": owing_bookings,
    })
    if box["see_income"]:
        box.update({
            "income": _text(money["income"]), "income_bookings": sum(1 for value in money["by_booking"].values() if value > 0),
            "kept": _text(money["kept"]), "kept_cancelled": _text(money["kept_cancelled"]), "rental_back": money["rental_back"] != 0,
            "methods": dashboard_money.method_lines(money["by_method"]),
        })
    else:
        box["today_money"] = dashboard_money.today_money(today)
    return box


@never_cache
@require_http_methods(["GET"])
def money_boxes_view(request):
    """The boxes alone, for the page to put in place when the dates change and every little while."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    return render(request, "arabela_admin/partials/money_boxes_body.html", {"money": boxes_context(request)})
