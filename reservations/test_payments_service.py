"""Recording money (reservations/payments.py): what is accepted, what is refused, and that nothing can be saved twice or silently changed.

₱3,500 rental + ₱2,000 deposit. The deposit is received EXACTLY (alone, or together with the rental as "Paid in full"); the rental balance may
be paid in parts; a deposit return is never more than the system says is refundable."""
import threading
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from accounts.models import CustomerMessage
from reservations import ledger, payments
from reservations.models import Deduction, Payment, ReceiptRecord, ReceiptSequence, Reservation, ReservationItem, ReservationStatusEvent
from reservations.payments import PaymentError
from reservations.test_ledger import make_booking, pay, stmt

User = get_user_model()
D = Decimal
P = Payment.Purpose
M = Payment.Method
REF = "5046933479702"
REF2 = "5046933479720"


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("pay_staff", password="x", is_staff=True, first_name="Maria", last_name="Cruz")

    def record(self, booking, purpose, amount, method=M.CASH, reference="", **kw):
        return payments.record_payment(reservation_id=booking.pk, purpose=purpose, amount=D(amount), method=method, reference=reference,
                                       user=self.staff, **kw)

    def refused(self, call, *args, **kw):
        with self.assertRaises(PaymentError) as caught:
            call(*args, **kw)
        return caught.exception.message


class DepositRulesTests(Base):
    def test_the_exact_deposit_is_received_and_gets_a_numbered_receipt(self):
        booking = make_booking()
        payment = self.record(booking, P.DEPOSIT, "2000")
        year = timezone.localdate().year
        self.assertEqual((payment.direction, payment.amount, payment.receipt_number), (Payment.Direction.IN, D("2000.00"), f"PR-{year}-000001"))
        self.assertEqual((payment.recorded_by, payment.recorded_by_name), (self.staff, "Maria Cruz"))
        self.assertEqual(stmt(booking).balance_due, D("3500.00"))

    def test_a_part_of_the_deposit_is_refused(self):
        booking = make_booking()
        for amount in ("1000", "1999.99", "2000.01", "4000"):
            with self.subTest(amount=amount):
                self.assertIn("received exactly", self.refused(self.record, booking, P.DEPOSIT, amount))
        self.assertEqual(Payment.objects.count(), 0)

    def test_paid_in_full_is_exactly_the_rental_and_the_deposit(self):
        booking = make_booking()
        self.assertIn("exactly", self.refused(self.record, booking, P.FULL, "5000"))
        payment = self.record(booking, P.FULL, "5500")
        self.assertEqual((payment.purpose, stmt(booking).balance_due), (P.FULL, D("0.00")))

    def test_the_deposit_is_asked_in_one_piece_whatever_the_owner_sets(self):
        booking = make_booking(deposit="4000", gowns=2)                       # two gowns: 2 x 2,000
        self.assertIn("received exactly: ₱4,000.00", self.refused(self.record, booking, P.DEPOSIT, "2000"))
        self.record(booking, P.DEPOSIT, "4000")

    def test_the_deposit_cannot_be_received_twice(self):
        booking = make_booking()
        self.record(booking, P.DEPOSIT, "2000")
        self.assertEqual(self.refused(self.record, booking, P.DEPOSIT, "2000").startswith("The deposit is already paid"), True)
        self.assertIn("no longer a choice", self.refused(self.record, booking, P.FULL, "3500"))


