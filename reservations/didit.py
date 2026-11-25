"""The Didit connection: hosted ID verification sessions, their results, and webhooks. Nothing else in the project talks to Didit.

What Didit does for the shop: the customer opens a page Didit hosts, picks their ID, scans it and takes a selfie (liveness + face match), and Didit answers
Approved / Declined / In Review. The shop only ever keeps the RESULT (and, for the owner, the front and back pictures of the ID).

Facts used here, from Didit's documentation and a real call against the SANDBOX (2026-11-25; see the memory note project_didit_id_verification):
  * Base https://verification.didit.me, header `x-api-key`. A SANDBOX application has a key `sk_test_...` (test data, never billed); the same code works for a live one.
  * POST /v3/session/ {workflow_id, vendor_data, callback, metadata} -> 201 {session_id, session_token, url, expires_at (7 days), status "Not Started", ...}.
    `vendor_data` is OUR opaque reference (never a name or an email). One unfinished session exists per (workflow, vendor_data): asking again returns the same one.
  * GET /v3/session/{id}/decision/ -> {status, id_verifications[], liveness_checks[], face_matches[], decision_trace{status_warnings[]}, ...}. The picture links in
    id_verifications[] (front_image, back_image, ...) are SIGNED and SHORT-LIVED: they are copied at once and never stored.
  * Sandbox only: the hosted page offers sample documents and an outcome picker; `sandbox_scenario` on create picks the outcome up front.
  * A webhook (status.updated) carries X-Signature-V2 (HMAC-SHA256, hex, of the body re-serialised with sorted keys / compact separators / unescaped Unicode,
    whole-valued floats written as integers), and X-Timestamp (unix seconds, refused when more than 5 minutes off). A webhook is only a PROMPT: the shop always
    reads the decision back from Didit before it acts, exactly as it does for a payment.

Nothing in the test suite reaches the network: every test patches `_request` (and the picture download)."""
import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from urllib.parse import urlparse

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

REAL_API = "https://verification.didit.me"
WEBHOOK_TOLERANCE_SECONDS = 300

# What Didit calls a session's state (compared without regard to case). The first group is "still going".
STATUS_APPROVED = "Approved"
STATUS_DECLINED = "Declined"
STATUS_REVIEW = "In Review"
STATUS_EXPIRED = "Expired"
STATUS_ABANDONED = "Abandoned"
STATUS_WAITING = ("Not Started", "In Progress", "Awaiting User", "Resubmitted")
_KNOWN = {s.lower(): s for s in (STATUS_APPROVED, STATUS_DECLINED, STATUS_REVIEW, STATUS_EXPIRED, STATUS_ABANDONED, "Kyc Expired", *STATUS_WAITING)}


