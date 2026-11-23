"""The money rules of a booking (reservations/ledger.py): balance, late fee, deductions, the deposit that can be handed back, and how an old
booking with no Payment rows is read. Pure arithmetic over saved rows -- no page involved.

Numbers used throughout: a ₱3,500 gown rental + a ₱2,000 deposit = ₱5,500 to pay; the late fee is ₱200 a day for each gown."""
import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from reservations import ledger
from reservations.models import Deduction, Payment, ReceiptRecord, Reservation, ReservationItem

User = get_user_model()
D = Decimal
IN, OUT = Payment.Direction.IN, Payment.Direction.OUT
P = Payment.Purpose


def make_booking(*, rental="3500", deposit="2000", gowns=1, status=Reservation.Status.CONFIRMED, channel=Reservation.Channel.WALK_IN,
                 rate=200, stage=ReservationItem.Stage.PICKUP, pickup=-6, ret=-2, returned_on=None, original_return=None, customer=None, **extra):
    """A booking with `gowns` gowns (the rental is split between them), dates as day offsets from today."""
    today = timezone.localdate()
    reservation = Reservation.objects.create(
        customer=customer, customer_name="Juan Tolentino", phone="09171234567", status=status, channel=channel, late_fee_per_day=rate,
        rental_subtotal=D(rental), security_deposit=D(deposit), total_amount=D(rental) + D(deposit), reviewed_at=timezone.now(), **extra)
    for n in range(gowns):
        ReservationItem.objects.create(
            reservation=reservation, gown_name=f"Wedding Gown {n + 1}", size="Large", rental_price=D(rental) / gowns,
            rental_date=today + timedelta(days=pickup), return_date=today + timedelta(days=ret),
            original_return_date=today + timedelta(days=original_return if original_return is not None else ret), stage=stage,
            picked_up_on=today + timedelta(days=pickup) if stage != ReservationItem.Stage.PICKUP else None,
            returned_on=(today + timedelta(days=returned_on)) if returned_on is not None else None)
    return reservation


def pay(reservation, purpose, amount, *, direction=IN, status=Payment.Status.RECORDED, method=Payment.Method.CASH):
    return Payment.objects.create(reservation=reservation, direction=direction, purpose=purpose, method=method, amount=D(amount), status=status,
                                  receipt_number=f"PR-TEST-{Payment.objects.count() + 1:06d}")


def stmt(reservation, **kw):
    return ledger.statement(Reservation.objects.get(pk=reservation.pk), **kw)


class BalanceTests(TestCase):
    def test_nothing_paid_yet_owes_the_rental_and_the_deposit(self):
        s = stmt(make_booking())
        self.assertEqual((s.total, s.received, s.balance_due, s.state), (D("5500.00"), D("0.00"), D("5500.00"), ledger.BALANCE))

    def test_deposit_only_leaves_the_rental_to_pay(self):
        booking = make_booking()
        pay(booking, P.DEPOSIT, "2000")
        s = stmt(booking)
        self.assertEqual((s.deposit_paid, s.rental_paid, s.balance_due, s.state), (D("2000.00"), D("0.00"), D("3500.00"), ledger.BALANCE))

    def test_paid_in_full_owes_nothing(self):
        booking = make_booking()
        pay(booking, P.FULL, "5500")
        s = stmt(booking)
        self.assertEqual((s.balance_due, s.state, s.deposit_paid, s.rental_paid), (D("0.00"), ledger.PAID, D("2000.00"), D("3500.00")))

    def test_the_rental_balance_can_be_paid_in_parts(self):
        booking = make_booking()
        pay(booking, P.DEPOSIT, "2000")
        pay(booking, P.BALANCE, "2000")
        s = stmt(booking)
        self.assertEqual((s.rental_paid, s.balance_due), (D("2000.00"), D("1500.00")))
        pay(booking, P.BALANCE, "1500")
        self.assertEqual(stmt(booking).balance_due, D("0.00"))

    def test_money_covers_the_deposit_first_then_the_rental(self):
        booking = make_booking()
        pay(booking, P.BALANCE, "2500")                        # more than the deposit alone
        s = stmt(booking)
        self.assertEqual((s.deposit_paid, s.rental_paid, s.deposit_unpaid, s.rental_unpaid), (D("2000.00"), D("500.00"), D("0.00"), D("3000.00")))

    def test_a_voided_payment_counts_for_nothing(self):
        booking = make_booking()
        pay(booking, P.DEPOSIT, "2000")
        pay(booking, P.BALANCE, "3500", status=Payment.Status.VOID)
        self.assertEqual(stmt(booking).balance_due, D("3500.00"))

    def test_a_booking_that_is_not_running_asks_for_nothing(self):
        for status in (Reservation.Status.PENDING, Reservation.Status.REJECTED):
            with self.subTest(status=status):
                s = stmt(make_booking(status=status))
                self.assertEqual((s.tracked, s.balance_due), (False, D("0.00")))

    def test_a_cancelled_booking_keeps_its_deposit_and_owes_nothing(self):
        booking = make_booking(status=Reservation.Status.CANCELLED)
        pay(booking, P.DEPOSIT, "2000")
        s = stmt(booking)
        self.assertEqual((s.state, s.balance_due, s.refund_available), (ledger.CANCELLED, D("0.00"), D("0.00")))

    def test_the_rental_follows_the_total_when_the_saved_rental_does_not_add_up(self):
        booking = make_booking()
        Reservation.objects.filter(pk=booking.pk).update(rental_subtotal=D("0"))             # an odd old row: total 5,500, rental 0
        self.assertEqual(stmt(booking).rental, D("3500.00"))

    def test_the_statement_is_plain_data_for_the_page(self):
        booking = make_booking(stage=ReservationItem.Stage.OVERDUE, ret=-3)
        pay(booking, P.DEPOSIT, "2000")
        data = json.loads(json.dumps(stmt(booking).as_dict()))
        self.assertEqual((data["balance_due"], data["late_fee"], data["late_lines"][0]["days"]), ("3500.00", "600.00", 3))


