"""The PayMongo connection: hosted checkout sessions, refunds and webhook signatures. Nothing else in the project talks to PayMongo.

Everything here follows PayMongo's own documentation (docs.paymongo.com):
  * HTTP Basic: the SECRET key is the user name, the password is empty. JSON bodies look like {"data": {"attributes": {...}}}.
  * Money is whole CENTAVOS (PHP 2,000.00 = 200000), PHP only.
  * A checkout session is created with POST /v2/checkout_sessions, read with GET /v1/checkout_sessions/{id} and expired with
    POST /v1/checkout_sessions/{id}/expire (a session never expires on its own). A refund is POST /v1/refunds.
  * A webhook carries `Paymongo-Signature: t=<time>,te=<test signature>,li=<live signature>`; the signature is the hex HMAC-SHA256 of
    "<t>.<raw body>" made with the webhook's own secret.
Where the documentation is silent (it does not say whether the read path differs for a v2 session, nor which refund path polls), the code tries
the other documented version only on a 404 and `manage.py paymongo_check` shows which one PayMongo really answers.

Nothing in the test suite reaches the network: every test patches `_request`."""
import base64
import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

CURRENCY = "PHP"
REFUND_REASON = "others"            # valid in both lists the documentation gives (the details go in the note)
MAX_NOTE = 255                      # PayMongo's own limit for a refund note
SIGNATURE_HEADER = "Paymongo-Signature"

# The names PayMongo reports for how a customer paid -> what the shop's own pages call it.
CHANNEL_LABELS = {"gcash": "GCash", "paymaya": "Maya", "qrph": "QR Ph"}


