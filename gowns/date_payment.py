"""Paying a date change request ONLINE (reservations/date_requests.py): the customer from their booking page, or on their phone from the
shop's QR code. The same engine as every other online payment (gowns/online_checkout.py), as a PaymentAttempt of purpose "extra" with the
request on it:

  * START (`start_customer` / `start_qr`). Nothing is recorded; a payment page is opened for the request's amount. One page at a time: an
    older one of the request is closed first (or, when it turns out to be paid, shown instead).
  * CONFIRM. online_checkout.confirm_attempt asks the payment service, then `record` saves the new dates and the payment together
    (date_requests.apply_paid). A request that can no longer take the money (not paid in time, cancelled, the days gone) sends it back.

The screens only DISPLAY what the attempt says; nothing the browser sends is believed about money."""
import logging
import secrets
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from accounts import rate_limit
from gowns import counter_payment, online_checkout, walkin_payment
from reservations import date_requests, gateway, payments
from reservations.ledger import money, peso
from reservations.models import DateChangeRequest, Payment, PaymentAttempt

logger = logging.getLogger(__name__)

MINUTES = walkin_payment.WALKIN_MINUTES             # the payment page stays open this long (Xendit asks for more than 10 minutes)
START_LIMIT = (10, 10 * 60)                         # presses of Pay per account per 10 minutes
A = PaymentAttempt.Status
MONEY_IN = (A.PAID, A.BOOKED, A.REFUNDED, A.FLAGGED, A.HANDLED)


def _open_request(request_id):
    row = DateChangeRequest.objects.select_related("reservation__customer", "item").filter(pk=request_id).first()
    if row is None or not row.is_open:
        raise payments.PaymentError("This payment request is no longer open. Please refresh the page.", status=409)
    return row


def _close_others(row):
    """Closes the other payment pages of the request. Returns the one that turned out to be paid (to be shown instead), else None."""
    for other in row.attempts.filter(status=A.CREATED).order_by("id"):
        closed = online_checkout.cancel_attempt(other.pk)
        if closed is not None and closed.status in MONEY_IN:
            return closed
        if closed is not None and closed.status == A.CREATED:
            raise payments.PaymentError("An earlier payment page of this request could not be closed just now. Please try again in a moment.", status=409)
    return row.attempts.filter(status=A.PAID).order_by("-id").first()


def _check_amount(row):
    if money(row.amount) < 1:
        raise payments.PaymentError("The smallest online payment is ₱1.00. Please pay this one at the shop.")
    if money(row.amount) > counter_payment.max_amount():
        raise payments.PaymentError(f"At most {peso(counter_payment.max_amount())} can be paid online at once. Please pay this one at the shop.")


def _open_page(request, attempt, row, *, success_path, cancel_path, email):
    try:
        session = gateway.create_checkout_session(
            amount=attempt.amount, name="Extra days for your new dates",
            description=f"Arabela Gown Rental: booking {row.reservation.reference_code}", reference_number=attempt.token,
            success_url=online_checkout.absolute(request, success_path), cancel_url=online_checkout.absolute(request, cancel_path),
            metadata={"attempt": attempt.pk, "purpose": attempt.purpose, "date_change": row.pk}, customer_name=attempt.customer_name,
            customer_email=email, expires_minutes=MINUTES)
    except gateway.GatewayError as exc:
        logger.warning("%s payment page could not be opened for date change attempt %s: %s", gateway.label(), attempt.pk, exc)
        PaymentAttempt.objects.filter(pk=attempt.pk, status=A.CREATED).update(
            status=A.EXPIRED, note=f"{gateway.label()} would not open a payment page.", finished_at=timezone.now())
        raise payments.PaymentError("We could not open the payment page just now. Nothing was charged. Please try again in a moment.", status=502)
    PaymentAttempt.objects.filter(pk=attempt.pk).update(session_id=session.id, checkout_url=session.checkout_url)
    return PaymentAttempt.objects.get(pk=attempt.pk)


def _new_attempt(row, **fields):
    reservation = row.reservation
    try:
        with transaction.atomic():
            # Two presses at the same moment: the request's lock lets only one of them open a page.
            DateChangeRequest.objects.select_for_update(of=("self",)).filter(pk=row.pk).first()
            if PaymentAttempt.objects.filter(date_change=row, status=A.CREATED).exists():
                raise payments.PaymentError("The payment page is opening. Please wait a moment.", status=409)
            return PaymentAttempt.objects.create(
                token=secrets.token_urlsafe(24), purpose=PaymentAttempt.Purpose.EXTRA, amount=row.amount, reservation=reservation, date_change=row,
                livemode=not gateway.is_test(), expires_at=timezone.now() + timedelta(minutes=MINUTES), **fields)
    except IntegrityError:
        raise payments.PaymentError("The payment page is opening. Please wait a moment.", status=409)


