"""When a security deposit is SETTLED (reservations/ledger.py and payments.sync_settled): only once every gown is back, nothing is left to pay and nothing is
left to hand back. The booking's history says so once, takes it back if money is moving again (a voided receipt, a new deduction), and the customer is
emailed only when no other message about it was sent."""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from reservations import ledger, payments
from reservations.models import Payment, Reservation, ReservationItem, ReservationStatusEvent

User = get_user_model()
D = Decimal
STAGE = ReservationItem.Stage
EMAILS_ON = dict(CUSTOMER_EMAILS_ENABLED=True, CUSTOMER_EMAILS_BACKGROUND=False, SITE_BASE_URL="https://arabela.example.org",
                 DEFAULT_FROM_EMAIL="Arabela <shop@gmail.com>")


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("settle_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user("settle_customer", email="settle.customer@gmail.com", password="x", first_name="Ana")
        UserProfile.objects.update_or_create(user=cls.customer, defaults={"display_name": "Ana Cruz"})

    def setUp(self):
        self.today = timezone.localdate()
        self.client.force_login(self.staff)

    def booking(self, *, stages=(STAGE.RETURNED,), deposit="2000", rental="3000", channel=Reservation.Channel.ONLINE, late_rate=200):
        """An approved booking (online: its deposit counts as paid) whose gowns are due back today."""
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Ana Cruz", status=Reservation.Status.CONFIRMED, channel=channel, reviewed_at=timezone.now(),
            rental_subtotal=D(rental), security_deposit=D(deposit), total_amount=D(rental) + D(deposit), late_fee_per_day=late_rate)
        for number, stage in enumerate(stages, start=1):
            ReservationItem.objects.create(
                reservation=reservation, gown_name=f"Settle Gown {number}", stage=stage, rental_date=self.today - timedelta(days=3), return_date=self.today,
                returned_on=self.today if stage == STAGE.RETURNED else None)
        return reservation

    def fresh(self, reservation):
        return Reservation.objects.get(pk=reservation.pk)

    def stage_of(self, reservation):
        booking = self.fresh(reservation)
        return ledger.deposit_stage(booking, ledger.statement(booking))

    def lines(self, reservation):
        return list(ReservationStatusEvent.objects.filter(reservation=reservation, label__in=(payments.SETTLED_EVENT, payments.UNSETTLED_EVENT))
                    .order_by("occurred_at", "id").values_list("label", flat=True))

    def pay_all(self, reservation):
        payments.record_payment(reservation_id=reservation.pk, purpose=Payment.Purpose.BALANCE, amount=D("3000"), method=Payment.Method.CASH, user=self.staff)
        return payments.return_deposit(reservation_id=reservation.pk, amount=D("2000"), method=Payment.Method.CASH, user=self.staff)


class DepositStageTests(Base):
    def test_the_four_words(self):
        unpaid = self.booking(stages=(STAGE.PICKUP,), channel=Reservation.Channel.WALK_IN)
        held = self.booking(stages=(STAGE.RETURNED, STAGE.RESERVED))
        due = self.booking()
        done = self.booking()
        self.pay_all(done)
        self.assertEqual([self.stage_of(b) for b in (unpaid, held, due, done)],
                         [ledger.DEPOSIT_NOT_PAID, ledger.DEPOSIT_HELD, ledger.DEPOSIT_RETURN_DUE, ledger.DEPOSIT_RETURNED])

    def test_the_day_it_was_returned_is_the_day_it_was_handed_back(self):
        reservation = self.booking()
        booking = self.fresh(reservation)
        self.assertIsNone(ledger.deposit_returned_day(booking, ledger.statement(booking)))
        self.pay_all(reservation)
        booking = self.fresh(reservation)
        self.assertEqual(ledger.deposit_returned_day(booking, ledger.statement(booking)), self.today)

    def test_a_deposit_used_up_by_fees_is_returned_on_the_day_the_last_gown_came_back(self):
        reservation = self.booking(late_rate=2000)
        item = reservation.items.get()
        ReservationItem.objects.filter(pk=item.pk).update(return_date=self.today - timedelta(days=1))        # one day late x 2,000 = the whole deposit
        booking = self.fresh(reservation)
        statement = ledger.statement(booking)
        self.assertEqual(statement.refund_available, D("0.00"))
        self.assertEqual(ledger.deposit_stage(booking, statement), ledger.DEPOSIT_RETURNED)
        self.assertEqual(ledger.deposit_returned_day(booking, statement), self.today)

    def test_settled_needs_every_gown_back_nothing_to_pay_and_nothing_to_hand_back(self):
        def settled(reservation):
            booking = self.fresh(reservation)
            return ledger.is_settled(booking, ledger.statement(booking))

        out = self.booking(stages=(STAGE.RESERVED,))
        owes = self.booking()
        self.assertFalse(settled(out))
        self.assertFalse(settled(owes))                                      # the rental is unpaid and the deposit still held
        self.pay_all(owes)
        self.assertTrue(settled(owes))
        nothing = self.booking(deposit="0", rental="0")
        self.assertTrue(settled(nothing))
        cancelled = self.booking(deposit="0", rental="0")
        Reservation.objects.filter(pk=cancelled.pk).update(status=Reservation.Status.CANCELLED)
        self.assertFalse(settled(cancelled))
        pending = self.booking(deposit="0", rental="0")
        Reservation.objects.filter(pk=pending.pk).update(status=Reservation.Status.PENDING, reviewed_at=None)
        self.assertFalse(settled(pending))


