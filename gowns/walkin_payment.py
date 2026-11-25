"""A walk-in customer pays ON THEIR PHONE: staff press Save in New Booking, a QR code appears, the customer scans it and pays on Xendit's page, and the
booking is made by itself the moment the money is confirmed.

It is the online checkout's engine (gowns/online_checkout.py) with a different front:

  * START (`start`). The staff member's New Booking form is checked exactly as a cash booking is (every gown free on its date, the payment choice, the
    notebook number) but NOTHING IS BOOKED. The form is kept in a PaymentAttempt (source "walkin", the staff member in `staff`, no customer account) and a
    Xendit payment page is opened for the exact amount: the deposit alone, or the rental and the deposit. The page closes by itself after WALKIN_MINUTES.
  * PAY. The customer scans the QR code and pays on Xendit's page (the TEST key's page is a simulation that moves no money).
  * CONFIRM. The same `online_checkout.confirm_attempt` as the website's checkout: it asks Xendit, never believing the caller or the webhook's body, and then
    `create_booking` below makes the booking in ONE transaction through the SAME function a cash booking uses (`staff_booking.record_booking`), so every
    rule (the dates, the rest days, "Customer takes the gown now", the owner's backdating) is identical. If the gown was taken while the customer paid,
    the whole payment is sent back instead.

The staff screen only DISPLAYS: it asks `state` every few seconds and never decides that anything is paid."""
import logging
import re
import secrets
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

from accounts import rate_limit
from gowns import availability, deposit, online_checkout
from reservations import gateway, ledger, staff_booking
from reservations.ledger import money
from reservations.models import Payment, PaymentAttempt, Reservation

logger = logging.getLogger(__name__)

WALKIN_MINUTES = 15                         # the payment page stays open this long (Xendit refuses 10 minutes or less): somebody is standing at the counter
START_LIMIT = (20, 10 * 60)                 # QR codes per staff member per 10 minutes
PAID_WAIT = timedelta(minutes=10)           # a payment that came in but is not booked yet blocks a second one for the same gowns for this long
_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{8,40}")
S = PaymentAttempt.Status


def qr_image(url):
    """The payment page's address as a QR code (a small PNG as a data address), drawn here so no outside service ever sees the link. '' when the
    drawing library is missing: the screen then still offers the link itself."""
    try:
        import segno
        return segno.make(url, error="m").png_data_uri(scale=8, border=3, dark="#0b0c0c")
    except ImportError:
        logger.warning("The QR drawing library (segno) is not installed: the payment screen shows the link only")
        return ""
    except Exception:                                       # noqa: BLE001
        logger.exception("The payment QR code could not be drawn")
        return ""


def _selection(lines):
    """What makes two walk-in forms the same booking to pay for: each gown with its event date."""
    return sorted((int(line["gown"]), str(line["event"]), bool(line.get("takes_now"))) for line in lines)


def _stored_lines(lines):
    return [{"gown": line["gown"].id, "event": line["event"].isoformat(), "takes_now": bool(line["takes_now"]),
             "picked_up_on": line["picked_up_on"].isoformat() if line.get("picked_up_on") else ""} for line in lines]


def urls_for(attempt):
    return {
        "statusUrl": reverse("arabela_admin:booking_pay_status", args=[attempt.token]),
        "cancelUrl": reverse("arabela_admin:booking_pay_cancel", args=[attempt.token]),
    }


def answer(attempt):
    """What the staff screen is told when a payment is started (or the same press is repeated): the state, the QR and where to ask next."""
    out = {"success": True, "token": attempt.token, "amount": f"{money(attempt.amount):,.2f}", "minutes": WALKIN_MINUTES,
           "purposeLabel": attempt.get_purpose_display(), **urls_for(attempt), **state(attempt)}
    if attempt.status == S.CREATED and attempt.checkout_url:
        out["checkoutUrl"] = attempt.checkout_url
        out["qr"] = qr_image(attempt.checkout_url)
    return out


