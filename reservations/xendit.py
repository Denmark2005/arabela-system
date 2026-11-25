"""The Xendit connection: hosted payment sessions, refunds and webhooks. It has the SAME functions as reservations/paymongo.py (so the checkout code does not
care which one it talks to: see reservations/gateway.py) and nothing else in the project talks to Xendit.

Everything here was checked with real calls in Xendit's TEST mode (2026-11-24; see the memory note reference_xendit_api_verified):
  * HTTP Basic: the SECRET key is the user name, the password is empty. Base https://api.xendit.co.
  * Money is PESOS as a number (PHP 2,000.00 = 2000), PHP only (PayMongo used centavos).
  * A payment SESSION is created with POST /sessions (mode PAYMENT_LINK: Xendit's own hosted page, the customer picks GCash / Maya / QR Ph there),
    read with GET /sessions/{ps-id} (status ACTIVE | COMPLETED | EXPIRED | CANCELED; a paid one carries payment_id "py-..."), closed with
    POST /sessions/{ps-id}/cancel. The session expires by itself at the `expires_at` we send.
  * The payment behind a session: GET /v3/payments/{py-id} (header api-version 2024-11-11): status SUCCEEDED, channel_code GCASH | PAYMAYA | QRPH,
    captures[].capture_amount.
  * A refund is POST /v3/refunds (api-version header; payment_id "py-...", a NEW reference_id each time, amount optional) and is read with
    GET /v3/refunds/{rfd-id}. GCash and Maya can be refunded (a part too); QR Ph cannot.
  * A webhook carries the account's callback token in the header `x-callback-token` (a plain shared token, not a signature), so every event the shop acts
    on is checked against XENDIT_WEBHOOK_TOKEN. A webhook is only a PROMPT: the shop always reads the payment back from Xendit before it books.

Nothing in the test suite reaches the network: every test patches `_request`."""
import hmac
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

CURRENCY = "PHP"
COUNTRY = "PH"
API_VERSION = "2024-11-11"           # the header the v3 payment and refund calls ask for
REFUND_REASON = "OTHERS"
WEBHOOK_TOKEN_HEADER = "x-callback-token"
SIGNATURE_HEADER = WEBHOOK_TOKEN_HEADER      # the name the webhook view reads (PayMongo's is a signature header, Xendit's a token)
REAL_API = "https://api.xendit.co"

# How Xendit names how a customer paid (lower-cased) -> what the shop's own pages call it. The same short names PayMongo's code uses.
CHANNEL_LABELS = {"gcash": "GCash", "paymaya": "Maya", "qrph": "QR Ph"}
# The channel codes the page offers (Xendit's names).
DEFAULT_CHANNELS = ["GCASH", "PAYMAYA", "QRPH"]


