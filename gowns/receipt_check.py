"""The receipt check: reads the GCash screenshot a customer uploads as proof of the security deposit and decides, BEFORE any booking exists,
whether it may go on.

Four outcomes (`Verdict.status`):
  * "pass"        -- every check was fine. It still waits for the owner to look in her own GCash: nothing here can talk to GCash.
  * "needs_check" -- could be fine but something is missing or odd (recipient not visible, a different amount, two readings that disagree...).
                     It goes on to the staff and the owner with the reasons written in `Verdict.notes`; the customer is never blocked by it.
  * "retake"      -- the picture cannot be read (blurry, cut off, no reference number). The customer is asked for a better picture. It is NOT a
                     refusal: it never counts against the account.
  * "reject"      -- clearly not a usable proof (a random picture, another app, a payment to someone else, an old or reused receipt...). Only
                     when the picture was sharp enough AND two independent readings agree on the reason. The customer sees `Verdict.message`;
                     nothing is booked and no dates are held.

A real receipt must never be refused because the reader misread it, so a refusal is VERIFIED before it is given:
  1. `read_receipt(...)`   asks Google's Gemini to READ the picture into fields (it only reads; it decides nothing) -> `Reading`.
  2. `evaluate(...)`       applies the rules to a `Reading` and what the database knows -> `Verdict`. A pure function.
  3. When step 2 would refuse (or cannot read the picture), `verify_read(...)` reads the picture a SECOND way (the reader copies every line of
     text and OUR code picks the fields out of it) and the two verdicts are reconciled (`_reconcile`): the same refusal from both, on a sharp
     picture, stands; anything else becomes "needs_check" or "retake".
  `check_receipt(...)` does all of it plus the database lookups (same reference / same picture used before).

GCash prints a receipt in several layouts (Express Send, the white "Sent via GCash" card, the history page "Transfer from ... to ..."), and the
recipient's number may be printed in full or hidden (+63 9•••••5992). The rules compare a hidden number on the digits that show
(gowns/gcash_numbers.py) together with the account name, so a real receipt in any layout matches the shop.
"""
import base64
import hashlib
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.utils import timezone
from PIL import Image

from gowns import gcash_numbers, receipt_quality

logger = logging.getLogger(__name__)

# ---- the rules' numbers (the plan the owner agreed) ----------------------------------------------------------------------------------
MAX_AGE = timedelta(hours=24)               # a receipt older than this is refused
BEFORE_BOOKING_GRACE = timedelta(minutes=10)  # paid up to this long before the booking started still counts as "after it started"
FUTURE_TOLERANCE = timedelta(minutes=10)    # a phone clock a little ahead is normal; more than this is odd
AMOUNT_OVER_LIMIT = Decimal("100")          # up to this many pesos over the deposit passes (with a note); more is "needs check"
REFERENCE_DIGITS = 13                       # a GCash reference number has 13 digits

STATUS_PASS = "pass"
STATUS_NEEDS_CHECK = "needs_check"
STATUS_RETAKE = "retake"
STATUS_REJECT = "reject"
STATUS_TRY_AGAIN = "try_again"      # the reader said "refuse" but a second reading could not be made in time: ask once more instead of booking

# ---- what the customer is told when a receipt is refused or must be taken again ----------------------------------------------------------
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
    "cut_off": "Part of this receipt is missing. Please upload the whole receipt, from the top down to the Ref No. and the date.",
    "try_again": "We couldn't check your receipt just now. Please try again in a moment.",
    "blurry": "We couldn't read this picture clearly. Please upload a sharp screenshot of your GCash receipt, or take a straight, well-lit "
              "photo of the whole screen (no glare, nothing cut off).",
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
    "blurry": "picture not readable", "cut_off": "receipt cut off", "try_again": "could not be checked yet",
}

# Refusals that are database facts (the same picture or reference is already on another booking): no second reading is needed for them.
CERTAIN_CODES = frozenset({"image_used", "reference_used"})
# Refusals that depend on small digits (a date, a phone number). Two readings can make the SAME mistake on a picture that is a little soft (both read
# 2026 as 2024 on a slightly blurred receipt), so these are only given on a sharp picture; otherwise the staff look.
NEEDS_SHARP_PICTURE = frozenset({"too_old", "wrong_recipient", "old_number"})
# Refusals that rest on reading small printed words or digits: on a picture too blurry or tiny to trust they become a retake. "Not a payment" and
# "a list of transactions" do not: when both readings call the picture CLEAR and say it is no receipt, a flat photo is still no receipt (a flat
# picture has few edges, so it measures as "blurry" without being unreadable).
QUALITY_SENSITIVE = NEEDS_SHARP_PICTURE | {"received", "merchant", "failed", "not_gcash"}
# "This picture is not a GCash payment receipt at all", said in different words. The reading that looks at the picture and the reading that copies
# its text do not always pick the same one (a screenshot of the admin page was called "a list of transactions" by one and "no receipt" by the
# other, and the two answers were taken as a disagreement, so the picture was let through). Two refusals from this group AGREE.
NOT_A_RECEIPT = frozenset({"not_payment", "list_screen", "not_gcash"})
# "This is not a payment to the shop", whether the picture is no receipt at all or a real GCash payment to somebody else's biller (a school's tuition
# through GCash GBills, a store). The reading that looks at the picture called such a receipt "a payment to a store or biller" while the reading that
# copies its text, finding no Express Send / Sent via GCash / Transaction Details title on it, called it "no payment receipt": two refusals that were
# taken as a disagreement, so a tuition receipt was let through to the staff (2026-11-18). Two refusals from this group AGREE.
NOT_FOR_THE_SHOP = NOT_A_RECEIPT | {"merchant"}
# A GCash receipt always shows the title of one of its layouts somewhere on it. A picture with none of these is not one, however many numbers,
# amounts and dates it happens to show (an admin page, a bank statement, a chat).
RECEIPT_SIGNS = ("express send", "sent via gcash", "transaction details", "transfer from", "total amount sent")
_RECEIPT_LAYOUTS = ("express_send", "sent_via_gcash", "transaction_details")


def reject_reason_text(code):
    return _REASON_TEXT.get(code, code)


