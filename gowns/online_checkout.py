"""The online checkout through PayMongo: everything between "the customer presses Pay" and "the booking exists" (or the money is sent back).

The shape of it (decided with the owner, 2026-11-24):

  1. START (`start_payment`). The checkout page sends the same selection the older checkout sent. It is checked the same way (Terms, lock, limits,
     the deposit the page showed, every gown free on its date, every price from the database) but NOTHING IS BOOKED. The selection is kept in a
     PaymentAttempt, and PayMongo opens a checkout for the exact amount: the deposit alone, or the rental and the deposit.
  2. PAY. The customer pays on PayMongo's own page (GCash, Maya or QR Ph; the TEST key's page is a simulation that moves no money).
  3. CONFIRM (`confirm_attempt`). Whoever hears of the payment first -- the customer's return page, PayMongo's webhook, or the sweep -- calls the
     one function. It ASKS PAYMONGO (never believing the caller or the webhook's body) whether the session was paid and for how much, then makes the
     booking in ONE transaction: the booking is confirmed at once, with its gown lines, rest days, timeline, and its Payment Receipt.
     If the gown was taken while the customer paid, the whole payment is sent back instead (`refund_attempt`) and the customer is told.
  4. A session never expires on its own, so stale attempts are closed by `expire_stale`; a payment that still arrives afterwards is honoured when
     the gown is free and refunded when it is not.

The older checkout (gowns/views.py reservation_submit) is NOT touched: it keeps working while PayMongo has no key. Its helpers are reused here
(`_plan_pickup`, `_authoritative_price`, ...) so a gown is judged free by exactly the same rules."""
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone

from accounts import rate_limit, terms
from accounts.models import CustomerMessage, UserProfile
from accounts.services import cancel_lockout_notice
from gowns import availability, deposit, late_fee, reservation_limits
from gowns import views as checkout
from gowns.context_processors import RESERVATION_HOLD_MINUTES
from gowns.models import COOLDOWN_DAYS, Gown, GownUnavailability
from reservations import gateway, payments
from reservations.ledger import money, peso
from reservations.models import Payment, PaymentAttempt, Reservation, ReservationItem, ReservationStatusEvent
from reservations.staff_booking import APPROVED_LABEL

logger = logging.getLogger(__name__)

START_LIMIT = (10, 10 * 60)             # Pay presses per account per 10 minutes (a real customer presses it once or twice)
POLL_GAP = timedelta(seconds=2)         # the return page may ask PayMongo about one attempt at most this often
SWEEP_GRACE = timedelta(minutes=1)      # an attempt is stale this long after its own 20 minutes
SWEEP_BATCH = 5
MAX_ONLINE_AMOUNT = Decimal("100000")   # PayMongo's e-wallet limit per payment; a larger booking is paid at the shop instead
PAYMENT_EVENT_LABEL = payments.PAYMENT_EVENT

REOPENABLE = (PaymentAttempt.Status.EXPIRED, PaymentAttempt.Status.CANCELLED)
_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{8,40}")


