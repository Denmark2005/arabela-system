"""Which picture each category's tile shows on the customer site.

For one category, in order:
  1. the picture the owner uploaded in Admin -> Categories (gowns.models.CategoryCover);
  2. the picture bundled with the site (static/images/categories/<key>.jpg);
  3. the plain placeholder -- the tile picture the site always had -- for a category with neither:
     Barong, and any category the owner adds before giving it a picture.

Customers must never see a broken page because of this: if the owner-pictures table can't be read
(for example the code is live before its migration has run) every tile simply uses 2 or 3.
"""
from __future__ import annotations

from django.db import DatabaseError, connection, transaction
from django.templatetags.static import static

from gowns.models import CategoryCover

# The categories that ship with a picture (static/images/categories/<key>.jpg). Barong is left out on
# purpose: it keeps the placeholder until the owner uploads a picture for it.
BUNDLED_COVER_KEYS = frozenset({
    "wedding", "evening-gown", "long-gown", "luxury-gown", "mother-gown", "suit", "filipiniana",
    "guest-gown", "dresses", "kids-gown", "ball-gown-tulle", "bridesmaid-dresses",
})

PLACEHOLDER_STATIC_PATH = "images/categories/placeholder.jpg"


def bundled_cover_path(key: str) -> str | None:
    """Path (under static/) of the picture that ships for this category, or None."""
    return f"images/categories/{key}.jpg" if key in BUNDLED_COVER_KEYS else None


def owner_cover_urls() -> dict[str, str]:
    """{category key: picture URL} for every picture the owner uploaded. Never raises: when the table
    can't be read the owner has no pictures as far as customers are concerned."""
    try:
        if connection.in_atomic_block:
            with transaction.atomic():  # a savepoint, so a failed read can't poison the caller's transaction
                return dict(CategoryCover.objects.values_list("key", "image_url"))
        return dict(CategoryCover.objects.values_list("key", "image_url"))
    except DatabaseError:
        return {}


def covers_ready() -> bool:
    """True once the owner-pictures table exists (the database update has been applied). Lets the Categories
    page say so up front, instead of letting the owner press Upload and fail. Never raises."""
    try:
        if connection.in_atomic_block:
            with transaction.atomic():
                CategoryCover.objects.exists()
        else:
            CategoryCover.objects.exists()
        return True
    except DatabaseError:
        return False


def cover_for(key: str, owner_urls: dict[str, str]) -> dict:
    """{"url", "is_photo", "owner"} for one category's tile.

    `is_photo` is False only for the placeholder; the tile draws its name differently then (white,
    over the picture, exactly as the placeholder tiles always have) because the placeholder is not a
    gown cut-out on grey. `owner` says the picture came from the owner's upload."""
    uploaded = owner_urls.get(key)
    if uploaded:
        return {"url": uploaded, "is_photo": True, "owner": True}
    bundled = bundled_cover_path(key)
    if bundled:
        return {"url": static(bundled), "is_photo": True, "owner": False}
    return {"url": static(PLACEHOLDER_STATIC_PATH), "is_photo": False, "owner": False}
