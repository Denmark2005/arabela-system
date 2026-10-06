"""Emails to CUSTOMERS about their own booking.

Until now every notice reached a customer only inside the website (the Messages page and the reservation
timeline), so they learned of an approval or a reminder only after opening the site. This module also emails
them -- and is deliberately a side channel that can never hurt the real work:

  * It hooks the two places notices already come from, so no existing view is rewritten:
        CustomerMessage        -> reminders, overdue, account flagged / unflagged
        ReservationStatusEvent -> "we received your reservation", approved, rejected, deposit settled
  * Nothing is sent unless settings.CUSTOMER_EMAILS_ENABLED is on (it is off by default and only the live
    Render service turns it on -- local development shares the live database).
  * Every hook swallows its own errors. A failed email must never roll back a reminder, an approval, or the
    timeline row it came from (ReservationStatusEvent.record() writes inside its caller's transaction).
  * Emails go out only AFTER the database transaction commits (a reminder that rolls back sends nothing), and
    from one background worker thread that never touches the database -- the hosting plan allows very few
    database connections, and a slow mail server must not slow an Approve click.
  * The in-site notice is always still there, whatever happens to the email.

The look lives in templates/emails/email.html (tables + inline styles, the only thing every mail app agrees on)
and the wording of each kind is built in _content() below.
"""
import logging
import queue
import re
import smtplib
import socket
import threading
import time
from datetime import timedelta
from decimal import Decimal
from email import policy as email_policy
from pathlib import Path

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe

logger = logging.getLogger("accounts.customer_emails")

# ---- the kinds of email -----------------------------------------------------------------------------------
RECEIVED = "received"
APPROVED = "approved"
REJECTED = "rejected"
DEPOSIT_SETTLED = "deposit_settled"
REMINDER = "reminder"
OVERDUE = "overdue"
ACCOUNT_FLAGGED = "account_flagged"
ACCOUNT_UNFLAGGED = "account_unflagged"

KIND_LABELS = {
    RECEIVED: "We received your reservation",
    APPROVED: "Reservation approved",
    REJECTED: "Reservation rejected",
    DEPOSIT_SETTLED: "Security deposit settled",
    REMINDER: "Pick-up / return reminder",
    OVERDUE: "Overdue notice",
    ACCOUNT_FLAGGED: "Account flagged",
    ACCOUNT_UNFLAGGED: "Account unflagged",
}

# The label each timeline entry is written with (arabela_admin/views.py, gowns/views.py). If a label is ever
# reworded the email silently stops -- which is why the tests drive the REAL views and count the emails.
EVENT_KINDS = {
    "Reservation submitted": RECEIVED,
    "Reservation approved": APPROVED,
    "Reservation rejected": REJECTED,
    "Security deposit settled": DEPOSIT_SETTLED,
}
# A staff double-click on Approve writes the same event twice; the second one is not emailed.
DUPLICATE_WINDOW = timedelta(minutes=10)

# Addresses that can never be a real customer (the demo accounts use example.invalid).
_NEVER_EMAIL_SUFFIXES = (".invalid", ".test", ".example", ".localhost")
_NEVER_EMAIL_DOMAINS = ("example.com", "example.net", "example.org")

_HEADERS = {"Auto-Submitted": "auto-generated", "X-Auto-Response-Suppress": "All"}


class BuiltEmail:
    def __init__(self, subject, text, html, inline_logo=False):
        self.subject = subject
        self.text = text
        self.html = html
        # True when the HTML points at the logo attached INSIDE the email (cid:) rather than at the website.
        self.inline_logo = inline_logo


# The logo travels inside the email itself. On Render's free plan the website falls asleep when idle, and a
# logo linked from it would not load when a customer opens the email hours later.
_LOGO_CID = "arabela-logo"
_LOGO_PATH = ("static", "images", "email", "arabela-logo-white.png")
_logo_cache = {}


def _logo_bytes():
    """The white logo PNG, or None when the file is missing (the email then links to the hosted copy instead)."""
    if "bytes" not in _logo_cache:
        try:
            _logo_cache["bytes"] = Path(settings.BASE_DIR).joinpath(*_LOGO_PATH).read_bytes()
        except OSError:
            _logo_cache["bytes"] = None
    return _logo_cache["bytes"]