class BalanceRulesTests(Base):
    def test_the_rental_balance_is_paid_in_parts_and_never_beyond_what_is_owed(self):
        booking = make_booking()
        self.record(booking, P.DEPOSIT, "2000")
        self.record(booking, P.BALANCE, "2000")
        self.assertIn("Only ₱1,500.00 is left", self.refused(self.record, booking, P.BALANCE, "1500.01"))
        self.record(booking, P.BALANCE, "1500")
        self.assertEqual(stmt(booking).balance_due, D("0.00"))
        self.assertIn("already paid", self.refused(self.record, booking, P.BALANCE, "1"))

    def test_the_deposit_must_be_paid_before_a_balance_payment(self):
        booking = make_booking()
        self.assertIn("has to be paid first", self.refused(self.record, booking, P.BALANCE, "500"))

    def test_nothing_or_a_negative_or_a_zero_amount_is_refused(self):
        booking = make_booking()
        self.record(booking, P.DEPOSIT, "2000")
        for amount in ("0", "-5", "0.001"):
            with self.subTest(amount=amount):
                self.assertIn("amount received", self.refused(self.record, booking, P.BALANCE, amount))

    def test_an_unknown_purpose_or_method_is_refused(self):
        booking = make_booking()
        self.assertIn("Choose what this payment is for", self.refused(self.record, booking, "Gift", "2000"))
        self.assertIn("Choose what this payment is for", self.refused(self.record, booking, P.DEPOSIT_RETURN, "2000"))
        self.assertIn("Choose Cash or GCash", self.refused(self.record, booking, P.DEPOSIT, "2000", method="Check"))

    def test_a_booking_that_is_not_running_takes_no_payment(self):
        for status in (Reservation.Status.PENDING, Reservation.Status.REJECTED, Reservation.Status.CANCELLED):
            with self.subTest(status=status):
                self.assertEqual(self.refused(self.record, make_booking(status=status), P.DEPOSIT, "2000").startswith("This booking is"), True)
        self.assertEqual(Payment.objects.count(), 0)

    def test_an_extra_amount_is_only_taken_when_a_late_fee_is_more_than_the_deposit(self):
        booking = make_booking(stage=ReservationItem.Stage.RETURNED, ret=-14, returned_on=-2)       # 12 days late = 2,400 > 2,000
        self.record(booking, P.FULL, "5500")
        self.assertEqual(stmt(booking).extra_owed, D("400.00"))
        self.assertIn("Only ₱400.00 is owed", self.refused(self.record, booking, P.EXTRA, "500"))
        self.record(booking, P.EXTRA, "400")
        self.assertEqual(stmt(booking).balance_due, D("0.00"))
        self.assertIn("Nothing extra", self.refused(self.record, booking, P.EXTRA, "1"))


class GcashReferenceTests(Base):
    def test_gcash_needs_the_thirteen_digit_reference_and_cash_needs_none(self):
        booking = make_booking()
        self.assertIn("13 digits", self.refused(self.record, booking, P.DEPOSIT, "2000", method=M.GCASH, reference="12345"))
        self.assertIn("13-digit", self.refused(self.record, booking, P.DEPOSIT, "2000", method=M.GCASH, reference=""))
        payment = self.record(booking, P.DEPOSIT, "2000", method=M.GCASH, reference="5046 933 479702")
        self.assertEqual(payment.reference, REF)
        cash = self.record(make_booking(), P.DEPOSIT, "2000", method=M.CASH, reference="999")
        self.assertEqual(cash.reference, "")

    def test_a_reference_number_pays_only_once(self):
        first, second = make_booking(), make_booking()
        self.record(first, P.DEPOSIT, "2000", method=M.GCASH, reference=REF)
        self.assertIn("already recorded on " + first.reference_code, self.refused(self.record, second, P.DEPOSIT, "2000", method=M.GCASH, reference=REF))
        self.record(second, P.DEPOSIT, "2000", method=M.GCASH, reference=REF2)

    def test_a_number_already_on_an_online_booking_is_refused_too(self):
        online = make_booking(channel=Reservation.Channel.ONLINE, gcash_reference=REF)
        other = make_booking()
        self.assertIn("already recorded on " + online.reference_code, self.refused(self.record, other, P.DEPOSIT, "2000", method=M.GCASH, reference=REF))

    def test_a_voided_payments_number_can_be_used_again(self):
        booking = make_booking()
        payment = self.record(booking, P.DEPOSIT, "2000", method=M.GCASH, reference=REF)
        payments.void_payment(payment_id=payment.pk, reason="typed the wrong booking", user=self.staff)
        self.record(booking, P.DEPOSIT, "2000", method=M.GCASH, reference=REF)


class NoDoubleSaveTests(Base):
    def test_the_same_request_twice_saves_one_payment_and_one_receipt_number(self):
        booking = make_booking()
        first = self.record(booking, P.DEPOSIT, "2000", request_id="abc-123")
        again = self.record(booking, P.DEPOSIT, "2000", request_id="abc-123")
        self.assertEqual((again.pk, Payment.objects.count()), (first.pk, 1))
        self.assertEqual(ReceiptSequence.objects.get().next_value, 2)

    def test_receipt_numbers_follow_each_other_and_never_repeat(self):
        booking = make_booking()
        numbers = [self.record(booking, P.DEPOSIT, "2000").receipt_number]
        numbers.append(self.record(booking, P.BALANCE, "100").receipt_number)
        numbers.append(self.record(booking, P.BALANCE, "100").receipt_number)
        self.assertEqual([int(n[-6:]) for n in numbers], [1, 2, 3])
        self.assertEqual(len(set(numbers)), 3)

    def test_a_refused_payment_uses_no_receipt_number(self):
        booking = make_booking()
        self.refused(self.record, booking, P.DEPOSIT, "1000")
        self.assertEqual(self.record(booking, P.DEPOSIT, "2000").receipt_number[-6:], "000001")


