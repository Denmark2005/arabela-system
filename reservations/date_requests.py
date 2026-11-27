"""A date change that costs more than the security deposit covers (a booking that pays such amounts right away: Reservation.pays_over_deposit).

Staff change dates in Active Reservations. While the booked days still fit in the deposit (and what was already paid over it), the change saves
at once. When they would cost MORE, nothing is saved: staff send a PAYMENT REQUEST instead (`create`).

  * The new days are HELD for that gown while the request waits (reservations/date_changes.held_requests: they count as booked everywhere).
  * The customer is told in their account and by email, with a Pay button. They pay online from their booking page, from the shop's QR code
    (gowns/date_payment.py), or in cash at the counter (`pay_cash`).
  * Once the money is in, the dates change by themselves (`apply_paid`: the same rules and the same saving as a change staff make), the payment
    gets its receipt, and the customer is told.
  * A request that is not paid in time, cancelled by staff, replaced, or no longer needed (the gown is back, the booking cancelled, the dates
    changed another way) changes nothing. Money that arrives for it afterwards is sent back.

Only this module changes a DateChangeRequest."""
import logging
from datetime import datetime, time, timedelta

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from accounts.roles import staff_display_name
from gowns.models import COOLDOWN_DAYS, Gown
from reservations import date_changes, ledger, payments
from reservations.date_changes import DateChangeError
from reservations.ledger import ZERO, money, peso
from reservations.models import DateChangeRequest, Payment, PaymentAttempt, Reservation, ReservationItem, ReservationStatusEvent

logger = logging.getLogger(__name__)

HOLD_HOURS = 24                 # how long the new days are held while the money is awaited
SWEEP_BATCH = 5
S = DateChangeRequest.Status
SENT_EVENT = "Payment request sent"
CANCELLED_EVENT = "Payment request cancelled"
EXPIRED_EVENT = "Payment request not paid in time"
CLOSED_EVENT = "Payment request closed"
OPEN_STATUSES = (Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE, Reservation.Status.OVERDUE)


def _long(day):
    return f"{day:%b} {day.day}, {day.year}"


def moment_text(value):
    """'Nov 28, 2026, 3:15 PM' in the shop's time."""
    local = timezone.localtime(value)
    return f"{local:%b} {local.day}, {local.year}, {local.hour % 12 or 12}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"


def _end_of(day):
    return timezone.make_aware(datetime.combine(day, time(23, 59)))


def expiry_for(plan, now=None):
    """When an unpaid request stops: 24 hours from now, and never past the day the customer would collect the gown on the new pick-up day,
    or past the new return day."""
    now = now or timezone.now()
    limits = [now + timedelta(hours=HOLD_HOURS), _end_of(plan["return"])]
    if plan["want_pickup"] is not None:
        limits.append(_end_of(plan["pickup"]))
    return min(limits)


# ---------------------------------------------------------------- the money
def _booking_for_money(reservation_id):
    return Reservation.objects.prefetch_related("items", "payments", "deductions", "receipt_records").get(pk=reservation_id)


def money_needed(item, plan, today=None):
    """What has to be paid BEFORE this change is saved: 0 when it saves at once. Only for a booking that pays amounts over the deposit right
    away, only when the change makes the date fees bigger, and only the part the deposit (and what was already paid over it) does not cover."""
    booking = _booking_for_money(item.reservation_id)
    if not booking.pays_over_deposit:
        return ZERO
    before = ledger.statement(booking, today)
    changed = next(i for i in booking.items.all() if i.id == item.id)       # the loaded copy: changed only in memory
    changed.reservation = booking
    date_changes.preview(changed, plan)
    after = ledger.statement(booking, today)
    if after.late_fee <= before.late_fee:
        return ZERO
    return max(after.late_fee + after.deductions_total - after.deposit - after.extra_received, ZERO)


# ---------------------------------------------------------------- reading
def waiting(reservation_id):
    """The request of this booking that is still waiting for money inside its time, or None."""
    return (DateChangeRequest.objects.filter(reservation_id=reservation_id, status=S.WAITING, expires_at__gt=timezone.now())
            .select_related("item", "reservation").first())


