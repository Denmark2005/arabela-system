"""Recording money on a booking: a payment received, a deduction from the deposit, the deposit handed back, and voiding a mistake.

Everything that CHANGES the payment records goes through this module, so the rules live in one place (the pages only ask):

  * Every change runs in one transaction with the booking row LOCKED, so two staff acting on the same booking at the same moment queue
    up and the second one sees what the first one did.
  * The DEPOSIT is received exactly (the deposit the booking was made with) -- either on its own ("Deposit") or together with the rental
    ("Paid in full"). A part of the deposit is refused. After that the rental balance may be paid in parts, never more than what is owed.
  * A deposit return is never more than the system says is refundable, and only once every gown is back.
  * Nothing is edited or deleted: a mistake is VOIDED with a reason (who, when), and the booking's numbers follow by themselves.
  * A request carrying the same `request_id` twice (a double click, a retried request) is answered with the payment already saved.
  * Each payment gets its Payment Receipt number (PR-2026-000123) and an immutable snapshot of what the receipt says.
  * The customer (if the booking has a website account) is told: a bell message always, and an email when customer emails are on.

The money arithmetic is reservations/ledger.py; this module only checks a request against it and writes the rows.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone

from accounts.roles import staff_display_name
from reservations import gateway, ledger
from reservations.ledger import ZERO, money, peso
from reservations.models import Deduction, Payment, ReceiptSequence, Reservation, ReservationStatusEvent

logger = logging.getLogger(__name__)

SHOP_NAME = "Arabela Gown Rental"
REASON_MAX = 200
METHODS = (Payment.Method.CASH, Payment.Method.GCASH)
PAYMENT_EVENT = "Payment received"
RETURN_EVENT = "Security deposit returned"
SETTLED_EVENT = "Security deposit settled"          # accounts.customer_emails.EVENT_KINDS emails it under the same words
SETTLED_DETAIL = "Your security deposit has been settled. This reservation is complete."
UNSETTLED_EVENT = "Security deposit not yet settled"
UNSETTLED_DETAIL = "The deposit still has to be settled. Please contact the shop if you have a question."
VOID_EVENT = "Receipt voided"
DEDUCTION_EVENT = "Deduction from the security deposit"


class PaymentError(Exception):
    """The request can't be done. `message` is written for staff; nothing has been changed."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------- small helpers
def _clean_reason(raw, *, what="reason", minimum=3):
    text = " ".join(str(raw or "").split())
    if len(text) < minimum:
        raise PaymentError(f"Please write the {what} (at least {minimum} letters).")
    return text[:REASON_MAX]


def _clean_note(raw):
    return " ".join(str(raw or "").split())[:REASON_MAX]


def _lock(reservation_id):
    # No select_related here: a nullable relation makes Postgres refuse FOR UPDATE on the joined row.
    try:
        return Reservation.objects.select_for_update(of=("self",)).get(pk=reservation_id)
    except Reservation.DoesNotExist:
        raise PaymentError("That booking was not found. Please refresh the page.", status=404)


def _method_and_reference(method, reference):
    """(method, digits). Cash has no reference; GCash needs the 13-digit reference number, and it can only be used once."""
    from gowns import typed_payment

    if method not in METHODS:
        raise PaymentError("Choose Cash or GCash.")
    if method == Payment.Method.CASH:
        return method, ""
    problem = typed_payment.reference_problem(reference)
    if problem:
        raise PaymentError(problem.replace("on your GCash receipt", "from the GCash screen").replace("your receipt", "the GCash screen"))
    return method, typed_payment.clean_reference(reference)


def _reference_in_use(digits, reservation):
    """The booking code a GCash reference number is already on, or ''. A number is one payment: it can't pay a second time."""
    other = Payment.objects.filter(reference=digits, status=Payment.Status.RECORDED).select_related("reservation").first()
    if other is not None:
        return other.reservation.reference_code
    taken = Reservation.objects.filter(Q(gcash_reference=digits) | Q(proof_typed_reference=digits)).exclude(pk=reservation.pk).first()
    return taken.reference_code if taken is not None else ""


def ensure_rows(reservation):
    """Give an OLD booking (no Payment rows) its rows, derived from what it already carries, so a new payment lands next to them. Idempotent;
    call inside the booking's lock. Returns how many rows it made."""
    if reservation.payments.exists():
        return 0
    made = 0
    for row in ledger.legacy_rows(reservation):
        Payment.objects.create(
            reservation=reservation, direction=row["direction"], purpose=row["purpose"], method=row["method"], amount=row["amount"],
            reference=row["reference"], paid_at=row["paid_at"], recorded_by_name="Before the Payments page", note=row["note"], is_legacy=True,
        )
        made += 1
    return made


def _fresh(reservation_id):
    return Reservation.objects.get(pk=reservation_id)


def _shop():
    from accounts import customer_emails

    info = customer_emails.shop_info()
    return {"name": SHOP_NAME, "address": info.get("address", ""), "phone": info.get("phone", "")}


def _day(value):
    return f"{value:%b} {value.day}"


def _gown_lines(reservation):
    """The gowns a receipt lists: pick-up and return, and -- a free-days booking -- the event and the return the customer planned (1 day after the
    event, when they chose that: they may still bring it until the return date for free)."""
    items = list(reservation.items.all())
    kept = [i for i in items if i.stage != i.Stage.CANCELLED] or items
    lines = []
    for i in kept:
        line = {"name": i.gown_name, "size": i.size, "pickup": _day(i.rental_date), "return": _day(i.return_date)}
        if i.uses_free_days:
            line["event"] = _day(i.effective_event_date)
            if i.shown_return_date != i.return_date:
                line["planned"] = _day(i.shown_return_date)
        lines.append(line)
    return lines


def fee_words(change_fee, overdue_fee, early_fee=0, later_fee=0):
    """["the ₱400.00 early pick-up fee", "the ₱1,400.00 date change fee", ...]: the fees a sentence names, only those above zero."""
    named = (("early pick-up fee", early_fee), ("later return fee", later_fee), ("date change fee", change_fee), ("overdue fee", overdue_fee))
    return [f"the {peso(amount)} {name}" for name, amount in named if amount and amount > 0]


def _listed(parts):
    """'a', 'a and b', 'a, b and c'."""
    if len(parts) < 2:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def _return_gowns(reservation):
    """The gowns of a deposit return receipt: the day each was first booked to come back (which the fee counts from), the day agreed now (when it was moved), the
    day it came back, and how many of those days were overdue."""
    out = []
    for i in reservation.items.all():
        if i.stage == i.Stage.CANCELLED:
            continue
        out.append({"name": i.gown_name, "size": i.size, "due": ledger.long_day(i.original_return_date or i.return_date), "back": ledger.long_day(i.returned_on),
                    "late_days": ledger.late_days(i), "agreed": ledger.long_day(ledger.agreed_return(i)), "overdue_days": ledger.overdue_days(i)})
    return out