class PaymongoError(Exception):
    """PayMongo refused, could not be reached, or answered something unexpected. `retryable` is True for a dropped connection or a busy PayMongo."""

    def __init__(self, message, *, status=None, retryable=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


# ---------------------------------------------------------------- switches
def secret_key():
    return (getattr(settings, "PAYMONGO_SECRET_KEY", "") or "").strip()


def mode():
    """'test' for an sk_test_ key (no real money), 'live' for an sk_live_ key, '' when no key is set."""
    key = secret_key()
    if not key:
        return ""
    return "live" if key.startswith("sk_live_") else "test"


def is_test():
    return mode() == "test"


def enabled():
    """True when the checkout should use PayMongo: a key is set, and a LIVE key is never used against a laptop copy of the database."""
    key = secret_key()
    if not key:
        return False
    if key.startswith("sk_live_") and getattr(settings, "USING_LAPTOP_DB", False):
        return False
    return True


def methods():
    """The payment methods the page offers, in PayMongo's names (settings.PAYMONGO_METHODS)."""
    return [m for m in (getattr(settings, "PAYMONGO_METHODS", None) or []) if m] or ["gcash", "paymaya", "qrph"]


# ---------------------------------------------------------------- money
def to_centavos(amount):
    """PHP 2,000.00 -> 200000. Exact: an amount with more than two decimals is rounded half up (ours never have)."""
    return int((Decimal(str(amount)) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def from_centavos(value):
    return (Decimal(int(value)) / 100).quantize(Decimal("0.01"))


# ---------------------------------------------------------------- the HTTP call
def _auth_header():
    token = base64.b64encode(f"{secret_key()}:".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _error_text(response):
    """A short, safe sentence for a failed call (PayMongo's own `errors[].detail` when it sent one). Never contains a key."""
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("errors"), list):
        parts = [str(e.get("detail") or e.get("code") or "") for e in data["errors"] if isinstance(e, dict)]
        text = "; ".join(p for p in parts if p)
        if text:
            return text[:300]
    return f"PayMongo answered {response.status_code}."


def _request(method, path, body=None):
    """One call to PayMongo; the parsed JSON (an empty dict for an empty answer). Raises PaymongoError."""
    if getattr(settings, "TESTING", False):
        raise PaymongoError("The test suite never reaches PayMongo: patch reservations.paymongo._request.")
    if not secret_key():
        raise PaymongoError("PayMongo is not set up (no secret key).")
    url = f"{settings.PAYMONGO_API_BASE}{path}"
    try:
        response = requests.request(
            method, url, json=body, timeout=getattr(settings, "PAYMONGO_TIMEOUT", 15),
            headers={"Authorization": _auth_header(), "Accept": "application/json", "Content-Type": "application/json"},
        )
    except requests.RequestException as exc:
        logger.warning("PayMongo %s %s could not be reached: %s", method, path, type(exc).__name__)
        raise PaymongoError("PayMongo could not be reached.", retryable=True) from exc
    if response.status_code >= 400:
        retryable = response.status_code in (408, 429) or response.status_code >= 500
        raise PaymongoError(_error_text(response), status=response.status_code, retryable=retryable)
    if not response.content:
        return {}
    try:
        data = response.json()
    except ValueError as exc:
        raise PaymongoError("PayMongo sent an answer that is not JSON.", status=response.status_code, retryable=True) from exc
    return data if isinstance(data, dict) else {}


def _data(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise PaymongoError("PayMongo's answer had no data.")
    return data


def _attributes(data):
    attributes = data.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


# ---------------------------------------------------------------- checkout sessions
@dataclass
class Session:
    id: str
    checkout_url: str
    raw: dict = field(default_factory=dict)


@dataclass
class SessionState:
    """What a checkout session says right now."""
    id: str
    status: str = ""                 # "active" | "expired"
    paid: bool = False
    payment_id: str = ""             # pay_... of the paid payment
    amount: Decimal = Decimal("0")   # what was paid, in pesos
    channel: str = ""                # gcash | paymaya | qrph | ...
    paid_at: int = 0
    reference_number: str = ""
    metadata: dict = field(default_factory=dict)
    livemode: bool = False
    raw: dict = field(default_factory=dict)


def _safe_checkout_url(url):
    """The page PayMongo sent the customer to must be a secure PayMongo page (a plain-http address is accepted only for the local stand-in the browser tests use)."""
    url = (url or "").strip()
    if url.startswith("https://"):
        return url
    base = getattr(settings, "PAYMONGO_API_BASE", "")
    if url.startswith("http://127.0.0.1") or url.startswith("http://localhost"):
        if base.startswith("http://127.0.0.1") or base.startswith("http://localhost"):
            return url
    raise PaymongoError("PayMongo sent a payment page address that is not secure.")


def create_checkout_session(*, amount, name, description, reference_number, success_url, cancel_url, metadata=None, customer_name="", customer_email=""):
    """Open a hosted checkout for exactly `amount` pesos (one line item, the shop absorbs PayMongo's fee). -> Session."""
    centavos = to_centavos(amount)
    if centavos < 100:
        raise PaymongoError("The amount is too small to pay online.")
    attributes = {
        "line_items": [{"name": name[:120], "amount": centavos, "currency": CURRENCY, "quantity": 1, "description": description[:200]}],
        "payment_method_types": methods(),
        "description": description[:200],
        "reference_number": reference_number,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "send_email_receipt": False,
        "show_description": True,
        "show_line_items": True,
        "metadata": {str(k): str(v) for k, v in (metadata or {}).items()},
    }
    billing = {}
    if customer_name:
        billing["name"] = customer_name[:120]
    if customer_email and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", customer_email):
        billing["email"] = customer_email
    if billing:
        attributes["billing"] = billing
    payload = _request("POST", "/v2/checkout_sessions", {"data": {"attributes": attributes}})
    data = _data(payload)
    session_id = str(data.get("id") or "")
    url = _safe_checkout_url(_attributes(data).get("checkout_url"))
    if not session_id.startswith("cs_"):
        raise PaymongoError("PayMongo's answer had no checkout session id.")
    return Session(id=session_id, checkout_url=url, raw=payload)


def parse_session(payload):
    """A session (a retrieved one, or the resource inside a webhook event) -> SessionState. Only a payment whose status is "paid" counts."""
    data = payload if "attributes" in payload and "data" not in payload else _data(payload)
    attributes = _attributes(data)
    state = SessionState(
        id=str(data.get("id") or ""), status=str(attributes.get("status") or ""), reference_number=str(attributes.get("reference_number") or ""),
        metadata=attributes.get("metadata") if isinstance(attributes.get("metadata"), dict) else {},
        livemode=bool(attributes.get("livemode")), raw=payload,
    )
    for payment in attributes.get("payments") or []:
        if not isinstance(payment, dict):
            continue
        pay_attributes = _attributes(payment)
        if str(pay_attributes.get("status") or "") != "paid":
            continue
        source = pay_attributes.get("source") if isinstance(pay_attributes.get("source"), dict) else {}
        state.paid = True
        state.payment_id = str(payment.get("id") or "")
        state.amount = from_centavos(pay_attributes.get("amount") or 0)
        state.channel = str(source.get("type") or "")
        state.paid_at = int(pay_attributes.get("paid_at") or attributes.get("paid_at") or 0)
        break                                           # the documentation: at most one payment of a session is paid
    return state


def retrieve_checkout_session(session_id):
    """Read a session from PayMongo (the authority on whether it was paid). -> SessionState."""
    try:
        payload = _request("GET", f"/v1/checkout_sessions/{session_id}")
    except PaymongoError as exc:
        if exc.status != 404:
            raise
        payload = _request("GET", f"/v2/checkout_sessions/{session_id}")        # the documentation does not say which version reads a v2 session
    return parse_session(payload)


def expire_checkout_session(session_id):
    """Close a session so it can no longer be paid. -> "expired" | "already_expired" | "has_payment" (it was paid, or a payment is still being
    processed: leave it and read it again) | "error" (PayMongo refused for another reason, or could not be reached)."""
    try:
        _request("POST", f"/v1/checkout_sessions/{session_id}/expire")
        return "expired"
    except PaymongoError as exc:
        message = str(exc).lower()
        if exc.status == 400 and "already expired" in message:
            return "already_expired"
        if exc.status == 400 and ("paid" in message or "pending" in message or "processing" in message):
            return "has_payment"
        logger.warning("PayMongo would not expire %s: %s", session_id, exc)
        return "error"


# ---------------------------------------------------------------- refunds
@dataclass
class Refund:
    id: str
    status: str = ""                 # pending | processing | succeeded | failed
    amount: Decimal = Decimal("0")
    payment_id: str = ""
    raw: dict = field(default_factory=dict)


def parse_refund(payload):
    data = payload if "attributes" in payload and "data" not in payload else _data(payload)
    attributes = _attributes(data)
    return Refund(id=str(data.get("id") or ""), status=str(attributes.get("status") or ""), amount=from_centavos(attributes.get("amount") or 0),
                  payment_id=str(attributes.get("payment_id") or ""), raw=payload)


def create_refund(*, payment_id, amount, note=""):
    """Ask PayMongo to send `amount` pesos of a paid payment back to the customer (a partial amount is allowed). -> Refund (status usually "pending")."""
    centavos = to_centavos(amount)
    if centavos < 100:
        raise PaymongoError("The amount is too small to refund online.")
    attributes = {"payment_id": payment_id, "amount": centavos, "reason": REFUND_REASON, "notes": (note or "")[:MAX_NOTE]}
    refund = parse_refund(_request("POST", "/v1/refunds", {"data": {"attributes": attributes}}))
    if not refund.id:
        raise PaymongoError("PayMongo's answer had no refund id.")
    return refund


def retrieve_refund(refund_id):
    try:
        payload = _request("GET", f"/v1/refunds/{refund_id}")
    except PaymongoError as exc:
        if exc.status != 404:
            raise
        payload = _request("GET", f"/refunds/{refund_id}")                       # the documentation lists this path without the version
    return parse_refund(payload)


# ---------------------------------------------------------------- webhooks
def _signature_parts(header):
    parts = {}
    for piece in (header or "").split(","):
        name, _, value = piece.strip().partition("=")
        if name:
            parts[name.strip()] = value.strip()
    return parts


def verify_webhook(raw_body, header, secret):
    """The event (a dict) when `header` is a genuine signature of `raw_body` made with `secret`, else None.

    The signed text is "<t>.<raw body>" exactly as received (never a re-serialised copy), the HMAC is SHA-256, and `te` signs a TEST event while
    `li` signs a LIVE one -- an event is believed only when the signature of ITS OWN mode matches, so a test event can never pass as a live one."""
    if not secret or not header or not isinstance(raw_body, (bytes, bytearray)):
        return None
    parts = _signature_parts(header)
    timestamp = parts.get("t", "")
    if not timestamp:
        return None
    try:
        event = json.loads(bytes(raw_body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    attributes = _attributes(event.get("data") if isinstance(event, dict) and isinstance(event.get("data"), dict) else {})
    livemode = bool(attributes.get("livemode"))
    sent = parts.get("li" if livemode else "te", "")
    if not sent:
        return None
    expected = hmac.new(secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + bytes(raw_body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sent):
        return None
    return event


def event_type(event):
    """'checkout_session.payment.paid', 'payment.refund.updated', ... ('' when the event has no type)."""
    attributes = _attributes(event.get("data") or {}) if isinstance(event, dict) else {}
    return str(attributes.get("type") or "")


def event_resource(event):
    """The resource inside an event (the checkout session, the refund, ...): {"id": ..., "attributes": {...}}, or {}."""
    attributes = _attributes(event.get("data") or {}) if isinstance(event, dict) else {}
    resource = attributes.get("data")
    return resource if isinstance(resource, dict) else {}


def event_livemode(event):
    attributes = _attributes(event.get("data") or {}) if isinstance(event, dict) else {}
    return bool(attributes.get("livemode"))