class DiditError(Exception):
    """Didit refused, could not be reached, or answered something unexpected. `retryable` is True for a dropped connection or a busy Didit."""

    def __init__(self, message, *, status=None, retryable=False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retryable = retryable


# ---------------------------------------------------------------- switches
def api_key():
    return (getattr(settings, "DIDIT_API_KEY", "") or "").strip()


def workflow_id():
    return (getattr(settings, "DIDIT_WORKFLOW_ID", "") or "").strip()


def webhook_secret():
    return (getattr(settings, "DIDIT_WEBHOOK_SECRET", "") or "").strip()


def mode():
    """'test' for a sandbox key (sk_test_...: test data, never billed), 'live' for any other key, '' when no key is set."""
    key = api_key()
    if not key:
        return ""
    return "test" if key.startswith("sk_test_") else "live"


def is_test():
    return mode() == "test"


def enabled():
    """True when ID verification can be used: a key and a workflow are set, and a LIVE key is never used against a laptop copy of the database."""
    if not api_key() or not workflow_id():
        return False
    if mode() == "live" and getattr(settings, "USING_LAPTOP_DB", False):
        return False
    return True


# ---------------------------------------------------------------- the HTTP call
def _error_text(response):
    """A short, safe sentence for a failed call. Never contains a key or a picture link."""
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        for key in ("detail", "message", "error"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value[:200]
    return f"Didit answered {response.status_code}."


def _request(method, path, body=None):
    """One call to Didit; the parsed JSON (an empty dict for an empty answer). Raises DiditError."""
    if getattr(settings, "TESTING", False):
        raise DiditError("The test suite never reaches Didit: patch reservations.didit._request.")
    if not api_key():
        raise DiditError("Didit is not set up (no API key).")
    base = (getattr(settings, "DIDIT_API_BASE", REAL_API) or REAL_API).rstrip("/")
    try:
        response = requests.request(
            method, f"{base}{path}", json=body, timeout=getattr(settings, "DIDIT_TIMEOUT", 15),
            headers={"x-api-key": api_key(), "Accept": "application/json", "Content-Type": "application/json"},
        )
    except requests.RequestException as exc:
        logger.warning("Didit %s %s could not be reached: %s", method, path.split("?")[0], type(exc).__name__)
        raise DiditError("Didit could not be reached.", retryable=True) from exc
    if response.status_code >= 400:
        retryable = response.status_code in (408, 429) or response.status_code >= 500
        raise DiditError(_error_text(response), status=response.status_code, retryable=retryable)
    if not response.content:
        return {}
    try:
        data = response.json()
    except ValueError as exc:
        raise DiditError("Didit sent an answer that is not JSON.", status=response.status_code, retryable=True) from exc
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- sessions
@dataclass
class Session:
    session_id: str
    url: str
    expires_at: datetime = None
    status: str = ""
    raw: dict = field(default_factory=dict)


def _safe_page_url(url):
    """The page the customer is sent to must be a secure Didit address (the customer is redirected there, so never anything else)."""
    url = (url or "").strip()
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and (host == "didit.me" or host.endswith(".didit.me")):
        return url
    raise DiditError("Didit sent a verification page address that is not secure.")


def _parse_time(value):
    if not value or not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt_timezone.utc)


def create_session(*, vendor_data, callback, metadata=None, scenario=None):
    """Open a verification page for one person. `vendor_data` is our own opaque reference (Didit keeps one unfinished session per reference: asking again
    returns the same one); `callback` is where the customer is sent after they finish. `scenario` (SANDBOX only) picks the outcome up front. -> Session."""
    body = {"workflow_id": workflow_id(), "vendor_data": str(vendor_data)[:100], "callback": callback, "metadata": metadata or {}}
    if scenario and is_test():
        body["sandbox_scenario"] = scenario
    data = _request("POST", "/v3/session/", body)
    session_id = str(data.get("session_id") or "")
    if not session_id:
        raise DiditError("Didit did not say which session it opened.")
    return Session(session_id=session_id, url=_safe_page_url(data.get("url")), expires_at=_parse_time(data.get("expires_at")),
                   status=_canonical_status(data.get("status")), raw=data)


# ---------------------------------------------------------------- decisions
# Didit's own risk codes -> a plain sentence for staff (and, for a declined customer, for them). Anything else shows Didit's short description.
_REASON_WORDS = {
    "DOCUMENT_EXPIRED": "The ID has expired.",
    "EXPIRED_DOCUMENT": "The ID has expired.",
    "LOW_FACE_MATCH_SIMILARITY": "The selfie does not match the picture on the ID.",
    "FACE_MATCH_LOW_SIMILARITY": "The selfie does not match the picture on the ID.",
    "LOW_LIVENESS_SCORE": "The selfie check did not show a live person.",
    "LIVENESS_FACE_ATTACK": "The selfie check did not show a live person.",
    "COULD_NOT_RECOGNIZE_DOCUMENT": "The ID could not be read.",
    "DOCUMENT_NOT_SUPPORTED": "That kind of ID is not accepted.",
    "MINIMUM_AGE_NOT_MET": "The person is under the minimum age.",
    "MRZ_VALIDATION_FAILED": "The ID's machine-readable lines did not check out.",
    "POSSIBLE_DUPLICATED_USER": "This ID has been used by another account.",
    "IP_ADDRESS_IN_BLOCKLIST": "The connection was blocked.",
    "DOCUMENT_PRINTED_COPY": "The ID looks like a printed copy.",
    "DOCUMENT_SCREEN_REPLAY": "The ID looks like a photo of a screen.",
    "INJECTION_ATTACK_DETECTED": "The camera check failed. Please try again with the phone's own camera.",       # seen in a real sandbox run (a virtual camera was used)
}


def _clean_reason(text):
    """One short reason, letters/digits/punctuation only, at most 120 characters (Didit's own words, never ID data)."""
    text = re.sub(r"[^A-Za-z0-9 .,'()-]", "", str(text or "")).strip()
    return text[:120]


def _canonical_status(value):
    text = str(value or "").strip()
    return _KNOWN.get(text.lower(), text)


@dataclass
class Decision:
    """What a session says right now. `name` is the name printed on the ID: it is compared and then thrown away, never stored."""
    session_id: str
    status: str = ""
    vendor_data: str = ""
    document_type: str = ""          # "Passport" | "Identity Card" | "Driver's License" | ...
    document_country: str = ""       # three-letter country code
    name: str = ""
    reasons: list = field(default_factory=list)
    front_url: str = ""
    back_url: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def is_waiting(self):
        return self.status in STATUS_WAITING or not self.status


def _first(items):
    return items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}


def _reasons(raw):
    """Up to 4 plain reasons a session was not approved: from the session's own warnings, else the ID report's."""
    sources = []
    trace = raw.get("decision_trace")
    if isinstance(trace, dict) and isinstance(trace.get("status_warnings"), list):
        sources.append(trace["status_warnings"])
    for key in ("id_verifications", "liveness_checks", "face_matches"):
        for entry in raw.get(key) or []:
            if isinstance(entry, dict) and isinstance(entry.get("warnings"), list):
                sources.append(entry["warnings"])
    out = []
    for warnings in sources:
        for warning in warnings:
            if not isinstance(warning, dict) or str(warning.get("log_type") or "").lower() not in ("error", "warning"):
                continue
            words = _REASON_WORDS.get(str(warning.get("risk") or "").upper()) or _clean_reason(warning.get("short_description"))
            if words and words not in out:
                out.append(words)
            if len(out) >= 4:
                return out
    return out