def _return_reason(before, after):
    """The plain sentence under a deposit return receipt: why the customer gets this amount and not the whole deposit."""
    late, taken = after.late_fee, after.deductions_total
    # An extra charge paid before that the fees no longer ask for goes back too (ledger.extra_credit).
    extra = f" and the {peso(after.extra_received)} extra charge paid before" if after.extra_credit > 0 else ""
    if late <= 0 and taken <= 0:
        if extra:
            return f"No fees and no deductions: the whole {peso(after.deposit_paid)} security deposit{extra} go back to the customer."
        if before.refunded <= 0 and after.refund_available <= 0:
            return "No fees and no deductions: the security deposit is returned in full."
        return f"No fees and no deductions: the whole {peso(after.deposit_paid)} security deposit goes back to the customer."
    parts = fee_words(after.change_fee, after.overdue_fee, after.early_fee, after.later_fee) + ([f"{peso(taken)} of deductions"] if taken > 0 else [])
    return f"The {peso(after.deposit_paid)} security deposit{extra} less {_listed(parts)} leaves {peso(after.refundable_total)} to give back."


def _working(reservation, after):
    """The fees and the deductions, line by line: what a receipt shows so the customer can see where an amount comes from."""
    return {
        "late_lines": [{"gown": l["gown"], "days": l["days"], "rate": f"{l['rate']:.0f}", "amount": f"{l['amount']:.2f}", "due": l.get("due", ""),
                        "returned": l.get("returned", ""), "back": l["back"]} for l in after.late_lines],
        "late_fee": f"{after.late_fee:.2f}",
        # the same money under its two names (reservations/ledger.py): what the receipt prints
        "fee_lines": [{**{k: l[k] for k in ("kind", "label", "gown", "days", "back", "booked", "agreed", "due", "returned", "pickup", "return", "free_from", "free_until", "until")},
                       "rate": f"{l['rate']:.0f}", "amount": f"{l['amount']:.2f}"} for l in after.fee_lines],
        "early_fee": f"{after.early_fee:.2f}",
        "later_fee": f"{after.later_fee:.2f}",
        "change_fee": f"{after.change_fee:.2f}",
        "overdue_fee": f"{after.overdue_fee:.2f}",
        "deductions": [{"reason": d.reason, "amount": f"{money(d.amount):.2f}"} for d in reservation.deductions.all() if not d.is_void],
        "deductions_total": f"{after.deductions_total:.2f}",
    }


def _snapshot(reservation, payment, before, after, *, served_by):
    """What the receipt says, frozen: a reprint years later is the same paper."""
    from gowns import typed_payment

    local = timezone.localtime(payment.paid_at)
    snap = {
        "title": payment.title,
        "fee_names": True,          # issued since the fee has two names (Date change / Overdue); a receipt saved before prints as it did
        "free_days": any(i.uses_free_days for i in reservation.items.all()),     # a free-days booking: the receipt's note says which days are free
        "shop": _shop(),
        "number": payment.receipt_number,
        "when": f"{local:%b} {local.day}, {local.year} {local.hour % 12 or 12}:{local:%M} {'AM' if local.hour < 12 else 'PM'}",
        "booking": reservation.reference_code,
        "customer": reservation.display_customer_name,
        "phone": reservation.phone,
        "served_by": served_by,
        "gowns": _gown_lines(reservation),
        "charges": {"rental": f"{before.rental:.2f}", "deposit": f"{before.deposit:.2f}", "total": f"{before.total:.2f}"},
        "late_rate": f"{before.late_rate:.0f}",
        "method": payment.get_method_display(),
        "reference": typed_payment.format_reference(payment.reference) if payment.reference else "",
        "amount": f"{money(payment.amount):.2f}",
        "purpose": payment.purpose,
        "purpose_label": payment.get_purpose_display(),
        "note": payment.note,
        "paid_before": f"{before.received:.2f}",
        "balance_after": f"{after.balance_due:.2f}",
    }
    if payment.purpose == Payment.Purpose.DEPOSIT_RETURN:
        snap["return"] = {
            "deposit_paid": f"{after.deposit_paid:.2f}",
            "extra_paid": f"{after.extra_received if after.extra_credit > 0 else ZERO:.2f}",     # an extra charge paid before that goes back too
            **_working(reservation, after),
            "returned_before": f"{before.refunded:.2f}",
            "returned_now": f"{money(payment.amount):.2f}",
            "still_to_return": f"{after.refund_available:.2f}",
            "gowns": _return_gowns(reservation),
            "why": _return_reason(before, after),
        }
    elif payment.purpose == Payment.Purpose.EXTRA:
        # the fees and deductions were more than the deposit, so nothing goes back: this receipt is where the customer sees why they pay more
        snap["extra"] = {**_working(reservation, after), "deposit_used": f"{after.deposit_used:.2f}", "over": f"{after.overflow:.2f}"}
    elif payment.direction == Payment.Direction.IN and after.total_deduction > 0:
        # A payment receipt of a booking that already has days outside its free days (or other fees): what will come out of the deposit when the
        # gowns are back, and what comes back -- so the customer sees it on paper from the first receipt, not only when the deposit is returned.
        work = _working(reservation, after)
        snap["deduct"] = {
            "fee_lines": work["fee_lines"], "deductions": work["deductions"], "total": f"{after.total_deduction:.2f}",
            "back": f"{max(after.deposit + after.extra_received - after.total_deduction, ZERO):.2f}",
            "over": f"{max(after.total_deduction - after.deposit - after.extra_received, ZERO):.2f}",
        }
    if payment.direction == Payment.Direction.IN and any(i.stage not in i.FINISHED_STAGES for i in reservation.items.all()):
        snap["change_dates_note"] = True       # "To change your dates, call the shop with this booking number" (the dates can't be changed online)
    return snap


def _notify(reservation, payment, text, *, subject, heading):
    """The bell message always (a customer with a website account), and the email when customer emails are on and the database commits."""
    from accounts.models import CustomerMessage

    if reservation.customer_id is None:
        return False
    CustomerMessage.objects.create(recipient_id=reservation.customer_id, category=CustomerMessage.Category.PAYMENT, body=text[:500])
    if payment is not None:
        Payment.objects.filter(pk=payment.pk).update(customer_notified_at=timezone.now())
    if getattr(settings, "CUSTOMER_EMAILS_ENABLED", False):
        reservation_id, payment_id = reservation.pk, (payment.pk if payment is not None else None)
        transaction.on_commit(lambda: _email_guarded(reservation_id, payment_id, text, subject, heading))
    return True


