"""Smaller gown photos on the customer pages.

Gown photos are uploaded at full camera size (about 2000x3500 pixels, 200-500 KB). Cloudinary can send a resized copy instead, just by
adding instructions to the address: f_auto (the best format the browser supports, usually WebP), q_auto (compressed so it looks the
same), c_limit,w_N (at most N pixels wide, never enlarged). Measured on the live photos: 16-26 KB in the grids, 46-74 KB on the
product page, instead of 182-492 KB -- faster pages on phones and far less of Cloudinary's free monthly allowance used.

Only the customer pages use this. The stored photo_url is never changed, and the admin pages still show the original.
Anything that is not a plain Cloudinary upload address (the placeholder picture, local files, an address that already carries
instructions) is returned unchanged, so a photo can never break because of this."""
import re

_CLOUDINARY_UPLOAD = re.compile(r"^(https?://res\.cloudinary\.com/[^/]+/image/upload/)(v\d+/.+)$")

GRID_WIDTH = 800       # collection grids, search, suggestions, cart: cards are at most ~400 px wide, 800 stays sharp on phones
PAGE_WIDTH = 1600      # the big photo on the product and order pages: up to ~800 px wide, 1600 stays sharp on high-density screens


def sized(url, width):
    match = _CLOUDINARY_UPLOAD.match(url or "")
    if not match:
        return url
    return f"{match.group(1)}f_auto,q_auto,c_limit,w_{width}/{match.group(2)}"
