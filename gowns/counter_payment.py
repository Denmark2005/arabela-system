"""A customer pays what an EXISTING booking owes ON THEIR PHONE: staff choose "Pay online" in Payments, a QR code appears, the customer scans it and pays on
Xendit's page, and the payment is recorded with its receipt by itself once Xendit confirms the money.

The same engine as New Booking's walk-in QR (gowns/walkin_payment.py, gowns/online_checkout.py), as a PaymentAttempt of source "shop" with its booking set from
the start:

  * START (`start`). The amount is checked with the same rule as cash at the counter (reservations/payments.check_amount); nothing is recorded. An older QR
    code of the booking is closed first (or, when it turns out to be paid, shown instead), so a booking never has two payable pages.
  * CONFIRM. online_checkout.confirm_attempt asks Xendit (never believing the screen or a webhook's body), then `record` writes the payment under the
    booking's lock. If the booking can no longer take that money (paid another way meanwhile, cancelled), the whole payment is sent back.
  * Cash while a QR code is open: `close_open` closes it first (arabela_admin/payments_views.payment_record_view), so a customer can't pay twice.
  * The window closed, or the page reloaded: opening the booking again shows the same QR code (`window_state`), and the customer's "Thank you" page also
    checks the payment, so it is recorded even when no staff screen is open.

The staff screen only DISPLAYS what `state` says; it never decides that anything is paid."""
import logging
import re
import secrets
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from accounts import rate_limit
from gowns import online_checkout, walkin_payment
from reservations import gateway, ledger, payments
from reservations.ledger import money, peso
from reservations.models import Payment, PaymentAttempt, Reservation

logger = logging.getLogger(__name__)

MINUTES = walkin_payment.WALKIN_MINUTES                 # the payment page stays open this long (Xendit asks for more than 10 minutes)
START_LIMIT = walkin_payment.START_LIMIT                # QR codes per staff member per 10 minutes
PAID_WAIT = walkin_payment.PAID_WAIT                    # a payment that came in but is not recorded yet blocks another QR code of the booking this long
_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{8,40}")
S = PaymentAttempt.Status
SHOP = PaymentAttempt.Source.SHOP
PURPOSES = {                                           # what the Payments window asks for -> how the attempt keeps it
    Payment.Purpose.DEPOSIT: PaymentAttempt.Purpose.DEPOSIT,
    Payment.Purpose.FULL: PaymentAttempt.Purpose.FULL,
    Payment.Purpose.BALANCE: PaymentAttempt.Purpose.BALANCE,
    Payment.Purpose.EXTRA: PaymentAttempt.Purpose.EXTRA,
}
_NAMES = {Payment.Purpose.DEPOSIT: "Security deposit", Payment.Purpose.FULL: "Gown rental and security deposit",
          Payment.Purpose.BALANCE: "Gown rental balance", Payment.Purpose.EXTRA: "Fees and deductions"}
NOT_NEEDED = "This booking no longer needs that payment (it was paid or changed in the meantime)."
_MONEY_IN = (S.PAID, S.BOOKED, S.REFUNDED, S.FLAGGED, S.HANDLED)


def max_amount():
    return min(online_checkout.MAX_ONLINE_AMOUNT, Decimal(gateway.max_amount()))


def urls_for(attempt):
    return {
        "statusUrl": reverse("arabela_admin:payment_pay_status", args=[attempt.token]),
        "cancelUrl": reverse("arabela_admin:payment_pay_cancel", args=[attempt.token]),
    }


def state(attempt):
    """The staff screen's picture of an attempt, as plain JSON: waiting | confirming | recorded | refunded | flagged | ended."""
    if attempt.status == S.BOOKED:
        payment = Payment.objects.filter(request_id=f"pm-{attempt.pk}").first()
        return {
            "state": "recorded", "amount": f"{money(attempt.paid_amount):,.2f}", "paidWith": gateway.CHANNEL_LABELS.get(attempt.channel, gateway.label()),
            "receiptNumber": payment.receipt_number if payment else "", "paymentId": payment.pk if payment else 0, "test": not attempt.livemode,
        }
    if attempt.status == S.PAID:
        return {"state": "confirming"}
    if attempt.status == S.REFUNDED:
        return {"state": "refunded", "message": attempt.note or NOT_NEEDED, "amount": f"{money(attempt.paid_amount):,.2f}",
                "refundFailed": attempt.refund_status == "failed"}
    if attempt.status in (S.FLAGGED, S.HANDLED):
        return {"state": "flagged", "message": attempt.note}
    if attempt.status in (S.EXPIRED, S.CANCELLED):
        return {"state": "ended"}
    left = max(0, int((attempt.expires_at - timezone.now()).total_seconds()))
    return {"state": "waiting", "secondsLeft": left}


