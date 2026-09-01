from __future__ import annotations

import re
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.models import CustomerMessage, UserProfile
from gowns.models import SiteSettings
from reservations.models import Reservation, ReservationItem

_PRICE_RE = re.compile(r"[^\d]")

_SLUG_ORDER = (
    "valencia-lace",
    "archive-satin",
    "florence-organza",
    "modernist-crepe",
    "opulence-pearl",
    "heritage-lace",
    "city-reception",
    "lumiere-silk",
)
_NUMERIC = ["One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight"]
_SEARCH_PRICES = (1600, 1800, 2000, 2200, 2400, 2600, 2800, 3000)

_FALLBACK_IMG = (
    "https://lh3.googleusercontent.com/aida-public/"
    "AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA"
)

# Canonical list of real rental categories, in the same order as
# gowns.models.Gown.Category -- the single source of truth for every
# collection list on the customer side (nav dropdown, mobile drawer, search
# sidebar, explore drawer, collections grid, and the fake product catalog
# below). Adding/renaming a category only ever needs to happen here.
_CATEGORIES = (
    {"key": "wedding", "url_name": "collection_wedding", "label": "Wedding Gown"},
    {"key": "ball-gown", "url_name": "collection_ball_gown", "label": "Ball Gown"},
    {"key": "sexy-gown", "url_name": "collection_sexy_gown", "label": "Sexy Gown"},
    {"key": "ninang-gown", "url_name": "collection_ninang_gown", "label": "Ninang Gown"},
    {"key": "suit", "url_name": "collection_suit", "label": "Suit"},
    {"key": "filipiniana", "url_name": "collection_filipiniana", "label": "Filipiniana"},
    {"key": "guest-gown", "url_name": "collection_guest_gown", "label": "Guest Gown"},
    {"key": "flower-girl", "url_name": "collection_flower_girl", "label": "Flower Girl"},
    {"key": "belo", "url_name": "collection_belo", "label": "Belo"},
    {"key": "thailand-gown", "url_name": "collection_thailand_gown", "label": "Thailand Gown"},
    {"key": "dresses", "url_name": "collection_dresses", "label": "Dresses"},
)

# "All" is a synthetic first row (not a real category) prepended wherever the
# full collection list is shown (nav dropdown, search sidebar, explore drawer).
_COLLECTION_ROWS = (
    {"key": "all", "url_name": "collection_all", "label": "All"},
) + _CATEGORIES


def _peso_to_number(value: str) -> int:
    if not value:
        return 0
    cleaned = _PRICE_RE.sub("", str(value))
    try:
        return int(cleaned)
    except ValueError:
        return 0


def _title_for_collection(collection_key: str, slug: str) -> str:
    idx = _SLUG_ORDER.index(slug) if slug in _SLUG_ORDER else 0
    n = _NUMERIC[idx]
    for category in _CATEGORIES:
        if category["key"] == collection_key:
            return f"{category['label']} {n}"
    return f"Wedding Gown {n}"


def _build_search_catalog() -> list[dict]:
    items: list[dict] = []
    for category in _CATEGORIES:
        collection_key = category["key"]
        for i, slug in enumerate(_SLUG_ORDER):
            price = _SEARCH_PRICES[i]
            title = _title_for_collection(collection_key, slug)
            price_label = f"₱{price:,}"
            items.append(
                {
                    "collection_key": collection_key,
                    "collection_label": category["label"],
                    "slug": slug,
                    "title": title,
                    "price_label": price_label,
                    "price": price,
                    "image": _FALLBACK_IMG,
                    "url": reverse(
                        "gowns:product_detail",
                        kwargs={"collection": collection_key, "slug": slug},
                    ),
                }
            )
    return items


def _collections_meta() -> list[dict]:
    return [
        {
            "key": row["key"],
            "label": row["label"],
            "url_name": row["url_name"],
            "url": reverse(f"gowns:{row['url_name']}"),
        }
        for row in _COLLECTION_ROWS
    ]


def featured_search_items(request):
    """
    Items for the search overlay "Featured" section.

    Notes:
    - This project renders product detail pages via `gowns.views.product_detail`
      using a (collection, slug) URL, not a database-backed Product model.
    - We keep this list small and stable to avoid altering UI layout.
    """
    featured_prices = (1600, 1800, 2000, 2200)
    featured = [
        {
            "collection_key": category["key"],
            "collection_label": category["label"],
            "slug": "valencia-lace",
            "title": f"{category['label']} One",
            "price_label": f"₱{price:,}",
            "price": price,
            "image": _FALLBACK_IMG,
        }
        for category, price in zip(_CATEGORIES[:4], featured_prices)
    ]

    catalog = _build_search_catalog()
    collections_meta = _collections_meta()

    return {
        "featured_search_items": featured,
        "search_overlay_catalog": catalog,
        "search_overlay_collections_meta": collections_meta,
    }


