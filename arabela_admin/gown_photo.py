"""What the gown photo window shows for one booking line (templates/arabela_admin/partials/gown_photo_window.html).

Staff click a gown's name on Active Reservations, All Bookings and Pending Approval and see that gown's picture. These two small
functions decide WHICH picture and the line under the name. They read only what the pages already load (each line's `gown`), so they cost
no database query, and they never change anything.

Photo, in this order:
  1. the gown's picture in the catalog today (it can be replaced there),
  2. the picture saved on the booking line when it was booked (the catalog gown may have been deleted since: `gown` is then empty),
  3. nothing: the window then says there is no photo for this gown.
"""


def photo_url(item):
    """The picture to show for a booking line, or '' when there is none."""
    gown = item.gown if item.gown_id else None
    current = (gown.photo_url or "").strip() if gown is not None else ""
    return current or (item.gown_photo_url or "").strip()


def subtitle(item, tag_color=""):
    """The line under the gown's name: its tag colour, its gown code and its size ("White tag · WH-017 · Large"), whichever are known.

    `tag_color` is what the page already worked out for the line (the same word staff read under the gown's name); it is left out for a
    line that is not matched to a catalog gown, exactly like the code. The size is the one booked, else the catalog gown's own."""
    gown = item.gown if item.gown_id else None
    size = (item.size or "").strip()
    if size in ("", "-", "TBD") and gown is not None:
        size = gown.size or ""
    parts = []
    if tag_color:
        parts.append(f"{tag_color} tag")
    if gown is not None and gown.gown_id:
        parts.append(gown.gown_id)
    if size and size not in ("-", "TBD"):
        parts.append(size)
    return " · ".join(parts)
