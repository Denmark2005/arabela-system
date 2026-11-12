"""Cancellation requests: how a staff account WITHOUT the "Cancel bookings" permission asks the owner to cancel a booking.

  staff   -> create()   a request (the booking stays exactly as it is; one open request per booking)
  owner   -> approve()  runs the normal cancellation (reservations/cancellation.py) -- re-checked under a lock, so a booking that was
                        picked up or cancelled in the meantime closes the request instead of cancelling by surprise
          -> decline()  needs a short note, which staff read
  staff   -> withdraw() takes it back while it is still waiting
  anyone waiting 12 hours -> ONE reminder email to the owner (send_due_reminders), never repeated
Nothing is approved automatically. A request that has waited 48 hours is shown red (is_overdue).

Every step is a staff-only line on the booking's timeline, and the views write the Audit Log lines."""
import logging
from collections import namedtuple
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from accounts.roles import staff_display_name
from reservations import cancellation
from reservations.cancellation import NOTE_MAX, REASONS, CannotCancel
from reservations.models import CancelRequest, Reservation, ReservationItem, ReservationStatusEvent

logger = logging.getLogger(__name__)

REMINDER_AFTER = timedelta(hours=12)
RED_AFTER = timedelta(hours=48)
DECISION_NOTE_MAX = 200
_REMINDER_THROTTLE_SECONDS = 300
_REMINDER_KEY = "arabela:cancel-request-reminder:last-run"

REQUESTED_LABEL = "Cancellation requested"
APPROVED_LABEL = "Cancellation request approved"
DECLINED_LABEL = "Cancellation request declined"
WITHDRAWN_LABEL = "Cancellation request withdrawn"
CLOSED_LABEL = "Cancellation request closed"

Outcome = namedtuple("Outcome", "kind request message result")           # kind: 'approved' | 'closed'
_ALREADY_WAITING = "A cancellation request is already waiting for this booking."


_LIVE_BOOKING = (Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE, Reservation.Status.OVERDUE)


def pending():
    """The requests still waiting for the owner. A request whose booking was cancelled (or otherwise ended) some other way is no longer
    waiting for anything, so it is left out here."""
    return CancelRequest.objects.filter(
        status=CancelRequest.Status.PENDING, reservation__status__in=_LIVE_BOOKING).select_related("reservation", "reservation__customer")


def pending_for(reservation):
    return CancelRequest.objects.filter(reservation=reservation, status=CancelRequest.Status.PENDING).first()


def is_overdue(request, now=None):
    """True once a waiting request is older than 48 hours (the page shows its age in red)."""
    return request.status == CancelRequest.Status.PENDING and (now or timezone.now()) - request.created_at >= RED_AFTER


def reason_text(request):
    """'The gown was not picked up - Called twice' as staff read it."""
    base = REASONS.get(request.reason_key, ("Another reason", ""))[0]
    return base + (f" - {request.note}" if request.note else "")


def what_text(request):
    """What the request is about, in words: the whole booking or the gowns."""
    if request.is_whole_booking:
        return "the whole booking"
    names = list(request.reservation.items.filter(id__in=request.item_id_list).values_list("gown_name", flat=True))
    return ", ".join(names) if names else "some gowns"


def _is_owner(user):
    if user is None or not getattr(user, "pk", None):
        return False
    if user.is_superuser:
        return True
    profile = UserProfile.objects.filter(user=user).first()
    return bool(profile and profile.role == UserProfile.Role.OWNER)


def _check_still_cancellable(reservation, item_ids):
    """Raises CannotCancel when what is asked for can't be cancelled right now -- the same tests the real cancel makes."""
    if reservation.status == Reservation.Status.CANCELLED:
        raise CannotCancel("This booking was already cancelled. Please refresh the page.", 409)
    if reservation.status == Reservation.Status.PENDING:
        raise CannotCancel("This booking is still waiting for approval. Use Reject in Pending Approval instead.")
    items = list(reservation.items.select_related("gown").order_by("rental_date", "id"))
    if item_ids:
        if not cancellation.can_cancel_gowns(reservation):
            raise CannotCancel(f"This booking can't be changed because its status is {reservation.status}.")
        cancellation._gowns_to_cancel(items, item_ids)
    else:
        if reservation.status not in (Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE):
            raise CannotCancel(f"This booking can't be cancelled because its status is {reservation.status}.")
        if not cancellation.can_cancel(reservation, items):
            raise CannotCancel(
                "A gown in this booking has already gone out, so the booking can't be cancelled as a whole. "
                "Ask to cancel the gowns nobody picked up one by one instead.")