def answer(attempt):
    """What the Payments window is told about a QR code: the state, and while it can be paid, the QR code and the payment page's address."""
    out = {"success": True, "token": attempt.token, "bookingId": attempt.reservation_id, "amount": f"{money(attempt.amount):,.2f}",
           "purposeLabel": attempt.get_purpose_display(), "minutes": MINUTES, **urls_for(attempt), **state(attempt)}
    if attempt.status == S.CREATED and attempt.checkout_url:
        out["checkoutUrl"] = attempt.checkout_url
        out["qr"] = walkin_payment.qr_image(attempt.checkout_url)
    return out


# ---------------------------------------------------------------- 1. START
def start(*, request, staff, staff_name, reservation_id, purpose, amount, note="", request_id=""):
    """Opens the payment page for money an existing booking owes. Returns what the window needs (see `answer`); raises payments.PaymentError when it cannot be
    paid online now (nothing has been recorded or charged)."""
    if not gateway.enabled():
        raise payments.PaymentError("Online payment is not available right now. Take cash instead.", status=503)
    if not rate_limit.allowed(f"counterpay:{staff.pk}", *START_LIMIT):
        raise payments.PaymentError("That was a lot of QR codes in a short time. Please wait a minute and try again.", status=429)
    if purpose not in PURPOSES:
        raise payments.PaymentError("Choose what this payment is for. If this page has been open for a while, refresh it.")
    amount = money(amount)
    note = " ".join(str(note or "").split())[:payments.REASON_MAX]
    request_id = (request_id or "").strip()
    if request_id and not _REQUEST_ID.fullmatch(request_id):
        request_id = ""
    now = timezone.now()

    # The same press again (a double tap, a screen that was refreshed): the same page, never a second one.
    if request_id:
        earlier = PaymentAttempt.objects.filter(staff=staff, request_id=request_id).first()
        if earlier is not None:
            if earlier.status not in (S.CREATED, S.CANCELLED, S.EXPIRED):
                return answer(earlier)                                          # money moved: show what happened to it, never start another
            if (earlier.reservation_id == reservation_id and earlier.status == S.CREATED and earlier.purpose == PURPOSES[purpose]
                    and earlier.amount == amount and earlier.checkout_url and earlier.expires_at > now):
                return answer(earlier)
            PaymentAttempt.objects.filter(pk=earlier.pk).update(request_id="")

    # The same rule as cash at the counter, read under the booking's lock (nothing is written here).
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update(of=("self",)).filter(pk=reservation_id).first()
        if reservation is None:
            raise payments.PaymentError("That booking was not found. Please refresh the page.", status=404)
        if not ledger.counts_as_tracked(reservation) or reservation.status == Reservation.Status.CANCELLED:
            raise payments.PaymentError(f"This booking is {reservation.status.lower()}, so it can't take a payment.", status=409)
        payments.check_amount(ledger.statement(Reservation.objects.get(pk=reservation.pk)), purpose, amount)
    if amount < 1:
        raise payments.PaymentError("The smallest online payment is ₱1.00. Take cash for this one.")
    if amount > max_amount():
        parts = "Pay it in parts, or take cash." if purpose in (Payment.Purpose.BALANCE, Payment.Purpose.EXTRA) else "Take cash for this one."
        raise payments.PaymentError(f"At most {peso(max_amount())} can be paid online at once. {parts}")

    # An older QR code of this booking (any staff member's) is closed first; one that turns out to be paid is shown instead of opening another.
    for other in PaymentAttempt.objects.filter(source=SHOP, reservation_id=reservation_id, status=S.CREATED).order_by("id"):
        closed = online_checkout.cancel_attempt(other.pk)
        if closed is not None and closed.status in _MONEY_IN:
            return answer(closed)
        if closed is not None and closed.status == S.CREATED:
            raise payments.PaymentError("An earlier QR code of this booking could not be closed just now. Please try again in a moment.", status=409)
    paying = PaymentAttempt.objects.filter(source=SHOP, reservation_id=reservation_id, status=S.PAID, paid_at__gte=now - PAID_WAIT).order_by("-id").first()
    if paying is not None:
        return answer(paying)

    try:
        with transaction.atomic():
            # Two staff pressing at the same moment: the booking's lock lets only one of them open a code.
            Reservation.objects.select_for_update(of=("self",)).filter(pk=reservation_id).first()
            if PaymentAttempt.objects.filter(source=SHOP, reservation_id=reservation_id, status=S.CREATED).exists():
                raise payments.PaymentError("Another QR code of this booking is just being opened. Close this window and open the booking again.", status=409)
            attempt = PaymentAttempt.objects.create(
                token=secrets.token_urlsafe(24), source=SHOP, staff=staff, customer=None, customer_name=reservation.display_customer_name[:120],
                purpose=PURPOSES[purpose], amount=amount, payload={"staff_name": staff_name, "note": note, "code": reservation.reference_code},
                request_id=request_id, reservation=reservation, livemode=not gateway.is_test(), expires_at=now + timedelta(minutes=MINUTES))
    except IntegrityError:
        raise payments.PaymentError("The payment page is opening. Please wait a moment.", status=409)
    done_path = reverse("gowns:walkin_pay_done", args=[attempt.token])
    try:
        session = gateway.create_checkout_session(
            amount=amount, name=_NAMES[purpose], description=f"Arabela Gown Rental: booking {reservation.reference_code}", reference_number=attempt.token,
            success_url=online_checkout.absolute(request, done_path), cancel_url=online_checkout.absolute(request, done_path),
            metadata={"attempt": attempt.pk, "purpose": attempt.purpose, "shop": 1}, customer_name=attempt.customer_name, customer_email="",
            expires_minutes=MINUTES)
    except gateway.GatewayError as exc:
        logger.warning("%s payment page could not be opened for counter attempt %s: %s", gateway.label(), attempt.pk, exc)
        PaymentAttempt.objects.filter(pk=attempt.pk, status=S.CREATED).update(
            status=S.EXPIRED, note=f"{gateway.label()} would not open a payment page.", finished_at=timezone.now())
        raise payments.PaymentError("We could not open the payment page just now. Nothing was charged. Please try again in a moment.", status=502)
    PaymentAttempt.objects.filter(pk=attempt.pk).update(session_id=session.id, checkout_url=session.checkout_url)
    return answer(PaymentAttempt.objects.get(pk=attempt.pk))


