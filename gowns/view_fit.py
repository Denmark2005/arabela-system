"""Fits a side or back photo to its front photo, so changing views on the product page never jumps.

The views are studio photos on a plain white (or nearly white) background. The garment is found in each photo, and the side or back garment is
drawn again on a pure white canvas the size of the front photo: the same height, the same top, centred where the front garment is. The front photo
is never changed. A photo that can't be fitted raises CannotFit with the reason, and the caller keeps the original."""
from io import BytesIO

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

MASK_WIDTH = 400          # the garment's outline is looked for on a smaller copy: quick, and fine detail doesn't matter for an outline
NOT_WHITE = 16            # a pixel this far from white always belongs to the garment ...
FAINT = 9                 # ... and on a very clean background a pale one does too (a white suit's shoulder), from this far at the least
BRIGHT = 232              # a pixel at least this bright in every colour counts as plain background
PLAIN_SHARE = 0.9         # at least this share of the picture's outer frame must be plain background
MAX_SIDE = 2400           # nothing is made bigger than this
MAX_PIXELS = 60_000_000   # more than any phone takes in a 5 MB file; opening a bigger one would run the web server out of memory
JPEG_QUALITY = 92
WHITE = (255, 255, 255)


class CannotFit(ValueError):
    """Why a photo can't be matched to the front, in words staff can read."""


def open_photo(data):
    """The photo in colour on white (a see-through background becomes white), turned the way the phone meant it, never larger than MAX_SIDE."""
    try:
        image = Image.open(BytesIO(data))
        if image.width * image.height > MAX_PIXELS:
            raise CannotFit("the picture is too large to work with")
        shrink = MAX_SIDE / max(image.size)
        if shrink < 1:                                              # a big JPEG is read already reduced, using far less memory
            image.draft(image.mode, (round(image.width * shrink), round(image.height * shrink)))
        image = ImageOps.exif_transpose(image).convert("RGBA")
    except CannotFit:
        raise
    except Exception as err:        # noqa: BLE001 - anything unreadable, whatever Pillow calls it
        raise CannotFit("the picture couldn't be opened") from err
    white = Image.new("RGBA", image.size, WHITE + (255,))
    white.alpha_composite(image)
    photo = white.convert("RGB")
    if max(photo.size) > MAX_SIDE:
        photo.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    return photo


