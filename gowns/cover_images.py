"""Turns a photo into a category "cover": the picture on a category's tile on the customer site.

ONE place does this, so the pictures bundled with the site and the ones the owner uploads from
Admin -> Categories always look the same:

- Every tile is 2:3 (portrait) on the shop's tile grey.
- A studio shot (a gown cut out on a plain white background, like the shop's own photos) is never
  squashed or cut through the gown: the gown is framed in the upper part of the tile, its white
  background is shaded to the tile grey so no white box shows, and the strip at the bottom stays
  free for the category name.
- Any other photo (a gown photographed in a room, outdoors...) fills the tile instead.

Only Pillow is used (it is already a requirement); nothing here touches the database.
"""
from __future__ import annotations

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
MIN_SIDE = 200

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


def _studio_background(img: Image.Image):
    """The photo's background colour when it is a plain near-white studio backdrop, else None.

    Looks only at a thin frame around the photo: if that frame is (nearly) all the same bright
    neutral colour, the subject was shot on a white backdrop. Portrait photos only -- a wide photo
    is a scene, not a cut-out gown."""
    width, height = img.size
    if height < width * 1.2:
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


def build_cover(img: Image.Image) -> Image.Image:
    """The cover (COVER_WIDTH x COVER_HEIGHT RGB image) for a photo."""
    img = _flatten(img)
    background = _studio_background(img)
    if background is None:
        # A scene photo fills the tile; keep the upper part, where the bodice and face are.
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


def cover_from_upload(upload) -> bytes:
    """JPEG bytes of the cover made from an uploaded file; raises CoverImageError (with a message
    for the owner) when the file isn't a usable photo."""
    try:
        upload.seek(0)
        img = Image.open(upload)
        width, height = img.size
    except Exception:
        raise CoverImageError("That file isn't a valid image.")
    if width < MIN_SIDE or height < MIN_SIDE:
        raise CoverImageError("That photo is too small. Use one at least 200 pixels wide and tall.")
    if width * height > MAX_PIXELS:
        raise CoverImageError("That photo is too large. Use one under 25 megapixels (most phone photos are fine).")
    try:
        if img.format == "JPEG":
            img.draft("RGB", (1200, 1800))  # decode a big JPEG at a reduced size -- a cover never needs more
        img.load()
        return cover_jpeg_bytes(build_cover(img))
    except CoverImageError:
        raise
    except Exception:
        raise CoverImageError("That file isn't a valid image.")
