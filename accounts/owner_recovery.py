"""Forgot-password for the shop OWNER only -- how it stays safe.

The owner can set a RECOVERY EMAIL in Account Settings. It is a separate address from the profile's contact email, because anything
that can be typed into a profile box without proof must never decide who may reset the admin password:

  * Setting or changing it needs the current password AND a 6-digit code mailed to the NEW address. The address is saved only when
    the code is typed back, so a mistyped or made-up address can never become the recovery address, and the old one stays active
    until the new one is proven. (The pending code lives in the owner's own session, as a keyed hash -- never in plain text and
    never in a table.)
  * "Forgot password?" asks for the owner's USERNAME, never an email address, and always answers the same thing, whether or not the
    username exists or has a recovery email. It mails a link only for an active owner account with a confirmed recovery email --
    never staff, never customers (a customer account may even share the owner's Gmail), and nobody is looked up by email.
  * The link works once, for 30 minutes, and dies when the password, the last sign-in or the recovery email changes.
  * Everything is rate limited (accounts.rate_limit, kept in the database), and the code and link emails reuse the shop's one email
    design (templates/emails/email.html).

The recovery email lives in accounts.OwnerRecovery. Until that table exists (the code is live before its migration has run), every
function here answers "not set up" instead of failing, so signing in and the admin panel never break."""
import hashlib
import hmac
import logging
import secrets
import threading
import time

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import DatabaseError
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import base36_to_int, urlsafe_base64_decode, urlsafe_base64_encode

from . import customer_emails as emails
from . import login_throttle, rate_limit
from .models import OwnerRecovery, UserProfile

logger = logging.getLogger("accounts.owner_recovery")
User = get_user_model()

CODE_DIGITS = 6
CODE_TTL_SECONDS = 15 * 60          # how long a confirmation code works
CODE_MAX_TRIES = 5                  # wrong codes before it is thrown away
SEND_COOLDOWN_SECONDS = 30          # between two "send me a code" clicks
SENDS_PER_HOUR = 5                  # code emails per owner per hour
RESET_LINK_TTL_SECONDS = 30 * 60    # how long a reset link works
FORGOT_PER_USERNAME = (3, 60 * 60)  # reset requests per username per hour
FORGOT_PER_IP = (10, 60 * 60)       # reset requests per connection per hour
RESET_PAGE_PER_IP = (60, 60 * 60)   # opens/submits of the reset page per connection per hour
SESSION_KEY = "owner_recovery_pending"

_SEND_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 4


def _now_ts():
    """The clock for code expiry and cooldowns -- one place, so tests can move it."""
    return time.time()


def _sleep(seconds):
    time.sleep(seconds)

KIND_CODE = "code"
KIND_RESET = "reset"
KIND_PASSWORD_CHANGED = "password_changed"
KIND_RECOVERY_CHANGED = "recovery_changed"


# ---- who is the owner ----------------------------------------------------------------------------------------------------
def is_owner(user):
    """The same rule as the admin panel's _is_owner(): an active superuser, or an active admin account whose profile role is Owner."""
    if user is None or not user.is_active or not (user.is_staff or user.is_superuser):
        return False
    if user.is_superuser:
        return True
    profile = UserProfile.objects.filter(user=user).first()
    return bool(profile and profile.role == UserProfile.Role.OWNER)


# ---- the recovery email -------------------------------------------------------------------------------------------------
def normalize_email(raw):
    """The address in lower case, or None when it cannot be one the shop could mail (wrong shape, too long, or a placeholder)."""
    address = (raw or "").strip().lower()
    if not address or len(address) > 254 or any(ch.isspace() for ch in address):
        return None
    try:
        validate_email(address)
    except ValidationError:
        return None
    domain = address.rsplit("@", 1)[1]
    if domain in emails._NEVER_EMAIL_DOMAINS or domain.endswith(emails._NEVER_EMAIL_SUFFIXES):
        return None
    return address


