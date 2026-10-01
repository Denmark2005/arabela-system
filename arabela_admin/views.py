import os
import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps

from django.conf import settings
from django.contrib.auth import authenticate, login, logout, get_user_model, update_session_auth_hash
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.db import DataError, IntegrityError, transaction
from django.db.models import Q, Count, Min, Prefetch
from django.shortcuts import redirect, render
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.http import urlencode
from django.views.decorators.http import require_http_methods
import json

from accounts.models import CustomerMessage, UserProfile
from accounts.services import CANCELLATION_FLAG_THRESHOLD
from gowns.models import (
    DEFAULT_CATEGORY_TAG_COLORS,
    GOWN_COLOR_PRESETS,
    Gown,
    GownRemoval,
    GownUnavailability,
    SiteSettings,
    TAG_COLOR_HEX,
    TAG_COLOR_PALETTE,
    resolve_tag_colors,
)
from reservations import reminders as reservation_reminders
from reservations import timeline as reservation_timeline
from reservations.models import Reservation, ReceiptRecord, ReservationItem, ReservationStatusEvent

User = get_user_model()


def _is_admin_staff(request):
    return request.user.is_authenticated and (request.user.is_staff or request.user.is_superuser)


def _require_admin_staff(view_func):
    """Redirects to the admin login page instead of silently rendering the panel for a
    session that isn't actually staff -- the header always shows a fixed placeholder
    name regardless of who's logged in, so without this gate a signed-out/expired
    session can browse the whole panel looking authenticated, then get a confusing
    'Unauthorized' only once a real action (like Verify) is attempted."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not _is_admin_staff(request):
            return redirect("arabela_admin:admin_login")
        return view_func(request, *args, **kwargs)
    return wrapper


def _is_owner(request):
    """Owner = the shop owner, who has full panel access (incl. Staff Management and
    business settings). A superuser is always an owner; otherwise the OWNER role on
    their profile decides. Staff/Manager accounts are everyone else with panel access."""
    user = request.user
    if not (user.is_authenticated and (user.is_staff or user.is_superuser)):
        return False
    if user.is_superuser:
        return True
    profile = UserProfile.objects.filter(user=user).first()
    return bool(profile and profile.role == UserProfile.Role.OWNER)


def _require_owner(view_func):
    """Owner-only gate. Page (GET) views redirect a non-owner staffer to the dashboard;
    API (POST) views return 403 JSON so the front-end can surface a clean error. The
    branch keys off the request method, matching how the existing panel splits page
    renders from fetch() endpoints."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not _is_owner(request):
            if not _is_admin_staff(request):
                return redirect("arabela_admin:admin_login")
            if request.method == "POST":
                return JsonResponse({"error": "Only the owner can do that."}, status=403)
            return redirect("arabela_admin:dashboard")
        return view_func(request, *args, **kwargs)
    return wrapper


@_require_admin_staff
def dashboard_view(request):
    # The shop's stand-in for a nightly cron job. This project has no scheduler, so the
    # once-a-day pick-up/return reminder sweep rides on the one page staff open every
    # day anyway. ReminderRun claims the day atomically, so repeated loads (and two
    # staff loading at once) cost nothing after the first.
    #
    # Wrapped because a reminder is a courtesy and the dashboard is the shop's control
    # panel: if messaging customers ever fails, staff must still get their dashboard.
    try:
        reservation_reminders.run_daily_sweep_if_due()
    except Exception:
        pass

    quick_verify_reservations = (
        Reservation.objects.filter(status=Reservation.Status.PENDING)
        .select_related("customer__profile")
        .order_by("-created_at")[:5]
    )

    # Every figure here is counted the SAME way as the page it links to, so the
    # dashboard can never contradict the detail page a staffer clicks through to --
    # active rentals uses _SCHEDULED_STATUSES (as Active Reservations does), the gown
    # tallies mirror gown_catalog_view, and customers exclude staff/superusers exactly
    # as clients_view does. These replaced hardcoded demo figures (286 / 5,359) that
    # disagreed with the real data by two orders of magnitude.
    gowns = Gown.objects.all()
    gown_total = gowns.count()

    # Reservations This Month: one per BOOKING (not per gown), counted in the month its
    # FIRST gown is picked up (the same first pick-up _reservation_window gives
    # Reservation Records), leaving out bookings that never became rentals. The tile
    # links to Reservation Records' ?month= filter, which applies this exact rule to
    # its own one-row-per-reservation list -- counting gowns here, or by any day of the
    # rental, would make the number on the tile disagree with the rows it opens.
    month_start = timezone.localdate().replace(day=1)
    next_month_start = (month_start + timedelta(days=32)).replace(day=1)
    reservations_this_month = (
        Reservation.objects.exclude(status__in=_NON_RENTAL_STATUSES)
        .annotate(first_pickup=Min("items__rental_date"))
        .filter(first_pickup__gte=month_start, first_pickup__lt=next_month_start)
        .count()
    )

    return render(
        request,
        "arabela_admin/dashboard.html",
        {
            "quick_verify_reservations": quick_verify_reservations,
            "registered_customers": User.objects.filter(
                is_staff=False, is_superuser=False
            ).count(),
            "active_rentals": Reservation.objects.filter(
                status__in=_SCHEDULED_STATUSES
            ).count(),
            "gown_total": gown_total,
            "gown_available": gowns.filter(status=Gown.Status.AVAILABLE).count(),
            "gown_needs_attention": gowns.filter(
                status=Gown.Status.OUT_OF_STOCK
            ).count(),
            # Drives the empty-state call to action: with no gowns the shop cannot take
            # a real booking at all, so it is the single most important thing to surface.
            "catalog_is_empty": gown_total == 0,
            "reservations_this_month": reservations_this_month,
            "reservations_this_month_label": date_format(month_start, "F Y"),
            "reservations_this_month_url": (
                reverse("arabela_admin:reservation_records")
                + "?" + urlencode({"month": month_start.strftime("%Y-%m")})
            ),
        },
    )


# Statuses that represent a live booking shown on the calendar / active list.
_SCHEDULED_STATUSES = [
    Reservation.Status.CONFIRMED,
    Reservation.Status.ACTIVE,
    Reservation.Status.OVERDUE,
]


# Bookings that never became an actual rental, so they must not be counted as one.
# Everything else (Pending/Confirmed/Active/Returned/Overdue) is a real booking that
# occupies a gown. Deliberately the same rule gowns.views._blocked_dates_for_category
# applies, so availability, the Monthly Rentals chart and Rental History all agree on
# what counts -- if these ever diverge the numbers on those three screens contradict.
_NON_RENTAL_STATUSES = [
    Reservation.Status.REJECTED,
    Reservation.Status.CANCELLED,
]


# Colour name -> actual rendered colour is fixed by the compiled CSS
# (event-fc-color.fc-bg-* rules in style.css / calendar.html's style block), NOT by the
# label -- fc-bg-danger renders red and fc-bg-warning renders amber. So Pick-up=Warning
# (amber), Reserved=Success (green), Return=Primary (blue), Overdue=Danger (red).


_STAGE_COLOR = {
    "Pick-up": "Warning",    # amber
    "Reserved": "Success",   # green
    "Return": "Primary",     # blue
    "Overdue": "Danger",     # red
}


def _event_date(item):
    """The customer's event/reserved day -- see ReservationItem.effective_event_date,
    the one shared definition (the customer's own order page reads it too)."""
    return item.effective_event_date


def _overdue_date(item):
    """The day the Overdue status marks. Defaults to the return date for any legacy row
    that has no explicit overdue date."""
    return item.overdue_date or item.return_date


def _original_event_date(item):
    """The event day the customer actually chose at checkout, recovered rather than
    stored directly -- nothing freezes it the way original_rental_date/original_return_date
    freeze the pick-up/return window (see reservations/models.py). Recovered from
    original_return_date - 2, mirroring products.html's own restoreSelectionFrom, which
    recovers the same day the same way for the exact reason given there: return is always
    exactly event + 2, whereas pick-up sometimes gets clamped to today for a booking made
    less than 2 days out (see clampedPickup), so only return round-trips reliably. Falls
    back to the current effective event date for the rare legacy row missing that field."""
    if item.original_return_date:
        return item.original_return_date - timedelta(days=2)
    return _event_date(item)


def _pickup_span(item):
    rental, event = item.rental_date, _event_date(item)
    end_incl = max(rental, event - timedelta(days=1))
    return rental, end_incl


def _reserved_span(item):
    event = _event_date(item)
    return event, event


def _return_span(item):
    event, ret = _event_date(item), item.return_date
    start = min(ret, event + timedelta(days=1))
    end_incl = ret
    if end_incl < start:
        start = end_incl = ret
    return start, end_incl


def _overdue_span(item):
    """Red from the day after the return date up to today -- it grows by itself each day
    the gown is still out."""
    start = item.return_date + timedelta(days=1)
    return start, max(start, timezone.localdate())


def _effective_stage(item, today=None):
    """The stage staff should SEE. Overdue is never chosen by hand: a gown that is out
    (Reserved) and whose return date has passed is Overdue, and one marked Overdue before
    its return date has passed is simply still out. Pick-up / Returned are unchanged."""
    today = today or timezone.localdate()
    if item.stage in (ReservationItem.Stage.RESERVED, ReservationItem.Stage.OVERDUE):
        if today > item.return_date:
            return ReservationItem.Stage.OVERDUE
        return ReservationItem.Stage.RESERVED
    return item.stage


def _stage_segments(item):
    """Which marker(s) show on the calendar for the booking -- cumulative, so the calendar
    always shows the full picture so far:
      Pick-up  -> Pick-up only (nothing else is relevant until the gown is out)
      Reserved -> Reserved AND Return together (it's out + when it's due back)
      Overdue  -> Reserved + Return + Overdue (derived from the return date, see
                  _effective_stage; it adds on top, it never replaces the history)
    Each entry is (label, span_fn). The pick-up / return spans follow the booking's
    CURRENT dates, so a pick-up the customer moved earlier is painted in the Pick-up
    colour and a later return in the Return colour -- not as a grey block."""
    stage = _effective_stage(item)
    if stage == ReservationItem.Stage.PICKUP:
        return [("Pick-up", _pickup_span)]
    if stage == ReservationItem.Stage.RESERVED:
        return [("Reserved", _reserved_span), ("Return", _return_span)]
    if stage == ReservationItem.Stage.OVERDUE:
        return [("Reserved", _reserved_span), ("Return", _return_span), ("Overdue", _overdue_span)]
    return []  # RETURNED (or anything unexpected) -- nothing shown


def _plural_days(n):
    return f"{n} day{'s' if n != 1 else ''}"


def _item_remarks(item, today=None):
    """The status remarks staff read, as [{"text", "tone"}] -- ONE definition used by both
    Active Reservations and the (read-only) Rental Schedule, so the two can never word or
    colour the same booking differently. tone: warning (orange, pick-up), success (green,
    out), primary (blue, return), danger (red, late/overdue), muted (grey, plain info).

    Early / late are always measured against what the customer ORIGINALLY booked
    (original_rental_date / original_return_date), the figures the Php 200/day charge is
    counted from -- so a pick-up the customer moved to the 10th keeps saying "6 days
    early" even if they finally collect it on the 17th, and a late return keeps counting
    from the original return date. Deliberately just day counts, no peso amounts."""
    today = today or timezone.localdate()
    scheduled_pickup = item.original_rental_date or item.rental_date
    scheduled_return = item.original_return_date or item.return_date
    stage = _effective_stage(item, today)
    remarks = []

    if stage == ReservationItem.Stage.RETURNED:
        when = item.returned_on
        remarks.append({"text": f"Returned {when:%b %d}" if when else "Returned", "tone": "success"})
        if when and when > scheduled_return:
            remarks.append({"text": f"{_plural_days((when - scheduled_return).days)} late", "tone": "danger"})
    elif stage == ReservationItem.Stage.PICKUP:
        days = (item.rental_date - today).days
        if days > 0:
            remarks.append({"text": f"Pick-up in {_plural_days(days)}", "tone": "muted"})
        elif days == 0:
            remarks.append({"text": "Pick up today", "tone": "warning"})
        else:
            remarks.append({"text": f"{_plural_days(-days)} late for pick-up", "tone": "danger"})
    else:  # out with the customer
        remarks.append({"text": "Out with customer", "tone": "success"})
        if stage == ReservationItem.Stage.OVERDUE:
            remarks.append({
                "text": f"Overdue · {_plural_days((today - scheduled_return).days)} late", "tone": "danger",
            })
        else:
            days = (item.return_date - today).days
            remarks.append({
                "text": "Return due today" if days == 0 else f"Return in {_plural_days(days)}",
                "tone": "primary",
            })

    moved = (scheduled_pickup - item.rental_date).days
    if moved > 0:
        remarks.append({"text": f"Changed pick-up date by customer · {_plural_days(moved)} early", "tone": "warning"})
    elif moved < 0:
        remarks.append({"text": f"Pick-up moved {_plural_days(-moved)} later", "tone": "muted"})
    extended = (item.return_date - scheduled_return).days
    if extended > 0:
        remarks.append({"text": f"Changed return date by customer · {_plural_days(extended)} late", "tone": "primary"})
    elif extended < 0:
        remarks.append({"text": f"Return moved {_plural_days(-extended)} earlier", "tone": "muted"})
    return remarks


def _holding_items(gown_ids):
    """Bookings that really hold these physical gowns -- the same rule checkout's
    _find_available_unit and the customer calendar use: every booking except one
    already Returned, or whose reservation was Rejected/Cancelled (those never held
    stock). Pending ones count: they're awaiting payment approval, not released."""
    return (
        ReservationItem.objects.filter(gown_id__in=gown_ids)
        .exclude(stage=ReservationItem.Stage.RETURNED)
        .exclude(reservation__status__in=_NON_RENTAL_STATUSES)
    )


def _schedule_conflict(item, rental_date, return_date):
    """What (if anything) already holds THIS physical gown on any day of
    rental_date..return_date, besides the booking being moved -- as a message naming
    it, or None. Inclusive on both ends, like checkout: a booking ending on the 12th
    collides with one starting on the 12th."""
    clash = (
        _holding_items([item.gown_id])
        .filter(rental_date__lte=return_date, return_date__gte=rental_date)
        .exclude(id=item.id)
        .select_related("reservation__customer__profile")
        .order_by("rental_date")
        .first()
    )
    if clash:
        return (
            f"{item.gown_name} is already booked {clash.rental_date:%b %d} – "
            f"{clash.return_date:%b %d, %Y} for {clash.reservation.display_customer_name} "
            f"({clash.reservation.reference_code}). Pick dates that don't overlap."
        )
    block = (
        GownUnavailability.objects.filter(
            gown_id=item.gown_id, start_date__lte=return_date, end_date__gte=rental_date,
        )
        # This item's OWN trailing cooldown isn't a foreign obstacle -- it's about to
        # be moved to sit after whatever return_date is being saved right now (see
        # _resync_cooldown_block below), so it must never block its own pick-up / return change.
        .exclude(auto_for_item=item)
        .order_by("start_date").first()
    )
    if block:
        return (
            f"{item.gown_name} is blocked for {block.get_reason_display().lower()} "
            f"{block.start_date:%b %d} – {block.end_date:%b %d, %Y}. Pick dates that "
            f"don't overlap."
        )
    return None