# ---- small formatting helpers -------------------------------------------------------------------------------
def _first_name(full_name, fallback="there"):
    parts = (full_name or "").split()
    return parts[0] if parts else fallback


def _peso(amount):
    amount = Decimal(amount)
    return f"₱{amount:,.0f}" if amount == amount.to_integral_value() else f"₱{amount:,.2f}"


def _date_range(start, end):
    if start.year == end.year:
        return f"{start:%b} {start.day} – {end:%b} {end.day}, {end.year}"
    return f"{start:%b} {start.day}, {start.year} – {end:%b} {end.day}, {end.year}"


_BOOKING_CODE = re.compile(r"(RSV-\d{4}-\d+)")


def _html_text(text):
    """A notice as safe HTML: everything escaped, line breaks kept, and a booking code never splits across two
    lines (it would otherwise wrap at its hyphens)."""
    escaped = escape(text or "").replace("\r\n", "\n")
    escaped = _BOOKING_CODE.sub(r'<span style="white-space:nowrap;">\1</span>', escaped)
    return mark_safe(escaped.replace("\n", "<br>"))


def _paragraphs(body):
    """A notice body as separate paragraphs (blank line = new paragraph)."""
    chunks = [part.strip() for part in (body or "").replace("\r\n", "\n").split("\n\n")]
    return [part for part in chunks if part]


def site_base_url():
    """https://the-live-site (no trailing slash) -- where every link and the logo in an email point.

    SITE_BASE_URL wins when set; otherwise it comes from the Site row this deployment uses (Render's SITE_ID).
    """
    explicit = getattr(settings, "SITE_BASE_URL", "")
    if explicit:
        return explicit
    try:
        from django.contrib.sites.models import Site
        domain = Site.objects.get_current().domain.strip().strip("/")
    except Exception:
        domain = ""
    if not domain:
        return "http://127.0.0.1:8000"
    local = domain.startswith(("127.", "localhost", "0.0.0.0", "[::1]"))
    return f"{'http' if local else 'https'}://{domain}"


def shop_info():
    """The shop's contact details, as the admin's Edit Profile page has them."""
    from gowns.models import SiteSettings
    shop = SiteSettings.load()
    country = " ".join(part for part in (shop.shop_country, shop.shop_postal_code) if part)
    address = ", ".join(part for part in (shop.shop_street, shop.shop_city, country) if part)
    facebook = shop.facebook_url if (shop.facebook_url or "").startswith(("http://", "https://")) else ""
    return {"phone": shop.phone or "", "facebook_url": facebook, "address": address}


