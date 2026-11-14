"""Admin panel sign-in with Google (owner and staff).

How it works (the owner's and the staff's Gmail replace the username and password):
  * The admin sign-in page sends the person to the SAME Google sign-in the customers use, telling it to come back to
    /admin-panel/google-finish/ ("next"). Nothing new has to be set up at Google.
  * When Google answers, accounts.adapters.ArabelaSocialAccountAdapter.pre_social_login sees that "next" and takes over BEFORE allauth
    signs anybody in on the customer site. It checks the Google-confirmed Gmail against the admin accounts: the owner (the Gmail in
    settings.ADMIN_OWNER_GMAIL, arabelaantipolo@gmail.com) and every staff account the owner added in Staff Management (their Gmail is
    the account's email). Anyone else is told they are not allowed.
  * An allowed person is handed to /admin-panel/google-finish/ with a short-lived signed link. That page is inside the admin area, so
    the admin session cookie (not the customer one) is created there, and it ends when the browser closes.
  * The owner can change her own sign-in Gmail from Edit Profile. The new Gmail is only saved after she has really signed in to it
    with Google (so a typo can never lock her out).

Nothing here stores a password: an admin account made this way has an unusable password.
"""
import logging
import re
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Case, IntegerField, Q, Value, When
from django.urls import reverse

logger = logging.getLogger(__name__)

SIGNIN_SALT = "arabela.admin-google-signin"
CHANGE_SALT = "arabela.admin-google-gmail-change"
SIGNIN_MAX_AGE = 120            # seconds the hand-over from Google to the admin panel stays valid (and it works once)
CHANGE_MAX_AGE = 15 * 60        # time the owner has to finish confirming a new sign-in Gmail
TRY_COOKIE = "arabela_admin_google"     # set while the admin sign-in page is open, so a failed Google answer comes back to it
STATE_COOKIE = "arabela_admin_google_state"     # the "state" code of the last Google answer that signed an admin in (see answer_state)
STATE_COOKIE_MAX_AGE = 12 * 60 * 60             # a working day; the sign-in itself ends when the browser closes

MESSAGES = {
    "not_allowed": "This Google account is not allowed to open the admin panel. Ask the owner to add your Gmail in Staff Management.",
    "inactive": "This admin account is switched off. Ask the owner to turn it on in Staff Management.",
    "owner_conflict": "This Gmail is already used by a customer account, so it cannot open the admin panel. Please contact the developer.",
    "owner_ambiguous": "The owner account could not be matched to this Gmail. Please contact the developer.",
    "not_verified": "Google did not confirm this email address, so it cannot be used to sign in. Please try another Google account.",
    "link_expired": "That sign-in link has expired. Please continue with Google again.",
    "change_expired": "The Gmail change took too long or was already used. Please start it again from Edit Profile.",
    "cancelled": "Google sign-in was cancelled. Please try again.",
    "incomplete": "Google sign-in did not finish. Please try once more.",
}
SIGN_IN_PROBLEMS = {"cancelled": MESSAGES["cancelled"], "incomplete": MESSAGES["incomplete"]}


def normalize_email(value):
    return (value or "").strip().lower()


GMAIL_DOMAINS = ("gmail.com", "googlemail.com")


def canonical_email(value):
    """The mailbox behind an address. Gmail ignores dots, "+tags" and googlemail.com, so rosa.staffer+shop@googlemail.com and
    rosastaffer@gmail.com are ONE Google account; every other address is just itself in lower case."""
    email = normalize_email(value)
    local, _, domain = email.partition("@")
    if domain in GMAIL_DOMAINS:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def same_mailbox(email):
    """A filter for every stored address that is the same mailbox as `email` (see canonical_email). The database does the matching, so it
    stays cheap however many customers there are. A non-Gmail address matches only itself (any capitals)."""
    email = normalize_email(email)
    local, _, domain = email.partition("@")
    base = local.split("+", 1)[0].replace(".", "") if domain in GMAIL_DOMAINS else ""
    if not base:
        return Q(email__iexact=email)
    return Q(email__iregex=r"^" + r"\.*".join(re.escape(ch) for ch in base) + r"\.*(\+[^@]*)?@(gmail|googlemail)\.com$")


