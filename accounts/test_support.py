"""Helpers for tests that book as a customer.

The checkout (the 20-minute hold and Confirm Rental) now refuses a customer who has not agreed to the Terms and Conditions
(accounts/terms.py), exactly as it should. A test that is about something else -- prices, sizes, dates, emails -- signs in a customer who
has already agreed, so it exercises the booking itself."""
from accounts import terms


def agreed(user):
    """The same customer, now on record as having agreed to the current terms. Staff and owner accounts are left alone (they never agree)."""
    if terms.applies_to(user):
        terms.record_acceptance(user)
    return user
