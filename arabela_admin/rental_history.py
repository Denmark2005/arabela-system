"""What Rental History says about one gown: its status in words, the three steps (Booked, Pick-up, Return), the facts of the opened row and where to act on it.

Everything is read from the booking, its dates and its money (reservations/ledger.py); nothing is saved here. The words are the ones Active Reservations
uses ("Pick-up in 3 days", "2 days late for pick-up", "Return due today", "N days overdue"), so the two pages never tell a different story."""
from datetime import timedelta
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

from reservations import ledger
from reservations.models import Reservation, ReservationItem

STAGE = ReservationItem.Stage
OUT = (STAGE.RESERVED, STAGE.RETURN, STAGE.OVERDUE)
SCHEDULED = (Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE, Reservation.Status.OVERDUE)

PENDING, WAITING, OUT_NOW, OVERDUE, RETURNED, MISSED = (
    "Pending approval", "Waiting for pick-up", "Out with customer", "Overdue", "Returned", "Not picked up")

# status -> (the chip's and circle's colour name, the sentence the circle says on hover). The filter buttons are drawn in this order.
LOOK = {
    PENDING: ("pending", "The booking is still waiting for approval"),
    WAITING: ("waiting", "Approved; the gown is still in the shop"),
    OUT_NOW: ("out", "The gown is with the customer and not overdue"),
    OVERDUE: ("overdue", "The gown is out with the customer and past its return date"),
    RETURNED: ("returned", "The gown is back"),
    MISSED: ("missed", "The dates passed and nobody picked the gown up"),
}
# The circle before a name uses the same colours as Active Reservations (partials/ar_list_styles.html); two are Rental History's own.
DOTS = {"pending": "rh-dot--pending", "waiting": "ar-dot--confirmed", "out": "ar-dot--active", "overdue": "ar-dot--overdue", "returned": "ar-dot--done",
        "missed": "rh-dot--missed"}


def status_of(item, today):
    """The one status of a gown (a Rejected / Cancelled booking and a cancelled gown never reach this page)."""
    if item.reservation.status == Reservation.Status.PENDING:
        return PENDING
    if item.stage == STAGE.RETURNED:
        return RETURNED
    if item.stage in OUT:
        return OVERDUE if today > item.return_date else OUT_NOW
    return MISSED if item.return_date < today else WAITING


def _day(value, today):
    """'Nov 30', with the year when it is not this year's."""
    return f"{value:%b} {value.day}" + ("" if value.year == today.year else f", {value.year}")


def _weekday(value, today):
    """'Mon, Nov 30'."""
    return f"{value:%a}, {_day(value, today)}"


def _days(count):
    return f"{count} day{'' if count == 1 else 's'}"


def _words(status, item, today, late_days):
    """(the short line under the status in the list, its tone, the headline of the opened row). Tones: '', 'warn', 'bad', 'ok'."""
    if status == PENDING:
        return "Waiting for approval", "warn", f"Waiting for approval · pick-up {_weekday(item.rental_date, today)}"
    if status == WAITING:
        days = (item.rental_date - today).days
        if days > 0:
            return f"Pick-up in {_days(days)}", "", f"Pick-up in {_days(days)} · {_weekday(item.rental_date, today)}"
        if days == 0:
            return "Pick up today", "warn", "Pick up today, 1:00 PM to 5:00 PM"
        return f"{_days(-days)} late for pick-up", "bad", f"{_days(-days)} late for pick-up · was due {_weekday(item.rental_date, today)}"
    if status == OUT_NOW:
        days = (item.return_date - today).days
        if days <= 0:
            return "Return due today", "warn", "Due back today"
        return f"Return in {_days(days)}", "", f"Due back in {_days(days)} · {_weekday(item.return_date, today)}"
    if status == OVERDUE:
        text = f"{_days(late_days or 1)} overdue"
        return text, "bad", f"{text} · was due {_weekday(item.return_date, today)}"
    if status == RETURNED:
        if late_days is None:
            return "", "", "The gown is back"
        back = f"Returned {_weekday(item.returned_on, today)}" if item.returned_on else "Returned"
        if late_days:
            return f"{_days(late_days)} overdue", "bad", f"{back} · {_days(late_days)} overdue"
        return "On time", "ok", f"{back} · on time"
    return "Never picked up", "bad", f"Never picked up · the return date was {_weekday(item.return_date, today)}"


def _was(original, shown, today):
    """'was Nov 26' when the day shown is not the one first booked."""
    return f"was {_day(original, today)}" if original and original != shown else ""


def _due(value, today):
    return "Today" if value == today else f"due {_day(value, today)}"