def owner_gmail():
    return normalize_email(getattr(settings, "ADMIN_OWNER_GMAIL", ""))


def finish_path():
    return reverse("arabela_admin:google_finish")


def is_admin_next(next_url):
    """True when `next` (what the admin sign-in page gave to Google) is the hand-over page."""
    try:
        return urlparse(next_url or "").path == finish_path()
    except ValueError:
        return False


def answer_state(request):
    """The "state" code that came back with Google's answer, or "" when there is none or it is not a plain short code. Every Google
    sign-in carries its own random code, so the same code arriving twice means the same answer arrived twice."""
    value = (request.GET.get("state") or "").strip()
    return value if re.fullmatch(r"[A-Za-z0-9_-]{4,128}", value) else ""


def peek_signin_token(token):
    """What a hand-over link says, when its signature is genuine -- even if the link is old or was already used. None for a forged one."""
    try:
        return signing.loads(token or "", salt=SIGNIN_SALT)
    except signing.BadSignature:
        return None


def change_token_from_next(next_url):
    values = parse_qs(urlparse(next_url or "").query).get("c") or []
    return values[0] if values else ""


def google_identity(sociallogin):
    """(lower-case email, confirmed-by-Google flag) of the Google account that just answered."""
    email, verified = "", False
    for address in getattr(sociallogin, "email_addresses", None) or []:
        if address.primary or not email:
            email, verified = normalize_email(address.email), bool(address.verified)
    if not email:
        extra = getattr(getattr(sociallogin, "account", None), "extra_data", None) or {}
        email = normalize_email(extra.get("email"))
        verified = bool(extra.get("email_verified") or extra.get("verified_email"))
    return email, verified


def is_admin_account(user):
    return bool(user is not None and user.is_active and (user.is_staff or user.is_superuser))


def _bind_owner(email):
    """The owner signs in with settings.ADMIN_OWNER_GMAIL. The first time, that Gmail is attached to the one existing owner account
    (the superuser). It refuses when it is not clear which account is the owner, or the Gmail already belongs to a customer."""
    User = get_user_model()
    owners = User.objects.filter(is_superuser=True, is_active=True)
    if owners.count() != 1:
        return None, "owner_ambiguous"
    if User.objects.filter(same_mailbox(email), is_staff=False, is_superuser=False).exists():
        return None, "owner_conflict"
    owner = owners.first()
    owner.email = email
    owner.save(update_fields=["email"])
    return owner, ""


def resolve_admin_user(email):
    """(admin account, "") for a Gmail that may open the admin panel, else (None, reason key of MESSAGES)."""
    email = normalize_email(email)
    if not email:
        return None, "not_allowed"
    User = get_user_model()
    admins = User.objects.filter(same_mailbox(email)).filter(Q(is_staff=True) | Q(is_superuser=True))
    # The account whose address is exactly what Google sent comes first; a dotted / "+tag" spelling of the same mailbox still opens it.
    exact_first = Case(When(email__iexact=email, then=Value(0)), default=Value(1), output_field=IntegerField())
    active = admins.filter(is_active=True).annotate(exact=exact_first).order_by("exact", "-is_superuser", "pk").first()
    if active is not None:
        return active, ""
    if admins.exists():
        return None, "inactive"
    if owner_gmail() and canonical_email(email) == canonical_email(owner_gmail()):
        return _bind_owner(email)
    return None, "not_allowed"


