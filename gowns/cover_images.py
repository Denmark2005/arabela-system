"""Turns a photo into a category "cover": the picture on a category's tile on the customer site.

ONE place does this, so the pictures bundled with the site and the ones the owner uploads from
Admin -> Categories always look the same:

- Every tile is 2:3 (portrait) on the shop's tile grey.
- The gown is framed in the upper part of the tile, never squashed or cut through, its white
  background shaded to the tile grey so no white box shows, and the strip at the bottom is left
  free for the category name.

An upload only becomes a cover if it follows the rules below (prepare_cover): a tall photo of the
WHOLE gown on a plain white background, big enough to stay sharp. That is what every picture the
shop already has looks like, so a new one can't make a tile look different. Each rule has one short
message for the owner; the first one a photo breaks is the one shown.

Only Pillow is used (it is already a requirement); nothing here touches the database.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageChops, ImageFilter, ImageOps

# 2:3, the shape of every category tile. 600 px wide is twice the width a tile is ever shown at.
COVER_WIDTH = 600
COVER_HEIGHT = 900

# The grey the category tiles have always had (the colour behind the original tile picture).
TILE_GREY = (251, 251, 251)

# Where a studio photo's gown goes: a stage in the upper part of the tile, so the strip below it
# stays clear for the name of the category. The gown is scaled to fill the stage, never beyond it.
STAGE_WIDTH_SHARE = 0.86
STAGE_TOP_SHARE = 0.05
STAGE_HEIGHT_SHARE = 0.80
# A small photo is never blown up more than this -- past it the picture turns soft.
MAX_UPSCALE = 1.6

# Refuse photos bigger than this many pixels before decoding them (a 5 MB file can still unpack to
# something huge, and the site runs on a small host); 25 megapixels is far more than a tile needs.
MAX_PIXELS = 25_000_000

# ---- the rules a photo must follow to become a category picture ---------------------------------------
TALL_RATIO = 1.2          # a tall photo is at least this many times as tall as it is wide ...
MIN_ASPECT = 0.45         # ... but not a sliver (width / height of at least this)
MIN_PHOTO_WIDTH = 400     # smaller than this and the picture would be blurry on the site
MIN_PHOTO_HEIGHT = 650
SHARP_PHOTO_WIDTH = 600   # below this the photo is accepted, with a note that a bigger one looks sharper
SHARP_PHOTO_HEIGHT = 900
EDGE_MARGIN = 0.005       # the gown may not touch the edge of the photo (that means it is cut off)
MIN_GOWN_HEIGHT = 350     # the gown itself, in pixels, so it can fill its place on the tile without going soft
SMALL_ON_TILE = 0.50      # a gown that ends up below this share of the tile's height gets a note (very wide gowns)

MSG_NOT_AN_IMAGE = "That file isn't a valid image."
MSG_TOO_LARGE = "That photo is too large. Use one under 25 megapixels (most phone photos are fine)."
MSG_NOT_TALL = "Use a tall (portrait) photo, not a wide, square or very narrow one."
MSG_TOO_SMALL = "That photo is too small. Use one at least 400 × 650 pixels."
MSG_BACKGROUND = "The background must be plain white, like the other gowns."
MSG_BACKGROUND_OR_EDGE = "Use a plain white background and keep the whole gown inside the photo."
MSG_NO_GOWN = "We couldn't find a gown in that photo."
MSG_CUT_OFF = "The whole gown has to be in the photo. It's cut off at the edge."
MSG_GOWN_SMALL = "The gown is too small in this photo. Move closer, or use a bigger photo."
NOTE_SOFT = "A bigger photo (600 × 900 or more) would look sharper."
NOTE_WIDE = "This gown is very wide, so it will look smaller than the others."

COVER_JPEG_QUALITY = 88


class CoverImageError(ValueError):
    """The upload can't be turned into a cover; the message is safe to show to the owner."""


def _flatten(img: Image.Image) -> Image.Image:
    """Upright (phone photos carry a rotation flag), no transparency, plain RGB."""
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        backdrop = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(backdrop, rgba).convert("RGB")
    return img.convert("RGB")


def _oriented_size(img: Image.Image):
    """(width, height) as a person sees the photo, i.e. after a phone's rotation flag is applied."""
    width, height = img.size
    try:
        orientation = img.getexif().get(0x0112, 1)
    except Exception:
        orientation = 1
    return (height, width) if orientation in (5, 6, 7, 8) else (width, height)