def _resync_cooldown_block(item, base_date):
    """Move this item's auto-created cooldown block (if it still has one) to sit
    right after base_date, matching reservation_submit's own [+1, +3] window.

    If staff already released/deleted it, that choice is respected -- this never
    recreates one. getattr is safe here: Django raises RelatedObjectDoesNotExist
    (an AttributeError subclass) for a reverse one-to-one with nothing on the other
    end, exactly like a missing plain attribute."""
    block = getattr(item, "auto_cooldown_block", None)
    if block is None:
        return
    block.start_date = base_date + timedelta(days=1)
    block.end_date = base_date + timedelta(days=3)
    block.save(update_fields=["start_date", "end_date"])


def _other_holds_by_gown(gown_ids):
    """Every upcoming booking and block per physical gown, oldest first -- shown in the
    Booking Details modal as "This gown is also booked", so staff moving a date can see
    what's in the way without leaving the page. Only windows that haven't fully ended
    yet; the server-side check (_schedule_conflict) still covers everything."""
    today = timezone.localdate()
    holds = defaultdict(list)
    for it in (
        _holding_items(gown_ids).filter(return_date__gte=today)
        .select_related("reservation__customer__profile")
    ):
        label = f"{it.reservation.display_customer_name} ({it.reservation.reference_code})"
        if it.reservation.status == Reservation.Status.PENDING:
            label += " · awaiting payment approval"
        holds[it.gown_id].append({
            "itemId": it.id, "start": it.rental_date.isoformat(),
            "end": it.return_date.isoformat(), "label": label,
        })
    for block in GownUnavailability.objects.filter(gown_id__in=gown_ids, end_date__gte=today):
        holds[block.gown_id].append({
            # A block's own auto_for_item_id (None for a manual staff block) lets the
            # calendar recognize "this is the very item I'm viewing's own cooldown",
            # instead of always reading as an unrelated hold.
            "itemId": block.auto_for_item_id, "start": block.start_date.isoformat(),
            "end": block.end_date.isoformat(), "label": f"Blocked · {block.get_reason_display()}",
        })
    for gown_holds in holds.values():
        gown_holds.sort(key=lambda h: h["start"])
    return holds


def _item_gown_tag(item, tag_colors):
    """(gown_id, tag color name, tag hex) for the exact physical gown a booking item is
    matched to -- what staff need to pick the RIGHT dress off the rack at pick-up when
    several gowns share a name. All three are '' for an item with no matched gown, so
    callers can render it unconditionally. Expects item.gown to be prefetched."""
    gown = item.gown if item.gown_id else None
    if gown is None:
        return "", "", ""
    color = tag_colors.get(gown.category, "")
    return gown.gown_id, color, TAG_COLOR_HEX.get(color, "")


def _calendar_events(reservations, holds_by_gown=None, tag_colors=None):
    """Each active booking renders as one or more markers depending on its current status
    (see _stage_segments) -- Reserved and Overdue build UP on what came before instead of
    replacing it, so the calendar always shows the whole story for that booking so far.
    Every marker for the same booking shares the same itemId/customer/gown/reference/stage,
    so clicking any of them opens the same booking panel. Returned bookings drop off."""
    events = []
    tag_colors = tag_colors or {}
    for reservation in reservations:
        for item in reservation.items.all():
            label = f"{reservation.display_customer_name} — {item.gown_name}"
            gown_code, tag_color, tag_hex = _item_gown_tag(item, tag_colors)
            base_props = {
                "itemId": item.id,
                "customer": reservation.display_customer_name,
                "reference": reservation.reference_code,
                "gownName": item.gown_name,
                # The exact physical gown this booking is matched to, and the color of
                # its category's tag -- shown in the Booking Details modal (see
                # calendar.html) so staff can match the booking to the dress by its tag.
                "gownCode": gown_code,
                "tagColor": tag_color,
                "tagHex": tag_hex,
                "stage": _effective_stage(item),
                # Read-only summary in the Booking Details window: the same remarks Active
                # Reservations shows, and where to go to actually act on this booking.
                "remarks": _item_remarks(item),
                "activeUrl": (
                    reverse("arabela_admin:active_reservations") + "?" + urlencode({"search": reservation.reference_code})
                ),
                "originalRentalDate": (item.original_rental_date or item.rental_date).isoformat(),
                "originalReturnDate": (item.original_return_date or item.return_date).isoformat(),
                "rentalDate": item.rental_date.isoformat(),
                "eventDate": _event_date(item).isoformat(),
                "returnDate": item.return_date.isoformat(),
                "overdueDate": _overdue_date(item).isoformat(),
                # What the customer actually picked, so the Booking Details modal can
                # show it whenever a reschedule has since moved eventDate away from it --
                # see calendar.html's own subtitle-note script, which reads this straight
                # out of this same JSON (calendar-init/bundle.js never touches it).
                "bookedEventDate": _original_event_date(item).isoformat(),
                # Everything else holding this same physical gown (see
                # _other_holds_by_gown) -- the modal lists it and warns live when a
                # moved date runs into one (the server's _schedule_conflict refuses it).
                "otherHolds": [
                    h for h in (holds_by_gown or {}).get(item.gown_id, [])
                    if h["itemId"] != item.id
                ] if item.gown_id else [],
            }
            for marker_label, span_fn in _stage_segments(item):
                start, end_incl = span_fn(item)
                events.append({
                    "id": f"resv-{item.id}-{marker_label.lower()}",
                    "title": f"{marker_label} · {label}",
                    "start": start.isoformat(),
                    "end": (end_incl + timedelta(days=1)).isoformat(),
                    "allDay": True,
                    "extendedProps": {**base_props, "calendar": _STAGE_COLOR[marker_label]},
                })
    return events


def _unavailability_events(blocks):
    """Maintenance blocks alongside the bookings, so the schedule answers "can this gown
    go out that day?" in one place instead of making staff cross-check the catalog.

    Deliberately carries no itemId: the calendar's click handler bails out early without
    one (see bundle.js bookingEventClick), so these render but stay read-only -- they're
    edited from the gown's own row in the Gown Catalog, which is where they're created.

    The title deliberately never includes block.note -- FullCalendar renders it as one
    unbroken line, in both the day cell and its own "+N more" popover, neither of which
    wrap or truncate cleanly. The auto-cooldown note in particular is a full sentence
    ("Auto-added post-rental cooldown, backfilled for a booking made before this rule
    shipped.") that overflowed the row and got cut off mid-word. The full note is still
    readable in its proper place -- the block's row in Gown Catalog's Blocked Dates list,
    which actually has room to wrap it -- so nothing is lost, just moved off a row too
    narrow for it."""
    events = []
    for block in blocks:
        events.append({
            "id": f"block-{block.id}",
            "title": f"{block.reason} · {block.gown.gown_id} — {block.gown.name}",
            "start": block.start_date.isoformat(),
            "end": (block.end_date + timedelta(days=1)).isoformat(),
            "allDay": True,
            "extendedProps": {"calendar": "Blocked", "blockId": block.id},
        })
    return events


@_require_admin_staff
def rental_schedule_view(request):
    reservations = list(
        Reservation.objects.filter(status__in=_SCHEDULED_STATUSES)
        .select_related("customer__profile")
        .prefetch_related("items__gown")
    )
    blocks = GownUnavailability.objects.filter(
        end_date__gte=timezone.localdate()
    ).select_related("gown")
    gown_ids = {item.gown_id for r in reservations for item in r.items.all() if item.gown_id}
    holds_by_gown = _other_holds_by_gown(gown_ids)
    tag_colors = SiteSettings.load().tag_colors()
    return render(
        request,
        "arabela_admin/calendar.html",
        {
            "page": "rental",
            "calendar_events": (
                _calendar_events(reservations, holds_by_gown, tag_colors)
                + _unavailability_events(blocks)
            ),
        },
    )


@_require_admin_staff
def payment_verification_view(request):
    reservations = (
        Reservation.objects.all()
        .select_related("customer__profile")
        .order_by("-created_at")
    )
    now = timezone.now()
    verified_statuses = [
        Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE,
        Reservation.Status.RETURNED, Reservation.Status.OVERDUE,
    ]
    # "Verified Today" -- a same-day follow-up list so a payment that gets
    # verified doesn't just vanish from Pending with nothing left to check.
    # Deliberately scoped to today only (not the whole month, unlike the stat
    # tile above): this is meant to be glanced at and cleared the same day,
    # not accumulate into a second, bigger backlog of its own.
    verified_today = list(
        reservations.filter(
            status__in=verified_statuses,
            reviewed_at__date=timezone.localdate(),
        )
        .prefetch_related("items")
        .order_by("-reviewed_at")
    )
    return render(
        request,
        "arabela_admin/payment-verification.html",
        {
            "page": "payment-verification",
            "reservations": reservations,
            "verified_today": verified_today,
            "pending_review_count": reservations.filter(status=Reservation.Status.PENDING).count(),
            "verified_this_month_count": reservations.filter(
                status__in=verified_statuses,
                reviewed_at__year=now.year, reviewed_at__month=now.month,
            ).count(),
            "rejected_count": reservations.filter(status=Reservation.Status.REJECTED).count(),
        },
    )


@_require_admin_staff
def rental_history_view(request):
    """Every gown that has gone out, one row per gown (ReservationItem), bucketed by
    rental_date and excluding bookings that never became rentals.

    Counts GOWNS, not bookings -- a three-gown reservation is three rows here but one
    in Reservation Records' monthly breakdown and the dashboard's Reservations This
    Month tile, so this page's month totals are expected to be higher than those.
    """
    today = timezone.localdate()
    items = list(
        ReservationItem.objects.exclude(
            reservation__status__in=_NON_RENTAL_STATUSES
        )
        .select_related("reservation__customer__profile")
        .order_by("-rental_date", "-id")
    )

    # Completed / Ongoing / Overdue are display-only labels -- they exist in neither
    # Reservation.Status nor ReservationItem.Stage -- so they are derived once here
    # rather than re-expressed as template conditionals in every place they appear
    # (badge, modal payload, search haystack).
    for item in items:
        if item.stage == ReservationItem.Stage.RETURNED:
            item.history_status = "Completed"
        elif item.return_date < today:
            item.history_status = "Overdue"
        else:
            item.history_status = "Ongoing"

    reservation_timeline.attach_to_items(items)

    # Mirrors the visible rows so the "Total Rentals" card can count what is actually
    # on screen. `q` is the same lowercase haystack the row filter searches, so the
    # card and the table can never disagree about what matches.
    rental_records = [
        {
            "month": item.rental_date.strftime("%b"),
            "year": item.rental_date.strftime("%Y"),
            "q": f"{item.reservation.display_customer_name} {item.gown_name}".lower(),
        }
        for item in items
    ]

    return render(
        request,
        "arabela_admin/rental-history.html",
        {
            "page": "rental-history",
            "rental_items": items,
            "rental_records": rental_records,
        },
    )


@_require_admin_staff
def security_deposits_view(request):
    reservations = list(
        Reservation.objects.filter(status__in=_SCHEDULED_STATUSES)
        .select_related("customer__profile")
        .prefetch_related("items")
    )
    for r in reservations:
        r.all_items_returned = all(
            i.stage == ReservationItem.Stage.RETURNED for i in r.items.all()
        )

    now = timezone.now()
    held_qs = [r for r in reservations if not r.deposit_returned_at]
    returned_this_month = sum(
        1 for r in reservations
        if r.deposit_returned_at
        and r.deposit_returned_at.year == now.year
        and r.deposit_returned_at.month == now.month
    )
    total_held_value = sum((r.security_deposit for r in held_qs), Decimal("0"))

    return render(
        request,
        "arabela_admin/security-deposits.html",
        {
            "page": "security-deposits",
            "reservations": reservations,
            "held_count": len(held_qs),
            "returned_this_month": returned_this_month,
            "total_held_value": total_held_value,
        },
    )


@require_http_methods(["POST"])
def reservation_return_deposit_view(request, pk):
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        reservation = Reservation.objects.prefetch_related("items").get(id=pk)
    except Reservation.DoesNotExist:
        return JsonResponse({"error": "Reservation not found"}, status=404)

    if reservation.deposit_returned_at:
        return JsonResponse({"error": "Deposit already returned."}, status=400)
    if reservation.items.exclude(stage=ReservationItem.Stage.RETURNED).exists():
        return JsonResponse(
            {"error": "Gown must be marked returned before releasing the deposit."}, status=400
        )

    reservation.deposit_returned_at = timezone.now()
    reservation.save(update_fields=["deposit_returned_at", "updated_at"])
    ReservationStatusEvent.record(
        reservation, "Security deposit returned",
        detail="Your deposit has been released. This reservation is complete.",
        actor=ReservationStatusEvent.Actor.STAFF,
    )
    return JsonResponse({
        "success": True,
        "deposit_returned_at": reservation.deposit_returned_at.isoformat(),
    })


@require_http_methods(["POST"])
@_require_admin_staff
def receipt_upload_view(request):
    """Attach a photo of a manually-issued receipt to a real reservation.

    The customer name is never typed by staff -- it's resolved from the reservation
    the reference code points to (Reservation.display_customer_name, the same
    always-current name every other admin screen shows), so a receipt can never end
    up filed under a name that doesn't match its own booking.
    """
    reference_code = (request.POST.get("reference_code") or "").strip().upper()
    if not reference_code:
        return JsonResponse({"error": "Please enter the reservation's reference code."}, status=400)

    try:
        reservation = Reservation.objects.select_related("customer__profile").get(
            reference_code=reference_code
        )
    except Reservation.DoesNotExist:
        return JsonResponse(
            {"error": f"No reservation found with reference code {reference_code}."}, status=404
        )

    photo_file = request.FILES.get("photo")
    error = _validate_receipt_photo(photo_file)
    if error:
        return JsonResponse({"error": error}, status=400)

    try:
        photo_url = _save_receipt_photo(photo_file)
    except Exception:
        return JsonResponse(
            {"error": "The receipt photo couldn't be uploaded just now. Please try again."},
            status=502,
        )

    receipt = ReceiptRecord.objects.create(
        reservation=reservation, photo_url=photo_url, uploaded_by=request.user,
    )
    return JsonResponse({"success": True, "receipt": _receipt_row(receipt)})


@require_http_methods(["POST"])
@_require_admin_staff
def receipt_replace_view(request, receipt_id):
    """Correct a mistake on an already-attached receipt photo -- the wrong file, a bad
    scan, etc. Deliberately leaves uploaded_at/uploaded_by alone: this is fixing the
    existing record, not creating a new one."""
    try:
        receipt = ReceiptRecord.objects.select_related(
            "reservation__customer__profile", "uploaded_by"
        ).get(id=receipt_id)
    except ReceiptRecord.DoesNotExist:
        return JsonResponse({"error": "Receipt not found."}, status=404)

    photo_file = request.FILES.get("photo")
    error = _validate_receipt_photo(photo_file)
    if error:
        return JsonResponse({"error": error}, status=400)

    try:
        photo_url = _save_receipt_photo(photo_file)
    except Exception:
        return JsonResponse(
            {"error": "The receipt photo couldn't be uploaded just now. Please try again."},
            status=502,
        )

    receipt.photo_url = photo_url
    receipt.save(update_fields=["photo_url"])
    return JsonResponse({"success": True, "receipt": _receipt_row(receipt)})


