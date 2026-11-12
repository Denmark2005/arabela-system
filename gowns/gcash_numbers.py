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
"""
import re
from datetime import timedelta

from django.utils import timezone

from gowns.models import GcashNumberHistory, SiteSettings

OVERLAP = timedelta(minutes=10)
GRACE_CHOICES = (0, 24, 48)
DEFAULT_GRACE_HOURS = 48


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


def clean_grace(value):
    """The grace the owner picked, as 0 / 24 / 48 (anything else falls back to the default)."""
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return DEFAULT_GRACE_HOURS
    return hours if hours in GRACE_CHOICES else DEFAULT_GRACE_HOURS


def record_change(old_number, new_number, grace_hours=DEFAULT_GRACE_HOURS, now=None):
    """Write the history for a change of the shop's number. Does nothing when the number did not really change.

    The old number's row is closed at `now` with the chosen grace, and a new open row starts for the new number. A shop that has no
    history yet gets the old number's row created first (valid since the beginning), so the old number is remembered."""
    old_n, new_n = normalize(old_number), normalize(new_number)
    if old_n == new_n:
        return
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
        GcashNumberHistory.objects.create(number=old_n, valid_from=None, valid_until=now, grace_hours=grace)
    if new_n:
        GcashNumberHistory.objects.create(number=new_n, valid_from=now)


def accepted_numbers_at(moment=None):
    """[(number, is_old), ...] for a payment made at `moment` (now when missing). `is_old` is True for a number the shop no longer uses."""
    moment = moment or timezone.now()
    current = normalize(SiteSettings.load().gcash_number)
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
    current = normalize(SiteSettings.load().gcash_number)
    if current:
        numbers.add(current)
    return numbers