def _studio_background(img: Image.Image):
    """The photo's background colour when it is a plain near-white studio backdrop, else None.

    Looks only at a thin frame around the photo: if that frame is (nearly) all the same bright
    neutral colour, the subject was shot on a white backdrop. Portrait photos only -- a wide photo
    is a scene, not a cut-out gown."""
    width, height = img.size
    if height < width * TALL_RATIO:
        return None
    small = img.copy()
    small.thumbnail((160, 240))
    w, h = small.size
    band = max(2, round(min(w, h) * 0.04))
    pixels = small.load()
    frame = []
    for y in range(h):
        for x in range(w):
            if x < band or x >= w - band or y < band or y >= h - band:
                frame.append(pixels[x, y])
    if not frame:
        return None
    bright = [p for p in frame if min(p) >= 240 and max(p) - min(p) <= 10]
    if len(bright) < len(frame) * 0.9:
        return None
    return tuple(sorted(p[channel] for p in bright)[len(bright) // 2] for channel in range(3))


def _frame_white_share(img: Image.Image) -> float:
    """0..1: how much of a thin frame around the photo is bright neutral, i.e. looks like a white backdrop. (The same
    frame _studio_background looks at; kept separate so that function, and so every picture made with it, stays as it was.)"""
    small = img.copy()
    small.thumbnail((160, 240))
    w, h = small.size
    band = max(2, round(min(w, h) * 0.04))
    pixels = small.load()
    frame = [
        pixels[x, y] for y in range(h) for x in range(w)
        if x < band or x >= w - band or y < band or y >= h - band
    ]
    if not frame:
        return 0.0
    return sum(1 for p in frame if min(p) >= 240 and max(p) - min(p) <= 10) / len(frame)


def _content_box(img: Image.Image, background):
    """Where the gown is: the smallest box holding everything that isn't the plain backdrop, padded
    so soft edges (tulle, lace) are never clipped. None when nothing stands out."""
    small = img.copy()
    small.thumbnail((600, 900))
    threshold = min(background) - 8
    mask = small.convert("L").point(lambda value: 255 if value < threshold else 0)
    mask = mask.filter(ImageFilter.MedianFilter(5))  # drops dust specks, so one stray pixel can't stretch the box
    box = mask.getbbox()
    if not box:
        return None
    scale_x = img.width / small.width
    scale_y = img.height / small.height
    pad = round(0.025 * img.height)
    left, top, right, bottom = box
    box = (
        max(0, round(left * scale_x) - pad),
        max(0, round(top * scale_y) - pad),
        min(img.width, round(right * scale_x) + pad),
        min(img.height, round(bottom * scale_y) + pad),
    )
    if (box[2] - box[0]) * (box[3] - box[1]) < 0.04 * img.width * img.height:
        return None  # a speck, not a gown
    return box


def _gown_extent(img: Image.Image, background):
    """(left, top, right, bottom) of the gown as fractions of the photo -- exactly where it is, no padding --
    or None when nothing stands out from the backdrop. The same measuring as _content_box."""
    small = img.copy()
    small.thumbnail((600, 900))
    threshold = min(background) - 8
    mask = small.convert("L").point(lambda value: 255 if value < threshold else 0)
    mask = mask.filter(ImageFilter.MedianFilter(5))
    box = mask.getbbox()
    if not box:
        return None
    w, h = small.size
    return box[0] / w, box[1] / h, box[2] / w, box[3] / h


def build_cover(img: Image.Image) -> Image.Image:
    """The cover (COVER_WIDTH x COVER_HEIGHT RGB image) for a photo.

    An upload never gets here unless it passed prepare_cover's rules (a tall photo on plain white); a photo
    that is not of that kind is simply made to fill the tile, which is only used by tests and old tooling."""
    img = _flatten(img)
    background = _studio_background(img)
    if background is None:
        # Not a white-backdrop photo: fill the tile; keep the upper part, where the bodice and face are.
        return ImageOps.fit(img, (COVER_WIDTH, COVER_HEIGHT), method=Image.LANCZOS, centering=(0.5, 0.3))

    box = _content_box(img, background)
    subject = img.crop(box) if box else img
    # Multiply by (tile grey / white): a pure white background becomes exactly the tile grey while
    # the gown itself barely changes (1.6% darker at most), and it needs no cut-out, so white and
    # ivory gowns keep their soft edges.
    shaded = ImageChops.multiply(subject, Image.new("RGB", subject.size, TILE_GREY))
    fill = tuple(round(c * TILE_GREY[0] / 255) for c in background)

    stage_width = COVER_WIDTH * STAGE_WIDTH_SHARE
    stage_height = COVER_HEIGHT * STAGE_HEIGHT_SHARE
    scale = min(stage_width / subject.width, stage_height / subject.height, MAX_UPSCALE)
    size = (max(1, round(subject.width * scale)), max(1, round(subject.height * scale)))
    shaded = shaded.resize(size, Image.LANCZOS)

    canvas = Image.new("RGB", (COVER_WIDTH, COVER_HEIGHT), fill)
    left = (COVER_WIDTH - size[0]) // 2
    top = round(COVER_HEIGHT * STAGE_TOP_SHARE + (stage_height - size[1]) / 2)
    canvas.paste(shaded, (left, top))
    return canvas


def cover_jpeg_bytes(cover: Image.Image) -> bytes:
    buffer = BytesIO()
    cover.save(buffer, format="JPEG", quality=COVER_JPEG_QUALITY, optimize=True, progressive=True)
    return buffer.getvalue()


def _gown_share_on_tile(cover: Image.Image) -> float:
    """How much of the tile's height the gown takes up (0..1)."""
    box = cover.convert("L").point(lambda value: 255 if value < 238 else 0).getbbox()
    return (box[3] - box[1]) / cover.height if box else 0.0


@dataclass(frozen=True)
class PreparedCover:
    data: bytes                    # the finished cover as a JPEG
    warnings: tuple = ()           # notes for the owner that do not stop the upload


def prepare_cover(upload) -> PreparedCover:
    """Check a photo against the rules and make its cover. Raises CoverImageError, with one short plain
    message for the owner, for the first rule the photo breaks; nothing is stored here."""
    try:
        upload.seek(0)
        img = Image.open(upload)
        raw_width, raw_height = img.size
        width, height = _oriented_size(img)
    except Exception:
        raise CoverImageError(MSG_NOT_AN_IMAGE)
    if raw_width * raw_height > MAX_PIXELS:
        raise CoverImageError(MSG_TOO_LARGE)
    if height < width * TALL_RATIO or width < height * MIN_ASPECT:
        raise CoverImageError(MSG_NOT_TALL)
    if width < MIN_PHOTO_WIDTH or height < MIN_PHOTO_HEIGHT:
        raise CoverImageError(MSG_TOO_SMALL)

    try:
        if img.format == "JPEG":
            # decode a big JPEG at a reduced size -- a cover never needs more
            img.draft("RGB", (1800, 1200) if (width, height) != (raw_width, raw_height) else (1200, 1800))
        img.load()
        photo = _flatten(img)
    except Exception:
        raise CoverImageError(MSG_NOT_AN_IMAGE)

    background = _studio_background(photo)
    if background is None:
        # mostly white all round, but not all of it: something crosses the edge (a gown cut off at the side) or the
        # backdrop is shadowed -- say both; a frame that is not white at all is simply the wrong background
        raise CoverImageError(MSG_BACKGROUND_OR_EDGE if _frame_white_share(photo) >= 0.60 else MSG_BACKGROUND)
    extent = _gown_extent(photo, background)
    if extent is None:
        raise CoverImageError(MSG_NO_GOWN)
    left, top, right, bottom = extent
    if min(left, top, 1 - right, 1 - bottom) < EDGE_MARGIN:
        raise CoverImageError(MSG_CUT_OFF)
    if (bottom - top) * photo.height < MIN_GOWN_HEIGHT:
        raise CoverImageError(MSG_GOWN_SMALL)

    try:
        cover = build_cover(photo)
        data = cover_jpeg_bytes(cover)
    except Exception:
        raise CoverImageError(MSG_NOT_AN_IMAGE)
    warnings = []
    if width < SHARP_PHOTO_WIDTH or height < SHARP_PHOTO_HEIGHT:
        warnings.append(NOTE_SOFT)
    if _gown_share_on_tile(cover) < SMALL_ON_TILE:
        warnings.append(NOTE_WIDE)
    return PreparedCover(data=data, warnings=tuple(warnings))


def cover_from_upload(upload) -> bytes:
    """JPEG bytes of the cover made from an uploaded file (the rules in prepare_cover apply)."""
    return prepare_cover(upload).data