# --- Reservation Records -------------------------------------------------------------
# One row per reservation, with everything a staff member needs to answer a customer's
# question in one place: the rental dates, where the booking actually is, the deposit,
# the customer's own payment proof, and the shop's manual receipts (upload/view/replace
# all live right here now -- the standalone Receipt Records page this used to just link
# out to was folded into this page and removed). Nothing here is new business logic --
# every value is read from the same fields the pages it links to (Security Deposits,
# Active Reservations) already use.

def _reservation_window(items):
    """(first pick-up, last return) across every gown in the booking -- a multi-gown
    reservation can have a different window per gown."""
    if not items:
        return None, None
    return min(i.rental_date for i in items), max(i.return_date for i in items)


def _gown_summary(items):
    """'Wedding Gown 14' or 'Wedding Gown 14 + 2 more' for a one-line table cell."""
    if not items:
        return "No gowns"
    first = items[0].gown_name
    return first if len(items) == 1 else f"{first} + {len(items) - 1} more"


def _reservation_progress(reservation, items, today):
    """One plain label for where this booking actually is right now.

    reservation.status alone can't say: nothing moves it past Confirmed once the gown
    goes out or comes back -- the gown's own stage tracks that -- so a finished rental
    would still read "Confirmed". Built from the same fields every other page reads;
    "Completed"/"Overdue" follow Rental History's own rule (Returned stage, or an
    unreturned gown past its return date)."""
    if reservation.status == Reservation.Status.PENDING:
        return "Pending approval"
    if reservation.status in _NON_RENTAL_STATUSES:
        return reservation.status
    if not items:
        return reservation.status
    returned = [i.stage == ReservationItem.Stage.RETURNED for i in items]
    if all(returned):
        return "Completed"
    if any(
        i.stage != ReservationItem.Stage.RETURNED
        and (i.stage == ReservationItem.Stage.OVERDUE or i.return_date < today)
        for i in items
    ):
        return "Overdue"
    if any(i.stage in (ReservationItem.Stage.RESERVED, ReservationItem.Stage.RETURN) for i in items):
        return "With customer"
    if any(returned):
        return "Partly returned"
    return "Awaiting pickup"


def _deposit_status(reservation):
    """Where the P2,000 deposit stands. Only Confirmed-and-later bookings actually hold
    one (the same set Security Deposits lists); Pending ones haven't been verified yet."""
    if reservation.deposit_returned_at:
        return "Returned"
    if reservation.status in _SCHEDULED_STATUSES:
        return "Held"
    if reservation.status == Reservation.Status.PENDING:
        return "Awaiting verification" if reservation.payment_proof_url else "Not paid"
    return "Not held"


def _peso(amount):
    return f"₱{amount:,.2f}"


@_require_admin_staff
def reservation_records_view(request):
    today = timezone.localdate()
    reservations = reservation_timeline.attach_to_reservations(
        Reservation.objects.select_related("customer__profile")
        .prefetch_related(
            Prefetch("items", queryset=ReservationItem.objects.select_related("gown").order_by("rental_date", "id")),
            Prefetch(
                "receipt_records",
                queryset=ReceiptRecord.objects.select_related("uploaded_by").order_by("-uploaded_at"),
            ),
        )
        .order_by("-created_at")
    )

    tag_colors = SiteSettings.load().tag_colors()
    records = []
    for r in reservations:
        items = list(r.items.all())
        start, end = _reservation_window(items)
        deposit_status = _deposit_status(r)
        booked = timezone.localtime(r.created_at)
        records.append({
            "id": r.id,
            "reference": r.reference_code,
            "customer": r.display_customer_name,
            "customerId": r.customer_id,
            "email": r.customer.email,
            "phone": r.phone,
            "bookedAs": r.booked_as_name,
            "booked": date_format(booked, "M j, Y"),
            "progress": _reservation_progress(r, items, today),
            "start": start.isoformat() if start else booked.date().isoformat(),
            "end": end.isoformat() if end else booked.date().isoformat(),
            "startDisplay": date_format(start, "M j, Y") if start else "—",
            "endDisplay": date_format(end, "M j, Y") if end else "—",
            # The one month this booking counts under in the monthly breakdown, the
            # ?month= filter, and the dashboard's Reservations This Month tile: the
            # month its FIRST gown is picked up (see dashboard_view). Blank for a
            # booking with no gowns, so it is never counted anywhere.
            "pickupMonth": start.strftime("%Y-%m") if start else "",
            # Rejected/Cancelled bookings never became rentals -- still listed here like
            # every other booking, but left out of those monthly counts.
            "countsAsRental": r.status not in _NON_RENTAL_STATUSES,
            "gownSummary": _gown_summary(items),
            "items": [
                {
                    "gown": i.gown_name,
                    "gownCode": i.gown.gown_id if i.gown_id else "",
                    # Color of the category tag on this exact gown, so the booking shows
                    # which tag to look for -- same helper the other booking screens use.
                    "tagColor": _item_gown_tag(i, tag_colors)[1],
                    "tagHex": _item_gown_tag(i, tag_colors)[2],
                    "size": i.size or "TBD",
                    "pickup": date_format(i.rental_date, "M j, Y"),
                    "event": date_format(i.effective_event_date, "M j, Y"),
                    "ret": date_format(i.return_date, "M j, Y"),
                    "stage": i.stage,
                    "pickedUpOn": date_format(i.picked_up_on, "M j, Y") if i.picked_up_on else "",
                    "returnedOn": date_format(i.returned_on, "M j, Y") if i.returned_on else "",
                }
                for i in items
            ],
            "subtotal": _peso(r.rental_subtotal),
            "deposit": _peso(r.security_deposit),
            "total": _peso(r.total_amount),
            "paymentState": r.payment_state,
            "paymentProofUrl": r.payment_proof_url,
            "depositStatus": deposit_status,
            "depositReturnedOn": (
                date_format(timezone.localtime(r.deposit_returned_at), "M j, Y")
                if r.deposit_returned_at else ""
            ),
            # Wherever staff would actually act on this deposit's CURRENT state: Held/
            # Returned bookings live on Security Deposits; a booking still Awaiting
            # verification isn't on that page at all yet (it only lists Confirmed-and-
            # later bookings) -- that one's payment proof is reviewed on Payment
            # Verification instead, which already reads this exact ?search= param.
            "depositLink": (
                reverse("arabela_admin:security_deposits") + "?" + urlencode({"search": r.reference_code})
                if deposit_status in ("Held", "Returned")
                else reverse("arabela_admin:payment_verification") + "?" + urlencode({"search": r.reference_code})
                if deposit_status == "Awaiting verification"
                else ""
            ),
            "depositLinkLabel": (
                "Open in Security Deposits" if deposit_status in ("Held", "Returned")
                else "Open in Payment Verification" if deposit_status == "Awaiting verification"
                else ""
            ),
            "receipts": [_receipt_row(rec, today) for rec in r.receipt_records.all()],
            # Everything the search box should match, lower-cased once here.
            "q": " ".join(filter(None, [
                r.display_customer_name, r.booked_as_name, r.customer.email, r.phone,
                r.reference_code, *[i.gown_name for i in items],
                *[i.gown.gown_id for i in items if i.gown_id],
            ])).lower(),
        })

    # The shop's own calendar (TIME_ZONE = Asia/Manila), not the viewer's browser clock,
    # decides what "today" / "this week" / "this month" mean for the date filter.
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    return render(
        request,
        "arabela_admin/reservation-records.html",
        {
            "page": "reservation-records",
            "records": records,
            "reservations": reservations,
            "date_ranges": {
                "today": today.isoformat(),
                "weekStart": week_start.isoformat(),
                "weekEnd": (week_start + timedelta(days=6)).isoformat(),
                "monthStart": month_start.isoformat(),
                "monthEnd": month_end.isoformat(),
                "thisMonth": month_start.strftime("%Y-%m"),
            },
        },
    )


