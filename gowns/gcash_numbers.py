"""The shop's GCash number over time, so a receipt is judged against the number that was the shop's WHEN IT WAS PAID.

The owner can change the GCash number (and QR) at any moment. A customer may have opened the QR page, paid the old number and uploaded
the receipt a few minutes after the change -- that is fair and must pass. A payment made to a number long after the owner stopped using it
is not. `accepted_numbers_at(moment)` answers "which numbers count for a payment made at this moment?" from the number history the owner's
changes leave behind (GcashNumberHistory), and `record_change` writes that history when the number is saved.

Rules, all in one place so they cannot disagree:
  * a number counts from 10 minutes BEFORE it became the shop's (clocks on phones are never exact);
  * the number counts until 10 minutes after it stopped being the shop's, plus the grace hours the owner chose for that change
    (0, 24 or 48; the default when she does not say is 48);
  * with no history at all (a shop that never changed it) the number saved in the settings counts from the beginning.

The shop has TWO GCash accounts (the owner's and the manager's, SiteSettings.gcash_* and SiteSettings.manager_gcash_*) and only ONE of them is
active: the one customers are shown. Every history row says whose number it is (`account`), switching the active account is a change of "the
shop's number" exactly like typing a new number (the old one keeps counting for the grace the owner chose), and `account_for_recipient` tells
which account a receipt was paid to -- that decides who has to confirm the payment (reservations/owner_check.py).
"""
import re
from datetime import timedelta

from django.utils import timezone

from gowns.models import GcashNumberHistory, SiteSettings

OVERLAP = timedelta(minutes=10)
GRACE_CHOICES = (0, 24, 48)
DEFAULT_GRACE_HOURS = 48

OWNER = "owner"
MANAGER = "manager"
ACCOUNTS = (OWNER, MANAGER)
ACCOUNT_LABELS = {OWNER: "Owner’s GCash", MANAGER: "Manager’s GCash"}


def clean_account(value, default=OWNER):
    return value if value in ACCOUNTS else default


def active_account(settings_obj=None):
    """Whose GCash customers are shown and pay to right now: "owner" or "manager"."""
    settings_obj = settings_obj or SiteSettings.load()
    return MANAGER if settings_obj.gcash_active == MANAGER else OWNER


def account_details(account, settings_obj=None):
    """{"name", "number", "qr_url"} of one account exactly as saved (the number is not normalised)."""
    s = settings_obj or SiteSettings.load()
    if clean_account(account) == MANAGER:
        return {"name": s.manager_gcash_name, "number": s.manager_gcash_number, "qr_url": s.manager_gcash_qr_url}
    return {"name": s.gcash_account_name, "number": s.gcash_number, "qr_url": s.gcash_qr_url}


def active_details(settings_obj=None):
    """What customers are shown: the active account's name, number and QR."""
    s = settings_obj or SiteSettings.load()
    return account_details(active_account(s), s)


