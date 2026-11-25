"""The pictures of an ID: copied from Didit the moment a check finishes, shrunk, kept in the file storage under a long random name, shown ONLY to the owner
(arabela_admin/identity_views.py) and deleted 30 days after the customer's last return (reservations/identity.py cleanup).

Only the FRONT and BACK of the ID are kept. The selfie is never copied, and the ID number is never typed out anywhere.
Didit's picture links are signed and short-lived, so they are used once, right away, and never saved."""
import io
import logging
import secrets
from datetime import timedelta

from django.core import signing
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db.models import F, Q
from django.utils import timezone
from PIL import Image, ImageOps

from . import didit
from .models import IdentityCheck, IdentityPhoto

logger = logging.getLogger(__name__)

MAX_SIDE = 1600                       # the longest side of a kept picture, in pixels
MAX_PIXELS = 40_000_000               # a picture bigger than this is refused (a decompression bomb)
COPY_STALE = timedelta(minutes=10)    # a copy that has been "being copied" this long was interrupted: it may be claimed again
VIEW_SALT = "arabela-id-photo-view"
VIEW_MAX_AGE = 300                    # seconds a viewing link works


class PhotoError(Exception):
    """A picture that cannot be kept (not a picture, or too big)."""


def shrink(data):
    """JPEG bytes of the picture: turned upright, at most 1600 px on the longest side, with every bit of metadata (camera, location) left out."""
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as picture:
            if picture.width * picture.height > MAX_PIXELS:
                raise PhotoError("The picture is too large.")
            picture = ImageOps.exif_transpose(picture).convert("RGB")
            picture.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            picture.save(out, format="JPEG", quality=82, optimize=True)           # no exif= argument: nothing from the camera survives
            return out.getvalue()
    except PhotoError:
        raise
    except Exception as exc:        # noqa: BLE001 -- anything Pillow cannot read is "not a picture"
        raise PhotoError("That is not a picture we can keep.") from exc


def copy_photos(check_id, decision=None):
    """Copy the FRONT and BACK of the ID for this check from Didit into the file storage. Safe to call any number of times, from anywhere at the same moment:
    only one caller claims the work, and a side that is already kept is never copied again. Never raises. Returns True when every picture Didit has is kept.

    The check's `photos_state` ends as 'stored' (all kept), 'failed' (something could not be copied: it is tried again later), or '' (Didit has no picture
    and the check was declined, so there is nothing to keep)."""
    now = timezone.now()
    claimed = IdentityCheck.objects.filter(pk=check_id).filter(
        Q(photos_state__in=('', 'pending', 'failed')) | Q(photos_state='copying', updated_at__lt=now - COPY_STALE),
    ).exclude(session_id='').update(photos_state='copying', photos_tries=F('photos_tries') + 1, updated_at=now)
    if not claimed:
        return False
    outcome = 'failed'
    try:
        check = IdentityCheck.objects.get(pk=check_id)
        if decision is None or not (decision.front_url or decision.back_url):
            decision = didit.retrieve_decision(check.session_id)          # fresh links: the ones from earlier have expired
        have = set(check.photos.values_list('side', flat=True))
        wanted = {IdentityPhoto.Side.FRONT: decision.front_url, IdentityPhoto.Side.BACK: decision.back_url}
        links = [side for side, url in wanted.items() if url]
        problems = 0
        for side in links:
            if side in have:
                continue
            try:
                data = shrink(didit.download_picture(wanted[side]))
                name = default_storage.save(f"id_photos/{secrets.token_urlsafe(24)}.jpg", ContentFile(data))
                IdentityPhoto.objects.create(id_check=check, side=side, storage_name=name, size_bytes=len(data))
            except (didit.DiditError, PhotoError) as exc:
                problems += 1
                logger.warning("An ID picture (%s) of check %s could not be kept: %s", side, check_id, type(exc).__name__)
            except Exception:       # noqa: BLE001 -- the storage can fail too; it must not take the page down
                problems += 1
                logger.exception("An ID picture (%s) of check %s could not be stored", side, check_id)
        if not links:
            outcome = '' if check.status == IdentityCheck.Status.DECLINED else 'failed'
        else:
            outcome = 'failed' if problems else 'stored'
    except didit.DiditError as exc:
        logger.warning("The ID pictures of check %s could not be fetched: %s", check_id, exc.message)
    except Exception:       # noqa: BLE001
        logger.exception("The ID pictures of check %s could not be copied", check_id)
    IdentityCheck.objects.filter(pk=check_id).update(photos_state=outcome, updated_at=timezone.now())
    return outcome == 'stored'


def delete_photos(check):
    """Delete every picture of this check (the rows, and through them the files). Returns how many were deleted."""
    photos = list(IdentityPhoto.objects.filter(id_check=check))
    for photo in photos:
        photo.delete()                      # post_delete removes the file from the storage
    IdentityCheck.objects.filter(pk=check.pk).update(photos_state='deleted', updated_at=timezone.now())
    return len(photos)


def read_photo(photo):
    """The bytes of one kept picture."""
    with default_storage.open(photo.storage_name, 'rb') as handle:
        return handle.read()


# ---------------------------------------------------------------- the owner's viewing links
def view_token(user_id, photo_id):
    """A link token for ONE picture, for ONE signed-in owner, good for 5 minutes (it is checked again with the owner's session on every request)."""
    return signing.dumps({"p": int(photo_id), "u": int(user_id)}, salt=VIEW_SALT)


def read_view_token(token):
    """(photo_id, user_id) of a good token, else None."""
    try:
        data = signing.loads(token or "", salt=VIEW_SALT, max_age=VIEW_MAX_AGE)
        return int(data["p"]), int(data["u"])
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return None