def create(reservation_id, user, *, reason_key, note, item_ids=None):
    """Staff asks the owner to cancel the booking (item_ids empty) or just those gowns. Raises CannotCancel (nothing is written)."""
    cancellation._reason_or_error(reason_key, note)
    note = (note or "").strip()[:NOTE_MAX]
    ids = list(dict.fromkeys(int(i) for i in (item_ids or [])))
    name = staff_display_name(user)
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update(of=("self",)).filter(id=reservation_id).first()
        if reservation is None:
            raise CannotCancel("Reservation not found.", 404)
        _check_still_cancellable(reservation, ids)
        if pending_for(reservation) is not None:
            raise CannotCancel(_ALREADY_WAITING, 409)
        try:
            with transaction.atomic():
                request = CancelRequest.objects.create(
                    reservation=reservation, item_ids=",".join(str(i) for i in ids), requested_by=user, requested_by_name=name,
                    reason_key=reason_key, note=note)
        except IntegrityError:          # two requests at the same instant: the database allows only one waiting
            raise CannotCancel(_ALREADY_WAITING, 409)
        ReservationStatusEvent.record(
            reservation, REQUESTED_LABEL, detail=f"{name} asked to cancel {what_text(request)}: {reason_text(request)}",
            actor=ReservationStatusEvent.Actor.STAFF, staff_only=True)
    return request


def _lock_waiting(request_id):
    request = CancelRequest.objects.select_for_update().filter(id=request_id).first()
    if request is None:
        raise CannotCancel("That request was not found. Please refresh the page.", 404)
    if request.status != CancelRequest.Status.PENDING:
        raise CannotCancel(f"This request was already answered ({request.get_status_display().lower()}). Please refresh the page.", 409)
    return request


def _finish(request, status, user, note=""):
    request.status = status
    request.decided_by = user
    request.decided_by_name = staff_display_name(user)
    request.decided_at = timezone.now()
    request.decision_note = (note or "")[:DECISION_NOTE_MAX]
    request.save(update_fields=["status", "decided_by", "decided_by_name", "decided_at", "decision_note"])


def approve(request_id, owner):
    """The owner says yes: the booking (or those gowns) is cancelled exactly as if she had cancelled it herself. If it can't be any more
    (a gown was picked up, someone cancelled it), nothing is cancelled and the request closes with the reason."""
    if not _is_owner(owner):
        raise CannotCancel("Only the owner can answer a cancellation request.", 403)
    owner_name = staff_display_name(owner)
    with transaction.atomic():
        request = _lock_waiting(request_id)
        reservation = request.reservation
        shown_as = f"{request.requested_by_name}, approved by {owner_name}"
        try:
            if request.is_whole_booking:
                cancellation.cancel_by_staff(
                    reservation.id, staff_name=shown_as, reason_key=request.reason_key, note=request.note,
                    typed_code=reservation.reference_code)
                result = None                       # a whole booking: nothing more to report than "cancelled"
            else:
                result = cancellation.cancel_gowns_by_staff(
                    reservation.id, request.item_id_list, staff_name=shown_as, reason_key=request.reason_key, note=request.note)
        except CannotCancel as exc:
            _finish(request, CancelRequest.Status.CLOSED, owner, exc.message)
            ReservationStatusEvent.record(
                reservation, CLOSED_LABEL, detail=f"No longer possible: {exc.message}"[:300],
                actor=ReservationStatusEvent.Actor.STAFF, staff_only=True)
            return Outcome("closed", request, exc.message, None)
        _finish(request, CancelRequest.Status.APPROVED, owner)
        ReservationStatusEvent.record(
            reservation, APPROVED_LABEL, detail=f"{owner_name} approved the request of {request.requested_by_name}.",
            actor=ReservationStatusEvent.Actor.STAFF, staff_only=True)
    return Outcome("approved", request, "", result)