class BookingUnavailable(Exception):
    """The paid selection can no longer be booked (a gown was taken, a date passed). `message` is written for the customer."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _fail(message, status=400, **extra):
    return JsonResponse({"success": False, "error": message, **extra}, status=status)


def absolute(request, path):
    """A full address for PayMongo to send the customer back to."""
    base = (getattr(settings, "SITE_BASE_URL", "") or "").strip().rstrip("/")
    return f"{base}{path}" if base else request.build_absolute_uri(path)


# ---------------------------------------------------------------- picking the gowns (the same rules the older checkout uses)
def _resolve_items(items, *, authoritative_price):
    """Finds a free physical gown for every selected item (MUST run inside transaction.atomic(): it takes the same row locks the older checkout does).

    Mutates each item: `matched_gown` (the unit, or None for a gown with no inventory row), `rental_date` (the real pick-up day the server works
    out), and -- when `authoritative_price` -- `price` from the database. Returns None when every item can be booked, else (message, status)."""
    assigned_unit_ids = set()
    now = availability.now_local()
    for item in sorted(items, key=lambda i: i["gown_name"].casefold()):
        event_date = item["return_date"] - timedelta(days=availability.RETURN_DAYS)
        outcome, unit, pickup = checkout._plan_pickup(
            item["gown_name"], event_date, gown_slug=item["gown_slug"], size=item["size"], already_taken=assigned_unit_ids, now=now)
        if outcome in (checkout._UNIT_DATE_PASSED, checkout._UNIT_TODAY_CLOSED):
            if outcome == checkout._UNIT_DATE_PASSED:
                return (f"The date {event_date:%b %d, %Y} for {item['gown_name']} has already passed. "
                        "Please open it from your order summary and choose new dates.", 400)
            return (f"Today's pick-up closed at {availability.PICKUP_CLOSES.strftime('%I:%M %p').lstrip('0')}. "
                    f"Please open {item['gown_name']} from your order summary and choose another date.", 400)
        if outcome != checkout._UNIT_UNAVAILABLE and pickup is not None:
            item["rental_date"] = pickup
        if outcome == checkout._UNIT_UNAVAILABLE:
            copies = sum(1 for other in items if other["gown_name"].casefold() == item["gown_name"].casefold()
                         and checkout._normalize_size(other["size"]) == checkout._normalize_size(item["size"]))
            if copies > 1:
                return (f"{item['gown_name']} is in your selection {copies} times, but there aren't {copies} of it free for "
                        f"{event_date:%b %d, %Y}. Please remove the extra ones from your selection and try again.", 400)
            return (f"{item['gown_name']} is no longer available for {event_date:%b %d, %Y}. "
                    "Please choose a different date or another gown.", 400)
        if outcome == checkout._UNIT_OUT_OF_STOCK:
            return (f"{item['gown_name']} is no longer available to rent -- it has been removed from the collection. "
                    "Please take it out of your bag or choose another gown.", 400)
        if outcome in (checkout._UNIT_SIZE_NOT_OFFERED, checkout._UNIT_SIZE_REQUIRED):
            size_value = checkout._normalize_size(item["size"])
            if outcome == checkout._UNIT_SIZE_NOT_OFFERED:
                return (f"{item['gown_name']} doesn't come in {size_value}. Please open it from your order summary and choose one of its sizes.", 400)
            return (f"Please choose a size for {item['gown_name']}: open it from your order summary and pick one of its sizes.", 400)
        item["matched_gown"] = unit
        if unit is not None:
            assigned_unit_ids.add(unit.id)
        if authoritative_price:
            price = checkout._authoritative_price(item["gown_name"], item["gown_slug"], unit)
            if price is None:
                return (f"{item['gown_name']} could not be verified. Please refresh the page and try again.", 400)
            item["price"] = price
    return None


# ---------------------------------------------------------------- 1. START
def _selection_key(rows):
    """What makes two selections the same booking to pay for: each gown, its size and its return date (the event is two days before it)."""
    return sorted((str(r["gown_name"]).casefold(), str(r.get("gown_slug", "")), str(r.get("size", "")), str(r["return_date"])) for r in rows)


def _parse_items(raw_items):
    parsed = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            return None
        gown_name = (raw.get("gown_name") or raw.get("name") or "").strip()
        rental_date = checkout._parse_date(raw.get("rental_date"))
        return_date = checkout._parse_date(raw.get("return_date"))
        if not gown_name or not rental_date or not return_date:
            return None
        parsed.append({
            "gown_name": gown_name,
            "gown_slug": (raw.get("gown_slug") or raw.get("slug") or "").strip()[:150],
            "size": (raw.get("size") or "").strip()[:20],
            "rental_date": rental_date,
            "return_date": return_date,
        })
    return parsed


def start_payment(request):
    """POST from the checkout page: validates, keeps the selection in a PaymentAttempt and opens PayMongo's checkout. A JsonResponse with
    {"success": True, "checkout_url": ...} or the same kind of refusal the older checkout gives (the page already knows how to show each one)."""
    if not gateway.enabled():
        return _fail("Online payment is not available right now. Please try again later.", 503)
    user = request.user
    refusal = checkout._terms_refusal(request)
    if refusal:
        return refusal
    try:
        raw_items = json.loads(request.POST.get("items") or "[]")
    except ValueError:
        return _fail("Invalid request data.")
    if not isinstance(raw_items, list):
        return _fail("Invalid request data.")
    if not rate_limit.allowed(f"payattempt:{user.pk}", *START_LIMIT):
        return checkout._too_fast()

    expected_user = (request.POST.get("expected_user") or "").strip()
    if expected_user and expected_user != str(user.pk):
        return _fail("The account signed in on this browser changed since this page was opened (for example, in another tab), so nothing was "
                     "booked. Please reload the page, check that you are in the right account, and try again.", 409, account_changed=True)
    lockout_seconds, lockout_message = cancel_lockout_notice(user)
    if lockout_seconds:
        return _fail(lockout_message, 403, locked=True)
    if not raw_items:
        return _fail("Your bag is empty.")
    items = _parse_items(raw_items)
    if items is None:
        return _fail("Each item needs a gown and valid rental dates.")

    limit_message = reservation_limits.gown_count_message(len(items)) or reservation_limits.open_bookings_message(user)
    if limit_message:
        return _fail(limit_message, 400, limit_reached=True)

    current_deposit = deposit.for_gowns(len(items))
    expected_raw = (request.POST.get("expected_deposit") or "").strip()
    if expected_raw:
        try:
            expected_deposit = Decimal(expected_raw)
        except InvalidOperation:
            expected_deposit = None
        if expected_deposit is not None and expected_deposit != current_deposit:
            per_gown = deposit.per_gown()
            return _fail(f"The security deposit changed to ₱{per_gown:,} per gown, so your deposit is now ₱{int(current_deposit):,}. "
                         "Please check the amount and try again.", 409, deposit_changed=True, deposit_per_gown=per_gown)

    purpose = (request.POST.get("pay_option") or "").strip()
    if purpose not in PaymentAttempt.Purpose.values:
        return _fail("Please choose how much to pay now: the deposit only, or everything.")

    request_id = (request.POST.get("request_id") or "").strip()
    if request_id and not _REQUEST_ID.fullmatch(request_id):
        request_id = ""
    if request_id:
        earlier = PaymentAttempt.objects.filter(customer=user, request_id=request_id).first()
        if earlier is not None:
            same = earlier.purpose == purpose and _selection_key(earlier.payload.get("items", [])) == _selection_key(
                [{"gown_name": i["gown_name"], "gown_slug": i["gown_slug"], "size": i["size"], "return_date": i["return_date"].isoformat()} for i in items])
            # Pressing Pay again (a double tap, a retry after a dropped connection, the Back button from PayMongo's page) is the SAME attempt when
            # nothing has changed: no second checkout is opened.
            if same and earlier.status == PaymentAttempt.Status.CREATED and earlier.checkout_url:
                return JsonResponse({"success": True, "checkout_url": earlier.checkout_url, "token": earlier.token})
            if same and earlier.status != PaymentAttempt.Status.CREATED:
                return JsonResponse({"success": True, "checkout_url": reverse("gowns:payment_return", args=[earlier.token]), "token": earlier.token})
            # A different choice (or a checkout that never opened): the old one gives up its id, is closed so it can no longer be paid, and a fresh
            # one is made below. A payment that already went through is never thrown away: the customer is shown what happened to it.
            PaymentAttempt.objects.filter(pk=earlier.pk).update(request_id="")
            if earlier.status == PaymentAttempt.Status.CREATED:
                closed = cancel_attempt(earlier.pk)
                if closed is not None and closed.status != PaymentAttempt.Status.CANCELLED:
                    return JsonResponse({"success": True, "checkout_url": reverse("gowns:payment_return", args=[earlier.token]), "token": earlier.token})

    profile, _ = UserProfile.objects.get_or_create(user=user)
    typed_name = f"{(request.POST.get('first_name') or '').strip()} {(request.POST.get('last_name') or '').strip()}".strip()
    customer_name = (typed_name or profile.display_name or f"{user.first_name} {user.last_name}".strip() or user.get_username())[:120]
    details = {
        "customer_name": customer_name,
        "phone": (request.POST.get("phone") or "").strip()[:20],
        "address": (request.POST.get("address") or "").strip()[:255],
        "city": (request.POST.get("city") or "").strip()[:100],
        "postal_code": (request.POST.get("postal_code") or "").strip()[:10],
    }

    # Every gown must be free right now (the same rules, the same locks, as the older checkout) and every price comes from the database.
    # Nothing is written: the locks are released when this block ends, and the selection is only KEPT in the attempt below.
    with transaction.atomic():
        get_user_model().objects.select_for_update().filter(pk=user.pk).first()
        problem = _resolve_items(items, authoritative_price=True)
    if problem is not None:
        return _fail(problem[0], problem[1])

    rental_subtotal = sum((item["price"] for item in items), Decimal("0"))
    amount = current_deposit if purpose == PaymentAttempt.Purpose.DEPOSIT else rental_subtotal + current_deposit
    if amount > min(MAX_ONLINE_AMOUNT, Decimal(gateway.max_amount())):
        return _fail("This booking is too large to pay online. Please pay at the shop, or choose the deposit only.", 400)

    now = timezone.now()
    payload = {
        "items": [{
            "gown_name": i["gown_name"], "gown_slug": i["gown_slug"], "size": i["size"], "event_date": (i["return_date"] - timedelta(days=availability.RETURN_DAYS)).isoformat(),
            "return_date": i["return_date"].isoformat(), "planned_pickup": i["rental_date"].isoformat(), "price": str(i["price"]),
        } for i in items],
        "details": details,
        "rental_subtotal": str(rental_subtotal),
        "security_deposit": str(current_deposit),
        "late_fee_per_day": late_fee.per_day(),
    }
    try:
        with transaction.atomic():
            attempt = PaymentAttempt.objects.create(
                token=secrets.token_urlsafe(24), customer=user, customer_name=customer_name, purpose=purpose, amount=amount, payload=payload,
                request_id=request_id, livemode=not gateway.is_test(), expires_at=now + timedelta(minutes=RESERVATION_HOLD_MINUTES),
            )
    except IntegrityError:
        # Two presses of Pay carrying the same request id arrived at the very same moment: the other one is opening the page.
        return _fail("Your payment page is opening. Please wait a moment.", 409)
    return_path = reverse("gowns:payment_return", args=[attempt.token])
    cancel_path = reverse("gowns:payment_cancel", args=[attempt.token])
    what = "Security deposit" if purpose == PaymentAttempt.Purpose.DEPOSIT else "Gown rental and security deposit"
    gowns = ", ".join(i["gown_name"] for i in items)[:140]
    try:
        session = gateway.create_checkout_session(
            amount=amount, name=what, description=f"Arabela Gown Rental: {gowns}", reference_number=attempt.token,
            success_url=absolute(request, return_path), cancel_url=absolute(request, cancel_path),
            metadata={"attempt": attempt.pk, "purpose": purpose, "customer": user.pk}, customer_name=customer_name, customer_email=user.email or "",
            expires_minutes=RESERVATION_HOLD_MINUTES,                 # Xendit closes the page by itself when the customer's hold ends (PayMongo's never does)
        )
    except gateway.GatewayError as exc:
        logger.warning("%s checkout could not be opened for attempt %s: %s", gateway.label(), attempt.pk, exc)
        PaymentAttempt.objects.filter(pk=attempt.pk, status=PaymentAttempt.Status.CREATED).update(
            status=PaymentAttempt.Status.EXPIRED, note=f"{gateway.label()} would not open a checkout.", finished_at=timezone.now())
        return _fail("We could not open the payment page just now. Nothing was charged. Please try again in a moment.", 502)
    PaymentAttempt.objects.filter(pk=attempt.pk).update(session_id=session.id, checkout_url=session.checkout_url)
    return JsonResponse({"success": True, "checkout_url": session.checkout_url, "token": attempt.token})


# ---------------------------------------------------------------- 3. CONFIRM
def _aware(epoch):
    try:
        return datetime.fromtimestamp(int(epoch), tz=dt_timezone.utc) if epoch else None
    except (OverflowError, OSError, ValueError):
        return None


def confirm_attempt(attempt_id, *, source="poll"):
    """Ask PayMongo whether this attempt was paid and, if so, make the booking. Safe to call any number of times, from anywhere, at the same moment:
    returns the attempt as it stands. `source`: "poll" (the return page), "webhook", "sweep"."""
    attempt = PaymentAttempt.objects.filter(pk=attempt_id).first()
    if attempt is None:
        return attempt
    reopen = source == "webhook" and attempt.status in REOPENABLE         # PayMongo says it was paid after we closed it: money is never ignored
    if attempt.is_final and not reopen:
        return attempt
    if attempt.status == PaymentAttempt.Status.CREATED or reopen:
        if not attempt.session_id:
            return attempt
        if source == "poll" and attempt.last_checked_at and timezone.now() - attempt.last_checked_at < POLL_GAP:
            return attempt
        PaymentAttempt.objects.filter(pk=attempt.pk).update(last_checked_at=timezone.now())
        try:
            state = gateway.retrieve_checkout_session(attempt.session_id)
        except gateway.GatewayError as exc:
            logger.warning("Could not read %s session %s: %s", gateway.label(), attempt.session_id, exc)
            return attempt                                   # still waiting; the next look asks again
        if not state.paid:
            if state.status == "expired":
                PaymentAttempt.objects.filter(pk=attempt.pk, status=PaymentAttempt.Status.CREATED).update(
                    status=PaymentAttempt.Status.EXPIRED, finished_at=timezone.now())
            return PaymentAttempt.objects.get(pk=attempt.pk)
        _mark_paid(attempt.pk, state)
    return _make_booking(attempt.pk)


def _mark_paid(attempt_id, state):
    """The money is confirmed: write it on the attempt (once). A payment that is not what was asked for is FLAGGED for a person and never booked."""
    with transaction.atomic():
        attempt = PaymentAttempt.objects.select_for_update().get(pk=attempt_id)
        if attempt.status != PaymentAttempt.Status.CREATED and attempt.status not in REOPENABLE:
            return attempt
        attempt.provider_payment_id = state.payment_id
        attempt.channel = state.channel[:20]
        attempt.paid_amount = state.amount
        attempt.paid_at = _aware(state.paid_at) or timezone.now()
        problem = ""
        if state.id != attempt.session_id:
            problem = "The paid session is not the one this attempt opened."
        elif money(state.amount) != money(attempt.amount):
            problem = f"{gateway.label()} says {peso(state.amount)} was paid but {peso(attempt.amount)} was asked."
        elif state.reference_number and state.reference_number != attempt.token:
            problem = "The paid session carries another reference number."
        if problem:
            attempt.status = PaymentAttempt.Status.FLAGGED
            attempt.note = problem[:300]
            attempt.finished_at = timezone.now()
            logger.error("Online payment flagged (attempt %s): %s", attempt.pk, problem)
        else:
            attempt.status = PaymentAttempt.Status.PAID
        try:
            with transaction.atomic():
                attempt.save()
        except IntegrityError:
            # The same PayMongo payment is already on another attempt: never booked twice.
            attempt.provider_payment_id = ""
            attempt.status = PaymentAttempt.Status.FLAGGED
            attempt.note = f"This {gateway.label()} payment is already used by another attempt."
            attempt.finished_at = timezone.now()
            attempt.save()
        return attempt


def _make_booking(attempt_id):
    """The paid attempt becomes a booking, in one transaction. A gown that is no longer free sends the whole payment back instead."""
    attempt = PaymentAttempt.objects.get(pk=attempt_id)
    if attempt.status != PaymentAttempt.Status.PAID:
        return attempt
    try:
        with transaction.atomic():
            locked = PaymentAttempt.objects.select_for_update().get(pk=attempt_id)
            if locked.status != PaymentAttempt.Status.PAID:
                return locked
            reservation = _create_booking(locked)
            PaymentAttempt.objects.filter(pk=locked.pk).update(
                status=PaymentAttempt.Status.BOOKED, reservation=reservation, finished_at=timezone.now(), note="")
    except BookingUnavailable as exc:
        refund_attempt(attempt_id, exc.message)
    except payments.PaymentError as exc:
        logger.error("The payment of attempt %s was refused by the ledger: %s", attempt_id, exc.message)
        PaymentAttempt.objects.filter(pk=attempt_id, status=PaymentAttempt.Status.PAID).update(
            status=PaymentAttempt.Status.FLAGGED, note=str(exc.message)[:300], finished_at=timezone.now())
    except Exception:                                       # noqa: BLE001
        # Nothing was saved (the whole transaction rolled back) and the attempt is still "paid": the next look, the webhook or the sweep tries again,
        # and a person is told when it stays that way (PaymentAttempt.needs_a_look).
        logger.exception("The booking for paid attempt %s could not be made; it will be tried again", attempt_id)
    return PaymentAttempt.objects.get(pk=attempt_id)


def _create_booking(attempt):
    """Make the confirmed booking and its payment for a paid attempt. Runs inside the caller's transaction; raises BookingUnavailable (before writing
    anything that matters) when a gown can no longer be booked."""
    payload = attempt.payload or {}
    user = attempt.customer
    if user is None:
        raise BookingUnavailable("The account this payment was made from no longer exists.")
    get_user_model().objects.select_for_update().filter(pk=user.pk).first()
    items = []
    for raw in payload.get("items", []):
        items.append({
            "gown_name": raw["gown_name"], "gown_slug": raw.get("gown_slug", ""), "size": raw.get("size", ""),
            "rental_date": checkout._parse_date(raw.get("planned_pickup")) or checkout._parse_date(raw["return_date"]),
            "return_date": checkout._parse_date(raw["return_date"]), "price": Decimal(raw["price"]),
        })
    if not items:
        raise BookingUnavailable("The selection was empty.")
    problem = _resolve_items(items, authoritative_price=False)          # the SNAPSHOT prices: the customer pays exactly what they saw
    if problem is not None:
        raise BookingUnavailable(problem[0])

    details = payload.get("details", {})
    rental_subtotal = sum((item["price"] for item in items), Decimal("0"))
    security_deposit = Decimal(payload.get("security_deposit") or "0")
    now = timezone.now()
    channel_method = {"gcash": Reservation.PaymentMethod.GCASH, "paymaya": Reservation.PaymentMethod.MAYA, "qrph": Reservation.PaymentMethod.QRPH}.get(attempt.channel, "")
    reservation = Reservation.objects.create(
        customer=user, customer_name=(details.get("customer_name") or attempt.customer_name or user.get_username())[:120],
        phone=details.get("phone", ""), address=details.get("address", ""), city=details.get("city", ""), postal_code=details.get("postal_code", ""),
        status=Reservation.Status.CONFIRMED, reviewed_at=now, channel=Reservation.Channel.ONLINE, payment_method=channel_method,
        rental_subtotal=rental_subtotal, security_deposit=security_deposit, late_fee_per_day=int(payload["late_fee_per_day"]) if payload.get("late_fee_per_day") is not None else late_fee.per_day(),
        total_amount=rental_subtotal + security_deposit,
    )
    for item in items:
        gown = item["matched_gown"]
        created = ReservationItem.objects.create(
            reservation=reservation, gown=gown,
            gown_name=(gown.name if gown is not None else item["gown_name"])[:150], gown_slug=item["gown_slug"],
            gown_photo_url=((gown.photo_url if gown is not None else "") or "")[:500],
            size=(gown.size if gown is not None else item["size"])[:20], rental_price=item["price"],
            rental_date=item["rental_date"], return_date=item["return_date"],
            original_rental_date=item["rental_date"], original_return_date=item["return_date"],
        )
        if gown is not None:
            GownUnavailability.objects.create(
                gown=gown, start_date=item["return_date"] + timedelta(days=1), end_date=item["return_date"] + timedelta(days=COOLDOWN_DAYS),
                reason=GownUnavailability.Reason.COOLDOWN,
                note="Auto-added post-rental cooldown. Shorten or release it once the gown's checked and ready to go out again.",
                auto_for_item=created,
            )
            if gown.status == Gown.Status.AVAILABLE:                  # paid = confirmed: the catalog shows the gown as Reserved for this booking
                gown.status = Gown.Status.RESERVED
                gown.save(update_fields=["status", "updated_at"])

    purpose = Payment.Purpose.DEPOSIT if attempt.purpose == PaymentAttempt.Purpose.DEPOSIT else Payment.Purpose.FULL
    payments.record_online_payment(
        reservation=reservation, purpose=purpose, amount=attempt.paid_amount, channel=attempt.channel, provider_ref=attempt.provider_payment_id,
        paid_at=attempt.paid_at, livemode=attempt.livemode, request_id=f"pm-{attempt.pk}",
    )
    how = gateway.CHANNEL_LABELS.get(attempt.channel, gateway.label())
    ReservationStatusEvent.record(
        reservation, APPROVED_LABEL, detail="Your payment was received. Your booking is confirmed.", actor=ReservationStatusEvent.Actor.SYSTEM)
    ReservationStatusEvent.record(
        reservation, "Booked and paid online",
        detail=(f"Paid {peso(attempt.paid_amount)} ({'deposit only' if purpose == Payment.Purpose.DEPOSIT else 'in full'}) with {how} on {gateway.label()}; "
                f"confirmed automatically. {gateway.label()} payment {attempt.provider_payment_id}."
                + ("" if attempt.livemode else " TEST PAYMENT: no real money moved.")),
        actor=ReservationStatusEvent.Actor.SYSTEM, staff_only=True)
    ReservationStatusEvent.record(
        reservation, "Terms and Conditions accepted",
        detail=f"The customer had agreed to the Terms and Conditions of {terms.label()} before booking.",
        actor=ReservationStatusEvent.Actor.CUSTOMER, staff_only=True)
    names = ", ".join(i["gown_name"] for i in items)
    CustomerMessage.objects.create(
        recipient=user, category=CustomerMessage.Category.GENERAL,
        body=f"Good news! Your reservation {reservation.reference_code} ({names}) is confirmed. Your payment was received.")
    return reservation


# ---------------------------------------------------------------- the money goes back
def refund_attempt(attempt_id, reason):
    """The customer paid but the booking cannot be made: send the whole payment back to them and say so. Single-flight: only one caller asks
    PayMongo, however many arrive at once. A refund PayMongo refuses is FLAGGED for a person (the money is never forgotten)."""
    with transaction.atomic():
        attempt = PaymentAttempt.objects.select_for_update().get(pk=attempt_id)
        if attempt.refund_id or attempt.refund_status not in ("", "failed") or attempt.status not in (
                PaymentAttempt.Status.PAID, PaymentAttempt.Status.FLAGGED, PaymentAttempt.Status.REFUNDED):
            return attempt
        attempt.status = PaymentAttempt.Status.REFUNDED
        attempt.refund_status = "requesting"
        attempt.refund_amount = attempt.paid_amount
        attempt.note = (reason or "")[:300]
        attempt.save(update_fields=["status", "refund_status", "refund_amount", "note"])
    how = gateway.CHANNEL_LABELS.get(attempt.channel, "account")
    try:
        refund = gateway.create_refund(payment_id=attempt.provider_payment_id, amount=attempt.paid_amount,
                                       note=f"Booking not possible: {reason}"[:255])
    except gateway.GatewayError as exc:
        logger.error("The refund for attempt %s was refused: %s", attempt.pk, exc)
        PaymentAttempt.objects.filter(pk=attempt.pk).update(
            status=PaymentAttempt.Status.FLAGGED, refund_status="failed", note=f"{(reason or '')[:150]} | Refund not sent: {str(exc)[:120]}", finished_at=timezone.now())
        payments.notify_user(
            attempt.customer, f"We could not reserve your gown ({reason}). Your payment of {peso(attempt.paid_amount)} will be sent back to you: "
                              "the shop has been told and will contact you.", subject="We need to send your payment back", heading="We could not reserve your gown")
        return PaymentAttempt.objects.get(pk=attempt.pk)
    PaymentAttempt.objects.filter(pk=attempt.pk).update(refund_id=refund.id, refund_status=refund.status or "pending", finished_at=timezone.now())
    payments.notify_user(
        attempt.customer,
        f"We could not reserve your gown: {reason} Your payment of {peso(attempt.paid_amount)} is being sent back to your {how}, usually within a day.",
        subject=f"Your payment of PHP {money(attempt.paid_amount):,.2f} is being sent back", heading="We are sending your payment back")
    return PaymentAttempt.objects.get(pk=attempt.pk)


def on_refund_updated(resource):
    """PayMongo says a refund changed (the webhook payment.refund.updated): keep our copy of its status. `resource` is the refund inside the event."""
    refund = gateway.parse_refund(resource if isinstance(resource, dict) else {})
    if not refund.id or not refund.status:
        return
    PaymentAttempt.objects.filter(refund_id=refund.id).update(refund_status=refund.status[:12])


# ---------------------------------------------------------------- 4. the customer leaves, and old attempts close
def cancel_attempt(attempt_id):
    """The customer came back from PayMongo's page without paying: close the session so it cannot be paid later. A payment that is already in
    (or being processed) is honoured instead. Returns the attempt."""
    attempt = PaymentAttempt.objects.filter(pk=attempt_id).first()
    if attempt is None or attempt.status != PaymentAttempt.Status.CREATED:
        return attempt
    outcome = gateway.expire_checkout_session(attempt.session_id) if attempt.session_id else "expired"
    if outcome in ("expired", "already_expired"):
        PaymentAttempt.objects.filter(pk=attempt.pk, status=PaymentAttempt.Status.CREATED).update(
            status=PaymentAttempt.Status.CANCELLED, finished_at=timezone.now())
    elif outcome == "has_payment":
        return confirm_attempt(attempt.pk, source="sweep")
    return PaymentAttempt.objects.get(pk=attempt.pk)


def expire_stale(limit=SWEEP_BATCH, now=None):
    """Close attempts nobody paid within their 20 minutes (a PayMongo session never expires by itself). A session that turns out to be paid is
    confirmed instead. Returns how many it looked at. Safe to run from anywhere, any number of times."""
    now = now or timezone.now()
    stale = list(PaymentAttempt.objects.filter(status=PaymentAttempt.Status.CREATED, expires_at__lt=now - SWEEP_GRACE).order_by("expires_at")[:limit])
    for attempt in stale:
        if not attempt.session_id:
            PaymentAttempt.objects.filter(pk=attempt.pk, status=PaymentAttempt.Status.CREATED).update(
                status=PaymentAttempt.Status.EXPIRED, finished_at=now, note=f"No {gateway.label()} checkout was ever opened.")
            continue
        outcome = gateway.expire_checkout_session(attempt.session_id)
        if outcome in ("expired", "already_expired"):
            PaymentAttempt.objects.filter(pk=attempt.pk, status=PaymentAttempt.Status.CREATED).update(status=PaymentAttempt.Status.EXPIRED, finished_at=now)
        elif outcome == "has_payment":
            confirm_attempt(attempt.pk, source="sweep")
    return len(stale)


# ---------------------------------------------------------------- what the return page shows
def attempt_state(attempt, *, request=None):
    """What the customer's return page should say about an attempt, as plain JSON:
    {"state": "waiting" | "confirming" | "booked" | "refunded" | "flagged" | "ended", "message": ..., "reference_code": ..., "receipt_url": ...}"""
    S = PaymentAttempt.Status
    if attempt.status == S.BOOKED and attempt.reservation_id:
        reservation = attempt.reservation
        payment = reservation.payments.filter(provider__in=gateway.PROVIDERS, direction=Payment.Direction.IN).order_by("id").first()
        item = reservation.items.order_by("id").first()
        return {
            "state": "booked", "reference_code": reservation.reference_code, "amount": f"{money(attempt.paid_amount):,.2f}", "test": not attempt.livemode,
            "receipt_url": reverse("gowns:receipt_view", args=[payment.pk]) if payment else "",
            "receipt_number": payment.receipt_number if payment else "",
            "booking_url": reverse("gowns:reservation_item_detail", args=[item.pk]) if item else reverse("gowns:orders"),
        }
    if attempt.status == S.PAID:
        return {"state": "confirming"}
    if attempt.status == S.REFUNDED:
        return {"state": "refunded", "message": attempt.note, "amount": f"{money(attempt.paid_amount):,.2f}"}
    if attempt.status == S.FLAGGED:
        return {"state": "flagged"}
    if attempt.status == S.HANDLED:
        return {"state": "handled"}
    if attempt.status in (S.EXPIRED, S.CANCELLED):
        return {"state": "ended"}
    return {"state": "waiting"}