# ---- building one email -----------------------------------------------------------------------------------------
def _content(kind, data, shop):
    """What an email of this kind says. `data` keys: first_name, reference_code, items [(gown, dates)],
    deposit / total (formatted), text (a notice body), reason (a rejection reason)."""
    code = data.get("reference_code", "")
    contact = []
    if shop.get("phone"):
        contact.append(f"call {shop['phone']}")
    if shop.get("facebook_url"):
        contact.insert(0, "message us on Facebook")
    reach_us = (" or ".join(contact) + ".") if contact else "contact the shop."

    content = {
        "subject": "",
        "heading": "",
        "preheader": "",
        "paragraphs": [],
        "note": None,
        "summary": False,
        "money": False,
        "button_label": "View my reservation",
        "button_url_name": "gowns:orders",
    }
    if kind == RECEIVED:
        content.update(
            subject=f"We received your reservation {code}",
            heading="We received your reservation",
            preheader="We're checking your GCash payment. You'll get another email once it's approved.",
            paragraphs=["Thanks for booking with Arabela. We're now checking your GCash payment, and you'll get "
                        "another email as soon as your reservation is approved."],
            summary=True, money=True,
        )
    elif kind == APPROVED:
        content.update(
            subject=f"Your reservation {code} is approved",
            heading="Your reservation is approved",
            preheader="Your payment was verified and your booking is confirmed.",
            paragraphs=["Good news — your payment was verified and your booking is confirmed.",
                        "We'll send you a reminder before your pick-up date."],
            summary=True, money=True,
        )
    elif kind == REJECTED:
        content.update(
            subject=f"Update on your reservation {code}",
            heading="We couldn't approve your reservation",
            preheader="Please read the reason from the shop.",
            paragraphs=["We're sorry — we weren't able to approve this reservation.",
                        f"If you have any questions, {reach_us}"],
            note={"tone": "quiet", "title": "Reason from the shop",
                  "text": data.get("reason") or "Please contact the shop for details."},
            summary=True,
        )
    elif kind == DEPOSIT_SETTLED:
        content.update(
            subject=f"Your security deposit is settled - {code}",
            heading="Your security deposit is settled",
            preheader="Your reservation is complete. Thank you for renting with Arabela.",
            paragraphs=[data.get("text") or "Your security deposit has been settled. This reservation is complete.",
                        "Thank you for renting with Arabela — we hope to see you again."],
            summary=True,
        )
    elif kind == REMINDER:
        content.update(
            subject="A reminder from Arabela",
            heading="A reminder about your rental",
            preheader=(data.get("text") or "")[:110],
            paragraphs=_paragraphs(data.get("text")),
        )
    elif kind == OVERDUE:
        content.update(
            subject="Your Arabela gown is overdue",
            heading="Your gown is overdue",
            preheader="Please return your gown to Arabela as soon as possible.",
            note={"tone": "warning", "title": "Please return your gown", "text": data.get("text") or ""},
        )
    elif kind == ACCOUNT_FLAGGED:
        content.update(
            subject="Important notice about your Arabela account",
            heading="A notice about your account",
            preheader="Please read this message from Arabela.",
            paragraphs=_paragraphs(data.get("text")),
            button_label="Open my messages", button_url_name="gowns:messages",
        )
    elif kind == ACCOUNT_UNFLAGGED:
        content.update(
            subject="Your Arabela account is no longer flagged",
            heading="Your account is no longer flagged",
            preheader="Your account is back in good standing.",
            paragraphs=_paragraphs(data.get("text")),
            button_label="Open my messages", button_url_name="gowns:messages",
        )
    else:
        raise ValueError(f"Unknown email kind: {kind!r}")
    return content


def build_email(kind, data, *, base_url=None, shop=None, inline_logo=False):
    """A BuiltEmail (subject, plain text, HTML) for one email. Reads nothing from the database unless `base_url` /
    `shop` are not given. Everything a customer typed is escaped by the template. `inline_logo=True` makes the HTML
    use the logo attached to the email (see _make_message); the admin preview uses the hosted copy instead."""
    base_url = base_url if base_url is not None else site_base_url()
    shop = shop if shop is not None else shop_info()
    content = _content(kind, data, shop)
    inline_logo = bool(inline_logo and _logo_bytes())

    rows = []
    if content["summary"] and data.get("reference_code"):
        rows.append({"label": "Booking code", "value": data["reference_code"], "sub": ""})
        for gown, dates in data.get("items", []):
            rows.append({"label": "Gown", "value": gown, "sub": dates})
        if content["money"] and data.get("deposit"):
            rows.append({"label": "Security deposit", "value": data["deposit"], "sub": "Refundable when you return the gown"})
        if content["money"] and data.get("total"):
            rows.append({"label": "Total", "value": data["total"], "sub": ""})

    context = {
        "subject": content["subject"],
        "heading": content["heading"],
        "preheader": content["preheader"],
        "greeting": f"Hi {data.get('first_name') or 'there'},",
        "paragraphs": content["paragraphs"],                       # plain text (the .txt version)
        "paragraphs_html": [_html_text(p) for p in content["paragraphs"]],
        "note": dict(content["note"], html=_html_text(content["note"]["text"])) if content["note"] else None,
        "rows": rows,
        "button_label": content["button_label"],
        "button_url": base_url + reverse(content["button_url_name"]),
        "logo_url": f"cid:{_LOGO_CID}" if inline_logo else base_url + static("images/email/arabela-logo-white.png"),
        "home_url": base_url + "/",
        "shop": shop,
        "year": timezone.localdate().year,
    }
    return BuiltEmail(
        subject=content["subject"],
        text=render_to_string("emails/email.txt", context).strip() + "\n",
        html=render_to_string("emails/email.html", context),
        inline_logo=inline_logo,
    )


