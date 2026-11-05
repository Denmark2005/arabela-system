"""The picture a customer uploads as proof of payment: decide whether it REALLY is a picture, and build the clean copy that gets stored.

Why this exists. The upload used to be accepted on its file name and on the "type" the sender's browser claims, and both are chosen by the
sender, so a PDF, a web page or a Windows program renamed to receipt.jpg was stored exactly as sent (and so was an empty file). Nothing
here trusts the name or the claimed type any more. The bytes are opened as an image; if that works, the picture is REBUILT pixel by pixel
and the rebuilt copy is what is stored, under a random name. Anything hidden in the file around the pixels (a PDF, a script, a program,
trailing data, metadata such as the phone's GPS position) is not part of the rebuilt copy.

A screenshot keeps its format (a PNG stays a PNG), so the small text and GCash reference numbers stay sharp; a photo stays a JPEG.
"""
import io
import uuid

from django.core.files.base import ContentFile
from PIL import Image, ImageOps

MAX_BYTES = 5 * 1024 * 1024        # the biggest upload accepted (the page already shrinks big camera photos before sending)
MAX_PIXELS = 25_000_000            # width x height: a 4000x3000 camera photo is 12 million, a phone screenshot about 3 million
MAX_SIDE = 4000                    # a bigger picture is scaled down so its longest side is at most this
ALLOWED = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}

NOT_AN_IMAGE = ("Proof of payment must be a JPG, PNG, or WEBP photo or screenshot. "
                "If your receipt is a PDF, take a screenshot of it and upload that.")
TOO_BIG = "Proof of payment must be 5MB or smaller."
EMPTY = "That file is empty. Please choose your receipt photo or screenshot."
TOO_LARGE_PICTURE = "That picture is too large. Please upload a normal photo or screenshot of your receipt."


def _flatten_to_rgb(img):
    """A copy without transparency (drawn on white), for saving as JPEG."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        base = Image.new("RGB", rgba.size, (255, 255, 255))
        base.paste(rgba, mask=rgba.getchannel("A"))
        return base
    return img.convert("RGB")


def _encode(img, fmt, quality=90):
    buffer = io.BytesIO()
    if fmt == "JPEG":
        picture = img if img.mode in ("RGB", "L") else _flatten_to_rgb(img)
        picture.save(buffer, "JPEG", quality=quality, optimize=True)
    elif fmt == "PNG":
        img.save(buffer, "PNG", optimize=True)
    else:
        picture = img if img.mode in ("RGB", "RGBA") else img.convert("RGBA" if "A" in img.mode else "RGB")
        picture.save(buffer, "WEBP", quality=quality, method=4)
    return buffer.getvalue()


def clean_proof_image(uploaded):
    """(ContentFile, "") for an upload that really is a JPEG, PNG or WEBP picture, or (None, message for the customer).

    The returned file is the rebuilt copy, named <random>.<jpg|png|webp>; the uploaded file's own name and claimed type are ignored."""
    size = getattr(uploaded, "size", None)
    if not uploaded or size is None:
        return None, EMPTY
    if size == 0:
        return None, EMPTY
    if size > MAX_BYTES:
        return None, TOO_BIG
    try:
        uploaded.seek(0)
        data = uploaded.read(MAX_BYTES + 1)
        uploaded.seek(0)
    except (OSError, ValueError, AttributeError):
        return None, NOT_AN_IMAGE
    if not data:
        return None, EMPTY
    if len(data) > MAX_BYTES:
        return None, TOO_BIG

    try:
        # formats=: only the three real picture decoders may even try, so a GIF, a PDF, a web page or a program is "not an image".
        img = Image.open(io.BytesIO(data), formats=list(ALLOWED))
        fmt = img.format
        if fmt not in ALLOWED:
            return None, NOT_AN_IMAGE
        width, height = img.size
        if width < 1 or height < 1:
            return None, NOT_AN_IMAGE
        if width * height > MAX_PIXELS:                 # looked at BEFORE decoding: a tiny file can hold a huge picture
            return None, TOO_LARGE_PICTURE
        img.load()                                      # decodes every pixel: a truncated or corrupt file fails here
        try:
            img = ImageOps.exif_transpose(img)         # a phone photo's rotation is applied to the pixels, so it stays upright
        except Exception:
            pass
        if max(img.size) > MAX_SIDE:
            img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
        clean = _encode(img, fmt)
        if len(clean) > MAX_BYTES and fmt != "JPEG":    # a very detailed PNG / WEBP: keep it under the limit as a JPEG
            fmt = "JPEG"
            clean = _encode(img, fmt)
        if len(clean) > MAX_BYTES:
            clean = _encode(img, "JPEG", quality=80)
        if len(clean) > MAX_BYTES:
            return None, TOO_BIG
    except Image.DecompressionBombError:
        return None, TOO_LARGE_PICTURE
    except Exception:
        # Not a picture, a damaged one, or something a decoder choked on: none of it is a usable receipt.
        return None, NOT_AN_IMAGE
    return ContentFile(clean, name=f"{uuid.uuid4().hex}{ALLOWED[fmt]}"), ""