def _email_guarded(reservation_id, payment_id, text, subject, heading):
    from accounts import customer_emails

    customer_emails._guarded(_send_email, reservation_id, payment_id, text, subject, heading)


def _send_email(reservation_id, payment_id, text, subject, heading):
    from accounts import customer_emails

    reservation = Reservation.objects.select_related("customer").get(pk=reservation_id)
    data = customer_emails._reservation_data(reservation, text=text)
    data.update(subject=subject, heading=heading)
    if payment_id:
        payment = Payment.objects.get(pk=payment_id)
        if payment.receipt_number:
            data.update(receipt_number=payment.receipt_number, receipt_path=reverse("gowns:receipt_view", args=[payment.pk]))
    customer_emails._deliver(reservation.customer, customer_emails.PAYMENT_NOTICE, data)


def printable_receipts(reservation):
    """This booking's receipts a person can print: recorded payments and deposit returns that have a receipt number (a payment from before the Payments
    page has none). Reads reservation.payments, so prefetch it for a list. In the order they were made."""
    return [p for p in reservation.payments.all() if p.receipt_number and not p.is_legacy]


def has_receipt(reservation):
    return any(p.receipt_number and not p.is_legacy for p in reservation.payments.all())


def notify_user(user, text, *, subject, heading):
    """A payment notice for a customer with NO booking to hang it on (an online payment that could not be turned into a booking): the bell message
    always, and the email when customer emails are on and the database commits. Returns False when there is no account to tell."""
    from accounts.models import CustomerMessage

    if user is None or not getattr(user, "pk", None):
        return False
    CustomerMessage.objects.create(recipient_id=user.pk, category=CustomerMessage.Category.PAYMENT, body=text[:500])
    if getattr(settings, "CUSTOMER_EMAILS_ENABLED", False):
        user_id = user.pk
        transaction.on_commit(lambda: _user_email_guarded(user_id, text, subject, heading))
    return True


def _user_email_guarded(user_id, text, subject, heading):
    from accounts import customer_emails

    customer_emails._guarded(_send_user_email, user_id, text, subject, heading)


def _send_user_email(user_id, text, subject, heading):
    from django.contrib.auth import get_user_model
    from accounts import customer_emails

    user = get_user_model().objects.get(pk=user_id)
    data = {"first_name": customer_emails._account_first_name(user), "text": text, "subject": subject, "heading": heading}
    customer_emails._deliver(user, customer_emails.PAYMENT_NOTICE, data)


def send_receipt_again(payment_id):
    """Staff pressed "Send receipt again": the bell message and the email once more. Returns False for a booking with no website account."""
    payment = Payment.objects.select_related("reservation").get(pk=payment_id)
    if payment.is_legacy or not payment.receipt_number:
        raise PaymentError("This was recorded before the Payments page. It has no receipt to send.")
    reservation = payment.reservation
    text = _notice_text(payment)
    with transaction.atomic():
        sent = _notify(reservation, payment, text, subject=_subject(payment), heading=_heading(payment))
    return sent


def _php(amount):
    """'PHP 2,000.00': an email subject stays plain ASCII (every mail app and relay agrees on it), so no peso sign there."""
    return f"PHP {money(amount):,.2f}"


def _subject(payment):
    if payment.is_void:
        return f"Receipt {payment.receipt_number} was voided"
    if payment.direction == Payment.Direction.OUT:
        return f"Your security deposit was returned - receipt {payment.receipt_number}"
    return f"Payment received {_php(payment.amount)} - receipt {payment.receipt_number}"


def _heading(payment):
    if payment.is_void:
        return "A receipt was voided"
    return "Your security deposit was returned" if payment.direction == Payment.Direction.OUT else "We received your payment"


def _notice_text(payment):
    """The one sentence the bell and the email say about a payment (also used when it is sent again)."""
    reservation = payment.reservation
    code = reservation.reference_code
    how = _way(payment) if payment.method != Payment.Method.OLD else "at the shop"
    if payment.is_void:
        return (f"Receipt {payment.receipt_number} ({peso(payment.amount)}) for {code} was voided: {payment.void_reason}. "
                "Please contact the shop if you have a question.")
    if payment.direction == Payment.Direction.OUT:
        later = " It can take up to a day to show in your account." if payment.provider in gateway.PROVIDERS else ""
        working = ""
        gone = (payment.snapshot or {}).get("return") or {}
        late, taken = money(gone.get("late_fee") or 0), money(gone.get("deductions_total") or 0)
        if late > 0 or taken > 0:          # the customer is told why it is not the whole deposit (true for a deposit handed back in parts too)
            paid = money(gone.get("deposit_paid") or 0)
            if "change_fee" in gone:
                fees = fee_words(money(gone["change_fee"]), money(gone.get("overdue_fee") or 0),
                                 money(gone.get("early_fee") or 0), money(gone.get("later_fee") or 0))
            else:                          # a receipt issued before the fee had its two names keeps the words printed on it
                fees = [f"the {peso(late)} late fee"] if late > 0 else []
            parts = fees + ([f"{peso(taken)} of deductions"] if taken > 0 else [])
            more = money(gone.get("extra_paid") or 0)
            also = f" and the {peso(more)} extra charge you paid before" if more > 0 else ""
            working = (f" Your {peso(paid)} deposit{also} less {_listed(parts)} comes to {peso(max(paid + more - late - taken, money(0)))}."
                       " The receipt shows the details.")
        return f"Your security deposit of {peso(payment.amount)} for {code} was returned ({how}). Receipt {payment.receipt_number}.{working}{later}"
    snapshot = payment.snapshot or {}
    balance = snapshot.get("balance_after")
    tail = f" Balance still to pay at the shop: {peso(balance)}." if balance and money(balance) > 0 else " Nothing is left to pay."
    deduct = snapshot.get("deduct") or {}
    if deduct and money(deduct.get("total") or 0) > 0:
        tail += (f" When the gown is returned, {peso(deduct['total'])} comes out of your security deposit for the booked days outside the free days"
                 f" (and any other fees), so {peso(deduct.get('back') or 0)} comes back.")
    return f"Payment received {peso(payment.amount)} for {code} ({payment.get_purpose_display()}, {how}). Receipt {payment.receipt_number}.{tail}"


def _event(reservation, label, detail):
    ReservationStatusEvent.record(reservation, label, detail=detail, actor=ReservationStatusEvent.Actor.STAFF)