def gmail_problem(email, exclude_pk=None, allow_owner_gmail=False):
    """Why this Gmail cannot be given to an admin/staff account, or "" when it can. A Gmail that is already a customer account is
    refused (the person must use a different Gmail), and so is one that already belongs to another admin account."""
    email = normalize_email(email)
    if not email:
        return "Enter the Gmail address."
    try:
        validate_email(email)
    except ValidationError:
        return "Enter a valid Gmail address, for example name@gmail.com."
    if len(email) > 254:
        return "That email address is too long."
    if not allow_owner_gmail and owner_gmail() and canonical_email(email) == canonical_email(owner_gmail()):
        return "That Gmail is the owner's own sign-in Gmail, so it cannot be given to a staff account."
    clash = get_user_model().objects.filter(same_mailbox(email))
    if exclude_pk is not None:
        clash = clash.exclude(pk=exclude_pk)
    if clash.filter(Q(is_staff=True) | Q(is_superuser=True)).exists():
        return "That Gmail already belongs to another admin account."
    if clash.exists():
        return "That Gmail is already used by a customer account. Please use a different Gmail."
    return ""


def _stamp(user):
    return user.last_login.isoformat() if user.last_login else ""


def make_signin_token(user, change_to=""):
    return signing.dumps({"u": user.pk, "e": normalize_email(user.email), "l": _stamp(user), "c": normalize_email(change_to)}, salt=SIGNIN_SALT)


def read_signin_token(token):
    """(user, new Gmail or "") for a valid, unused hand-over link; (None, "") otherwise. Signing in changes last_login, so a link
    works only once."""
    try:
        data = signing.loads(token or "", salt=SIGNIN_SALT, max_age=SIGNIN_MAX_AGE)
    except signing.BadSignature:
        return None, ""
    user = get_user_model().objects.filter(pk=data.get("u")).first()
    if not is_admin_account(user):
        return None, ""
    if normalize_email(user.email) != data.get("e") or _stamp(user) != data.get("l"):
        return None, ""
    return user, data.get("c") or ""


def is_owner_account(user):
    if not is_admin_account(user):
        return False
    if user.is_superuser:
        return True
    from accounts.models import UserProfile
    profile = UserProfile.objects.filter(user=user).first()
    return bool(profile and profile.role == UserProfile.Role.OWNER)


def make_change_token(user, new_email):
    return signing.dumps({"u": user.pk, "n": normalize_email(new_email)}, salt=CHANGE_SALT)


def read_change_token(token):
    """(owner account, new Gmail) for a valid request to change the owner's own sign-in Gmail, else (None, "")."""
    try:
        data = signing.loads(token or "", salt=CHANGE_SALT, max_age=CHANGE_MAX_AGE)
    except signing.BadSignature:
        return None, ""
    user = get_user_model().objects.filter(pk=data.get("u")).first()
    if not is_owner_account(user):
        return None, ""
    return user, data.get("n") or ""


def google_login_url(request, change_token=""):
    """The Google sign-in address that comes back to the admin hand-over page (with the Gmail-change request when there is one)."""
    from allauth.socialaccount.adapter import get_adapter
    next_url = finish_path() + (f"?c={change_token}" if change_token else "")
    provider = get_adapter().get_provider(request, "google")
    return provider.get_login_url(request, process="login", next=next_url)


def sign_in_context(request, error=""):
    """What the admin sign-in page needs: the Google address for its button, and the message to show. If the Google app is not set up
    (no SocialApp row), the page still opens and says so, instead of crashing."""
    try:
        return {"google_url": google_login_url(request), "error": error}
    except Exception:       # noqa: BLE001 -- SocialApp.DoesNotExist and friends
        logger.warning("Admin sign-in page: the Google sign-in is not set up", exc_info=True)
        return {"google_url": "", "error": error or "Google sign-in is not set up yet. Please contact the developer."}


_SAFE_USERNAME = re.compile(r"[^a-z0-9._-]+")


def unique_username(email):
    """A free username for a Gmail-only account (Django needs one, but nobody types it: the account signs in with Google)."""
    User = get_user_model()
    base = _SAFE_USERNAME.sub("", normalize_email(email).split("@")[0])[:20] or "staff"
    candidate, n = base, 1
    while User.objects.filter(username__iexact=candidate).exists():
        n += 1
        candidate = f"{base}{n}"
    return candidate
