"""Gown Monitoring (Inventory Management): what staff have to do with the gowns on the days they pick. It took the Gown Board's place.

  To prepare   the gowns customers collect on those days, still waiting in the shop:
                 TO PREPARE   steam it, check it, hang it on the ready rack
                 PROBLEM      it can't be ready: it needs repair, it is Out-of-Stock, another customer still has it, or it is blocked that day
                 READY        staff marked it (the customer with a website account is told, once per pick-up day)
  For return   OVERDUE gowns (on every date), the gowns DUE back on those days, and gowns already returned that are being CLEANED / resting then.

The page changes ONE thing: the "ready" mark. Pick-ups, returns and money are recorded in Active Reservations (each line links to its booking), where the
condition, balance and late-fee rules are. Everything else is read from the bookings, the gowns and their blocked days, so it can never disagree with them."""
from collections import defaultdict
from datetime import date, timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from accounts import customer_emails
from accounts.audit import audited
from accounts.models import CustomerMessage
from accounts.roles import staff_display_name
from arabela_admin import audit_describe, gown_photo
from gowns.models import Gown, GownUnavailability, SiteSettings
from gowns.photos import sized
from reservations import reminders
from reservations.models import Reservation, ReservationItem

from .views import (_NON_RENTAL_STATUSES, _OUT_STAGES, _SCHEDULED_STATUSES, _days_late, _is_admin_staff, _item_gown_tag, _require_admin_staff)

STAGE = ReservationItem.Stage
NEXT_DAYS = 3                  # "Next 3 days" = today and the two days after it
READY_AHEAD_DAYS = 2           # a gown can be marked ready on its pick-up day or up to 2 days before (earlier it could still go out to someone else)
MAX_DAYS_AHEAD = 366 * 3       # a date typed or linked further ahead than this shows today instead
THUMB_WIDTH = 200
PICKUP_HOURS = "1:00 PM to 5:00 PM only"
PREPARE, PROBLEM, READY = "prepare", "problem", "ready"
LATE, DUE, CLEANING = "late", "due", "cleaning"
STATUS_WORDS = {PREPARE: "To prepare", PROBLEM: "Problem", READY: "Ready", LATE: "Overdue", DUE: "Due", CLEANING: "Cleaning"}
BLOCK_WORDS = {
    GownUnavailability.Reason.COOLDOWN: "Resting after its last rental", GownUnavailability.Reason.CLEANING: "Being cleaned",
    GownUnavailability.Reason.REPAIR: "In repair", GownUnavailability.Reason.ALTERATION: "Being altered", GownUnavailability.Reason.OTHER: "Blocked",
}
# What the "ready" message to the customer starts and continues with: the same message is found again (and brought up to date) by these words.
READY_OPENING = "Good news! "
READY_MIDDLE = " ready for pick-up at Arabela on "


# ---------------------------------------------------------------- the days chosen
def _short(day, today):
    """'Nov 28', with the year when it is not this year's."""
    return f"{day:%b} {day.day}" + ("" if day.year == today.year else f", {day.year}")


def _long(day):
    """'Saturday, November 28, 2026'."""
    return f"{day:%A}, {day:%B} {day.day}, {day.year}"


def _day_word(day, today):
    """'Today', 'Tomorrow', else 'Nov 28'."""
    if day == today:
        return "Today"
    if day == today + timedelta(days=1):
        return "Tomorrow"
    return _short(day, today)