# ---------------------------------------------------------------- 2. the payment, once the money is confirmed
def record(attempt):
    """Writes the payment of a paid attempt on its booking. Runs inside online_checkout._make_booking's transaction; raises
    online_checkout.BookingUnavailable (nothing written) when the booking can no longer take this money -- the payment then goes back."""
    reservation = Reservation.objects.select_for_update(of=("self",)).filter(pk=attempt.reservation_id).first()
    if reservation is None:
        raise online_checkout.BookingUnavailable("The booking no longer exists.")
    payload = attempt.payload or {}
    purpose = next(key for key, value in PURPOSES.items() if value == attempt.purpose)
    try:
        payment = payments.record_online_booking_payment(
            reservation=reservation, purpose=purpose, amount=attempt.paid_amount, channel=attempt.channel, provider_ref=attempt.provider_payment_id,
            paid_at=attempt.paid_at, livemode=attempt.livemode, request_id=f"pm-{attempt.pk}", note=payload.get("note", ""), served_by_user=attempt.staff)
    except payments.PaymentError as exc:
        logger.warning("The online payment of counter attempt %s is not needed any more: %s", attempt.pk, exc.message)
        raise online_checkout.BookingUnavailable(NOT_NEEDED)
    # The same Audit Log line a payment recorded at the counter writes (this runs from the payment's confirmation, not from a staff request).
    from accounts import audit
    from arabela_admin import audit_describe
    kind, target_id, label = audit_describe._booking_target(reservation)
    audit.record(attempt.staff, category="Payments", action="payment.record", actor_name=payload.get("staff_name") or "",
                 summary=f"recorded a payment of {peso(payment.amount)} (the customer paid online on their phone) on {audit_describe._booking_text(reservation)}",
                 target_type=kind, target_id=str(target_id), target_label=label,
                 details={"receipt": payment.receipt_number, "method": payment.get_method_display()})
    return reservation


# ---------------------------------------------------------------- 3. the window, and money coming in another way
def refreshed_state(attempt):
    """Asks Xendit (at most every couple of seconds) and returns the up-to-date attempt and state. A page that ran out is closed here, so the screen never hangs."""
    attempt = online_checkout.confirm_attempt(attempt.pk, source="poll") or attempt
    if attempt.status == S.CREATED and attempt.expires_at <= timezone.now():
        attempt = online_checkout.cancel_attempt(attempt.pk) or attempt
    return attempt, state(attempt)


def window_state(reservation_id):
    """What the booking's window shows about its QR code: None when there is nothing to show, else the `answer` of the one still open (after asking Xendit once;
    a payment that came in while no screen was watching is recorded by that look)."""
    attempt = PaymentAttempt.objects.filter(source=SHOP, reservation_id=reservation_id, status__in=(S.CREATED, S.PAID)).order_by("-id").first()
    if attempt is None:
        return None
    attempt, now_state = refreshed_state(attempt)
    return None if now_state["state"] == "ended" else answer(attempt)


def close_open(reservation_id):
    """Close this booking's open QR codes before money is recorded another way. True when one of them turned out to be paid (it is recorded now, or being
    sent back), so the caller can ask staff to look at the amounts again."""
    came_in = False
    for attempt in PaymentAttempt.objects.filter(source=SHOP, reservation_id=reservation_id, status__in=(S.CREATED, S.PAID)).order_by("id"):
        if attempt.status == S.PAID:
            online_checkout.confirm_attempt(attempt.pk, source="sweep")
            came_in = True
            continue
        closed = online_checkout.cancel_attempt(attempt.pk)
        if closed is not None and closed.status in _MONEY_IN:
            came_in = True
    return came_in
