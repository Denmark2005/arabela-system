import json
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.files.storage import default_storage
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from accounts.models import CustomerMessage, UserProfile
from accounts.services import record_abandoned_hold, sync_cancellation_flag
from gowns.context_processors import (
    _CATEGORIES,
    _FALLBACK_IMG,
    _NUMERIC,
    _SLUG_ORDER,
    clear_reservation_hold,
    get_reservation_hold_deadline,
    start_reservation_hold,
)
from gowns.models import Gown, GownUnavailability
from reservations.models import Reservation, ReservationItem

_VALID_COLLECTIONS = frozenset(category["key"] for category in _CATEGORIES)
_SEARCH_PRICES = (1600, 1800, 2000, 2200, 2400, 2600, 2800, 3000)

# How far ahead the product calendar computes availability. Four months is well past
# any realistic booking lead time and keeps the per-day scan trivially cheap.
_AVAILABILITY_HORIZON_DAYS = 120


def _label_for(collection_key: str) -> str:
    for category in _CATEGORIES:
        if category["key"] == collection_key:
            return category["label"]
    return "Wedding Gown"


def _title_and_label(collection_key: str, slug: str) -> tuple[str, str]:
    idx = _SLUG_ORDER.index(slug) if slug in _SLUG_ORDER else 0
    n = _NUMERIC[idx]
    label = _label_for(collection_key)
    return (f"{label} {n}", label)


def _products_for_category(collection_key: str) -> list[dict]:
    """Real Gown rows for this category if any exist -- once a category has real
    inventory, customers browse the actual gowns instead of the placeholder. Falls
    back to the 8 fake products ("X One" through "X Eight") for any category still
    at zero real inventory, so the rest of the demo is untouched."""
    label = _label_for(collection_key)
    real_gowns = (
        Gown.objects.filter(category=label)
        .exclude(status=Gown.Status.OUT_OF_STOCK)
        .order_by("color_code", "gown_id")
    )
    if real_gowns:
        return [
            {
                "slug": g.slug,
                "title": g.name,
                # "price" stays a bare number -- the grid's onclick handler embeds it
                # unquoted as a JS literal; "price_display" is the text shown on the card.
                "price": int(g.rental_price),
                "price_display": f"₱{g.rental_price:,.0f}",
                "image": g.photo_url or _FALLBACK_IMG,
                "collection_key": collection_key,
                "reserved": g.status == Gown.Status.RESERVED,
            }
            for g in real_gowns
        ]

    products = []
    for i, slug in enumerate(_SLUG_ORDER):
        products.append(
            {
                "slug": slug,
                "title": f"{label} {_NUMERIC[i]}",
                "price": _SEARCH_PRICES[i],
                "price_display": f"₱{_SEARCH_PRICES[i]:,}",
                "image": _FALLBACK_IMG,
                "collection_key": collection_key,
                "reserved": i == 1,
            }
        )
    return products


def homepage(request):
    return render(request, 'home.html', {})


def faqs(request):
    return render(request, 'faqs.html', {})


def about(request):
    return render(request, 'about.html', {})


def how_it_works(request):
    return render(request, 'how_it_works.html', {})


def contact(request):
    return render(request, "contact.html", {})


def terms_and_conditions(request):
    return render(request, 'terms_and_conditions.html', {})


def collections(request):
    return render(request, 'collections.html', {})


def _render_collection(request, template_name, collection_key):
    return render(
        request,
        template_name,
        {
            "products": _products_for_category(collection_key),
            "label": _label_for(collection_key),
        },
    )


def collection_wedding(request):
    return _render_collection(request, 'wedding.html', 'wedding')


def collection_ball_gown(request):
    return _render_collection(request, 'ball_gown.html', 'ball-gown')


def collection_sexy_gown(request):
    return _render_collection(request, 'sexy_gown.html', 'sexy-gown')


def collection_ninang_gown(request):
    return _render_collection(request, 'ninang_gown.html', 'ninang-gown')


def collection_suit(request):
    return _render_collection(request, 'suit.html', 'suit')


def collection_filipiniana(request):
    return _render_collection(request, 'filipiniana.html', 'filipiniana')


def collection_guest_gown(request):
    return _render_collection(request, 'guest_gown.html', 'guest-gown')


def collection_flower_girl(request):
    return _render_collection(request, 'flower_girl.html', 'flower-girl')


