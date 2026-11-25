"""ID verification: who has to verify, how a check is started, read back from Didit, decided by the owner, bound to a booking and cleaned up.

The rules (decided with the owner, 2026-11-25):
  * A customer must have a verified valid ID BEFORE they can pay online, and staff must record one for every walk-in booking. The owner has one switch for both
    (SiteSettings.require_id_before_payment); the online rule also needs Didit to be set up (reservations/didit.py), because a customer cannot verify without it.
  * Staff can satisfy the walk-in rule two ways: the customer verifies on their own phone from a QR code (Didit), or staff look at the ORIGINAL ID at the counter
    and confirm it (their name and the time are kept, and it is audited). A linked website account that is already verified also counts.
  * A verified ID counts for 12 months. Didit's "In Review" results are decided by the OWNER (approve / decline); staff cannot.
  * What is kept: the result, the ID type and the front and back pictures (owner-only, deleted 30 days after the customer's last return). Never the selfie or the
    ID number.

Like a payment, a check is never believed from the browser or from a webhook's body: the server reads the decision back from Didit (`refresh`)."""
import logging
import re
import secrets
import unicodedata
from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts import rate_limit
from accounts.models import CustomerMessage, UserProfile
from gowns.models import SiteSettings

from . import didit, identity_photos
from .models import IdentityCheck, Reservation

logger = logging.getLogger(__name__)

VALID_FOR = timedelta(days=365)               # a verified ID counts this long
PHOTOS_KEEP = timedelta(days=30)              # the pictures go this long after the customer's last return
POLL_GAP = timedelta(seconds=2)               # the return page may ask Didit about one check at most this often
WALKIN_CHECK_LIFE = timedelta(hours=12)       # a walk-in's check must be used for a booking within this long
START_LIMIT = (10, 60 * 60)                   # "Verify my ID" presses per account per hour
WALKIN_START_LIMIT = (30, 10 * 60)            # QR codes / counter checks per staff member per 10 minutes
RETRY_FAILED_FOR = timedelta(days=3)          # a picture copy that failed is tried again for this long

CONSENT_TEXT = ("I agree that Arabela Gown Rental may check my valid ID with its verification service, and may keep the front and back "
                "pictures of my ID, seen only by the shop owner, until 30 days after my last return.")

S = IdentityCheck.Status
SRC = IdentityCheck.Source
OPEN_RESERVATION = (Reservation.Status.PENDING, Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE, Reservation.Status.OVERDUE)


class IdentityError(Exception):
    """Something staff or a customer must be told. `message` is plain words; `status` is the HTTP code; `extra` rides along in the JSON answer."""

    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra


# ---------------------------------------------------------------- the rules
def switch_on():
    """The owner's switch (Edit Profile): ID before booking."""
    return bool(SiteSettings.load().require_id_before_payment)


def required_online():
    """An online customer must have a verified ID to pay: the switch is on AND Didit is set up (without it nobody could verify)."""
    return switch_on() and didit.enabled()


def required_walkin():
    """A walk-in booking needs an ID check: the switch is on (staff can always check at the counter, so Didit is not needed)."""
    return switch_on()


def phone_digits(phone):
    return re.sub(r"\D", "", str(phone or ""))


def valid_check(user):
    """The customer's newest verified ID that still counts, or None."""
    if user is None or not getattr(user, "pk", None):
        return None
    return (IdentityCheck.objects.filter(customer=user, status=S.APPROVED, expires_at__gt=timezone.now())
            .order_by("-verified_at", "-id").first())


def is_verified(user):
    return valid_check(user) is not None


def _display_name(user):
    profile = UserProfile.objects.filter(user=user).first()
    return ((profile.display_name if profile else "") or user.get_full_name() or user.get_username())[:120]


def _plain_day(moment):
    return f"{moment:%b} {moment.day}, {moment.year}" if moment else ""