def looks_like_gcash_receipt(reading):
    """True when the reading found the title of one of GCash's receipt layouts (Express Send, Sent via GCash, Transaction Details / Transfer
    from ..., Total Amount Sent)."""
    if reading is None:
        return False
    if reading.layout in _RECEIPT_LAYOUTS:
        return True
    signs = " ".join(reading.gcash_signs).lower()
    return any(sign in signs for sign in RECEIPT_SIGNS)


# ---- what the reader found ------------------------------------------------------------------------------------------------------------
@dataclass
class Reading:
    is_payment_screen: bool = False
    is_transaction_list: bool = False
    app: str = "unknown"                 # gcash | maya | bank | other | unknown
    direction: str = "unknown"           # sent | received | merchant_payment | unknown
    recipient_number: str = ""           # the 11-digit number (09...) when EVERY digit shows on the receipt, else ""
    recipient_name: str = ""
    sender_number: str = ""
    amount: Decimal | None = None
    reference: str = ""                  # digits only
    paid_at: datetime | None = None      # timezone-aware, shop time
    status: str = "unknown"              # completed | failed | pending | unknown
    gcash_signs: list = field(default_factory=list)
    recipient_pattern: str = ""          # when only SOME digits show: 11 characters, "?" for each hidden digit ("09?????5992")
    layout: str = "unknown"              # express_send | sent_via_gcash | transaction_details | other | unknown
    picture: str = "unknown"             # screenshot | photo_of_screen | unknown
    clarity: str = "unknown"             # the reader's own view of the picture: clear | blurry | unreadable | unknown
    cut_off: bool = False                # part of the receipt is missing (no top, no bottom, no reference or date)

    @property
    def recipient_shown(self):
        """What identifies the recipient on this receipt: the full number, or the digits that show."""
        return self.recipient_number or self.recipient_pattern

    def to_json(self):
        return json.dumps({
            "is_payment_screen": self.is_payment_screen, "is_transaction_list": self.is_transaction_list, "app": self.app,
            "direction": self.direction, "recipient_number": self.recipient_number, "recipient_pattern": self.recipient_pattern,
            "recipient_name": self.recipient_name,
            "sender_number": self.sender_number, "amount": str(self.amount) if self.amount is not None else None,
            "reference": self.reference, "paid_at": self.paid_at.isoformat() if self.paid_at else None,
            "status": self.status, "gcash_signs": self.gcash_signs, "layout": self.layout, "picture": self.picture,
            "clarity": self.clarity, "cut_off": self.cut_off,
        })


def reading_from_stored(data):
    """A Reading back from what Reading.to_json wrote (a dict). Unlike reading_from_dict -- which understands the READER'S answer, with its own key
    names -- this is the exact opposite of to_json, so a reading kept for a second try comes back whole (its date included). None when it cannot
    be rebuilt."""
    if not isinstance(data, dict):
        return None
    try:
        paid_at = datetime.fromisoformat(data["paid_at"]) if data.get("paid_at") else None
        if paid_at is not None and timezone.is_naive(paid_at):
            paid_at = timezone.make_aware(paid_at)
        amount = Decimal(data["amount"]) if data.get("amount") is not None else None
        signs = data.get("gcash_signs")
        return Reading(
            is_payment_screen=data.get("is_payment_screen") is True, is_transaction_list=data.get("is_transaction_list") is True,
            app=str(data.get("app") or "unknown"), direction=str(data.get("direction") or "unknown"),
            recipient_number=str(data.get("recipient_number") or ""), recipient_name=str(data.get("recipient_name") or ""),
            sender_number=str(data.get("sender_number") or ""), amount=amount, reference=str(data.get("reference") or ""), paid_at=paid_at,
            status=str(data.get("status") or "unknown"), gcash_signs=[str(x) for x in signs] if isinstance(signs, list) else [],
            recipient_pattern=str(data.get("recipient_pattern") or ""), layout=str(data.get("layout") or "unknown"),
            picture=str(data.get("picture") or "unknown"), clarity=str(data.get("clarity") or "unknown"), cut_off=data.get("cut_off") is True,
        )
    except (ValueError, InvalidOperation, TypeError):
        return None


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


def _choice(value, allowed, default="unknown"):
    text = _as_text(value, 24).lower().replace(" ", "_")
    return text if text in allowed else default


_LAYOUTS = ("express_send", "sent_via_gcash", "transaction_details", "other")
_PICTURES = ("screenshot", "photo_of_screen")
_CLARITIES = ("clear", "blurry", "unreadable")


def reading_from_dict(data):
    """A Reading from the reader's JSON object. Anything missing or odd becomes the "don't know" value, never an exception."""
    if not isinstance(data, dict):
        return None
    printed = data.get("recipient_number_printed") or data.get("recipient_number")
    number, pattern = gcash_numbers.parse_printed(printed)
    if not number and not pattern and data.get("recipient_number") and data.get("recipient_number") != printed:
        number, pattern = gcash_numbers.parse_printed(data.get("recipient_number"))
    sender = gcash_numbers.normalize(data.get("sender_number")) or _digits(data.get("sender_number"))
    signs = data.get("gcash_signs")
    return Reading(
        is_payment_screen=data.get("is_payment_screen") is True,
        is_transaction_list=data.get("is_transaction_list") is True,
        app=_as_text(data.get("app"), 12).lower() or "unknown",
        direction=_as_text(data.get("direction"), 20).lower() or "unknown",
        recipient_number=number,
        recipient_pattern=pattern,
        recipient_name=_as_text(data.get("recipient_name")),
        sender_number=sender,
        amount=_parse_amount(data.get("amount")),
        reference=_digits(data.get("reference")),
        paid_at=_parse_when(data.get("date_time")),
        status=_as_text(data.get("status"), 12).lower() or "unknown",
        gcash_signs=[_as_text(s, 40) for s in signs][:8] if isinstance(signs, list) else [],
        layout=_choice(data.get("layout"), _LAYOUTS),
        picture=_choice(data.get("picture"), _PICTURES),
        clarity=_choice(data.get("clarity"), _CLARITIES),
        cut_off=data.get("cut_off") is True,
    )