class DepositReturnTests(Base):
    def back_late(self, days=3):
        booking = make_booking(stage=ReservationItem.Stage.RETURNED, ret=-6, returned_on=-6 + days)
        self.record(booking, P.FULL, "5500")
        return booking

    def give_back(self, booking, amount, method=M.CASH, reference="", **kw):
        return payments.return_deposit(reservation_id=booking.pk, amount=D(amount), method=method, reference=reference, user=self.staff, **kw)

    def test_the_refund_is_the_deposit_less_the_late_fee_and_it_prints_the_working(self):
        booking = self.back_late(3)
        payment = self.give_back(booking, "1400")
        self.assertEqual((payment.direction, payment.purpose, payment.title), (Payment.Direction.OUT, P.DEPOSIT_RETURN, "DEPOSIT RETURN"))
        ret = payment.snapshot["return"]
        self.assertEqual((ret["deposit_paid"], ret["late_fee"], ret["returned_now"], ret["still_to_return"]), ("2000.00", "600.00", "1400.00", "0.00"))
        self.assertEqual(ret["late_lines"][0]["days"], 3)
        self.assertEqual(stmt(booking).state, ledger.SETTLED)

    def test_never_more_than_the_system_says_is_refundable(self):
        booking = self.back_late(3)
        self.assertIn("Only ₱1,400.00 can be handed back", self.refused(self.give_back, booking, "1400.01"))
        self.give_back(booking, "1000")
        self.assertIn("Only ₱400.00", self.refused(self.give_back, booking, "500"))
        self.give_back(booking, "400")
        self.assertIn("no deposit left", self.refused(self.give_back, booking, "1"))

    def test_only_once_every_gown_is_back(self):
        booking = make_booking(stage=ReservationItem.Stage.OVERDUE, ret=-3)
        self.record(booking, P.FULL, "5500")
        self.assertIn("once every gown", self.refused(self.give_back, booking, "100"))

    def test_a_cancelled_booking_keeps_its_deposit(self):
        booking = make_booking(status=Reservation.Status.CANCELLED)
        self.assertIn("keeps its deposit", self.refused(self.give_back, booking, "100"))

    def test_a_gcash_return_needs_its_reference_number(self):
        booking = self.back_late(0)
        self.assertIn("13-digit", self.refused(self.give_back, booking, "2000", method=M.GCASH))
        payment = self.give_back(booking, "2000", method=M.GCASH, reference=REF)
        self.assertEqual((payment.method, payment.reference), (M.GCASH, REF))

    def test_the_same_request_twice_hands_back_once(self):
        booking = self.back_late(0)
        a = self.give_back(booking, "2000", request_id="ret-1")
        b = self.give_back(booking, "2000", request_id="ret-1")
        self.assertEqual((a.pk, Payment.objects.filter(direction=Payment.Direction.OUT).count()), (b.pk, 1))