# ---------------------------------------------------------------- what the customer is shown
def customer_state(user):
    """The customer's ID situation as plain JSON for their pages: {required, available, state, label, ...}. state: verified | checking | review | declined | expired | none."""
    available = didit.enabled()
    base = {"required": required_online(), "available": available, "types": [{"value": v, "label": l} for v, l in IdentityCheck.IdType.choices],
            "consent": CONSENT_TEXT, "state": "none", "label": "Not verified", "url": "", "until": "", "idType": "", "idTypeLabel": "", "reasons": []}
    if user is None or not getattr(user, "pk", None):
        return base
    current = valid_check(user)
    if current is not None:
        base.update(state="verified", label="Verified", until=_plain_day(timezone.localtime(current.expires_at)), idType=current.id_type, idTypeLabel=current.get_id_type_display())
        return base
    latest = IdentityCheck.objects.filter(customer=user).order_by("-created_at", "-id").first()
    if latest is None:
        return base
    base.update(idType=latest.id_type, idTypeLabel=latest.get_id_type_display())
    if latest.status == S.PENDING:
        base.update(state="checking", label="Checking your ID", url=latest.session_url)
    elif latest.status == S.REVIEW:
        base.update(state="review", label="Waiting for the shop to review your ID")
    elif latest.status == S.DECLINED:
        base.update(state="declined", label="Your ID could not be verified", reasons=list(latest.reasons or []))
    elif latest.status == S.APPROVED:                      # approved, but past its 12 months
        base.update(state="expired", label="Your ID check has expired")
    return base


def page_state(check):
    """What the customer's "checking your ID" page shows for one check."""
    if check.status == S.APPROVED:
        return {"state": "approved", "until": _plain_day(timezone.localtime(check.expires_at)) if check.expires_at else ""}
    if check.status == S.DECLINED:
        return {"state": "declined", "reasons": list(check.reasons or [])}
    if check.status == S.REVIEW:
        return {"state": "review"}
    if check.status == S.PENDING:
        return {"state": "checking"}
    return {"state": "ended"}


# ---------------------------------------------------------------- starting a customer's check
def start_for_customer(*, user, id_type, consent, callback_for):
    """The customer pressed "Verify my ID". `callback_for(token)` gives the address Didit sends them back to. Returns {state, url?, token?}:
      * already verified            -> state "verified" (no new check);
      * a check still open          -> the SAME check (state "checking" with its page, or "review"): pressing twice never makes a second one;
      * otherwise                   -> a new check and a new Didit page.
    Raises IdentityError with words the customer can be shown."""
    if not didit.enabled():
        raise IdentityError("ID verification is not available right now. Please try again later.", 503)
    if id_type not in IdentityCheck.IdType.values:
        raise IdentityError("Please choose the kind of ID you will use.")
    if consent is not True:
        raise IdentityError("Please tick the box to agree before we check your ID.")
    if not rate_limit.allowed(f"idstart:{user.pk}", *START_LIMIT):
        raise IdentityError("You have tried this several times. Please wait a little while and try again.", 429)

    current = valid_check(user)
    if current is not None:
        return {"state": "verified", "until": _plain_day(timezone.localtime(current.expires_at))}

    open_check = IdentityCheck.objects.filter(customer=user, source=SRC.ONLINE, status__in=(S.PENDING, S.REVIEW)).order_by("-id").first()
    if open_check is not None:
        open_check = refresh(open_check.pk, source="start") or open_check          # it may have finished while the customer was away
        if open_check.status == S.APPROVED and open_check.is_valid:
            return {"state": "verified", "until": _plain_day(timezone.localtime(open_check.expires_at))}
        if open_check.status == S.REVIEW:
            return {"state": "review", "token": open_check.token}
        if open_check.status == S.PENDING and open_check.session_url:
            if open_check.id_type != id_type:
                IdentityCheck.objects.filter(pk=open_check.pk).update(id_type=id_type, updated_at=timezone.now())
            return {"state": "checking", "url": open_check.session_url, "token": open_check.token}

    try:
        with transaction.atomic():
            get_user_model().objects.select_for_update().filter(pk=user.pk).first()             # two presses at once wait for each other here
            twin = IdentityCheck.objects.filter(customer=user, source=SRC.ONLINE, status=S.PENDING).first()
            if twin is not None and twin.session_url:
                return {"state": "checking", "url": twin.session_url, "token": twin.token}
            check = IdentityCheck.objects.create(
                token=secrets.token_urlsafe(24), source=SRC.ONLINE, customer=user, subject_name=_display_name(user), id_type=id_type, consent_at=timezone.now())
    except IntegrityError:
        twin = IdentityCheck.objects.filter(customer=user, source=SRC.ONLINE, status=S.PENDING).first()
        if twin is not None and twin.session_url:
            return {"state": "checking", "url": twin.session_url, "token": twin.token}
        raise IdentityError("Your ID check is opening. Please wait a moment and try again.", 409)

    return _open_session(check, vendor_data=f"c{user.pk}", callback_for=callback_for)