def receipt_reservation_search_view(request):
    """Live lookup behind Reservation Records' "find the reservation" box: staff type
    part of a customer's name, email, a reference code, or a gown, and pick the booking
    from the results -- instead of having to know its exact reference code by heart.
    Read-only; the upload itself still goes through receipt_upload_view unchanged."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    query = (request.GET.get("q") or "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})

    matches = list(
        Reservation.objects.filter(
            Q(customer_name__icontains=query)
            | Q(reference_code__icontains=query)
            | Q(customer__profile__display_name__icontains=query)
            | Q(customer__first_name__icontains=query)
            | Q(customer__last_name__icontains=query)
            | Q(customer__email__icontains=query)
            | Q(items__gown_name__icontains=query)
        )
        .distinct()
        .select_related("customer__profile")
        .prefetch_related(
            Prefetch("items", queryset=ReservationItem.objects.order_by("rental_date", "id"))
        )
        .order_by("-created_at")[:8]
    )
    receipt_counts = {
        row["reservation_id"]: row["n"]
        for row in ReceiptRecord.objects.filter(reservation_id__in=[r.id for r in matches])
        .values("reservation_id")
        .annotate(n=Count("id"))
    }

    today = timezone.localdate()
    results = []
    for r in matches:
        items = list(r.items.all())
        start, end = _reservation_window(items)
        results.append({
            "reference": r.reference_code,
            "customer": r.display_customer_name,
            "email": r.customer.email,
            "dates": (
                f"{date_format(start, 'M j')} – {date_format(end, 'M j, Y')}" if start else "No dates"
            ),
            "gownSummary": _gown_summary(items),
            "progress": _reservation_progress(r, items, today),
            "receiptCount": receipt_counts.get(r.id, 0),
        })
    return JsonResponse({"results": results})


@_require_admin_staff
def active_reservations_view(request):
    reservations = reservation_timeline.attach_to_reservations(
        Reservation.objects.filter(status__in=_SCHEDULED_STATUSES)
        .select_related("customer__profile")
        .prefetch_related("items__gown")
    )

    # The wording staff see pre-filled in the Send Reminder dialog. Computed here rather
    # than in the browser so the manual message and the automatic one are produced by the
    # exact same rules -- staff should never be offered text the system itself wouldn't send.
    today = timezone.localdate()
    tag_colors = SiteSettings.load().tag_colors()
    holds_by_gown = _other_holds_by_gown(
        {i.gown_id for r in reservations for i in r.items.all() if i.gown_id}
    )
    item_holds = {}
    for reservation in reservations:
        for item in reservation.items.all():
            # The exact gown this line is matched to + its tag color, shown under the
            # gown name so staff at pick-up can find the right dress among look-alikes.
            item.gown_code, item.tag_color, item.tag_hex = _item_gown_tag(item, tag_colors)
            item.suggested_reminder = reservation_reminders.suggested_message(item, today)
            # What the customer originally booked (never touched by a later change --
            # see the model) next to the current dates, plus the status remarks. The
            # early / late counts the Php 200/day charge is based on come from comparing
            # the two; deliberately day counts only, no peso amounts.
            item.scheduled_pickup = item.original_rental_date or item.rental_date
            item.scheduled_return = item.original_return_date or item.return_date
            item.effective_stage = _effective_stage(item, today)
            item.remarks = _item_remarks(item, today)
            # Everything else holding this same physical gown, so the Change pick-up /
            # return calendars can grey out the days that are taken and say why. The
            # server still decides (_schedule_conflict); this is only the early guide.
            item_holds[item.id] = [
                h for h in holds_by_gown.get(item.gown_id, []) if h["itemId"] != item.id
            ] if item.gown_id else []

        # The Status column shows this, not the raw reservation.status: a stage
        # change made on the Rental Schedule calendar (item.stage) is otherwise
        # invisible here, since nothing ever writes it back onto the reservation
        # itself -- reservation.status only ever becomes Overdue if something
        # explicitly sets it, which nothing in this codebase does. Computed fresh
        # on every load instead of stored, so it can never go stale, and reused
        # by Payment Verification, Security Deposits, the reminder sweep, etc.
        # exactly as before -- only what THIS page displays changes.
        reservation.has_overdue_item = any(
            item.effective_stage == ReservationItem.Stage.OVERDUE for item in reservation.items.all()
        )

    return render(
        request,
        "arabela_admin/active-reservations.html",
        {
            "page": "active", "reservations": reservations, "item_holds": item_holds,
            # The shop's own "today" (Asia/Manila), so the calendars grey out past days by
            # the same clock the server enforces -- not the browser's.
            "today_iso": today.isoformat(),
        },
    )


@_require_admin_staff
def pending_approval_view(request):
    reservations = reservation_timeline.attach_to_reservations(
        Reservation.objects.filter(status=Reservation.Status.PENDING)
        .select_related("customer__profile")
        .prefetch_related("items__gown")
    )
    tag_colors = SiteSettings.load().tag_colors()
    for reservation in reservations:
        for item in reservation.items.all():
            item.gown_code, item.tag_color, item.tag_hex = _item_gown_tag(item, tag_colors)
    return render(
        request,
        "arabela_admin/pending-approval.html",
        {"page": "pending", "reservations": reservations},
    )


# A gown counts as "due for a check" once nobody has confirmed it's physically there
# for this many days (or never has).
CHECK_STALE_DAYS = 7
_REMOVAL_LOG_LIMIT = 500


def _decorate_gowns_for_catalog(gowns, tag_colors, now):
    """Sets the per-gown display fields the catalog needs -- tag color, the number staff
    read off the tag, the search text, and how long since the last physical check --
    directly on each Gown instance, so the table row and the client-side filter data
    (gowns_min) are both built from the SAME values and can never disagree.

    The search text is one lowercase string per gown: ID, name, category, real color,
    size, tag color, and the tag number in every form staff might type it ("012",
    "12", "#12"). The catalog splits what staff type into words and requires every word
    to appear, so "white 012" or "blue #4" both work."""
    for g in gowns:
        g.tag_color = tag_colors.get(g.category, "White")
        g.tag_hex = TAG_COLOR_HEX.get(g.tag_color, "#FFFFFF")
        number = g.tracking_number
        g.tag_number = f"{number:03d}" if number is not None else "—"
        # "tag" / "no." are in there so a natural phrase like "white tag 12" or "tag no. 012"
        # (every word must match) finds the gown, not just the bare number.
        parts = [g.gown_id, g.name, g.category, g.color_name, g.size, g.tag_color, "tag"]
        if number is not None:
            parts += ["no.", f"{number:03d}", str(number), f"#{number}"]
        g.search_hay = " ".join(parts).lower()
        if g.last_checked_at is None:
            g.days_since_check = None
            g.check_label = "Never checked"
        else:
            g.days_since_check = max(0, (now - g.last_checked_at).days)
            when = timezone.localtime(g.last_checked_at)
            who = _staff_display_name(g.last_checked_by) if g.last_checked_by_id else ""
            g.check_label = f"Checked {when:%b} {when.day}" + (f" by {who}" if who else "")


@_require_admin_staff
def gown_catalog_view(request):
    today = timezone.localdate()
    now = timezone.now()
    site_settings = SiteSettings.load()
    tag_colors = site_settings.tag_colors()
    # one query (plus the staff name for "checked by"); iterated by the row loop AND below
    gowns = list(Gown.objects.select_related("last_checked_by"))
    # Category, then the number staff read off the tag -- so a category's gowns list in
    # the order they were added, instead of grouped by color with the numbers jumping
    # around inside each color. Gowns with no readable number sink to the end of their
    # category; the ID is only the tiebreaker (old IDs repeat 001 across colors).
    gowns.sort(key=lambda g: (
        g.category,
        g.tracking_number if g.tracking_number is not None else 10**9,
        g.gown_id,
    ))
    _decorate_gowns_for_catalog(gowns, tag_colors, now)

    # Minimal per-gown data the catalog's Alpine layer needs for the client-side
    # "no gowns match your filters" count and the select-all-visible checkbox. Kept
    # separate from the rendered rows, but built from the same `gowns` list so the
    # two can never drift. `hay` is the row's own search haystack.
    gowns_min = [
        {
            "id": g.id,
            "category": g.category,
            "status": g.status,
            "hay": g.search_hay,
            # None = never checked. Lets the "not checked lately" filter count and
            # select-all-visible agree with which rows the table actually shows.
            "check_days": g.days_since_check,
            "check_label": g.check_label,
            # color_name/color_code: lets the Add/Edit modal warn about a code clash
            # (and suggest a free one) against every REAL gown already in the catalog,
            # not just the 36 presets, without a separate request for that check.
            "color_name": g.color_name,
            "color_code": g.color_code,
        }
        for g in gowns
    ]

    # Keyed by gown pk so the catalog's Alpine modal can look up whichever gown the
    # staffer opened without re-fetching. Expired blocks are left out -- a finished
    # cleaning window is history and the gown is already back in the pool.
    blocks_by_gown = defaultdict(list)
    for block in GownUnavailability.objects.filter(end_date__gte=today):
        blocks_by_gown[block.gown_id].append({
            "id": block.id,
            "start": block.start_date.isoformat(),
            "end": block.end_date.isoformat(),
            "range": (
                block.start_date.strftime("%b %d, %Y")
                if block.start_date == block.end_date
                else f'{block.start_date.strftime("%b %d")} – {block.end_date.strftime("%b %d, %Y")}'
            ),
            "reason": block.reason,
            "note": block.note,
            "active": block.covers_today,
            "delete_url": reverse("arabela_admin:gown_block_delete", args=[block.id]),
        })

    # Counted from `gowns`, already fetched above -- no extra query. Out-of-Stock
    # excluded, matching every other "in stock" tally on this page. This one list
    # backs the category filter dropdown, the Add/Edit Gown dropdown, AND the
    # browse-by-category chip strip below, instead of each hardcoding its own copy
    # of Gown.Category (the old, easy-to-forget pattern that needed a manual edit in
    # 4 separate places every time a category was added).
    category_rows = [
        {
            "key": key,
            "label": label,
            "count": sum(
                1 for g in gowns
                if g.category == key and g.status != Gown.Status.OUT_OF_STOCK
            ),
            # The category's physical-tag color, so the Add Gown form and the Tag Colors
            # section read it from the same row the rest of the category data lives in.
            "tag_color": tag_colors.get(key, "White"),
            "tag_hex": TAG_COLOR_HEX.get(tag_colors.get(key, "White"), "#FFFFFF"),
        }
        for key, label in Gown.Category.choices
    ]

    # The Removal Log panel -- every gown ever removed and why, newest first. Capped so
    # the page can't grow without bound; the cap is far above what a single shop's
    # catalog could realistically remove.
    gown_removals = list(GownRemoval.objects.all()[:_REMOVAL_LOG_LIMIT])
    removal_hays = []
    for removal in gown_removals:
        removal.number_label = f"{removal.tracking_number:03d}" if removal.tracking_number is not None else "—"
        # One lowercase string per log entry for the panel's search box (ID, the number in
        # every form staff might type it, name, category, reason, note, who removed it).
        number = removal.tracking_number
        removal_hays.append(" ".join(filter(None, [
            removal.gown_id, removal.name, removal.category, removal.get_reason_display(),
            removal.note, removal.removed_by_name,
            f"{number:03d} {number} #{number}" if number is not None else "",
        ])).lower())

    # "Blocked Gowns" tile/list -- every gown with an active block TODAY, right
    # here in Inventory instead of only on the Rental Schedule calendar, so staff
    # can find one and release it without leaving this page. Sorted so whichever
    # gown frees up soonest is checked first. At most one active block per gown
    # (an overlapping block on the same gown is rejected at creation), so each
    # blocked gown contributes exactly the one block that covers today.
    blocked_gowns_today = []
    for g in gowns:
        active_block = next((b for b in blocks_by_gown.get(g.id, []) if b["active"]), None)
        if active_block:
            blocked_gowns_today.append({"gown": g, "block": active_block})
    blocked_gowns_today.sort(key=lambda row: row["block"]["end"])

    return render(
        request,
        "arabela_admin/gown-catalog.html",
        {
            "page": "gown",
            "gowns": gowns,
            "gowns_min": gowns_min,
            # The one list the Add/Edit dropdown, the live color-code warning, and the
            # live suggestion all read -- see GOWN_COLOR_PRESETS's own docstring.
            "color_presets": [{"name": n, "code": c} for n, c in GOWN_COLOR_PRESETS],
            "gown_blocks": dict(blocks_by_gown),
            # Cooldown is left out on purpose -- it's the label the system uses for
            # its own auto-added post-rental blocks (see reservation_submit), not a
            # reason a staff member would ever pick by hand for a new one.
            "block_reasons": [r for r in GownUnavailability.Reason.values if r != GownUnavailability.Reason.COOLDOWN],
            "today_iso": today.isoformat(),
            "total_gowns_count": len(gowns),
            "available_count": sum(1 for g in gowns if g.status == Gown.Status.AVAILABLE),
            "reserved_count": sum(1 for g in gowns if g.status == Gown.Status.RESERVED),
            "blocked_gowns_today": blocked_gowns_today,
            "category_rows": category_rows,
            # Tag Colors: everyone sees the list, only the owner gets the edit controls.
            # Decided HERE from _is_owner(request) -- the same check the save endpoint
            # enforces -- rather than the context-processor's admin_is_owner flag, which
            # can disagree for an admin account that has no UserProfile.
            "can_edit_tag_colors": _is_owner(request),
            "tag_colors_data": {
                key: {"color": tag_colors.get(key, "White"), "hex": TAG_COLOR_HEX.get(tag_colors.get(key, "White"), "#FFFFFF")}
                for key, _label in Gown.Category.choices
            },
            "tag_palette_data": {name: hex_ for name, hex_ in TAG_COLOR_PALETTE},
            "tag_palette": [{"name": name, "hex": hex_} for name, hex_ in TAG_COLOR_PALETTE],
            "gown_removals": gown_removals,
            "removal_hays": removal_hays,
            "removal_reasons": [
                {"value": value, "label": label} for value, label in GownRemoval.Reason.choices
            ],
            "check_stale_days": CHECK_STALE_DAYS,
            "checked_recent_count": sum(
                1 for g in gowns
                if g.days_since_check is not None and g.days_since_check < CHECK_STALE_DAYS
            ),
            # The chip strip doesn't show all 15 with equal weight -- a shop that has
            # only just started stocking one category would otherwise show a wall of
            # zeros next to it, which reads as broken more than "not stocked yet".
            # Populated categories surface first, by how much stock they actually
            # carry; empty ones collapse behind a single "+N more" toggle instead.
            "category_rows_populated": sorted(
                (row for row in category_rows if row["count"] > 0),
                key=lambda row: row["count"], reverse=True,
            ),
            "category_rows_empty": [row for row in category_rows if row["count"] == 0],
        },
    )


@require_http_methods(["POST"])
def gown_status_update_view(request, gown_id):
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        gown = Gown.objects.get(id=gown_id)
    except Gown.DoesNotExist:
        return JsonResponse({"error": "Gown not found"}, status=404)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    new_status = data.get("status")
    if new_status not in Gown.Status.values:
        return JsonResponse({"error": "Invalid status"}, status=400)

    gown.status = new_status
    gown.save(update_fields=["status", "updated_at"])
    return JsonResponse({"success": True, "status": gown.status})


def _gown_blocking_reservation_item(gown):
    """The ReservationItem (if any) that makes this gown un-deletable -- a live
    commitment: not Returned, and not on a Rejected/Cancelled reservation. Same rule
    as gowns.views._blocked_dates_for_category / _find_available_unit / the
    availability context processor, so a delete guard built on this can never
    disagree with what the availability calendar already shows as booked. Returns
    the item (reservation + customer preloaded for the message) or None. Shared by
    the single delete and the bulk delete so the two can't drift."""
    return (
        gown.reservation_items
        .exclude(stage=ReservationItem.Stage.RETURNED)
        .exclude(reservation__status__in=[Reservation.Status.REJECTED, Reservation.Status.CANCELLED])
        .select_related("reservation__customer__profile")
        .first()
    )


_REMOVAL_NOTE_MAX = 300


def _parse_removal(data):
    """Validates the "why is this gown being removed" answer from a delete request.
    Returns (reason, note, error) -- error is '' only when the reason is one of the
    real choices, the note fits, and an "Other" reason comes with a note (an "Other"
    with nothing written explains nothing, which is exactly the vague gap the removal
    log exists to prevent)."""
    if not isinstance(data, dict):
        data = {}
    reason = str(data.get("reason") or "").strip()
    note = str(data.get("note") or "").strip()
    if reason not in GownRemoval.Reason.values:
        return None, "", "Please choose why this gown is being removed."
    if len(note) > _REMOVAL_NOTE_MAX:
        return None, "", f"Please keep the note to {_REMOVAL_NOTE_MAX} characters or fewer."
    if reason == GownRemoval.Reason.OTHER and not note:
        return None, "", "Please add a short note explaining why (you chose Other)."
    return reason, note, ""


def _remove_gown(gown, reason, note, user):
    """Writes the Removal Log entry and deletes the gown as ONE atomic step: either both
    happen or neither does, so the log can never claim a gown was removed that is still
    there, and a gown can never disappear without leaving its reason behind. The log row
    copies what identifies the gown (ID, name, category, color, size, photo) because the
    gown row itself is about to stop existing."""
    with transaction.atomic():
        GownRemoval.objects.create(
            gown_id=gown.gown_id,
            tracking_number=gown.tracking_number,
            name=gown.name,
            category=gown.category,
            color_name=gown.color_name,
            size=gown.size,
            photo_url=gown.photo_url,
            reason=reason,
            note=note,
            removed_by=user if getattr(user, "is_authenticated", False) else None,
            removed_by_name=_staff_display_name(user) if getattr(user, "is_authenticated", False) else "",
            removed_at=timezone.now(),
        )
        gown.delete()


@require_http_methods(["POST"])
def gown_delete_view(request, gown_id):
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        gown = Gown.objects.get(id=gown_id)
    except Gown.DoesNotExist:
        return JsonResponse({"error": "Gown not found"}, status=404)

    try:
        data = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = {}
    reason, note, reason_error = _parse_removal(data)
    if reason_error:
        return JsonResponse({"error": reason_error}, status=400)

    blocking_item = _gown_blocking_reservation_item(gown)
    if blocking_item:
        reservation = blocking_item.reservation
        return JsonResponse(
            {
                "error": (
                    f"{gown.gown_id} is still on an active reservation "
                    f"({reservation.reference_code} - {reservation.display_customer_name}). "
                    "Mark it Returned, or reject/cancel the reservation, before deleting."
                )
            },
            status=400,
        )

    removed_id = gown.gown_id
    _remove_gown(gown, reason, note, request.user)
    return JsonResponse({
        "success": True,
        "removed": removed_id,
        "message": f"{removed_id} removed. Its number is retired and won't be used again.",
    })


_BULK_MAX_IDS = 200


@require_http_methods(["POST"])
def gown_bulk_action_view(request):
    """One request, many gowns -- the catalog's multi-select toolbar. action='status'
    flips every selected gown's status; action='checked' stamps them as physically
    confirmed present right now, by whoever is logged in; action='delete' removes them
    (one reason for the whole batch, each gown getting its own Removal Log entry),
    skipping (not failing) any that are still on a live reservation and reporting which.
    Kept a single endpoint rather than a loop of per-gown fetches from the browser so a
    half-finished batch can't happen from a dropped connection mid-loop."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid request."}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({"error": "Invalid request."}, status=400)

    action = data.get("action")
    if action not in ("status", "delete", "checked"):
        return JsonResponse({"error": "Unknown bulk action."}, status=400)

    raw_ids = data.get("ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return JsonResponse({"error": "Please select at least one gown."}, status=400)
    try:
        ids = {int(x) for x in raw_ids}
    except (TypeError, ValueError):
        return JsonResponse({"error": "Invalid selection."}, status=400)
    if len(ids) > _BULK_MAX_IDS:
        return JsonResponse(
            {"error": f"Please select {_BULK_MAX_IDS} gowns or fewer at a time."},
            status=400,
        )

    if action == "status":
        new_status = data.get("status")
        if new_status not in Gown.Status.values:
            return JsonResponse({"error": "Invalid status."}, status=400)
        # QuerySet.update() bypasses Model.save(), so it does NOT auto-bump the
        # auto_now `updated_at` -- set it here, or the admin notification feed
        # (which orders Out-of-Stock gowns by -updated_at) would show stale order.
        with transaction.atomic():
            updated = Gown.objects.filter(id__in=ids).update(
                status=new_status, updated_at=timezone.now()
            )
        noun = "gown" if updated == 1 else "gowns"
        return JsonResponse({
            "success": True,
            "updated": updated,
            "message": f"{updated} {noun} marked {new_status}.",
        })

    if action == "checked":
        # Same explicit-updated_at reason as the status branch above: .update() doesn't
        # run save(), so the auto_now field would otherwise stay stale.
        now = timezone.now()
        with transaction.atomic():
            updated = Gown.objects.filter(id__in=ids).update(
                last_checked_at=now, last_checked_by=request.user, updated_at=now
            )
        noun = "gown" if updated == 1 else "gowns"
        return JsonResponse({
            "success": True,
            "updated": updated,
            "message": f"{updated} {noun} marked as checked.",
        })

    # action == "delete" -- the reason is validated ONCE, up front, before anything is
    # touched: a batch with no reason must delete nothing, not delete some of it.
    reason, note, reason_error = _parse_removal(data)
    if reason_error:
        return JsonResponse({"error": reason_error}, status=400)

    # Best-effort, NOT one atomic block: deleting what can be deleted and reporting
    # the rest is the whole point.
    deleted = 0
    skipped = []
    for gown in Gown.objects.filter(id__in=ids):
        blocking = _gown_blocking_reservation_item(gown)
        if blocking:
            skipped.append({
                "gown_id": gown.gown_id,
                "reference_code": blocking.reservation.reference_code,
            })
            continue
        try:
            _remove_gown(gown, reason, note, request.user)
            deleted += 1
        except Exception:
            skipped.append({"gown_id": gown.gown_id, "reference_code": None})

    noun = "gown" if deleted == 1 else "gowns"
    message = f"{deleted} {noun} removed and logged."
    if skipped:
        message += f" {len(skipped)} skipped (still on active reservations)."
    return JsonResponse({
        "success": True,
        "deleted": deleted,
        "skipped": skipped,
        "message": message,
    })


@require_http_methods(["POST"])
def gown_tag_colors_update_view(request):
    """Saves which physical-tag color each category uses. OWNER ONLY, enforced here on
    the server: the Tag Colors section hides its edit controls from staff, but hiding a
    button protects nothing on its own -- a staff account sending this request by hand
    must get refused too. (Inline rather than @_require_owner: that decorator redirects
    a signed-out session to the login page, which a fetch() would follow and choke on;
    like every other endpoint on this page, this one answers in JSON.)

    Accepts {"colors": {"Wedding Gown": "White", ...}} -- any subset of categories. The
    whole request is rejected if any entry is bad, so a typo can never save half."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    if not _is_owner(request):
        return JsonResponse({"error": "Only the owner can change tag colors."}, status=403)
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid request."}, status=400)
    colors = data.get("colors") if isinstance(data, dict) else None
    if not isinstance(colors, dict) or not colors:
        return JsonResponse({"error": "No tag colors were sent."}, status=400)

    for category, color in colors.items():
        if category not in DEFAULT_CATEGORY_TAG_COLORS:
            return JsonResponse({"error": f"'{category}' isn't a gown category."}, status=400)
        if color not in TAG_COLOR_HEX:
            return JsonResponse({"error": f"'{color}' isn't one of the tag colors."}, status=400)

    with transaction.atomic():
        # Locked so two simultaneous saves (two owner tabs) apply one after the other
        # instead of one overwriting the other's whole dict.
        site_settings = SiteSettings.objects.select_for_update().filter(pk=1).first() or SiteSettings.load()
        saved = dict(site_settings.category_tag_colors) if isinstance(site_settings.category_tag_colors, dict) else {}
        saved.update(colors)
        site_settings.category_tag_colors = saved
        site_settings.save(update_fields=["category_tag_colors", "updated_at"])

    resolved = resolve_tag_colors(saved)
    # Two categories sharing one color defeats the point of color-coding: the tag would
    # no longer say which of them a gown belongs to. Allowed (the owner may have a
    # reason, or be mid-swap) but flagged so it's never a surprise.
    by_color = defaultdict(list)
    for category, color in resolved.items():
        by_color[color].append(category)
    shared = [
        {"color": color, "categories": cats}
        for color, cats in by_color.items() if len(cats) > 1
    ]
    return JsonResponse({
        "success": True,
        "colors": [
            {"category": category, "color": color, "hex": TAG_COLOR_HEX[color]}
            for category, color in resolved.items()
        ],
        "shared": shared,
    })