def new_dates_text(request):
    """'pick-up Oct 3, 2026 and return Oct 12, 2026': the dates the request would save."""
    parts = []
    if request.want_pickup:
        parts.append(f"pick-up {_long(request.want_pickup)}")
    if request.want_return:
        parts.append(f"return {_long(request.want_return)}")
    if request.want_event:
        parts.append(f"event {_long(request.want_event)}")
    return " and ".join(parts) if len(parts) < 3 else f"{parts[0]}, {parts[1]} and {parts[2]}"


def booking_path(request):
    """The customer's page of the gown the request is about, at its payment box."""
    return reverse("gowns:reservation_item_detail", args=[request.item_id]) + "#pay-request"


def add_pay_buttons(user, messages):
    """Gives the newest message of each request still waiting for this customer's money its button: `pay_url` and `pay_label` on the message
    object (the Messages page shows them). `messages`: the page's messages, newest first. Nothing is stored."""
    asking = [m for m in messages if m.category == "Payment" and "Please pay " in (m.body or "")]
    if not asking:
        return
    open_requests = DateChangeRequest.objects.filter(
        reservation__customer=user, status=S.WAITING, expires_at__gt=timezone.now()).select_related("reservation")
    for request in open_requests:
        code, words = request.reservation.reference_code, f"Please pay {peso(request.amount)}"
        message = next((m for m in asking if code in m.body and words in m.body), None)
        if message is not None:
            message.pay_url, message.pay_label = booking_path(request), f"Pay PHP {money(request.amount):,.2f}"


# ---------------------------------------------------------------- telling the customer
def tell(reservation, text, *, subject, heading, path="", button=""):
    """A message in the customer's account (the bell) and, when customer emails are on, an email whose button opens `path`. False for a booking
    with no website account."""
    from accounts.models import CustomerMessage

    if reservation.customer_id is None:
        return False
    CustomerMessage.objects.create(recipient_id=reservation.customer_id, category=CustomerMessage.Category.PAYMENT, body=text[:500])
    if getattr(settings, "CUSTOMER_EMAILS_ENABLED", False):
        reservation_id = reservation.pk
        transaction.on_commit(lambda: _email_guarded(reservation_id, text, subject, heading, path, button))
    return True


def _email_guarded(reservation_id, text, subject, heading, path, button):
    from accounts import customer_emails

    customer_emails._guarded(_send_email, reservation_id, text, subject, heading, path, button)


def _send_email(reservation_id, text, subject, heading, path, button):
    from accounts import customer_emails

    reservation = Reservation.objects.select_related("customer").get(pk=reservation_id)
    data = customer_emails._reservation_data(reservation, text=text)
    data.update(subject=subject, heading=heading)
    if path:
        data.update(button_path=path, button_label=button)
    customer_emails._deliver(reservation.customer, customer_emails.PAYMENT_NOTICE, data)


def _tell_request(request, *, again=False):
    """The message that asks for the money."""
    reservation = request.reservation
    code = reservation.reference_code
    opening = "Reminder: your new dates" if again else "Your new dates"
    text = (f"{opening} for {request.item.gown_name} ({code}) -- {new_dates_text(request)} -- cost {peso(request.amount)} more than your security "
            f"deposit covers. Please pay {peso(request.amount)} by {moment_text(request.expires_at)} to confirm them. Until then your dates stay as they are.")
    return tell(reservation, text, subject=f"Please pay PHP {money(request.amount):,.2f} for your new dates - {code}",
                heading="Payment needed for your new dates", path=booking_path(request), button=f"Pay PHP {money(request.amount):,.2f}")


def _tell_closed(request, why):
    reservation = request.reservation
    code = reservation.reference_code
    text = (f"The request to pay {peso(request.amount)} for new dates on {code} ({request.item.gown_name}) is closed: {why} "
            "There is nothing to pay for it.")
    return tell(reservation, text, subject=f"Your payment request is closed - {code}", heading="Payment request closed",
                path=booking_path(request), button="View my reservation")