def collection_belo(request):
    return _render_collection(request, 'belo.html', 'belo')


def collection_thailand_gown(request):
    return _render_collection(request, 'thailand_gown.html', 'thailand-gown')


def collection_dresses(request):
    return _render_collection(request, 'dresses.html', 'dresses')


def featured_men_collections(request):
    return render(request, 'featured_men_collections.html', {})


def featured_women_collections(request):
    return render(request, 'featured_women_collections.html', {})


def collection_all(request):
    """Rent → All: cycle through every category's product grid (same slugs/prices as each collection page)."""
    panels = [
        {
            "collection_key": category["key"],
            "label": category["label"],
            "products": _products_for_category(category["key"]),
        }
        for category in _CATEGORIES
    ]
    return render(request, "all.html", {"all_panels": panels})


def legacy_wedding_product_url(request, slug: str):
    """Old path /collections/wedding/products/<slug>/ — redirect to routed product URL."""
    target = reverse("gowns:product_detail", kwargs={"collection": "wedding", "slug": slug})
    if request.path == target:
        return product_detail(request, "wedding", slug)
    if request.GET:
        target += "?" + request.GET.urlencode()
    return redirect(target)


_WEDDING_PRODUCTS = {
    "valencia-lace": {
        "title": "Wedding One",
        "price": "₱1,600",
        "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        "availability": "Available Now",
        "collection": "Wedding",
    },
    "archive-satin": {
        "title": "Wedding Two",
        "price": "₱1,800",
        "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        "availability": "Available Now",
        "collection": "Wedding",
    },
    "florence-organza": {
        "title": "Wedding Three",
        "price": "₱2,000",
        "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        "availability": "Available Now",
        "collection": "Wedding",
    },
    "modernist-crepe": {
        "title": "Wedding Four",
        "price": "₱2,200",
        "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        "availability": "Available Now",
        "collection": "Wedding",
    },
    "opulence-pearl": {
        "title": "Wedding Five",
        "price": "₱2,400",
        "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        "availability": "Available Now",
        "collection": "Wedding",
    },
    "heritage-lace": {
        "title": "Wedding Six",
        "price": "₱2,600",
        "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        "availability": "Available Now",
        "collection": "Wedding",
    },
    "city-reception": {
        "title": "Wedding Seven",
        "price": "₱2,800",
        "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        "availability": "Available Now",
        "collection": "Wedding",
    },
    "lumiere-silk": {
        "title": "Wedding Eight",
        "price": "₱3,000",
        "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        "availability": "Available Now",
        "collection": "Wedding",
    },
}


_FALLBACK_IMG = (
    "https://lh3.googleusercontent.com/aida-public/"
    "AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA"
)


def _blocked_dates_for_category(label: str) -> list[str]:
    """Dates in the next _AVAILABILITY_HORIZON_DAYS where EVERY physical gown unit in
    this category is already spoken for, so the customer calendar must grey them out.

    Reads the same reservations.ReservationItem rows that drive the admin panel's
    Bookings & Schedule calendar (arabela_admin.views._calendar_events) -- one source of
    truth, so what staff see there is exactly what customers can't book here.

    Two things take a unit out of the pool on a given day:
      * an active booking -- occupied for its whole rental_date..return_date window,
        covering Pick-up, Reserved and Return. Only stage='Returned' (set by staff from
        the admin calendar) releases it, which is what makes admin the sole unlock.
        Rejected/Cancelled reservations never held real stock, so they're ignored.
      * an admin-declared GownUnavailability block -- the post-return cleaning/repair gap.

    It's quantity-aware: units are counted individually, so a date only becomes
    unavailable once the number of distinct spoken-for units reaches the category's
    total. Booking the same dates as someone else is fine while spare units remain.

    Tracking a SET of unit ids per day (rather than adding up counts) means a unit that
    is both booked and blocked on the same day is still just one unit off the pool.
    """
    unit_ids = set(
        Gown.objects.filter(category=label)
        .exclude(status=Gown.Status.OUT_OF_STOCK)
        .values_list("id", flat=True)
    )
    total_units = len(unit_ids)
    # No real inventory recorded for this category yet (the public catalog is still a
    # placeholder), so there is nothing to be full -- never block anything. Without this
    # guard the "spoken for >= total" test would be 0 >= 0 and black out every date.
    if total_units == 0:
        return []

    today = timezone.localdate()
    horizon = today + timedelta(days=_AVAILABILITY_HORIZON_DAYS)
    spoken_for = defaultdict(set)

    def occupy(gown_pk, start, end):
        if gown_pk not in unit_ids:
            return
        day = max(start, today)
        last = min(end, horizon)
        while day <= last:
            spoken_for[day].add(gown_pk)
            day += timedelta(days=1)

    booked_items = (
        ReservationItem.objects.filter(
            gown__isnull=False,
            gown__category=label,
            return_date__gte=today,
            rental_date__lte=horizon,
        )
        .exclude(stage=ReservationItem.Stage.RETURNED)
        .exclude(
            reservation__status__in=[
                Reservation.Status.REJECTED,
                Reservation.Status.CANCELLED,
            ]
        )
        .values_list("gown_id", "rental_date", "return_date")
    )
    for gown_pk, rental_date, return_date in booked_items:
        occupy(gown_pk, rental_date, return_date)

    blocks = GownUnavailability.objects.filter(
        gown__category=label,
        end_date__gte=today,
        start_date__lte=horizon,
    ).values_list("gown_id", "start_date", "end_date")
    for gown_pk, start_date, end_date in blocks:
        occupy(gown_pk, start_date, end_date)

    return sorted(
        day.isoformat() for day, units in spoken_for.items() if len(units) >= total_units
    )