def sync_settled(reservation_id, *, quiet=False, actor=ReservationStatusEvent.Actor.STAFF, reopens=False):
    """Keeps the booking's history truthful after a gown comes back or money moves: "Security deposit settled" is written only once nothing is left to
    move (ledger.is_settled). `reopens`: the change can open a settled deposit again (a deduction, a voided receipt or deduction, a refund that failed),
    so a line says so when it did -- a payment or a hand-back never can (an older booking's check-in line, from before this rule, is left alone).
    `quiet`: the change that got it there already told the customer, so the line sends no email of its own.

    The booking row is locked while it decides, so two staff acting at once can't write the line twice. Like every history line, it is a record OF an
    action and never a reason that action fails: a problem here is logged and the action stands."""
    try:
        with transaction.atomic():
            reservation = Reservation.objects.select_for_update(of=("self",)).filter(pk=reservation_id).first()
            if reservation is None:
                return
            settled = ledger.is_settled(reservation, ledger.statement(reservation))
            last = reservation.status_events.filter(label__in=(SETTLED_EVENT, UNSETTLED_EVENT)).order_by("occurred_at", "id").last()
            was_settled = last is not None and last.label == SETTLED_EVENT
            if settled and not was_settled:
                ReservationStatusEvent.record(reservation, SETTLED_EVENT, detail=SETTLED_DETAIL, actor=actor, quiet=quiet)
            elif reopens and was_settled and not settled and reservation.status != Reservation.Status.CANCELLED:
                ReservationStatusEvent.record(reservation, UNSETTLED_EVENT, detail=UNSETTLED_DETAIL, actor=actor, quiet=True)
    except Exception:
        logger.exception("Could not bring the deposit-settled line up to date for reservation %s", reservation_id)


# ---------------------------------------------------------------- what a window may offer
def choices(statement):
    """What the Record payment window should offer for this statement, so the page and the rules can never disagree.
    {"kind": "deposit", "options": [{"purpose", "label", "amount"}]} while the deposit is unpaid (exact amounts only);
    {"kind": "balance", "max": ...} for the rental; {"kind": "extra", "max": ...} for fees or deductions beyond the deposit; {"kind": "none"}."""
    if not statement.tracked or statement.cancelled:
        return {"kind": "none"}
    if statement.deposit_unpaid > 0:
        options = [{"purpose": Payment.Purpose.DEPOSIT, "label": "Deposit only", "amount": f"{statement.deposit_unpaid:.2f}"}]
        if statement.rental_unpaid > 0:
            options.append({"purpose": Payment.Purpose.FULL, "label": "Paid in full", "amount": f"{statement.balance_core:.2f}"})
        return {"kind": "deposit", "options": options}
    if statement.rental_unpaid > 0:
        return {"kind": "balance", "max": f"{statement.rental_unpaid:.2f}"}
    if statement.extra_owed > 0:
        return {"kind": "extra", "max": f"{statement.extra_owed:.2f}"}
    return {"kind": "none"}


# ---------------------------------------------------------------- an approved booking
def record_approval_payment(reservation_id, user=None, now=None):
    """Called when staff APPROVE a booking, so its payments are on the Payments page from the first day:

      * an ONLINE booking paid its deposit by GCash (the screenshot staff just checked): the deposit is recorded with its numbered receipt,
        method "GCash (online)". The customer is not told again (the approval already reached them) and it can be printed or seen in their
        Receipts. (Phase 2 will record the online payment itself.)
      * a booking staff recorded (an old pending walk-in) gets the rows its labelled receipt photos stand for.

    Never raises into the approval: the caller wraps it, and the Payments page reads a booking with no rows from what it already carries."""
    now = now or timezone.now()
    with transaction.atomic():
        reservation = _lock(reservation_id)
        if reservation.payments.exists():
            return None
        if reservation.channel != Reservation.Channel.ONLINE:
            ensure_rows(reservation)
            return None
        deposit = money(reservation.security_deposit)
        if deposit <= 0 or reservation.status not in ledger.LIVE_STATUSES:
            return None
        served_by = staff_display_name(user) if user is not None else ""
        payment = Payment.objects.create(
            reservation=reservation, direction=Payment.Direction.IN, purpose=Payment.Purpose.DEPOSIT, method=Payment.Method.ONLINE_GCASH, amount=deposit,
            reference=(reservation.proof_typed_reference or reservation.gcash_reference or "")[:20], paid_at=reservation.reviewed_at or now,
            recorded_by=user if getattr(user, "pk", None) else None, recorded_by_name=served_by,
            note="Paid online by GCash; checked by staff when the booking was approved.",
            receipt_number=ReceiptSequence.next_number(timezone.localtime(now).year),
        )
        # This is the booking's first row, so "before" is the same figures less this payment (read before saving it, an online booking with no
        # rows would count its deposit as already paid -- the old-booking guess -- and the receipt would say "Paid before").
        after = ledger.statement(_fresh(reservation.pk))
        before = ledger.Statement(**{**after.__dict__, "received": money(after.received) - deposit})
        payment.snapshot = _snapshot(reservation, payment, before, after, served_by=served_by)
        payment.save(update_fields=["snapshot"])
        return payment


# ---------------------------------------------------------------- a payment received
def check_amount(statement, purpose, amount):
    """Raises PaymentError unless a payment of `amount` for `purpose` is what the booking can take right now (the same rules at the counter and online)."""
    if amount <= 0:
        raise PaymentError("Please type the amount received.")
    if purpose == Payment.Purpose.DEPOSIT:
        if statement.deposit_unpaid <= 0:
            raise PaymentError("The deposit is already paid. Refresh the page.", status=409)
        if amount != statement.deposit_unpaid:
            raise PaymentError(f"The deposit is received exactly: {peso(statement.deposit_unpaid)}. A part of the deposit can't be received.")
    elif purpose == Payment.Purpose.FULL:
        if statement.deposit_unpaid <= 0:
            raise PaymentError("The deposit is already paid, so \"Paid in full\" is no longer a choice. Record the balance instead.", status=409)
        if amount != statement.balance_core:
            raise PaymentError(f"Paid in full is exactly {peso(statement.balance_core)} (the rental and the deposit).")
    elif purpose == Payment.Purpose.BALANCE:
        if statement.deposit_unpaid > 0:
            raise PaymentError(f"The deposit ({peso(statement.deposit_unpaid)}) has to be paid first.")
        if statement.rental_unpaid <= 0:
            raise PaymentError("The rental is already paid. Refresh the page.", status=409)
        if amount > statement.rental_unpaid:
            raise PaymentError(f"Only {peso(statement.rental_unpaid)} is left to pay. You can't receive more than that.")
    elif purpose == Payment.Purpose.EXTRA:
        if statement.extra_owed <= 0:
            raise PaymentError("Nothing extra is owed on this booking.", status=409)
        if amount > statement.extra_owed:
            raise PaymentError(f"Only {peso(statement.extra_owed)} is owed. You can't receive more than that.")
    else:
        raise PaymentError("Choose what this payment is for. If this page has been open for a while, refresh it.")


