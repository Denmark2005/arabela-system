"""The receipt check: reads the GCash screenshot a customer uploads as proof of the security deposit and decides, BEFORE any booking exists,
whether it may go on.

Three outcomes (`Verdict.status`):
  * "reject"      -- clearly not a usable proof (a random picture, another app, a payment to someone else, an old or reused receipt...).
                     The customer sees `Verdict.message`; nothing is booked and no dates are held.
  * "needs_check" -- could be fine but something is missing or odd (recipient not visible, a different amount, ...). It goes on to the staff
                     and the owner with the reasons written in `Verdict.notes`; the customer is never blocked by it.
  * "pass"        -- every check was fine. It still waits for the owner to look in her own GCash: nothing here can talk to GCash.

Two separate parts, so the rules can be tested without ever calling the reader:
  * `read_receipt(...)`   asks Google's Gemini to READ the picture (it only reads; it decides nothing) -> `Reading`.
  * `evaluate(...)`       applies the rules to a `Reading` and what the database knows -> `Verdict`. A pure function.
  * `check_receipt(...)`  does both plus the database lookups (same reference / same picture used before).

What the reader sees is judged against the shop's settings: its GCash number AT THE TIME PRINTED ON THE RECEIPT (gowns/gcash_numbers.py,
so a number the owner changed is handled), its account name, and the deposit of THIS booking.
"""
import base64
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.utils import timezone

from gowns import gcash_numbers

logger = logging.getLogger(__name__)

# ---- the rules' numbers (the plan the owner agreed) ----------------------------------------------------------------------------------
MAX_AGE = timedelta(hours=24)               # a receipt older than this is refused
BEFORE_BOOKING_GRACE = timedelta(minutes=10)  # paid up to this long before the booking started still counts as "after it started"
FUTURE_TOLERANCE = timedelta(minutes=10)    # a phone clock a little ahead is normal; more than this is odd
AMOUNT_OVER_LIMIT = Decimal("100")          # up to this many pesos over the deposit passes (with a note); more is "needs check"
REFERENCE_DIGITS = 13                       # a GCash reference number has 13 digits

STATUS_PASS = "pass"
STATUS_NEEDS_CHECK = "needs_check"
STATUS_REJECT = "reject"

# ---- what the customer is told when a receipt is refused ------------------------------------------------------------------------------
MESSAGES = {
    "not_payment": "This doesn't look like a payment receipt. Please upload the screenshot of your GCash payment to Arabela.",
    "list_screen": "That screenshot shows many transactions. Please open your payment to Arabela and screenshot its details "
                   "(the page that shows the reference number).",
    "not_gcash": "Only GCash receipts are accepted. Please pay with the GCash app and upload that receipt. If you already paid from "
                 "another app, message the shop on Facebook with your receipt.",
    "failed": "This payment shows as failed. Please upload the receipt of a payment that went through.",
    "received": "This screen shows money you received, not money you sent. Please upload the receipt of the payment you sent to Arabela.",
    "merchant": "This payment went to a store or biller, not to Arabela's GCash number. Please upload the receipt of your payment to Arabela.",
    "no_reference": "The receipt must show the reference number. Please screenshot the whole receipt, including the Ref No.",
    "wrong_recipient": "This payment was not sent to Arabela's GCash number. Please check the number on the QR and upload the receipt of "
                       "your payment to Arabela.",
    "old_number": "This number is no longer used by the shop. If you already paid it, please message the shop on Facebook with your receipt.",
    "too_old": "This receipt is from {when}. Please upload the receipt of today's payment.",
    "reference_used": "This receipt was already used for another booking.",
    "image_used": "This receipt was already used for another booking.",
}

_REASON_TEXT = {      # short, plain, for the Reservation's notes and the ProofRejection log
    "not_payment": "not a payment receipt", "list_screen": "a list of transactions", "not_gcash": "not a GCash receipt",
    "failed": "failed payment", "received": "money received, not sent", "merchant": "payment to a store or biller",
    "no_reference": "no reference number", "wrong_recipient": "sent to another number", "old_number": "old GCash number",
    "too_old": "receipt too old", "reference_used": "reference already used", "image_used": "picture already used",
}


def reject_reason_text(code):
    return _REASON_TEXT.get(code, code)


