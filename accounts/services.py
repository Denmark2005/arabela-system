"""Account-level business rules that span apps.

Kept out of models.py so `accounts` can reach into `reservations` without
creating an import cycle (reservations.models only imports settings/auth).
"""

from .models import CustomerMessage, UserProfile

# How many cancellation attempts a customer may accumulate before the system
# flags their account for admin review. Matches the published policy in
# templates/terms_and_conditions.html, templates/faqs.html, templates/products.html
# and the AI chatbot's grounding prompt -- change them together.
#
# Why 15 and why abandoned holds count: the P2,000 deposit must be paid before a
# reservation can be submitted and it isn't refundable, so nobody would ever
# cancel a submitted reservation enough times to trip a low threshold -- the
# counter would never fire. The behaviour actually worth limiting is repeatedly
# holding a selection and walking away, which costs the customer nothing.
CANCELLATION_FLAG_THRESHOLD = 15


def count_cancellations(user) -> int:
    """Cancelled reservations for this customer.

    Derived from the Reservation rows rather than a stored counter, so it can
    never drift out of sync. Only customer-initiated CANCELLED rows count --
    REJECTED is an admin decision and must not be held against the customer.
    """
    from reservations.models import Reservation

    return Reservation.objects.filter(
        customer=user, status=Reservation.Status.CANCELLED
    ).count()


def count_abandoned_holds(user) -> int:
    """Selections the customer held and walked away from, submitting nothing.

    Reads straight from the DB rather than `user.profile`: Django caches that
    relation on the user instance, so a profile loaded earlier in the same
    request would still report the pre-increment value and the threshold check
    would silently run on a stale number.
    """
    return (
        UserProfile.objects.filter(user=user)
        .values_list('hold_abandon_count', flat=True)
        .first()
        or 0
    )


def count_cancellation_attempts(user) -> int:
    """Everything that counts toward the flag threshold: cancelled reservations
    plus abandoned holds. The first half stays derived so it can't drift; only
    the half with no other record (holds live in the session) is stored."""
    return count_cancellations(user) + count_abandoned_holds(user)


def record_abandoned_hold(user) -> int:
    """Log that a held selection was given up -- pressing Cancel on the countdown
    or letting it lapse -- and re-check the flag threshold.

    Uses an F() expression so two tabs racing can't lose an increment."""
    from django.db.models import F

    profile, _ = UserProfile.objects.get_or_create(user=user)
    UserProfile.objects.filter(pk=profile.pk).update(
        hold_abandon_count=F('hold_abandon_count') + 1
    )
    return sync_cancellation_flag(user)


def sync_cancellation_flag(user) -> int:
    """Auto-flag an account once it reaches the attempt threshold.

    Called after a customer cancels or abandons a hold. Flagging is one-way
    here: only an admin can lift a flag (via the Client List), so a customer
    can't clear it by having reservations approved afterwards. Returns the
    total attempt count.
    """
    count = count_cancellation_attempts(user)
    if count < CANCELLATION_FLAG_THRESHOLD:
        return count

    profile, _ = UserProfile.objects.get_or_create(user=user)
    if profile.is_flagged:
        # Already flagged (manually or by an earlier cancellation) -- don't
        # re-flag or the customer gets a duplicate message on every cancel.
        return count

    profile.is_flagged = True
    profile.save(update_fields=["is_flagged"])

    CustomerMessage.objects.create(
        recipient=user,
        category=CustomerMessage.Category.ACCOUNT_FLAGGED,
        body=(
            f"Your account has been flagged automatically after {count} cancelled "
            f"or abandoned reservations. You can still browse the collection, but "
            f"our staff will review your account before your next reservation is "
            f"confirmed. If you think this is a mistake, please contact us."
        ),
    )
    return count