def product_detail(request, collection: str, slug: str):
    col = collection.strip().lower()
    if col not in _VALID_COLLECTIONS:
        col = "wedding"

    # A real gown, if this slug belongs to one, wins over the placeholder catalog --
    # slugs are auto-generated unique per gown (Gown.save()) and never collide with the
    # 8 fixed placeholder slugs, so there's no ambiguity. Out-of-Stock units are excluded,
    # matching them already being excluded from availability capacity.
    real_gown = Gown.objects.filter(slug=slug).exclude(status=Gown.Status.OUT_OF_STOCK).first()

    if real_gown:
        blocked_dates = _blocked_dates_for_category(real_gown.category)
        product = {
            "title": real_gown.name,
            "price": f"₱{real_gown.rental_price:,.0f}",
            "image": real_gown.photo_url or _FALLBACK_IMG,
            "availability": "Limited Availability" if blocked_dates else "Available Now",
            "collection": real_gown.category,
        }
    else:
        base = dict(_WEDDING_PRODUCTS[slug]) if slug in _WEDDING_PRODUCTS else None

        # Availability is computed per CATEGORY, not per catalog slug: a real Gown row has
        # a category but no link to the placeholder catalog's per-product slugs, so every
        # product in a collection draws on that category's shared pool of physical units.
        blocked_dates = _blocked_dates_for_category(_label_for(col))

        title, lbl = _title_and_label(col, slug)
        if not base:
            product = {
                "title": slug.replace("-", " ").title(),
                "price": "₱0",
                "image": _FALLBACK_IMG,
                "availability": "Available Now",
                "collection": lbl,
            }
        else:
            base["title"] = title
            base["collection"] = lbl
            base["availability"] = "Limited Availability" if blocked_dates else "Available Now"
            product = base

    return render(
        request,
        'products.html',
        {
            "product": product,
            "slug": slug,
            "collection": col,
            "blocked_dates_json": json.dumps(blocked_dates),
        },
    )


def reservation(request):
    profile_display_name = ""
    profile_first_name = ""
    profile_last_name = ""

    if request.user.is_authenticated:
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        profile_display_name = profile.display_name or ""
        name_parts = profile_display_name.split(maxsplit=1) if profile_display_name else []
        profile_first_name = name_parts[0] if len(name_parts) > 0 else ""
        profile_last_name = name_parts[1] if len(name_parts) > 1 else ""

    # NOTE: the hold is deliberately NOT started here. Opening this page proves
    # nothing about whether the customer actually has a selection (the bag lives
    # in the browser), so auto-starting meant someone who merely landed here --
    # or came back after already booking -- saw a countdown claiming to hold a
    # selection that didn't exist, and could be charged an abandoned-hold strike
    # for it. It now starts from gowns:reservation_hold_start, which the
    # "Proceed to Reservation" buttons call once they've confirmed real items.

    return render(
        request,
        'reservation.html',
        {
            "profile_display_name": profile_display_name,
            "profile_first_name": profile_first_name,
            "profile_last_name": profile_last_name,
        },
    )


