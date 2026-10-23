"""Bookings that staff record in the admin's New Booking page: a customer who walked into the shop. The web side (the form, the
gown search, the availability check) is arabela_admin/new_booking.py; this is everything that decides and saves.

A recorded booking is an ordinary Reservation -- the same booking number series, the same gown lines, the same automatic rest
days -- so Active Reservations, the Rental Schedule, Security Deposits, Reservation Records and the customer website's calendar
all treat it exactly like an online one. What is different:
  * the customer may have no website account (Reservation.customer is empty): the name and phone are the customer;
  * it says how it came in (Reservation.channel) and who recorded it (Reservation.recorded_by);
  * staff pick the exact physical gown the customer chose (online, the system picks a free one of the design itself);
  * payment is taken at the shop, and a walk-in always pays something: "Paid in full" (rental + deposit) or "Deposit only". Either
    one confirms the booking at once and needs the shop's receipt photo. A deposit-only booking is still short of its rental
    receipt: it is flagged "Payment incomplete" in Reservation Records until staff upload that one there. How the customer paid
    (cash, GCash...) is the shop's own business and is not recorded.
"""
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from gowns import availability
from gowns.models import COOLDOWN_DAYS, Gown, GownUnavailability
from reservations.models import ReceiptRecord, Reservation, ReservationItem, ReservationStatusEvent

DEPOSIT_PER_GOWN = Decimal("2000")
MAX_GOWNS = 10
MAX_RECEIPTS = 1                # one photo here; a second one (the rental receipt) is added later in Reservation Records
NOTE_MAX = 200

PAYMENT_FULL = "full"           # the rental and the deposit were paid
PAYMENT_DEPOSIT = "deposit"     # only the deposit was paid; the rental is still to pay
PAYMENTS = (PAYMENT_FULL, PAYMENT_DEPOSIT)

RECORDED_LABEL = "Booking recorded at the shop"            # the customer's timeline (if they have an account)
STAFF_NOTE_LABEL = "Booking recorded by staff"              # staff-only: who, how it came in, payment, note
APPROVED_LABEL = "Reservation approved"                     # the same line Approve writes (and the same email, if any)

STAFF_CHANNELS = (Reservation.Channel.WALK_IN,)


class BookingError(Exception):
    """The booking can't be saved. `message` is written for staff; nothing has been changed."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------- the customer
def clean_name(raw) -> str:
    name = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not name:
        raise BookingError("Please type the customer's name.")
    if len(name) > 120:
        raise BookingError("The customer's name is too long (120 characters at most).")
    if not re.search(r"[^\W\d_]", name):
        raise BookingError("Please type the customer's real name (letters, not only numbers).")
    return name


def clean_phone(raw) -> str:
    """A Philippine mobile number in one form, 09XXXXXXXXX ('0917 123 4567', '+63 917 123 4567', '639171234567' all work)."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        raise BookingError("Please type the customer's mobile number.")
    if digits.startswith("63") and len(digits) == 12:
        digits = "0" + digits[2:]
    elif digits.startswith("9") and len(digits) == 10:
        digits = "0" + digits
    if not (len(digits) == 11 and digits.startswith("09")):
        raise BookingError("That mobile number doesn't look right. Type it like 0917 123 4567.")
    return digits


def phone_display(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("09"):
        return f"{digits[:4]} {digits[4:7]} {digits[7:]}"
    return phone or ""


def clean_notebook_number(raw) -> str:
    """Optional. Capital letters, digits, spaces, dashes, slashes and #; at most 30 characters."""
    number = re.sub(r"\s+", " ", str(raw or "")).strip().upper()
    if not number:
        return ""
    if len(number) > 30:
        raise BookingError("The notebook number is too long (30 characters at most).")
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9 /#-]*", number):
        raise BookingError("The notebook number can only have letters, numbers, spaces, dashes (-), slashes (/) and #.")
    return number


def notebook_number_owner(number, exclude_id=None):
    """The booking already using this notebook number, or None."""
    if not number:
        return None
    qs = Reservation.objects.filter(notebook_number=number)
    if exclude_id:
        qs = qs.exclude(id=exclude_id)
    return qs.first()


def find_account(account_id):
    """A customer's website account to link the booking to (never a staff or owner account, never a deactivated one)."""
    if not account_id:
        return None
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        raise BookingError("That website account wasn't found. Search for it again.")
    user = get_user_model().objects.filter(id=account_id, is_staff=False, is_superuser=False, is_active=True).first()
    if user is None:
        raise BookingError("That website account wasn't found. Search for it again.")
    return user


def same_customer_hint(name="", phone="", limit=3):
    """Earlier bookings that look like the same person (same phone, or the very same name), newest first -- shown before
    saving so one customer isn't typed in two different ways."""
    from django.db.models import Q
    query = Q()
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) >= 10:
        query |= Q(phone__endswith=digits[-10:])
    name = re.sub(r"\s+", " ", name or "").strip()
    if len(name) >= 3:
        query |= Q(customer_name__iexact=name)
    if not query:
        return []
    return list(
        Reservation.objects.filter(query).select_related("customer__profile").order_by("-created_at")[:limit]
    )