# ---- what the reader found ------------------------------------------------------------------------------------------------------------
@dataclass
class Reading:
    is_payment_screen: bool = False
    is_transaction_list: bool = False
    app: str = "unknown"                 # gcash | maya | bank | other | unknown
    direction: str = "unknown"           # sent | received | merchant_payment | unknown
    recipient_number: str = ""           # 11 digits starting 09 when it is a Philippine mobile number, else the digits as shown
    recipient_name: str = ""
    sender_number: str = ""
    amount: Decimal | None = None
    reference: str = ""                  # digits only
    paid_at: datetime | None = None      # timezone-aware, shop time
    status: str = "unknown"              # completed | failed | pending | unknown
    gcash_signs: list = field(default_factory=list)

    def to_json(self):
        return json.dumps({
            "is_payment_screen": self.is_payment_screen, "is_transaction_list": self.is_transaction_list, "app": self.app,
            "direction": self.direction, "recipient_number": self.recipient_number, "recipient_name": self.recipient_name,
            "sender_number": self.sender_number, "amount": str(self.amount) if self.amount is not None else None,
            "reference": self.reference, "paid_at": self.paid_at.isoformat() if self.paid_at else None,
            "status": self.status, "gcash_signs": self.gcash_signs,
        })


def _digits(value):
    return re.sub(r"\D", "", str(value or ""))


def _parse_amount(value):
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value).replace(",", "").replace("₱", "").replace("PHP", "").strip()).copy_abs()
    except (InvalidOperation, ValueError):
        return None
    return amount.quantize(Decimal("0.01"))


def _parse_when(value):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return timezone.make_aware(naive, timezone.get_current_timezone())
    return None


def _as_text(value, limit=100):
    return str(value or "").strip()[:limit]


def reading_from_dict(data):
    """A Reading from the reader's JSON object. Anything missing or odd becomes the "don't know" value, never an exception."""
    if not isinstance(data, dict):
        return None
    number = gcash_numbers.normalize(data.get("recipient_number")) or _digits(data.get("recipient_number"))
    sender = gcash_numbers.normalize(data.get("sender_number")) or _digits(data.get("sender_number"))
    signs = data.get("gcash_signs")
    return Reading(
        is_payment_screen=data.get("is_payment_screen") is True,
        is_transaction_list=data.get("is_transaction_list") is True,
        app=_as_text(data.get("app"), 12).lower() or "unknown",
        direction=_as_text(data.get("direction"), 20).lower() or "unknown",
        recipient_number=number,
        recipient_name=_as_text(data.get("recipient_name")),
        sender_number=sender,
        amount=_parse_amount(data.get("amount")),
        reference=_digits(data.get("reference")),
        paid_at=_parse_when(data.get("date_time")),
        status=_as_text(data.get("status"), 12).lower() or "unknown",
        gcash_signs=[_as_text(s, 40) for s in signs][:8] if isinstance(signs, list) else [],
    )


# ---- the reader (Gemini) --------------------------------------------------------------------------------------------------------------
PROMPT = """You read screenshots of phone-wallet payments made in the Philippines. Look ONLY at the picture and answer with ONE JSON object and nothing else.
Copy what is printed; never guess. Use "" (or null for numbers) when something is not visible.

Keys:
- "is_payment_screen": true ONLY when the picture is a phone screenshot of ONE completed payment or transfer (a receipt, or the details page of one transaction). false for anything else: a photo, a chat, a form, a bill, a screenshot of something else.
- "is_transaction_list": true when the picture shows a LIST of several transactions instead of one.
- "app": "gcash", "maya", "bank", "other" or "unknown". GCash receipts: a blue GCash screen or a white card with a check mark; words like "Express Send", "Sent via GCash", "Transaction Details", "Ref No." / "Reference Number" with 13 digits; titles like "Transfer from 09... to 09..." or "Payment to ...". Maya has a green "maya" logo, "Sent money to", "Received money from", "Reference ID". Banks show their own name.
- "gcash_signs": a short list of GCash-specific words or logos you can actually see (for example "Express Send", "Sent via GCash", "Transaction Details", "Ref No.").
- "direction": "sent" when the person SENT money or made a payment to a mobile number; "received" when the screen is about money received (for example "Received money from"); "merchant_payment" for a payment to a store, biller or merchant (for example "Payment to ...", "Bancnet P2M", "Shopee"); "unknown" otherwise.
- "recipient_number": the Philippine mobile number the money was sent TO, digits only, written in full with its leading 0 like 09171234567 (a number printed as +63 917 123 4567 is 09171234567; for "Transfer from A to B" it is B), or "".
- "recipient_name": the recipient's name exactly as printed (it may be partly hidden with * or dots), or "".
- "sender_number": the number the money came FROM, digits only, or "".
- "amount": the amount sent in pesos as a plain number (use the "Total Amount Sent" when there is one; no minus sign, no currency sign), or null.
- "reference": the reference number, digits only (remove spaces), or "".
- "date_time": the date and time of the payment as "YYYY-MM-DD HH:MM" in 24-hour time exactly as printed on the screen, or "".
- "status": "completed", "failed", "pending" or "unknown".
"""