class DeductionTests(Base):
    def test_a_deduction_needs_a_reason_and_makes_the_refund_smaller(self):
        booking = make_booking(stage=ReservationItem.Stage.RETURNED, ret=-3, returned_on=-3)
        self.record(booking, P.FULL, "5500")
        self.assertIn("reason", self.refused(payments.add_deduction, reservation_id=booking.pk, amount=D("500"), reason="  ", user=self.staff))
        self.assertIn("amount", self.refused(payments.add_deduction, reservation_id=booking.pk, amount=D("0"), reason="Stain", user=self.staff))
        payments.add_deduction(reservation_id=booking.pk, amount=D("500"), reason="Stain on the skirt", user=self.staff)
        self.assertEqual(stmt(booking).refundable_total, D("1500.00"))

    def test_no_deduction_after_the_deposit_was_all_handed_back(self):
        booking = make_booking(stage=ReservationItem.Stage.RETURNED, ret=-3, returned_on=-3)
        self.record(booking, P.FULL, "5500")
        payments.return_deposit(reservation_id=booking.pk, amount=D("2000"), method=M.CASH, user=self.staff)
        message = self.refused(payments.add_deduction, reservation_id=booking.pk, amount=D("500"), reason="Stain", user=self.staff)
        self.assertIn("already handed back", message)
        self.assertEqual(Deduction.objects.count(), 0)

    def test_the_same_request_twice_adds_one_deduction(self):
        booking = make_booking(stage=ReservationItem.Stage.RETURNED, ret=-3, returned_on=-3)
        self.record(booking, P.FULL, "5500")
        for _ in range(2):
            payments.add_deduction(reservation_id=booking.pk, amount=D("500"), reason="Stain", user=self.staff, request_id="ded-1")
        self.assertEqual(Deduction.objects.count(), 1)

    def test_a_deduction_can_be_removed_with_a_reason(self):
        booking = make_booking(stage=ReservationItem.Stage.RETURNED, ret=-3, returned_on=-3)
        self.record(booking, P.FULL, "5500")
        deduction = payments.add_deduction(reservation_id=booking.pk, amount=D("500"), reason="Stain", user=self.staff)
        payments.void_deduction(deduction_id=deduction.pk, reason="it washed out", user=self.staff)
        self.assertEqual((stmt(booking).refundable_total, Deduction.objects.get().status), (D("2000.00"), Deduction.Status.VOID))
        self.assertIn("already removed", self.refused(payments.void_deduction, deduction_id=deduction.pk, reason="again", user=self.staff))


class VoidTests(Base):
    def test_a_void_keeps_the_row_with_who_why_and_when_and_the_numbers_follow(self):
        booking = make_booking()
        payment = self.record(booking, P.FULL, "5500")
        self.assertEqual(stmt(booking).balance_due, D("0.00"))
        payments.void_payment(payment_id=payment.pk, reason="typed on the wrong booking", user=self.staff)
        payment.refresh_from_db()
        self.assertEqual((payment.status, payment.void_reason, payment.voided_by_name), (Payment.Status.VOID, "typed on the wrong booking", "Maria Cruz"))
        self.assertIsNotNone(payment.voided_at)
        self.assertEqual((Payment.objects.count(), stmt(booking).balance_due), (1, D("5500.00")))

    def test_a_reason_is_needed_and_a_receipt_is_voided_once(self):
        booking = make_booking()
        payment = self.record(booking, P.DEPOSIT, "2000")
        self.assertIn("reason", self.refused(payments.void_payment, payment_id=payment.pk, reason="", user=self.staff))
        payments.void_payment(payment_id=payment.pk, reason="mistake", user=self.staff)
        self.assertIn("already voided", self.refused(payments.void_payment, payment_id=payment.pk, reason="again", user=self.staff))

    def test_the_money_a_deposit_return_depends_on_cannot_be_voided_first(self):
        booking = make_booking(stage=ReservationItem.Stage.RETURNED, ret=-3, returned_on=-3)
        paid = self.record(booking, P.FULL, "5500")
        returned = payments.return_deposit(reservation_id=booking.pk, amount=D("2000"), method=M.CASH, user=self.staff)
        self.assertIn("Void the deposit return receipt first", self.refused(payments.void_payment, payment_id=paid.pk, reason="mistake", user=self.staff))
        payments.void_payment(payment_id=returned.pk, reason="wrong amount", user=self.staff)          # the return can be voided at any time
        self.assertEqual(stmt(booking).refund_available, D("2000.00"))
        payments.void_payment(payment_id=paid.pk, reason="mistake", user=self.staff)

    def test_a_payment_from_before_the_payments_page_cannot_be_voided(self):
        booking = make_booking(channel=Reservation.Channel.ONLINE)
        payments.ensure_rows(booking)
        legacy = Payment.objects.get()
        self.assertIn("before the Payments page", self.refused(payments.void_payment, payment_id=legacy.pk, reason="mistake", user=self.staff))