def selection_for(params, today):
    """The days the page shows, from the address: ?date=YYYY-MM-DD (one exact day), or ?when=tomorrow / next3; nothing means today. A date that is wrong, in
    the past or years away shows today, so a typed or old link can never break the page.
    Returns {"mode": today|tomorrow|next3|date, "start", "end", "label", "words" (for "nothing ..." sentences), "query" (to keep it in links)}."""
    raw = (params.get("date") or "").strip()
    when = (params.get("when") or "").strip()
    tomorrow = today + timedelta(days=1)
    mode, start, end = "today", today, today
    if raw:
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            day = None
        if day is not None and today <= day <= today + timedelta(days=MAX_DAYS_AHEAD):
            start = end = day
            mode = "today" if day == today else "tomorrow" if day == tomorrow else "date"
    elif when == "tomorrow":
        mode, start, end = "tomorrow", tomorrow, tomorrow
    elif when == "next3":
        mode, start, end = "next3", today, today + timedelta(days=NEXT_DAYS - 1)
    if mode == "next3":
        label, words, query = f"Next {NEXT_DAYS} days · {_short(start, today)} – {_short(end, today)}", f"in the next {NEXT_DAYS} days", {"when": "next3"}
    elif mode == "tomorrow":
        label, words, query = f"Tomorrow · {_long(start)}", "tomorrow", {"when": "tomorrow"}
    elif mode == "date":
        label, words, query = _long(start), f"on {_short(start, today)}", {"date": start.isoformat()}
    else:
        label, words, query = f"Today · {_long(start)}", "today", {}
    return {"mode": mode, "start": start, "end": end, "label": label, "words": words, "query": query}


def page_url(tab="prepare", query=None):
    params = ({"tab": "return"} if tab == "return" else {}) | (query or {})
    return reverse("arabela_admin:gown_monitoring") + (("?" + urlencode(params)) if params else "")


# ---------------------------------------------------------------- one line
def _line(item, tag_colors):
    """Everything about one booked gown that both lists show. Expects reservation__customer__profile, gown and reservation__items loaded."""
    code, color, hex_color = _item_gown_tag(item, tag_colors)
    reservation = item.reservation
    # Where "View booking" goes: a booking still running is in Active Reservations (it leaves that page once every gown is back or cancelled).
    running = reservation.status in _SCHEDULED_STATUSES and any(other.stage not in ReservationItem.FINISHED_STAGES for other in reservation.items.all())
    page = reverse("arabela_admin:active_reservations" if running else "arabela_admin:reservation_records")
    photo = gown_photo.photo_url(item)
    size = (item.size or "").strip()
    return {
        "id": item.id, "gown": item.gown_name, "code": code, "code_words": code.split(), "tag_color": color, "tag_hex": hex_color,
        "size": "" if size in ("-", "TBD") else size,
        "photo": photo, "thumb": sized(photo, THUMB_WIDTH) if photo else "", "window_sub": gown_photo.subtitle(item, color),
        "customer": reservation.display_customer_name, "phone": (reservation.phone or "").strip(), "booking": reservation.reference_code,
        "booking_url": f"{page}?{urlencode({'search': reservation.reference_code})}",
    }


def _items():
    return ReservationItem.objects.select_related("reservation__customer__profile", "gown").prefetch_related("reservation__items")


# ---------------------------------------------------------------- to prepare
def gown_problems(items, today):
    """{item id: why this gown can't be ready for its pick-up}, for gowns still waiting to go out. Two queries however many gowns there are.
    Another customer who has it only counts when they are late, or when their rental still runs on the pick-up day."""
    linked = [i for i in items if i.gown_id]
    if not linked:
        return {}
    gown_ids = {i.gown_id for i in linked}
    held = defaultdict(list)
    for other in (ReservationItem.objects.filter(gown_id__in=gown_ids, stage__in=_OUT_STAGES).exclude(reservation__status__in=_NON_RENTAL_STATUSES)
                  .select_related("reservation__customer__profile").order_by("return_date", "id")):
        held[other.gown_id].append(other)
    first, last = min(i.rental_date for i in linked), max(i.rental_date for i in linked)
    blocks = defaultdict(list)
    for block in GownUnavailability.objects.filter(gown_id__in=gown_ids, start_date__lte=last, end_date__gte=first).order_by("-end_date", "id"):
        blocks[block.gown_id].append(block)
    problems = {}
    for item in linked:
        gown, others = item.gown, [o for o in held[item.gown_id] if o.id != item.id]
        late = next((o for o in others if o.return_date < today), None)
        busy = next((o for o in others if o.return_date >= item.rental_date), None)
        block = next((b for b in blocks[item.gown_id] if b.start_date <= item.rental_date <= b.end_date and b.auto_for_item_id != item.id), None)
        if gown.condition == Gown.Condition.NEEDS_REPAIR:
            why = "Needs repair"
        elif gown.status == Gown.Status.OUT_OF_STOCK:
            why = "Marked Out-of-Stock"
        elif late is not None:
            why = f"Not returned: {late.reservation.display_customer_name} was due {_short(late.return_date, today)}"
        elif busy is not None:
            why = f"With {busy.reservation.display_customer_name} until {_short(busy.return_date, today)}"
        elif block is not None:
            why = f"{BLOCK_WORDS.get(block.reason, 'Blocked')} until {_short(block.end_date, today)}"
        else:
            continue
        problems[item.id] = why
    return problems


