"""The Gown Board: pick one exact day and see what the shop has to get ready, what comes back, what is late, and what is in a hurry.

  To get ready   the gowns customers collect on that day (with what each customer still owes, so it is collected at the counter)
  To come back   the gowns due back on that day (and the ones that did come back)
  Overdue now    gowns past their return date and still out, right now (whatever day is shown)
  Rush           a pick-up in the next few days whose gown is NOT ready: it needs repair, is marked Out-of-Stock, is being cleaned or repaired on that
                 day, or is still out with the last customer (late)
  Ready again    gowns whose cleaning / repair / rest ended the day before, so they are back on the shelf that day

It only READS: marking a pick-up or a return stays in Active Reservations (it has the condition, balance and late-fee rules), and every line links there.
Nothing is stored for it (no table, no migration): everything comes from the bookings, the gowns' rest days and the payments, which is also why it can
never disagree with them. The page marks its list as a live region, so it updates by itself like the other lists."""
from datetime import date, timedelta
from urllib.parse import urlencode

from django.db.models import Q
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from gowns.models import Gown, GownUnavailability, SiteSettings
from reservations import ledger
from reservations.models import Reservation, ReservationItem

from .views import _OUT_STAGES, _days_late, _item_gown_tag, _require_admin_staff

STAGE = ReservationItem.Stage
# Bookings whose gowns can be on the board: confirmed, out, late, or finished (so a past day still shows what came back).
BOARD_STATUSES = (Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE, Reservation.Status.OVERDUE, Reservation.Status.RETURNED)
RUSH_DAYS = 2                 # Rush is checked for a pick-up today and in the next 2 days (further out, a late gown is probably back by then)
MAX_YEARS = 3                 # a date typed or linked outside this many years either way shows today instead


def parse_day(raw, today=None):
    """A YYYY-MM-DD date from the address, or today when it is missing, malformed or absurdly far away."""
    today = today or timezone.localdate()
    try:
        day = date.fromisoformat(str(raw or "").strip())
    except ValueError:
        return today
    if abs((day - today).days) > 366 * MAX_YEARS:
        return today
    return day


def _day_label(day):
    return f"{day:%A}, {day:%B} {day.day}, {day.year}"


def _money_text(amount):
    return f"{amount:,.2f}"