# ---------------------------------------------------------------- 1. START
def start(*, request, staff, staff_name, is_owner, name, phone, address, city, postal_code, account, notebook_number, lines, payment, note,
          request_id, expected_amount=None, id_check=None):
    """Opens the payment page for a walk-in form. Returns what the screen needs (see `answer`); raises staff_booking.BookingError when the form cannot
    be paid for (nothing has been saved)."""
    if not gateway.enabled():
        raise staff_booking.BookingError("Online payment is not available right now. Take cash, or a GCash reference, instead.", 503)
    if not rate_limit.allowed(f"walkinpay:{staff.pk}", *START_LIMIT):
        raise staff_booking.BookingError("That was a lot of QR codes in a short time. Please wait a minute and try again.", 429)
    staff_booking.check_payment(payment=payment, lines=lines)
    if notebook_number:
        taken = staff_booking.notebook_number_owner(notebook_number)
        if taken:
            raise staff_booking.BookingError(f"Notebook number {notebook_number} is already on {taken.reference_code} "
                                             f"({taken.display_customer_name}). Check the number.", status=409)
    request_id = (request_id or "").strip()
    if request_id and not _REQUEST_ID.fullmatch(request_id):
        request_id = ""
    purpose = PaymentAttempt.Purpose.FULL if payment == staff_booking.PAYMENT_FULL else PaymentAttempt.Purpose.DEPOSIT
    stored = _stored_lines(lines)
    key = _selection(stored)
    now = timezone.now()

    # The same press again (a double tap, a screen that was refreshed): the same page, never a second one.
    if request_id:
        earlier = PaymentAttempt.objects.filter(staff=staff, request_id=request_id).first()
        if earlier is not None:
            if earlier.status not in (S.CREATED, S.CANCELLED, S.EXPIRED):
                return answer(earlier)                                          # money moved: show what happened to it, never start another
            same = earlier.purpose == purpose and _selection((earlier.payload or {}).get("lines", [])) == key
            if same and earlier.status == S.CREATED and earlier.checkout_url and earlier.expires_at > now:
                return answer(earlier)
            PaymentAttempt.objects.filter(pk=earlier.pk).update(request_id="")
            if earlier.status == S.CREATED:
                closed = online_checkout.cancel_attempt(earlier.pk)
                if closed is not None and closed.status != S.CANCELLED:
                    return answer(closed)

    # An older page of THIS staff member for the same gowns is closed first (a refreshed New Booking screen would otherwise leave two payable pages);
    # one that turns out to be paid is shown instead of starting another.
    for other in PaymentAttempt.objects.filter(staff=staff, source=PaymentAttempt.Source.WALKIN, status=S.CREATED).order_by("id"):
        if _selection((other.payload or {}).get("lines", [])) == key:
            closed = online_checkout.cancel_attempt(other.pk)
            if closed is not None and closed.status in (S.PAID, S.BOOKED, S.REFUNDED, S.FLAGGED, S.HANDLED):
                return answer(closed)
    paying = PaymentAttempt.objects.filter(staff=staff, source=PaymentAttempt.Source.WALKIN, status=S.PAID, paid_at__gte=now - PAID_WAIT).order_by("-id")
    for other in paying:
        if _selection((other.payload or {}).get("lines", [])) == key:
            return answer(other)

    # Every gown must be free right now, under the same locks a cash booking takes. Nothing is written: the locks are released when this block ends.
    with transaction.atomic():
        staff_booking.lock_and_check(lines, now=availability.now_local(), is_owner=is_owner)
        rental_subtotal = sum((line["gown"].rental_price for line in lines), Decimal("0"))
        security_deposit = deposit.for_gowns(len(lines))
    amount = security_deposit if purpose == PaymentAttempt.Purpose.DEPOSIT else rental_subtotal + security_deposit
    if expected_amount is not None and money(expected_amount) != money(amount):
        raise staff_booking.BookingError(f"The amount changed to ₱{amount:,.2f} (the price or the deposit was updated). Please check the amounts and try again.", 409)
    if amount > min(online_checkout.MAX_ONLINE_AMOUNT, Decimal(gateway.max_amount())):
        raise staff_booking.BookingError("This booking is too large to pay online. Take cash, or choose \"Deposit only\".", 400)

    payload = {
        "lines": stored,
        "details": {"name": name, "phone": phone, "address": address, "city": city, "postal_code": postal_code},
        "account_id": account.id if account is not None else None,
        "notebook_number": notebook_number,
        "note": note,
        "payment": payment,
        "is_owner": bool(is_owner),
        "staff_name": staff_name,
        "rental_subtotal": str(rental_subtotal),
        "security_deposit": str(security_deposit),
        "identity_check": id_check.pk if id_check is not None else None,       # the customer's valid-ID check the booking will rest on
    }
    try:
        with transaction.atomic():
            attempt = PaymentAttempt.objects.create(
                token=secrets.token_urlsafe(24), source=PaymentAttempt.Source.WALKIN, staff=staff, customer=None, customer_name=name[:120], purpose=purpose,
                amount=amount, payload=payload, request_id=request_id, livemode=not gateway.is_test(),
                expires_at=now + timedelta(minutes=WALKIN_MINUTES),
            )
    except IntegrityError:
        raise staff_booking.BookingError("The payment page is opening. Please wait a moment.", 409)
    done_path = reverse("gowns:walkin_pay_done", args=[attempt.token])
    what = "Security deposit" if purpose == PaymentAttempt.Purpose.DEPOSIT else "Gown rental and security deposit"
    gowns = ", ".join(line["gown"].name for line in lines)[:140]
    try:
        session = gateway.create_checkout_session(
            amount=amount, name=what, description=f"Arabela Gown Rental: {gowns}", reference_number=attempt.token,
            success_url=online_checkout.absolute(request, done_path), cancel_url=online_checkout.absolute(request, done_path),
            metadata={"attempt": attempt.pk, "purpose": purpose, "walkin": 1}, customer_name=name[:120], customer_email="",
            expires_minutes=WALKIN_MINUTES,
        )
    except gateway.GatewayError as exc:
        logger.warning("%s payment page could not be opened for walk-in attempt %s: %s", gateway.label(), attempt.pk, exc)
        PaymentAttempt.objects.filter(pk=attempt.pk, status=S.CREATED).update(
            status=S.EXPIRED, note=f"{gateway.label()} would not open a payment page.", finished_at=timezone.now())
        raise staff_booking.BookingError("We could not open the payment page just now. Nothing was charged. Please try again in a moment.", 502)
    PaymentAttempt.objects.filter(pk=attempt.pk).update(session_id=session.id, checkout_url=session.checkout_url)
    return answer(PaymentAttempt.objects.get(pk=attempt.pk))


