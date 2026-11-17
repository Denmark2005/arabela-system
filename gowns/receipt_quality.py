"""How readable an uploaded receipt picture is, measured on OUR side with Pillow (no AI, no network).

The receipt reader (gowns/receipt_check.py) can misread a blurry, tiny or very dark picture, and a misread must never be mistaken for a fake
receipt. This module gives the check an honest, repeatable number for "is this picture sharp enough to trust a refusal?". It is only ever used to
SOFTEN a decision (ask for a better picture instead of refusing); it never refuses anything by itself.

Sharpness = how much the brightness changes from one pixel to the next (the variance of a Laplacian filter), measured on a copy scaled to a
standard size, so a small sharp screenshot and a big sharp one score alike while a blurry photo scores low either way.
"""
import io

from PIL import Image, ImageFilter, ImageOps, ImageStat

STANDARD_SIDE = 1000          # the picture is scaled so its longest side is this before measuring (never enlarged past 2x)
BLURRY_BELOW = 10.0           # a Laplacian variance under this = too blurry to trust (real sharp receipts measure 125-380; the same receipts blurred by 1.5 px measure 6-8)
SOFT_BELOW = 80.0             # between the two = a little soft: usually readable, but small digits (a date, a number) may be misread, so no refusal on them
TINY_SIDE = 380               # a picture whose longest side is smaller than this is too small to read the reference number
DARK_BELOW = 45               # average brightness (0-255) under this = a very dark picture

_LAPLACIAN = ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128)


def picture_quality(image_bytes):
    """{"label": "sharp" | "soft" | "blurry", "sharpness": float, "brightness": float, "width": int, "height": int, "tiny": bool, "dark": bool,
    "poor": bool}. `poor` means "do not trust a refusal made from this picture". Never raises: a picture that cannot be measured is "unknown"."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            image = ImageOps.exif_transpose(source).convert("L")
    except Exception:       # noqa: BLE001  (any decoding trouble: nothing to measure)
        return {"label": "unknown", "sharpness": 0.0, "brightness": 0.0, "width": 0, "height": 0, "tiny": False, "dark": False, "poor": False}
    width, height = image.size
    longest = max(width, height)
    scale = min(STANDARD_SIDE / longest, 2.0) if longest else 1.0
    if abs(scale - 1.0) > 0.01:
        image = image.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.LANCZOS)
    # Pillow leaves the outer pixel ring of a filtered picture unfiltered, which would add a fixed amount that depends on the background colour;
    # measure the inside only.
    edges = image.filter(_LAPLACIAN)
    edges = edges.crop((2, 2, max(3, edges.width - 2), max(3, edges.height - 2)))
    sharpness = float(ImageStat.Stat(edges).var[0])
    brightness = float(ImageStat.Stat(image).mean[0])
    label = "blurry" if sharpness < BLURRY_BELOW else "soft" if sharpness < SOFT_BELOW else "sharp"
    tiny = longest < TINY_SIDE
    dark = brightness < DARK_BELOW
    return {"label": label, "sharpness": round(sharpness, 1), "brightness": round(brightness, 1), "width": width, "height": height,
            "tiny": tiny, "dark": dark, "poor": label == "blurry" or tiny}