# ---------------------------------------------------------------- closing
def _close_pages(request_ids):
    """Closes the payment pages still open for these requests, after the change that closed them is saved. A page that turns out to be paid is
    handled by the usual confirmation, which sends the money back."""
    from gowns import online_checkout

    for attempt in PaymentAttempt.objects.filter(date_change_id__in=request_ids, status=PaymentAttempt.Status.CREATED).order_by("id"):
        try:
            online_checkout.cancel_attempt(attempt.pk)
        except Exception:                                       # noqa: BLE001 -- it expires by itself, and a payment on it is sent back
            logger.exception("The payment page of attempt %s could not be closed", attempt.pk)


def close(reservation_id, *, status=S.CLOSED, note="", item_ids=None, request_ids=None, tell_customer=True):
    """Stops the waiting request of a booking (only the ones about `item_ids`, or only `request_ids`, when given): the held days open again,
    its payment pages are closed, the history says why. Returns how many it closed. Call it inside the transaction of the change that makes
    it unnecessary."""
    with transaction.atomic():
        rows = DateChangeRequest.objects.select_for_update(of=("self",)).filter(reservation_id=reservation_id, status=S.WAITING)
        if item_ids is not None:
            rows = rows.filter(item_id__in=list(item_ids))
        if request_ids is not None:
            rows = rows.filter(pk__in=list(request_ids))
        found = list(rows)
        for request in found:
            request.status, request.note, request.finished_at = status, (note or "")[:300], timezone.now()
            request.save(update_fields=["status", "note", "finished_at"])
            label = {S.CANCELLED: CANCELLED_EVENT, S.EXPIRED: EXPIRED_EVENT}.get(status, CLOSED_EVENT)
            ReservationStatusEvent.record(
                request.reservation, label, item=request.item,
                detail=f"{peso(request.amount)} for {new_dates_text(request)}: {note}".strip(),
                actor=ReservationStatusEvent.Actor.SYSTEM if status == S.EXPIRED else ReservationStatusEvent.Actor.STAFF)
            if tell_customer:
                _tell_closed(request, note)
        if found:
            ids = [request.pk for request in found]
            transaction.on_commit(lambda: _close_pages(ids))
    return len(found)


# ---------------------------------------------------------------- sending
def create(item_id, *, want_pickup, want_return, want_event=None, user=None):
    """Staff ask the customer to pay for new dates: checks the change exactly like saving it, holds the days and tells the customer. An older
    request of the booking is replaced. Raises DateChangeError (nothing changed) when the change isn't possible or needs no payment."""
    now = timezone.now()
    with transaction.atomic():
        reservation_id = ReservationItem.objects.filter(pk=item_id).values_list("reservation_id", flat=True).first()
        reservation = Reservation.objects.select_for_update(of=("self",)).filter(pk=reservation_id).first()
        if reservation is None:
            raise DateChangeError("Item not found", 404)
        item = ReservationItem.objects.select_related("gown", "reservation").get(pk=item_id)
        if item.gown_id:
            list(Gown.objects.select_for_update().filter(id=item.gown_id))
        item = ReservationItem.objects.select_related("gown", "reservation").get(pk=item_id)
        if reservation.status not in OPEN_STATUSES:
            raise DateChangeError("Only approved bookings can be changed here. Approve it first in Pending Approval.")
        plan = date_changes.check(item, want_pickup, want_return, want_event)
        due = money_needed(item, plan)
        if due <= 0:
            raise DateChangeError("This change needs no payment: it fits in the security deposit. Save it normally.", 409)
        close(reservation.pk, status=S.REPLACED, note="Replaced by a newer request.", tell_customer=False)
        rest = COOLDOWN_DAYS if getattr(item, "auto_cooldown_block", None) is not None else 0
        request = DateChangeRequest.objects.create(
            reservation=reservation, item=item, gown_id=item.gown_id, want_pickup=want_pickup, want_return=want_return, want_event=want_event,
            from_pickup=item.rental_date, from_return=item.return_date, hold_start=plan["pickup"], hold_end=plan["return"] + timedelta(days=rest),
            amount=due, requested_by=user if getattr(user, "pk", None) else None,
            requested_by_name=staff_display_name(user) if user is not None else "", created_at=now, expires_at=expiry_for(plan, now))
        ReservationStatusEvent.record(
            reservation, SENT_EVENT, item=item,
            detail=(f"{peso(due)} to pay for {new_dates_text(request)} by {moment_text(request.expires_at)}. "
                    "The dates change once it is paid."),
            actor=ReservationStatusEvent.Actor.STAFF)
        _tell_request(request)
    return request


