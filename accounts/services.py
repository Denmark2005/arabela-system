"""Account-level business rules that span apps.

Kept out of models.py so `accounts` can reach into `reservations` without
creating an import cycle (reservations.models only imports settings/auth).
"""

from datetime import timedelta

from django.utils import timezone

from .models import CustomerMessage, UserProfile

# Graduated response to repeated cancellations/abandoned holds. Matches the
# published policy in templates/terms_and_conditions.html, templates/faqs.html,
# templates/products.html, and ai_recommendation/views.py's grounding prompt --
# change them together.
#
# Why abandoned holds count the same as cancelling a submitted reservation: the
# P2,000 deposit must be paid before a reservation can be submitted and it isn't
# refundable, so nobody would ever cancel a submitted reservation enough times to
# trip a low threshold on its own -- the counter would rarely fire. The behaviour
# actually worth limiting is repeatedly holding a selection and walking away,
# which costs the customer nothing.
#
# Three steps on one counter, not three separate counters: 5 and 10 are early
# warnings (a temporary lockout on starting a NEW hold/reservation, nothing else
# is restricted), 15 is the ceiling (a permanent flag, unchanged from before).
CANCELLATION_LOCKOUT_TIER_1 = 5
CANCELLATION_LOCKOUT_TIER_1_MINUTES = 30
CANCELLATION_LOCKOUT_TIER_2 = 10
CANCELLATION_LOCKOUT_TIER_2_HOURS = 2
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
    """Auto-escalate an account once it reaches each attempt checkpoint.

    Called after every customer cancellation or abandoned hold. Each tier fires
    exactly once (guarded by its own persisted flag) rather than re-arming on
    every subsequent cancel past that count. Flagging (the top tier) is one-way:
    only an admin can lift it (via the Client List), so a customer can't clear it
    by having reservations approved afterwards. Returns the total attempt count.
    """
    count = count_cancellation_attempts(user)
    profile, _ = UserProfile.objects.get_or_create(user=user)

    if count >= CANCELLATION_FLAG_THRESHOLD:
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

    if count >= CANCELLATION_LOCKOUT_TIER_2 and not profile.cancel_tier2_lockout_sent:
        profile.cancel_tier2_lockout_sent = True
        profile.cancel_lockout_until = timezone.now() + timedelta(hours=CANCELLATION_LOCKOUT_TIER_2_HOURS)
        profile.save(update_fields=["cancel_tier2_lockout_sent", "cancel_lockout_until"])
        CustomerMessage.objects.create(
            recipient=user,
            category=CustomerMessage.Category.CANCELLATION_LOCKOUT,
            body=(
                f"You've now reached {count} cancelled or abandoned reservations. Your "
                f"account is temporarily locked from starting a new reservation for "
                f"{CANCELLATION_LOCKOUT_TIER_2_HOURS} hours. Reaching "
                f"{CANCELLATION_FLAG_THRESHOLD} total will flag your account for staff "
                f"review, so please complete or cancel reservations you intend to keep."
            ),
        )
        return count

    if count >= CANCELLATION_LOCKOUT_TIER_1 and not profile.cancel_tier1_lockout_sent:
        profile.cancel_tier1_lockout_sent = True
        profile.cancel_lockout_until = timezone.now() + timedelta(minutes=CANCELLATION_LOCKOUT_TIER_1_MINUTES)
        profile.save(update_fields=["cancel_tier1_lockout_sent", "cancel_lockout_until"])
        CustomerMessage.objects.create(
            recipient=user,
            category=CustomerMessage.Category.CANCELLATION_LOCKOUT,
            body=(
                f"You've now reached {count} cancelled or abandoned reservations. Your "
                f"account is temporarily locked from starting a new reservation for "
                f"{CANCELLATION_LOCKOUT_TIER_1_MINUTES} minutes. Repeating this can lead "
                f"to a longer lockout, and {CANCELLATION_FLAG_THRESHOLD} total will flag "
                f"your account for staff review."
            ),
        )
        return count

    return count


def get_cancel_lockout_remaining_seconds(user) -> int:
    """Seconds left on a temporary cancellation lockout, or 0 if none is active.

    Reads straight from the DB for the same reason count_abandoned_holds does:
    a cached `user.profile` from earlier in the request could report a stale,
    already-expired (or not-yet-set) value."""
    until = (
        UserProfile.objects.filter(user=user)
        .values_list('cancel_lockout_until', flat=True)
        .first()
    )
    if not until:
        return 0
    return max(int((until - timezone.now()).total_seconds()), 0)
