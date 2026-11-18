"""What the customer types about their GCash payment, and how it is checked against the receipt picture (2026-11-18).

Why it exists. The receipt reader (gowns/receipt_check.py) only had the picture to go on, so a misread digit or a receipt that belongs to some
other booking looked exactly like a good one. Now the customer also types two things from their own GCash app, and the server compares them
with what the reader found:

  * the reference number (the 13-digit "Ref No."), and
  * the amount they paid.

The date is NOT typed: the reader already finds it and the receipt check already judges it (24 hours, paid before the booking started).
The deposit that is due, the shop's GCash number and name and the time the booking started are never typed or sent by the browser: the server
already knows them.

The rules, in the order they are used (gowns/views.py):

  1. FORMAT, no database: the reference must be exactly 13 digits (spaces and dashes are ignored; a GCash bill payment has 9, so it is turned
     away at the typing); the amount must be a number and at least the deposit that is due. A customer who fixes the box is never penalised.
  2. DUPLICATE, one cheap query, BEFORE the picture is read: a reference already on another booking is refused at once (counted like any
     refused receipt), so no time and no reader quota is spent on it.
  3. COMPARE, after the picture is read: the typed reference and amount must equal what the reader found. The first time they differ the
     customer is asked to look again (nothing is booked, nothing is counted against the account). If they still differ on the next try the
     booking goes through as "Needs check" with a note for the staff and the owner: the reader can misread a digit, and an honest customer
     must never be stuck because of it. A field the reader could not find is not a difference.

Nothing here is ever more lenient than before: a typed value can only add a check, never remove one.
"""
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from gowns import receipt_check

REFERENCE_DIGITS = receipt_check.REFERENCE_DIGITS
MAX_AMOUNT = Decimal("1000000")

# What the typed boxes and the reader can disagree about, as stored on the booking (Reservation.proof_match).
MATCH = "match"
REFERENCE_DIFFERS = "reference"
AMOUNT_DIFFERS = "amount"
BOTH_DIFFER = "both"
NOT_COMPARED = ""

SESSION_KEY = "typed_payment_attempts"


def peso(amount):
    amount = Decimal(amount)
    return f"₱{amount:,.0f}" if amount == amount.to_integral_value() else f"₱{amount:,.2f}"


def clean_reference(text):
    """The digits of whatever was typed or pasted (GCash prints 5046 933 479702; people type spaces, dashes, a '#')."""
    return re.sub(r"\D", "", unicodedata.normalize("NFKC", str(text or "")))


def format_reference(digits):
    """5046933479702 -> '5046 933 479702', the way GCash prints it (anything that is not 13 digits is returned as it is)."""
    digits = clean_reference(digits)
    if len(digits) != REFERENCE_DIGITS:
        return digits
    return f"{digits[:4]} {digits[4:7]} {digits[7:]}"


def reference_problem(text):
    """'' when the typed reference can be a GCash reference, else the sentence to show under the box."""
    digits = clean_reference(text)
    if not digits:
        return f"Please type the {REFERENCE_DIGITS}-digit reference number from your GCash receipt."
    if len(digits) != REFERENCE_DIGITS:
        return (f"A GCash reference number has {REFERENCE_DIGITS} digits, and you typed {len(digits)}. "
                "Please check the Ref No. on your receipt.")
    return ""


def parse_amount(text):
    """The typed amount as a Decimal with two places, or None when it is not a plain amount (2000, 2,000, ₱2,000.00, PHP 2000.50)."""
    cleaned = unicodedata.normalize("NFKC", str(text or "")).strip()
    cleaned = re.sub(r"^(?:php|p|₱)\s*", "", cleaned, flags=re.I).replace(",", "").replace(" ", "")
    if not re.fullmatch(r"\d{1,9}(?:\.\d{1,2})?", cleaned):
        return None
    try:
        amount = Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return amount if 0 < amount <= MAX_AMOUNT else None


def amount_problem(text, due):
    """'' when the typed amount is a real amount of at least the deposit that is due, else the sentence to show under the box."""
    if not str(text or "").strip():
        return "Please type the amount you paid."
    amount = parse_amount(text)
    if amount is None:
        return "Please type the amount as numbers only, like 2000 or 2,000.00."
    if amount < Decimal(due):
        return (f"You typed {peso(amount)}, but the deposit for this booking is {peso(due)}. "
                "Please check the amount you paid.")
    return ""


def compare(typed_reference, typed_amount, verdict):
    """MATCH / REFERENCE_DIFFERS / AMOUNT_DIFFERS / BOTH_DIFFER for the typed values against what the reader found (the verdict of
    gowns.receipt_check.check_receipt). A field the reader could not find is left out of the comparison: that is not a difference."""
    reference_differs = bool(verdict.reference) and clean_reference(verdict.reference) != clean_reference(typed_reference)
    amount_differs = verdict.amount is not None and Decimal(verdict.amount).quantize(Decimal("0.01")) != Decimal(typed_amount).quantize(Decimal("0.01"))
    if reference_differs and amount_differs:
        return BOTH_DIFFER
    if reference_differs:
        return REFERENCE_DIFFERS
    if amount_differs:
        return AMOUNT_DIFFERS
    return MATCH


def mismatch_message(kind):
    """What the customer is told the first time the typed values and the receipt differ (never what the receipt reads)."""
    if kind == BOTH_DIFFER:
        return ("The reference number and the amount you typed don't match your receipt. "
                "Please check both against the receipt and try again.")
    if kind == REFERENCE_DIFFERS:
        return "The reference number you typed doesn't match your receipt. Please check the Ref No. on your receipt and try again."
    return "The amount you typed doesn't match your receipt. Please check the amount on your receipt and try again."


def mismatch_field(kind):
    return {REFERENCE_DIFFERS: "reference", AMOUNT_DIFFERS: "amount", BOTH_DIFFER: "both"}.get(kind, "")


def mismatch_note(kind, typed_reference, typed_amount, verdict):
    """The line the staff and the owner read when a booking goes through although the typed values differ from the receipt."""
    typed = f"the reference {format_reference(typed_reference)} and {peso(typed_amount)}"
    read = []
    if verdict.reference:
        read.append(f"the reference {format_reference(verdict.reference)}")
    if verdict.amount is not None:
        read.append(peso(verdict.amount))
    what = {REFERENCE_DIFFERS: "reference number", AMOUNT_DIFFERS: "amount", BOTH_DIFFER: "reference number and amount"}[kind]
    return (f"The customer typed {typed}, but the receipt reads {' and '.join(read) or 'something else'}. "
            f"The {what} typed does not match the picture after two tries: look at the receipt yourself.")


# ---- the first try / second try memory -------------------------------------------------------------------------------------------------
def tries(session, key):
    """How many times the typed values already differed from the receipt for this checkout (or this upload)."""
    data = session.get(SESSION_KEY)
    return int(data.get(key, 0)) if isinstance(data, dict) else 0


def remember_difference(session, key):
    data = session.get(SESSION_KEY)
    data = dict(data) if isinstance(data, dict) else {}
    data[key] = int(data.get(key, 0)) + 1
    session[SESSION_KEY] = data
    return data[key]


def forget(session, key):
    data = session.get(SESSION_KEY)
    if isinstance(data, dict) and key in data:
        data = {k: v for k, v in data.items() if k != key}
        session[SESSION_KEY] = data