# ---- the second reading: every line of text copied, the fields picked out by OUR code ---------------------------------------------------
_MARKS = "•*●·∙"
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_DATE_RE = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4}),?\s+(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp]\.?[Mm]\.?)?")
_REF_RE = re.compile(r"Ref(?:erence)?\.?\s*(?:No\.?|Number|ID|#)?\s*[:#]?\s*((?:\d\s?){10,17})", re.I)
_REF_LOOSE_RE = re.compile(r"\b(\d{4}\s?\d{3}\s?\d{6})\b")
_TOTAL_RE = re.compile(r"Total\s+Amount\s+Sent\s*[:]?\s*[₱P]?\s*([\d,]+(?:\.\d{1,2})?)", re.I)
_AMOUNT_RE = re.compile(r"\bAmount\b\s*[:]?\s*([+\-−–]?)\s*[₱P]?\s*([\d,]+\.\d{2})")
_PESO_RE = re.compile(r"₱\s?([\d,]+\.\d{2})")
_TRANSFER_RE = re.compile(r"Transfer\s+from\s+(\S+)\s+to\s+(\S+)", re.I)
_CARD_NUMBER_RE = re.compile(r"\+\s?63[\s\d•*●·∙xX?\-]{8,18}")
_PLAIN_NUMBER_RE = re.compile(r"(?<![\d+])0\s?9[\d\s\-]{8,12}")
_BANK_RE = re.compile(r"\b(instapay|pesonet|bpi|bdo|unionbank|landbank|metrobank|security bank|rcbc|chinabank|pnb|seabank|gotyme)\b", re.I)


def _transcript_when(flat):
    found = _DATE_RE.search(flat)
    if not found:
        return None
    month = _MONTHS.get(found.group(1)[:3].lower())
    if not month:
        return None
    hour, minute = int(found.group(4)), int(found.group(5))
    marker = (found.group(6) or "").replace(".", "").lower()
    if marker:
        if not 1 <= hour <= 12:
            return None
        hour = (hour % 12) + (12 if marker == "pm" else 0)
    elif hour > 23:
        return None
    return _parse_when(f"{found.group(3)}-{month:02d}-{int(found.group(2)):02d} {hour:02d}:{minute:02d}")


def reading_from_transcript(data):
    """A Reading made ONLY by our own parsing of the lines the reader copied from the picture (no interpretation by the AI). None when the
    reader gave no text at all. This is the second, independent way of reading a receipt (see the module docstring)."""
    if not isinstance(data, dict) or not isinstance(data.get("lines"), list):
        return None
    lines = [_as_text(line, 200) for line in data["lines"] if isinstance(line, (str, int, float))][:80]
    lines = [line for line in lines if line]
    if not lines:
        # The reader answered, and found no text at all in the picture (a photo of a gown or a person): that is an answer, not a failure. It is
        # not a payment screen (unless the reader itself says the picture is unreadable, which makes it a retake).
        return Reading(picture=_choice(data.get("picture"), _PICTURES), clarity=_choice(data.get("clarity"), _CLARITIES),
                       cut_off=data.get("cut_off") is True)
    flat = " ".join(lines)
    low = flat.lower()

    signs = [word for word in ("Express Send", "Sent via GCash", "Transaction Details", "Transfer from", "Ref No.", "Reference Number",
                               "Total Amount Sent") if word.lower() in low]
    if signs or "gcash" in low:
        app = "gcash"
    elif "maya" in low:
        app = "maya"
    elif _BANK_RE.search(low):
        app = "bank"
    else:
        app = "unknown"

    # the reference number
    found_ref = _REF_RE.search(flat)
    reference = _digits(found_ref.group(1))[:REFERENCE_DIGITS] if found_ref else ""
    if not reference:
        loose = _REF_LOOSE_RE.search(flat)
        reference = _digits(loose.group(1)) if loose else ""
    without_ref = flat.replace(found_ref.group(0), " ") if found_ref else flat

    # the amount (and its sign, which tells a history page "sent" from "received")
    sign = ""
    total = _TOTAL_RE.search(flat)
    if total:
        amount = _parse_amount(total.group(1))
    else:
        plain = _AMOUNT_RE.search(flat)
        amount = _parse_amount(plain.group(2)) if plain else None
        sign = plain.group(1) if plain else ""
    if not sign:
        signed = _AMOUNT_RE.search(flat)
        sign = signed.group(1) if signed else ""
    if amount is None:                      # some apps print the amount with no label, only the peso sign
        peso = _PESO_RE.search(flat)
        amount = _parse_amount(peso.group(1)) if peso else None

    # who it was sent to
    sender_text = ""
    recipient_text = ""
    transfer = _TRANSFER_RE.search(flat)
    if transfer:
        sender_text, recipient_text = transfer.group(1), transfer.group(2)
    else:
        card = _CARD_NUMBER_RE.search(without_ref)
        if card:
            recipient_text = card.group(0)
        else:
            plain_number = _PLAIN_NUMBER_RE.search(without_ref)
            recipient_text = plain_number.group(0) if plain_number else ""
    number, pattern = gcash_numbers.parse_printed(recipient_text)
    sender = gcash_numbers.normalize(sender_text)

    # the recipient's name: GCash hides most of it with dots ("DE••••K C.")
    name = ""
    for line in lines:
        if (any(mark in line for mark in _MARKS) and not re.search(r"\d", line) and len(line) <= 40
                and re.search(r"\s[A-Za-z]\.?$", line)):
            name = line
            break

    # what kind of screen and what direction
    if re.search(r"received money from|you received|received via|money received", low):
        direction = "received"
    elif re.search(r"payment to |paid to |bancnet|\bp2m\b|merchant|bills? pay", low):
        direction = "merchant_payment"
    elif transfer:
        direction = "received" if sign == "+" else "sent" if sign in ("-", "−", "–") else "unknown"
    elif "sent via" in low or "express send" in low or "send money" in low:
        direction = "sent"
    else:
        direction = "unknown"
    layout = ("transaction_details" if transfer else "express_send" if "express send" in low else "sent_via_gcash" if "sent via" in low
              else "other")
    paid_at = _transcript_when(flat)
    many_dates = len(_DATE_RE.findall(flat)) >= 3
    status = "failed" if re.search(r"\b(failed|unsuccessful|declined)\b", low) else "completed"
    # A reference, an amount and a date are not enough: an admin page, a bank statement or a chat can show all three. A GCash receipt also shows the
    # title of one of its layouts (RECEIPT_SIGNS), and that is what makes this a payment screen. (A Maya or bank receipt is still read as a payment
    # screen so it is refused as "not GCash", with the message that tells the customer to pay with GCash.)
    titled = any(sign.lower() in RECEIPT_SIGNS for sign in signs) or app in ("maya", "bank")
    return Reading(
        is_payment_screen=bool((reference or paid_at) and amount is not None) and not many_dates and titled,
        is_transaction_list=many_dates,
        app=app, direction=direction, recipient_number=number, recipient_pattern=pattern, recipient_name=name, sender_number=sender,
        amount=amount, reference=reference, paid_at=paid_at, status=status, gcash_signs=signs, layout=layout,
        picture=_choice(data.get("picture"), _PICTURES), clarity=_choice(data.get("clarity"), _CLARITIES), cut_off=data.get("cut_off") is True,
    )