# ---- the data behind each kind ---------------------------------------------------------------------------------
def _reservation_data(reservation, text=""):
    items = [(item.gown_name, _date_range(item.rental_date, item.return_date)) for item in reservation.items.all()]
    return {
        "first_name": _first_name(reservation.customer_name),
        "reference_code": reservation.reference_code,
        "items": items,
        "deposit": _peso(reservation.security_deposit),
        "total": _peso(reservation.total_amount),
        "text": text,
    }


def _account_first_name(user):
    profile = getattr(user, "profile", None)
    return _first_name((profile.display_name if profile else "") or user.first_name)


def _deliverable_address(user):
    """The customer's email address, or "" when this account must not be emailed."""
    if user is None or user.is_staff or user.is_superuser:
        return ""
    address = (user.email or "").strip()
    if "@" not in address:
        return ""
    domain = address.rsplit("@", 1)[1].lower()
    if domain in _NEVER_EMAIL_DOMAINS or domain.endswith(_NEVER_EMAIL_SUFFIXES):
        return ""
    return address


def _mask(address):
    name, _, domain = address.partition("@")
    return f"{name[:1]}***@{domain}"


# ---- sending -------------------------------------------------------------------------------------------------------
_QUEUE = queue.Queue(maxsize=200)
_worker = None
_worker_lock = threading.Lock()
_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 4


def explain_send_error(exc):
    """A sentence the owner can act on, for the 'Send test to me' button and the logs."""
    from accounts.email_backends import EmailRelayError

    if isinstance(exc, EmailRelayError):
        return str(exc)
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ("Gmail refused the login. EMAIL_HOST_USER / EMAIL_HOST_PASSWORD must be the shop's Gmail address and "
                "a Gmail App Password (16 letters), not the normal Gmail password.")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The mail server did not accept that recipient address."
    if isinstance(exc, (smtplib.SMTPSenderRefused, smtplib.SMTPDataError)):
        return f"The mail server refused the email (it may be at its daily sending limit): {exc}"
    if isinstance(exc, (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, TimeoutError, socket.timeout,
                        ConnectionError, OSError)):
        return ("Could not reach the mail server (timed out or blocked). Render's FREE plan blocks outgoing Gmail/SMTP "
                "(ports 25, 465, 587): use the free Google relay (apps_script/mail_relay.gs, see README) or upgrade "
                "the Render service to a paid plan.")
    return f"{type(exc).__name__}: {exc}"


def _send_now(message, kind=""):
    """Send one email, retrying once. Never raises. Returns True when it went out."""
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            message.send(fail_silently=False)
            logger.info("Customer email sent: kind=%s to=%s", kind, _mask(message.to[0]))
            return True
        except Exception as exc:
            logger.warning("Customer email attempt %d/%d failed: kind=%s to=%s -- %s",
                           attempt, _ATTEMPTS, kind, _mask(message.to[0]), explain_send_error(exc))
            if attempt < _ATTEMPTS:
                time.sleep(_RETRY_DELAY_SECONDS)
    logger.error("Customer email NOT sent: kind=%s to=%s (the in-site notice is unaffected)", kind, _mask(message.to[0]))
    return False


def _worker_loop():
    while True:
        message, kind = _QUEUE.get()
        try:
            _send_now(message, kind)
        except Exception:
            logger.exception("Customer email worker error")
        finally:
            _QUEUE.task_done()


def _enqueue(message, kind):
    """Hand an email to the single background worker (or send it right here when background sending is off,
    which is how the tests keep everything in order)."""
    if not getattr(settings, "CUSTOMER_EMAILS_BACKGROUND", True):
        _send_now(message, kind)
        return
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_worker_loop, name="customer-email-worker", daemon=True)
            _worker.start()
    try:
        _QUEUE.put_nowait((message, kind))
    except queue.Full:
        logger.error("Customer email queue is full; dropped kind=%s (the in-site notice is unaffected)", kind)