def get_recovery(user):
    """The owner's confirmed recovery email row, or None (also None while the table does not exist yet)."""
    if user is None or not getattr(user, "pk", None):
        return None
    try:
        return OwnerRecovery.objects.filter(user=user).first()
    except DatabaseError:
        logger.warning("Recovery email lookup failed (the database table may not exist yet).")
        return None


def mask(address):
    """'ar***@gmail.com' -- what the pages show instead of the full address."""
    return emails._mask(address)


def status(user):
    """What Account Settings shows: {'available': bool, 'verified': bool, 'email_masked': str}."""
    try:
        row = OwnerRecovery.objects.filter(user=user).first()
    except DatabaseError:
        return {"available": False, "verified": False, "email_masked": ""}
    return {"available": True, "verified": row is not None, "email_masked": mask(row.email) if row else ""}


# ---- the 6-digit code ---------------------------------------------------------------------------------------------------
def _new_code():
    return f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"


def _hash_code(user, address, code):
    """A keyed hash of the code, bound to this owner and this address -- so a code for one address can never confirm another."""
    message = f"owner-recovery:{user.pk}:{address}:{code}".encode()
    return hmac.new(force_bytes(settings.SECRET_KEY), message, hashlib.sha256).hexdigest()


def _password_key(user):
    return f"recov:{user.pk}"


def password_lock_seconds(user):
    """Seconds left before the owner may type the current password again on the recovery card (0 = not locked). The same rule as the
    sign-in page -- 5 wrong tries lock it for 15 minutes -- but its own count, so it never locks the sign-in page itself."""
    return login_throttle.seconds_locked(_password_key(user))


def password_failed(user):
    login_throttle.record_failure(_password_key(user))


def password_ok(user):
    login_throttle.clear(_password_key(user))