class OldBookingTests(Base):
    def test_a_payment_on_an_old_booking_lands_next_to_the_rows_the_old_data_stands_for(self):
        booking = make_booking(channel=Reservation.Channel.ONLINE)                      # approved online: deposit paid online
        self.record(booking, P.BALANCE, "3500")
        rows = list(Payment.objects.filter(reservation=booking).order_by("id"))
        self.assertEqual([(r.purpose, r.is_legacy, r.receipt_number is None) for r in rows], [(P.DEPOSIT, True, True), (P.BALANCE, False, False)])
        self.assertEqual(stmt(booking).balance_due, D("0.00"))

    def test_the_backfill_command_prints_a_list_and_writes_nothing_without_write(self):
        booking = make_booking(channel=Reservation.Channel.ONLINE)
        make_booking(status=Reservation.Status.PENDING)
        out = StringIO()
        call_command("backfill_payments", stdout=out)
        text = out.getvalue()
        self.assertIn(booking.reference_code, text)
        self.assertIn("dry run", text.lower())
        self.assertEqual(Payment.objects.count(), 0)

    def test_the_backfill_command_with_write_saves_the_rows_once(self):
        booking = make_booking(channel=Reservation.Channel.ONLINE)
        ReceiptRecord.objects.create(reservation=booking, photo_url="https://example.test/r.jpg", covers=ReceiptRecord.Covers.BOTH)
        walk_in = make_booking()
        ReceiptRecord.objects.create(reservation=walk_in, photo_url="https://example.test/r2.jpg", covers=ReceiptRecord.Covers.DEPOSIT)
        before = {b: ledger.statement(Reservation.objects.get(pk=b.pk)).as_dict() for b in (booking, walk_in)}
        call_command("backfill_payments", "--write", stdout=StringIO())
        self.assertEqual(Payment.objects.filter(reservation=booking).count(), 2)
        self.assertEqual(Payment.objects.filter(reservation=walk_in).count(), 1)
        self.assertEqual(Payment.objects.filter(is_legacy=True, receipt_number__isnull=False).count(), 0)
        call_command("backfill_payments", "--write", stdout=StringIO())                 # again: nothing new
        self.assertEqual(Payment.objects.count(), 3)
        for b in (booking, walk_in):                                                    # the numbers did not move: the rows ARE what was derived
            after = ledger.statement(Reservation.objects.get(pk=b.pk)).as_dict()
            for key in ("deposit_paid", "rental_paid", "balance_due", "state"):
                self.assertEqual(after[key], before[b][key], key)
            self.assertFalse(after["legacy"])


class SnapshotAndPrintTests(Base):
    def test_the_receipt_says_what_it_should_and_is_frozen(self):
        booking = make_booking(pickup=5, ret=9, customer=None)
        payment = self.record(booking, P.DEPOSIT, "2000", method=M.GCASH, reference=REF, note="walk-in")
        snap = payment.snapshot
        self.assertEqual(snap["title"], "PAYMENT RECEIPT")
        self.assertEqual((snap["number"], snap["booking"], snap["customer"], snap["served_by"]), (payment.receipt_number, booking.reference_code, "Juan Tolentino", "Maria Cruz"))
        self.assertEqual((snap["charges"], snap["amount"], snap["paid_before"], snap["balance_after"], snap["reference"]),
                         ({"rental": "3500.00", "deposit": "2000.00", "total": "5500.00"}, "2000.00", "0.00", "3500.00", "5046 933 479702"))
        self.assertEqual(snap["gowns"][0]["name"], "Wedding Gown 1")
        self.record(booking, P.BALANCE, "3500")
        payment.refresh_from_db()
        self.assertEqual(payment.snapshot["balance_after"], "3500.00")                   # a later payment never rewrites an earlier receipt

    def test_the_first_print_is_the_original_and_every_later_one_a_reprint(self):
        payment = self.record(make_booking(), P.DEPOSIT, "2000")
        self.assertEqual([payments.mark_printed(payment.pk) for _ in range(3)], [0, 1, 2])
        payment.refresh_from_db()
        self.assertEqual((payment.first_printed_at is not None, payment.reprint_count), (True, 2))

    def test_the_window_offers_only_what_the_rules_accept(self):
        booking = make_booking()
        offer = payments.choices(stmt(booking))
        self.assertEqual((offer["kind"], [(o["purpose"], o["amount"]) for o in offer["options"]]), ("deposit", [(P.DEPOSIT, "2000.00"), (P.FULL, "5500.00")]))
        self.record(booking, P.DEPOSIT, "2000")
        self.assertEqual(payments.choices(stmt(booking)), {"kind": "balance", "max": "3500.00"})
        self.record(booking, P.BALANCE, "3500")
        self.assertEqual(payments.choices(stmt(booking)), {"kind": "none"})
        self.assertEqual(payments.choices(stmt(make_booking(status=Reservation.Status.CANCELLED))), {"kind": "none"})