# ---- the reader (Gemini) --------------------------------------------------------------------------------------------------------------
PROMPT = """You read screenshots of phone-wallet payments made in the Philippines. Look ONLY at the picture and answer with ONE JSON object and nothing else.
Copy what is printed; never guess. Use "" (or null for numbers) when something is not visible. The picture may be a camera photo of another phone's screen (tilted, with glare or a visible screen edge): read it the same way.

Keys:
- "is_payment_screen": true ONLY when the picture is a phone screenshot or a photo of a phone screen showing ONE completed payment or transfer (a receipt, or the details page of one transaction). false for anything else: a photo of a person or place, a chat, a form, a bill, and any screenshot of a website, a computer screen or an admin / dashboard page (even when it shows payment details, amounts, reference numbers or a picture of a receipt inside it).
- "is_transaction_list": true when the picture shows a LIST of several transactions instead of one.
- "app": "gcash", "maya", "bank", "other" or "unknown". GCash receipts: a blue GCash screen or a white card with a check mark; words like "Express Send", "Sent via GCash", "Transaction Details", "Ref No." / "Reference Number" with 13 digits; titles like "Transfer from 09... to 09..." or "Payment to ...". Maya has a green "maya" logo, "Sent money to", "Received money from", "Reference ID". Banks show their own name.
- "gcash_signs": a short list of GCash-specific words or logos you can actually see (for example "Express Send", "Sent via GCash", "Transaction Details", "Ref No.").
- "layout": "express_send" (the blue Express Send screen), "sent_via_gcash" (the white card with a check mark and "Sent via GCash"), "transaction_details" (the history page "Transfer from ... to ..."), or "other".
- "picture": "screenshot" or "photo_of_screen" (a camera photo of a screen).
- "clarity": "clear" when you can read every field with confidence; "blurry" when some of the text is hard to read; "unreadable" when you cannot read the reference number or the recipient at all.
- "cut_off": true when part of the receipt is missing (the top, the bottom, the reference number or the date is not in the picture).
- "direction": "sent" when the person SENT money or made a payment to a mobile number; "received" when the screen is about money received (for example "Received money from", or a history page where the amount has a + sign); "merchant_payment" for a payment to a store, biller or merchant (for example "Payment to ...", "Bancnet P2M", "Shopee"); "unknown" otherwise. On a history page "Transfer from A to B" with a minus amount the person SENT money.
- "recipient_number_printed": the number the money was sent TO written EXACTLY as printed: keep the + sign, the spaces and every dot or asterisk that hides a digit (a number printed as +63 9•••••5992 stays +63 9•••••5992). Never fill in a hidden digit. For "Transfer from A to B" it is B. "" when no number is shown.
- "recipient_number": the same number as digits only, written in full with its leading 0 like 09171234567 (+63 917 123 4567 is 09171234567), ONLY when EVERY digit is visible; "" when any digit is hidden.
- "recipient_name": the recipient's name exactly as printed (it may be partly hidden with * or dots), or "".
- "sender_number": the number the money came FROM, digits only, or "".
- "amount": the amount sent in pesos as a plain number (use the "Total Amount Sent" when there is one; no minus sign, no currency sign), or null.
- "reference": the reference number, digits only (remove spaces), or "".
- "date_time": the date and time of the payment as "YYYY-MM-DD HH:MM" in 24-hour time exactly as printed on the screen, or "".
- "status": "completed", "failed", "pending" or "unknown".
"""

PROMPT_TRANSCRIBE = """You read screenshots and camera photos of phone-wallet payment receipts made in the Philippines. Do NOT interpret anything.
Copy ALL the text you can see in the picture, one line at a time from top to bottom, exactly as printed: keep every space, every sign (+ - ₱), all punctuation, and every dot • or asterisk * that hides a character. Never fill in, correct or guess a hidden or unclear character; if a line is hard to read, write what you can see of it.
Answer with ONE JSON object and nothing else:
{"lines": ["first line", "second line", "..."], "picture": "screenshot" or "photo_of_screen", "clarity": "clear" (every line is easy to read) or "blurry" (some text is hard to read) or "unreadable" (the reference number or the recipient cannot be read), "cut_off": true when part of the receipt is missing (no top, no bottom, no reference number or no date), else false}
"""

# The whole check (both readings) must answer in about 15 seconds: the customer waits on a spinner (no words) while it works. A normal receipt
# needs one reading (about 5 to 8 seconds); only a receipt the first reading would refuse gets a second look, and when that cannot finish in time the
# customer is asked to try again (the second try reuses the first reading and gets a fresh 15 seconds) instead of being kept waiting.
READER_TIMEOUT_SECONDS = 15
READER_CUTOFF_SECONDS = 10          # no new attempt starts after this
SECOND_READ_SKIP_AFTER_SECONDS = 7   # the second reading is skipped when the first one already took this long (the page must answer in time)
SECOND_READ_CUTOFF_SECONDS = 12
SECOND_READ_TIMEOUT_SECONDS = 15
# A receipt dated more than this long ago, on a SHARP picture, is refused on ONE reading: a misread digit cannot make a fresh receipt look this old
# unless the picture is soft, so no second reading (and no extra wait) is needed. Between 24 hours and this, two readings must still agree.
SINGLE_READING_OLD_AFTER = timedelta(days=3)
READER_MAX_SIDE = 1800              # a bigger picture is scaled down before it is sent, so it uploads and is read faster
READER_MAX_BYTES = 600_000
SECOND_READ_STRONGER_MODEL_SECONDS = 8  # a model chosen with GEMINI_VERIFY_MODEL gets one try of at most this long, then the usual models take over
RETRYABLE = frozenset({500, 502, 503, 504})
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _models():
    models = [settings.GEMINI_MODEL]
    backup = (getattr(settings, "GEMINI_FALLBACK_MODEL", "") or "").strip()
    if backup and backup != settings.GEMINI_MODEL:
        models.append(backup)
    return models