def send_again(request_id):
    """Staff press "Resend": the message (and email) once more. False for a booking with no website account."""
    request = DateChangeRequest.objects.select_related("reservation", "item").filter(pk=request_id).first()
    if request is None or not request.is_open:
        raise DateChangeError("This payment request is no longer open. Please refresh the page.", 409)
    with transaction.atomic():
        return _tell_request(request, again=True)


def cancel(request_id, *, user=None):
    """Staff cancel a waiting request. The dates stay as they were."""
    request = DateChangeRequest.objects.filter(pk=request_id).first()
    if request is None or request.status != S.WAITING:
        raise DateChangeError("This payment request is no longer open. Please refresh the page.", 409)
    who = staff_display_name(user) if user is not None else "the shop"
    if not close(request.reservation_id, status=S.CANCELLED, note=f"Cancelled by {who}. Your dates stay as they were.", request_ids=[request.pk]):
        raise DateChangeError("This payment request is no longer open. Please refresh the page.", 409)
    return DateChangeRequest.objects.get(pk=request_id)


def expire_due(limit=SWEEP_BATCH):
    """Closes requests whose time ran out. A payment page that turns out to be paid in time is confirmed first (which saves the new dates).
    Safe to run from anywhere, any number of times; never raises. Returns how many it closed."""
    from gowns import online_checkout

    closed = 0
    try:
        due = list(DateChangeRequest.objects.filter(status=S.WAITING, expires_at__lte=timezone.now()).order_by("expires_at")[:limit])
        for request in due:
            for attempt in request.attempts.filter(status__in=(PaymentAttempt.Status.CREATED, PaymentAttempt.Status.PAID)).order_by("id"):
                if attempt.status == PaymentAttempt.Status.PAID:
                    online_checkout.confirm_attempt(attempt.pk, source="sweep")
                else:
                    online_checkout.cancel_attempt(attempt.pk)
            closed += close(request.reservation_id, status=S.EXPIRED, note="It was not paid in time. Your dates stay as they were.",
                            request_ids=[request.pk])          # only this one: staff may have sent a new request while its page was being closed
    except Exception:                                           # noqa: BLE001 -- a sweep must never break the page that runs it
        logger.exception("Overdue date change requests could not be closed")
    return closed


# ---------------------------------------------------------------- the money arrived
def refusal_for(request, paid_at):
    """Why this request can no longer take a payment made at `paid_at` ('' when it can)."""
    if request.status == S.APPLIED:
        return "This date change was already paid."
    if request.status == S.CANCELLED:
        return "The shop cancelled this payment request."
    if request.status == S.REPLACED:
        return "A newer date change replaced this payment request."
    if request.status == S.CLOSED:
        return "This payment request is no longer needed."
    if paid_at > request.expires_at:
        return "The time to pay for these new dates ran out."
    return ""


def cannot_apply(request):
    """The DateChangeError that would stop this waiting request from being saved if it were paid right now (None when it would save). Asked
    before a payment page is opened, so nobody pays for dates that can no longer be given."""
    item = ReservationItem.objects.select_related("gown", "reservation").get(pk=request.item_id)
    if (item.rental_date, item.return_date) != (request.from_pickup, request.from_return):
        return DateChangeError("The dates of this gown changed in the meantime.", 409)
    try:
        date_changes.check(item, request.want_pickup, request.want_return, request.want_event)
    except DateChangeError as exc:
        return exc
    return None