def record_payment(*, reservation_id, purpose, amount, method, reference="", note="", user=None, request_id="", now=None):
    """Save a payment received at the shop. Returns the Payment (an earlier one when `request_id` was already used)."""
    now = now or timezone.now()
    request_id = (request_id or "")[:40] or None
    method, digits = _method_and_reference(method, reference)
    amount = money(amount)
    if amount <= 0:
        raise PaymentError("Please type the amount received.")
    note = _clean_note(note)
    served_by = staff_display_name(user) if user is not None else ""

    with transaction.atomic():
        reservation = _lock(reservation_id)
        if request_id:
            earlier = Payment.objects.filter(request_id=request_id).first()
            if earlier is not None:
                return earlier
        if not ledger.counts_as_tracked(reservation) or reservation.status == Reservation.Status.CANCELLED:
            raise PaymentError(f"This booking is {reservation.status.lower()}, so it can't take a payment.", status=409)
        ensure_rows(reservation)
        before = ledger.statement(_fresh(reservation.pk))
        check_amount(before, purpose, amount)

        if digits:
            used = _reference_in_use(digits, reservation)
            if used:
                raise PaymentError(f"This GCash reference number is already recorded on {used}. A reference number pays only once.", status=409)

        payment = Payment.objects.create(
            reservation=reservation, direction=Payment.Direction.IN, purpose=purpose, method=method, amount=amount, reference=digits, paid_at=now,
            recorded_by=user if getattr(user, "pk", None) else None, recorded_by_name=served_by, note=note, request_id=request_id,
            receipt_number=ReceiptSequence.next_number(timezone.localtime(now).year),
        )
        after = ledger.statement(_fresh(reservation.pk))
        payment.snapshot = _snapshot(reservation, payment, before, after, served_by=served_by)
        payment.save(update_fields=["snapshot"])

        _event(reservation, PAYMENT_EVENT, f"{peso(amount)} received ({payment.get_purpose_display()}, {_way(payment)}). "
                                           f"Receipt {payment.receipt_number}.")
        _notify(reservation, payment, _notice_text(payment), subject=_subject(payment), heading=_heading(payment))
        sync_settled(reservation.pk, quiet=True)
    return payment


# ---------------------------------------------------------------- money PayMongo confirmed
ONLINE_METHODS = {
    "gcash": Payment.Method.PM_GCASH,
    "paymaya": Payment.Method.PM_MAYA,
    "qrph": Payment.Method.PM_QRPH,
}
ONLINE_SERVED_BY = "Online payment"


def online_method(channel):
    """How PayMongo names the way the customer paid (gcash, paymaya, qrph) -> the Payment method the shop's pages show."""
    return ONLINE_METHODS.get((channel or "").lower(), Payment.Method.PM_OTHER)


def record_online_payment(*, reservation, purpose, amount, channel, provider_ref, paid_at=None, livemode=False, request_id="", served_by_user=None):
    """Record the payment a customer made on PayMongo's checkout, with its numbered Payment Receipt.

    Called by the online checkout INSIDE the transaction that makes the booking (gowns/online_checkout.py), right after the booking's rows exist,
    so the booking and its payment are saved together or not at all. The booking is a NEW one with nothing recorded yet: this is the first row,
    so nothing is "derived" from older data (a booking with no rows is read as an old one, which is exactly what must not happen here).

    The amount must be exactly what the choice asks for -- the deposit, or the rental and the deposit -- else it refuses and the booking is not made.
    Returns the Payment (an earlier one when `request_id` was already used). The customer's bell message and email are sent like any payment.
    `served_by_user`: a walk-in customer who paid on their phone at the counter -- the receipt then names the staff member who served them
    (an online customer's says "Online payment")."""
    request_id = (request_id or "")[:40] or None
    amount = money(amount)
    served_by = staff_display_name(served_by_user) if served_by_user is not None else ONLINE_SERVED_BY
    if purpose not in (Payment.Purpose.DEPOSIT, Payment.Purpose.FULL):
        raise PaymentError("An online payment is the deposit or the deposit with the rental.")
    deposit_due, rental_due = money(reservation.security_deposit), ledger.rental_due(reservation)
    expected = deposit_due if purpose == Payment.Purpose.DEPOSIT else deposit_due + rental_due
    if amount != expected:
        raise PaymentError(f"The online payment of {peso(amount)} is not the {peso(expected)} this booking asks for.")
    now = timezone.now()
    with transaction.atomic():
        if request_id:
            earlier = Payment.objects.filter(request_id=request_id).first()
            if earlier is not None:
                return earlier
        method = online_method(channel)
        how = Payment.Method(method).label
        note = f"Paid online on {gateway.label()}'s checkout ({how})." + ("" if livemode else " TEST PAYMENT: no real money moved.")
        payment = Payment.objects.create(
            reservation=reservation, direction=Payment.Direction.IN, purpose=purpose, method=method, amount=amount, reference="", paid_at=paid_at or now,
            recorded_by=served_by_user if getattr(served_by_user, "pk", None) else None, recorded_by_name=served_by, note=note, request_id=request_id,
            provider=gateway.name(), provider_ref=(provider_ref or "")[:60],
            receipt_number=ReceiptSequence.next_number(timezone.localtime(now).year),
        )
        # The statement is read AFTER the row exists (so it is this booking's own row that is counted, never an old-booking guess); the figures
        # "before" are the same ones less this payment.
        after = ledger.statement(_fresh(reservation.pk))
        before = ledger.Statement(**{**after.__dict__, "received": money(after.received) - amount})
        payment.snapshot = _snapshot(reservation, payment, before, after, served_by=served_by)
        payment.save(update_fields=["snapshot"])
        ReservationStatusEvent.record(
            reservation, PAYMENT_EVENT, detail=f"{peso(amount)} received ({payment.get_purpose_display()}, {how}). Receipt {payment.receipt_number}.",
            actor=ReservationStatusEvent.Actor.SYSTEM,
        )
        _notify(reservation, payment, _notice_text(payment), subject=_subject(payment), heading=_heading(payment))
    return payment