def _open_session(check, *, vendor_data, callback_for, fresh=True):
    """Ask Didit for the verification page of this check and keep it. Returns the answer for the screen; on trouble the check is closed (nothing is left half-made)."""
    try:
        session = didit.create_session(vendor_data=vendor_data, callback=callback_for(check.token), metadata={"check": check.pk})
    except didit.DiditError as exc:
        logger.warning("The ID check %s could not be opened: %s", check.pk, exc.message)
        IdentityCheck.objects.filter(pk=check.pk, status=S.PENDING).update(status=S.CANCELLED, note="The verification page could not be opened.", finished_at=timezone.now())
        raise IdentityError("We could not open the ID check just now. Nothing was saved. Please try again in a moment.", 502)
    try:
        with transaction.atomic():
            IdentityCheck.objects.filter(pk=check.pk).update(
                session_id=session.session_id, session_url=session.url, session_expires_at=session.expires_at, provider_status=session.status[:30], updated_at=timezone.now())
    except IntegrityError:
        # Didit gave back a session another check of ours already holds (it keeps one unfinished session per person): it is never saved on two checks.
        other = IdentityCheck.objects.filter(session_id=session.session_id).exclude(pk=check.pk).first()
        revivable = bool(other is not None and other.status in (S.CANCELLED, S.EXPIRED) and other.customer_id and other.customer_id == check.customer_id)
        if revivable or (other is not None and other.status == S.PENDING and other.session_url):
            # This check gives its place up first (a customer may have only one waiting check), and the other check's page is the one used.
            IdentityCheck.objects.filter(pk=check.pk, status=S.PENDING).update(status=S.CANCELLED, note="Another check already holds this session.", finished_at=timezone.now())
            if revivable:
                # The same person's older check that was closed here while Didit still has its page open: that page is still the right one, so it is reopened.
                try:
                    with transaction.atomic():
                        IdentityCheck.objects.filter(pk=other.pk, status__in=(S.CANCELLED, S.EXPIRED)).update(status=S.PENDING, finished_at=None, note="", updated_at=timezone.now())
                except IntegrityError:
                    pass
                other = IdentityCheck.objects.get(pk=other.pk)
            if other.status == S.PENDING and other.session_url:
                return {"state": "checking", "url": other.session_url, "token": other.token}
        elif other is not None and fresh:
            # The session belongs to a check that is DECIDED here (the owner answered it, say) while Didit still has its page open. That page cannot serve a new
            # check, so Didit is asked again under a name of its own, which gives a fresh page (never a customer stuck on "opening" for good).
            return _open_session(check, vendor_data=f"{vendor_data}-{check.pk}", callback_for=callback_for, fresh=False)
        IdentityCheck.objects.filter(pk=check.pk, status=S.PENDING).update(status=S.CANCELLED, note="Another check already holds this session.", finished_at=timezone.now())
        raise IdentityError("Your ID check is opening. Please wait a moment and try again.", 409)
    return {"state": "checking", "url": session.url, "token": check.token}