def _verify_chosen():
    return (getattr(settings, "GEMINI_VERIFY_MODEL", "") or "").strip()


def _verify_models():
    """The models for the second reading. A model the owner chose with GEMINI_VERIFY_MODEL comes first (none by default: the free "stronger" model
    was tried and timed out every time). Then the BACKUP model, so the second reading is made by a different model than the first one and a
    mistake that belongs to one model is not repeated, then the usual one."""
    models = []
    for name in [_verify_chosen(), (getattr(settings, "GEMINI_FALLBACK_MODEL", "") or "").strip(), settings.GEMINI_MODEL]:
        if name and name not in models:
            models.append(name)
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


def _ask_gemini(prompt, image_bytes, mime_type, models, to_reading, *, started, cutoff=READER_CUTOFF_SECONDS, hard_limit=READER_TIMEOUT_SECONDS,
                max_tokens=3072, label="reader", first_model_cap=None):
    """(Reading, "") when the picture was read, else (None, why). Tries each model twice (a 'high demand' answer is retried once). Never raises.

    `first_model_cap` is for the second reading's stronger model, which is often busy or slow on the free tier: it gets ONE try of at most this
    many seconds, and a slow or busy answer simply moves on to the next model, so the page is never kept waiting for it."""
    if not getattr(settings, "RECEIPT_READER_ENABLED", False):
        return None, "disabled"
    if not getattr(settings, "GEMINI_API_KEY", ""):
        return None, "not_configured"
    body = {
        "contents": [{"role": "user", "parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode("ascii")}},
        ]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens, "responseMimeType": "application/json"},
    }
    failure = "network"
    tries = 0
    for position, model in enumerate(models):
        capped = first_model_cap is not None and position == 0
        for attempt in range(1 if capped else 2):
            elapsed = time.monotonic() - started
            if tries and elapsed > cutoff:
                return None, failure
            tries += 1
            allowed = max(hard_limit - elapsed, 3)
            try:
                response = requests.post(
                    GEMINI_URL.format(model=model), params={"key": settings.GEMINI_API_KEY}, json=body,
                    timeout=min(allowed, first_model_cap) if capped else allowed,
                )
            except requests.Timeout:
                logger.warning("Receipt %s %s: timed out", label, model)
                if capped:
                    failure = "timeout"
                    break                   # too slow: the next model gets the time that is left
                return None, "timeout"
            except requests.RequestException as exc:
                logger.warning("Receipt %s %s: connection problem %s", label, model, type(exc).__name__)
                failure = "network"
                time.sleep(1)
                continue
            if response.status_code == 200:
                try:
                    reading = to_reading(_extract_json(response.json()))
                except ValueError:
                    reading = None
                if reading is not None:
                    return reading, ""
                logger.warning("Receipt %s %s: answer could not be understood", label, model)
                failure = "unreadable"
                break                       # the backup model gets a try
            failure = f"http_{response.status_code}"
            logger.warning("Receipt %s %s: HTTP %s", label, model, response.status_code)
            if capped or response.status_code not in RETRYABLE:
                break
            time.sleep(1)
    return None, failure


def read_receipt(image_bytes, mime_type, started=None):
    """(Reading, "") when the picture was read, else (None, why) -- why is a short code for the log: disabled, not_configured, timeout, network,
    http_NNN, unreadable. Never raises: whatever goes wrong here must only mean "staff look at it", never "the customer gets an error"."""
    return _ask_gemini(PROMPT, image_bytes, mime_type, _models(), reading_from_dict, started=started or time.monotonic(), label="reader")


def verify_read(image_bytes, mime_type, started=None):
    """The SECOND reading (see the module docstring): the reader copies every line of text, and our own code picks the fields out of it.
    (Reading, "") or (None, why). Skipped ("no_time") when the first reading already used most of the time the page may take."""
    started = started or time.monotonic()
    if time.monotonic() - started > SECOND_READ_SKIP_AFTER_SECONDS:
        return None, "no_time"
    return _ask_gemini(PROMPT_TRANSCRIBE, image_bytes, mime_type, _verify_models(), reading_from_transcript, started=started,
                       cutoff=SECOND_READ_CUTOFF_SECONDS, hard_limit=SECOND_READ_TIMEOUT_SECONDS, max_tokens=4096, label="verifier",
                       first_model_cap=SECOND_READ_STRONGER_MODEL_SECONDS if _verify_chosen() else None)


# ---- the rules ------------------------------------------------------------------------------------------------------------------------
@dataclass
class Verdict:
    status: str
    codes: list = field(default_factory=list)       # machine reasons ("amount_low", "too_old", ...)
    message: str = ""                               # for the customer, only when rejected or when a better picture is needed
    notes: list = field(default_factory=list)       # for staff and the owner (pass / needs_check)
    reading: Reading | None = None
    old_number: bool = False
    amount: Decimal | None = None
    paid_at: datetime | None = None
    reference: str = ""
    recipient: str = ""
    fingerprint: str = ""
    image_hash: str = ""
    account: str = ""            # whose GCash the receipt was paid to: "owner" or "manager" (gowns/gcash_numbers.account_for_recipient)
    single_reading: bool = False  # refused as clearly too old on ONE sharp reading (SINGLE_READING_OLD_AFTER), without a second reading

    @property
    def rejected(self):
        return self.status == STATUS_REJECT

    @property
    def try_again(self):
        return self.status == STATUS_TRY_AGAIN

    @property
    def retake(self):
        return self.status == STATUS_RETAKE


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


def _match_recipient(full, pattern, accepted, known):
    """How the recipient on the receipt compares with the shop's numbers: (state, number, is_old).

    "match"  -- the number (or the digits that show of a hidden one) is one the shop can be paid on; `is_old` when it is a number the shop
                stopped using but a payment made then still counts.
    "old"    -- it is a number the shop used before and a payment made now no longer counts.
    "other"  -- it is clearly somebody else's number.
    "unset"  -- the shop has no GCash number saved at all, so there is nothing to compare with."""
    if not accepted and not known:
        return "unset", "", False
    if full:
        if full in accepted:
            return "match", full, accepted[full]
        if full in known:
            return "old", full, True
        return "other", "", False
    hits = [n for n in accepted if gcash_numbers.pattern_matches(pattern, n)]
    if hits:
        current = [n for n in hits if not accepted[n]]
        number = (current or hits)[0]
        return "match", number, accepted[number]
    if any(gcash_numbers.pattern_matches(pattern, n) for n in known):
        return "old", "", True
    return "other", "", False