# ---------------------------------------------------------------- an existing booking paid online from the shop's QR code
def record_online_booking_payment(*, reservation, purpose, amount, channel, provider_ref, paid_at=None, livemode=False, request_id="", note="",
                                  served_by_user=None):
    """Record the payment a customer made on the online payment page for a booking that ALREADY exists (the Payments page's "Pay online" QR code:
    gowns/counter_payment.py). The same rules as a payment received at the shop (check_amount), its own numbered receipt, the customer told and the history
    written. Call it inside a transaction with the booking row locked; it raises PaymentError (nothing written) when the booking can no longer take this
    money. Returns the Payment (an earlier one when `request_id` was already used)."""
    request_id = (request_id or "")[:40] or None
    if request_id:
        earlier = Payment.objects.filter(request_id=request_id).first()
        if earlier is not None:
            return earlier
    amount = money(amount)
    if not ledger.counts_as_tracked(reservation) or reservation.status == Reservation.Status.CANCELLED:
        raise PaymentError(f"This booking is {reservation.status.lower()}, so it can't take a payment.", status=409)
    ensure_rows(reservation)
    before = ledger.statement(_fresh(reservation.pk))
    check_amount(before, purpose, amount)
    served_by = staff_display_name(served_by_user) if served_by_user is not None else ONLINE_SERVED_BY
    method = online_method(channel)
    how = Payment.Method(method).label
    words = f"Paid online on {gateway.label()}'s checkout ({how})." + ("" if livemode else " TEST PAYMENT: no real money moved.")
    note = _clean_note(note)
    now = timezone.now()
    payment = Payment.objects.create(
        reservation=reservation, direction=Payment.Direction.IN, purpose=purpose, method=method, amount=amount, reference="", paid_at=paid_at or now,
        recorded_by=served_by_user if getattr(served_by_user, "pk", None) else None, recorded_by_name=served_by,
        note=(f"{words} {note}" if note else words)[:REASON_MAX], request_id=request_id, provider=gateway.name(), provider_ref=(provider_ref or "")[:60],
        receipt_number=ReceiptSequence.next_number(timezone.localtime(now).year),
    )
    after = ledger.statement(_fresh(reservation.pk))
    payment.snapshot = _snapshot(reservation, payment, before, after, served_by=served_by)
    payment.save(update_fields=["snapshot"])
    ReservationStatusEvent.record(
        reservation, PAYMENT_EVENT, detail=f"{peso(amount)} received ({payment.get_purpose_display()}, {how}). Receipt {payment.receipt_number}.",
        actor=ReservationStatusEvent.Actor.SYSTEM,
    )
    _notify(reservation, payment, _notice_text(payment), subject=_subject(payment), heading=_heading(payment))
    sync_settled(reservation.pk, quiet=True, actor=ReservationStatusEvent.Actor.SYSTEM)
    return payment


# ---------------------------------------------------------------- the deposit handed back
def return_deposit(*, reservation_id, amount, method, reference="", note="", user=None, request_id="", now=None):
    """Record the security deposit handed back to the customer (Cash, or GCash sent from the shop's own GCash app)."""
    now = now or timezone.now()
    request_id = (request_id or "")[:40] or None
    method, digits = _method_and_reference(method, reference)
    amount = money(amount)
    if amount <= 0:
        raise PaymentError("Please type the amount handed back.")
    note = _clean_note(note)
    served_by = staff_display_name(user) if user is not None else ""

    with transaction.atomic():
        reservation = _lock(reservation_id)
        if request_id:
            earlier = Payment.objects.filter(request_id=request_id).first()
            if earlier is not None:
                return earlier
        if not ledger.counts_as_tracked(reservation) or reservation.status == Reservation.Status.CANCELLED:
            raise PaymentError("A cancelled booking keeps its deposit, so there is nothing to give back.", status=409)
        ensure_rows(reservation)
        before = ledger.statement(_fresh(reservation.pk))
        if not before.all_returned:
            raise PaymentError("The deposit can be handed back once every gown of the booking is returned.", status=409)
        if before.refund_available <= 0:
            raise PaymentError("There is no deposit left to hand back on this booking.", status=409)
        if amount > before.refund_available:
            raise PaymentError(f"Only {peso(before.refund_available)} can be handed back (the deposit less the fees and deductions).")
        if digits:
            used = _reference_in_use(digits, reservation)
            if used:
                raise PaymentError(f"This GCash reference number is already recorded on {used}. A reference number is used once.", status=409)

        payment = Payment.objects.create(
            reservation=reservation, direction=Payment.Direction.OUT, purpose=Payment.Purpose.DEPOSIT_RETURN, method=method, amount=amount,
            reference=digits, paid_at=now, recorded_by=user if getattr(user, "pk", None) else None, recorded_by_name=served_by, note=note,
            request_id=request_id, receipt_number=ReceiptSequence.next_number(timezone.localtime(now).year),
        )
        after = ledger.statement(_fresh(reservation.pk))
        payment.snapshot = _snapshot(reservation, payment, before, after, served_by=served_by)
        payment.save(update_fields=["snapshot"])
        _event(reservation, RETURN_EVENT, f"{peso(amount)} of the security deposit was handed back ({_way(payment)}). "
                                          f"Receipt {payment.receipt_number}.")
        _notify(reservation, payment, _notice_text(payment), subject=_subject(payment), heading=_heading(payment))
        sync_settled(reservation.pk, quiet=True)
    return payment


# ---------------------------------------------------------------- the deposit sent back through PayMongo
# How long after the payment PayMongo can still send money back, per way of paying. PayMongo's own documented limits are 180 days (GCash), 12 months
# (Maya) and 30 days (QR Ph); the shop stays a little inside them so a refund is never asked for on the last day.
def _way(payment):
    """How a payment was made, for a sentence: "Cash", "GCash", "Maya online" (the receipt itself reads "Maya (online payment)")."""
    return payment.get_method_display().replace(" (online payment)", " online")


def _refund_failed_reason():
    return f"{gateway.label()} could not send this refund, so it was cancelled here. Hand the deposit back another way."


def _online_payments(reservation, direction):
    """The payments a customer made through an online service (PayMongo or Xendit), in one direction."""
    return [p for p in reservation.payments.all() if p.provider in gateway.PROVIDERS and p.direction == direction and not p.is_void]


def _deposit_sources(reservation):
    """The online payments that covered the security deposit ("Deposit" or "Paid in full"): the only money that can be sent back online."""
    return [p for p in _online_payments(reservation, Payment.Direction.IN)
            if p.provider_ref and p.purpose in (Payment.Purpose.DEPOSIT, Payment.Purpose.FULL)]


def _online_refund_source(reservation, amount, now):
    """The online payment a refund of `amount` would be made against -> (Payment, "") or (None, why not). PayMongo sends money back only for a
    payment made through it, inside the time limit of the way it was paid, never more than that payment came to, and (Maya) a PART of a payment
    only from the day after it was paid."""
    sources = _deposit_sources(reservation)
    if not sources:
        return None, f"This booking was not paid on {gateway.label()}, so the deposit is handed back at the shop."
    source = sources[0]
    days = gateway.refund_days().get(source.method)
    if days is None:
        return None, f"{gateway.label()} cannot send this kind of payment back automatically, so hand it back in cash."
    if now - source.paid_at > timedelta(days=days):
        return None, f"It was paid more than {days} days ago, which is too long for {gateway.label()} to send it back."
    already = sum((money(p.amount) for p in _online_payments(reservation, Payment.Direction.OUT)), Decimal("0"))
    if already + money(amount) > money(source.amount):
        return None, "That is more than the online payment came to."
    if source.method == Payment.Method.PM_MAYA and money(amount) != money(source.amount) and timezone.localdate(source.paid_at) == timezone.localdate(now):
        return None, "Maya can send back part of a payment only from the day after it was paid."
    return source, ""


