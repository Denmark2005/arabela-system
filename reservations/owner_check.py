"""The owner's own look at her GCash, the step no software here can do.

The shop's GCash account belongs to the owner alone, so the staff who approve a booking cannot see whether the money really arrived. The
receipt check (gowns/receipt_check.py) can only read the picture; this is the check against the real thing:

  * every online booking with a GCash proof starts with `owner_check = "pending"` and shows up in the owner's bell (arabela_admin/notifications.py);
  * she opens it, looks for the payment in her GCash and presses  Received  or  Not found  (arabela_admin.views.reservation_owner_check_view,
    owner-only, enforced on the server);
  * Not found cancels the booking (when nothing has gone out yet), frees the gowns, tells the customer, and counts as a rejected proof;
  * a payment still waiting 12 hours after the booking was made gets ONE reminder email to the owner (a single email lists every one).

The shop has TWO GCash accounts, and the rule follows the account the customer actually PAID (`Reservation.proof_account`, worked out from the
receipt), not the one that is active now:
  * paid to the OWNER's GCash  -> staff cannot approve until she has pressed Received (`blocks_approval`); the owner's own Approve counts as Received;
  * paid to the MANAGER's GCash -> staff approve as usual (the manager is in the shop with that phone); the manager who holds it, or the owner, may
                                  still press Received. These bookings never show on the owner's list or in her reminder email;
  * the manager's account out of service (no holder, or the holder's account is switched off) -> its unconfirmed payments fall back to the owner;
  * Rejecting is never held back, a booking with no `proof_account` (a walk-in, or one made before this existed) is never held back.
"""
import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomerMessage, UserProfile
from accounts.services import record_rejected_proof
from gowns import gcash_numbers
from gowns.models import SiteSettings

from .cancellation import _release_gowns
from .models import ProofRejection, Reservation, ReservationItem, ReservationStatusEvent

logger = logging.getLogger(__name__)

REMINDER_AFTER = timedelta(hours=12)
NOT_FOUND_NOTE = (
    "The shop could not find your payment in its GCash account, so this booking was cancelled. "
    "If you already paid, please message the shop on Facebook with your receipt."
)
_REMINDER_THROTTLE_SECONDS = 300
_REMINDER_KEY = "arabela:owner-check-reminder:last-run"


# ---- who has to confirm which payment --------------------------------------------------------------------------------------------------
def manager_in_service(settings_obj=None):
    """True while the manager's GCash account can be trusted to be looked at: it has a valid number AND the staff account that holds it is
    active. Out of service, its unconfirmed payments go back to the owner (so nobody can approve a payment nobody is checking)."""
    shop = settings_obj or SiteSettings.load()
    holder = shop.manager_gcash_holder
    return bool(gcash_numbers.normalize(shop.manager_gcash_number) and holder is not None and holder.is_active)


def owner_must_confirm(reservation, in_service=None):
    """True when this booking's payment still waits for the OWNER's check (whatever the booking's status): the customer paid her GCash, or the
    manager's account it was paid to is out of service. A walk-in or an older booking (no `proof_account`) is never held for her."""
    if reservation.owner_check != Reservation.OwnerCheck.PENDING:
        return False
    if reservation.proof_account == Reservation.ProofAccount.OWNER:
        return True
    if reservation.proof_account == Reservation.ProofAccount.MANAGER:
        return not (manager_in_service() if in_service is None else in_service)
    return False


def blocks_approval(reservation, in_service=None):
    """True when the staff must NOT approve this booking yet: it is Pending and `owner_must_confirm`. `in_service` lets a list work it out once
    for all rows."""
    return reservation.status == Reservation.Status.PENDING and owner_must_confirm(reservation, in_service)


def waiting_filter():
    """The same rule as a database filter (Q), so Approve can refuse in the very UPDATE that claims the booking."""
    q = Q(proof_account=Reservation.ProofAccount.OWNER, owner_check=Reservation.OwnerCheck.PENDING)
    if not manager_in_service():
        q |= Q(proof_account=Reservation.ProofAccount.MANAGER, owner_check=Reservation.OwnerCheck.PENDING)
    return q