# Same allow-list this project already uses for reservation payment-proof uploads
# (gowns.views PROOF_ALLOWED_EXTENSIONS et al) -- mirrored here rather than imported
# across apps, matching how ReservationItem.ReturnCondition mirrors gowns.Gown.Condition.
GOWN_PHOTO_ALLOWED_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
GOWN_PHOTO_ALLOWED_CONTENT_TYPES = ('image/jpeg', 'image/png', 'image/webp')
GOWN_PHOTO_MAX_BYTES = 5 * 1024 * 1024  # 5 MB


def _validate_gown_photo(photo_file) -> str:
    """Return '' when the upload is acceptable (including "no file" -- a photo is
    optional at creation time), else the message to show staff."""
    if not photo_file:
        return ""
    name = (getattr(photo_file, "name", "") or "").lower()
    if not name.endswith(GOWN_PHOTO_ALLOWED_EXTENSIONS):
        return "Gown photo must be a JPG, PNG, or WEBP image."
    content_type = (getattr(photo_file, "content_type", "") or "").lower()
    if content_type and content_type not in GOWN_PHOTO_ALLOWED_CONTENT_TYPES:
        return "Gown photo must be a JPG, PNG, or WEBP image."
    if photo_file.size > GOWN_PHOTO_MAX_BYTES:
        return "Gown photo must be 5MB or smaller."
    return ""


def _save_gown_photo(photo_file) -> str:
    """Store the upload under a collision-proof name and return its public URL.
    Uses the storage backend's own .url() rather than MEDIA_URL + path so this
    keeps working whether files land on local disk or on Cloudinary."""
    saved_path = default_storage.save(
        f"gown_photos/{uuid.uuid4().hex}_{photo_file.name}", photo_file
    )
    return default_storage.url(saved_path)


# Same allow-list as gown photos and (in gowns.views) customer payment-proof uploads --
# mirrored here rather than shared, matching how this file already mirrors the gown
# photo constants instead of importing them across apps.
RECEIPT_PHOTO_ALLOWED_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
RECEIPT_PHOTO_ALLOWED_CONTENT_TYPES = ('image/jpeg', 'image/png', 'image/webp')
RECEIPT_PHOTO_MAX_BYTES = 5 * 1024 * 1024  # 5 MB


def _validate_receipt_photo(photo_file) -> str:
    """Return '' when the upload is acceptable, else the message to show staff.
    Unlike a gown photo, a receipt photo is never optional -- there is nothing to
    attach without one."""
    if not photo_file:
        return "Please attach a photo of the receipt."
    name = (getattr(photo_file, "name", "") or "").lower()
    if not name.endswith(RECEIPT_PHOTO_ALLOWED_EXTENSIONS):
        return "Receipt photo must be a JPG, PNG, or WEBP image."
    content_type = (getattr(photo_file, "content_type", "") or "").lower()
    if content_type and content_type not in RECEIPT_PHOTO_ALLOWED_CONTENT_TYPES:
        return "Receipt photo must be a JPG, PNG, or WEBP image."
    if photo_file.size > RECEIPT_PHOTO_MAX_BYTES:
        return "Receipt photo must be 5MB or smaller."
    return ""


def _save_receipt_photo(photo_file) -> str:
    saved_path = default_storage.save(
        f"receipt_photos/{uuid.uuid4().hex}_{photo_file.name}", photo_file
    )
    return default_storage.url(saved_path)


def _receipt_row(receipt, today=None):
    """Serialize one ReceiptRecord for the Alpine table -- shape matches exactly what
    the page's existing client-side search/filter code already expects
    (customer/reservation/uploaded/photoUrl), plus year/isThisWeek so that filtering
    can be done generically in the template instead of the page's old hardcoded
    per-row 'yearFilter === "2026"'-style conditions (one meant literally for every
    single row, real data can't be baked into the template like that)."""
    today = today or timezone.localdate()
    week_start = today - timedelta(days=today.weekday())
    local_uploaded = timezone.localtime(receipt.uploaded_at)
    # date_format, not strftime's platform-specific "no leading zero" flags (%-d is
    # Linux-only; Windows needs %#d) -- this dev machine is Windows, the deploy target
    # is Linux, and Django's own formatter (j/g are already "no leading zero" by
    # definition) works identically on both.
    uploaded_display = f"{date_format(local_uploaded, 'M j, Y')} · {date_format(local_uploaded, 'g:i A')}"
    return {
        "id": receipt.id,
        "customer": receipt.reservation.display_customer_name,
        "reservation": receipt.reservation.reference_code,
        "uploaded": uploaded_display,
        # Who attached it -- so the owner can see, per receipt, which staff member
        # filed it. "Removed account" when that staff account was later deleted
        # (uploaded_by is SET_NULL on purpose: the receipt itself must survive).
        "uploadedBy": _staff_display_name(receipt.uploaded_by) if receipt.uploaded_by else "Removed account",
        "photoUrl": receipt.photo_url,
        "year": str(local_uploaded.year),
        "isThisWeek": local_uploaded.date() >= week_start,
    }


def _validate_gown_fields(name, category, color_name, color_code, size, design_variant="",
                          exclude_gown_id=None):
    """Shared field checks for Add Gown and Edit Gown, so a rule added to one can never
    silently miss the other. Returns an error message, or '' when everything is valid.

    Every length check here mirrors the model's max_length, so an over-long value is
    turned into a sentence staff can act on instead of a raw database DataError. The
    catalog's own dropdowns can't produce over-long values, but the "Other" free-text
    fallbacks (and any caller that skips the page's JS) can.

    `exclude_gown_id` is the gown currently being edited (gown_update_view passes its
    own id): without it, a gown that already correctly owns a code would collide with
    itself on every single edit, even one that changes nothing about its color."""
    if not name:
        return "Please enter a gown name."
    if len(name) > 150:
        return "Gown name must be 150 characters or fewer."
    if category not in Gown.Category.values:
        return "Please choose a valid category."
    if not color_name:
        return "Please enter a color name."
    if not color_code:
        return "Please enter a color code."
    if len(color_code) > 2:
        return "Color code must be 1-2 letters (e.g. WH)."
    if len(color_name) > 40:
        return "Color name must be 40 characters or fewer."
    if size not in Gown.Size.values:
        return "Please choose a valid size."
    if len(design_variant) > 60:
        return "Design variant must be 60 characters or fewer."

    # A 2-letter code must always mean the same color everywhere in the catalog --
    # this is the actual guarantee behind the Add/Edit dropdown's live warning and
    # suggestion (both client-side conveniences; this check is what makes either of
    # them a promise rather than just a hint). Checked two ways, matching the two
    # ways a color can be "known": a name on the preset list below, or a name some
    # earlier gown already established for a code that isn't a preset at all (a
    # custom color entered once through "Other").
    error = _check_color_code_consistency(color_code, color_name, exclude_gown_id)
    if error:
        return error

    return ""


def _check_color_code_consistency(color_code, color_name, exclude_gown_id=None):
    """Returns an error message if `color_code` already means a color other than
    `color_name`, or '' if the pairing is fine (new, or already correct).

    Comparison is case-insensitive so "white"/"White"/"WHITE" are the same color, but
    whatever the caller submitted is what gets saved -- this never rewrites input.
    """
    preset_names_by_code = {code: name for name, code in GOWN_COLOR_PRESETS}
    preset_name = preset_names_by_code.get(color_code)
    if preset_name is not None:
        if preset_name.casefold() != color_name.casefold():
            return (
                f'"{color_code}" already means {preset_name}. Please use a different '
                f"code, or change the color name to {preset_name} if that's what this is."
            )
        return ""

    # Not a preset code -- it may still already mean something, from an earlier "Other"
    # entry. One indexed lookup; excludes the gown being edited so correcting (or simply
    # re-saving) its own existing pairing never reads as a conflict with itself.
    conflict = Gown.objects.filter(color_code=color_code).exclude(color_name__iexact=color_name)
    if exclude_gown_id is not None:
        conflict = conflict.exclude(id=exclude_gown_id)
    existing = conflict.first()
    if existing is not None:
        return (
            f'"{color_code}" already means {existing.color_name} '
            f"(used on {existing.gown_id}). Please choose a different code."
        )
    return ""


def _parse_rental_price(raw):
    """Returns (price, error). error is '' only when raw is a finite amount that fits
    Gown.rental_price (max_digits=8, decimal_places=2): 0 < price <= 999999.99, quantized
    to 2 decimal places. Every other input yields a staff-facing sentence, never a
    downstream database DataError."""
    price_raw = str(raw or "").replace(",", "").replace("₱", "").strip()
    try:
        price = Decimal(price_raw)
    except (InvalidOperation, ValueError):
        return None, "Please enter a valid rental price."
    # is_finite() must be tested first: Decimal('NaN') <= 0 raises InvalidOperation, so
    # this short-circuit is what keeps NaN / sNaN / Infinity out of the comparison.
    if not price.is_finite() or price <= 0:
        return None, "Please enter a valid rental price."
    try:
        price = price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None, "That rental price looks too high. Please check the amount."
    if price <= 0:  # e.g. "0.004" quantizes down to 0.00
        return None, "Please enter a valid rental price."
    if price > Decimal("999999.99"):
        return None, "That rental price looks too high. Please check the amount."
    return price, ""


def _next_free_gown_name(category, name):
    """`name` with the smallest "(n)" suffix (n >= 2) that no gown in this category
    already uses -- "White" -> "White (2)" -> "White (3)". Any suffix already on `name`
    is dropped first, so re-adding "White (2)" gives "White (3)" and never "White (2)
    (2)". Compared case-insensitively, the same way the customer site decides whether
    two gowns share a name. The base is trimmed if needed so the result always fits the
    150-character name column."""
    root = re.sub(r"\s*\(\d+\)\s*$", "", name).strip() or name
    taken = {
        existing.casefold()
        for existing in Gown.objects.filter(category=category, name__istartswith=root)
        .values_list("name", flat=True)
    }
    n = 2
    while True:
        suffix = f" ({n})"
        candidate = root[: 150 - len(suffix)] + suffix
        if candidate.casefold() not in taken:
            return candidate
        n += 1


@require_http_methods(["POST"])
def gown_create_view(request):
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    name = (request.POST.get("name") or "").strip()
    category = request.POST.get("category")
    color_name = (request.POST.get("color_name") or "").strip()
    color_code = (request.POST.get("color_code") or "").strip().upper()
    size = request.POST.get("size")
    design_variant = (request.POST.get("design_variant") or "").strip()
    status = request.POST.get("status") or Gown.Status.AVAILABLE
    # The Add form sends these too -- read them so they're not silently dropped
    # (before this, condition/notes could only be set later via Edit Gown).
    condition = request.POST.get("condition") or Gown.Condition.GOOD
    if condition not in Gown.Condition.values:
        condition = Gown.Condition.GOOD
    notes = (request.POST.get("notes") or "").strip()

    error = _validate_gown_fields(name, category, color_name, color_code, size, design_variant)
    if error:
        return JsonResponse({"error": error}, status=400)

    if status not in Gown.Status.values:
        status = Gown.Status.AVAILABLE

    rental_price, price_error = _parse_rental_price(request.POST.get("rental_price"))
    if price_error:
        return JsonResponse({"error": price_error}, status=400)

    photo_file = request.FILES.get("photo")
    photo_error = _validate_gown_photo(photo_file)
    if photo_error:
        return JsonResponse({"error": photo_error}, status=400)

    # Same name already used in this category? The customer site groups every gown that
    # shares a name into ONE product with a quantity ("3 available") -- exactly right for
    # another size/unit of the same dress, and exactly wrong for a genuinely different
    # dress that just happens to share the name (it would silently vanish into the other
    # one's listing). Only the person adding it knows which this is, so ask -- and do it
    # here, BEFORE the photo upload below, so answering the question never uploads the
    # photo twice.
    name_choice = (request.POST.get("name_choice") or "").strip()
    same_name = Gown.objects.filter(category=category, name__iexact=name)
    if same_name.exists():
        if name_choice not in ("same", "different"):
            existing = list(same_name.order_by("gown_id")[:6])
            return JsonResponse({
                "code": "name_conflict",
                "error": f'A gown named "{name}" already exists in {category}.',
                "name_conflict": {
                    "name": name,
                    "count": same_name.count(),
                    "existing": [
                        {"gown_id": g.gown_id, "size": g.size, "color_name": g.color_name}
                        for g in existing
                    ],
                },
            }, status=409)
        if name_choice == "different":
            name = _next_free_gown_name(category, name)

    # Photo first: nothing is in the DB yet, so a storage failure here is a clean
    # "try again" with zero cleanup rather than a half-created gown.
    photo_url = ""
    if photo_file:
        try:
            photo_url = _save_gown_photo(photo_file)
        except Exception:
            return JsonResponse(
                {"error": "The gown photo couldn't be uploaded just now. Please try again."},
                status=502,
            )

    # Neither gown_id nor slug can collide from a genuine race anymore:
    # next_tracking_number() and Gown._generate_unique_slug() (via GownSequence /
    # GownSlugSequence) each hand out their value under a real row lock, so no two
    # requests can ever receive the same one -- see those models' docstrings. This
    # retry now stays purely as defense-in-depth for hand-edited/corrupted data, not
    # as the primary defense it used to be. Each attempt re-reads via a fresh
    # transaction.atomic() (a savepoint), which keeps the connection usable after a
    # failed INSERT instead of poisoning the whole request.
    #
    # The number comes from ONE counter for the whole category (color plays no part in
    # it), so every gown in a category gets a number no other gown in it has ever had.
    gown = None
    for _attempt in range(3):
        tracking_number = Gown.next_tracking_number(category)
        gown_id = f"{category}-{color_code}-{tracking_number:03d}"
        try:
            with transaction.atomic():
                gown = Gown.objects.create(
                    gown_id=gown_id,
                    name=name,
                    category=category,
                    color_name=color_name,
                    color_code=color_code,
                    size=size,
                    design_variant=design_variant,
                    rental_price=rental_price,
                    condition=condition,
                    notes=notes,
                    status=status,
                    photo_url=photo_url,
                )
        except IntegrityError:
            gown = None
            continue
        except DataError:
            return JsonResponse(
                {"error": "Some of those values are out of range. Please shorten the "
                          "text fields or lower the price."},
                status=400,
            )
        break

    if gown is None:
        return JsonResponse(
            {"error": "Couldn't save that gown just now. Please try again."},
            status=409,
        )

    return JsonResponse({
        "success": True,
        "gown": {
            "id": gown.id,
            "gown_id": gown.gown_id,
            "slug": gown.slug,
            "name": gown.name,
            "category": gown.category,
            "color_name": gown.color_name,
            "size": gown.size,
            "rental_price": str(gown.rental_price),
            "status": gown.status,
            "photo_url": gown.photo_url,
        },
    })