def evaluate(reading, *, expected_amount, started_at=None, now=None, shop_name="", reference_used_by="", image_used_by="",
             fingerprint_used_by="", accepted_at=None, known_numbers=None, names_by_number=None):
    """Apply every rule to what the reader found. Returns a Verdict; never touches the network or the database.

    accepted_at(moment) -> [(number, is_old)] and known_numbers (a set) come from gowns/gcash_numbers.py; they are parameters so a test can
    hand in its own. `names_by_number` ({number: account name}) says which name belongs to which of the shop's TWO GCash accounts: a receipt
    to the manager's number is compared with the manager's account name (without it every receipt is compared with `shop_name`). `reference_used_by` / `image_used_by` / `fingerprint_used_by` are the booking code that already has it ('' = none).

    A verdict of "reject" here is only what ONE reading says; check_receipt confirms it with a second reading before the customer sees it."""
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

    def retake(code):
        verdict.status = STATUS_RETAKE
        verdict.codes = [code]
        verdict.message = MESSAGES[code]
        verdict.notes = []
        return verdict

    if image_used_by:
        return refuse("image_used")

    if reading is None:
        verdict.status = STATUS_NEEDS_CHECK
        verdict.codes.append("reader_unavailable")
        verdict.notes.append("The receipt could not be read automatically, so nothing was checked. Look at the picture yourself.")
        return verdict

    full, pattern = reading.recipient_number, reading.recipient_pattern
    usable_recipient = bool(full) or gcash_numbers.visible_digits(pattern) >= gcash_numbers.MIN_VISIBLE_DIGITS

    # 0. can it be read at all? An unreadable picture is a picture problem, never a fake.
    if reading.clarity == "unreadable" or (reading.clarity == "blurry" and not reading.reference and not usable_recipient):
        return retake("blurry")
    # 1. is it a payment receipt at all?
    if reading.is_transaction_list:
        return refuse("list_screen")
    if not reading.is_payment_screen:
        if reading.clarity == "blurry":
            return retake("blurry")
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
    # 4. the reference number must be on it. A missing one means the picture is cut off or blurry: the customer sends a whole one.
    if not reading.reference:
        return retake("cut_off" if reading.cut_off else "no_reference")

    paid_at = reading.paid_at
    moment = paid_at or now
    # 5. the recipient: the shop's number AT THE TIME OF THE PAYMENT, compared in full, or on the digits that show when GCash hides some
    verdict.recipient = reading.recipient_shown
    name_state = names_match(reading.recipient_name, shop_name)
    if usable_recipient:
        accepted = dict(accepted_at(moment))
        state, number, is_old = _match_recipient(full, pattern, accepted, known_numbers)
        if state == "match":
            if names_by_number and number in names_by_number:
                name_state = names_match(reading.recipient_name, names_by_number[number])      # the name of the account this number belongs to
            if is_old:
                verdict.old_number = True
                verdict.codes.append("old_number_used")
                verdict.notes.append(f"Paid to the shop's OLD GCash number ({number}). Look for it in the old account.")
            if name_state is False:
                verdict.codes.append("name_differs")
                verdict.notes.append(f"The name on the receipt ({reading.recipient_name}) is not the shop's account name. It may have been edited.")
            if not full:
                shown = gcash_numbers.show_pattern(pattern)
                if name_state is None:
                    verdict.codes.append("number_hidden")
                    verdict.notes.append(f"GCash hides part of the recipient's number on this receipt ({shown}) and no name shows, so it was matched "
                                         "on the digits that show only. Check the reference in your GCash.")
                elif name_state is True:
                    verdict.notes.append(f"GCash hides part of the recipient's number on this receipt ({shown}); the digits that show match the shop's "
                                         "number and the name matches.")
        elif state == "old":
            return refuse("old_number")
        elif state == "unset":
            # The shop has no GCash number saved at all, so there is nothing to compare the receipt's number with. That is a settings gap, never
            # the customer's fault: the booking goes through and the staff / owner check it.
            verdict.codes.append("shop_number_not_set")
            verdict.notes.append("The shop's GCash number is not saved in the settings, so the number on the receipt was not checked.")
        else:
            shown = full or gcash_numbers.show_pattern(pattern)
            return _refuse_wrong_recipient(verdict, shown)
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
    fingerprint = _fingerprint(amount, paid_at, reading.recipient_shown)
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


def _refuse_wrong_recipient(verdict, shown):
    verdict.status = STATUS_REJECT
    verdict.codes = ["wrong_recipient"]
    verdict.message = MESSAGES["wrong_recipient"] + (f" The receipt shows {shown}." if shown else "")
    verdict.notes = []
    return verdict