def online_refund_choice(reservation, statement, now=None):
    """What the Return-the-deposit form should offer for this booking: {"offered", "available", "label", "reason"}. Money only ever goes back online to a
    DEPOSIT that was paid online; "offered" is False when there is none (or online refunds are off) and "reason" says why it is cash. The server checks
    it all again when the form is sent."""
    def cash(reason):
        return {"offered": False, "available": False, "label": "", "reason": reason}

    if not gateway.auto_refund() or not gateway.enabled():
        return cash("Online refunds are switched off, so the deposit is handed back in cash.")
    sources = _deposit_sources(reservation)
    if not sources:
        return cash("The deposit was not paid online, so it is handed back in cash. Money can only be sent back online to a payment that was made online.")
    now = now or timezone.now()
    amount = statement.refund_available if statement.refund_available > 0 else money(sources[0].amount)
    source, reason = _online_refund_source(reservation, amount, now)
    label = Payment.Method(sources[0].method).label.replace(" (online payment)", "")
    return {"offered": True, "available": source is not None, "label": label, "reason": reason}


def _void_quietly(payment_id, reason):
    """Cancel a deposit-return row whose refund never left (PayMongo refused it): voided with the reason, and nobody is told -- the customer was never
    told it was sent."""
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        if payment.is_void:
            return
        payment.status = Payment.Status.VOID
        payment.void_reason = reason[:REASON_MAX]
        payment.voided_by_name = "System"
        payment.voided_at = timezone.now()
        payment.save(update_fields=["status", "void_reason", "voided_by_name", "voided_at"])


def return_deposit_online(*, reservation_id, amount, note="", user=None, request_id="", now=None):
    """Hand the security deposit back through PayMongo, to the GCash / Maya / QR Ph account the customer paid with.

    The receipt row is saved FIRST (so money can never leave without a record), then PayMongo is asked; if PayMongo refuses, the row is cancelled and
    the staff member is told (nothing was sent). If PayMongo accepts, the customer is told and the receipt is theirs. A request that already
    has a row but no PayMongo refund (the server stopped halfway) is refused until a person checks PayMongo's dashboard -- PayMongo has no way to
    say "this request was already made", so asking twice could send the money twice."""
    if not gateway.auto_refund() or not gateway.enabled():
        raise PaymentError("Automatic refunds are not switched on. Hand the deposit back in cash.", status=409)
    now = now or timezone.now()
    request_id = (request_id or "")[:40] or None
    amount = money(amount)
    if amount <= 0:
        raise PaymentError("Please type the amount handed back.")
    note = _clean_note(note)
    served_by = staff_display_name(user) if user is not None else ""

    with transaction.atomic():
        reservation = _lock(reservation_id)
        if request_id:
            earlier = Payment.objects.filter(request_id=request_id).first()
            if earlier is not None:
                if earlier.provider in gateway.PROVIDERS and not earlier.provider_ref and not earlier.is_void:
                    raise PaymentError(f"An earlier try to send this refund did not finish. Check {gateway.label()}'s dashboard to see whether it was sent, then void "
                                       "that receipt before trying again.", status=409)
                return earlier
        if not ledger.counts_as_tracked(reservation) or reservation.status == Reservation.Status.CANCELLED:
            raise PaymentError("A cancelled booking keeps its deposit, so there is nothing to give back.", status=409)
        ensure_rows(reservation)
        before = ledger.statement(_fresh(reservation.pk))
        if not before.all_returned:
            raise PaymentError("The deposit can be handed back once every gown of the booking is returned.", status=409)
        if before.refund_available <= 0:
            raise PaymentError("There is no deposit left to hand back on this booking.", status=409)
        if amount > before.refund_available:
            raise PaymentError(f"Only {peso(before.refund_available)} can be handed back (the deposit less the fees and deductions).")
        source, why = _online_refund_source(reservation, amount, now)
        if source is None:
            raise PaymentError(why, status=409)
        payment = Payment.objects.create(
            reservation=reservation, direction=Payment.Direction.OUT, purpose=Payment.Purpose.DEPOSIT_RETURN, method=source.method, amount=amount, reference="",
            paid_at=now, recorded_by=user if getattr(user, "pk", None) else None, recorded_by_name=served_by, note=note, request_id=request_id,
            provider=gateway.name(), provider_ref="", receipt_number=ReceiptSequence.next_number(timezone.localtime(now).year),
        )
        after = ledger.statement(_fresh(reservation.pk))
        payment.snapshot = _snapshot(reservation, payment, before, after, served_by=served_by)
        payment.save(update_fields=["snapshot"])
        code, paid_ref = reservation.reference_code, source.provider_ref

    try:
        refund = gateway.create_refund(payment_id=paid_ref, amount=amount, note=f"Security deposit return for {code}")
    except gateway.GatewayError as exc:
        _void_quietly(payment.pk, f"{gateway.label()} would not send the refund: {str(exc)[:120]}")
        raise PaymentError(f"{gateway.label()} would not send the refund just now ({str(exc)[:120]}). Nothing was sent or recorded. Try again, or hand the deposit back "
                           "in cash.", status=502)

    with transaction.atomic():
        Payment.objects.filter(pk=payment.pk).update(provider_ref=refund.id[:60])
        payment = Payment.objects.select_related("reservation").get(pk=payment.pk)
        reservation = payment.reservation
        _event(reservation, RETURN_EVENT, f"{peso(amount)} of the security deposit was sent back to the customer through {gateway.label()} "
                                          f"({_way(payment)}). Receipt {payment.receipt_number}.")
        _notify(reservation, payment, _notice_text(payment), subject=_subject(payment), heading=_heading(payment))
        sync_settled(reservation.pk, quiet=True)
    return payment


