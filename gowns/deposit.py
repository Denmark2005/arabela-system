"""The security deposit the owner asks for each gown -- ONE setting, read everywhere.

`SiteSettings.security_deposit_per_gown` (whole pesos, 1 to 10,000, default 2,000) is what the checkout charges, what the walk-in booking
adds, what the bag / selection / checkout pages show, what the FAQ, How It Works and Terms say, and what the AI chat tells customers.
A booking keeps the deposit it was made with (Reservation.security_deposit), so changing the setting only affects NEW bookings.
"""
from decimal import Decimal

DEFAULT_PER_GOWN = 2000
MIN_PER_GOWN = 1
MAX_PER_GOWN = 10000


def clean(value):
    """The deposit as a whole number inside the allowed range, or None when it isn't one (the owner typed 0, 1.5, -3, 'abc'...)."""
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
    if value < MIN_PER_GOWN or value > MAX_PER_GOWN:
        return None
    return value


def per_gown():
    """The deposit per gown as an int. Falls back to the default if the stored value is somehow outside the allowed range."""
    from gowns.models import SiteSettings

    stored = clean(SiteSettings.load().security_deposit_per_gown)
    return stored if stored is not None else DEFAULT_PER_GOWN


def per_gown_decimal():
    return Decimal(per_gown())


def for_gowns(count):
    """The deposit for `count` gowns, as a Decimal (what is saved on a booking)."""
    return per_gown_decimal() * int(count)


def php_text(amount):
    """'Php 2,000.00' -- how the FAQ, How It Works, Terms and booking page write the deposit."""
    return f"Php {Decimal(amount):,.2f}"


def php_short(amount):
    """'Php 2,000' -- the shorter form the Terms summary uses."""
    return f"Php {int(amount):,}"


def peso_sign(amount):
    """'P2,000' -- how the AI chat's instructions write an amount."""
    return f"P{int(amount):,}"