# ---------------------------------------------------------------- the customer, from their booking page
def start_customer(request, request_id):
    """The Pay button on the booking page -> the address to go to (the payment page, or the page that shows a payment already made)."""
    if not gateway.enabled():
        raise payments.PaymentError("Online payment is not available right now. Please pay at the shop.", status=503)
    # Somebody else's request (or a number that does not exist) answers the same, whatever state it is in.
    if not DateChangeRequest.objects.filter(pk=request_id, reservation__customer=request.user).exists():
        raise payments.PaymentError("This payment request is no longer open. Please refresh the page.", status=404)
    row = _open_request(request_id)
    if not rate_limit.allowed(f"datepay:{request.user.pk}", *START_LIMIT):
        raise payments.PaymentError("That was a lot of tries in a short time. Please wait a minute and try again.", status=429)
    _check_amount(row)
    if date_requests.cannot_apply(row) is not None:          # e.g. the gown was blocked since it was asked: don't take money for it
        raise payments.PaymentError("These new dates can't be saved right now, so nothing was charged. Please contact the shop.", status=409)
    now = timezone.now()
    # The same press again (a double tap, the Back button): the page that is still open.
    mine = row.attempts.filter(status=A.CREATED, source=PaymentAttempt.Source.ONLINE, customer=request.user, expires_at__gt=now + timedelta(minutes=1)).exclude(
        checkout_url="").order_by("-id").first()
    if mine is not None:
        return mine.checkout_url
    paid = _close_others(row)
    if paid is not None:
        return reverse("gowns:payment_return", args=[paid.token])
    attempt = _new_attempt(row, customer=request.user, source=PaymentAttempt.Source.ONLINE,
                           customer_name=row.reservation.display_customer_name[:120],
                           payload={"date_change": row.pk, "code": row.reservation.reference_code})
    attempt = _open_page(request, attempt, row, email=request.user.email or "",
                         success_path=reverse("gowns:payment_return", args=[attempt.token]),
                         cancel_path=reverse("gowns:payment_cancel", args=[attempt.token]))
    return attempt.checkout_url


# ---------------------------------------------------------------- staff, a QR code at the counter
def start_qr(request, request_id, *, staff, staff_name):
    """"Show QR" on a waiting request: -> what the window shows (counter_payment.answer)."""
    if not gateway.enabled():
        raise payments.PaymentError("Online payment is not available right now. Take cash instead.", status=503)
    row = _open_request(request_id)
    if not rate_limit.allowed(f"counterpay:{staff.pk}", *counter_payment.START_LIMIT):
        raise payments.PaymentError("That was a lot of QR codes in a short time. Please wait a minute and try again.", status=429)
    _check_amount(row)
    problem = date_requests.cannot_apply(row)
    if problem is not None:
        raise payments.PaymentError(f"{problem.message} Cancel this request, or send a new one.", status=409)
    now = timezone.now()
    shown = row.attempts.filter(status=A.CREATED, source=PaymentAttempt.Source.SHOP, expires_at__gt=now + timedelta(minutes=1)).exclude(
        checkout_url="").order_by("-id").first()
    if shown is not None:
        return counter_payment.answer(shown)
    paid = _close_others(row)
    if paid is not None:
        return counter_payment.answer(paid)
    attempt = _new_attempt(row, staff=staff, source=PaymentAttempt.Source.SHOP, customer=None,
                           customer_name=row.reservation.display_customer_name[:120],
                           payload={"staff_name": staff_name, "date_change": row.pk, "code": row.reservation.reference_code})
    done = reverse("gowns:walkin_pay_done", args=[attempt.token])
    attempt = _open_page(request, attempt, row, success_path=done, cancel_path=done, email="")
    return counter_payment.answer(attempt)


# ---------------------------------------------------------------- the money is confirmed
def record(attempt):
    """Saves the new dates and the payment of a paid attempt. Runs inside online_checkout._make_booking's transaction; raises
    online_checkout.BookingUnavailable (nothing written) when the request can no longer take this money -- the payment then goes back."""
    row = DateChangeRequest.objects.select_related("reservation").filter(pk=attempt.date_change_id).first()
    if row is None:
        raise online_checkout.BookingUnavailable("The payment request no longer exists.")
    payload = attempt.payload or {}

    def pay(reservation):
        return payments.record_online_booking_payment(
            reservation=reservation, purpose=Payment.Purpose.EXTRA, amount=attempt.paid_amount, channel=attempt.channel,
            provider_ref=attempt.provider_payment_id, paid_at=attempt.paid_at, livemode=attempt.livemode, request_id=f"pm-{attempt.pk}",
            note="Paid for a date change.", served_by_user=attempt.staff, requested=True)

    try:
        row, payment = date_requests.apply_paid(row.pk, paid_at=attempt.paid_at or timezone.now(), pay=pay)
    except (date_requests.DateChangeError, payments.PaymentError) as exc:
        logger.warning("The payment of date change attempt %s cannot be used: %s", attempt.pk, exc.message)
        # The customer reads this reason, so a staff message (it can name another customer) is never passed on.
        why = exc.public if isinstance(exc, date_requests.DateChangeError) else exc.message
        raise online_checkout.BookingUnavailable(why or "The new dates can no longer be saved.")
    reservation = row.reservation
    from accounts import audit
    from arabela_admin import audit_describe
    kind, target_id, label = audit_describe._booking_target(reservation)
    staff = attempt.staff or row.requested_by
    audit.record(staff, category="Bookings", action="item.date_paid", actor_name=payload.get("staff_name") or row.requested_by_name,
                 summary=(f"the {peso(payment.amount)} asked for a date change on {audit_describe._booking_text(reservation)} was paid online: "
                          f"the new dates are saved"),
                 target_type=kind, target_id=str(target_id), target_label=label,
                 details={"receipt": payment.receipt_number, "method": payment.get_method_display(),
                          "new dates": date_requests.new_dates_text(row)})
    return reservation