# ---------------------------------------------------------------- reading the result back
_TYPE_OK = {
    "philid": {"Identity Card"}, "umid": {"Identity Card"}, "drivers_license": {"Driver's License"}, "passport": {"Passport"},
    "sss": {"Social Security Card", "Identity Card"}, "prc": {"Identity Card", "Other"}, "voters_id": {"Identity Card", "Other"}, "postal": {"Identity Card", "Other"},
}


def type_check(id_type, document_type):
    """'match' / 'differs' / '' : the ID type the person chose against the document Didit says it scanned. 'Other' (or nothing scanned) is never compared."""
    if id_type not in _TYPE_OK or not document_type:
        return ""
    return "match" if document_type in _TYPE_OK[id_type] else "differs"


def _name_tokens(name):
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii").lower()
    return {word for word in re.split(r"[^a-z0-9]+", text) if len(word) > 1}


def name_check(given, on_id):
    """'match' / 'differs' / '' : the name on the ID against the name given. Spelling order, middle names and accents do not matter; two shared words (or all of a
    one-word name) is a match. The name on the ID is compared and then thrown away."""
    a, b = _name_tokens(given), _name_tokens(on_id)
    if not a or not b:
        return ""
    shared = a & b
    return "match" if (len(shared) >= 2 or (shared and len(shared) == min(len(a), len(b)))) else "differs"


def refresh(check_id, *, source="poll"):
    """Ask Didit how this check stands and write it down (once). Safe to call any number of times, from anywhere, at the same moment. `source`: "poll" (a page asking),
    "webhook", "start", "sweep". Returns the check as it stands."""
    check = IdentityCheck.objects.filter(pk=check_id).first()
    if check is None or not check.session_id:
        return check
    # A check the OWNER or staff already decided is never changed by what Didit says. A check waiting for the owner is only looked at when Didit tells us (webhook).
    if check.status == S.PENDING or (check.status == S.REVIEW and source in ("webhook", "sweep")):
        pass
    else:
        return check
    now = timezone.now()
    if source in ("poll", "start") and check.last_checked_at and now - check.last_checked_at < POLL_GAP:
        return check
    IdentityCheck.objects.filter(pk=check.pk).update(last_checked_at=now)
    try:
        decision = didit.retrieve_decision(check.session_id)
    except didit.DiditError as exc:
        logger.warning("ID check %s could not be read from Didit: %s", check.pk, exc.message)
        return IdentityCheck.objects.get(pk=check.pk)                    # still waiting; the next look asks again
    return apply_decision(check.pk, decision)


def apply_decision(check_id, decision):
    """Write Didit's decision on the check. Idempotent; a decision already made by the owner or staff is never overwritten. Returns the check."""
    with transaction.atomic():
        check = IdentityCheck.objects.select_for_update().get(pk=check_id)
        if check.status not in (S.PENDING, S.REVIEW) or check.decision_source in (IdentityCheck.DecisionSource.OWNER, IdentityCheck.DecisionSource.COUNTER):
            return check
        now = timezone.now()
        check.provider_status = (decision.status or "")[:30]
        changed = False
        if decision.status == didit.STATUS_APPROVED:
            check.status = S.APPROVED
            check.decision_source = IdentityCheck.DecisionSource.DIDIT
            check.decided_at = check.verified_at = check.finished_at = now
            check.expires_at = now + VALID_FOR
            check.reasons = []
            check.type_check = type_check(check.id_type, decision.document_type)
            check.name_check = name_check(check.subject_name, decision.name)
            changed = True
        elif decision.status == didit.STATUS_DECLINED:
            check.status = S.DECLINED
            check.decision_source = IdentityCheck.DecisionSource.DIDIT
            check.decided_at = check.finished_at = now
            check.reasons = list(decision.reasons)[:4]
            check.type_check = type_check(check.id_type, decision.document_type)
            changed = True
        elif decision.status == didit.STATUS_REVIEW:
            if check.status != S.REVIEW:
                changed = True
            check.status = S.REVIEW
            check.reasons = list(decision.reasons)[:4]
            check.type_check = type_check(check.id_type, decision.document_type)
            check.name_check = name_check(check.subject_name, decision.name)
        elif decision.status in (didit.STATUS_EXPIRED, didit.STATUS_ABANDONED, "Kyc Expired"):
            check.status = S.EXPIRED
            check.finished_at = now
            changed = True
        if changed and check.status in (S.APPROVED, S.DECLINED, S.REVIEW) and check.photos_state in ('', 'failed'):
            check.photos_state = 'pending'
        check.save()
    if changed and check.status in (S.APPROVED, S.DECLINED, S.REVIEW):
        identity_photos.copy_photos(check.pk, decision)               # at once: Didit's picture links do not last
        check = IdentityCheck.objects.get(pk=check.pk)
    return check