def prepare_rows(selection, today, tag_colors):
    """The gowns waiting to be collected on the chosen days, the earliest day first: a problem, then to prepare, then ready."""
    items = list(_items().select_related("ready_by").filter(
        stage=STAGE.PICKUP, rental_date__gte=selection["start"], rental_date__lte=selection["end"], reservation__status__in=_SCHEDULED_STATUSES,
    ).order_by("rental_date", "id"))
    problems = gown_problems(items, today)
    rows = []
    for item in items:
        why = problems.get(item.id, "")
        status = PROBLEM if why else READY if item.is_ready_for_pickup else PREPARE
        row = _line(item, tag_colors)
        detail = why
        if status == READY:
            when = timezone.localtime(item.ready_at)
            clock = when.strftime("%I:%M %p").lstrip("0")
            when_text = clock if when.date() == today else f"{_short(when.date(), today)}, {clock}"
            detail = (f"by {staff_display_name(item.ready_by)}, " if item.ready_by else "") + when_text
        elif status == PROBLEM and item.is_ready_for_pickup:
            detail = f"{why} (it was marked ready)"
        mark_from = item.rental_date - timedelta(days=READY_AHEAD_DAYS)
        row.update({
            "status": status, "status_word": STATUS_WORDS[status], "detail": detail, "day": item.rental_date,
            "date_label": "Pick-up", "date_text": _day_word(item.rental_date, today), "date_is_today": item.rental_date == today,
            "event_text": _short(item.effective_event_date, today),
            "can_ready": status == PREPARE and today >= mark_from,
            "ready_from": "" if status != PREPARE or today >= mark_from else _short(mark_from, today),
            "told": item.ready_told_for == item.rental_date and item.reservation.customer_id is not None,
        })
        rows.append(row)
    order = {PROBLEM: 0, PREPARE: 1, READY: 2}
    rows.sort(key=lambda r: (r["day"], order[r["status"]], r["customer"].lower(), r["id"]))
    return rows


# ---------------------------------------------------------------- for return
def return_rows(selection, today, tag_colors):
    """Overdue gowns first (the most days overdue on top), then the gowns due back on the chosen days, then returned gowns still being cleaned or resting then."""
    start, end = selection["start"], selection["end"]
    rows = []

    def add(item, status, detail, date_label, date_text, sort_key):
        row = _line(item, tag_colors)
        row.update({"status": status, "status_word": STATUS_WORDS[status], "detail": detail, "date_label": date_label, "date_text": date_text,
                    "date_is_today": date_text == "Today", "event_text": _short(item.effective_event_date, today), "sort": sort_key})
        rows.append(row)

    # Out with a customer: overdue (whatever day is shown), or due back on the chosen days.
    for item in _items().filter(stage__in=_OUT_STAGES, return_date__lte=end, reservation__status__in=_SCHEDULED_STATUSES).order_by("return_date", "id"):
        if item.return_date < today:
            days = _days_late(item, today) or 0
            add(item, LATE, f"{days} day{'s' if days != 1 else ''} overdue", "Was due", _short(item.return_date, today), (0, -days))
        elif item.return_date >= start:
            add(item, DUE, "", "Return", _day_word(item.return_date, today), (1, item.return_date.toordinal()))

    returned = _items().filter(stage=STAGE.RETURNED, returned_on__isnull=False, returned_on__lte=end).exclude(reservation__status__in=_NON_RENTAL_STATUSES)
    # Returned in good shape: cleaned and resting until its rest days end (staff releasing those days in the catalog takes it off this list).
    for item in (returned.exclude(return_condition=ReservationItem.ReturnCondition.NEEDS_REPAIR).filter(auto_cooldown_block__end_date__gte=start)
                 .select_related("auto_cooldown_block").order_by("auto_cooldown_block__end_date", "id")):
        until = item.auto_cooldown_block.end_date
        add(item, CLEANING, f"Resting until {_short(until, today)}", "Returned", _day_word(item.returned_on, today), (3, until.toordinal()))
    # Returned needing repair: listed while the gown still needs it (a current fact, so only when the chosen days include today).
    if start <= today:
        seen = set()
        for item in returned.filter(return_condition=ReservationItem.ReturnCondition.NEEDS_REPAIR, gown__condition=Gown.Condition.NEEDS_REPAIR).order_by("-returned_on", "-id"):
            if item.gown_id in seen:
                continue
            seen.add(item.gown_id)
            add(item, CLEANING, "Needs repair", "Returned", _day_word(item.returned_on, today), (2, item.returned_on.toordinal()))
    rows.sort(key=lambda r: (r["sort"], r["customer"].lower(), r["id"]))
    return rows


