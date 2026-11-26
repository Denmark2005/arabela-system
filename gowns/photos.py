"""Smaller gown photos on the customer pages.

Gown photos are uploaded at full camera size (about 2000x3500 pixels, 200-500 KB). Cloudinary can send a resized copy instead, just by
adding instructions to the address: f_auto (the best format the browser supports, usually WebP), q_auto (compressed so it looks the
same), c_limit,w_N (at most N pixels wide, never enlarged). Measured on the live photos: 16-26 KB in the grids, 46-74 KB on the
product page, instead of 182-492 KB -- faster pages on phones and far less of Cloudinary's free monthly allowance used.

Only the customer pages use this. The stored photo_url is never changed, and the admin pages still show the original.
Anything that is not a plain Cloudinary upload address (the placeholder picture, local files, an address that already carries
instructions) is returned unchanged, so a photo can never break because of this."""
import logging
import re
from urllib.parse import unquote

import requests
from django.conf import settings
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)

_CLOUDINARY_UPLOAD = re.compile(r"^(https?://res\.cloudinary\.com/[^/]+/image/upload/)(v\d+/.+)$")

GRID_WIDTH = 800       # collection grids, search, suggestions, cart: cards are at most ~400 px wide, 800 stays sharp on phones
PAGE_WIDTH = 1600      # the big photo on the product and order pages: up to ~800 px wide, 1600 stays sharp on high-density screens
THUMB_WIDTH = 240      # the small Front / Side / Back pictures under the big one
READ_LIMIT = 15 * 1024 * 1024


def read(url):
    """The stored photo's bytes (to match a side or back view to its front): from this site's own storage, or over the web. None when it
    can't be read."""
    url = (url or "").strip()
    try:
        if url.startswith(settings.MEDIA_URL):
            with default_storage.open(unquote(url[len(settings.MEDIA_URL):])) as stored:
                data = stored.read()
        elif url.startswith(("https://", "http://")):
            response = requests.get(url, timeout=20)
            response.raise_for_status()
            data = response.content
        else:
            return None
    except Exception:       # noqa: BLE001 - storage or network trouble: the caller keeps the photo as it was uploaded
        logger.warning("Could not read the stored photo %s", url, exc_info=True)
        return None
    return data if 0 < len(data) <= READ_LIMIT else None


def sized(url, width):
    match = _CLOUDINARY_UPLOAD.match(url or "")
    if not match:
        return url
    return f"{match.group(1)}f_auto,q_auto,c_limit,w_{width}/{match.group(2)}"