# Proof-of-payment upload rules, shared by the reservation form and the
# upload-later endpoint so both accept exactly the same files.
PROOF_ALLOWED_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
PROOF_ALLOWED_CONTENT_TYPES = ('image/jpeg', 'image/png', 'image/webp')
PROOF_MAX_BYTES = 5 * 1024 * 1024  # 5 MB


def _validate_proof_file(proof_file) -> str:
    """Return an empty string when the upload is acceptable, else the message to
    show the customer. Checks the extension AND the browser-reported type, so a
    renamed executable fails even though its extension looks fine."""
    if not proof_file:
        return "Please upload your proof of payment."
    name = (getattr(proof_file, "name", "") or "").lower()
    if not name.endswith(PROOF_ALLOWED_EXTENSIONS):
        return "Proof of payment must be a JPG, PNG, or WEBP image."
    content_type = (getattr(proof_file, "content_type", "") or "").lower()
    if content_type and content_type not in PROOF_ALLOWED_CONTENT_TYPES:
        return "Proof of payment must be a JPG, PNG, or WEBP image."
    if proof_file.size > PROOF_MAX_BYTES:
        return "Proof of payment must be 5MB or smaller."
    return ""


def _save_proof_file(proof_file) -> str:
    """Store the upload under a collision-proof name and return its public URL."""
    saved_path = default_storage.save(
        f"payment_proofs/{uuid.uuid4().hex}_{proof_file.name}", proof_file
    )
    return settings.MEDIA_URL + saved_path