def _tally(rows, *statuses):
    return {status: sum(1 for r in rows if r["status"] == status) for status in statuses}


# ---------------------------------------------------------------- the page
@_require_admin_staff
def gown_monitoring_view(request):
    today = timezone.localdate()
    selection = selection_for(request.GET, today)
    tab = "return" if request.GET.get("tab") == "return" else "prepare"
    tag_colors = SiteSettings.load().tag_colors()
    prepare = prepare_rows(selection, today, tag_colors)
    returns = return_rows(selection, today, tag_colors)
    return render(request, "arabela_admin/gown-monitoring.html", {
        "page": "gown-monitoring", "tab": tab, "selection": selection,
        "rows": returns if tab == "return" else prepare,
        "prepare_count": len(prepare), "prepare_tally": _tally(prepare, PROBLEM, PREPARE, READY),
        "return_count": len(returns), "return_tally": _tally(returns, LATE, DUE, CLEANING),
        "prepare_url": page_url("prepare", selection["query"]), "return_url": page_url("return", selection["query"]),
        "today_url": page_url(tab), "tomorrow_url": page_url(tab, {"when": "tomorrow"}), "next_url": page_url(tab, {"when": "next3"}),
        "date_value": "" if selection["mode"] == "next3" else selection["start"].isoformat(),
        "today_iso": today.isoformat(), "last_iso": (today + timedelta(days=MAX_DAYS_AHEAD)).isoformat(),
        "next_days": NEXT_DAYS, "ready_ahead_days": READY_AHEAD_DAYS, "pickup_hours": PICKUP_HOURS,
    })


@_require_admin_staff
def gown_board_redirect_view(request):
    """The old Gown Board address: Gown Monitoring took its place (a date in the old link is kept)."""
    raw = (request.GET.get("date") or "").strip()
    return redirect(page_url("prepare", {"date": raw} if raw else None))


# ---------------------------------------------------------------- the ready mark
def tell_customer_ready(reservation, item):
    """The bell message (and the email, when customer emails are on) that a booking's gowns are ready: ONE message per booking and pick-up day, naming every
    gown of that day staff marked ready -- marking the next gown brings the same message up to date (unread again) instead of adding another."""
    ready = [i for i in ReservationItem.objects.filter(reservation=reservation, rental_date=item.rental_date).order_by("id") if i.is_ready_for_pickup]
    names = reminders._gown_list(ready)
    day = _long(item.rental_date)
    body = f"{READY_OPENING}{names} {'is' if len(ready) == 1 else 'are'}{READY_MIDDLE}{day} ({PICKUP_HOURS}). Reference: {reservation.reference_code}."
    earlier = CustomerMessage.objects.filter(
        recipient_id=reservation.customer_id, category=CustomerMessage.Category.GENERAL, body__startswith=READY_OPENING,
        body__contains=f"{READY_MIDDLE}{day} (", body__endswith=f"Reference: {reservation.reference_code}.",
    ).order_by("-id").first()
    if earlier is not None:
        CustomerMessage.objects.filter(pk=earlier.pk).update(body=body, is_read=False, created_at=timezone.now())
    else:
        CustomerMessage.objects.create(recipient_id=reservation.customer_id, category=CustomerMessage.Category.GENERAL, body=body)
    if getattr(settings, "CUSTOMER_EMAILS_ENABLED", False):
        reservation_id = reservation.pk
        transaction.on_commit(lambda: customer_emails._guarded(_send_ready_email, reservation_id, body))


