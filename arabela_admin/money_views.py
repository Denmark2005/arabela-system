"""The money on the Payments page (asked for 2026-11-28): the three boxes, and the buttons that sort the one list by balance, refund or a
kind of money, each with its total.

ONE calculation, made here on the server and sent with the page, so choosing a button or a date needs no further request: for every booking,
what it holds, owes and gets back now, and what moved on which day (row_money). The page only adds up what it was sent.
  money that moved        arabela_admin/dashboard_money.money_between, by the day it was paid (the same sums as the Dashboard and Rental History)
  deposit held now        the Security Deposits page's own states (views._deposit_state)
  to give back, owed      the booking's own statement (reservations/ledger.py), as the Payments page and the receipts read it
A staff member without "See rental income" is never sent the earnings: only deposit money, balances, refunds and today's money by method."""
from datetime import date

from django.db.models import Q

from reservations import ledger
from reservations.models import Payment, Reservation, ReservationItem

from . import dashboard_money
from .views import _deposit_state, _focus_dates, _is_deposit_booking

# What a chosen date can mean: the day a gown is picked up, comes back or is worn, or the day money was paid or handed back.
KINDS = ("pickup", "return", "event", "paid")
WRONG_ORDER = "The To date can't be before the From date."


def tracked_bookings():
    """Every booking whose money is tracked (see ledger.counts_as_tracked), with everything the sums read loaded in a few queries."""
    return (
        Reservation.objects.filter(Q(status__in=ledger.LIVE_STATUSES) | Q(status=Reservation.Status.CANCELLED, reviewed_at__isnull=False))
        .select_related("customer__profile")
        .prefetch_related("items", "payments", "deductions", "receipt_records", dashboard_money.cancel_events())
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
    """(still to collect, from how many bookings, to give back now, to how many bookings): Rental History's own figures."""
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


def _cents(value):
    return int(ledger.money(value) * 100)


def row_money(reservations, today, see_income):
    """{booking id: {"h": deposit held now, "g": to give back now, "o": still owed, "e": [[day, kind, amount], ...]}}, amounts in centavos.

    The kinds of "e" (money that moved, by the day): "d" deposit collected, "r" deposit returned, "x" extra charge returned; for who may see
    the income also "i" rental income, "k" fees and deductions kept, "c" kept from a cancellation and "m:<method>" money in or out; for the
    others only "t:<method>", today's money in or out."""
    money = dashboard_money.money_between(None, None, today, reservations=reservations)
    sums = {}

    def add(reservation, day, kind, value):
        if value:
            key = (reservation.pk, day.isoformat(), kind)
            sums[key] = sums.get(key, 0) + _cents(value)

    for entry in money["payments"]:
        reservation, day, paid = entry["reservation"], entry["day"], entry["row"]
        method = dashboard_money.method_group(paid["method"])
        signed = paid["amount"] if paid["direction"] == Payment.Direction.IN else -paid["amount"]
        if see_income:
            add(reservation, day, "i", entry["rental"])
            add(reservation, day, "k", entry["kept"])
            add(reservation, day, f"m:{method}", signed)
        elif day == today:
            add(reservation, day, f"t:{method}", signed)
        add(reservation, day, "d", entry["deposit"])
        add(reservation, day, "r", entry["back_deposit"])
        add(reservation, day, "x", entry["back_extra"])
    if see_income:
        for entry in money["deposit_kept"]:
            add(entry["reservation"], entry["day"], "k", entry["amount"])
        for entry in money["cancel_kept"]:
            add(entry["reservation"], entry["day"], "c", entry["amount"])

    out = {}
    for reservation in reservations:
        statement = ledger.statement(reservation, today)
        held = ledger.ZERO
        if _is_deposit_booking(reservation):
            live = [i for i in reservation.items.all() if i.stage != ReservationItem.Stage.CANCELLED]
            held = _deposit_state(reservation, live, today)["deposit_held"]
        out[reservation.pk] = {"h": _cents(held), "g": _cents(statement.refund_available), "o": _cents(statement.balance_due), "e": []}
    for (pk, day, kind), amount in sorted(sums.items()):
        if amount and pk in out:
            out[pk]["e"].append([day, kind, amount])
    return out


def _text(cents):
    return f"{cents / 100:,.2f}"


def boxes(amounts, days, params, today, see_income):
    """What the three boxes (and the money choices) add up to for the dates in the address, from row_money's `amounts` and every booking's
    booking_days (`days`). The page's script adds the same numbers up again when the dates are changed."""
    start, end, kind, error = read_dates(params)
    box = {"error": error, "see_income": see_income,
           "has_other": any(e[1] in ("m:Other", "t:Other") for row in amounts.values() for e in row["e"]),
           "has_extra": any(e[1] == "x" for row in amounts.values() for e in row["e"])}
    if error:
        return box
    low, high = start.isoformat() if start else "", end.isoformat() if end else "9999"
    totals, earners, todays = {}, 0, {}
    for row in amounts.values():
        income = 0
        for day, what, value in row["e"]:
            if what.startswith("t:"):                                  # today's money: today whatever the dates
                todays[what[2:]] = todays.get(what[2:], 0) + value
            elif low <= day <= high:
                totals[what] = totals.get(what, 0) + value
                income += value if what == "i" else 0
        earners += 1 if income > 0 else 0
    owing = [row["o"] for pk, row in amounts.items() if row["o"] > 0 and in_dates(days[pk][kind], start, end)]
    held = [row["h"] for row in amounts.values() if row["h"] > 0]
    give = [row["g"] for row in amounts.values() if row["g"] > 0]

    def methods(source, prefix=""):
        return [(name, _text(source.get(prefix + name, 0))) for name in dashboard_money.METHOD_GROUPS
                if name != "Other" or source.get(prefix + name, 0)]

    box.update({
        "held": _text(sum(held)), "held_bookings": len(held), "give_back": _text(sum(give)), "give_back_bookings": len(give),
        "owing": _text(sum(owing)), "owing_bookings": len(owing), "collected": _text(totals.get("d", 0)),
        "returned_deposit": _text(totals.get("r", 0)), "returned_extra": _text(totals.get("x", 0)),
    })
    if see_income:
        box.update({"income": _text(totals.get("i", 0)), "income_bookings": earners, "kept": _text(totals.get("k", 0)),
                    "kept_cancelled": _text(totals.get("c", 0)), "methods": methods(totals, "m:")})
    else:
        box["today_money"] = {"net": _text(sum(todays.values())), "methods": methods(todays)}
    return box
