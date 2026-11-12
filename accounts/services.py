"""Account-level business rules that span apps.

Kept out of models.py so `accounts` can reach into `reservations` without
creating an import cycle (reservations.models only imports settings/auth).
"""

from datetime import timedelta

from django.utils import timezone
from django.utils.dateformat import format as format_datetime

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

    A booking staff recorded for a customer with no website account (user is None) has no account to count against.
    """
    if user is None:
        return 0
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


def _plural(number, word):
    return f"{number} {word}{'' if number == 1 else 's'}"


def cancel_lockout_notice(user):
    """`(seconds_left, message)` for an active temporary lockout, or `(0, "")` when there is none.

    The one wording every step uses -- Proceed (reservation_hold_start), the checkout page and Confirm
    Rental (reservation_submit) -- so a locked customer is told why, for how long and until when, instead
    of a vague "try again later". Only ever reads THIS account's own row: a lock never touches anyone else.
    """
    profile = (
        UserProfile.objects.filter(user=user)
        .values('cancel_lockout_until', 'cancel_tier2_lockout_sent')
        .first()
    )
    until = profile['cancel_lockout_until'] if profile else None
    if not until:
        return 0, ""
    seconds = int((until - timezone.now()).total_seconds())
    if seconds <= 0:
        return 0, ""

    # The 2-hour lock is the second step; it can only have been set after the 30-minute one.
    length = (
        _plural(CANCELLATION_LOCKOUT_TIER_2_HOURS, "hour") if profile['cancel_tier2_lockout_sent']
        else _plural(CANCELLATION_LOCKOUT_TIER_1_MINUTES, "minute")
    )
    local_until = timezone.localtime(until)
    when = format_datetime(local_until, "g:i A")
    if local_until.date() != timezone.localdate():
        when = format_datetime(local_until, "M j, g:i A")
    minutes_left = -(-seconds // 60)  # rounded up, so it never says "0 minutes left"
    hours, minutes = divmod(minutes_left, 60)
    left = _plural(minutes, "minute") if not hours else (
        _plural(hours, "hour") + (f" {_plural(minutes, 'minute')}" if minutes else "")
    )
    message = (
        f"You've reached {count_cancellation_attempts(user)} cancelled or abandoned reservations, so new "
        f"reservations are paused for {length}, until {when} (about {left} left). You can still browse "
        f"the gowns in the meantime."
    )
    return seconds, message


# ---- Refused payment proofs ------------------------------------------------------------------------------------------------------
# A customer whose payment proofs keep being refused (by the receipt check at upload, by the staff, or by the owner finding no such payment
# in her GCash) is paused after PROOF_REJECTION_LIMIT of them inside PROOF_REJECTION_WINDOW: the account is flagged for the owner's review
# (the Client List shows it, the customer gets the usual notice) AND cannot start a new reservation until the owner or a manager lifts it
# (customer_flag_view, which also restarts the count). This is separate from the cancellation tiers above: that flag keeps the account
# able to reserve; this one pauses it, because the point is to stop someone who keeps sending fake proofs.
PROOF_REJECTION_LIMIT = 4
PROOF_REJECTION_WINDOW = timedelta(days=7)
PROOF_BLOCK_MESSAGE = (
    "Your account is paused because several of your payment proofs were rejected. Please message the shop on Facebook "
    "so the owner can review it."
)


def count_recent_rejected_proofs(user, now=None) -> int:
    """Proofs refused for this account inside the last PROOF_REJECTION_WINDOW (and after the owner last lifted a pause)."""
    from reservations.models import ProofRejection

    now = now or timezone.now()
    since = now - PROOF_REJECTION_WINDOW
    reset_at = (
        UserProfile.objects.filter(user=user).values_list('proof_block_reset_at', flat=True).first()
    )
    if reset_at and reset_at > since:
        since = reset_at
    return ProofRejection.objects.filter(user=user, created_at__gte=since).count()


def record_rejected_proof(user, *, source, reason='', reservation=None) -> bool:
    """Write one refused proof and pause the account when it reaches the limit. Returns True when the account is NOW paused (or already was).

    A booking staff recorded for a customer with no website account (user is None) has no account to count against."""
    if user is None:
        return False
    from reservations.models import ProofRejection

    ProofRejection.objects.create(user=user, reservation=reservation, source=source, reason=(reason or '')[:200])
    return sync_proof_block(user)


def sync_proof_block(user) -> bool:
    profile, _ = UserProfile.objects.get_or_create(user=user)
    if profile.proof_blocked:
        return True
    count = count_recent_rejected_proofs(user)
    if count < PROOF_REJECTION_LIMIT:
        return False
    profile.proof_blocked = True
    profile.is_flagged = True
    profile.save(update_fields=["proof_blocked", "is_flagged"])
    CustomerMessage.objects.create(
        recipient=user,
        category=CustomerMessage.Category.ACCOUNT_FLAGGED,
        body=(
            f"Your account has been flagged and paused after {count} of your payment proofs were rejected. "
            "You can still browse the collection, but you cannot start a new reservation until the owner reviews your account. "
            "Please message the shop on Facebook. If you think this is a mistake, please contact us."
        ),
    )
    return True


def proof_block_notice(user) -> str:
    """The message for an account that is paused because of refused proofs, or '' when it is not."""
    paused = UserProfile.objects.filter(user=user).values_list('proof_blocked', flat=True).first()
    return PROOF_BLOCK_MESSAGE if paused else ''


def clear_proof_block(profile) -> None:
    """Lift the pause (the owner or a manager un-flagged the account) and start the count again from now."""
    profile.proof_blocked = False
    profile.proof_block_reset_at = timezone.now()
    profile.save(update_fields=["proof_blocked", "proof_block_reset_at"])