# ---------------------------------------------------------------- the owner decides a check that needed a look
def owner_decide(check_id, *, approve, user, note=""):
    """The owner approves or declines a check Didit sent for review. Returns the check. Raises IdentityError when it is not waiting any more."""
    note = re.sub(r"\s+", " ", str(note or "")).strip()[:200]
    with transaction.atomic():
        check = IdentityCheck.objects.select_for_update().filter(pk=check_id).first()
        if check is None:
            raise IdentityError("That ID check was not found. Please refresh the page.", 404)
        if check.status != S.REVIEW:
            raise IdentityError("This ID check is not waiting for a decision any more. Please refresh the page.", 409)
        now = timezone.now()
        check.status = S.APPROVED if approve else S.DECLINED
        check.decision_source = IdentityCheck.DecisionSource.OWNER
        check.decided_by, check.decided_at, check.finished_at, check.note = user, now, now, note
        if approve:
            check.verified_at, check.expires_at, check.reasons = now, now + VALID_FOR, []
        check.save()
    if check.customer_id:
        CustomerMessage.objects.create(
            recipient_id=check.customer_id, category=CustomerMessage.Category.GENERAL,
            body=("The shop checked your ID and it is verified. You can go ahead and pay for your booking." if approve
                  else "The shop checked your ID and could not verify it. Please verify again with a clear, valid ID, or visit the shop."))
    return check


# ---------------------------------------------------------------- walk-in bookings (staff)
def _clean_phone(phone):
    digits = phone_digits(phone)
    if digits.startswith("63") and len(digits) == 12:
        digits = "0" + digits[2:]
    elif digits.startswith("9") and len(digits) == 10:
        digits = "0" + digits
    return digits


def start_walkin(*, staff, name, phone, id_type, callback_for):
    """Staff pressed "Scan to verify" in New Booking: a QR code for the customer's own phone. The same press again (same customer) is the same QR code.
    Returns {state, url, token}."""
    if not didit.enabled():
        raise IdentityError("Scanning is not available right now. Use \"Checked at the counter\" instead.", 503)
    if id_type not in IdentityCheck.IdType.values:
        raise IdentityError("Choose the kind of ID the customer will use.")
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:120]
    digits = _clean_phone(phone)
    if not name or len(digits) != 11:
        raise IdentityError("Type the customer's name and mobile number first. The ID check is made for that customer.")
    if not rate_limit.allowed(f"idwalkin:{staff.pk}", *WALKIN_START_LIMIT):
        raise IdentityError("That was a lot of QR codes in a short time. Please wait a minute and try again.", 429)
    cutoff = timezone.now() - timedelta(hours=1)
    same = IdentityCheck.objects.filter(source=SRC.WALKIN, staff=staff, subject_phone=digits, status=S.PENDING, created_at__gte=cutoff).exclude(session_url='').order_by("-id").first()
    if same is not None:
        if same.id_type != id_type or same.subject_name != name:
            IdentityCheck.objects.filter(pk=same.pk).update(id_type=id_type, subject_name=name, updated_at=timezone.now())
        return {"state": "checking", "url": same.session_url, "token": same.token}
    check = IdentityCheck.objects.create(token=secrets.token_urlsafe(24), source=SRC.WALKIN, staff=staff, subject_name=name, subject_phone=digits,
                                         id_type=id_type, consent_at=timezone.now())
    return _open_session(check, vendor_data=f"w{check.pk}", callback_for=callback_for)


