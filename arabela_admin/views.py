import os
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.conf import settings
from django.contrib.auth import authenticate, login, logout, get_user_model
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.db.models import Q, Count, Prefetch
from django.db.models.functions import ExtractMonth
from django.shortcuts import redirect, render
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_http_methods
import json

from accounts.models import CustomerMessage, UserProfile
from accounts.services import CANCELLATION_FLAG_THRESHOLD
from gowns.models import Gown, GownUnavailability, SiteSettings
from reservations.models import Reservation, ReservationItem

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

    # Monthly Rentals chart: how many GOWNS went out in each month of the current year.
    # Counts ReservationItem (one row per gown), not Reservation -- a booking with three
    # gowns is three rentals -- and keys off rental_date (the day the gown actually
    # leaves) rather than created_at (the day it was booked). Rental History lists the
    # same rows keyed the same way, so clicking a bar lands on exactly that many rows;
    # keying the two differently is what would make the drill-down lie.
    current_year = timezone.localdate().year
    counts_by_month = {
        row["month"]: row["count"]
        for row in (
            ReservationItem.objects.filter(rental_date__year=current_year)
            .exclude(reservation__status__in=_NON_RENTAL_STATUSES)
            .annotate(month=ExtractMonth("rental_date"))
            .values("month")
            .annotate(count=Count("id"))
        )
    }
    monthly_rentals = [counts_by_month.get(m, 0) for m in range(1, 13)]

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
                status__in=[Gown.Status.IN_CLEANING, Gown.Status.OUT_OF_STOCK]
            ).count(),
            # Drives the empty-state call to action: with no gowns the shop cannot take
            # a real booking at all, so it is the single most important thing to surface.
            "catalog_is_empty": gown_total == 0,
            "monthly_rentals": monthly_rentals,
            "monthly_rentals_year": current_year,
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
    """The customer's event/reserved day. Falls back to 2 days after pick-up (the default
    2-before / event / 2-after window) for any legacy row that has no explicit event date."""
    return item.event_date or (item.rental_date + timedelta(days=2))


def _overdue_date(item):
    """The day the Overdue status marks. Defaults to the return date for any legacy row
    that has no explicit overdue date."""
    return item.overdue_date or item.return_date


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
    day = _overdue_date(item)
    return day, day


def _stage_segments(item):
    """Which marker(s) show for the booking's CURRENT admin-chosen status -- this is
    cumulative, not exclusive, so the calendar always shows the full picture so far:
      Pick-up  -> Pick-up only (nothing else is relevant until the gown is out)
      Reserved -> Reserved AND Return together (admin sees it's out + when it's due back)
      Overdue  -> Reserved + Return + Overdue together (the overdue flag adds on top,
                  it never replaces the history of when it was reserved/due)
    Each entry is (label, span_fn)."""
    if item.stage == ReservationItem.Stage.PICKUP:
        return [("Pick-up", _pickup_span)]
    if item.stage == ReservationItem.Stage.RESERVED:
        return [("Reserved", _reserved_span), ("Return", _return_span)]
    if item.stage == ReservationItem.Stage.OVERDUE:
        return [("Reserved", _reserved_span), ("Return", _return_span), ("Overdue", _overdue_span)]
    return []  # RETURNED (or anything unexpected) -- nothing shown