READER_TIMEOUT_SECONDS = 28
READER_CUTOFF_SECONDS = 22          # no new attempt starts after this
RETRYABLE = frozenset({500, 502, 503, 504})
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _models():
    models = [settings.GEMINI_MODEL]
    backup = (getattr(settings, "GEMINI_FALLBACK_MODEL", "") or "").strip()
    if backup and backup != settings.GEMINI_MODEL:
        models.append(backup)
    return models


def _extract_json(response_json):
    try:
        parts = response_json["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except ValueError:
        found = re.search(r"\{.*\}", text, re.S)
        if not found:
            return None
        try:
            return json.loads(found.group(0))
        except ValueError:
            return None


def read_receipt(image_bytes, mime_type):
    """(Reading, "") when the picture was read, else (None, why) -- why is a short code for the log: disabled, not_configured, timeout, network,
    http_NNN, unreadable. Never raises: whatever goes wrong here must only mean "staff look at it", never "the customer gets an error"."""
    if not getattr(settings, "RECEIPT_READER_ENABLED", False):
        return None, "disabled"
    if not getattr(settings, "GEMINI_API_KEY", ""):
        return None, "not_configured"
    body = {
        "contents": [{"role": "user", "parts": [
            {"text": PROMPT},
            {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode("ascii")}},
        ]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 3072, "responseMimeType": "application/json"},
    }
    started = time.monotonic()
    failure = "network"
    tries = 0
    for model in _models():
        for attempt in range(2):
            elapsed = time.monotonic() - started
            if tries and elapsed > READER_CUTOFF_SECONDS:
                return None, failure
            tries += 1
            try:
                response = requests.post(
                    GEMINI_URL.format(model=model), params={"key": settings.GEMINI_API_KEY}, json=body,
                    timeout=max(READER_TIMEOUT_SECONDS - elapsed, 3),
                )
            except requests.Timeout:
                logger.warning("Receipt reader %s: timed out", model)
                return None, "timeout"
            except requests.RequestException as exc:
                logger.warning("Receipt reader %s: connection problem %s", model, type(exc).__name__)
                failure = "network"
                time.sleep(1)
                continue
            if response.status_code == 200:
                try:
                    reading = reading_from_dict(_extract_json(response.json()))
                except ValueError:
                    reading = None
                if reading is not None:
                    return reading, ""
                logger.warning("Receipt reader %s: answer could not be understood", model)
                failure = "unreadable"
                break                       # the backup model gets a try
            failure = f"http_{response.status_code}"
            logger.warning("Receipt reader %s: HTTP %s", model, response.status_code)
            if response.status_code not in RETRYABLE:
                break
            time.sleep(1)
    return None, failure


# ---- the rules ------------------------------------------------------------------------------------------------------------------------
@dataclass
class Verdict:
    status: str
    codes: list = field(default_factory=list)       # machine reasons ("amount_low", "too_old", ...)
    message: str = ""                               # for the customer, only when rejected
    notes: list = field(default_factory=list)       # for staff and the owner (pass / needs_check)
    reading: Reading | None = None
    old_number: bool = False
    amount: Decimal | None = None
    paid_at: datetime | None = None
    reference: str = ""
    recipient: str = ""
    fingerprint: str = ""
    image_hash: str = ""

    @property
    def rejected(self):
        return self.status == STATUS_REJECT


def _peso(amount):
    amount = Decimal(amount)
    return f"₱{amount:,.0f}" if amount == amount.to_integral_value() else f"₱{amount:,.2f}"


def _name_key(text):
    """(first two letters of the first word, first letter of the last word) in capitals -- GCash hides the middle of a name the same way
    on the QR and on receipts ("DE****K C."), so only these two parts can be compared. None when there is no name."""
    words = [w for w in re.split(r"\s+", re.sub(r"[^A-Za-z*•●·.\s]", "", str(text or "")).strip()) if w]
    if not words:
        return None
    first = re.sub(r"[*•●·.]", "", words[0])[:2].upper()
    last = re.sub(r"[*•●·.]", "", words[-1])[:1].upper() if len(words) > 1 else ""
    return first, last


def names_match(shown, shop_name):
    """True / False when both names are there to compare, None when one of them is missing."""
    a, b = _name_key(shown), _name_key(shop_name)
    if a is None or b is None or not a[0] or not b[0]:
        return None
    if a[0] != b[0]:
        return False
    if a[1] and b[1] and a[1] != b[1]:
        return False
    return True


def _fingerprint(amount, paid_at, recipient):
    if amount is None or paid_at is None or not recipient:
        return ""
    return f"{amount}|{timezone.localtime(paid_at).strftime('%Y-%m-%d %H:%M')}|{recipient}"


def evaluate(reading, *, expected_amount, started_at=None, now=None, shop_name="", reference_used_by="", image_used_by="",
             fingerprint_used_by="", accepted_at=None, known_numbers=None):
    """Apply every rule to what the reader found. Returns a Verdict; never touches the network or the database.

    accepted_at(moment) -> [(number, is_old)] and known_numbers (a set) come from gowns/gcash_numbers.py; they are parameters so a test can
    hand in its own. `reference_used_by` / `image_used_by` / `fingerprint_used_by` are the booking code that already has it ('' = none)."""
    now = now or timezone.now()
    accepted_at = accepted_at or gcash_numbers.accepted_numbers_at
    known_numbers = known_numbers if known_numbers is not None else gcash_numbers.known_numbers()
    expected_amount = Decimal(expected_amount)
    verdict = Verdict(status=STATUS_PASS, reading=reading)

    def refuse(code, **fmt):
        verdict.status = STATUS_REJECT
        verdict.codes = [code]
        verdict.message = MESSAGES[code].format(**fmt) if fmt else MESSAGES[code]
        verdict.notes = []
        return verdict

    if image_used_by:
        return refuse("image_used")

    if reading is None:
        verdict.status = STATUS_NEEDS_CHECK
        verdict.codes.append("reader_unavailable")
        verdict.notes.append("The receipt could not be read automatically, so nothing was checked. Look at the picture yourself.")
        return verdict

    # 1. is it a payment receipt at all?
    if reading.is_transaction_list:
        return refuse("list_screen")
    if not reading.is_payment_screen:
        return refuse("not_payment")
    # 2. GCash only
    if reading.app in ("maya", "bank", "other"):
        return refuse("not_gcash")
    if reading.status == "failed":
        return refuse("failed")
    # 3. money the customer SENT
    if reading.direction == "received":
        return refuse("received")
    if reading.direction == "merchant_payment":
        return refuse("merchant")
    # 4. the reference number must be on it
    if not reading.reference:
        return refuse("no_reference")

    paid_at = reading.paid_at
    moment = paid_at or now
    # 5. the recipient: the shop's number AT THE TIME OF THE PAYMENT
    recipient = reading.recipient_number
    verdict.recipient = recipient
    name_state = names_match(reading.recipient_name, shop_name)
    if recipient:
        accepted = dict(accepted_at(moment))
        if recipient in accepted:
            if accepted[recipient]:
                verdict.old_number = True
                verdict.codes.append("old_number_used")
                verdict.notes.append(f"Paid to the shop's OLD GCash number ({recipient}). Look for it in the old account.")
            if name_state is False:
                verdict.codes.append("name_differs")
                verdict.notes.append(f"The name on the receipt ({reading.recipient_name}) is not the shop's account name. It may have been edited.")
        elif recipient in known_numbers:
            return refuse("old_number")
        elif not accepted and not known_numbers:
            # The shop has no GCash number saved at all, so there is nothing to compare the receipt's number with. That is a settings gap, never
            # the customer's fault: the booking goes through and the staff / owner check it.
            verdict.codes.append("shop_number_not_set")
            verdict.notes.append("The shop's GCash number is not saved in the settings, so the number on the receipt was not checked.")
        else:
            return refuse("wrong_recipient")
    else:
        verdict.codes.append("recipient_not_visible")
        if name_state is True:
            verdict.notes.append("The receipt shows no number, only a name that matches the shop's. Check the reference in your GCash.")
        elif name_state is False:
            verdict.notes.append(f"The receipt shows no number and a different name ({reading.recipient_name}).")
        else:
            verdict.notes.append("The receipt shows neither the recipient's number nor a name. Check the reference in your GCash.")

    # 6. the date
    if paid_at is not None:
        age = now - paid_at
        if age > MAX_AGE:
            return refuse("too_old", when=timezone.localtime(paid_at).strftime("%b %d, %Y").replace(" 0", " "))
        if paid_at > now + FUTURE_TOLERANCE:
            verdict.codes.append("paid_in_future")
            verdict.notes.append("The receipt is dated in the future (the phone's clock may be wrong, or the picture was edited).")
        elif started_at is not None and paid_at < started_at - BEFORE_BOOKING_GRACE:
            verdict.codes.append("paid_before_booking")
            verdict.notes.append("The payment was made before this booking was started (but within the last 24 hours).")
    else:
        verdict.codes.append("date_unreadable")
        verdict.notes.append("The date and time on the receipt could not be read.")

    # 7. the same reference used before
    if reference_used_by:
        return refuse("reference_used")

    # needs-check details (these never refuse) ---------------------------------------------------------------------------------------------
    if len(reading.reference) != REFERENCE_DIGITS:
        verdict.codes.append("reference_length")
        verdict.notes.append(f"The reference has {len(reading.reference)} digits; a GCash reference has {REFERENCE_DIGITS}. Read it from the picture.")
    if reading.app == "unknown":
        verdict.codes.append("app_unclear")
        verdict.notes.append("It was not clear that this is a GCash receipt.")
    amount = reading.amount
    if amount is None:
        verdict.codes.append("amount_unreadable")
        verdict.notes.append("The amount on the receipt could not be read.")
    elif amount < expected_amount:
        verdict.codes.append("amount_low")
        verdict.notes.append(f"Paid {_peso(amount)}, but the deposit for this booking is {_peso(expected_amount)}.")
    elif amount - expected_amount > AMOUNT_OVER_LIMIT:
        verdict.codes.append("amount_high")
        verdict.notes.append(f"Paid {_peso(amount)}, much more than the deposit of {_peso(expected_amount)}. It may be a typing mistake.")
    elif amount > expected_amount:
        verdict.notes.append(f"Paid {_peso(amount)}; the deposit is {_peso(expected_amount)} (+{_peso(amount - expected_amount)} more than required).")
    fingerprint = _fingerprint(amount, paid_at, recipient)
    verdict.fingerprint = fingerprint
    if fingerprint_used_by:
        verdict.codes.append("same_amount_and_time")
        verdict.notes.append(f"Same amount, minute and recipient as {fingerprint_used_by}. Make sure it is not the same receipt.")

    verdict.amount = amount
    verdict.paid_at = paid_at
    verdict.reference = reading.reference
    blocking = [c for c in verdict.codes if c != "old_number_used"]
    verdict.status = STATUS_NEEDS_CHECK if blocking else STATUS_PASS
    return verdict


# ---- the whole check (with the database) ----------------------------------------------------------------------------------------------
def _mime_for(name):
    name = (name or "").lower()
    if name.endswith(".png"):
        return "image/png"
    if name.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def check_receipt(image_bytes, file_name, *, expected_amount, started_at=None, exclude_reservation_id=None, now=None):
    """Read the cleaned picture and judge it. `expected_amount` is THIS booking's deposit; `started_at` is when the booking was started
    (the checkout hold's start, or the reservation's creation for an upload made later); `exclude_reservation_id` keeps a booking from
    counting as "another" booking when its own proof is being replaced."""
    from gowns.models import SiteSettings
    from reservations.models import Reservation

    now = now or timezone.now()
    image_hash = hashlib.sha256(image_bytes).hexdigest()

    def other(**lookup):
        query = Reservation.objects.filter(**lookup)
        if exclude_reservation_id:
            query = query.exclude(pk=exclude_reservation_id)
        return query.values_list("reference_code", flat=True).first() or ""

    image_used_by = other(proof_hash=image_hash)
    reading, why = (None, "")
    if not image_used_by:
        reading, why = read_receipt(image_bytes, _mime_for(file_name))
        if reading is None:
            logger.info("Receipt not read (%s): sent to staff as needs_check", why)
    reference_used_by = other(gcash_reference=reading.reference) if reading is not None and reading.reference else ""
    fingerprint = _fingerprint(reading.amount, reading.paid_at, reading.recipient_number) if reading is not None else ""
    fingerprint_used_by = other(proof_fingerprint=fingerprint) if fingerprint else ""

    verdict = evaluate(
        reading, expected_amount=expected_amount, started_at=started_at, now=now,
        shop_name=SiteSettings.load().gcash_account_name,
        reference_used_by=reference_used_by, image_used_by=image_used_by, fingerprint_used_by=fingerprint_used_by,
    )
    verdict.image_hash = image_hash
    if reading is not None and not verdict.reference:
        verdict.reference = reading.reference
    return verdict