def annotate_waiting(reservations):
    """Set `_waiting_for_owner` on every row (Reservation.waiting_for_owner reads it) with ONE look at the manager's account, and return the rows
    as a list. Call it on every list a page shows with an Approve button."""
    rows = list(reservations)
    in_service = manager_in_service() if any(r.proof_account == Reservation.ProofAccount.MANAGER for r in rows) else True
    for row in rows:
        row._waiting_for_owner = blocks_approval(row, in_service)
    return rows


def holds_manager_account(user, shop=None):
    """True when `user` is the staff account that holds the manager's GCash (and it is in service)."""
    shop = shop or SiteSettings.load()
    return bool(user is not None and getattr(user, "pk", None) and user.pk == shop.manager_gcash_holder_id and manager_in_service(shop))


def can_answer(user, reservation, *, is_owner, shop=None):
    """May `user` press Received / Not found on this booking? The owner always; the manager who holds the manager's GCash for a payment made to it."""
    if is_owner:
        return True
    return reservation.proof_account == Reservation.ProofAccount.MANAGER and holds_manager_account(user, shop)


def awaiting():
    """The bookings waiting for the owner to look in her GCash, oldest first. A booking that was rejected, released or cancelled is not on her
    list: its gowns are free already, so there is nothing left for her check to protect. A payment made to the MANAGER's GCash is not hers to
    check (while that account is in service), so it never reaches her list, her bell or her reminder email."""
    rows = (
        Reservation.objects.filter(owner_check=Reservation.OwnerCheck.PENDING)
        .exclude(status__in=[Reservation.Status.REJECTED, Reservation.Status.CANCELLED])
    )
    if manager_in_service():
        rows = rows.exclude(proof_account=Reservation.ProofAccount.MANAGER)
    return rows.order_by("created_at")


def _gown_names(items):
    names = [item.gown_name for item in items]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _live_items(reservation):
    items = list(reservation.items.select_related("gown"))
    return [i for i in items if i.stage != ReservationItem.Stage.CANCELLED] or items


def nothing_went_out(reservation):
    """True when no gown of this booking has left the shop (so the booking can still be cancelled without anything to settle)."""
    if reservation.status not in (Reservation.Status.PENDING, Reservation.Status.CONFIRMED):
        return False
    live = [i for i in reservation.items.all() if i.stage != ReservationItem.Stage.CANCELLED]
    return all(i.stage == ReservationItem.Stage.PICKUP for i in live)


def confirm_received(reservation, user, now=None, *, by_approving=False):
    """The owner (or the manager who holds the manager's GCash) found the payment. The row must already be locked and in the 'pending' state (the
    view checks both). `by_approving` is the owner pressing Approve on a booking that was waiting for her: that counts as Received."""
    now = now or timezone.now()
    reservation.owner_check = Reservation.OwnerCheck.RECEIVED
    reservation.owner_checked_at = now
    reservation.owner_checked_by = user
    reservation.save(update_fields=["owner_check", "owner_checked_at", "owner_checked_by", "updated_at"])
    if reservation.proof_account == Reservation.ProofAccount.MANAGER:
        title, detail = "Payment confirmed in the manager's GCash", "This payment was found in the manager's GCash account."
    else:
        title = "Payment confirmed by the owner"
        detail = ("The owner approved this booking, which confirms the payment in her GCash account." if by_approving
                  else "The owner found this payment in the shop's GCash account.")
    ReservationStatusEvent.record(reservation, title, detail=detail, actor=ReservationStatusEvent.Actor.STAFF, staff_only=True)