def _calendar_events(reservations):
    """Each active booking renders as one or more markers depending on its current status
    (see _stage_segments) -- Reserved and Overdue build UP on what came before instead of
    replacing it, so the calendar always shows the whole story for that booking so far.
    Every marker for the same booking shares the same itemId/customer/gown/reference/stage,
    so clicking any of them opens the same booking panel. Returned bookings drop off."""
    events = []
    for reservation in reservations:
        for item in reservation.items.all():
            label = f"{reservation.display_customer_name} — {item.gown_name}"
            base_props = {
                "itemId": item.id,
                "customer": reservation.display_customer_name,
                "reference": reservation.reference_code,
                "gownName": item.gown_name,
                "stage": item.stage,
                "rentalDate": item.rental_date.isoformat(),
                "eventDate": _event_date(item).isoformat(),
                "returnDate": item.return_date.isoformat(),
                "overdueDate": _overdue_date(item).isoformat(),
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
    edited from the gown's own row in the Gown Catalog, which is where they're created."""
    events = []
    for block in blocks:
        note = f" · {block.note}" if block.note else ""
        events.append({
            "id": f"block-{block.id}",
            "title": f"{block.reason} · {block.gown.gown_id} — {block.gown.name}{note}",
            "start": block.start_date.isoformat(),
            "end": (block.end_date + timedelta(days=1)).isoformat(),
            "allDay": True,
            "extendedProps": {"calendar": "Blocked", "blockId": block.id},
        })
    return events


@_require_admin_staff
def rental_schedule_view(request):
    reservations = (
        Reservation.objects.filter(status__in=_SCHEDULED_STATUSES)
        .select_related("customer__profile")
        .prefetch_related("items")
    )
    blocks = GownUnavailability.objects.filter(
        end_date__gte=timezone.localdate()
    ).select_related("gown")
    return render(
        request,
        "arabela_admin/calendar.html",
        {
            "page": "rental",
            "calendar_events": _calendar_events(reservations) + _unavailability_events(blocks),
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
    return render(
        request,
        "arabela_admin/payment-verification.html",
        {
            "page": "payment-verification",
            "reservations": reservations,
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
    """Every gown that has gone out, one row per gown.

    Deliberately keyed exactly like the dashboard's Monthly Rentals chart -- one
    ReservationItem per row, bucketed by rental_date, excluding bookings that never
    became rentals -- so clicking a bar lands on precisely that many rows. If the two
    ever diverge the drill-down silently lies about the numbers.
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
    return JsonResponse({
        "success": True,
        "deposit_returned_at": reservation.deposit_returned_at.isoformat(),
    })


@_require_admin_staff
def receipt_records_view(request):
    return render(request, "arabela_admin/receipt-records.html", {"page": "receipt-records"})


@_require_admin_staff
def active_reservations_view(request):
    reservations = (
        Reservation.objects.filter(status__in=_SCHEDULED_STATUSES)
        .select_related("customer__profile")
        .prefetch_related("items")
    )
    return render(
        request,
        "arabela_admin/active-reservations.html",
        {"page": "active", "reservations": reservations},
    )


@_require_admin_staff
def pending_approval_view(request):
    reservations = (
        Reservation.objects.filter(status=Reservation.Status.PENDING)
        .select_related("customer__profile")
        .prefetch_related("items")
    )
    return render(
        request,
        "arabela_admin/pending-approval.html",
        {"page": "pending", "reservations": reservations},
    )


@_require_admin_staff
def gown_catalog_view(request):
    today = timezone.localdate()
    gowns = Gown.objects.all()

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

    return render(
        request,
        "arabela_admin/gown-catalog.html",
        {
            "page": "gown",
            "gowns": gowns,
            "gown_blocks": dict(blocks_by_gown),
            "block_reasons": GownUnavailability.Reason.values,
            "today_iso": today.isoformat(),
            "blocked_today_count": sum(
                1 for rows in blocks_by_gown.values() if any(r["active"] for r in rows)
            ),
            "available_count": gowns.filter(status=Gown.Status.AVAILABLE).count(),
            "reserved_count": gowns.filter(status=Gown.Status.RESERVED).count(),
            "needs_attention_count": gowns.filter(
                status__in=[Gown.Status.IN_CLEANING, Gown.Status.OUT_OF_STOCK]
            ).count(),
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


@require_http_methods(["POST"])
def gown_delete_view(request, gown_id):
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        gown = Gown.objects.get(id=gown_id)
    except Gown.DoesNotExist:
        return JsonResponse({"error": "Gown not found"}, status=404)
    gown.delete()
    return JsonResponse({"success": True})


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

    # One specific message per field instead of a single generic OR-check, so staff
    # can tell exactly what's wrong instead of guessing across five possibilities.
    if not name:
        return JsonResponse({"error": "Please enter a gown name."}, status=400)
    if category not in Gown.Category.values:
        return JsonResponse({"error": "Please choose a valid category."}, status=400)
    if not color_name:
        return JsonResponse({"error": "Please enter a color name."}, status=400)
    if not color_code:
        return JsonResponse({"error": "Please enter a color code."}, status=400)
    if size not in Gown.Size.values:
        return JsonResponse({"error": "Please choose a valid size."}, status=400)

    if status not in Gown.Status.values:
        status = Gown.Status.AVAILABLE

    price_raw = str(request.POST.get("rental_price", "")).replace(",", "").replace("₱", "").strip()
    try:
        rental_price = Decimal(price_raw)
        if rental_price <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return JsonResponse({"error": "Please enter a valid rental price."}, status=400)

    photo_file = request.FILES.get("photo")
    photo_error = _validate_gown_photo(photo_file)
    if photo_error:
        return JsonResponse({"error": photo_error}, status=400)

    tracking_number = Gown.next_tracking_number(category, color_code)
    gown_id = f"{category}-{color_code}-{tracking_number:03d}-A"

    gown = Gown.objects.create(
        gown_id=gown_id,
        name=name,
        category=category,
        color_name=color_name,
        color_code=color_code,
        size=size,
        design_variant=design_variant,
        rental_price=rental_price,
        status=status,
        photo_url=_save_gown_photo(photo_file) if photo_file else "",
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

    gown.photo_url = _save_gown_photo(photo_file)
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

    reason = data.get("reason")
    if reason not in GownUnavailability.Reason.values:
        reason = GownUnavailability.Reason.CLEANING

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
    return JsonResponse({"success": True, "status": reservation.status})


@require_http_methods(["POST"])
def reservation_item_mark_returned_view(request, item_id):
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        item = ReservationItem.objects.select_related("gown").get(id=item_id)
    except ReservationItem.DoesNotExist:
        return JsonResponse({"error": "Item not found"}, status=404)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    condition = data.get("condition", "")
    if condition not in ReservationItem.ReturnCondition.values:
        return JsonResponse({"error": "A return condition (Good, Fair, or Needs Repair) is required."}, status=400)

    item.stage = ReservationItem.Stage.RETURNED
    item.returned_on = timezone.localdate()
    item.return_condition = condition
    item.save(update_fields=["stage", "returned_on", "return_condition", "updated_at"])

    if item.gown_id:
        item.gown.condition = condition
        item.gown.status = (
            Gown.Status.OUT_OF_STOCK
            if condition == ReservationItem.ReturnCondition.NEEDS_REPAIR
            else Gown.Status.AVAILABLE
        )
        item.gown.last_returned_at = timezone.localdate()
        item.gown.save(update_fields=["condition", "status", "last_returned_at", "updated_at"])

    return JsonResponse({
        "success": True, "stage": item.stage,
        "returned_on": item.returned_on.isoformat(),
        "return_condition": item.return_condition,
    })


@require_http_methods(["POST"])
def reservation_item_reschedule_view(request, item_id):
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        item = ReservationItem.objects.select_related("gown").get(id=item_id)
    except ReservationItem.DoesNotExist:
        return JsonResponse({"error": "Item not found"}, status=404)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    try:
        rental_date = datetime.strptime(data.get("rental_date", ""), "%Y-%m-%d").date()
        event_date = datetime.strptime(data.get("event_date", ""), "%Y-%m-%d").date()
        return_date = datetime.strptime(data.get("return_date", ""), "%Y-%m-%d").date()
        overdue_date = datetime.strptime(data.get("overdue_date", ""), "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"error": "Invalid dates"}, status=400)

    # Pick-up <= Event <= Return <= Overdue keeps the four status dates in the right order.
    if not (rental_date <= event_date <= return_date <= overdue_date):
        return JsonResponse(
            {"error": "Dates must run Pick-up ≤ Event ≤ Return ≤ Overdue."}, status=400
        )

    # Status is chosen by staff -- Pick-up, Reserved, or Overdue. "Return" is no longer
    # picked directly; it now shows automatically alongside Reserved (and Overdue), see
    # _stage_segments.
    stage = data.get("stage")
    settable = {
        ReservationItem.Stage.PICKUP, ReservationItem.Stage.RESERVED,
        ReservationItem.Stage.OVERDUE,
    }
    if stage not in settable:
        return JsonResponse({"error": "Invalid status"}, status=400)

    item.rental_date = rental_date
    item.event_date = event_date
    item.return_date = return_date
    item.overdue_date = overdue_date
    item.stage = stage
    update_fields = ["rental_date", "event_date", "return_date", "overdue_date", "stage", "updated_at"]

    # Leaving Pick-up implies the gown has actually gone out -- reflect it in the catalog.
    if stage != ReservationItem.Stage.PICKUP and not item.picked_up_on:
        item.picked_up_on = timezone.localdate()
        update_fields.append("picked_up_on")
    item.save(update_fields=update_fields)

    if (stage != ReservationItem.Stage.PICKUP and item.gown_id
            and item.gown.status == Gown.Status.AVAILABLE):
        item.gown.status = Gown.Status.RESERVED
        item.gown.save(update_fields=["status", "updated_at"])

    return JsonResponse({
        "success": True,
        "stage": item.stage,
        "rental_date": item.rental_date.isoformat(),
        "event_date": item.event_date.isoformat(),
        "return_date": item.return_date.isoformat(),
        "overdue_date": item.overdue_date.isoformat(),
    })


@_require_admin_staff
def categories_view(request):
    return render(request, "arabela_admin/categories.html", {"page": "categories"})


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
    # Only the static template-pack demo pages + the real Edit Profile page. Note this
    # deliberately EXCLUDES calendar / payment-verification / security-deposits: those
    # have their own gated named routes, and letting the "<page>.html" catch-all also
    # render them bypassed that auth (and served them with no context).
    allowed_pages = {
        "alerts",
        "badge",
        "buttons",
        "form-elements",
        "profile",
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

    return render(request, f"arabela_admin/{page}.html")


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
    or reference code, and points each result at the list page an admin would actually
    act on it from -- Pending Approval while awaiting review, Active Reservations once
    approved, Payment Verification as the catch-all for everything else (it lists every
    reservation regardless of status). Those target pages read the ?search= query string
    back into their own row filter, so the click actually lands pre-filtered."""
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
        .order_by("-created_at")[:8]
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
            "customer_name": name,
            "reference_code": r.reference_code,
            "status": r.status,
            "target_url": target_url,
        })

    return JsonResponse({"results": results})
