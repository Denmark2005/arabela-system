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
UPLOAD_MAX_BYTES = 8 * 1024 * 1024    # one picture staff add at the counter (a phone's photo is routinely 3-8 MB; it is shrunk before it is kept)
MAX_PIXELS = 40_000_000               # a picture bigger than this is refused (a decompression bomb)
COPY_STALE = timedelta(minutes=10)    # a copy that has been "being copied" this long was interrupted: it may be claimed again
VIEW_SALT = "arabela-id-photo-view"
VIEW_MAX_AGE = 300                    # seconds a viewing link works


class PhotoError(Exception):
    """A picture that cannot be kept (not a picture, or too big). `code`: "unreadable" | "too_large" | "" (the words to show are the message)."""

    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


def shrink(data):
    """JPEG bytes of the picture: turned upright, at most 1600 px on the longest side, with every bit of metadata (camera, location) left out."""
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as picture:
            if picture.width * picture.height > MAX_PIXELS:
                raise PhotoError("The picture is too large.", code="too_large")
            picture.draft("RGB", (MAX_SIDE, MAX_SIDE))          # a big JPEG is decoded already scaled down (never below 1600 px): far less memory and time; other kinds ignore this
            picture = ImageOps.exif_transpose(picture).convert("RGB")
            picture.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            picture.save(out, format="JPEG", quality=82, optimize=True)           # no exif= argument: nothing from the camera survives
            return out.getvalue()
    except PhotoError:
        raise
    except Exception as exc:        # noqa: BLE001 -- anything Pillow cannot read is "not a picture"
        raise PhotoError("That is not a picture we can keep.", code="unreadable") from exc


def read_upload(uploaded, side):
    """The bytes of one picture a member of staff added at the counter (an entry of request.FILES), or None when nothing was added, or PhotoError in words for staff.
    Only the SIZE is judged here; what the file really is gets decided by Pillow in shrink() (never by its name or the type the browser claims)."""
    if uploaded is None:
        return None
    size = getattr(uploaded, "size", 0) or 0
    if size <= 0:
        raise PhotoError(f"The {side} picture is empty. Please take it again or choose it again.", code="unreadable")
    if size > UPLOAD_MAX_BYTES:
        raise PhotoError(f"The {side} picture is over 8 MB. Please take it again or choose a smaller photo.", code="too_large")
    data = uploaded.read(UPLOAD_MAX_BYTES + 1)
    if not data or len(data) > UPLOAD_MAX_BYTES:
        raise PhotoError(f"The {side} picture could not be read. Please take it again or choose it again.", code="unreadable")
    return data


def remove_files(names):
    """Remove stored picture files by name (after something failed). Never raises: a file that cannot be removed now is left to the clean-up."""
    for name in names or []:
        try:
            default_storage.delete(name)
        except Exception:       # noqa: BLE001
            logger.warning("An ID picture file could not be removed just now")


def store_photos(check, pictures):
    """Keep the pictures staff added at the counter for `check`: [("front", bytes), ("back", bytes)]. Each one is shrunk, cleaned of camera and location details and kept under a
    long random name, exactly like the pictures copied from Didit. ALL OR NOTHING: when one cannot be kept, the ones already saved are removed again and PhotoError (in words for
    staff) is raised. Returns the stored file names."""
    names = []
    try:
        for side, data in pictures:
            try:
                jpeg = shrink(data)
            except PhotoError as exc:
                if exc.code == "too_large":
                    raise PhotoError(f"The {side} picture is too large. Please take it again.", code="too_large") from exc
                raise PhotoError(f"The {side} picture is not a photo we can read. Please take it again or choose a JPG, PNG or WEBP picture.", code="unreadable") from exc
            try:
                name = default_storage.save(f"id_photos/{secrets.token_urlsafe(24)}.jpg", ContentFile(jpeg))
            except Exception as exc:        # noqa: BLE001 -- the storage can fail; staff are told in words and nothing is left half-saved
                logger.exception("A counter ID picture (%s) of check %s could not be stored", side, check.pk)
                raise PhotoError(f"The {side} picture could not be saved just now. Please try again.") from exc
            names.append(name)
            IdentityPhoto.objects.create(id_check=check, side=side, storage_name=name, size_bytes=len(jpeg))
        IdentityCheck.objects.filter(pk=check.pk).update(photos_state="stored", updated_at=timezone.now())
        check.photos_state = "stored"               # the object in hand says what the row now says
    except Exception:
        remove_files(names)
        raise
    return names


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
