from __future__ import annotations

import re

from django.urls import reverse

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

_COLLECTION_ROWS = (
    ("all", "All", "gowns:collection_all"),
    ("dresses", "Dresses", "gowns:collection_dresses"),
    ("filipiniana", "Filipiniana", "gowns:collection_filipiniana"),
    ("kid-suit", "Kids suit", "gowns:collection_kid_suit"),
    ("wedding", "Wedding", "gowns:collection_wedding"),
    ("suit", "Suit", "gowns:collection_suit"),
    ("ball-gown", "Ball gown", "gowns:collection_ball_gown"),
)


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
    if collection_key == "ball-gown":
        return f"Ball Gown {n}"
    if collection_key == "kid-suit":
        return f"Kid Suit {n}"
    if collection_key == "suit":
        return f"Suit {n}"
    if collection_key == "filipiniana":
        return f"Filipiniana {n}"
    if collection_key == "dresses":
        return f"Dresses {n}"
    if collection_key == "wedding":
        return f"Wedding {n}"
    return f"Wedding {n}"


def _build_search_catalog() -> list[dict]:
    items: list[dict] = []
    for collection_key, _label, _urlname in _COLLECTION_ROWS:
        for i, slug in enumerate(_SLUG_ORDER):
            price = _SEARCH_PRICES[i]
            title = _title_for_collection(collection_key, slug)
            price_label = f"₱{price:,}"
            items.append(
                {
                    "collection_key": collection_key,
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
            "key": key,
            "label": label,
            "url": reverse(urlname),
        }
        for key, label, urlname in _COLLECTION_ROWS
    ]


def featured_search_items(request):
    """
    Items for the search overlay "Featured" section.

    Notes:
    - This project renders product detail pages via `gowns.views.product_detail`
      using a (collection, slug) URL, not a database-backed Product model.
    - We keep this list small and stable to avoid altering UI layout.
    """
    featured = [
        {
            "collection_key": "kid-suit",
            "slug": "valencia-lace",
            "title": "Kid Suit One",
            "price_label": "₱1,600",
            "price": _peso_to_number("₱1,600"),
            "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        },
        {
            "collection_key": "dresses",
            "slug": "valencia-lace",
            "title": "Dresses One",
            "price_label": "₱1,800",
            "price": _peso_to_number("₱1,800"),
            "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        },
        {
            "collection_key": "filipiniana",
            "slug": "valencia-lace",
            "title": "Filipiniana One",
            "price_label": "₱2,000",
            "price": _peso_to_number("₱2,000"),
            "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        },
        {
            "collection_key": "wedding",
            "slug": "valencia-lace",
            "title": "Wedding One",
            "price_label": "₱2,200",
            "price": _peso_to_number("₱2,200"),
            "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        },
    ]

    catalog = _build_search_catalog()
    collections_meta = _collections_meta()

    return {
        "featured_search_items": featured,
        "search_overlay_catalog": catalog,
        "search_overlay_collections_meta": collections_meta,
    }
