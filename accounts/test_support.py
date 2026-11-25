"""Helpers for tests that book as a customer.

The checkout (the 20-minute hold and Confirm Rental) now refuses a customer who has not agreed to the Terms and Conditions
(accounts/terms.py), exactly as it should. A test that is about something else -- prices, sizes, dates, emails -- signs in a customer who
has already agreed, so it exercises the booking itself."""
from accounts import terms


def id_rule_off():
    """Switches the owner's "valid ID before booking" rule OFF, for a test that is about something else (a walk-in booking's price, dates, receipt, ...). The rule is ON by
    default (it is the shop's rule), and has its own tests (reservations/test_identity.py, gowns/test_identity_customer.py, arabela_admin/test_identity_admin.py)."""
    from gowns.models import SiteSettings
    shop = SiteSettings.load()
    shop.require_id_before_payment = False
    shop.save(update_fields=["require_id_before_payment", "updated_at"])


def agreed(user):
    """The same customer, now on record as having agreed to the current terms. Staff and owner accounts are left alone (they never agree)."""
    if terms.applies_to(user):
        terms.record_acceptance(user)
    return user
