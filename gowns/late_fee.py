"""The late-return fee the owner charges for EACH gown, per day late -- ONE setting, like the security deposit (gowns/deposit.py).

`SiteSettings.late_fee_per_day` (whole pesos, 0 to 10,000, default 200; 0 means no late fee) is what a NEW booking is made with
(Reservation.late_fee_per_day), what the Payments page and the receipts use to work out a late fee, and what the Terms and FAQ say
(they still say 200 until the owner changes it and the wording is updated). A booking keeps the rate it was made with.
"""
from decimal import Decimal

DEFAULT_PER_DAY = 200
MIN_PER_DAY = 0
MAX_PER_DAY = 10000


def clean(value):
    """The fee as a whole number inside the allowed range, or None when it isn't one (the owner typed 1.5, -3, 'abc'...)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if value != int(value):
            return None
        value = int(value)
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            return None
        value = int(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text.isdigit():
            return None
        value = int(text)
    if not isinstance(value, int):
        return None
    if value < MIN_PER_DAY or value > MAX_PER_DAY:
        return None
    return value


def per_day():
    """The late fee per gown per day as an int. Falls back to the default if the stored value is somehow outside the allowed range."""
    from gowns.models import SiteSettings

    stored = clean(SiteSettings.load().late_fee_per_day)
    return stored if stored is not None else DEFAULT_PER_DAY