def board_for(day, today=None, tag_colors=None):
    """Everything the board shows for `day`: {"rush", "pickups", "returns", "overdue", "ready_again", "counts", ...}. Lines are plain dicts."""
    today = today or timezone.localdate()
    tag_colors = SiteSettings.load().tag_colors() if tag_colors is None else tag_colors

    live = ReservationItem.objects.filter(reservation__status__in=BOARD_STATUSES).exclude(stage=STAGE.CANCELLED)
    wanted = live.filter(Q(rental_date=day) | Q(picked_up_on=day) | Q(return_date=day) | Q(returned_on=day)) | live.filter(stage__in=_OUT_STAGES, return_date__lt=today)
    items = list(
        wanted.select_related("reservation__customer__profile", "gown")
        .prefetch_related("reservation__items", "reservation__payments", "reservation__deductions", "reservation__receipt_records")
        .order_by("rental_date", "id")
    )

    owed = {}

    def balance(reservation):
        if reservation.pk not in owed:
            owed[reservation.pk] = ledger.statement(reservation, today).balance_due
        return owed[reservation.pk]

    def line(item, state, text):
        code, color, hex_color = _item_gown_tag(item, tag_colors)
        reservation = item.reservation
        # Where "Open booking" goes: a running booking opens in Active Reservations (where it is marked picked up or returned); a finished one in Records.
        finished = reservation.status == Reservation.Status.RETURNED
        base = reverse("arabela_admin:reservation_records" if finished else "arabela_admin:active_reservations")
        return {
            "id": item.id, "gown": item.gown_name, "code": code, "tag_color": color, "tag_hex": hex_color, "size": item.size or "",
            "customer": reservation.display_customer_name, "phone": reservation.phone or "", "booking": reservation.reference_code,
            "booking_id": reservation.id, "state": state, "state_text": text, "pickup": item.rental_date, "ret": item.return_date,
            "event": item.effective_event_date, "balance": "", "balance_value": ledger.ZERO, "late_days": 0, "reason": "",
            "open_url": base + "?" + urlencode({"search": reservation.reference_code}),
        }

    pickups, returns, overdue = [], [], []
    for item in items:
        # ---- the gowns picked up (or waiting to be) on this day
        if item.rental_date == day or item.picked_up_on == day:
            if item.stage == STAGE.PICKUP:
                row = line(item, "missed" if day < today else "waiting", "Not picked up" if day < today else "Waiting for pick-up")
                due = balance(item.reservation)
                if due > 0:
                    row["balance"], row["balance_value"] = _money_text(due), due
            else:
                row = line(item, "picked", "Picked up")
            pickups.append((item, row))
        # ---- the gowns due back (or back) on this day
        if (item.return_date == day or item.returned_on == day) and item.stage != STAGE.PICKUP:
            if item.stage == STAGE.RETURNED:
                row = line(item, "returned", "Returned" + (f" {item.returned_on:%b} {item.returned_on.day}" if item.returned_on else ""))
            elif item.return_date < today:
                days = _days_late(item, today) or 0
                row = line(item, "late", f"{days} day{'s' if days != 1 else ''} late")
                row["late_days"] = days
            else:
                row = line(item, "due", "Due back")
            returns.append(row)
        # ---- right now: out past the return date, whatever day is shown
        if item.stage in _OUT_STAGES and item.return_date < today:
            days = _days_late(item, today) or 0
            row = line(item, "late", f"{days} day{'s' if days != 1 else ''} late")
            row["late_days"] = days
            overdue.append(row)

    rush, calm = [], []
    horizon = today <= day <= today + timedelta(days=RUSH_DAYS)
    reasons = _rush_reasons([i for i, _ in pickups if i.stage == STAGE.PICKUP and i.gown_id], day) if horizon else {}
    for item, row in pickups:
        row["reason"] = reasons.get(item.id, "")
        (rush if row["reason"] else calm).append(row)

    ready_again = []
    for block in (GownUnavailability.objects.filter(end_date=day - timedelta(days=1)).select_related("gown").order_by("gown__name", "id")):
        ready_again.append({"gown": block.gown.name, "code": block.gown.gown_id, "why": block.get_reason_display(), "ended": block.end_date})

    overdue.sort(key=lambda r: (-r["late_days"], r["customer"].lower()))
    returns.sort(key=lambda r: ({"late": 0, "due": 1, "returned": 2}[r["state"]], r["customer"].lower()))
    calm.sort(key=lambda r: ({"missed": 0, "waiting": 1, "picked": 2}[r["state"]], r["customer"].lower()))
    rush.sort(key=lambda r: r["customer"].lower())
    return {
        "day": day, "iso": day.isoformat(), "label": _day_label(day), "is_today": day == today, "is_past": day < today,
        "prev": (day - timedelta(days=1)).isoformat(), "next": (day + timedelta(days=1)).isoformat(),
        "rush": rush, "pickups": calm, "returns": returns, "overdue": overdue, "ready_again": ready_again, "rush_checked": horizon,
        "counts": {"pickups": len(rush) + len(calm), "returns": len(returns), "overdue": len(overdue), "rush": len(rush)},
    }


def _rush_reasons(waiting, day):
    """{item id: why this pick-up is in a hurry}: only for a gown still waiting to go out, and only when its gown is not ready.
    Three small queries however many gowns are waiting."""
    if not waiting:
        return {}
    gown_ids = {i.gown_id for i in waiting}
    waiting_ids = {i.id for i in waiting}
    still_out = {}
    for other in ReservationItem.objects.filter(gown_id__in=gown_ids, stage__in=_OUT_STAGES, return_date__lt=day).exclude(id__in=waiting_ids).order_by("return_date"):
        still_out.setdefault(other.gown_id, other)
    resting = {}
    for block in GownUnavailability.objects.filter(gown_id__in=gown_ids, start_date__lte=day, end_date__gte=day).order_by("end_date"):
        resting.setdefault(block.gown_id, block)
    out = {}
    for item in waiting:
        gown, why = item.gown, ""
        if gown.condition == Gown.Condition.NEEDS_REPAIR:
            why = "Needs repair before it can go out"
        elif gown.status == Gown.Status.OUT_OF_STOCK:
            why = "Marked Out-of-Stock"
        elif item.gown_id in still_out:
            why = f"Still out with the last customer (was due {still_out[item.gown_id].return_date:%b} {still_out[item.gown_id].return_date.day})"
        elif item.gown_id in resting:
            block = resting[item.gown_id]
            why = f"{block.get_reason_display()} until {block.end_date:%b} {block.end_date.day}"
        if why:
            out[item.id] = why
    return out


@_require_admin_staff
def gown_board_view(request):
    today = timezone.localdate()
    day = parse_day(request.GET.get("date"), today)
    return render(request, "arabela_admin/gown-board.html", {
        "page": "gown-board", "board": board_for(day, today), "today_iso": today.isoformat(), "tomorrow_iso": (today + timedelta(days=1)).isoformat(),
    })