def _send_ready_email(reservation_id, body):
    reservation = Reservation.objects.select_related("customer").get(pk=reservation_id)
    customer_emails._deliver(reservation.customer, customer_emails.GOWN_READY, customer_emails._reservation_data(reservation, text=body))


def _locked(item_id):
    """The booking and its gown line, both locked (booking first), or (None, None). Two staff pressing at once go one after the other."""
    reservation_id = ReservationItem.objects.filter(pk=item_id).values_list("reservation_id", flat=True).first()
    if reservation_id is None:
        return None, None
    reservation = Reservation.objects.select_for_update(of=("self",)).filter(pk=reservation_id).first()
    item = ReservationItem.objects.select_for_update(of=("self",)).filter(pk=item_id).first()
    return reservation, item


def _refuse(message, status=409):
    return JsonResponse({"error": message}, status=status)


@require_http_methods(["POST"])
@audited('item.ready', 'Bookings', audit_describe.item_ready)
@transaction.atomic
def reservation_item_ready_view(request, item_id):
    """Mark a gown ready for its pick-up (today or up to READY_AHEAD_DAYS ahead, and only when nothing keeps it from being ready)."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    reservation, item = _locked(item_id)
    if item is None or reservation is None:
        return _refuse("That gown was not found. The list will refresh.", status=404)
    if reservation.status not in _SCHEDULED_STATUSES or item.stage != STAGE.PICKUP:
        return _refuse("This gown is no longer waiting for pick-up. The list will refresh.")
    today = timezone.localdate()
    if item.rental_date < today:
        return _refuse("Its pick-up day has passed. Check the booking in Active Reservations.")
    if item.rental_date > today + timedelta(days=READY_AHEAD_DAYS):
        return _refuse(f"It can be marked ready from {_short(item.rental_date - timedelta(days=READY_AHEAD_DAYS), today)}, "
                       f"{READY_AHEAD_DAYS} days before its pick-up.", status=400)
    if item.is_ready_for_pickup:
        who = f" by {staff_display_name(item.ready_by)}" if item.ready_by else ""
        return _refuse(f"It is already marked ready{who}. The list will refresh.")
    why = gown_problems([item], today).get(item.id)
    if why:
        return _refuse(f"It can't be marked ready yet: {why}.")
    tell = reservation.customer_id is not None and item.ready_told_for != item.rental_date
    item.ready_at, item.ready_by, item.ready_for = timezone.now(), request.user, item.rental_date
    if tell:
        item.ready_told_for = item.rental_date
    item.save(update_fields=["ready_at", "ready_by", "ready_for", "ready_told_for", "updated_at"])
    if tell:
        tell_customer_ready(reservation, item)
    if tell:
        message = f"{item.gown_name} is ready. {reservation.display_customer_name} was told."
    elif reservation.customer_id is not None:
        message = f"{item.gown_name} is ready. The customer was already told about this pick-up."
    else:
        message = f"{item.gown_name} is ready. This customer has no website account, so let them know yourself."
    return JsonResponse({"success": True, "told": tell, "message": message})


@require_http_methods(["POST"])
@audited('item.unready', 'Bookings', audit_describe.item_unready)
@transaction.atomic
def reservation_item_unready_view(request, item_id):
    """Take the ready mark back (a mistake, or the gown turned out not to be ready). Nothing is sent to the customer."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    reservation, item = _locked(item_id)
    if item is None or reservation is None:
        return _refuse("That gown was not found. The list will refresh.", status=404)
    if not item.is_ready_for_pickup:
        return _refuse("This gown isn't marked ready any more. The list will refresh.")
    told = item.ready_told_for == item.rental_date and reservation.customer_id is not None
    item.ready_at, item.ready_by, item.ready_for = None, None, None
    item.save(update_fields=["ready_at", "ready_by", "ready_for"])
    message = f"{item.gown_name} is back to To prepare."
    if told:
        message += " The customer was already told it was ready, so let them know."
    return JsonResponse({"success": True, "told": told, "message": message})