def active_reservations_count(request):
    """
    Badge count for the Reservations nav row: how many active items are new or
    changed (created, or had their stage/dates edited by staff) since the customer
    last opened their Reservations page. Dismissible like the Messages badge --
    clears the moment they visit /reservations/ (`gowns.views.orders` stamps
    `UserProfile.reservations_last_seen_at`), and comes back the next time
    something about an active booking changes.
    """
    if not request.user.is_authenticated:
        return {"active_reservations_count": 0}

    active_items = (
        ReservationItem.objects
        .filter(reservation__customer=request.user)
        .exclude(stage=ReservationItem.Stage.RETURNED)
        .exclude(reservation__status__in=[Reservation.Status.REJECTED, Reservation.Status.CANCELLED])
    )

    profile = UserProfile.objects.filter(user=request.user).first()
    last_seen = profile.reservations_last_seen_at if profile else None
    if last_seen:
        active_items = active_items.filter(updated_at__gt=last_seen)

    return {"active_reservations_count": active_items.count()}


def unread_messages_count(request):
    """Badge count for the Messages nav link: how many messages from staff this
    customer hasn't opened the Messages page to see yet."""
    if not request.user.is_authenticated:
        return {"unread_messages_count": 0}

    count = CustomerMessage.objects.filter(recipient=request.user, is_read=False).count()
    return {"unread_messages_count": count}


def _phone_tel_href(phone: str) -> str:
    p = (phone or "").strip()
    if not p:
        return ""
    if p.startswith("+"):
        return "tel:" + p.replace(" ", "")
    digits = p.replace(" ", "").replace("-", "")
    if digits.startswith("0"):
        return "tel:+63" + digits[1:]
    return "tel:" + digits


def site_settings(request):
    """Business contact info shown on the public site (footer, Contact page) and
    edited from the admin Edit Profile page -- one row, one source of truth."""
    s = SiteSettings.load()
    address_line = " ".join(
        p for p in [s.shop_street, s.shop_city, s.shop_country, s.shop_postal_code] if p
    )
    return {
        "site_facebook_url": s.facebook_url,
        "site_phone": s.phone,
        "site_phone_tel": _phone_tel_href(s.phone),
        "site_shop_street": s.shop_street,
        "site_shop_city": s.shop_city,
        "site_shop_country": s.shop_country,
        "site_shop_postal_code": s.shop_postal_code,
        "site_shop_address_line": address_line,
    }


# --- Reservation hold (the 20-minute checkout window) -------------------------
# A hold starts when the customer opens the reservation page and ends when they
# submit. It lives in the session rather than on a Reservation row because
# nothing has been submitted yet -- and unlike localStorage, the customer can't
# extend it by editing browser storage.

RESERVATION_HOLD_SESSION_KEY = "reservation_hold_until"
RESERVATION_HOLD_MINUTES = 20


def get_reservation_hold_deadline(request):
    """The current hold's deadline, or None if there is no live hold."""
    raw = request.session.get(RESERVATION_HOLD_SESSION_KEY)
    if not raw:
        return None
    parsed = parse_datetime(raw)
    if parsed is None:
        # Corrupt/hand-edited value -- treat as no hold.
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_default_timezone())
    return parsed if parsed > timezone.now() else None


def start_reservation_hold(request):
    """Begin a hold, or leave an already-running one alone.

    Re-entering the reservation page must NOT restart the clock, otherwise the
    countdown means nothing -- a customer could sit on it forever by refreshing.
    """
    if not request.user.is_authenticated:
        return None
    existing = get_reservation_hold_deadline(request)
    if existing:
        return existing
    deadline = timezone.now() + timedelta(minutes=RESERVATION_HOLD_MINUTES)
    request.session[RESERVATION_HOLD_SESSION_KEY] = deadline.isoformat()
    return deadline


def clear_reservation_hold(request):
    """End the hold (the reservation was submitted, or it expired)."""
    if request.session.pop(RESERVATION_HOLD_SESSION_KEY, None) is not None:
        request.session.modified = True


def expire_reservation_hold_if_lapsed(request):
    """Retire a hold whose 20 minutes ran out, counting it as abandoned.

    Letting the countdown lapse has to count the same as pressing Cancel --
    otherwise the limit is dodged by simply closing the tab. Popping the session
    key makes this naturally idempotent: it can only fire once per hold, however
    many pages the customer then loads.

    Accepted limitation: a customer who never comes back is never counted (there
    is no background job). That errs on the lenient side, which is the right way
    for this to fail.
    """
    raw = request.session.get(RESERVATION_HOLD_SESSION_KEY)
    if not raw:
        return
    if get_reservation_hold_deadline(request):
        return  # still live
    clear_reservation_hold(request)
    from accounts.services import record_abandoned_hold

    record_abandoned_hold(request.user)


def reservation_hold(request):
    """Feeds the site-wide countdown banner in base.html.

    An elapsed deadline reads as 'no hold' and is retired here, so a stale
    session value self-heals on the next page load without a cleanup job.
    """
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {"reservation_hold_until": "", "reservation_hold_seconds": 0}
    expire_reservation_hold_if_lapsed(request)
    deadline = get_reservation_hold_deadline(request)
    if not deadline:
        return {"reservation_hold_until": "", "reservation_hold_seconds": 0}
    # Seconds-remaining is what the countdown actually anchors to: a wrong clock
    # on the customer's device would otherwise show a bogus (or already-expired)
    # timer even though the server-side hold is perfectly valid.
    remaining = int((deadline - timezone.now()).total_seconds())
    return {
        "reservation_hold_until": deadline.isoformat(),
        "reservation_hold_seconds": max(remaining, 0),
    }