@require_http_methods(["POST"])
def gown_update_view(request, gown_id):
    """Edits an existing gown's descriptive/business details.

    Deliberately leaves 2 things untouched no matter what's submitted:
      * gown_id -- a once-assigned tag, not something that should shuffle every time
        a typo gets fixed elsewhere on the row.
      * slug -- the stable, permanent identity a customer's product link (and the
        booking-matching fix in gowns.views._find_available_unit) depends on. Letting
        this regenerate on a name edit would silently break bookmarked product links.
    Status and photo already have their own dedicated actions (gown_status_update_view,
    gown_photo_update_view) and are intentionally not duplicated here."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        gown = Gown.objects.get(id=gown_id)
    except Gown.DoesNotExist:
        return JsonResponse({"error": "Gown not found"}, status=404)

    name = (request.POST.get("name") or "").strip()
    category = request.POST.get("category")
    color_name = (request.POST.get("color_name") or "").strip()
    color_code = (request.POST.get("color_code") or "").strip().upper()
    size = request.POST.get("size")
    design_variant = (request.POST.get("design_variant") or "").strip()
    condition = request.POST.get("condition") or gown.condition
    notes = (request.POST.get("notes") or "").strip()

    error = _validate_gown_fields(
        name, category, color_name, color_code, size, design_variant,
        exclude_gown_id=gown.id,
    )
    if error:
        return JsonResponse({"error": error}, status=400)

    rental_price, price_error = _parse_rental_price(request.POST.get("rental_price"))
    if price_error:
        return JsonResponse({"error": price_error}, status=400)

    if condition not in Gown.Condition.values:
        condition = gown.condition

    gown.name = name
    gown.category = category
    gown.color_name = color_name
    gown.color_code = color_code
    gown.size = size
    gown.design_variant = design_variant
    gown.rental_price = rental_price
    gown.condition = condition
    gown.notes = notes
    # _validate_gown_fields + _parse_rental_price already bound every field to its
    # column, so DataError here should be unreachable -- this is a backstop that keeps
    # even a future unguarded field from turning into a raw 500 for staff.
    try:
        with transaction.atomic():
            gown.save(update_fields=[
                "name", "category", "color_name", "color_code", "size",
                "design_variant", "rental_price", "condition", "notes", "updated_at",
            ])
    except DataError:
        return JsonResponse(
            {"error": "Some of those values are out of range. Please shorten the "
                      "text fields or lower the price."},
            status=400,
        )

    return JsonResponse({
        "success": True,
        "gown": {
            "id": gown.id,
            "gown_id": gown.gown_id,
            "slug": gown.slug,
            "name": gown.name,
            "category": gown.category,
            "color_name": gown.color_name,
            "color_code": gown.color_code,
            "size": gown.size,
            "design_variant": gown.design_variant,
            "rental_price": str(gown.rental_price),
            "condition": gown.condition,
            "notes": gown.notes,
            "status": gown.status,
            "photo_url": gown.photo_url,
        },
    })


@require_http_methods(["POST"])
def gown_photo_update_view(request, gown_id):
    """Lets staff attach/replace a photo on a gown that already exists -- without this,
    a gown created with no photo (or a bad one) could never get a real customer-facing
    photo once it's listed on the live site."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        gown = Gown.objects.get(id=gown_id)
    except Gown.DoesNotExist:
        return JsonResponse({"error": "Gown not found"}, status=404)

    photo_file = request.FILES.get("photo")
    error = _validate_gown_photo(photo_file)
    if error:
        return JsonResponse({"error": error}, status=400)
    if not photo_file:
        return JsonResponse({"error": "Please choose a photo to upload."}, status=400)

    try:
        gown.photo_url = _save_gown_photo(photo_file)
    except Exception:
        return JsonResponse(
            {"error": "The gown photo couldn't be uploaded just now. Please try again."},
            status=502,
        )
    gown.save(update_fields=["photo_url", "updated_at"])
    return JsonResponse({"success": True, "photo_url": gown.photo_url})