def normalize(raw):
    """A GCash number as 11 digits starting with 09, or '' when it is not one. Accepts 0917 123 4567, +63 917 123 4567, 639171234567 and
    917 123 4567 (the leading 0 left off -- the receipt reader sometimes returns a number that way)."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("63") and len(digits) == 12:
        digits = "0" + digits[2:]
    elif len(digits) == 10 and digits.startswith("9"):
        digits = "0" + digits
    if len(digits) == 11 and digits.startswith("09"):
        return digits
    return ""


# The marks GCash (and a reader that copies it) uses for a digit that is hidden on a receipt: +63 9•••••5992.
HIDDEN_MARKS = "•*●·∙◦○xX?"
MIN_VISIBLE_DIGITS = 4          # fewer visible digits than this cannot tell one number from another


def parse_printed(raw):
    """(full number, pattern) for a recipient number as it is printed on a receipt.

    `full` is the 11-digit number (09...) when every digit shows, else "". `pattern` is 11 characters when only some digits show: the digits that
    show and a "?" for each hidden one ("09?????5992" for "+63 9•••••5992"), or "" when nothing usable shows. When only the END of the number
    shows (••• 5992) the pattern is the end, padded with "?" (that is all that can be compared). Never guesses a hidden digit."""
    text = str(raw or "").strip()
    if not text:
        return "", ""
    s = re.sub(r"[\s\-().,]", "", text)
    s = "".join("?" if ch in HIDDEN_MARKS else ch for ch in s)
    s = s.replace("+", "")
    if not all(ch.isdigit() or ch == "?" for ch in s) or not s:
        return "", ""
    if s.startswith("63") and len(s) == 12:
        s = "0" + s[2:]
    elif len(s) == 10 and s[0] in "9?":
        s = "0" + s                                   # the leading 0 left off
    if len(s) == 11 and s[0] in "0?":
        s = "0" + s[1:]
        if "?" not in s:
            return (s, "") if s.startswith("09") else ("", "")
        if s[1] in "9?" and visible_digits(s) >= MIN_VISIBLE_DIGITS:
            return "", s
        return "", ""
    # some other length: a reader that dropped the dots ("95992"), or a cut-off number. Only the END can be trusted, and only when the number was
    # visibly hidden or short; a long run of plain digits with one missing is just a misread number, which tells nothing.
    if "?" not in s and len(s) >= 8:
        return "", ""
    tail = re.search(r"(\d{%d,})$" % MIN_VISIBLE_DIGITS, s)
    if tail:
        end = tail.group(1)[-MIN_VISIBLE_DIGITS:]
        return "", "?" * (11 - len(end)) + end
    return "", ""


def visible_digits(pattern):
    """How many digits of a number really show. The first character is not counted: every mobile number starts with 0, so it tells nothing."""
    return sum(ch.isdigit() for ch in str(pattern or "")[1:])


def pattern_matches(pattern, number):
    """True when `number` (11 digits) has the same digit wherever `pattern` shows one."""
    pattern, number = str(pattern or ""), str(number or "")
    if len(pattern) != 11 or len(number) != 11:
        return False
    return all(p == "?" or p == n for p, n in zip(pattern, number))


def show_pattern(pattern):
    """A pattern as a customer sees it printed: 09•••••5992."""
    return str(pattern or "").replace("?", "•")


def clean_grace(value):
    """The grace the owner picked, as 0 / 24 / 48 (anything else falls back to the default)."""
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return DEFAULT_GRACE_HOURS
    return hours if hours in GRACE_CHOICES else DEFAULT_GRACE_HOURS


def record_change(old_number, new_number, grace_hours=DEFAULT_GRACE_HOURS, now=None, account=OWNER, old_account=None):
    """Write the history for a change of the shop's number. Does nothing when the number did not really change.

    The old number's row is closed at `now` with the chosen grace, and a new open row starts for the new number. A shop that has no
    history yet gets the old number's row created first (valid since the beginning), so the old number is remembered.
    `account` is whose number the NEW one is, `old_account` whose the OLD one was (the same account when the owner only types a new number;
    the other one when she switches the active account)."""
    old_n, new_n = normalize(old_number), normalize(new_number)
    if old_n == new_n:
        return
    account = clean_account(account)
    old_account = clean_account(old_account, account)
    now = now or timezone.now()
    grace = clean_grace(grace_hours)
    open_rows = list(GcashNumberHistory.objects.filter(valid_until__isnull=True))
    closed_old = False
    for row in open_rows:
        if row.number == old_n:
            row.valid_until = now
            row.grace_hours = grace
            row.save(update_fields=["valid_until", "grace_hours"])
            closed_old = True
        else:
            # Anything else still open is stale (a number that was replaced without its history): close it with no grace.
            row.valid_until = now
            row.grace_hours = 0
            row.save(update_fields=["valid_until", "grace_hours"])
    if old_n and not closed_old:
        GcashNumberHistory.objects.create(number=old_n, account=old_account, valid_from=None, valid_until=now, grace_hours=grace)
    if new_n:
        GcashNumberHistory.objects.create(number=new_n, account=account, valid_from=now)


def accepted_numbers_at(moment=None):
    """[(number, is_old), ...] for a payment made at `moment` (now when missing). `is_old` is True for a number the shop no longer uses."""
    moment = moment or timezone.now()
    current = normalize(active_details()["number"])         # the active account's number: the one customers are shown now
    rows = list(GcashNumberHistory.objects.all())
    if not rows:
        return [(current, False)] if current else []
    accepted = []
    for row in rows:
        starts_ok = row.valid_from is None or moment >= row.valid_from - OVERLAP
        ends_ok = row.valid_until is None or moment <= row.valid_until + OVERLAP + timedelta(hours=row.grace_hours)
        if starts_ok and ends_ok and all(row.number != n for n, _ in accepted):
            accepted.append((row.number, row.valid_until is not None))
    # The number saved in the settings is always the shop's number now, even if its history row is missing (a change made another way).
    if current and all(current != n for n, _ in accepted) and not any(current == r.number for r in rows):
        accepted.append((current, False))
    return accepted


def known_numbers():
    """Every number the shop has ever had (to tell 'a number the shop stopped using' from 'someone else's number')."""
    numbers = {row.number for row in GcashNumberHistory.objects.all()}
    shop = SiteSettings.load()
    for account in ACCOUNTS:
        number = normalize(account_details(account, shop)["number"])
        if number:
            numbers.add(number)
    return numbers


def account_of_number(number):
    """"owner" or "manager": whose GCash `number` is (the latest history row that has it, else the saved settings), or "" when it is neither's."""
    number = normalize(number)
    if not number:
        return ""
    row = GcashNumberHistory.objects.filter(number=number).order_by("-id").first()
    if row is not None:
        return clean_account(row.account)
    shop = SiteSettings.load()
    for account in (MANAGER, OWNER):
        if normalize(account_details(account, shop)["number"]) == number:
            return account
    return ""


def names_by_number():
    """{number: the account name saved for that number's account}, for every number the shop has had -- a receipt's recipient name is compared
    with the name of the account that number belongs to (the owner's name for her number, the manager's for the manager's)."""
    shop = SiteSettings.load()
    names = {}
    for row in GcashNumberHistory.objects.all():
        names.setdefault(row.number, account_details(row.account, shop)["name"])
    for account in ACCOUNTS:
        details = account_details(account, shop)
        number = normalize(details["number"])
        if number:
            names[number] = details["name"]
    return names


def account_for_recipient(shown, moment=None):
    """Which account a receipt was paid to: "owner" or "manager". `shown` is what the receipt prints for the recipient (a full number, or the
    digits that show: 09?????5992). A number nobody can place (or none showing) is taken to be the ACTIVE account's, the one the customer was
    shown."""
    shown = str(shown or "")
    for number, _ in accepted_numbers_at(moment):
        if number == shown or pattern_matches(shown, number):
            found = account_of_number(number)
            if found:
                return found
    return active_account()