def apply_paid(request_id, *, paid_at, pay):
    """The money is in: saves the new dates and records the payment, together or not at all. `pay(reservation)` records the payment and returns
    it. Raises DateChangeError / PaymentError (nothing saved) when the change can no longer be made -- the caller sends online money back."""
    with transaction.atomic():
        reservation_id = DateChangeRequest.objects.filter(pk=request_id).values_list("reservation_id", flat=True).first()
        reservation = Reservation.objects.select_for_update(of=("self",)).filter(pk=reservation_id).first()
        if reservation is None:
            raise DateChangeError("The booking no longer exists.", 404, public="The booking no longer exists.")
        request = DateChangeRequest.objects.select_for_update(of=("self",)).get(pk=request_id)
        why = refusal_for(request, paid_at)
        if why:
            raise DateChangeError(why, 409, public=why)
        if reservation.status not in OPEN_STATUSES:
            why = f"The booking is {reservation.status.lower()}, so its dates can't change."
            raise DateChangeError(why, 409, public=why)
        if request.item.gown_id:
            list(Gown.objects.select_for_update().filter(id=request.item.gown_id))
        # The line is locked too: a check-in marked at the same moment waits, so the dates are never moved under it.
        item = ReservationItem.objects.select_for_update(of=("self",)).select_related("gown", "reservation").get(pk=request.item_id)
        if (item.rental_date, item.return_date) != (request.from_pickup, request.from_return):
            why = "The dates of this gown changed in the meantime."
            raise DateChangeError(why, 409, public=why)
        plan = date_changes.check(item, request.want_pickup, request.want_return, request.want_event, today=timezone.localtime(paid_at).date())
        date_changes.apply(item, plan, by_name=request.requested_by_name or "the shop")
        payment = pay(reservation)
        request.status, request.finished_at, request.payment, request.note = S.APPLIED, timezone.now(), payment, ""
        request.save(update_fields=["status", "finished_at", "payment", "note"])
        _tell_applied(request, item, payment)
        ids = [request.pk]
        transaction.on_commit(lambda: _close_pages(ids))              # another page opened for the same request is closed
    return request, payment


def _tell_applied(request, item, payment):
    """One message for both: the payment and its receipt, and the new dates."""
    reservation = request.reservation
    text = (f"Payment received {peso(payment.amount)} for {reservation.reference_code} ({payments._way(payment)}). Receipt {payment.receipt_number}. "
            f"Your new dates for {item.gown_name} are saved: pick-up {_long(item.rental_date)}, return {_long(item.return_date)}.")
    payments._notify(reservation, payment, text, subject=f"Your new dates are saved - receipt {payment.receipt_number}",
                     heading="Your dates are changed")


def pay_cash(request_id, *, user=None, request_key=""):
    """The customer pays the request in cash at the counter. Returns (request, payment). A double press returns the payment already saved."""
    from gowns import online_checkout

    request = DateChangeRequest.objects.select_related("payment").filter(pk=request_id).first()
    if request is None:
        raise DateChangeError("That payment request was not found. Please refresh the page.", 404)
    key = (request_key or "")[:40]
    if request.status == S.APPLIED and request.payment_id and key and request.payment.request_id == key:
        return request, request.payment
    why = refusal_for(request, timezone.now())
    if why:
        raise DateChangeError(why, 409)
    # An online payment page of this request is closed first, so the customer can't pay twice.
    for attempt in PaymentAttempt.objects.filter(date_change=request, status__in=(PaymentAttempt.Status.CREATED, PaymentAttempt.Status.PAID)):
        if attempt.status == PaymentAttempt.Status.PAID:
            online_checkout.confirm_attempt(attempt.pk, source="sweep")
            closed = None
        else:
            closed = online_checkout.cancel_attempt(attempt.pk)
        if closed is None or closed.status != PaymentAttempt.Status.CANCELLED:
            raise DateChangeError("The customer's online payment just arrived. Please refresh the page before taking cash.", 409)

    def pay(reservation):
        try:
            return payments.record_payment(
                reservation_id=reservation.pk, purpose=Payment.Purpose.EXTRA, amount=request.amount, method=Payment.Method.CASH, user=user,
                request_id=key, note="Paid at the shop for a date change.", tell=False)
        except payments.PaymentError as exc:
            raise DateChangeError(f"{exc.message} The amounts changed since this request was sent: cancel it and send a new one.", exc.status)

    return apply_paid(request_id, paid_at=timezone.now(), pay=pay)
