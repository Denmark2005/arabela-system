"""Real (tiny) pictures for tests.

The payment-proof upload now opens what it is given as an image (gowns/proof_image.py), so the fake "JPEG" bytes the tests used to send
-- a JPEG header and nothing else -- are refused, exactly as a renamed PDF would be. Tests that upload a proof use these instead.
Pillow is a real dependency of the project, so nothing here needs a file on disk."""
import io
import itertools

from PIL import Image


def make_image_bytes(fmt="JPEG", size=(8, 8), color=(10, 98, 255)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, fmt)
    return buffer.getvalue()


TINY_JPEG = make_image_bytes()

_picture_number = itertools.count(1)


def unique_jpeg():
    """A different real picture on every call. The receipt check refuses a picture that was already used for another booking, so a test
    that books more than once must not upload the same bytes each time (a real customer pays, and screenshots, every time)."""
    n = next(_picture_number)
    return make_image_bytes(color=((n * 53) % 256, (n * 97) % 256, (n * 29 + 7) % 256))