def mark_not_found(reservation, user, now=None):
    """The owner could not find the payment. Returns "cancelled" when the booking was cancelled and its gowns freed, or "left_as_is" when a
    gown had already gone out (then only the owner's answer is recorded and the shop settles it in person)."""
    now = now or timezone.now()
    reservation.owner_check = Reservation.OwnerCheck.NOT_FOUND
    reservation.owner_checked_at = now
    reservation.owner_checked_by = user
    changed = ["owner_check", "owner_checked_at", "owner_checked_by", "updated_at"]
    outcome = "left_as_is"
    if nothing_went_out(reservation):
        outcome = "cancelled"
        reservation.status = Reservation.Status.REJECTED
        reservation.reviewed_at = now
        reservation.notes = NOT_FOUND_NOTE
        changed += ["status", "reviewed_at", "notes"]
    reservation.save(update_fields=changed)
    ReservationStatusEvent.record(
        reservation, "Owner: payment not found",
        detail="The owner could not find this payment in the shop's GCash account.",
        actor=ReservationStatusEvent.Actor.STAFF, staff_only=True,
    )
    if outcome == "cancelled":
        _release_gowns(_live_items(reservation))
        ReservationStatusEvent.record(
            reservation, "Reservation cancelled",
            detail="The shop could not find your payment, so the booking was cancelled and the gowns were released.",
            actor=ReservationStatusEvent.Actor.STAFF,
        )
        if reservation.has_account:
            CustomerMessage.objects.create(
                recipient=reservation.customer,
                category=CustomerMessage.Category.GENERAL,
                body=(
                    f"Your reservation {reservation.reference_code} ({_gown_names(_live_items(reservation))}) was cancelled because the shop "
                    "could not find your payment in its GCash account. If you already paid, please message the shop on Facebook "
                    "with your receipt."
                ),
            )
    if reservation.has_account:
        record_rejected_proof(
            reservation.customer, source=ProofRejection.Source.OWNER, reason="payment not found in the shop's GCash", reservation=reservation,
        )
    return outcome


# ---- the 12-hour reminder to the owner ---------------------------------------------------------------------------------------------
def _owner_accounts():
    User = get_user_model()
    return User.objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(profile__role=UserProfile.Role.OWNER)
    ).distinct()


def due_for_reminder(now=None):
    now = now or timezone.now()
    return awaiting().filter(created_at__lte=now - REMINDER_AFTER, owner_reminder_sent_at__isnull=True)


def send_due_reminders(now=None):
    """One email to the owner listing every payment that has waited more than 12 hours for her check. Returns how many bookings it listed
    (0 when nothing was due or nobody could be emailed). Never raises: an email problem must not break the page that triggered it."""
    from accounts import customer_emails as emails

    now = now or timezone.now()
    due = list(due_for_reminder(now).select_related("customer__profile")[:50])
    if not due:
        return 0
    addresses = []
    for user in _owner_accounts():
        address = emails._deliverable_address(user)
        if address and address not in [a for a, _ in addresses]:
            addresses.append((address, emails._account_first_name(user)))
    lines = []
    for reservation in due[:10]:
        lines.append(
            f"{reservation.reference_code}: {reservation.display_customer_name}, "
            f"₱{reservation.security_deposit:,.0f} deposit, waiting since "
            f"{timezone.localtime(reservation.created_at).strftime('%b %d, %I:%M %p').replace(' 0', ' ')}."
        )
    if len(due) > 10:
        lines.append(f"...and {len(due) - 10} more.")
    count = len(due)
    plural = "payment" if count == 1 else "payments"
    content = {
        "subject": f"{count} {plural} waiting for your GCash check",
        "heading": f"{count} {plural} to check in your GCash",
        "preheader": "Open each one, find the payment in your GCash, and press Received or Not found.",
        "paragraphs": [
            f"{count} customer {plural} have waited more than 12 hours for you to look in your GCash. Staff cannot see your GCash account, "
            "so only you can confirm that the money arrived. Open each one, find the payment, and press Received or Not found.",
            *lines,
        ],
        "note": {"tone": "quiet", "title": "Why you are getting this",
                 "text": "Every online GCash payment waits for the owner's check. You get this reminder once per payment."},
        "summary": False,
        "money": False,
        "button_label": "Open payments to check",
        "button_path": reverse("arabela_admin:payment_verification") + "?check=owner",
    }
    sent_to = 0
    try:
        for address, first_name in addresses:
            built = emails.render_email(content, {"first_name": first_name}, inline_logo=True)
            emails._enqueue(emails._make_message(built, address), "owner_payment_check")
            sent_to += 1
    except Exception:       # noqa: BLE001
        logger.exception("Owner payment reminder email failed (the pages are unaffected)")
    if sent_to:
        Reservation.objects.filter(pk__in=[r.pk for r in due]).update(owner_reminder_sent_at=now)
    return len(due) if sent_to else 0


def maybe_send_reminders():
    """The throttled call the middleware makes: at most once every 5 minutes per process, and never raises."""
    try:
        if not cache.add(_REMINDER_KEY, 1, _REMINDER_THROTTLE_SECONDS):
            return 0
        return send_due_reminders()
    except Exception:       # noqa: BLE001
        logger.exception("Owner payment reminders failed (pages are unaffected)")
        return 0
