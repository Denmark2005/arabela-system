"""The admin's New Booking page and the "Check availability" answers it shares with Gown Catalog.

Staff (and the owner) record a booking that didn't come through the website: a walk-in, a Facebook message, a phone call.
Everything that decides and saves is reservations/staff_booking.py; the availability rules are gowns/availability.py (the
same ones the customer website and checkout use). This module is only the web side: the page, its small JSON lookups, and
turning the form into a call to staff_booking.record_booking.
"""
import json
import logging

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.http import require_http_methods

from arabela_admin.views import (
    _is_admin_staff,
    _is_owner,
    _receipt_duplicate_error,
    _receipt_file_hash,
    _require_admin_staff,
    _save_receipt_photo,
    _staff_display_name,
    _validate_receipt_photo,
)
from gowns import availability
from gowns.models import TAG_COLOR_HEX, Gown, SiteSettings
from gowns.photos import sized
from reservations import staff_booking
from reservations.models import ReceiptRecord, Reservation

logger = logging.getLogger(__name__)

THUMB_WIDTH = 160
SEARCH_LIMIT = 15


def _peso(amount):
    return f"₱{amount:,.0f}" if amount == int(amount) else f"₱{amount:,.2f}"


def _day(day):
    return f"{day:%b} {day.day}, {day.year}" if day else ""


def _gown_card(gown, tag_colors):
    """What the page shows about one physical gown: enough for staff to match it to the dress in their hands."""
    color = tag_colors.get(gown.category, "")
    return {
        "id": gown.id,
        "gownId": gown.gown_id,
        "name": gown.name,
        "category": gown.category,
        "size": gown.size,
        "color": gown.color_name,
        "price": _peso(gown.rental_price),
        "priceValue": float(gown.rental_price),
        "photo": sized(gown.photo_url, THUMB_WIDTH) if gown.photo_url else "",
        "status": gown.status,
        "outOfStock": gown.status == Gown.Status.OUT_OF_STOCK,
        "tagColor": color,
        "tagHex": TAG_COLOR_HEX.get(color, ""),
        "number": gown.tracking_number,
    }


def _backdate_days(request):
    return availability.OWNER_BACKDATE_DAYS if _is_owner(request) else 0


# ---------------------------------------------------------------- the page
@_require_admin_staff
def new_booking_view(request):
    today = availability.now_local().date()
    return render(request, "arabela_admin/new-booking.html", {
        "page": "new-booking",
        "today_iso": today.isoformat(),
        "is_owner": _is_owner(request),
        "owner_backdate_days": availability.OWNER_BACKDATE_DAYS,
        "deposit_per_gown": int(staff_booking.DEPOSIT_PER_GOWN),
        "max_gowns": staff_booking.MAX_GOWNS,
        "max_receipts": staff_booking.MAX_RECEIPTS,
        "note_max": staff_booking.NOTE_MAX,
        "channels": [(value, label) for value, label in Reservation.Channel.choices if value != Reservation.Channel.ONLINE],
        "covers_choices": ReceiptRecord.Covers.values,
    })