def counter_check(*, staff, name, phone, id_type, note=""):
    """Staff looked at the customer's ORIGINAL valid ID at the counter and confirm it. Kept: who, when, the ID type and a note (no picture). Returns the check."""
    if id_type not in IdentityCheck.IdType.values:
        raise IdentityError("Choose the kind of ID you checked.")
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:120]
    digits = _clean_phone(phone)
    if not name or len(digits) != 11:
        raise IdentityError("Type the customer's name and mobile number first. The ID check is made for that customer.")
    if not rate_limit.allowed(f"idwalkin:{staff.pk}", *WALKIN_START_LIMIT):
        raise IdentityError("That was a lot of ID checks in a short time. Please wait a minute and try again.", 429)
    now = timezone.now()
    return IdentityCheck.objects.create(
        token=secrets.token_urlsafe(24), source=SRC.COUNTER, status=S.APPROVED, staff=staff, subject_name=name, subject_phone=digits, id_type=id_type,
        note=re.sub(r"\s+", " ", str(note or "")).strip()[:200], decision_source=IdentityCheck.DecisionSource.COUNTER, decided_by=staff, decided_at=now,
        verified_at=now, expires_at=now + VALID_FOR, finished_at=now, consent_at=now)


def staff_state(check):
    """What the staff screen's ID row shows for one check."""
    out = {"state": {S.APPROVED: "approved", S.DECLINED: "declined", S.REVIEW: "review", S.PENDING: "checking"}.get(check.status, "ended"),
           "token": check.token, "source": check.source, "idType": check.id_type, "idTypeLabel": check.get_id_type_display(),
           "reasons": list(check.reasons or []), "counter": check.decision_source == IdentityCheck.DecisionSource.COUNTER}
    if check.status == S.APPROVED and check.expires_at:
        out["until"] = _plain_day(timezone.localtime(check.expires_at))
    return out


def usable_walkin_check(token, *, phone, now=None):
    """The walk-in / counter check this token names, if it can be used for a booking for `phone`: verified, made for that phone, made in the last 12 hours and not already
    used for another booking. Raises IdentityError with words for staff otherwise."""
    check = IdentityCheck.objects.filter(token=str(token or ""), source__in=(SRC.WALKIN, SRC.COUNTER)).first()
    if check is None:
        raise IdentityError("That ID check was not found. Check the customer's ID again.", 409, id_required=True)
    if not check.is_valid:
        raise IdentityError("The customer's ID is not verified yet. Wait for the scan to finish, or choose \"Checked at the counter\".", 409, id_required=True)
    if check.subject_phone != _clean_phone(phone):
        raise IdentityError("That ID check was made for a different mobile number. Check the customer's ID again.", 409, id_required=True)
    if (now or timezone.now()) - check.created_at > WALKIN_CHECK_LIFE:
        raise IdentityError("That ID check is more than 12 hours old. Check the customer's ID again.", 409, id_required=True)
    if check.reservations.exists():
        raise IdentityError("That ID check was already used for another booking. Check the customer's ID again.", 409, id_required=True)
    return check