def _steps(status, item, today, late_days):
    """Booked, Pick-up, Return: each with its state ('done', 'now', 'bad' or 'todo'), the line leading to it ('done', 'bad' or 'todo'), the date, the day it
    was first booked for when it moved, and a short word under it."""
    booked_day = timezone.localtime(item.reservation.created_at).date()
    booked = {"name": "Booked", "state": "done", "date": _day(booked_day, today), "was": "", "note": "Waiting for approval" if status == PENDING else "",
              "tone": "warn" if status == PENDING else ""}

    out_or_back = item.stage != STAGE.PICKUP
    shown_pickup = item.picked_up_on or item.rental_date
    pickup = {"name": "Pick-up", "date": _day(shown_pickup, today), "was": _was(item.original_rental_date, shown_pickup, today), "note": "", "tone": ""}
    if status == PENDING:
        pickup.update(state="todo")
    elif out_or_back:
        pickup.update(state="done", note="Picked up" if item.picked_up_on else "")
    elif status == MISSED:
        pickup.update(state="bad", note="Never picked up", tone="bad")
    elif item.rental_date < today:
        pickup.update(state="bad", note="Late for pick-up", tone="bad", date=_due(item.rental_date, today))
    else:
        pickup.update(state="now", date=_due(item.rental_date, today))

    shown_return = (item.returned_on or item.return_date) if status == RETURNED else item.return_date
    back = {"name": "Return", "date": _due(item.return_date, today), "was": _was(item.original_return_date, shown_return, today), "note": "", "tone": ""}
    if status == RETURNED:
        back.update(state="done", date=_day(shown_return, today))
        if late_days is not None:
            back.update(note=f"{_days(late_days)} overdue" if late_days else "On time", tone="bad" if late_days else "ok")
    elif status == OVERDUE:
        back.update(state="bad", note=f"{_days(late_days or 1)} overdue", tone="bad")
    elif status == OUT_NOW:
        back.update(state="now")
    else:
        back.update(state="todo")

    steps = [booked, pickup, back]
    for step in steps[1:]:
        step["line"] = "done" if step["state"] in ("done", "now") else ("bad" if step["state"] == "bad" else "todo")
    booked["line"] = ""
    return steps


def _event(status, item, today):
    """The customer's event day, the day first booked when it was changed, and a warning while it sits outside the rental days of a booking staff can
    still change (Active Reservations has the "Set new event date" button then)."""
    event = item.effective_event_date
    original = item.original_return_date - timedelta(days=2) if item.original_return_date else None
    flag = ""
    if status in (WAITING, OUT_NOW, OVERDUE):
        if event < item.rental_date:
            flag = "before pick-up"
        elif event > item.return_date:
            flag = "after return"
    return {"date": _weekday(event, today), "was": _was(original, event, today), "flag": flag}


def _fees(status, item, today):
    """The fees of this gown for the dates as they stand, as Active Reservations shows them: [{"label", "amount", "kind"}]. A gown nobody picked up is never charged."""
    rate = int(item.reservation.late_fee_per_day or 0)
    if rate <= 0 or status == MISSED:
        return []
    planned, overdue = ledger.planned_late_days(item, today), ledger.overdue_days(item, today)
    rows = []
    for kind, days in ((ledger.CHANGE_FEE, planned - overdue), (ledger.OVERDUE_FEE, overdue)):
        if days > 0:
            rows.append({"label": ledger.FEE_LABELS[kind], "amount": ledger.peso(days * rate), "kind": kind})
    return rows


def _money(status, reservation, statement):
    """(payment words, tone, deposit words) of the booking."""
    if status == PENDING:
        return reservation.payment_state, "warn", ""
    if not statement.tracked:
        return "", "", ""
    payment, tone = (f"Balance {ledger.peso(statement.balance_due)} to pay", "bad") if statement.balance_due > 0 else ("Paid in full", "ok")
    stage = ledger.deposit_stage(reservation, statement)
    if stage == ledger.DEPOSIT_HELD:
        deposit = f"Held {ledger.peso(statement.deposit_paid)}"
    elif stage == ledger.DEPOSIT_RETURN_DUE:
        deposit = f"Return due {ledger.peso(statement.refund_available)}"
    else:
        deposit = stage
    return payment, tone, deposit


def _link(status, reservation):
    """Where staff act on this booking: (words, address)."""
    code = urlencode({"search": reservation.reference_code})
    if status == PENDING:
        return "Open in Pending Approval", reverse("arabela_admin:pending_approval") + "?" + code
    running = reservation.status in SCHEDULED and any(i.stage not in ReservationItem.FINISHED_STAGES for i in reservation.items.all())
    if running:
        return "Open in Active Reservations", reverse("arabela_admin:active_reservations") + "?" + code
    return "Open in All Bookings", reverse("arabela_admin:reservation_records") + "?" + code


def describe(item, today, late_days, statement):
    """Everything the page draws for one gown (read from what the view already loaded: no extra database question)."""
    reservation = item.reservation
    status = status_of(item, today)
    kind, tip = LOOK[status]
    note, tone, headline = _words(status, item, today, late_days)
    payment, payment_tone, deposit = _money(status, reservation, statement)
    link_label, link_url = _link(status, reservation)
    return {
        "status": status, "kind": kind, "dot": DOTS[kind], "tip": tip, "note": note, "tone": tone, "headline": headline,
        "steps": _steps(status, item, today, late_days), "event": _event(status, item, today), "fees": _fees(status, item, today),
        "payment": payment, "payment_tone": payment_tone, "deposit": deposit, "link_label": link_label, "link_url": link_url,
    }