# ---------------------------------------------------------------- lookups
def booking_gown_search_view(request):
    """Gowns matching what staff typed: gown ID, tag number, name, color or category. Out-of-Stock gowns are listed (so
    staff aren't left wondering) but marked, and can't be booked."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    query = (request.GET.get("q") or "").strip()
    if len(query) < 1:
        return JsonResponse({"results": []})
    match = (Q(gown_id__icontains=query) | Q(name__icontains=query) | Q(color_name__icontains=query)
             | Q(category__icontains=query))
    if query.isdigit():
        # "12" finds the gown whose tag number is 12 ("...-012"), not only IDs that happen to contain "12".
        match |= Q(gown_id__endswith=f"-{int(query):03d}") | Q(gown_id__endswith=f"-{query}")
    gowns = list(Gown.objects.filter(match).order_by("status", "category", "name", "gown_id")[:SEARCH_LIMIT])
    tag_colors = SiteSettings.load().tag_colors()
    return JsonResponse({"results": [_gown_card(g, tag_colors) for g in gowns]})


def _check_payload(gown, result, *, now, tag_colors, with_extras=True):
    payload = {
        "ok": result["ok"],
        "reason": result["reason"],
        "message": availability.check_message(gown, result),
        # The same answer without the list of what's in the way: shown above that list on screen.
        "headline": availability.check_message(gown, result, with_names=not result["conflicts"]),
        "gown": _gown_card(gown, tag_colors),
        "pickup": result["pickup"].isoformat() if result["pickup"] else "",
        "pickupText": _day(result["pickup"]),
        "event": result["event"].isoformat(),
        "eventText": _day(result["event"]),
        "returnDate": result["return_date"].isoformat(),
        "returnText": _day(result["return_date"]),
        "restUntil": result["rest_until"].isoformat(),
        "restUntilText": _day(result["rest_until"]),
        "conflicts": [
            {"kind": c["kind"], "label": c["label"], "reference": c["reference"],
             "start": c["start"].isoformat(), "end": c["end"].isoformat(),
             "text": f"{c['label']}, {_day(c['start'])} – {_day(c['end'])}",
             "url": (reverse("arabela_admin:reservation_records") + "?" + urlencode({"search": c["reference"]}))
             if c["reference"] else ""}
            for c in result["conflicts"]
        ],
        "nextFree": "",
        "nextFreeText": "",
        "alternatives": [],
    }
    if with_extras and not result["ok"] and result["reason"] in ("booked", "rest"):
        nxt = availability.next_free_event(gown, result["event"], now=now)
        payload["nextFree"] = nxt.isoformat() if nxt else ""
        payload["nextFreeText"] = _day(nxt)
        # Other gowns of the same design (any size) that ARE free on this exact date -- the other option to offer.
        for other in (Gown.objects.filter(category=gown.category, name__iexact=gown.name)
                      .exclude(id=gown.id).exclude(status=Gown.Status.OUT_OF_STOCK).order_by("gown_id")[:12]):
            other_result = availability.check_unit(
                other, result["event"], picked_up_on=result.get("picked_up_on"), now=now,
                backdate_days=result.get("backdate_days", 0),
            )
            if other_result["ok"]:
                payload["alternatives"].append(_gown_card(other, tag_colors))
    return payload


def availability_check_view(request):
    """GET ?gown=<id>&event=YYYY-MM-DD[&takes_now=1[&picked_up_on=YYYY-MM-DD]] -> can this exact gown be booked?
    Read-only: nothing is saved. Used by New Booking and by Gown Catalog's "Check availability"."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        gown = Gown.objects.get(id=int(request.GET.get("gown") or 0))
    except (Gown.DoesNotExist, TypeError, ValueError):
        return JsonResponse({"error": "That gown wasn't found. Search for it again."}, status=404)
    event = staff_booking._date(request.GET.get("event"))
    if event is None:
        return JsonResponse({"error": "Choose the event date."}, status=400)
    now = availability.now_local()
    picked_up_on = None
    if request.GET.get("takes_now") in ("1", "true", "on"):
        picked_up_on = staff_booking._date(request.GET.get("picked_up_on")) or now.date()
        if picked_up_on != now.date() and not _is_owner(request):
            picked_up_on = now.date()        # staff can only record today's pick-up; the form never offers more
    backdate = _backdate_days(request)
    result = availability.check_unit(gown, event, picked_up_on=picked_up_on, now=now, backdate_days=backdate)
    result["picked_up_on"], result["backdate_days"] = picked_up_on, backdate
    return JsonResponse(_check_payload(gown, result, now=now, tag_colors=SiteSettings.load().tag_colors()))