def lock_message(seconds):
    minutes = max(1, -(-int(seconds) // 60))
    return f"Too many wrong passwords. Please try again in {minutes} minute{'s' if minutes != 1 else ''}."


def begin_verification(request, user, new_address):
    """Mail a confirmation code to `new_address`. Returns (outcome, plain-words message); outcome is 'sent', 'wait' (too soon),
    'limit' (too many this hour) or 'failed' (the email could not be sent).

    Nothing is saved except the pending code in the owner's session; the recovery email only changes in confirm_code()."""
    now = _now_ts()
    pending = request.session.get(SESSION_KEY) or {}
    if now - float(pending.get("sent_at") or 0) < SEND_COOLDOWN_SECONDS:
        return "wait", "Please wait a few seconds before asking for another code."
    if not rate_limit.allowed(f"recov-send:{user.pk}", SENDS_PER_HOUR, 60 * 60):
        return "limit", "Too many codes were sent. Please try again in an hour."
    code = _new_code()
    built = build_email(KIND_CODE, first_name=_first_name(user), code=code)
    ok, reason = send_now(built, new_address)
    if not ok:
        request.session.pop(SESSION_KEY, None)
        return "failed", ("The code email could not be sent, so nothing was changed. " + reason).strip()
    request.session[SESSION_KEY] = {
        "email": new_address, "hash": _hash_code(user, new_address, code), "expires": now + CODE_TTL_SECONDS,
        "tries": 0, "sent_at": now,
    }
    return "sent", ""


def confirm_code(request, user, typed):
    """Check the code the owner typed. Returns (True, '') after saving the recovery email, else (False, plain-words reason)."""
    pending = request.session.get(SESSION_KEY)
    if not pending:
        return False, "Ask for a new code first."
    if _now_ts() > float(pending.get("expires") or 0):
        request.session.pop(SESSION_KEY, None)
        return False, "That code has expired. Ask for a new one."
    if not rate_limit.allowed(f"recov-verify:{user.pk}", 20, 60 * 60):
        return False, "Too many tries. Please try again in an hour."
    pending["tries"] = int(pending.get("tries") or 0) + 1
    request.session[SESSION_KEY] = pending
    address = pending["email"]
    typed = "".join(ch for ch in (typed or "") if ch.isdigit())
    if not hmac.compare_digest(_hash_code(user, address, typed), str(pending.get("hash") or "")):
        if pending["tries"] >= CODE_MAX_TRIES:
            request.session.pop(SESSION_KEY, None)
            return False, "That code is not right, and you have used all your tries. Ask for a new code."
        return False, f"That code is not right. {CODE_MAX_TRIES - pending['tries']} tries left."
    try:
        old = OwnerRecovery.objects.filter(user=user).first()
        old_address = old.email if old else ""
        OwnerRecovery.objects.update_or_create(user=user, defaults={"email": address, "verified_at": timezone.now()})
    except DatabaseError:
        logger.exception("Could not save the recovery email.")
        return False, "Could not save your recovery email. Please try again."
    request.session.pop(SESSION_KEY, None)
    if old_address and old_address != address:
        send_in_background(build_email(KIND_RECOVERY_CHANGED, first_name=_first_name(user), masked=mask(address)), old_address)
    return True, ""


# ---- the reset link -----------------------------------------------------------------------------------------------------
class _ResetTokenGenerator(PasswordResetTokenGenerator):
    """Django's one-time token, tied to the password, the last sign-in AND the recovery email, and good for 30 minutes."""

    key_salt = "accounts.owner_recovery.reset"

    def _make_hash_value(self, user, timestamp):
        login_stamp = "" if user.last_login is None else user.last_login.replace(microsecond=0, tzinfo=None)
        recovery = get_recovery(user)
        return f"{user.pk}{user.password}{login_stamp}{timestamp}{recovery.email if recovery else ''}"

    def check_token(self, user, token):
        if not user or not token or not super().check_token(user, token):
            return False
        try:
            issued = base36_to_int(token.split("-", 1)[0])
        except ValueError:
            return False
        return (self._num_seconds(self._now()) - issued) <= RESET_LINK_TTL_SECONDS


token_generator = _ResetTokenGenerator()


def make_reset_link(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    path = reverse("arabela_admin:reset_password", kwargs={"uidb64": uid, "token": token_generator.make_token(user)})
    return emails.site_base_url() + path


def user_for_reset(uidb64, token):
    """The owner this link belongs to, or None when the link is wrong, expired, already used, or not an owner's."""
    try:
        user = User.objects.filter(pk=force_str(urlsafe_base64_decode(uidb64))).first()
    except (TypeError, ValueError, OverflowError, UnicodeDecodeError):
        return None
    if user is None or not is_owner(user) or get_recovery(user) is None:
        return None
    return user if token_generator.check_token(user, token) else None


def request_reset(username, ip):
    """The 'Forgot password?' form. ALWAYS returns None: the page says the same thing whatever happens here."""
    username = (username or "").strip()[:150]
    if not username:
        return
    if not rate_limit.allowed(f"forgot-ip:{ip}", *FORGOT_PER_IP):
        return
    if not rate_limit.allowed(f"forgot-user:{username.lower()}", *FORGOT_PER_USERNAME):
        return
    matches = list(User.objects.filter(username__iexact=username, is_active=True).order_by("pk")[:5])
    user = next((u for u in matches if u.username == username), matches[0] if matches else None)
    if user is None or not is_owner(user):
        return
    recovery = get_recovery(user)
    if recovery is None:
        return
    built = build_email(KIND_RESET, first_name=_first_name(user), link=make_reset_link(user))
    send_in_background(built, recovery.email)


def notify_password_changed(user):
    """'Your password was changed' to the recovery address (after a reset)."""
    recovery = get_recovery(user)
    if recovery is not None:
        send_in_background(build_email(KIND_PASSWORD_CHANGED, first_name=_first_name(user)), recovery.email)


# ---- the emails ---------------------------------------------------------------------------------------------------------
def _first_name(user):
    return emails._first_name(user.first_name or "", fallback="there")


def build_email(kind, *, first_name="there", code="", link="", masked=""):
    """One of the four owner emails in the shop's usual email design (see customer_emails.render_email)."""
    minutes = CODE_TTL_SECONDS // 60
    content = {
        "subject": "", "heading": "", "preheader": "", "paragraphs": [], "note": None, "summary": False, "money": False,
        "button_label": "Open admin sign-in", "button_url_name": "arabela_admin:admin_login",
    }
    if kind == KIND_CODE:
        content.update(
            subject="Your Arabela recovery email code",
            heading="Confirm your recovery email",
            preheader=f"Your code is {code}. It works for {minutes} minutes.",
            paragraphs=["Type this code in Account Settings to confirm that this address can receive messages for the Arabela admin account. "
                        f"It works for {minutes} minutes."],
            note={"tone": "quiet", "title": "Didn't ask for this?",
                  "text": "Ignore this email. Nothing changes unless the code is typed in Account Settings."},
            highlight={"label": "Your confirmation code", "value": code},
            button_label="Open Account Settings", button_path=reverse("arabela_admin:page", kwargs={"page": "account-settings"}),
        )
    elif kind == KIND_RESET:
        content.update(
            subject="Reset your Arabela admin password",
            heading="Reset your admin password",
            preheader="Use the button to choose a new password. The link works once, for 30 minutes.",
            paragraphs=["We got a request to reset the password of the Arabela admin account. Use the button below to choose a new one. "
                        f"The link works once and expires in {RESET_LINK_TTL_SECONDS // 60} minutes."],
            note={"tone": "quiet", "title": "Didn't ask for this?",
                  "text": "Ignore this email. Your password stays the same unless the link is used."},
            button_label="Choose a new password", button_url=link,
        )
    elif kind == KIND_PASSWORD_CHANGED:
        content.update(
            subject="Your Arabela admin password was changed",
            heading="Your admin password was changed",
            preheader="If this was not you, act now.",
            paragraphs=["The password of the Arabela admin account was just changed, and every device that was signed in was signed out."],
            note={"tone": "warning", "title": "Was this not you?",
                  "text": "Tell the developer right away so the account can be secured."},
        )
    elif kind == KIND_RECOVERY_CHANGED:
        content.update(
            subject="Your Arabela recovery email was changed",
            heading="Your recovery email was changed",
            preheader="This address is no longer the recovery email for the Arabela admin account.",
            paragraphs=[f"The recovery email of the Arabela admin account was changed to {masked}. "
                        "This address can no longer be used to reset the password."],
            note={"tone": "warning", "title": "Was this not you?",
                  "text": "Tell the developer right away so the account can be secured."},
        )
    else:
        raise ValueError(f"Unknown owner email kind: {kind!r}")
    return emails.render_email(content, {"first_name": first_name}, inline_logo=True)


# ---- sending ------------------------------------------------------------------------------------------------------------
def _send_with_retry(message):
    """Send one email, once more after a short wait if it fails. Returns (ok, plain-words reason)."""
    reason = ""
    for attempt in range(1, _SEND_ATTEMPTS + 1):
        try:
            message.send(fail_silently=False)
            return True, ""
        except Exception as exc:                     # noqa: BLE001 -- every mail error is reported, never raised
            reason = emails.explain_send_error(exc)
            logger.warning("Owner recovery email attempt %d/%d failed: %s", attempt, _SEND_ATTEMPTS, reason)
            if attempt < _SEND_ATTEMPTS:
                _sleep(_RETRY_DELAY_SECONDS)
    return False, reason


def send_now(built, to_address):
    """Send right here and say how it went -- for the code email, where the owner is waiting and must be told if it failed."""
    return _send_with_retry(emails._make_message(built, to_address))


def send_in_background(built, to_address):
    """Send from a background thread (no database use), so the page answers at the same speed whether or not an email goes out."""
    message = emails._make_message(built, to_address)
    if not getattr(settings, "CUSTOMER_EMAILS_BACKGROUND", True):
        _send_with_retry(message)
        return
    threading.Thread(target=_send_with_retry, args=(message,), name="owner-recovery-email", daemon=True).start()
