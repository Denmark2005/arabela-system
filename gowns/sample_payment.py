"""Test helper: the reference number and amount a customer types at checkout (gowns/typed_payment.py), for the tests that post a booking.

Every call to typed_reference() gives a NEW 13-digit number, because a reference that is already on another booking is refused. The
amount is higher than any deposit the tests use, so the "at least the deposit" rule never gets in the way of a test about something else."""
import itertools

_counter = itertools.count(1)

TYPED_AMOUNT = "99999"


def typed_reference():
    return f"2026111{next(_counter):06d}"          # 7 + 6 = 13 digits


def typed_fields(**overrides):
    """The two POST fields of a checkout, ready to merge into the request's data."""
    return {"gcash_reference": typed_reference(), "gcash_amount": TYPED_AMOUNT, **overrides}