def booking_customer_hint_view(request):
    """GET ?name=&phone= -> earlier bookings that look like the same customer (same phone, or exactly the same name)."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    matches = staff_booking.same_customer_hint(request.GET.get("name") or "", request.GET.get("phone") or "")
    return JsonResponse({"results": [
        {
            "reference": r.reference_code,
            "customer": r.display_customer_name,
            "phone": staff_booking.phone_display(r.phone),
            "channel": r.get_channel_display(),
            "status": r.status,
            "booked": _day(r.created_at.date()),
            "url": reverse("arabela_admin:reservation_records") + "?" + urlencode({"search": r.reference_code}),
        }
        for r in matches
    ]})


def booking_account_search_view(request):
    """GET ?q= -> customers' website accounts (never staff or owner accounts) to link a booking to."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    query = (request.GET.get("q") or "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})
    users = (
        get_user_model().objects.filter(is_staff=False, is_superuser=False, is_active=True)
        .filter(Q(email__icontains=query) | Q(first_name__icontains=query) | Q(last_name__icontains=query)
                | Q(profile__display_name__icontains=query))
        .select_related("profile").order_by("first_name", "last_name", "id")[:8]
    )
    results = []
    for user in users:
        profile = getattr(user, "profile", None)
        name = (profile.display_name if profile and profile.display_name else "") or user.get_full_name() or user.email
        results.append({"id": user.id, "name": name, "email": user.email})
    return JsonResponse({"results": results})


# ---------------------------------------------------------------- saving
@require_http_methods(["POST"])
def booking_create_view(request):
    """Save the booking (multipart: the form's fields, `lines` as JSON, and up to two receipt photos)."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    post = request.POST
    is_owner = _is_owner(request)
    now = availability.now_local()
    try:
        name = staff_booking.clean_name(post.get("name"))
        phone = staff_booking.clean_phone(post.get("phone"))
        notebook_number = staff_booking.clean_notebook_number(post.get("notebook_number"))
        account = staff_booking.find_account(post.get("account"))
        try:
            raw_lines = json.loads(post.get("lines") or "[]")
        except json.JSONDecodeError:
            raise staff_booking.BookingError("Something in the gown list is wrong. Please refresh the page and try again.")
        lines = staff_booking.parse_lines(raw_lines, today=now.date(), is_owner=is_owner)
        paid = post.get("paid") == "1"
        if post.get("paid") not in ("0", "1"):
            raise staff_booking.BookingError("Choose \"Paid in full\" or \"Not paid yet\".")

        receipts, hashes = [], set()
        for index in range(1, staff_booking.MAX_RECEIPTS + 1):
            photo = request.FILES.get(f"receipt_{index}")
            if not photo:
                continue
            error = _validate_receipt_photo(photo)
            if error:
                raise staff_booking.BookingError(error)
            file_hash = _receipt_file_hash(photo)
            if file_hash in hashes:
                raise staff_booking.BookingError("The same receipt photo was attached twice. Attach each receipt once.")
            duplicate = _receipt_duplicate_error(file_hash)
            if duplicate:
                raise staff_booking.BookingError(duplicate, status=409)
            hashes.add(file_hash)
            receipts.append((photo, (post.get(f"receipt_{index}_covers") or "").strip(), file_hash))

        reservation = staff_booking.record_booking(
            recorded_by=request.user,
            is_owner=is_owner,
            staff_name=_staff_display_name(request.user),
            channel=(post.get("channel") or "").strip(),
            name=name,
            phone=phone,
            account=account,
            address=(post.get("address") or "").strip(),
            notebook_number=notebook_number,
            lines=lines,
            paid=paid,
            payment_method=(post.get("payment_method") or "").strip(),
            receipts=receipts,
            note=post.get("note") or "",
            save_receipt_photo=_save_receipt_photo,
            now=now,
        )
    except staff_booking.BookingError as exc:
        return JsonResponse({"error": exc.message}, status=exc.status)
    except Exception:
        logger.exception("New Booking could not be saved")
        return JsonResponse({"error": "The booking couldn't be saved just now. Nothing was booked. Please try again."},
                            status=500)

    confirmed = reservation.status == Reservation.Status.CONFIRMED
    target = "arabela_admin:active_reservations" if confirmed else "arabela_admin:pending_approval"
    return JsonResponse({
        "success": True,
        "reference_code": reservation.reference_code,
        "status": reservation.status,
        "url": reverse(target) + "?" + urlencode({"search": reservation.reference_code}),
        "where": "Active Reservations" if confirmed else "Pending Approval",
    })