def on_online_refund_updated(refund_id, status):
    """PayMongo says a refund it was asked for changed (webhook payment.refund.updated). A refund that FAILED cancels the deposit-return row it
    belonged to (so the deposit shows as still to hand back), and the customer is told the shop will hand it back. Returns True when the refund was
    one of ours."""
    payment = Payment.objects.select_related("reservation__customer").filter(provider__in=gateway.PROVIDERS, provider_ref=refund_id, direction=Payment.Direction.OUT).first()
    if payment is None:
        return False
    if status != "failed" or payment.is_void:
        return True
    with transaction.atomic():
        locked = Payment.objects.select_for_update().get(pk=payment.pk)
        if locked.is_void:
            return True
        locked.status = Payment.Status.VOID
        locked.void_reason = _refund_failed_reason()[:REASON_MAX]
        locked.voided_by_name = gateway.label()
        locked.voided_at = timezone.now()
        locked.save(update_fields=["status", "void_reason", "voided_by_name", "voided_at"])
        reservation = payment.reservation
        ReservationStatusEvent.record(reservation, "Automatic deposit refund failed",
                                      detail=f"{gateway.label()} could not send {peso(locked.amount)} back (receipt {locked.receipt_number}). Hand the deposit back another way.",
                                      actor=ReservationStatusEvent.Actor.SYSTEM, staff_only=True)
        if reservation.customer_id:
            notify_user(reservation.customer, f"We could not send your security deposit of {peso(locked.amount)} for {reservation.reference_code} back automatically. "
                                              "The shop will hand it back to you. Please contact the shop if you have a question.",
                        subject="We will hand your security deposit back at the shop", heading="Your security deposit")
        sync_settled(reservation.pk, quiet=True, actor=ReservationStatusEvent.Actor.SYSTEM, reopens=True)
    return True


# ---------------------------------------------------------------- a deduction from the deposit
def add_deduction(*, reservation_id, amount, reason, user=None, request_id="", now=None):
    """Take an amount off the customer's deposit (damage, a stain, a missing item). Needs a reason; the customer is told."""
    now = now or timezone.now()
    request_id = (request_id or "")[:40] or None
    amount = money(amount)
    if amount <= 0:
        raise PaymentError("Please type the amount of the deduction.")
    reason = _clean_reason(reason)
    with transaction.atomic():
        reservation = _lock(reservation_id)
        if request_id:
            earlier = Deduction.objects.filter(request_id=request_id).first()
            if earlier is not None:
                return earlier
        if not ledger.counts_as_tracked(reservation) or reservation.status == Reservation.Status.CANCELLED:
            raise PaymentError("A cancelled booking keeps its deposit, so there is nothing to deduct from.", status=409)
        ensure_rows(reservation)
        deduction = Deduction.objects.create(
            reservation=reservation, amount=amount, reason=reason, recorded_by=user if getattr(user, "pk", None) else None,
            recorded_by_name=staff_display_name(user) if user is not None else "", request_id=request_id, created_at=now,
        )
        after = ledger.statement(_fresh(reservation.pk))
        if after.refunded > after.refundable_total:
            raise PaymentError("The deposit was already handed back. Void the deposit return receipt first, then add the deduction.", status=409)
        _event(reservation, DEDUCTION_EVENT, f"{peso(amount)} was taken off the security deposit: {reason}.")
        _notify(reservation, None,
                f"{peso(amount)} was taken off the security deposit of {reservation.reference_code}: {reason}. "
                f"Deposit to get back: {peso(after.refundable_total)}.",
                subject=f"A deduction on your security deposit - {reservation.reference_code}", heading="A deduction on your security deposit")
        sync_settled(reservation.pk, quiet=True, reopens=True)
    return deduction


# ---------------------------------------------------------------- voiding a mistake
def void_payment(*, payment_id, reason, user=None, now=None):
    """Void a recorded payment (a mistake). The row stays, marked void with the reason, who and when; the booking's numbers follow."""
    now = now or timezone.now()
    reason = _clean_reason(reason, what="reason for voiding it")
    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().get(pk=payment_id)
        except Payment.DoesNotExist:
            raise PaymentError("That receipt was not found. Please refresh the page.", status=404)
        reservation = _lock(payment.reservation_id)
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        if payment.is_void:
            raise PaymentError("This receipt is already voided.", status=409)
        if payment.is_legacy:
            raise PaymentError("This payment was recorded before the Payments page, so it has no receipt to void.", status=409)
        payment.status = Payment.Status.VOID
        payment.void_reason = reason
        payment.voided_by = user if getattr(user, "pk", None) else None
        payment.voided_by_name = staff_display_name(user) if user is not None else ""
        payment.voided_at = now
        payment.save(update_fields=["status", "void_reason", "voided_by", "voided_by_name", "voided_at"])
        after = ledger.statement(_fresh(reservation.pk))
        if after.refunded > after.refundable_total:
            raise PaymentError("The deposit was already handed back out of this money. Void the deposit return receipt first.", status=409)
        _event(reservation, VOID_EVENT, f"Receipt {payment.receipt_number} ({peso(payment.amount)}) was voided: {reason}.")
        _notify(reservation, payment, _notice_text(payment), subject=_subject(payment), heading=_heading(payment))
        sync_settled(reservation.pk, quiet=True, reopens=True)
    return payment


def void_deduction(*, deduction_id, reason, user=None, now=None):
    now = now or timezone.now()
    reason = _clean_reason(reason, what="reason for removing it")
    with transaction.atomic():
        try:
            deduction = Deduction.objects.select_for_update().get(pk=deduction_id)
        except Deduction.DoesNotExist:
            raise PaymentError("That deduction was not found. Please refresh the page.", status=404)
        reservation = _lock(deduction.reservation_id)
        deduction = Deduction.objects.select_for_update().get(pk=deduction_id)
        if deduction.is_void:
            raise PaymentError("This deduction is already removed.", status=409)
        deduction.status = Deduction.Status.VOID
        deduction.void_reason = reason
        deduction.voided_by = user if getattr(user, "pk", None) else None
        deduction.voided_by_name = staff_display_name(user) if user is not None else ""
        deduction.voided_at = now
        deduction.save(update_fields=["status", "void_reason", "voided_by", "voided_by_name", "voided_at"])
        _event(reservation, DEDUCTION_EVENT, f"The {peso(deduction.amount)} deduction was removed: {reason}.")
        _notify(reservation, None, f"The {peso(deduction.amount)} deduction on the security deposit of {reservation.reference_code} was removed: {reason}.",
                subject=f"A deduction was removed - {reservation.reference_code}", heading="A deduction was removed")
        sync_settled(reservation.pk, quiet=True, reopens=True)
    return deduction


# ---------------------------------------------------------------- printing
def mark_printed(payment_id):
    """Count a staff view of the print page. The first one is the original paper; every later one is a REPRINT (returned as its number)."""
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        if payment.first_printed_at is None:
            Payment.objects.filter(pk=payment.pk).update(first_printed_at=timezone.now())
            return 0
        Payment.objects.filter(pk=payment.pk).update(reprint_count=F("reprint_count") + 1)
        return payment.reprint_count + 1