# ---- the whole check (with the database) ----------------------------------------------------------------------------------------------
def _mime_for(name):
    name = (name or "").lower()
    if name.endswith(".png"):
        return "image/png"
    if name.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def _for_reader(image_bytes, mime):
    """(bytes, mime) to SEND to the reader. A big picture is scaled down (the text on a phone screenshot stays sharp at 1800 px) so it uploads and is
    read faster; a small one goes as it is. The stored picture and its hash are never changed. Anything that goes wrong sends the original."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if max(img.size) <= READER_MAX_SIDE and len(image_bytes) <= READER_MAX_BYTES:
            return image_bytes, mime
        img.load()
        if max(img.size) > READER_MAX_SIDE:
            img.thumbnail((READER_MAX_SIDE, READER_MAX_SIDE), Image.LANCZOS)
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            img = flat
        else:
            img = img.convert("RGB")
        buffer = io.BytesIO()
        img.save(buffer, "JPEG", quality=88, optimize=True)
        smaller = buffer.getvalue()
        if len(smaller) >= len(image_bytes):
            return image_bytes, mime
        return smaller, "image/jpeg"
    except Exception:
        return image_bytes, mime


def _fill_from_reading(verdict, reading):
    """A verdict that stopped early (a refusal) has no amount / date / reference yet; fill what the reading did find, for the staff."""
    if reading is None:
        return verdict
    verdict.reference = verdict.reference or reading.reference
    verdict.amount = verdict.amount if verdict.amount is not None else reading.amount
    verdict.paid_at = verdict.paid_at or reading.paid_at
    verdict.recipient = verdict.recipient or reading.recipient_shown
    return verdict


def _needs_check(base, reading, note, code):
    """`base` turned into "needs_check": the customer is not blocked, and the staff see why."""
    result = replace(base, status=STATUS_NEEDS_CHECK, message="", codes=[*base.codes, code] if base.status != STATUS_REJECT else [code],
                     notes=[note, *base.notes])
    return _fill_from_reading(result, reading)


def _reason(verdict):
    code = verdict.codes[0] if verdict.codes else ""
    return reject_reason_text(code) if code else verdict.status


_WHY_WORDS = {
    "no_time": "the first reading had already used the time", "timeout": "Google took too long to answer", "network": "the connection to Google failed",
    "disabled": "the reader is switched off", "not_configured": "the reader has no key", "unreadable": "the answer could not be understood",
}


def _why_words(code):
    """The reason a second reading could not be made, in words the staff and the owner can read (the short code stays in the log)."""
    code = str(code or "")
    if code in _WHY_WORDS:
        return _WHY_WORDS[code]
    if code.startswith("http_"):
        return "Google was busy or refused the request (" + code[5:] + ")"
    return code or "no reason given"


def _reconcile(first, second, second_why, quality, first_reading, ask_again=False):
    """The final verdict from the first reading's refusal (or "cannot read") and the second, independent reading.

    A refusal stands only when both readings give the SAME reason and the picture is sharp enough to trust. Anything else must not cost a real
    customer their booking: it becomes "retake" (the picture cannot be read) or "needs_check" (the staff look).

    `ask_again`: when the first reading would have REFUSED and the second could not be made in time, the customer is asked to try again (nothing
    is booked, nothing counted) instead of the booking going through for the staff; the caller turns it off on the second try, so an honest
    customer is never stuck."""
    if second is None:
        if first.status == STATUS_RETAKE:
            return first
        if ask_again and first.status == STATUS_REJECT:
            return Verdict(status=STATUS_TRY_AGAIN, codes=["check_unfinished"], message=MESSAGES["try_again"], reading=first_reading)
        return _needs_check(first, first_reading,
                            f"The reader would have refused this receipt ({_reason(first)}), but a second reading could not be made "
                            f"({_why_words(second_why)}), so it was sent to the staff to look at.", "second_read_unavailable")
    if first.status == STATUS_REJECT:
        both_say_not_for_the_shop = (second.status == STATUS_REJECT and second.codes and first.codes[0] in NOT_FOR_THE_SHOP
                                     and second.codes[0] in NOT_FOR_THE_SHOP)
        if second.status == STATUS_REJECT and second.codes and (second.codes[0] == first.codes[0] or both_say_not_for_the_shop):
            # "A payment to a store or biller" says more than "no receipt": when only one reading found the biller, the customer is told that.
            agreed = second if (second.codes[0] == "merchant" and first.codes[0] in NOT_A_RECEIPT) else first
            if quality.get("poor") and agreed.codes[0] in QUALITY_SENSITIVE:
                return replace(first, status=STATUS_RETAKE, codes=["blurry"], message=MESSAGES["blurry"], notes=[])
            if agreed.codes[0] in NEEDS_SHARP_PICTURE and quality.get("label") != "sharp":
                return _needs_check(first, first_reading,
                                    f"Both readings say this receipt is not for the shop ({_reason(first)}), but the picture is not sharp enough to be "
                                    "sure of the digits, so it was sent to the staff to look at.", "picture_not_sharp")
            return agreed                                           # confirmed twice on a picture that can be trusted
        if second.status == STATUS_RETAKE and first.codes[0] == "not_payment" and not quality.get("poor"):
            # The first reading found no payment on the picture and the second one could not read anything from it. On a picture that our own
            # measure does not call blurry or tiny, "nothing to read" means there is no receipt in it (a photo of a gown, a wall): refuse it.
            return first
        if second.status == STATUS_RETAKE:
            return replace(first, status=STATUS_RETAKE, codes=[second.codes[0]], message=second.message, notes=[])
        return _needs_check(second if second.status in (STATUS_PASS, STATUS_NEEDS_CHECK) else first, first_reading,
                            f"Two readings of this receipt disagreed: the first would have refused it ({_reason(first)}), the second "
                            f"{'accepted it' if second.status in (STATUS_PASS, STATUS_NEEDS_CHECK) else 'found a different problem (' + _reason(second) + ')'}. "
                            "Look at the picture yourself.", "readings_disagree")
    # the first reading could not read the picture (a retake): a second reading that CAN read it decides
    if second.status in (STATUS_PASS, STATUS_NEEDS_CHECK):
        second.notes = ["The first reading could not read this picture; the second reading did.", *second.notes]
        return second
    if (second.status == STATUS_RETAKE and not quality.get("poor") and _found_nothing(first_reading) and _found_nothing(second.reading)):
        # Neither reading found a single field, a layout title or a payment word on a picture our own measure does not call blurry or tiny: there
        # is nothing to read because there is no receipt in it (a drawing, a photo of a gown). A blurred real receipt always gives the readers
        # SOMETHING (a reference, an amount, a date) or measures as blurry, so it is still asked for again instead.
        return replace(first, status=STATUS_REJECT, codes=["not_payment"], message=MESSAGES["not_payment"], notes=[])
    return first


def _found_nothing(reading):
    """True when a reading found no payment screen, no list, no layout title and not one field (reference, amount, date, recipient)."""
    return (reading is not None and not reading.is_payment_screen and not reading.is_transaction_list and not reading.reference
            and reading.amount is None and reading.paid_at is None and not reading.recipient_shown and not reading.gcash_signs
            and reading.app in ("unknown", ""))


def _confirm_layout(first, second, second_why, quality, first_reading):
    """The first reading accepted a picture but did not find the title of any GCash receipt layout on it (looks_like_gcash_receipt): a random
    screenshot can look like a payment to one reader. The second reading (which copies the text and is judged by our own rules, where a payment
    screen must carry a layout title) has to confirm it. Real receipts are never refused here: only "this is no receipt at all" from the
    second reading stops the picture, and anything unclear goes to the staff with a note."""
    if second is None:
        return _needs_check(first, first_reading,
                            f"This does not look like one of GCash's usual receipt screens, and a second reading could not be made ({_why_words(second_why)}) to "
                            "confirm it. Look at the picture yourself.", "layout_unconfirmed")
    if second.status == STATUS_REJECT and second.codes and second.codes[0] in NOT_A_RECEIPT:
        if quality.get("poor"):
            return replace(first, status=STATUS_RETAKE, codes=["blurry"], message=MESSAGES["blurry"], notes=[])
        return replace(first, status=STATUS_REJECT, codes=["not_payment"], message=MESSAGES["not_payment"], notes=[])
    if second.status in (STATUS_PASS, STATUS_NEEDS_CHECK):
        return first                                    # the second reading found a GCash layout title on it: it is a receipt
    return _needs_check(first, first_reading,
                        "This does not look like one of GCash's usual receipt screens, and a second reading did not confirm it "
                        f"({_reason(second)}). Look at the picture yourself.", "layout_unconfirmed")


def check_receipt(image_bytes, file_name, *, expected_amount, started_at=None, exclude_reservation_id=None, now=None,
                  single_old_ok=False, ask_again_when_unsure=False, prior_first_reading=None, final_try=False):
    """Read the cleaned picture and judge it. `expected_amount` is THIS booking's deposit; `started_at` is when the booking was started
    (the checkout hold's start, or the reservation's creation for an upload made later); `exclude_reservation_id` keeps a booking from
    counting as "another" booking when its own proof is being replaced.

    The site passes four more things (the defaults keep the older behaviour, so the rules can still be tested one by one):
      * `single_old_ok`: a receipt dated more than SINGLE_READING_OLD_AFTER ago, on a sharp picture, is refused on the FIRST reading alone.
      * `ask_again_when_unsure`: a refusal the second reading could not confirm in time becomes STATUS_TRY_AGAIN (see _reconcile) ...
      * `final_try`: ... except on the customer's second try, when it goes to the staff as before.
      * `prior_first_reading`: the first reading of this same picture from the earlier try, so it is not read (or waited for) twice."""
    from reservations.models import Reservation

    began = time.monotonic()
    now = now or timezone.now()
    image_hash = hashlib.sha256(image_bytes).hexdigest()
    mime = _mime_for(file_name)
    shop_name = gcash_numbers.active_details()["name"]          # the account customers are shown (the receipt's own account's name wins in evaluate)
    account_names = gcash_numbers.names_by_number()

    def other(**lookup):
        query = Reservation.objects.filter(**lookup)
        if exclude_reservation_id:
            query = query.exclude(pk=exclude_reservation_id)
        return query.values_list("reference_code", flat=True).first() or ""

    def judge(reading, image_used_by=""):
        reference_used_by = other(gcash_reference=reading.reference) if reading is not None and reading.reference else ""
        fingerprint = _fingerprint(reading.amount, reading.paid_at, reading.recipient_shown) if reading is not None else ""
        fingerprint_used_by = other(proof_fingerprint=fingerprint) if fingerprint else ""
        return evaluate(
            reading, expected_amount=expected_amount, started_at=started_at, now=now, shop_name=shop_name,
            reference_used_by=reference_used_by, image_used_by=image_used_by, fingerprint_used_by=fingerprint_used_by,
            names_by_number=account_names,
        )

    image_used_by = other(proof_hash=image_hash)
    reading, why = None, ""
    sent_bytes, sent_mime = _for_reader(image_bytes, mime)
    if not image_used_by:
        if prior_first_reading is not None:
            reading = prior_first_reading                      # the same picture, already read on the first try
        else:
            reading, why = read_receipt(sent_bytes, sent_mime)
            if reading is None:
                logger.info("Receipt not read (%s): sent to staff as needs_check", why)
    verdict = judge(reading, image_used_by)

    # A receipt clearly too old (days, not hours) on a SHARP picture needs no second reading: refuse it now.
    one_reading = False
    if (single_old_ok and verdict.status == STATUS_REJECT and verdict.codes[:1] == ["too_old"] and reading is not None
            and reading.paid_at is not None and now - reading.paid_at > SINGLE_READING_OLD_AFTER
            and receipt_quality.picture_quality(image_bytes).get("label") == "sharp"):
        one_reading = True
        verdict.single_reading = True

    # A refusal (or "cannot read it") from ONE reading is never final: a real receipt must not be lost to a misread. Read it a second way first.
    # The same goes for a picture the first reading ACCEPTED without finding the title of any GCash receipt layout on it.
    refused_or_unread = (verdict.status in (STATUS_REJECT, STATUS_RETAKE) and verdict.codes and verdict.codes[0] not in CERTAIN_CODES
                         and not one_reading)
    unusual = (verdict.status in (STATUS_PASS, STATUS_NEEDS_CHECK) and reading is not None and not image_used_by
               and not looks_like_gcash_receipt(reading))
    if refused_or_unread or unusual:
        quality = receipt_quality.picture_quality(image_bytes)
        second_reading, second_why = verify_read(sent_bytes, sent_mime, started=began)
        second = judge(second_reading) if second_reading is not None else None
        first = verdict
        if unusual:
            verdict = _confirm_layout(first, second, second_why, quality, reading)
        else:
            verdict = _reconcile(first, second, second_why, quality, reading, ask_again=ask_again_when_unsure and not final_try)
        logger.warning(
            "Receipt check: first reading %s %s -> final %s %s | second reading %s%s | picture %s | first=%s second=%s",
            first.status, first.codes, verdict.status, verdict.codes, second.status if second else "none", f" {second.codes}" if second else f" ({second_why})",
            quality, reading.to_json()[:900] if reading is not None else None, second_reading.to_json()[:900] if second_reading is not None else None)
    verdict.image_hash = image_hash
    if reading is not None and not verdict.reference:
        verdict.reference = reading.reference
    # Whose GCash was paid decides who confirms the payment (the owner for hers, the manager for the manager's).
    shown = (verdict.recipient or (reading.recipient_shown if reading is not None else ""))
    verdict.account = gcash_numbers.account_for_recipient(shown, verdict.paid_at or now)
    return verdict