# ---------------------------------------------------------------- 2. the booking, once the money is confirmed
def create_booking(attempt):
    """Makes the booking for a paid walk-in attempt. Runs inside the caller's transaction (online_checkout._make_booking); raises
    online_checkout.BookingUnavailable (before anything that matters is written) when it can no longer be booked, and the payment then goes back."""
    payload = attempt.payload or {}
    details = payload.get("details") or {}
    is_owner = bool(payload.get("is_owner"))
    now = availability.now_local()
    try:
        lines = staff_booking.parse_lines(payload.get("lines"), today=now.date(), is_owner=is_owner)
        try:
            account = staff_booking.find_account(payload.get("account_id"))
        except staff_booking.BookingError:
            account = None                                   # the linked website account is gone: the booking is still made, just not linked
        from reservations.models import IdentityCheck
        id_check = IdentityCheck.objects.filter(pk=payload["identity_check"]).first() if payload.get("identity_check") else None
        reservation = staff_booking.record_booking(
            recorded_by=attempt.staff, is_owner=is_owner, staff_name=payload.get("staff_name") or "the shop", channel=Reservation.Channel.WALK_IN,
            name=details.get("name") or attempt.customer_name, phone=details.get("phone", ""), account=account, address=details.get("address", ""),
            city=details.get("city", ""), postal_code=details.get("postal_code", ""), notebook_number=payload.get("notebook_number", ""),
            lines=lines, payment=payload.get("payment") or staff_booking.PAYMENT_DEPOSIT, method="", reference="", amount=attempt.paid_amount,
            note=payload.get("note", ""), request_id="", now=now, online_attempt=attempt, identity_check=id_check)
    except staff_booking.BookingError as exc:
        raise online_checkout.BookingUnavailable(exc.message)
    # The same Audit Log line a cash booking writes (this runs from the payment's confirmation, not from a staff request, so it is written here).
    from accounts import audit
    from arabela_admin import audit_describe
    kind, target_id, label = audit_describe._booking_target(reservation)
    audit.record(attempt.staff, category="Bookings", action="booking.create", actor_name=payload.get("staff_name") or "",
                 summary=f"recorded {audit_describe._booking_text(reservation)} (the customer paid online on their phone)",
                 target_type=kind, target_id=str(target_id), target_label=label, details={"reference_code": reservation.reference_code})
    return reservation