class XenditError(Exception):
    """Xendit refused, could not be reached, or answered something unexpected. `retryable` is True for a dropped connection or a busy Xendit."""

    def __init__(self, message, *, status=None, retryable=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


# ---------------------------------------------------------------- switches
def secret_key():
    return (getattr(settings, "XENDIT_SECRET_KEY", "") or "").strip()


def mode():
    """'test' for an xnd_development_ key (no real money), 'live' for an xnd_production_ key, '' when no key is set."""
    key = secret_key()
    if not key:
        return ""
    return "live" if key.startswith("xnd_production_") else "test"


def is_test():
    return mode() == "test"


def is_demo():
    """There is no practice gateway for Xendit: its own test mode is the practice (the practice shop `python demo.py` uses the PayMongo stand-in)."""
    return False


def enabled():
    """True when the checkout should use Xendit: a key is set and looks like a Xendit secret key, and a LIVE key is never used against a laptop copy of the
    database."""
    key = secret_key()
    if not key or not key.startswith(("xnd_development_", "xnd_production_")):
        return False
    if key.startswith("xnd_production_") and getattr(settings, "USING_LAPTOP_DB", False):
        return False
    return True


def methods():
    """The channels the page offers, in Xendit's names (settings.XENDIT_CHANNELS)."""
    return [m.upper() for m in (getattr(settings, "XENDIT_CHANNELS", None) or []) if m] or list(DEFAULT_CHANNELS)


# ---------------------------------------------------------------- money
def to_amount(amount):
    """Pesos for Xendit's JSON: a whole number as an int, otherwise two decimals (2000 / 150.5)."""
    value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(value) if value == value.to_integral_value() else float(value)


def from_amount(value):
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:       # noqa: BLE001
        return Decimal("0.00")


# ---------------------------------------------------------------- the HTTP call
def _auth():
    return (secret_key(), "")


def _error_text(response):
    """A short, safe sentence for a failed call (Xendit's own `message` when it sent one). Never contains a key."""
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        text = str(data.get("message") or data.get("error_code") or "")
        if text:
            return text[:300]
    return f"Xendit answered {response.status_code}."


def _request(method, path, body=None, headers=None):
    """One call to Xendit; the parsed JSON (an empty dict for an empty answer). Raises XenditError."""
    if getattr(settings, "TESTING", False):
        raise XenditError("The test suite never reaches Xendit: patch reservations.xendit._request.")
    if not secret_key():
        raise XenditError("Xendit is not set up (no secret key).")
    url = f"{(getattr(settings, 'XENDIT_API_BASE', REAL_API) or REAL_API).rstrip('/')}{path}"
    try:
        response = requests.request(
            method, url, json=body, auth=_auth(), timeout=getattr(settings, "XENDIT_TIMEOUT", 15),
            headers={"Accept": "application/json", "Content-Type": "application/json", **(headers or {})},
        )
    except requests.RequestException as exc:
        logger.warning("Xendit %s %s could not be reached: %s", method, path, type(exc).__name__)
        raise XenditError("Xendit could not be reached.", retryable=True) from exc
    if response.status_code >= 400:
        retryable = response.status_code in (408, 429) or response.status_code >= 500
        raise XenditError(_error_text(response), status=response.status_code, retryable=retryable)
    if not response.content:
        return {}
    try:
        data = response.json()
    except ValueError as exc:
        raise XenditError("Xendit sent an answer that is not JSON.", status=response.status_code, retryable=True) from exc
    return data if isinstance(data, dict) else {}


def _versioned():
    return {"api-version": API_VERSION}


# ---------------------------------------------------------------- payment sessions
@dataclass
class Session:
    id: str
    checkout_url: str
    raw: dict = field(default_factory=dict)


@dataclass
class SessionState:
    """What a payment session says right now (the same fields PayMongo's SessionState has)."""
    id: str
    status: str = ""                 # "active" | "expired"
    paid: bool = False
    payment_id: str = ""             # py-... of the paid payment
    amount: Decimal = Decimal("0")   # what was paid, in pesos
    channel: str = ""                # gcash | paymaya | qrph
    paid_at: int = 0
    reference_number: str = ""
    metadata: dict = field(default_factory=dict)
    livemode: bool = False
    raw: dict = field(default_factory=dict)


def _safe_checkout_url(url):
    """The page Xendit sent the customer to must be a secure address."""
    url = (url or "").strip()
    if url.startswith("https://"):
        return url
    raise XenditError("Xendit sent a payment page address that is not secure.")


def _iso_utc(moment):
    return moment.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def create_checkout_session(*, amount, name, description, reference_number, success_url, cancel_url, metadata=None, customer_name="", customer_email="",
                            expires_minutes=None):
    """Open a hosted payment page for exactly `amount` pesos (the shop absorbs Xendit's fee). `reference_number` is our own attempt token. The customer's
    name and email are NOT sent (Xendit does not need them for this and the shop keeps them to itself). `expires_minutes` closes the page by itself
    after that long (Xendit asks for at least 10). -> Session."""
    value = to_amount(amount)
    if Decimal(str(value)) < 1:
        raise XenditError("The amount is too small to pay online.")
    body = {
        "reference_id": reference_number,
        "session_type": "PAY",
        "mode": "PAYMENT_LINK",
        "amount": value,
        "currency": CURRENCY,
        "country": COUNTRY,
        "description": f"{name}. {description}"[:1000],
        "success_return_url": success_url,
        "cancel_return_url": cancel_url,
        "allowed_payment_channels": methods(),
        "metadata": {str(k)[:40]: str(v)[:500] for k, v in (metadata or {}).items()},
    }
    if expires_minutes:
        body["expires_at"] = _iso_utc(datetime.now(dt_timezone.utc) + timedelta(minutes=max(int(expires_minutes), 11)))
    payload = _request("POST", "/sessions", body)
    session_id = str(payload.get("payment_session_id") or "")
    if not session_id.startswith("ps-"):
        raise XenditError("Xendit's answer had no payment session id.")
    return Session(id=session_id, checkout_url=_safe_checkout_url(payload.get("payment_link_url")), raw=payload)


def _epoch(text):
    """'2026-11-24T11:41:17.366Z' -> seconds since 1970 (0 when it cannot be read)."""
    try:
        return int(datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return 0


def parse_payment(payload):
    """A payment (GET /v3/payments/{id}) -> (paid, amount, channel, paid_at). Only a payment whose status is SUCCEEDED counts."""
    payload = payload if isinstance(payload, dict) else {}
    if str(payload.get("status") or "").upper() != "SUCCEEDED":
        return False, Decimal("0.00"), "", 0
    captures = [c for c in (payload.get("captures") or []) if isinstance(c, dict)]
    if captures:
        amount = sum((from_amount(c.get("capture_amount")) for c in captures), Decimal("0.00"))
        paid_at = max((_epoch(c.get("capture_timestamp")) for c in captures), default=0)
    else:
        amount, paid_at = from_amount(payload.get("request_amount")), _epoch(payload.get("updated") or payload.get("created"))
    return True, amount, str(payload.get("channel_code") or "").lower(), paid_at


def parse_session(payload):
    """A session answer -> SessionState WITHOUT the payment details (retrieve_checkout_session adds those): `paid` here only says the session is COMPLETED."""
    payload = payload if isinstance(payload, dict) else {}
    status = str(payload.get("status") or "").upper()
    return SessionState(
        id=str(payload.get("payment_session_id") or ""),
        status="expired" if status in ("EXPIRED", "CANCELED") else status.lower(),
        paid=status == "COMPLETED", payment_id=str(payload.get("payment_id") or ""),
        reference_number=str(payload.get("reference_id") or ""),
        metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        livemode=mode() == "live", raw=payload,
    )


def retrieve_checkout_session(session_id):
    """Read a session from Xendit (the authority on whether it was paid), and, when it is COMPLETED, the payment behind it (how much, by which channel,
    when). -> SessionState; `paid` is True only when that payment really SUCCEEDED."""
    state = parse_session(_request("GET", f"/sessions/{session_id}"))
    if not state.paid:
        return state
    state.paid = False
    if not state.payment_id:
        return state
    paid, amount, channel, paid_at = parse_payment(_request("GET", f"/v3/payments/{state.payment_id}", headers=_versioned()))
    if paid:
        state.paid, state.amount, state.channel, state.paid_at = True, amount, channel, paid_at
    return state


def expire_checkout_session(session_id):
    """Close a session so it can no longer be paid. -> "expired" | "already_expired" | "has_payment" (it was paid: leave it and read it again) |
    "error" (Xendit refused for another reason, or could not be reached)."""
    try:
        current = parse_session(_request("GET", f"/sessions/{session_id}"))
    except XenditError as exc:
        logger.warning("Xendit session %s could not be read before closing it: %s", session_id, exc)
        return "error"
    if current.paid:
        return "has_payment"
    if current.status == "expired":
        return "already_expired"
    try:
        closed = parse_session(_request("POST", f"/sessions/{session_id}/cancel"))
    except XenditError as exc:
        message = str(exc).lower()
        if exc.status in (400, 409) and ("complete" in message or "paid" in message):
            return "has_payment"
        logger.warning("Xendit would not cancel %s: %s", session_id, exc)
        return "error"
    return "has_payment" if closed.paid else "expired"


# ---------------------------------------------------------------- refunds
@dataclass
class Refund:
    id: str
    status: str = ""                 # pending | succeeded | failed
    amount: Decimal = Decimal("0")
    payment_id: str = ""
    raw: dict = field(default_factory=dict)


def parse_refund(payload):
    """A refund (an answer, or the data inside a webhook event) -> Refund. Accepts the refund itself or an event wrapper that holds it in "data"."""
    payload = payload if isinstance(payload, dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) and "refund_id" not in payload else payload
    return Refund(id=str(data.get("refund_id") or ""), status=str(data.get("status") or "").lower(), amount=from_amount(data.get("amount") or 0),
                  payment_id=str(data.get("payment_id") or ""), raw=payload)


def create_refund(*, payment_id, amount, note=""):
    """Ask Xendit to send `amount` pesos of a paid payment back to the customer (a part is allowed for GCash and Maya; QR Ph cannot be refunded).
    -> Refund (status usually "pending")."""
    value = to_amount(amount)
    if Decimal(str(value)) < 1:
        raise XenditError("The amount is too small to refund online.")
    body = {"reference_id": f"rf-{uuid.uuid4().hex}", "payment_id": payment_id, "reason": REFUND_REASON, "amount": value,
            "metadata": {"note": (note or "")[:480]}}
    refund = parse_refund(_request("POST", "/v3/refunds", body, headers=_versioned()))
    if not refund.id:
        raise XenditError("Xendit's answer had no refund id.")
    return refund


def retrieve_refund(refund_id):
    return parse_refund(_request("GET", f"/v3/refunds/{refund_id}", headers=_versioned()))


# ---------------------------------------------------------------- webhooks
def verify_webhook(raw_body, header, secret):
    """The event (a dict) when `header` is the shop's callback token (the one set in Xendit's dashboard), else None. The comparison takes the same time
    however much of the token is right. An event is believed only with the token; a shop with no token set believes nothing."""
    if not secret or not header or not isinstance(raw_body, (bytes, bytearray)):
        return None
    if not hmac.compare_digest(str(header).strip().encode("utf-8"), str(secret).strip().encode("utf-8")):
        return None
    try:
        event = json.loads(bytes(raw_body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return event if isinstance(event, dict) else None


def _event_data(event):
    data = event.get("data") if isinstance(event, dict) else None
    return data if isinstance(data, dict) else {}


def event_type(event):
    """The kind of event, in the names the shop's webhook view uses: 'checkout_session.payment.paid' (a session was completed),
    'payment.refund.updated' (a refund succeeded or failed), anything else is returned as Xendit names it ('' when there is no type)."""
    name = str(event.get("event") or "") if isinstance(event, dict) else ""
    if name == "payment_session.completed":
        return "checkout_session.payment.paid"
    if name in ("refund.succeeded", "refund.failed"):
        return "payment.refund.updated"
    return name


def event_resource(event):
    """The resource inside an event, as {"id": ..., ...}: the session for a completed session, the refund (with its refund_id and status) for a refund."""
    data = dict(_event_data(event))
    name = str(event.get("event") or "") if isinstance(event, dict) else ""
    # Xendit's own documentation names the session "payment_session_id", but the sample its dashboard sends (Test and save) names it "id": both are read.
    if name == "payment_session.completed":
        data["id"] = str(data.get("payment_session_id") or data.get("id") or "")
    elif name.startswith("refund."):
        data["id"] = str(data.get("refund_id") or data.get("id") or "")
        data["refund_id"] = data["id"]
        data.setdefault("status", "succeeded" if name.endswith("succeeded") else "failed")
    return data


def event_livemode(event):
    return mode() == "live"
