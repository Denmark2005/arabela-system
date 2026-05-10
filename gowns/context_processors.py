from __future__ import annotations

import re


_PRICE_RE = re.compile(r"[^\d]")


def _peso_to_number(value: str) -> int:
    if not value:
        return 0
    cleaned = _PRICE_RE.sub("", str(value))
    try:
        return int(cleaned)
    except ValueError:
        return 0


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
            "slug": "archive-satin",
            "title": "Dresses One",
            "price_label": "₱1,800",
            "price": _peso_to_number("₱1,800"),
            "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        },
        {
            "collection_key": "filipiniana",
            "slug": "florence-organza",
            "title": "Filipiniana One",
            "price_label": "₱2,000",
            "price": _peso_to_number("₱2,000"),
            "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        },
        {
            "collection_key": "wedding",
            "slug": "modernist-crepe",
            "title": "Wedding One",
            "price_label": "₱2,200",
            "price": _peso_to_number("₱2,200"),
            "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        },
    ]

    return {"featured_search_items": featured}