def is_plain(photo):
    """Is the picture's frame (just inside the edges, where stray lines can be) plain white or nearly white?"""
    width, height = photo.size
    inset, band = max(2, width // 100), max(2, width // 50)
    inner = photo.crop((inset, inset, width - inset, height - inset))
    w, h = inner.size
    bright = total = 0
    for box in ((0, 0, w, band), (0, h - band, w, h), (0, band, band, h - band), (w - band, band, w, h - band)):
        red, green, blue = inner.crop(box).split()
        histogram = ImageChops.darker(ImageChops.darker(red, green), blue).histogram()
        bright += sum(histogram[BRIGHT:])
        total += sum(histogram)
    return total > 0 and bright >= total * PLAIN_SHARE


def _edge_noise(distance):
    """How far from white this picture's own background gets: 99% of the pixels in a band just inside its frame are no further than this."""
    width, height = distance.size
    inset, band = max(2, width // 100), max(2, width // 50)
    inner = distance.crop((inset, inset, width - inset, height - inset))
    w, h = inner.size
    counts = [0] * 256
    for box in ((0, 0, w, band), (0, h - band, w, h), (0, band, band, h - band), (w - band, band, w, h - band)):
        counts = [a + b for a, b in zip(counts, inner.crop(box).histogram())]
    seen, enough = 0, sum(counts) * 0.99
    for value, count in enumerate(counts):
        seen += count
        if seen >= enough:
            return value
    return 255


def _shapes(pixels, width, height):
    """The separate shapes of a black-and-white mask (touching side by side or top to bottom): (pixel numbers, box) for each."""
    seen = bytearray(width * height)
    for start in range(width * height):
        if not pixels[start] or seen[start]:
            continue
        seen[start] = 1
        stack, members = [start], []
        left = right = start % width
        top = bottom = start // width
        while stack:
            i = stack.pop()
            members.append(i)
            y, x = divmod(i, width)
            left, right, top, bottom = min(left, x), max(right, x), min(top, y), max(bottom, y)
            for j in (i - 1 if x else -1, i + 1 if x < width - 1 else -1, i - width if y else -1, i + width if y < height - 1 else -1):
                if j >= 0 and pixels[j] and not seen[j]:
                    seen[j] = 1
                    stack.append(j)
        yield members, (left, top, right + 1, bottom + 1)


def _filled(keep, width, height):
    """Everything the outside can't reach is part of the garment (a white shirt inside the outline stays)."""
    outside = bytearray(width * height)
    border = [x for x in range(width)] + [(height - 1) * width + x for x in range(width)]
    border += [y * width for y in range(height)] + [y * width + width - 1 for y in range(height)]
    stack = []
    for i in border:
        if not keep[i] and not outside[i]:
            outside[i] = 1
            stack.append(i)
    while stack:
        i = stack.pop()
        y, x = divmod(i, width)
        for j in (i - 1 if x else -1, i + 1 if x < width - 1 else -1, i - width if y else -1, i + width if y < height - 1 else -1):
            if j >= 0 and not keep[j] and not outside[j]:
                outside[j] = 1
                stack.append(j)
    return bytes(0 if flag else 255 for flag in outside)


def garment(photo):
    """(soft mask of the garment at the photo's size, the garment's box). The garment is the biggest shape that isn't white, plus anything
    close to it (a hanger above the shoulders)."""
    width, height = photo.size
    scale = min(1.0, MASK_WIDTH / width)
    small = photo.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.BILINEAR) if scale < 1 else photo
    red, green, blue = ImageChops.invert(small).split()
    distance = ImageChops.lighter(ImageChops.lighter(red, green), blue)           # how far each pixel is from white
    limit = min(NOT_WHITE, max(FAINT, _edge_noise(distance) + 4))                  # the cleaner the background, the paler the cloth found
    mask = distance.point(lambda v: 255 if v > limit else 0)
    w, h = mask.size
    margin = max(2, w // 150)                                                      # faint lines along the very edges are not the garment
    draw = ImageDraw.Draw(mask)
    for box in ((0, 0, w - 1, margin - 1), (0, h - margin, w - 1, h - 1), (0, 0, margin - 1, h - 1), (w - margin, 0, w - 1, h - 1)):
        draw.rectangle(box, fill=0)
    mask = mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))  # small gaps in the outline are closed
    shapes = sorted(_shapes(mask.tobytes(), w, h), key=lambda shape: len(shape[0]), reverse=True)
    if not shapes or len(shapes[0][0]) < w * h * 0.02:
        raise CannotFit("no garment was found in it")
    main, (left, top, right, bottom) = shapes[0]
    keep = bytearray(w * h)
    for members, (x0, y0, x1, y1) in shapes:
        near = x0 < right and x1 > left and y0 < bottom and y1 > top - h // 8      # something just above or inside the garment
        if members is main or (near and len(members) >= max(4, w * h // 5000)):
            for i in members:
                keep[i] = 255
    soft = Image.frombytes("L", (w, h), _filled(keep, w, h)).filter(ImageFilter.MaxFilter(3))
    soft = soft.resize((width, height), Image.BILINEAR).filter(ImageFilter.GaussianBlur(max(1.0, width / 900)))
    box = soft.point(lambda v: 255 if v > 127 else 0).getbbox()
    return soft, box


def fit_to_front(view_data, front_data):
    """The side or back photo redrawn to match the front photo, as JPEG bytes. Raises CannotFit when either photo can't be used."""
    front = open_photo(front_data)
    view = open_photo(view_data)
    if not is_plain(front):
        raise CannotFit("the front photo isn't on a plain white background")
    if not is_plain(view):
        raise CannotFit("it isn't on a plain white background")
    front_left, front_top, front_right, front_bottom = garment(front)[1]
    soft, (left, top, right, bottom) = garment(view)
    clean = Image.composite(view, Image.new("RGB", view.size, WHITE), soft)
    pad = 2 + view.width // 400
    cut = (max(0, left - pad), max(0, top - pad), min(view.width, right + pad), min(view.height, bottom + pad))
    piece = clean.crop(cut)
    scale = (front_bottom - front_top) / (bottom - top)
    scale = min(scale, front.width / piece.width)                                  # a very wide view is at most as wide as the picture
    piece = piece.resize((max(1, round(piece.width * scale)), max(1, round(piece.height * scale))), Image.LANCZOS)
    x = round((front_left + front_right) / 2 - ((left + right) / 2 - cut[0]) * scale)
    y = round(front_top - (top - cut[1]) * scale)
    canvas = Image.new("RGB", front.size, WHITE)
    canvas.paste(piece, (x, y))
    out = BytesIO()
    canvas.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()