class _EmailWithLogo(EmailMultiAlternatives):
    """A normal email whose HTML part carries the logo as an inline picture: the HTML and the image sit together in
    a multipart/related part, the standard way to embed an image the HTML refers to as cid:... (Django 6 no longer
    lets a message pick its own container types, so the picture is added when the message is assembled)."""

    logo_png = None

    def message(self, *, policy=email_policy.default):
        msg = super().message(policy=policy)
        if self.logo_png:
            html_part = msg.get_body(preferencelist=("html",))
            if html_part is not None:
                html_part.add_related(
                    self.logo_png, "image", "png", cid=f"<{_LOGO_CID}>", disposition="inline", filename="arabela-logo.png",
                )
        return msg


def _make_message(built, to_address, subject=None):
    """The real email (plain text + HTML, logo attached inline). Used for customer emails AND the owner's test
    button, so a test shows exactly what a customer would get."""
    message = _EmailWithLogo(
        subject=subject or built.subject, body=built.text, from_email=settings.DEFAULT_FROM_EMAIL,
        to=[to_address], headers=_HEADERS,
    )
    message.attach_alternative(built.html, "text/html")
    if built.inline_logo:
        message.logo_png = _logo_bytes()
    return message


def _deliver(user, kind, data):
    address = _deliverable_address(user)
    if not address:
        logger.info("Customer email skipped (no usable address): kind=%s", kind)
        return False
    _enqueue(_make_message(build_email(kind, data, inline_logo=True), address), kind)
    return True


# ---- the hooks ---------------------------------------------------------------------------------------------------------
def _guarded(function, *args):
    """Runs a hook; whatever goes wrong is logged and swallowed -- an email can never break the real action."""
    try:
        function(*args)
    except Exception:
        logger.exception("Customer email hook failed (the booking/notice itself is unaffected)")


def _email_for_event(event_id, kind):
    from reservations.models import ReservationStatusEvent

    event = ReservationStatusEvent.objects.select_related("reservation__customer").get(pk=event_id)
    # Only an EARLIER identical event suppresses this one. (If both a double-click's events looked at "any other
    # one", each would skip itself and the customer would get nothing.)
    if ReservationStatusEvent.objects.filter(
        reservation_id=event.reservation_id, label=event.label, pk__lt=event.pk,
        occurred_at__gte=event.occurred_at - DUPLICATE_WINDOW,
    ).exists():
        logger.info("Customer email skipped (same event just sent): kind=%s", kind)
        return
    reservation = event.reservation
    data = _reservation_data(reservation, text=event.detail if kind == DEPOSIT_SETTLED else "")
    if kind == REJECTED:
        data["reason"] = event.detail
    _deliver(reservation.customer, kind, data)


def _email_for_message(message_id, kind):
    from accounts.models import CustomerMessage

    message = CustomerMessage.objects.select_related("recipient", "recipient__profile").get(pk=message_id)
    user = message.recipient
    data = {"first_name": _account_first_name(user), "text": message.body}
    _deliver(user, kind, data)


def on_status_event_saved(sender, instance, created, **kwargs):
    if not created or not getattr(settings, "CUSTOMER_EMAILS_ENABLED", False):
        return
    kind = EVENT_KINDS.get(instance.label)
    if kind is None or instance.staff_only or instance.item_id:
        return
    pk = instance.pk
    transaction.on_commit(lambda: _guarded(_email_for_event, pk, kind))


def on_customer_message_saved(sender, instance, created, **kwargs):
    if not created or not getattr(settings, "CUSTOMER_EMAILS_ENABLED", False):
        return
    from accounts.models import CustomerMessage

    kind = {
        CustomerMessage.Category.RESERVATION_REMINDER: REMINDER,
        CustomerMessage.Category.RETURN_OVERDUE: OVERDUE,
        CustomerMessage.Category.ACCOUNT_FLAGGED: ACCOUNT_FLAGGED,
        CustomerMessage.Category.ACCOUNT_UNFLAGGED: ACCOUNT_UNFLAGGED,
    }.get(instance.category)
    if kind is None:
        return
    pk = instance.pk
    transaction.on_commit(lambda: _guarded(_email_for_message, pk, kind))