class LateFeeTests(TestCase):
    def test_a_gown_not_picked_up_is_never_late(self):
        self.assertEqual(stmt(make_booking(stage=ReservationItem.Stage.PICKUP, ret=-5)).late_fee, D("0.00"))

    def test_a_gown_still_out_counts_the_days_so_far_per_day_late(self):
        s = stmt(make_booking(stage=ReservationItem.Stage.OVERDUE, ret=-3))
        self.assertEqual((s.late_days_total, s.late_fee, s.late_now), (3, D("600.00"), True))

    def test_a_gown_back_on_time_costs_nothing(self):
        s = stmt(make_booking(stage=ReservationItem.Stage.RETURNED, ret=-3, returned_on=-3))
        self.assertEqual((s.late_fee, s.late_now), (D("0.00"), False))

    def test_a_gown_back_late_counts_up_to_the_day_it_came_back(self):
        s = stmt(make_booking(stage=ReservationItem.Stage.RETURNED, ret=-6, returned_on=-3))
        self.assertEqual((s.late_days_total, s.late_fee), (3, D("600.00")))

    def test_the_fee_is_per_gown(self):
        s = stmt(make_booking(gowns=2, stage=ReservationItem.Stage.OVERDUE, ret=-2))
        self.assertEqual((s.late_days_total, s.late_fee, len(s.late_lines)), (4, D("800.00"), 2))

    def test_moving_the_return_date_later_does_not_make_the_fee_disappear(self):
        # booked to return 3 days ago; the staff moved the return date to tomorrow for the customer: the 3 days still cost
        booking = make_booking(stage=ReservationItem.Stage.RESERVED, ret=1, original_return=-3)
        self.assertEqual(stmt(booking).late_fee, D("600.00"))

    def test_the_rate_is_the_one_the_booking_was_made_with(self):
        self.assertEqual(stmt(make_booking(stage=ReservationItem.Stage.OVERDUE, ret=-2, rate=500)).late_fee, D("1000.00"))
        self.assertEqual(stmt(make_booking(stage=ReservationItem.Stage.OVERDUE, ret=-2, rate=0)).late_fee, D("0.00"))

    def test_a_gown_the_shop_cancelled_is_not_charged(self):
        booking = make_booking(gowns=2, stage=ReservationItem.Stage.OVERDUE, ret=-2)
        ReservationItem.objects.filter(pk=booking.items.first().pk).update(stage=ReservationItem.Stage.CANCELLED)
        self.assertEqual(stmt(booking).late_fee, D("400.00"))

    def test_it_agrees_with_the_overdue_days_active_reservations_shows_when_nothing_was_moved(self):
        from arabela_admin.views import _days_late
        today = timezone.localdate()
        for stage, ret, back in ((ReservationItem.Stage.OVERDUE, -4, None), (ReservationItem.Stage.RETURNED, -6, -2), (ReservationItem.Stage.RETURNED, -2, -2),
                                 (ReservationItem.Stage.RESERVED, 3, None), (ReservationItem.Stage.PICKUP, -4, None)):
            with self.subTest(stage=stage, ret=ret):
                item = ReservationItem.objects.get(pk=make_booking(stage=stage, ret=ret, returned_on=back).items.first().pk)
                self.assertEqual(ledger.late_days(item, today), _days_late(item, today) or 0)