def parse_decision(data, session_id=""):
    """The Decision of a /decision/ (or a webhook's `decision`) answer. Pure: no network."""
    raw = data if isinstance(data, dict) else {}
    report = _first(raw.get("id_verifications"))
    status = _canonical_status(raw.get("status"))
    return Decision(
        session_id=str(raw.get("session_id") or session_id or ""), status=status, vendor_data=str(raw.get("vendor_data") or ""),
        document_type=str(report.get("document_type") or ""), document_country=str(report.get("issuing_state") or "").upper()[:3],
        name=str(report.get("full_name") or " ".join(p for p in (report.get("first_name"), report.get("last_name")) if p) or ""),
        reasons=_reasons(raw) if status in (STATUS_DECLINED, STATUS_REVIEW) else [],
        front_url=str(report.get("front_image") or ""), back_url=str(report.get("back_image") or ""), raw=raw,
    )


def retrieve_decision(session_id):
    """Read a session's decision from Didit. -> Decision. (The picture links in it are good for a short time only.)"""
    session_id = str(session_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", session_id):
        raise DiditError("That is not a verification session.")
    return parse_decision(_request("GET", f"/v3/session/{session_id}/decision/"), session_id)


# ---------------------------------------------------------------- webhooks
def _shorten_floats(value):
    """Whole-valued numbers written as integers (4.0 -> 4), the way Didit writes them before it signs."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _shorten_floats(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shorten_floats(v) for v in value]
    return value


def canonical_json(data):
    return json.dumps(_shorten_floats(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sign(data, secret):
    """The X-Signature-V2 of a webhook body (what Didit sends; used by the tests to build genuine ones)."""
    return hmac.new(secret.encode("utf-8"), canonical_json(data).encode("utf-8"), hashlib.sha256).hexdigest()


def verify_webhook(request, now=None):
    """The webhook's JSON when it really came from Didit (signed with the shop's webhook secret, sent within 5 minutes), else None. With no secret set,
    every webhook is refused. A webhook is only a prompt: whoever calls this still reads the decision back from Didit."""
    secret = webhook_secret()
    if not secret:
        return None
    try:
        stamp = int(request.headers.get("X-Timestamp", ""))
    except (TypeError, ValueError):
        return None
    if abs((now if now is not None else time.time()) - stamp) > WEBHOOK_TOLERANCE_SECONDS:
        return None
    try:
        data = json.loads(request.body or b"")
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    expected = sign(data, secret)
    for header in ("X-Signature-V2", "X-Signature-V2-Previous"):
        given = request.headers.get(header)
        if given and hmac.compare_digest(given.strip().lower(), expected):
            return data
    raw_expected = hmac.new(secret.encode("utf-8"), request.body or b"", hashlib.sha256).hexdigest()
    given = request.headers.get("X-Signature")
    if given and hmac.compare_digest(given.strip().lower(), raw_expected):
        return data
    return None


def event_session_id(event):
    return str((event or {}).get("session_id") or "")


# ---------------------------------------------------------------- the pictures
MAX_PICTURE_BYTES = 8 * 1024 * 1024


def download_picture(url):
    """The bytes of one ID picture from a signed link Didit gave (https only, at most 8 MB, it must say it is an image). Raises DiditError. The links are
    pre-signed, so the key is not sent -- unless Didit's own address refuses without it."""
    url = str(url or "").strip()
    parts = urlparse(url)
    if parts.scheme != "https" or not parts.hostname:
        raise DiditError("A picture link was not secure.")
    host = parts.hostname.lower()
    timeout = (5, 10)
    try:
        response = requests.get(url, timeout=timeout, stream=True, allow_redirects=True)
        if response.status_code in (401, 403) and (host == "didit.me" or host.endswith(".didit.me")):
            response.close()
            response = requests.get(url, timeout=timeout, stream=True, headers={"x-api-key": api_key()})
    except requests.RequestException as exc:
        logger.warning("An ID picture could not be downloaded: %s", type(exc).__name__)
        raise DiditError("An ID picture could not be downloaded.", retryable=True) from exc
    try:
        if response.status_code >= 400:
            raise DiditError(f"An ID picture link answered {response.status_code}.", status=response.status_code, retryable=response.status_code >= 500)
        if urlparse(response.url).scheme != "https":
            raise DiditError("A picture link led somewhere that is not secure.")
        kind = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if kind and not kind.startswith("image/"):
            raise DiditError("A picture link did not give a picture.")
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=65536):
            size += len(chunk)
            if size > MAX_PICTURE_BYTES:
                raise DiditError("An ID picture is too large.")
            chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            raise DiditError("An ID picture was empty.")
        return data
    finally:
        response.close()