# ---------------------------------------------------------------- what to book
def parse_lines(raw_lines, *, today, is_owner):
    """[{gown, event, takes_now, picked_up_on}] from the form, with the gown rows loaded -> list of dicts."""
    if not isinstance(raw_lines, list) or not raw_lines:
        raise BookingError("Add at least one gown.")
    if len(raw_lines) > MAX_GOWNS:
        raise BookingError(f"One booking can have at most {MAX_GOWNS} gowns.")
    lines, seen = [], set()
    for raw in raw_lines:
        if not isinstance(raw, dict):
            raise BookingError("Something in the gown list is wrong. Please refresh the page and try again.")
        try:
            gown_id = int(raw.get("gown"))
        except (TypeError, ValueError):
            raise BookingError("Choose a gown for every line.")
        gown = Gown.objects.filter(id=gown_id).first()
        if gown is None:
            raise BookingError("One of the gowns no longer exists. Please search for it again.")
        if gown.id in seen:
            raise BookingError(f"{gown.name} ({gown.gown_id}) is in the list twice. One gown can only be booked once.")
        seen.add(gown.id)
        event = _date(raw.get("event"))
        if event is None:
            raise BookingError(f"Choose the event date for {gown.name}.")
        takes_now = bool(raw.get("takes_now"))
        picked_up_on = None
        if takes_now:
            picked_up_on = _date(raw.get("picked_up_on")) or today
            if picked_up_on != today and not is_owner:
                raise BookingError("Staff record a gown taken now with today's date. Only the owner can enter an earlier day.")
        lines.append({"gown": gown, "event": event, "takes_now": takes_now, "picked_up_on": picked_up_on})
    return lines


def _date(raw):
    from datetime import date
    try:
        return date.fromisoformat(str(raw or "").strip())
    except ValueError:
        return None


def check_line(line, *, now, is_owner):
    """availability.check_unit for one form line."""
    return availability.check_unit(
        line["gown"], line["event"], picked_up_on=line["picked_up_on"], now=now,
        backdate_days=availability.OWNER_BACKDATE_DAYS if is_owner else 0,
    )


# ---------------------------------------------------------------- payment
def receipt_covers_for(payment) -> str:
    """What the receipt attached while booking is for: decided by the Payment choice, never asked for separately."""
    return ReceiptRecord.Covers.BOTH if payment == PAYMENT_FULL else ReceiptRecord.Covers.DEPOSIT


def check_payment(*, payment, receipts, lines):
    """receipts: [(file, file_hash)]. Raises BookingError when the payment part doesn't add up."""
    if payment not in PAYMENTS:
        raise BookingError("Choose \"Paid in full\" or \"Deposit only\". If this page has been open for a while, refresh it.")
    if len(receipts) > MAX_RECEIPTS:
        raise BookingError("Attach one receipt photo. A second one can be added later in Reservation Records.")
    if payment != PAYMENT_FULL and any(line["takes_now"] for line in lines):
        raise BookingError("A gown can only leave the shop once the rental is paid. Choose \"Paid in full\", or untick "
                           "\"Customer takes the gown now\".")
    if not receipts:
        raise BookingError("Attach the photo of the shop's receipt.")