def decline(request_id, owner, note):
    """The owner says no. The short note is what staff read, so it is required."""
    if not _is_owner(owner):
        raise CannotCancel("Only the owner can answer a cancellation request.", 403)
    note = (note or "").strip()
    if not note:
        raise CannotCancel("Write a short note so staff know why.")
    if len(note) > DECISION_NOTE_MAX:
        raise CannotCancel(f"Please keep the note under {DECISION_NOTE_MAX} characters.")
    with transaction.atomic():
        request = _lock_waiting(request_id)
        _finish(request, CancelRequest.Status.DECLINED, owner, note)
        ReservationStatusEvent.record(
            request.reservation, DECLINED_LABEL, detail=f"{request.decided_by_name}: {note}"[:300],
            actor=ReservationStatusEvent.Actor.STAFF, staff_only=True)
    return request


def withdraw(request_id, user):
    """The staff member who asked (or the owner) takes the request back while it is still waiting."""
    with transaction.atomic():
        request = _lock_waiting(request_id)
        if not (_is_owner(user) or (request.requested_by_id is not None and request.requested_by_id == user.pk)):
            raise CannotCancel("Only the person who asked can withdraw this request.", 403)
        _finish(request, CancelRequest.Status.WITHDRAWN, user)
        ReservationStatusEvent.record(
            request.reservation, WITHDRAWN_LABEL, detail=f"{request.decided_by_name} took the request back.",
            actor=ReservationStatusEvent.Actor.STAFF, staff_only=True)
    return request


# ---- the 12-hour reminder to the owner ---------------------------------------------------------------------------------------------
def _owner_accounts():
    User = get_user_model()
    return User.objects.filter(is_active=True).filter(Q(is_superuser=True) | Q(profile__role=UserProfile.Role.OWNER)).distinct()


def due_for_reminder(now=None):
    now = now or timezone.now()
    return pending().filter(created_at__lte=now - REMINDER_AFTER, reminder_sent_at__isnull=True)


def send_due_reminders(now=None):
    """One email to the owner listing every request that has waited more than 12 hours. Returns how many requests it listed (0 when nothing
    was due or nobody could be emailed). Never raises: an email problem must not break the page that triggered it."""
    from accounts import customer_emails as emails

    now = now or timezone.now()
    due = list(due_for_reminder(now)[:50])
    if not due:
        return 0
    addresses = []
    for user in _owner_accounts():
        address = emails._deliverable_address(user)
        if address and address not in [a for a, _ in addresses]:
            addresses.append((address, emails._account_first_name(user)))
    lines = []
    for request in due[:10]:
        lines.append(
            f"{request.reservation.reference_code}: {request.requested_by_name} asked to cancel {what_text(request)} "
            f"({reason_text(request)}), waiting since "
            f"{timezone.localtime(request.created_at).strftime('%b %d, %I:%M %p').replace(' 0', ' ')}.")
    if len(due) > 10:
        lines.append(f"...and {len(due) - 10} more.")
    count = len(due)
    plural = "request" if count == 1 else "requests"
    content = {
        "subject": f"{count} cancellation {plural} waiting for you",
        "heading": f"{count} cancellation {plural} to answer",
        "preheader": "Open Active Reservations and press Approve or Decline.",
        "paragraphs": [
            f"{count} cancellation {plural} from your staff {'has' if count == 1 else 'have'} waited more than 12 hours. "
            "The booking stays as it is until you answer. Open each one and press Approve or Decline.",
            *lines,
        ],
        "note": {"tone": "quiet", "title": "Why you are getting this",
                 "text": "Staff who are not allowed to cancel bookings ask you instead. You get this reminder once per request."},
        "summary": False,
        "money": False,
        "button_label": "Open Active Reservations",
        "button_path": reverse("arabela_admin:active_reservations"),
    }
    sent_to = 0
    try:
        for address, first_name in addresses:
            built = emails.render_email(content, {"first_name": first_name}, inline_logo=True)
            emails._enqueue(emails._make_message(built, address), "cancel_request_reminder")
            sent_to += 1
    except Exception:       # noqa: BLE001
        logger.exception("Cancellation request reminder email failed (the pages are unaffected)")
    if sent_to:
        CancelRequest.objects.filter(pk__in=[r.pk for r in due]).update(reminder_sent_at=now)
    return len(due) if sent_to else 0


def maybe_send_reminders():
    """The throttled call the middleware makes: at most once every 5 minutes per process, and never raises."""
    try:
        if not cache.add(_REMINDER_KEY, 1, _REMINDER_THROTTLE_SECONDS):
            return 0
        return send_due_reminders()
    except Exception:       # noqa: BLE001
        logger.exception("Cancellation request reminders failed (pages are unaffected)")
        return 0
