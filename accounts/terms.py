"""Customers agree to the Terms and Conditions before they rent (the pop-up in templates/partials/terms_gate.html).

What is stored. UserProfile.terms_accepted_at (a column that has always been there, and that nothing used to fill) holds WHEN the customer
agreed. No new database column is needed -- which matters, because this project's local and live sites share one database.

When the terms change. TERMS_UPDATED_ON is the day the current wording was published (templates/partials/terms_body.html). A customer counts as
having agreed only if they agreed on or after that day, so changing the terms and moving this date makes every customer read and agree once
more. Move it to the day the new wording goes live.

Who has to agree. Customers only: not an anonymous visitor (they sign in first), and not an admin / staff / owner account that happens to
look at the customer site."""
from datetime import date

from django.utils import timezone
from django.utils.formats import date_format

from accounts.models import UserProfile

TERMS_UPDATED_ON = date(2026, 11, 8)


def label():
    """The terms' version as customers and staff read it: 'Nov 8, 2026'."""
    return date_format(TERMS_UPDATED_ON, "M j, Y")


def applies_to(user):
    """True for a signed-in customer account -- the only kind that has to agree."""
    return bool(
        user is not None and user.is_authenticated and user.is_active
        and not user.is_staff and not user.is_superuser
    )


def _agreed_in_time(accepted_at):
    return bool(accepted_at) and timezone.localtime(accepted_at).date() >= TERMS_UPDATED_ON


def has_accepted(user):
    """Whether this account agreed to the CURRENT terms (one small query)."""
    accepted_at = UserProfile.objects.filter(user=user).values_list("terms_accepted_at", flat=True).first()
    return _agreed_in_time(accepted_at)


def needs_to_accept(user):
    """A customer who has not yet agreed to the current terms. False for everyone else, without touching the database."""
    return applies_to(user) and not has_accepted(user)


def record_acceptance(user):
    """Save that this customer agrees now. Someone who already agreed to the current terms keeps their ORIGINAL date and time.
    Returns the moment on record."""
    profile, _ = UserProfile.objects.get_or_create(user=user)
    if _agreed_in_time(profile.terms_accepted_at):
        return profile.terms_accepted_at
    profile.terms_accepted_at = timezone.now()
    profile.save(update_fields=["terms_accepted_at"])
    return profile.terms_accepted_at