def walkin_requirement(*, account, phone, token):
    """The ID check a walk-in booking rests on, or None when none is needed. With the owner's switch ON a booking needs a verified check: a good one named by `token`,
    or the linked website account's own verified ID. With it OFF, nothing is needed (a good check that was made is still kept with the booking)."""
    if token:
        try:
            return usable_walkin_check(token, phone=phone)
        except IdentityError:
            if required_walkin():
                raise
            return None
    if account is not None:
        current = valid_check(account)
        if current is not None:
            return current
    if required_walkin():
        raise IdentityError("Check the customer's valid ID first: have them scan the QR code, or confirm you checked the original ID at the counter.", 400, id_required=True)
    return None


# ---------------------------------------------------------------- the online gate
def online_gate(user):
    """None when this customer may pay online; else an IdentityError to turn the payment away (the checkout page shows it by the ID box)."""
    if not required_online():
        return None
    current = valid_check(user)
    if current is not None:
        return None
    return IdentityError("Please verify your valid ID before you pay. It only takes a minute.", 403, id_required=True)


def current_check_for(user):
    """The check a customer's new online booking rests on (their verified ID), or None."""
    return valid_check(user)


# ---------------------------------------------------------------- what staff see on a booking
def who(check):
    """The name to show for a check: the customer's current profile name when they have an account, else the name typed at the shop."""
    if check.customer_id:
        profile = getattr(check.customer, "profile", None)
        return (profile.display_name if profile and profile.display_name else "") or check.subject_name or check.customer.get_username()
    return check.subject_name or "Walk-in customer"


def badge(reservation):
    """{kind, words, check} for the small ID chip on a booking: kind is ok | warn | bad | wait | none."""
    check = getattr(reservation, "identity_check", None)
    if check is None:
        return {"kind": "none", "words": "No ID on file", "check": None}
    kind, words = check.badge
    return {"kind": kind, "words": words, "check": check}


# ---------------------------------------------------------------- the daily cleanup
def _last_return_day(check):
    """The day the customer's last booking on this check ended (returned, or its return date), or None when a booking on it is still open."""
    last = None
    for reservation in check.reservations.prefetch_related("items"):
        if reservation.status in OPEN_RESERVATION:
            return None
        for item in reservation.items.all():
            day = item.returned_on or item.return_date
            if day and (last is None or day > last):
                last = day
    return last


def photos_due(check, now=None):
    """True when this check's pictures should be gone: no booking on it is still open, and 30 days have passed since the last return (or, with no booking, since it was verified)."""
    now = now or timezone.now()
    last = _last_return_day(check)
    if last is None and check.reservations.filter(status__in=OPEN_RESERVATION).exists():
        return False
    anchor = timezone.make_aware(datetime.combine(last, time.min)) if last else (check.verified_at or check.created_at)
    return now - anchor >= PHOTOS_KEEP


def cleanup_photos(now=None, limit=200):
    """Delete the pictures that are due, and try again the copies that failed. Returns (deleted_checks, retried). Safe to run any number of times."""
    now = now or timezone.now()
    deleted = 0
    kept = IdentityCheck.objects.filter(photos_state__in=('stored', 'failed', 'pending', 'copying')).order_by('id')[:limit]
    for check in kept:
        try:
            if photos_due(check, now):
                identity_photos.delete_photos(check)
                deleted += 1
        except Exception:       # noqa: BLE001 -- one bad row must not stop the rest
            logger.exception("ID photos of check %s could not be cleaned up", check.pk)
    retried = 0
    for check in IdentityCheck.objects.filter(photos_state__in=('failed', 'pending'), status__in=(S.APPROVED, S.DECLINED, S.REVIEW),
                                              updated_at__gte=now - RETRY_FAILED_FOR).exclude(session_id='')[:10]:
        if identity_photos.copy_photos(check.pk):
            retried += 1
    return deleted, retried


def run_cleanup_if_due():
    """The shop has no scheduler, so the daily cleanup rides on staff visits (like the reminders): at most once an hour per server process. Never raises."""
    if not cache.add("arabela:id-photos:last-run", 1, 60 * 60):
        return None
    try:
        return cleanup_photos()
    except Exception:       # noqa: BLE001
        logger.exception("The ID photo cleanup failed")
        return None