def _parse_money(value) -> Decimal:
    """Turn '₱ 3,500' / '3500' / 3500 into a Decimal, defaulting to 0."""
    if value in (None, ""):
        return Decimal("0")
    try:
        cleaned = str(value).replace("₱", "").replace(",", "").strip()
        return Decimal(cleaned or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _parse_date(value):
    """Accept ISO (YYYY-MM-DD) or common M/D/Y display formats; return a date or None."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


@login_required(login_url='accounts:login')
@require_http_methods(["POST"])
def reservation_submit(request):
    """Persist a customer's reservation request (status=Pending) so it flows into
    the admin's Pending Approval + Payment Verification modules. Sent as multipart
    form data from reservation.html so the uploaded proof-of-payment rides along."""
    try:
        raw_items = json.loads(request.POST.get("items") or "[]")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid request data."}, status=400)

    if not isinstance(raw_items, list) or not raw_items:
        return JsonResponse({"success": False, "error": "Your bag is empty."}, status=400)

    parsed_items = []
    for raw in raw_items:
        gown_name = (raw.get("gown_name") or raw.get("name") or "").strip()
        rental_date = _parse_date(raw.get("rental_date"))
        return_date = _parse_date(raw.get("return_date"))
        if not gown_name or not rental_date or not return_date:
            return JsonResponse(
                {"success": False, "error": "Each item needs a gown and valid rental dates."},
                status=400,
            )
        parsed_items.append({
            "gown_name": gown_name,
            "gown_slug": (raw.get("gown_slug") or raw.get("slug") or "").strip()[:150],
            "size": (raw.get("size") or "").strip()[:20],
            "rental_price": _parse_money(raw.get("rental_price") or raw.get("price")),
            "rental_date": rental_date,
            "return_date": return_date,
        })

    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    typed_first_name = (request.POST.get("first_name") or "").strip()
    typed_last_name = (request.POST.get("last_name") or "").strip()
    typed_name = f"{typed_first_name} {typed_last_name}".strip()
    customer_name = (
        typed_name
        or profile.display_name
        or (f"{request.user.first_name} {request.user.last_name}".strip())
        or request.user.get_username()
    )
    phone = (request.POST.get("phone") or "").strip()[:20]
    address = (request.POST.get("address") or "").strip()[:255]
    city = (request.POST.get("city") or "").strip()[:100]
    postal_code = (request.POST.get("postal_code") or "").strip()[:10]

    payment_method = (request.POST.get("payment_method") or "").strip()
    if payment_method not in Reservation.PaymentMethod.values:
        payment_method = ""

    rental_subtotal = _parse_money(request.POST.get("rental_subtotal"))
    if rental_subtotal == 0:
        rental_subtotal = sum((i["rental_price"] for i in parsed_items), Decimal("0"))
    security_deposit = _parse_money(request.POST.get("security_deposit")) or Decimal("2000")
    total_amount = _parse_money(request.POST.get("total_amount"))
    if total_amount == 0:
        total_amount = rental_subtotal + security_deposit

    # Proof of payment is required. The form already blocks submitting without
    # it, but enforcing it here too stops a reservation being created unpaid by
    # anything that bypasses the page's JS -- those rows are indistinguishable
    # from paid ones in the admin's Payment Verification table.
    proof_file = request.FILES.get("proof_of_payment")
    error = _validate_proof_file(proof_file)
    if error:
        return JsonResponse({"success": False, "error": error}, status=400)
    payment_proof_url = _save_proof_file(proof_file)

    with transaction.atomic():
        reservation_obj = Reservation.objects.create(
            customer=request.user,
            customer_name=customer_name[:120],
            phone=phone,
            address=address,
            city=city,
            postal_code=postal_code,
            payment_method=payment_method,
            payment_proof_url=payment_proof_url,
            rental_subtotal=rental_subtotal,
            security_deposit=security_deposit,
            total_amount=total_amount,
        )
        for item in parsed_items:
            matched_gown = Gown.objects.filter(name__iexact=item["gown_name"]).first()
            ReservationItem.objects.create(
                reservation=reservation_obj,
                gown=matched_gown,
                gown_name=item["gown_name"][:150],
                gown_slug=item["gown_slug"],
                size=item["size"],
                rental_price=item["rental_price"],
                rental_date=item["rental_date"],
                return_date=item["return_date"],
            )

    # The reservation exists now, so the checkout hold is over -- drop the
    # countdown banner instead of leaving it ticking on the confirmation page.
    clear_reservation_hold(request)

    return JsonResponse({"success": True, "reference_code": reservation_obj.reference_code})


def selection(request):
    return render(request, 'selection.html', {})


def confirmation(request):
    return render(request, 'confirmation.html', {})


@login_required(login_url='accounts:login')
def orders(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    reservations = (
        request.user.reservations
        .prefetch_related('items')
        .order_by('-created_at')
    )
    profile.reservations_last_seen_at = timezone.now()
    profile.save(update_fields=["reservations_last_seen_at"])
    return render(
        request,
        'reservations.html',
        {"display_name": profile.display_name, "reservations": reservations},
    )


def _variation_for(reservation, item):
    """Which status layout the item detail page renders. Computed once here rather
    than repeated as {% if %} chains across the template, since several independent
    blocks (hero treatment, copy, CTAs, color family) all key off the same branch."""
    if reservation.status == Reservation.Status.PENDING:
        return "pending"
    if reservation.status == Reservation.Status.REJECTED:
        return "rejected"
    if reservation.status == Reservation.Status.CANCELLED:
        return "cancelled"
    if item.stage == ReservationItem.Stage.RETURNED:
        return "returned"
    return "accepted"


@login_required(login_url='accounts:login')
def reservation_item_detail(request, item_id):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    item = get_object_or_404(
        ReservationItem.objects.select_related('reservation', 'gown'),
        id=item_id, reservation__customer=request.user,
    )
    reservation = item.reservation
    gown_photo = item.gown.photo_url if (item.gown_id and item.gown.photo_url) else _FALLBACK_IMG
    return render(
        request,
        'reservation_item_detail.html',
        {
            "display_name": profile.display_name,
            "reservation": reservation,
            "item": item,
            "variation": _variation_for(reservation, item),
            "gown_photo": gown_photo,
            "rejection_reason": (
                (reservation.notes or "").strip()
                if reservation.status == Reservation.Status.REJECTED else ""
            ),
            "reservation_items": reservation.items.select_related("gown").all(),
        },
    )


@login_required(login_url='accounts:login')
@require_http_methods(["POST"])
def reservation_item_cancel(request, item_id):
    item = get_object_or_404(
        ReservationItem.objects.select_related('reservation', 'gown'),
        id=item_id, reservation__customer=request.user,
    )
    if not item.can_customer_cancel:
        return JsonResponse(
            {"success": False, "error": "This reservation can no longer be cancelled."},
            status=400,
        )

    reservation = item.reservation
    reservation.status = Reservation.Status.CANCELLED
    reservation.save(update_fields=["status", "updated_at"])

    # Mirror/reverse reservation_approve_view's Available->Reserved flip
    # (arabela_admin/views.py): only revert gowns still sitting at Reserved, so we
    # don't clobber In-Cleaning/Out-of-Stock that staff set for unrelated reasons.
    for sibling in reservation.items.select_related('gown').all():
        if sibling.gown_id and sibling.gown.status == Gown.Status.RESERVED:
            sibling.gown.status = Gown.Status.AVAILABLE
            sibling.gown.save(update_fields=["status", "updated_at"])

    # Repeat cancellations auto-flag the account for admin review (see
    # accounts.services.CANCELLATION_FLAG_THRESHOLD). No-op below the threshold.
    sync_cancellation_flag(request.user)

    return JsonResponse({"success": True, "status": reservation.status})


@login_required(login_url='accounts:login')
@require_http_methods(["POST"])
def reservation_upload_proof(request, item_id):
    """Attach proof of payment to a reservation that was submitted without one.

    Scoped to the signed-in customer's own reservations, and only while the
    reservation is still awaiting review with nothing uploaded yet -- staff
    verify against the original file, so it must never be replaceable."""
    item = get_object_or_404(
        ReservationItem.objects.select_related('reservation'),
        id=item_id, reservation__customer=request.user,
    )
    reservation = item.reservation
    if not reservation.can_upload_proof:
        return JsonResponse(
            {"success": False, "error": "This reservation no longer accepts a payment upload."},
            status=400,
        )

    proof_file = request.FILES.get("proof_of_payment")
    error = _validate_proof_file(proof_file)
    if error:
        return JsonResponse({"success": False, "error": error}, status=400)

    reservation.payment_proof_url = _save_proof_file(proof_file)
    reservation.save(update_fields=["payment_proof_url", "updated_at"])

    return JsonResponse({"success": True, "payment_state": reservation.payment_state})


@login_required(login_url='accounts:login')
@require_http_methods(["POST"])
def reservation_hold_start(request):
    """Begin the 20-minute checkout hold.

    Called by the "Proceed to Reservation" buttons, which only fire once they've
    confirmed the customer actually has items -- the bag lives in the browser, so
    this is the earliest point the server can know a real selection exists. An
    already-running hold is left alone so refreshing never extends the clock."""
    deadline = start_reservation_hold(request)
    return JsonResponse({
        "success": True,
        "seconds_remaining": max(int((deadline - timezone.now()).total_seconds()), 0) if deadline else 0,
    })


@login_required(login_url='accounts:login')
@require_http_methods(["POST"])
def reservation_hold_release(request):
    """Give up the 20-minute checkout hold on purpose (the timer's Cancel button).

    Counts as an abandoned hold: holding gowns and walking away is the cheap,
    repeatable behaviour the flag threshold exists to limit. The deadline lives
    in the session, so it has to be cleared server-side or the countdown
    reappears on the next page load."""
    if get_reservation_hold_deadline(request):
        # Only record it when a hold was actually live -- a double-click or a
        # stale tab re-POSTing must not inflate the customer's count.
        record_abandoned_hold(request.user)
    clear_reservation_hold(request)
    return JsonResponse({"success": True})


@login_required(login_url='accounts:login')
def messages_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    message_list = list(request.user.customer_messages.all())
    unread_ids = [m.id for m in message_list if not m.is_read]
    if unread_ids:
        CustomerMessage.objects.filter(id__in=unread_ids).update(is_read=True)
    for m in message_list:
        m.was_unread = m.id in unread_ids

    return render(
        request,
        'messages.html',
        {"display_name": profile.display_name, "customer_messages": message_list},
    )


@login_required(login_url='accounts:login')
def profile(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)

    if request.method == "POST":
        first_name = (request.POST.get("first_name") or "").strip()
        last_name = (request.POST.get("last_name") or "").strip()
        full_name = " ".join(part for part in [first_name, last_name] if part).strip()
        profile.display_name = full_name
        profile.save(update_fields=["display_name"])
        return redirect("gowns:profile")

    name_parts = profile.display_name.split(maxsplit=1) if profile.display_name else []
    first_name = name_parts[0] if len(name_parts) > 0 else ""
    last_name = name_parts[1] if len(name_parts) > 1 else ""
    return render(
        request,
        'profile.htmls',
        {
            "display_name": profile.display_name,
            "profile_first_name": first_name,
            "profile_last_name": last_name,
        },
    )