def connect_signals():
    """Called once when the accounts app starts (accounts/apps.py)."""
    from django.db.models.signals import post_save
    from accounts.models import CustomerMessage
    from reservations.models import ReservationStatusEvent

    post_save.connect(on_customer_message_saved, sender=CustomerMessage, dispatch_uid="customer_email_message")
    post_save.connect(on_status_event_saved, sender=ReservationStatusEvent, dispatch_uid="customer_email_event")


# ---- previews and the owner's test button (admin "Customer emails" page) -----------------------------------------------
def sample_data(kind):
    data = {
        "first_name": "Rainer",
        "reference_code": "RSV-2026-0054",
        "items": [("Wedding Gown 17", "Oct 13 – Oct 17, 2026"), ("Evening Gown 4", "Oct 13 – Oct 17, 2026")],
        "deposit": _peso(4000),
        "total": _peso(26500),
        "text": "",
        "reason": "We couldn't match the GCash payment to your receipt. Please message us the reference number.",
    }
    data["text"] = {
        DEPOSIT_SETTLED: "Your security deposit has been settled. This reservation is complete.",
        REMINDER: "Reminder: Wedding Gown 17 is due back tomorrow, October 17, 2026. Returning on time keeps your "
                  "security deposit fully refundable. Reference: RSV-2026-0054.",
        OVERDUE: "Wedding Gown 17 was due back on October 17, 2026 and is now 2 days overdue. Please return it to "
                 "Arabela as soon as possible -- overdue rentals may affect your security deposit refund. "
                 "Reference: RSV-2026-0054.",
        ACCOUNT_FLAGGED: "Your account has been flagged automatically after 15 cancelled or abandoned reservations. "
                         "You can still browse the collection, but our staff will review your account before your next "
                         "reservation is confirmed. If you think this is a mistake, please contact us.",
        ACCOUNT_UNFLAGGED: "Your account is no longer flagged.",
    }.get(kind, "")
    return data


def sample_email(kind):
    return build_email(kind, sample_data(kind))


def send_test_email(kind, to_address):
    """Sends a sample of `kind` to `to_address` right now (not in the background) so the owner sees the real
    result. Works whether or not CUSTOMER_EMAILS_ENABLED is on. Returns (ok, message)."""
    if kind not in KIND_LABELS:
        return False, "Unknown email type."
    if "@" not in (to_address or ""):
        return False, "Your admin account has no email address. Add one in Edit Profile, then try again."
    built = build_email(kind, sample_data(kind), inline_logo=True)
    message = _make_message(built, to_address, subject=f"[TEST] {built.subject}")
    try:
        message.send(fail_silently=False)
    except Exception as exc:
        logger.warning("Test email failed: %s", explain_send_error(exc))
        return False, explain_send_error(exc)
    return True, f"Sent to {to_address}. It usually arrives within a minute -- check Spam too."


def delivery_status():
    """What the admin page shows about how emails would go out right now."""
    backend = getattr(settings, "EMAIL_BACKEND", "")
    base_url = site_base_url()
    warnings = []
    if backend.endswith("smtp.EmailBackend"):
        method = "Gmail over SMTP"
        configured = bool(getattr(settings, "EMAIL_HOST_USER", "") and getattr(settings, "EMAIL_HOST_PASSWORD", ""))
        if configured:
            warnings.append("Render's FREE plan blocks SMTP. If this site runs on the Free plan, use the Google relay.")
    elif backend.endswith("AppsScriptRelayBackend"):
        method = "Google relay (over HTTPS)"
        configured = bool(getattr(settings, "EMAIL_RELAY_URL", "") and getattr(settings, "EMAIL_RELAY_SECRET", ""))
    else:
        method = "Test mode (no real email leaves the server)"
        configured = False
    if base_url.startswith("http://") and not base_url.startswith(("http://127.", "http://localhost")):
        warnings.append("Links in emails use http:// instead of https://.")
    if "example.com" in base_url or "127.0.0.1" in base_url or "localhost" in base_url:
        warnings.append("Links in emails point to a local/test address. On the live site this must be the real address.")
    return {
        "enabled": bool(getattr(settings, "CUSTOMER_EMAILS_ENABLED", False)),
        "method": method,
        "configured": configured,
        "from_address": settings.DEFAULT_FROM_EMAIL,
        "base_url": base_url,
        "warnings": warnings,
    }