@override_settings(CUSTOMER_EMAILS_ENABLED=True, CUSTOMER_EMAILS_BACKGROUND=False)
class CustomerIsToldTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.customer = User.objects.create_user("pay_customer", "pay.customer@gmail.com", "x", first_name="Juan")

    def test_an_account_gets_a_bell_message_a_timeline_line_and_an_email_with_the_receipt_link(self):
        booking = make_booking(customer=self.customer)
        with self.captureOnCommitCallbacks(execute=True):
            payment = self.record(booking, P.DEPOSIT, "2000")
        message = CustomerMessage.objects.get(recipient=self.customer)
        self.assertEqual(message.category, CustomerMessage.Category.PAYMENT)
        self.assertIn(payment.receipt_number, message.body)
        self.assertIn("Balance still to pay at the shop: ₱3,500.00", message.body)
        self.assertTrue(ReservationStatusEvent.objects.filter(reservation=booking, label=payments.PAYMENT_EVENT, staff_only=False).exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(payment.receipt_number, mail.outbox[0].subject)
        self.assertIn(f"/receipts/{payment.pk}/", mail.outbox[0].body)
        payment.refresh_from_db()
        self.assertIsNotNone(payment.customer_notified_at)

    def test_a_walk_in_with_no_account_is_told_nothing_and_nothing_breaks(self):
        booking = make_booking(customer=None)
        with self.captureOnCommitCallbacks(execute=True):
            self.record(booking, P.DEPOSIT, "2000")
        self.assertEqual((CustomerMessage.objects.count(), len(mail.outbox)), (0, 0))

    def test_a_void_and_a_deposit_return_are_told_too(self):
        booking = make_booking(customer=self.customer, stage=ReservationItem.Stage.RETURNED, ret=-3, returned_on=-3)
        payment = self.record(booking, P.FULL, "5500")
        with self.captureOnCommitCallbacks(execute=True):
            payments.return_deposit(reservation_id=booking.pk, amount=D("2000"), method=M.CASH, user=self.staff)
            payments.void_payment(payment_id=Payment.objects.get(direction=Payment.Direction.OUT).pk, reason="wrong amount", user=self.staff)
        bodies = list(CustomerMessage.objects.filter(recipient=self.customer).order_by("id").values_list("body", flat=True))
        self.assertTrue(any("security deposit of ₱2,000.00" in b and "was returned" in b for b in bodies))
        self.assertTrue(any("was voided: wrong amount" in b for b in bodies))

    def test_with_customer_emails_off_only_the_bell_rings(self):
        booking = make_booking(customer=self.customer)
        with override_settings(CUSTOMER_EMAILS_ENABLED=False), self.captureOnCommitCallbacks(execute=True):
            self.record(booking, P.DEPOSIT, "2000")
        self.assertEqual((CustomerMessage.objects.count(), len(mail.outbox)), (1, 0))

    def test_sending_a_receipt_again_rings_the_bell_again(self):
        booking = make_booking(customer=self.customer)
        payment = self.record(booking, P.DEPOSIT, "2000")
        self.assertTrue(payments.send_receipt_again(payment.pk))
        self.assertEqual(CustomerMessage.objects.filter(recipient=self.customer).count(), 2)
        walk_in = self.record(make_booking(customer=None), P.DEPOSIT, "2000")
        self.assertFalse(payments.send_receipt_again(walk_in.pk))


class TwoAtOnceTests(TransactionTestCase):
    """Real threads against the real database lock: only on PostgreSQL (SQLite cannot lock a row)."""

    def test_ten_simultaneous_payments_get_ten_different_receipt_numbers(self):
        if connection.vendor != "postgresql":
            self.skipTest("needs PostgreSQL row locks")
        staff = User.objects.create_user("race_staff", password="x", is_staff=True)
        booking = make_booking()
        payments.record_payment(reservation_id=booking.pk, purpose=P.DEPOSIT, amount=D("2000"), method=M.CASH, user=staff)
        numbers, errors = [], []

        def work():
            try:
                numbers.append(payments.record_payment(reservation_id=booking.pk, purpose=P.BALANCE, amount=D("100"), method=M.CASH, user=staff).receipt_number)
            except Exception as exc:                                    # noqa: BLE001
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=work) for _ in range(10)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(len(set(numbers)), 10)