def _parse_iso_date(raw):
    try:
        return datetime.strptime(str(raw).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


@require_http_methods(["POST"])
def gown_block_create_view(request, gown_id):
    """Take one gown unit off the rental pool for a date range -- the post-return
    cleaning/repair gap. The customer product calendar subtracts these from that
    category's capacity per date (gowns.views._blocked_dates_for_category)."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        gown = Gown.objects.get(id=gown_id)
    except Gown.DoesNotExist:
        return JsonResponse({"error": "Gown not found"}, status=404)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    start_date = _parse_iso_date(data.get("start_date"))
    end_date = _parse_iso_date(data.get("end_date"))
    if not start_date or not end_date:
        return JsonResponse({"error": "Please choose both a start and an end date."}, status=400)
    if end_date < start_date:
        return JsonResponse({"error": "The end date can't be before the start date."}, status=400)
    # The template sets min= on the date inputs, but that's client-side only -- enforce
    # it here too so a block can't be created entirely in the past (it would do nothing
    # and just clutter the list).
    if end_date < timezone.localdate():
        return JsonResponse({"error": "The end date is already in the past."}, status=400)

    reason = data.get("reason")
    if reason not in GownUnavailability.Reason.values:
        reason = GownUnavailability.Reason.CLEANING

    # Locking the gown row serializes two staff blocking the same gown at the same
    # instant, so the overlap check just below can never be fooled by a race -- the
    # second request waits for the first to commit, then sees its new block.
    with transaction.atomic():
        try:
            Gown.objects.select_for_update().get(id=gown.id)
        except Gown.DoesNotExist:
            return JsonResponse({"error": "Gown not found"}, status=404)

        # Same inclusive-overlap rule gowns.views._blocked_dates_for_category and
        # _find_available_unit already use for GownUnavailability -- one active block
        # per date range per gown, so this can never disagree with what those two
        # already treat as occupied. Expired blocks (end_date in the past) are left
        # out -- they're history, not a live conflict.
        conflict = (
            GownUnavailability.objects
            .filter(gown=gown, end_date__gte=timezone.localdate())
            .filter(start_date__lte=end_date, end_date__gte=start_date)
            .first()
        )
        if conflict:
            return JsonResponse(
                {
                    "error": (
                        f"This overlaps an existing block: {conflict.start_date:%b %d, %Y} "
                        f"– {conflict.end_date:%b %d, %Y} ({conflict.reason}). Release "
                        "that block first, or choose dates that don't overlap."
                    )
                },
                status=400,
            )

        block = GownUnavailability.objects.create(
            gown=gown,
            start_date=start_date,
            end_date=end_date,
            reason=reason,
            note=(data.get("note") or "").strip()[:200],
        )
    return JsonResponse({
        "success": True,
        "block": {
            "id": block.id,
            "start_date": block.start_date.isoformat(),
            "end_date": block.end_date.isoformat(),
            "reason": block.reason,
            "note": block.note,
        },
    })


@require_http_methods(["POST"])
def gown_block_delete_view(request, block_id):
    """Hand the blocked days back -- the gown is available again from this moment on."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        block = GownUnavailability.objects.get(id=block_id)
    except GownUnavailability.DoesNotExist:
        return JsonResponse({"error": "Blocked range not found"}, status=404)
    block.delete()
    return JsonResponse({"success": True})


@require_http_methods(["POST"])
def reservation_approve_view(request, pk):
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        reservation = Reservation.objects.get(id=pk)
    except Reservation.DoesNotExist:
        return JsonResponse({"error": "Reservation not found"}, status=404)

    reservation.status = Reservation.Status.CONFIRMED
    reservation.reviewed_at = timezone.now()
    reservation.save(update_fields=["status", "reviewed_at", "updated_at"])
    ReservationStatusEvent.record(
        reservation, "Reservation approved",
        detail="Your payment was verified. Your booking is confirmed.",
        actor=ReservationStatusEvent.Actor.STAFF,
    )

    # Flip any linked inventory gowns to Reserved so the catalog reflects the booking.
    for item in reservation.items.all():
        if item.gown_id and item.gown.status == Gown.Status.AVAILABLE:
            item.gown.status = Gown.Status.RESERVED
            item.gown.save(update_fields=["status", "updated_at"])

    return JsonResponse({"success": True, "status": reservation.status})


@require_http_methods(["POST"])
def reservation_reject_view(request, pk):
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        reservation = Reservation.objects.get(id=pk)
    except Reservation.DoesNotExist:
        return JsonResponse({"error": "Reservation not found"}, status=404)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        data = {}
    reason = (data.get("reason") or "").strip()

    reservation.status = Reservation.Status.REJECTED
    reservation.reviewed_at = timezone.now()
    update_fields = ["status", "reviewed_at", "updated_at"]
    if reason:
        reservation.notes = reason
        update_fields.append("notes")
    reservation.save(update_fields=update_fields)
    ReservationStatusEvent.record(
        reservation, "Reservation rejected",
        detail=reason or "Please contact the shop for details.",
        actor=ReservationStatusEvent.Actor.STAFF,
    )
    return JsonResponse({"success": True, "status": reservation.status})


@require_http_methods(["POST"])
def reservation_item_mark_returned_view(request, item_id):
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        item = ReservationItem.objects.select_related("gown", "reservation").get(id=item_id)
    except ReservationItem.DoesNotExist:
        return JsonResponse({"error": "Item not found"}, status=404)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    # Only two ways to check a gown back in: "Returned" (sent as Good -- straight back
    # on the website) or "Returned -- needs repair" (Needs Repair -- kept off it until
    # staff set it back to Available in Gown Catalog). "Fair" used to be a third choice
    # but did exactly what Good does, so it's no longer offered; older bookings that
    # recorded it keep it. What the gown's condition was is the shop's business, not
    # the customer's: the customer-facing timeline entry is the same either way, and
    # the repair note goes on a staff-only entry.
    condition = data.get("condition", "")
    if condition not in (ReservationItem.ReturnCondition.GOOD, ReservationItem.ReturnCondition.NEEDS_REPAIR):
        return JsonResponse({"error": "Choose Returned or Returned — needs repair."}, status=400)
    needs_repair = condition == ReservationItem.ReturnCondition.NEEDS_REPAIR

    item.stage = ReservationItem.Stage.RETURNED
    item.returned_on = timezone.localdate()
    item.return_condition = condition
    item.save(update_fields=["stage", "returned_on", "return_condition", "updated_at"])
    ReservationStatusEvent.record(
        item.reservation, f"{item.gown_name} returned", item=item,
        detail="Checked in by staff.",
        actor=ReservationStatusEvent.Actor.STAFF,
    )
    if needs_repair:
        ReservationStatusEvent.record(
            item.reservation, f"{item.gown_name} needs repair", item=item,
            detail="Checked in needing repair — kept off the website until it's set back to Available in Gown Catalog.",
            actor=ReservationStatusEvent.Actor.STAFF,
            staff_only=True,
        )

    if item.gown_id:
        item.gown.condition = condition
        if needs_repair:
            item.gown.status = Gown.Status.OUT_OF_STOCK
        elif not item.gown_held_by_another_active_item:
            # Don't clobber Reserved back to Available if this same physical gown is
            # also booked (for different, non-overlapping dates) by another still-active
            # reservation -- that booking's own return is what should flip it, not this one.
            item.gown.status = Gown.Status.AVAILABLE
        item.gown.last_returned_at = timezone.localdate()
        item.gown.save(update_fields=["condition", "status", "last_returned_at", "updated_at"])
        # Good condition only: resync the cooldown to the ACTUAL return day, which
        # may be earlier or later than what was planned. Skipped for needs-repair --
        # Out-of-Stock already keeps the gown off the market indefinitely on its own,
        # so a temporary cooldown window would be redundant (and, if left stale,
        # confusing once staff restore it to Available later).
        if not needs_repair:
            _resync_cooldown_block(item, item.returned_on)

    return JsonResponse({
        "success": True, "stage": item.stage,
        "returned_on": item.returned_on.isoformat(),
        "return_condition": item.return_condition,
    })


@require_http_methods(["POST"])
def reservation_item_send_reminder_view(request, item_id):
    """Send a reminder a staff member wrote, to the customer holding this gown.

    The counterpart to the automatic sweep in reservations/reminders.py. Staff can send
    one on ANY active booking, whether or not a date is near: they can see things the
    schedule cannot, and a button that refuses to work when the operator knows better is
    worse than no button. Terminal bookings are refused, though -- messaging someone
    about a gown they already returned, or a reservation that was cancelled, would be
    the shop contradicting its own records.
    """
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        item = ReservationItem.objects.select_related("reservation__customer").get(id=item_id)
    except ReservationItem.DoesNotExist:
        return JsonResponse({"error": "Item not found"}, status=404)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    if item.reservation.status not in _SCHEDULED_STATUSES:
        return JsonResponse(
            {"error": "Reminders can only be sent for confirmed or active bookings."},
            status=400,
        )
    if item.stage == ReservationItem.Stage.RETURNED:
        return JsonResponse(
            {"error": "This gown has already been returned."}, status=400
        )

    body = (data.get("body") or "").strip()
    if not body:
        return JsonResponse({"error": "The reminder message cannot be empty."}, status=400)

    try:
        sent = reservation_reminders.send_manual_reminder(
            item, body, sent_by=_staff_display_name(request.user)
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception:
        return JsonResponse(
            {"error": "Could not send this reminder. Please try again."}, status=500
        )

    return JsonResponse({"success": True, "body": sent})


def _parse_iso_date(raw):
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _load_open_item(request, item_id):
    """(item, None) for a booking staff may act on, else (None, JsonResponse). Shared by
    the three scheduling actions below: signed in as staff, the item exists, its
    reservation is approved (Confirmed/Active/Overdue) and the gown isn't already back."""
    if not _is_admin_staff(request):
        return None, JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        item = ReservationItem.objects.select_related("gown", "reservation").get(id=item_id)
    except ReservationItem.DoesNotExist:
        return None, JsonResponse({"error": "Item not found"}, status=404)
    if item.reservation.status not in _SCHEDULED_STATUSES:
        return None, JsonResponse(
            {"error": "Only approved bookings can be changed here. Approve it first in Pending Approval."},
            status=400,
        )
    if item.stage == ReservationItem.Stage.RETURNED:
        return None, JsonResponse({"error": "This gown has already been returned."}, status=400)
    return item, None


@require_http_methods(["POST"])
@transaction.atomic  # the availability check locks the gown row until the save commits
def reservation_item_mark_picked_up_view(request, item_id):
    """The gown leaves the shop. Only on or after the booking's CURRENT pick-up date (the
    one the customer moved it to, if they did) -- before that the booking isn't due yet.
    The recorded pick-up is always today. Late is allowed: a customer who turns up after
    their day must still be recordable."""
    item, error = _load_open_item(request, item_id)
    if error:
        return error
    if item.stage != ReservationItem.Stage.PICKUP:
        return JsonResponse({"error": "This item has already been marked picked up."}, status=400)
    today = timezone.localdate()
    if today < item.rental_date:
        return JsonResponse({
            "error": (
                f"Pick-up is {item.rental_date:%b %d, %Y}, not yet. It can be marked picked up "
                f"on that day or after. If the customer wants it earlier, use Change pick-up date."
            ),
        }, status=400)

    item.stage = ReservationItem.Stage.RESERVED
    item.picked_up_on = today
    item.save(update_fields=["stage", "picked_up_on", "updated_at"])
    ReservationStatusEvent.record(
        item.reservation, f"{item.gown_name} picked up", item=item,
        detail="The gown is now with the customer.",
        actor=ReservationStatusEvent.Actor.STAFF,
    )
    if item.gown_id and item.gown.status == Gown.Status.AVAILABLE:
        item.gown.status = Gown.Status.RESERVED
        item.gown.save(update_fields=["status", "updated_at"])
    return JsonResponse({"success": True, "stage": item.stage, "picked_up_on": today.isoformat()})


@require_http_methods(["POST"])
@transaction.atomic
def reservation_item_change_pickup_view(request, item_id):
    """The customer wants the gown EARLIER: move the booking's pick-up date back. Only
    earlier (a later pick-up is just a late pick-up, counted as late), never before today,
    and never onto days another booking, a block, or another booking's cooldown holds the
    same physical gown. The booking's own pick-up moves, so everything that reads it -- the
    Rental Schedule (painted in the Pick-up colour), availability, the early count against
    original_rental_date -- follows by itself."""
    item, error = _load_open_item(request, item_id)
    if error:
        return error
    if item.stage != ReservationItem.Stage.PICKUP:
        return JsonResponse({"error": "The gown has already been picked up, so its pick-up date can't change."}, status=400)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    new_date = _parse_iso_date(data.get("date"))
    if not new_date:
        return JsonResponse({"error": "Invalid date"}, status=400)

    today = timezone.localdate()
    if new_date >= item.rental_date:
        return JsonResponse({
            "error": (
                f"The new pick-up date must be earlier than the current one "
                f"({item.rental_date:%b %d, %Y}). A customer who comes later is just marked late."
            ),
        }, status=400)
    if new_date < today:
        return JsonResponse({"error": "The new pick-up date can't be a day that has already passed."}, status=400)

    if item.gown_id:
        list(Gown.objects.select_for_update().filter(id=item.gown_id))
        conflict = _schedule_conflict(item, new_date, item.return_date)
        if conflict:
            return JsonResponse({"error": conflict}, status=409)

    old_date = item.rental_date
    if item.original_rental_date is None:
        item.original_rental_date = old_date
    item.rental_date = new_date
    item.save(update_fields=["rental_date", "original_rental_date", "updated_at"])
    days_early = ((item.original_rental_date or old_date) - new_date).days
    ReservationStatusEvent.record(
        item.reservation, f"{item.gown_name} pick-up date changed", item=item,
        detail=(
            f"Pick-up moved from {old_date:%b %d} to {new_date:%b %d, %Y} at the customer's request "
            f"({_plural_days(days_early)} earlier than originally booked)."
        ),
        actor=ReservationStatusEvent.Actor.STAFF,
    )
    return JsonResponse({"success": True, "rental_date": new_date.isoformat(), "days_early": days_early})


@require_http_methods(["POST"])
@transaction.atomic
def reservation_item_change_return_view(request, item_id):
    """The customer needs the gown LONGER: move the return date later. Only later (there
    is no early return -- bringing it back sooner changes nothing), never to a day that has
    passed, and never into days another booking or block holds the gown. The gown's own
    cooldown moves along with it. Counted as late days against the ORIGINAL return date."""
    item, error = _load_open_item(request, item_id)
    if error:
        return error
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    new_date = _parse_iso_date(data.get("date"))
    if not new_date:
        return JsonResponse({"error": "Invalid date"}, status=400)

    today = timezone.localdate()
    if new_date <= item.return_date:
        return JsonResponse({
            "error": (
                f"The new return date must be later than the current one "
                f"({item.return_date:%b %d, %Y}). There is no early return."
            ),
        }, status=400)
    if new_date < today:
        return JsonResponse({"error": "The new return date can't be a day that has already passed."}, status=400)

    if item.gown_id:
        list(Gown.objects.select_for_update().filter(id=item.gown_id))
        conflict = _schedule_conflict(item, item.rental_date, new_date)
        if conflict:
            return JsonResponse({"error": conflict}, status=409)

    old_date = item.return_date
    if item.original_return_date is None:
        item.original_return_date = old_date
    # The event day is the customer's own and doesn't move with the return -- but until it
    # is saved it is *computed* as return - 2, so freeze it before the return changes.
    if item.event_date is None:
        item.event_date = item.effective_event_date
    item.return_date = new_date
    item.overdue_date = new_date
    item.save(update_fields=["return_date", "overdue_date", "original_return_date", "event_date", "updated_at"])
    if item.gown_id:
        _resync_cooldown_block(item, new_date)
    days_late = (new_date - (item.original_return_date or old_date)).days
    ReservationStatusEvent.record(
        item.reservation, f"{item.gown_name} return date changed", item=item,
        detail=(
            f"Return moved from {old_date:%b %d} to {new_date:%b %d, %Y} at the customer's request "
            f"({_plural_days(days_late)} later than originally booked)."
        ),
        actor=ReservationStatusEvent.Actor.STAFF,
    )
    return JsonResponse({"success": True, "return_date": new_date.isoformat(), "days_late": days_late})


@require_http_methods(["POST"])
def reservation_item_undo_pickup_view(request, item_id):
    """Reverses an accidental "Mark Picked Up" click: clears picked_up_on and puts
    the item back at the Pick-up stage, exactly as if it was never clicked. Not
    owner-gated -- it only erases a record (logged all the same, below) rather than
    fabricating one; re-marking it afterwards goes through Mark Picked Up, which
    stamps today and only works on or after the pick-up date."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        item = ReservationItem.objects.select_related("reservation").get(id=item_id)
    except ReservationItem.DoesNotExist:
        return JsonResponse({"error": "Item not found"}, status=404)

    if not item.picked_up_on:
        return JsonResponse({"error": "This item hasn't been marked picked up."}, status=400)
    if item.returned_on or item.stage == ReservationItem.Stage.RETURNED:
        return JsonResponse({
            "error": "This item has already been marked returned, so its pickup can't be undone here.",
        }, status=400)

    item.picked_up_on = None
    item.stage = ReservationItem.Stage.PICKUP
    item.save(update_fields=["picked_up_on", "stage", "updated_at"])

    ReservationStatusEvent.record(
        item.reservation, f"{item.gown_name} pickup undone", item=item,
        detail="Mark Picked Up was undone -- reverted to Pick-up.",
        actor=ReservationStatusEvent.Actor.STAFF,
    )

    return JsonResponse({"success": True, "stage": item.stage})


@_require_admin_staff
def clients_view(request):
    now = timezone.now()
    customers = list(
        User.objects.filter(is_staff=False, is_superuser=False)
        .select_related("profile")
        .annotate(reservation_count=Count("reservations"))
        .prefetch_related(
            Prefetch(
                "reservations",
                queryset=Reservation.objects.order_by("-created_at"),
                to_attr="ordered_reservations",
            )
        )
        .order_by("-date_joined")
    )

    for c in customers:
        profile = getattr(c, "profile", None)
        latest = c.ordered_reservations[0] if c.ordered_reservations else None
        c.contact_phone = latest.phone if latest else ""
        c.contact_address = latest.address if latest else ""
        c.contact_city = latest.city if latest else ""
        c.contact_postal = latest.postal_code if latest else ""
        c.is_flagged = profile.is_flagged if profile else False
        # Counted from the already-prefetched rows rather than a second
        # annotate() -- a Count over the same relation as reservation_count
        # risks join multiplication, and this costs no extra query.
        c.submitted_cancellations = sum(
            1
            for r in c.ordered_reservations
            if r.status == Reservation.Status.CANCELLED
        )
        c.abandoned_holds = profile.hold_abandon_count if profile else 0
        # What actually drives the auto-flag: cancelled reservations plus
        # selections held and walked away from.
        c.cancellation_count = c.submitted_cancellations + c.abandoned_holds
        c.at_cancellation_limit = c.cancellation_count >= CANCELLATION_FLAG_THRESHOLD
        # The two lighter tiers below the flag (see accounts.services). Only
        # meaningful while cancel_lockout_until is still in the future -- it's
        # left in place (not cleared) once it passes, so this is what actually
        # tells "currently locked" apart from "was locked once, a while ago".
        lockout_until = profile.cancel_lockout_until if profile else None
        c.cancel_lockout_until = lockout_until
        c.cancel_locked_now = bool(lockout_until and lockout_until > now)
        c.display_name = UserProfile.customer_display_name(c)
        c.is_new_this_month = (
            c.date_joined.year == now.year and c.date_joined.month == now.month
        )

    return render(
        request,
        "arabela_admin/clients.html",
        {
            "page": "clients",
            "customers": customers,
            "total_customers": len(customers),
            "new_this_month": sum(1 for c in customers if c.is_new_this_month),
            "flagged_count": sum(1 for c in customers if c.is_flagged),
        },
    )


@require_http_methods(["POST"])
def customer_flag_view(request, user_id):
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        customer = User.objects.get(id=user_id, is_staff=False, is_superuser=False)
    except User.DoesNotExist:
        return JsonResponse({"error": "Customer not found"}, status=404)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    flagged = bool(data.get("flagged"))
    reason = (data.get("reason") or "").strip()
    if flagged and not reason:
        return JsonResponse(
            {"error": "Please provide a reason for flagging this account."}, status=400
        )

    profile, _ = UserProfile.objects.get_or_create(user=customer)
    profile.is_flagged = flagged
    profile.save(update_fields=["is_flagged"])

    if flagged:
        body = reason
        category = CustomerMessage.Category.ACCOUNT_FLAGGED
    else:
        body = reason or "Your account is no longer flagged."
        category = CustomerMessage.Category.ACCOUNT_UNFLAGGED
    CustomerMessage.objects.create(recipient=customer, category=category, body=body)

    return JsonResponse({"success": True, "flagged": profile.is_flagged})


def _staff_display_name(user):
    """The one name to show for a staff/manager account everywhere in admin (Staff
    Management's own table, and the notifications context processor). Deliberately
    does NOT consult UserProfile.display_name -- that field is a customer-facing
    concept (see UserProfile.customer_display_name); staff are named from the real
    name entered when the account was created."""
    return f"{user.first_name} {user.last_name}".strip() or user.get_username()


def _staff_row(user):
    """Serialize one staff/manager account for the table + JSON responses. `date_added`
    is a pre-formatted display string so the whole dict is JSON-serializable (the client
    seeds its table array from these via json_script)."""
    profile = getattr(user, "profile", None)
    return {
        "id": user.id,
        "name": _staff_display_name(user),
        "username": user.username,
        "role": (profile.role if profile else UserProfile.Role.STAFF),
        "is_active": user.is_active,
        "date_added": user.date_joined.strftime("%b %d, %Y"),
    }


def _staff_queryset():
    """The Manager & Staff roster: admin-capable accounts that are NOT owners."""
    return (
        User.objects.filter(is_staff=True, is_superuser=False)
        .exclude(profile__role=UserProfile.Role.OWNER)
        .select_related("profile")
        .order_by("-date_joined")
    )


@_require_owner
def staff_management_view(request):
    staff_accounts = [_staff_row(u) for u in _staff_queryset()]
    # The stat cards + table count only the Manager/Staff roster (the accounts this page
    # manages), NOT owner/superuser logins -- so a shop with no staff yet correctly reads 0.
    return render(
        request,
        "arabela_admin/staff-management.html",
        {
            "page": "staff-management",
            "staff_accounts": staff_accounts,
            "staff_count": len(staff_accounts),
        },
    )


_STAFF_ROLES = {UserProfile.Role.MANAGER, UserProfile.Role.STAFF}


def _split_name(full_name):
    parts = (full_name or "").strip().split(maxsplit=1)
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""
    return first, last


def _get_manageable_staff(user_id, request):
    """Resolve a target staff account for edit/status/delete, enforcing the guardrails:
    the target must be a real non-owner staff account, and the owner can't act on their
    own account or on another owner/superuser. Returns (user, None) or (None, error_response)."""
    target = User.objects.filter(id=user_id).select_related("profile").first()
    if not target or not target.is_staff:
        return None, JsonResponse({"error": "Staff account not found."}, status=404)
    if target.id == request.user.id:
        return None, JsonResponse({"error": "You can't change your own account here."}, status=400)
    profile = getattr(target, "profile", None)
    if target.is_superuser or (profile and profile.role == UserProfile.Role.OWNER):
        return None, JsonResponse({"error": "That is an owner account."}, status=400)
    return target, None


@require_http_methods(["POST"])
@_require_owner
def staff_create_view(request):
    """Create a Manager/Staff admin account. is_staff=True so it can log in at the admin
    login page; is_superuser=False so it never gains owner powers."""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid request."}, status=400)

    full_name = (data.get("name") or "").strip()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = (data.get("role") or "").strip()

    if not full_name:
        return JsonResponse({"error": "Full name is required."}, status=400)
    if not username:
        return JsonResponse({"error": "Username is required."}, status=400)
    if role not in _STAFF_ROLES:
        return JsonResponse({"error": "Choose a role of Manager or Staff."}, status=400)
    if len(password) < 8:
        return JsonResponse({"error": "Password must be at least 8 characters."}, status=400)
    if User.objects.filter(username__iexact=username).exists():
        return JsonResponse({"error": "That username is already taken."}, status=400)

    first, last = _split_name(full_name)
    user = User.objects.create_user(
        username=username,
        password=password,
        first_name=first,
        last_name=last,
        is_staff=True,
        is_superuser=False,
        is_active=True,
    )
    UserProfile.objects.update_or_create(
        user=user, defaults={"role": role, "display_name": full_name}
    )
    return JsonResponse({"success": True, "account": _staff_row(user)})


@require_http_methods(["POST"])
@_require_owner
def staff_update_view(request, user_id):
    """Edit a staff account's name/role, and optionally reset the password."""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid request."}, status=400)

    target, error = _get_manageable_staff(user_id, request)
    if error:
        return error

    full_name = (data.get("name") or "").strip()
    role = (data.get("role") or "").strip()
    password = data.get("password") or ""

    if not full_name:
        return JsonResponse({"error": "Full name is required."}, status=400)
    if role not in _STAFF_ROLES:
        return JsonResponse({"error": "Choose a role of Manager or Staff."}, status=400)
    if password and len(password) < 8:
        return JsonResponse({"error": "Password must be at least 8 characters."}, status=400)

    first, last = _split_name(full_name)
    target.first_name = first
    target.last_name = last
    if password:
        target.set_password(password)
    target.save()
    profile, _ = UserProfile.objects.get_or_create(user=target)
    profile.role = role
    profile.display_name = full_name
    profile.save(update_fields=["role", "display_name"])
    # target.profile was cached (stale) by the select_related in _get_manageable_staff;
    # point it at the freshly-saved profile so the serialized row reflects the new role.
    target.profile = profile
    return JsonResponse({"success": True, "account": _staff_row(target)})


@require_http_methods(["POST"])
@_require_owner
def change_own_password_view(request):
    """Let the currently signed-in OWNER change their own login password.

    Deliberately owner-only, by the user's explicit request: staff accounts have no
    self-service password change at all -- only the owner may change any password
    (their own here, or a staff member's via staff_update_view above). @_require_owner
    is the real enforcement; nothing about what the client sends can bypass it. This
    also means a staff account can never even reach this endpoint, so there is no
    "target user" to confuse with the caller -- it is always request.user.

    Requires the CURRENT password first (staff_update_view resetting someone ELSE's
    password doesn't need this -- the owner is already proven who they are by being
    logged in as owner; here the owner is proving it's really them, not someone who
    walked up to an unlocked session).
    """
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid request."}, status=400)

    current_password = data.get("current_password") or ""
    new_password = data.get("new_password") or ""
    confirm_password = data.get("confirm_password") or ""

    if not request.user.check_password(current_password):
        return JsonResponse({"error": "Your current password is incorrect."}, status=400)
    if len(new_password) < 8:
        return JsonResponse({"error": "New password must be at least 8 characters."}, status=400)
    if new_password != confirm_password:
        return JsonResponse({"error": "New passwords do not match."}, status=400)
    if request.user.check_password(new_password):
        return JsonResponse(
            {"error": "That's your current password. Please choose a different one."}, status=400
        )

    request.user.set_password(new_password)
    request.user.save(update_fields=["password"])
    # Changing your own password rotates Django's session auth hash -- without this the
    # owner would be silently signed out immediately after successfully changing it.
    update_session_auth_hash(request, request.user)
    return JsonResponse({"success": True})


@require_http_methods(["POST"])
@_require_owner
def staff_toggle_status_view(request, user_id):
    """Activate / deactivate a staff account -- deactivating blocks their login without
    deleting their record."""
    target, error = _get_manageable_staff(user_id, request)
    if error:
        return error
    target.is_active = not target.is_active
    target.save(update_fields=["is_active"])
    return JsonResponse({"success": True, "is_active": target.is_active, "account": _staff_row(target)})


@require_http_methods(["POST"])
@_require_owner
def staff_delete_view(request, user_id):
    """Permanently remove a staff account."""
    target, error = _get_manageable_staff(user_id, request)
    if error:
        return error
    target.delete()
    return JsonResponse({"success": True})


# Brute-force protection for the admin login: 5 failed attempts locks that username out
# for 15 minutes. Tracked in Django's cache (no new model/migration needed) -- each failed
# attempt refreshes the 15-minute window, so a lockout expires 15 minutes after the LAST
# failed try, not the first.
_LOGIN_ATTEMPT_LIMIT = 5
_LOGIN_LOCKOUT_SECONDS = 15 * 60
_LOCKOUT_MESSAGE = "Too many failed login attempts. Please try again in 15 minutes."


def _login_attempts_cache_key(username):
    return f"admin_login_attempts:{username.strip().lower()}"


def admin_login_view(request):
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")

        if not username:
            return render(
                request,
                "arabela_admin/admin-login.html",
                {"error": "Please enter your username.", "username": username},
            )

        cache_key = _login_attempts_cache_key(username)
        failed_attempts = cache.get(cache_key, 0)
        if failed_attempts >= _LOGIN_ATTEMPT_LIMIT:
            return render(
                request,
                "arabela_admin/admin-login.html",
                {"error": _LOCKOUT_MESSAGE, "username": username},
            )

        if not password:
            return render(
                request,
                "arabela_admin/admin-login.html",
                {"error": "Please enter your password.", "username": username},
            )

        authenticated_user = authenticate(request, username=username, password=password)
        if not authenticated_user:
            failed_attempts += 1
            cache.set(cache_key, failed_attempts, _LOGIN_LOCKOUT_SECONDS)
            if failed_attempts >= _LOGIN_ATTEMPT_LIMIT:
                error_message = _LOCKOUT_MESSAGE
            else:
                remaining = _LOGIN_ATTEMPT_LIMIT - failed_attempts
                error_message = (
                    f"Invalid username or password. {remaining} attempt"
                    f"{'s' if remaining != 1 else ''} remaining before temporary lockout."
                )
            return render(
                request,
                "arabela_admin/admin-login.html",
                {"error": error_message, "username": username},
            )

        if not (authenticated_user.is_staff or authenticated_user.is_superuser):
            return render(
                request,
                "arabela_admin/admin-login.html",
                {"error": "This account has no admin access.", "username": username},
            )

        # Successful login clears any accumulated failed-attempt count for this username.
        cache.delete(cache_key)
        login(request, authenticated_user)
        remember_me = request.POST.get("remember_me") == "on"
        request.session.set_expiry(settings.REMEMBER_ME_AGE if remember_me else 0)
        return redirect("arabela_admin:dashboard")

    # Already signed in as staff -- go straight to the dashboard instead of showing the
    # login form again. Signing out is a deliberate action (see admin_logout_view) so a
    # "remembered" session survives simply loading this URL (bookmark, back/forward, etc).
    if _is_admin_staff(request):
        return redirect("arabela_admin:dashboard")
    return render(request, "arabela_admin/admin-login.html")


@require_http_methods(["POST"])
def admin_logout_view(request):
    """Explicit, deliberate sign-out -- POST-only so it can't fire from merely loading a
    URL (a GET-triggered logout would defeat 'Keep me logged in' any time that URL is hit
    for any reason: a bookmark, browser back/forward, etc.)."""
    logout(request)
    return redirect("arabela_admin:admin_login")


def page_view(request, page: str):
    # Only the real Edit Profile / Account Settings pages. Note this deliberately
    # EXCLUDES calendar / payment-verification / security-deposits: those have their
    # own gated named routes, and letting the "<page>.html" catch-all also render
    # them bypassed that auth (and served them with no context).
    allowed_pages = {
        "profile",
        "account-settings",
    }
    if page not in allowed_pages:
        raise Http404("Page not found")

    # Every branch requires a signed-in staff session -- these are admin-panel pages.
    if not _is_admin_staff(request):
        return redirect("arabela_admin:admin_login")

    if page == "profile":
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        return render(request, "arabela_admin/profile.html", {
            "first_name": request.user.first_name,
            "last_name": request.user.last_name,
            "email": request.user.email,
            "bio": profile.display_name,
        })

    # page == "account-settings"
    # admin_full_name/admin_role/admin_is_owner already come from the global admin
    # context processor -- only the raw login username is specific to this page.
    return render(request, "arabela_admin/account-settings.html", {
        "admin_username": request.user.get_username(),
    })


@require_http_methods(["POST"])
def save_profile_view(request):
    """Save admin staff profile information (name/email/bio, personal to the
    logged-in user) plus the site-wide contact info (Facebook/phone/shop
    address, shared with the public site via SiteSettings)."""
    if not request.user.is_authenticated or not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    try:
        data = json.loads(request.body)

        # Update User fields
        if 'firstName' in data:
            request.user.first_name = data['firstName']
        if 'lastName' in data:
            request.user.last_name = data['lastName']
        if 'email' in data:
            request.user.email = data['email']
        request.user.save()

        # Update UserProfile fields
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        if 'bio' in data:
            profile.display_name = data['bio']
        profile.save()

        # Update site-wide contact info (public Facebook/phone/shop address). These are
        # core business settings -- OWNER ONLY. A non-owner staffer editing their profile
        # simply has these keys ignored (their personal name/email/bio above still saved),
        # and the UI hides these sections from them anyway; this is the server-side guard.
        site_fields = {'facebook', 'phone', 'country', 'cityState', 'postalCode', 'street'}
        if (site_fields & data.keys()) and _is_owner(request):
            settings_obj = SiteSettings.load()
            if 'facebook' in data:
                settings_obj.facebook_url = data['facebook']
            if 'phone' in data:
                settings_obj.phone = data['phone']
            if 'street' in data:
                settings_obj.shop_street = data['street']
            if 'country' in data:
                settings_obj.shop_country = data['country']
            if 'cityState' in data:
                settings_obj.shop_city = data['cityState']
            if 'postalCode' in data:
                settings_obj.shop_postal_code = data['postalCode']
            settings_obj.save()

        return JsonResponse({"success": True, "message": "Profile updated successfully"})
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


_AVATAR_MAX_BYTES = 5 * 1024 * 1024
_AVATAR_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _clamp_position(value, default=50):
    try:
        return max(0, min(100, round(float(value))))
    except (TypeError, ValueError):
        return default


def _delete_old_avatar(profile):
    """Remove the previously uploaded file so replacing a photo doesn't orphan it.
    Only touches files we saved ourselves under profile_pictures/ -- an externally
    hosted picture (e.g. the Google avatar set by accounts/adapters.py on social
    login) is left alone. Matches on the folder name appearing anywhere in the
    URL (rather than requiring it to start with MEDIA_URL) so this still finds
    the right file whether it's on local disk (/media/profile_pictures/x.jpg) or
    on Cloudinary (https://res.cloudinary.com/.../profile_pictures/x.jpg)."""
    old = profile.profile_picture_url or ""
    marker = "profile_pictures/"
    idx = old.find(marker)
    if idx == -1:
        return
    rel = old[idx:]
    try:
        if default_storage.exists(rel):
            default_storage.delete(rel)
    except Exception:
        # A missing/locked old file must never block setting the new one.
        pass


@require_http_methods(["POST"])
def upload_profile_picture_view(request):
    """Store a new avatar for the signed-in staff member and return its URL so the
    page can swap the image in without a reload."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    upload = request.FILES.get("profile_picture")
    if not upload:
        return JsonResponse({"error": "No image was selected."}, status=400)

    if upload.size > _AVATAR_MAX_BYTES:
        return JsonResponse({"error": "Image must be 5MB or smaller."}, status=400)

    ext = os.path.splitext(upload.name)[1].lower()
    if ext not in _AVATAR_EXTENSIONS:
        return JsonResponse(
            {"error": "Use a JPG, PNG, WEBP or GIF image."}, status=400
        )

    # Confirm the bytes really are an image, not just a renamed file.
    try:
        from PIL import Image

        Image.open(upload).verify()
    except Exception:
        return JsonResponse({"error": "That file isn't a valid image."}, status=400)
    upload.seek(0)

    try:
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        _delete_old_avatar(profile)
        saved_path = default_storage.save(
            f"profile_pictures/{uuid.uuid4().hex}{ext}", upload
        )
        profile.profile_picture_url = default_storage.url(saved_path)
        # A freshly chosen photo is positioned via the same drag-to-place step the
        # upload form always runs first, so trust whatever it sent (default center
        # if it's missing for any reason) rather than keeping the old photo's crop.
        profile.avatar_position_x = _clamp_position(request.POST.get("position_x"))
        profile.avatar_position_y = _clamp_position(request.POST.get("position_y"))
        profile.save(update_fields=["profile_picture_url", "avatar_position_x", "avatar_position_y"])
        return JsonResponse({
            "success": True,
            "url": profile.profile_picture_url,
            "position_x": profile.avatar_position_x,
            "position_y": profile.avatar_position_y,
        })
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@require_http_methods(["POST"])
def remove_profile_picture_view(request):
    """Clear the avatar, falling back to the default person icon."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    try:
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        _delete_old_avatar(profile)
        profile.profile_picture_url = ""
        profile.avatar_position_x = 50
        profile.avatar_position_y = 50
        profile.save(update_fields=["profile_picture_url", "avatar_position_x", "avatar_position_y"])
        return JsonResponse({"success": True, "url": ""})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@require_http_methods(["POST"])
def update_avatar_position_view(request):
    """Reposition the currently saved photo (no new file) -- used by 'Adjust position'
    on an avatar that's already uploaded."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    try:
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        if not profile.profile_picture_url:
            return JsonResponse({"error": "No photo to reposition."}, status=400)
        profile.avatar_position_x = _clamp_position(data.get("position_x"), profile.avatar_position_x)
        profile.avatar_position_y = _clamp_position(data.get("position_y"), profile.avatar_position_y)
        profile.save(update_fields=["avatar_position_x", "avatar_position_y"])
        return JsonResponse({
            "success": True,
            "position_x": profile.avatar_position_x,
            "position_y": profile.avatar_position_y,
        })
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


def admin_search_view(request):
    """Global header search (all admin pages): matches reservations by customer name
    or reference code, and matches gowns by gown ID, name, or category. Each result
    points at the list page an admin would actually act on it from -- for reservations
    that's Pending Approval while awaiting review, Active Reservations once approved,
    Payment Verification as the catch-all for everything else (it lists every
    reservation regardless of status); for gowns it's always Gown Catalog. Those target
    pages read the ?search= query string back into their own row filter, so the click
    actually lands pre-filtered (and, on Gown Catalog, highlighted)."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    query = (request.GET.get("q") or "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})

    matches = (
        Reservation.objects.filter(
            Q(customer_name__icontains=query)
            | Q(reference_code__icontains=query)
            | Q(customer__profile__display_name__icontains=query)
        )
        .select_related("customer__profile")
        .order_by("-created_at")[:5]
    )

    results = []
    for r in matches:
        if r.status == Reservation.Status.PENDING:
            target_name = "arabela_admin:pending_approval"
        elif r.status in _SCHEDULED_STATUSES:
            target_name = "arabela_admin:active_reservations"
        else:
            target_name = "arabela_admin:payment_verification"
        name = r.display_customer_name
        target_url = reverse(target_name) + "?" + urlencode({"search": name})
        results.append({
            "type": "reservation",
            "customer_name": name,
            "reference_code": r.reference_code,
            "status": r.status,
            "target_url": target_url,
        })

    # Gowns are matched the same way the catalog's own local search already matches them
    # (gown_id/name/category, case-insensitive) -- landing there with ?search=<gown_id>
    # reuses that existing filter + row highlight, no new page logic needed.
    gown_matches = (
        Gown.objects.filter(
            Q(gown_id__icontains=query)
            | Q(name__icontains=query)
            | Q(category__icontains=query)
        )
        .order_by("gown_id")[:5]
    )
    for g in gown_matches:
        target_url = reverse("arabela_admin:gown_catalog") + "?" + urlencode({"search": g.gown_id})
        results.append({
            "type": "gown",
            "gown_id": g.gown_id,
            "gown_name": g.name,
            "category": g.category,
            "status": g.status,
            "target_url": target_url,
        })

    return JsonResponse({"results": results})