class SyncTests(Base):
    def test_the_line_is_written_once(self):
        reservation = self.booking(deposit="0", rental="0")
        for _ in range(3):
            payments.sync_settled(reservation.pk)
        self.assertEqual(self.lines(reservation), [payments.SETTLED_EVENT])
        line = ReservationStatusEvent.objects.get(reservation=reservation, label=payments.SETTLED_EVENT)
        self.assertEqual((line.detail, line.staff_only, line.actor), (payments.SETTLED_DETAIL, False, "Staff"))

    def test_nothing_is_written_while_money_is_still_to_move(self):
        reservation = self.booking()
        payments.sync_settled(reservation.pk)
        self.assertEqual(self.lines(reservation), [])

    def test_an_older_booking_marked_settled_at_check_in_gets_no_confusing_line_when_it_is_paid(self):
        reservation = self.booking()
        ReservationStatusEvent.record(reservation, payments.SETTLED_EVENT, detail=payments.SETTLED_DETAIL, actor="Staff")     # how it used to be written
        payments.record_payment(reservation_id=reservation.pk, purpose=Payment.Purpose.BALANCE, amount=D("3000"), method=Payment.Method.CASH, user=self.staff)
        self.assertEqual(self.lines(reservation), [payments.SETTLED_EVENT])
        payments.return_deposit(reservation_id=reservation.pk, amount=D("2000"), method=Payment.Method.CASH, user=self.staff)
        self.assertEqual(self.lines(reservation), [payments.SETTLED_EVENT])

    def test_a_voided_deposit_return_takes_the_line_back_and_handing_it_back_again_settles_it_again(self):
        reservation = self.booking()
        with override_settings(**EMAILS_ON), self.captureOnCommitCallbacks(execute=True):
            receipt = self.pay_all(reservation)
        self.assertEqual(self.lines(reservation), [payments.SETTLED_EVENT])
        mail.outbox.clear()
        with override_settings(**EMAILS_ON), self.captureOnCommitCallbacks(execute=True):
            payments.void_payment(payment_id=receipt.pk, reason="wrong amount typed", user=self.staff)
        self.assertEqual(self.lines(reservation), [payments.SETTLED_EVENT, payments.UNSETTLED_EVENT])
        self.assertEqual([m.subject for m in mail.outbox], [f"Receipt {receipt.receipt_number} was voided"])       # one message: the void
        payments.return_deposit(reservation_id=reservation.pk, amount=D("2000"), method=Payment.Method.CASH, user=self.staff)
        self.assertEqual(self.lines(reservation), [payments.SETTLED_EVENT, payments.UNSETTLED_EVENT, payments.SETTLED_EVENT])
        self.assertEqual(self.stage_of(reservation), ledger.DEPOSIT_RETURNED)

    def test_a_new_deduction_beyond_the_deposit_opens_it_again_until_it_is_paid(self):
        reservation = self.booking(late_rate=2000)
        ReservationItem.objects.filter(reservation=reservation).update(return_date=self.today - timedelta(days=1))
        payments.record_payment(reservation_id=reservation.pk, purpose=Payment.Purpose.BALANCE, amount=D("3000"), method=Payment.Method.CASH, user=self.staff)
        self.assertEqual(self.lines(reservation), [payments.SETTLED_EVENT])          # the fee used the whole deposit and the rental is paid
        payments.add_deduction(reservation_id=reservation.pk, amount=D("500"), reason="torn lace", user=self.staff)
        self.assertEqual(self.lines(reservation), [payments.SETTLED_EVENT, payments.UNSETTLED_EVENT])
        payments.record_payment(reservation_id=reservation.pk, purpose=Payment.Purpose.EXTRA, amount=D("500"), method=Payment.Method.CASH, user=self.staff)
        self.assertEqual(self.lines(reservation), [payments.SETTLED_EVENT, payments.UNSETTLED_EVENT, payments.SETTLED_EVENT])

    def test_removing_a_deduction_hands_money_back_and_opens_it_again(self):
        reservation = self.booking(late_rate=0)
        payments.record_payment(reservation_id=reservation.pk, purpose=Payment.Purpose.BALANCE, amount=D("3000"), method=Payment.Method.CASH, user=self.staff)
        deduction = payments.add_deduction(reservation_id=reservation.pk, amount=D("2000"), reason="lost veil", user=self.staff)
        self.assertEqual(self.lines(reservation), [payments.SETTLED_EVENT])          # the deduction used the whole deposit
        payments.void_deduction(deduction_id=deduction.pk, reason="found the veil", user=self.staff)
        self.assertEqual(self.lines(reservation), [payments.SETTLED_EVENT, payments.UNSETTLED_EVENT])
        self.assertEqual(self.stage_of(reservation), ledger.DEPOSIT_RETURN_DUE)

    def test_a_settled_line_written_by_a_payment_sends_no_email_of_its_own(self):
        reservation = self.booking()
        with override_settings(**EMAILS_ON), self.captureOnCommitCallbacks(execute=True):
            self.pay_all(reservation)
        self.assertEqual(self.lines(reservation), [payments.SETTLED_EVENT])
        self.assertNotIn(f"Your security deposit is settled - {reservation.reference_code}", [m.subject for m in mail.outbox])
        self.assertEqual(len(mail.outbox), 2)                                          # the two receipts

    def test_settled_at_check_in_sends_the_email(self):
        reservation = self.booking(stages=(STAGE.RESERVED,), deposit="0", rental="0")
        item = reservation.items.get()
        with override_settings(**EMAILS_ON), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("arabela_admin:reservation_item_mark_returned", args=[item.pk]),
                                        data=json.dumps({"condition": "Good"}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([m.subject for m in mail.outbox], [f"Your security deposit is settled - {reservation.reference_code}"])

    def test_a_problem_writing_the_line_never_undoes_the_check_in(self):
        reservation = self.booking(stages=(STAGE.RESERVED,), deposit="0", rental="0")
        item = reservation.items.get()
        with patch("reservations.ledger.statement", side_effect=RuntimeError("boom")), self.assertLogs("reservations.payments", level="ERROR"):
            payments.sync_settled(reservation.pk)
        with patch("reservations.payments.ledger.is_settled", side_effect=RuntimeError("boom")), self.assertLogs("reservations.payments", level="ERROR"):
            response = self.client.post(reverse("arabela_admin:reservation_item_mark_returned", args=[item.pk]),
                                        data=json.dumps({"condition": "Good"}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        item.refresh_from_db()
        self.assertEqual(item.stage, STAGE.RETURNED)
        self.assertEqual(self.lines(reservation), [])

    def test_a_booking_that_is_gone_is_ignored(self):
        payments.sync_settled(999999)


class CustomerPageTests(Base):
    """The customer's booking page says the deposit is settled only when it is."""

    def page(self, reservation):
        self.client.force_login(self.customer)
        item = reservation.items.order_by("id").first()
        response = self.client.get(reverse("gowns:reservation_item_detail", args=[item.pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_words_follow_the_money(self):
        reservation = self.booking()
        html = self.page(reservation)
        self.assertIn("Your security deposit of PHP 2,000.00 will be handed back", html)
        self.assertNotIn("Security deposit settled", html)
        self.pay_all(reservation)
        self.assertIn(f"Security deposit settled {self.today:%b %d, %Y}", self.page(reservation))

    def test_a_balance_with_nothing_to_hand_back_is_still_to_pay(self):
        reservation = self.booking(channel=Reservation.Channel.WALK_IN)
        html = self.page(reservation)
        self.assertIn("PHP 5,000.00 is still to pay at the shop", html)
        self.assertNotIn("Security deposit settled", html)

    def test_while_a_gown_is_out_it_waits_for_every_gown(self):
        reservation = self.booking(stages=(STAGE.RETURNED, STAGE.RESERVED))
        self.assertIn("Security deposit settled once every gown is returned", self.page(reservation))