class DepositAndRefundTests(TestCase):
    def returned_late(self, days=3, deposit_paid=True):
        booking = make_booking(stage=ReservationItem.Stage.RETURNED, ret=-6, returned_on=-6 + days)
        if deposit_paid:
            pay(booking, P.FULL, "5500")
        return booking

    def test_the_late_fee_comes_out_of_the_deposit(self):
        s = stmt(self.returned_late(3))
        self.assertEqual((s.late_fee, s.deposit_used, s.refundable_total, s.refund_available, s.state), (D("600.00"), D("600.00"), D("1400.00"), D("1400.00"), ledger.REFUND))

    def test_a_gown_back_on_time_gives_the_whole_deposit_back(self):
        s = stmt(self.returned_late(0))
        self.assertEqual((s.refund_available, s.state), (D("2000.00"), ledger.REFUND))

    def test_nothing_is_refundable_while_a_gown_is_still_out(self):
        booking = make_booking(stage=ReservationItem.Stage.OVERDUE, ret=-3)
        pay(booking, P.FULL, "5500")
        s = stmt(booking)
        self.assertEqual((s.all_returned, s.refund_available, s.state), (False, D("0.00"), ledger.PAID))

    def test_a_deduction_makes_the_refund_smaller(self):
        booking = self.returned_late(0)
        Deduction.objects.create(reservation=booking, amount=D("500"), reason="Stain on the skirt")
        s = stmt(booking)
        self.assertEqual((s.deductions_total, s.refundable_total), (D("500.00"), D("1500.00")))

    def test_a_removed_deduction_counts_for_nothing(self):
        booking = self.returned_late(0)
        Deduction.objects.create(reservation=booking, amount=D("500"), reason="x", status=Deduction.Status.VOID)
        self.assertEqual(stmt(booking).refundable_total, D("2000.00"))

    def test_fees_bigger_than_the_deposit_leave_an_extra_amount_owed(self):
        booking = self.returned_late(days=12)                 # 12 x 200 = 2,400 against a 2,000 deposit
        s = stmt(booking)
        self.assertEqual((s.late_fee, s.refundable_total, s.overflow, s.extra_owed, s.balance_due, s.state), (D("2400.00"), D("0.00"), D("400.00"), D("400.00"), D("400.00"), ledger.BALANCE))
        pay(booking, P.EXTRA, "400")
        s = stmt(booking)
        self.assertEqual((s.extra_owed, s.balance_due, s.state), (D("0.00"), D("0.00"), ledger.SETTLED))

    def test_handing_the_deposit_back_settles_the_booking(self):
        booking = self.returned_late(3)
        pay(booking, P.DEPOSIT_RETURN, "1400", direction=OUT)
        s = stmt(booking)
        self.assertEqual((s.refunded, s.refund_available, s.state), (D("1400.00"), D("0.00"), ledger.SETTLED))

    def test_a_part_handed_back_leaves_the_rest_refundable(self):
        booking = self.returned_late(0)
        pay(booking, P.DEPOSIT_RETURN, "500", direction=OUT)
        self.assertEqual(stmt(booking).refund_available, D("1500.00"))

    def test_a_deposit_that_was_never_paid_has_nothing_to_give_back(self):
        s = stmt(self.returned_late(0, deposit_paid=False))
        self.assertEqual((s.refund_available, s.state), (D("0.00"), ledger.BALANCE))


class OldBookingTests(TestCase):
    """A booking with no Payment rows is read from what it already carries (until the backfill saves those rows)."""

    def receipt(self, booking, covers):
        return ReceiptRecord.objects.create(reservation=booking, photo_url="https://example.test/r.jpg", covers=covers)

    def test_an_approved_online_booking_paid_its_deposit_online(self):
        s = stmt(make_booking(channel=Reservation.Channel.ONLINE))
        self.assertEqual((s.legacy, s.deposit_paid, s.rental_paid, s.balance_due), (True, D("2000.00"), D("0.00"), D("3500.00")))

    def test_an_online_booking_with_a_shop_receipt_also_paid_the_rental(self):
        booking = make_booking(channel=Reservation.Channel.ONLINE)
        self.receipt(booking, ReceiptRecord.Covers.BOTH)
        self.assertEqual(stmt(booking).balance_due, D("0.00"))

    def test_a_walk_in_paid_in_full_by_its_receipt(self):
        booking = make_booking()
        self.receipt(booking, ReceiptRecord.Covers.BOTH)
        self.assertEqual((stmt(booking).balance_due, stmt(booking).state), (D("0.00"), ledger.PAID))

    def test_a_walk_in_deposit_only_by_its_receipt(self):
        booking = make_booking()
        self.receipt(booking, ReceiptRecord.Covers.DEPOSIT)
        s = stmt(booking)
        self.assertEqual((s.deposit_paid, s.rental_paid, s.balance_due), (D("2000.00"), D("0.00"), D("3500.00")))

    def test_a_walk_in_with_a_deposit_and_a_rental_receipt_is_paid_in_full(self):
        booking = make_booking()
        self.receipt(booking, ReceiptRecord.Covers.DEPOSIT)
        self.receipt(booking, ReceiptRecord.Covers.RENTAL)
        self.assertEqual(stmt(booking).balance_due, D("0.00"))

    def test_a_walk_in_with_no_labelled_receipt_has_paid_nothing_the_system_knows_of(self):
        booking = make_booking()
        self.receipt(booking, "")
        self.assertEqual(stmt(booking).balance_due, D("5500.00"))

    def test_once_rows_exist_the_old_reading_is_not_used_any_more(self):
        booking = make_booking(channel=Reservation.Channel.ONLINE)
        pay(booking, P.DEPOSIT, "2000", status=Payment.Status.VOID)          # every row voided: it is NOT "no rows"
        s = stmt(booking)
        self.assertEqual((s.legacy, s.deposit_paid), (False, D("0.00")))