# ---------------------------------------------------------------- saving
def record_booking(*, recorded_by, is_owner, staff_name, channel, name, phone, account, address, notebook_number,
                   lines, payment, receipts, note, save_receipt_photo, now=None):
    """Save the booking. Everything is checked again inside one transaction with the gowns locked, so neither another
    staff member nor an online customer can take the same gown in between: either the whole booking is saved, or nothing.
    save_receipt_photo(file) -> URL stores one receipt photo (the admin's own helper, so storage works the same way).
    Returns the new Reservation."""
    now = now or availability.now_local()
    if channel not in STAFF_CHANNELS:
        raise BookingError("Only walk-in bookings can be recorded here. Bookings from the website come in by themselves.")
    note = re.sub(r"\s+", " ", note or "").strip()[:NOTE_MAX]
    check_payment(payment=payment, receipts=receipts, lines=lines)
    if notebook_number:
        taken = notebook_number_owner(notebook_number)
        if taken:
            raise BookingError(f"Notebook number {notebook_number} is already on {taken.reference_code} "
                               f"({taken.display_customer_name}). Check the number.", status=409)

    try:
        with transaction.atomic():
            # Lock the chosen gowns one by one in the same order checkout locks gowns (by name), so this can never deadlock
            # with an online checkout for the same gowns; whoever is second waits, then sees the first one's booking.
            ordered = sorted(lines, key=lambda line: (line["gown"].name.casefold(), line["gown"].id))
            for line in ordered:
                line["gown"] = Gown.objects.select_for_update().get(id=line["gown"].id)
                result = check_line(line, now=now, is_owner=is_owner)
                if not result["ok"]:
                    raise BookingError(availability.check_message(line["gown"], result), status=409)
                line["pickup"] = result["pickup"]
                line["return_date"] = result["return_date"]

            rental_subtotal = sum((line["gown"].rental_price for line in lines), Decimal("0"))
            security_deposit = DEPOSIT_PER_GOWN * len(lines)
            reservation = Reservation.objects.create(
                customer=account,
                customer_name=name,
                phone=phone,
                address=(address or "")[:255],
                status=Reservation.Status.CONFIRMED,        # something was paid at the shop, so it is confirmed at once
                rental_subtotal=rental_subtotal,
                security_deposit=security_deposit,
                total_amount=rental_subtotal + security_deposit,
                reviewed_at=timezone.now(),
                channel=channel,
                recorded_by=recorded_by,
                notebook_number=notebook_number,
            )
            for line in lines:
                gown = line["gown"]
                item = ReservationItem.objects.create(
                    reservation=reservation,
                    gown=gown,
                    gown_name=gown.name[:150],
                    gown_slug=gown.slug,
                    size=gown.size,
                    rental_price=gown.rental_price,
                    rental_date=line["pickup"],
                    return_date=line["return_date"],
                    original_rental_date=line["pickup"],
                    original_return_date=line["return_date"],
                    stage=ReservationItem.Stage.RESERVED if line["takes_now"] else ReservationItem.Stage.PICKUP,
                    picked_up_on=line["pickup"] if line["takes_now"] else None,
                )
                line["item"] = item
                # The same automatic rest days checkout adds (see gowns.views.reservation_submit).
                GownUnavailability.objects.create(
                    gown=gown,
                    start_date=line["return_date"] + timedelta(days=1),
                    end_date=line["return_date"] + timedelta(days=COOLDOWN_DAYS),
                    reason=GownUnavailability.Reason.COOLDOWN,
                    note="Auto-added post-rental cooldown. Shorten or release it once "
                         "the gown's checked and ready to go out again.",
                    auto_for_item=item,
                )
                # Paid = approved (Approve does the same): the catalog shows the gown as Reserved for this booking.
                if gown.status == Gown.Status.AVAILABLE:
                    gown.status = Gown.Status.RESERVED
                    gown.save(update_fields=["status", "updated_at"])

            for photo_file, file_hash in receipts:
                ReceiptRecord.objects.create(
                    reservation=reservation, photo_url=save_receipt_photo(photo_file), uploaded_by=recorded_by,
                    covers=receipt_covers_for(payment), file_hash=file_hash,
                )

            channel_text = reservation.get_channel_display()
            ReservationStatusEvent.record(
                reservation, RECORDED_LABEL,
                detail=f"{channel_text} booking recorded by the shop.",
                actor=ReservationStatusEvent.Actor.STAFF,
            )
            full = payment == PAYMENT_FULL
            payment_text = "Paid in full." if full else "Deposit paid; the rental is still to pay."
            ReservationStatusEvent.record(
                reservation, STAFF_NOTE_LABEL,
                detail=f"{staff_name}: {channel_text}. {payment_text}" + (f" Note: {note}" if note else ""),
                actor=ReservationStatusEvent.Actor.STAFF, staff_only=True,
            )
            ReservationStatusEvent.record(
                reservation, APPROVED_LABEL,
                detail="Paid at the shop. Your booking is confirmed." if full
                else "Deposit paid at the shop. Your booking is confirmed.",
                actor=ReservationStatusEvent.Actor.STAFF,
            )
            for line in lines:
                if line["takes_now"]:
                    ReservationStatusEvent.record(
                        reservation, f"{line['item'].gown_name} picked up", item=line["item"],
                        detail="The gown is now with the customer.",
                        actor=ReservationStatusEvent.Actor.STAFF,
                    )
    except IntegrityError:
        # Two staff typing the same notebook number at the very same moment: the database refuses the second one.
        taken = notebook_number_owner(notebook_number)
        if taken:
            raise BookingError(f"Notebook number {notebook_number} is already on {taken.reference_code} "
                               f"({taken.display_customer_name}). Check the number.", status=409)
        raise BookingError("The booking couldn't be saved just now. Please try again.", status=500)
    return reservation