# ---------------------------------------------------------------- 3. what the staff screen shows
def state(attempt):
    """The staff screen's picture of an attempt, as plain JSON: waiting | confirming | booked | refunded | flagged | ended."""
    if attempt.status == S.BOOKED and attempt.reservation_id:
        reservation = attempt.reservation
        payment = reservation.payments.filter(provider__in=gateway.PROVIDERS, direction=Payment.Direction.IN).order_by("id").first()
        balance = ledger.statement(Reservation.objects.get(pk=reservation.pk)).balance_due
        return {
            "state": "booked", "reference_code": reservation.reference_code, "status": reservation.status,
            "receiptUrl": reverse("arabela_admin:payment_receipt_print", args=[payment.pk]) if payment else "",
            "receiptNumber": payment.receipt_number if payment else "",
            "balance": f"{balance:,.2f}", "depositOnly": attempt.purpose == PaymentAttempt.Purpose.DEPOSIT,
            "paidWith": gateway.CHANNEL_LABELS.get(attempt.channel, gateway.label()), "paidAmount": f"{money(attempt.paid_amount):,.2f}", "test": not attempt.livemode,
            "paymentsUrl": reverse("arabela_admin:payments") + "?" + urlencode({"open": reservation.pk}),
            "url": reverse("arabela_admin:active_reservations") + "?" + urlencode({"search": reservation.reference_code}),
            "where": "Active Reservations",
        }
    if attempt.status == S.PAID:
        return {"state": "confirming"}
    if attempt.status == S.REFUNDED:
        return {"state": "refunded", "message": attempt.note or "The booking could not be made.", "amount": f"{money(attempt.paid_amount):,.2f}",
                "refundFailed": attempt.refund_status == "failed"}
    if attempt.status in (S.FLAGGED, S.HANDLED):
        return {"state": "flagged", "message": attempt.note}
    if attempt.status in (S.EXPIRED, S.CANCELLED):
        return {"state": "ended"}
    left = max(0, int((attempt.expires_at - timezone.now()).total_seconds()))
    return {"state": "waiting", "secondsLeft": left}


def refreshed_state(attempt):
    """Asks Xendit (at most every couple of seconds) and returns the up-to-date state. A page that ran out is closed here, so the screen never hangs."""
    attempt = online_checkout.confirm_attempt(attempt.pk, source="poll") or attempt
    if attempt.status == S.CREATED and attempt.expires_at <= timezone.now():
        attempt = online_checkout.cancel_attempt(attempt.pk) or attempt
    return attempt, state(attempt)
