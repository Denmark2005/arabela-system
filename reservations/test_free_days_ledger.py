"""The money of a booking made under the free-days rule (reservations/ledger.py): 2 days before the event and 2 days after it are free, every
BOOKED day outside them costs the booking's fee per day (₱200) per gown from the deposit, a late return costs it on top, and staff changing the
saved dates moves the fee with them.

Numbers used throughout: event Dec 15 (free: Dec 13 - Dec 17), ₱3,500 rental, ₱2,000 deposit, ₱200 per day per gown."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from reservations import ledger
from reservations.models import Deduction, Payment, Reservation, ReservationItem
from reservations.test_ledger import pay

User = get_user_model()
D = Decimal
P = Payment.Purpose
OUT = Payment.Direction.OUT
S = ReservationItem.Stage

EVENT = date(2026, 12, 15)


def dec(day):
    return date(2026, 12, day) if day > 0 else date(2026, 11, 30) + timedelta(days=day)


def make(*gowns, deposit="2000", rental="3500", rate=200, paid="2000", status=Reservation.Status.CONFIRMED):
    """A free-days booking. Each gown is a dict: pickup/ret (day of December), and optionally booked (original return), stage, picked_up, returned."""
    gowns = gowns or ({"pickup": 13, "ret": 17},)
    booking = Reservation.objects.create(
        customer_name="Maria Cruz", phone="09171234567", status=status, channel=Reservation.Channel.WALK_IN, late_fee_per_day=rate,
        rental_subtotal=D(rental), security_deposit=D(deposit), total_amount=D(rental) + D(deposit))
    for n, spec in enumerate(gowns):
        old = spec.get("old", False)
        ReservationItem.objects.create(
            reservation=booking, gown_name=f"Gown {n + 1}", size="Medium", rental_price=D(rental) / len(gowns),
            rental_date=dec(spec["pickup"]), return_date=dec(spec["ret"]), event_date=spec.get("event", EVENT),
            original_rental_date=dec(spec["pickup"]), original_return_date=dec(spec.get("booked", spec["ret"])),
            free_pickup_date=None if old else EVENT - timedelta(days=2), free_return_date=None if old else EVENT + timedelta(days=2),
            stage=spec.get("stage", S.PICKUP), picked_up_on=dec(spec["picked_up"]) if "picked_up" in spec else None,
            returned_on=dec(spec["returned"]) if "returned" in spec else None)
    if paid:
        pay(booking, P.DEPOSIT, paid)
    return booking


def stmt(booking, today=dec(1)):
    return ledger.statement(Reservation.objects.get(pk=booking.pk), today=today)


class FreeDaysTableTests(TestCase):
    """The agreed examples (event Dec 15)."""

    def check(self, gown, *, early, later, change=0, overdue=0, back=None, today=dec(1)):
        booking = make(gown)
        s = stmt(booking, today=today)
        got = (s.early_days_total, s.later_days_total, s.change_days_total, s.overdue_days_total)
        self.assertEqual(got, (early, later, change, overdue), gown)
        total = D(200) * (early + later + change + overdue)
        self.assertEqual((s.late_fee, s.total_deduction), (total, total), gown)
        if back is not None:
            self.assertEqual(s.refundable_total, D(back), gown)
        return s

    def test_the_free_days_cost_nothing(self):
        self.check({"pickup": 13, "ret": 17}, early=0, later=0, back="2000")
        self.check({"pickup": 14, "ret": 17}, early=0, later=0, back="2000")
        self.check({"pickup": 15, "ret": 17}, early=0, later=0, back="2000")

    def test_two_days_early_and_two_days_later_is_800_and_1200_comes_back(self):
        s = self.check({"pickup": 11, "ret": 19}, early=2, later=2, back="1200")
        self.assertEqual((s.early_fee, s.later_fee, s.extra_days_fee), (D("400.00"), D("400.00"), D("800.00")))
        kinds = [(line["kind"], line["label"], line["days"], line["amount"]) for line in s.fee_lines]
        self.assertEqual(kinds, [("early", "Early pick-up fee", 2, D("400.00")), ("later", "Later return fee", 2, D("400.00"))])
        self.assertEqual((s.fee_lines[0]["free_from"], s.fee_lines[1]["free_until"]), ("Dec 13, 2026", "Dec 17, 2026"))
        self.assertEqual((s.deposit_paid, s.deposit_used, s.balance_due, s.extra_owed), (D("2000.00"), D("800.00"), D("3500.00"), D("0.00")))
        self.assertFalse(s.late_now)                  # booked days do not grow
        self.assertFalse(s.overdue_now)

    def test_picking_up_later_or_returning_earlier_than_booked_still_costs_the_booked_days(self):
        self.check({"pickup": 11, "ret": 19, "stage": S.RETURNED, "picked_up": 12, "returned": 17}, early=2, later=2, back="1200", today=dec(20))
        self.check({"pickup": 11, "ret": 19, "stage": S.RETURNED, "picked_up": 13, "returned": 15}, early=2, later=2, back="1200", today=dec(20))

    def test_only_one_side_outside_the_free_days_costs_only_that_side(self):
        self.check({"pickup": 11, "ret": 16}, early=2, later=0, back="1600")
        self.check({"pickup": 13, "ret": 19}, early=0, later=2, back="1600")

    def test_staff_moving_the_return_back_takes_the_fee_back_down(self):
        self.check({"pickup": 11, "ret": 17, "booked": 19}, early=2, later=0, back="1600")
        self.check({"pickup": 11, "ret": 18, "booked": 19}, early=2, later=1, back="1400")

    def test_staff_moving_the_pick_up_to_the_free_day_takes_the_early_fee_away(self):
        self.check({"pickup": 13, "ret": 17, "booked": 19}, early=0, later=0, back="2000")

    def test_staff_moving_the_return_past_the_booked_one_is_a_date_change_fee(self):
        self.check({"pickup": 13, "ret": 19, "booked": 17}, early=0, later=0, change=2, back="1600")
        self.check({"pickup": 11, "ret": 21, "booked": 19}, early=2, later=2, change=2, back="800")
        self.check({"pickup": 13, "ret": 21, "booked": 19}, early=0, later=2, change=2, back="1200")

    def test_bringing_it_back_after_the_saved_date_is_overdue_on_top(self):
        s = self.check({"pickup": 13, "ret": 19, "stage": S.RETURNED, "picked_up": 13, "returned": 20}, early=0, later=2, overdue=1, back="1400",
                       today=dec(21))
        self.assertEqual([line["kind"] for line in s.fee_lines], ["later", "overdue"])

    def test_a_gown_still_out_past_its_date_keeps_counting(self):
        gown = {"pickup": 13, "ret": 19, "stage": S.OVERDUE, "picked_up": 13}
        s = stmt(make(gown), today=dec(22))
        self.assertEqual((s.overdue_days_total, s.late_fee, s.late_now, s.overdue_now), (3, D("1000.00"), True, True))
        s = stmt(make({**gown, "stage": S.RETURN}), today=dec(19))              # out, due today: not overdue, nothing growing
        self.assertEqual((s.overdue_days_total, s.late_fee, s.late_now), (0, D("400.00"), False))

    def test_a_damage_deduction_adds_to_the_date_fees(self):
        booking = make({"pickup": 11, "ret": 19, "stage": S.RETURNED, "picked_up": 11, "returned": 19})
        Deduction.objects.create(reservation=booking, amount=D("300"), reason="Torn hem")
        s = stmt(booking, today=dec(20))
        self.assertEqual((s.total_deduction, s.refundable_total, s.refund_available), (D("1100.00"), D("900.00"), D("900.00")))

    def test_more_days_than_the_deposit_covers_leaves_the_rest_to_pay(self):
        booking = make({"pickup": 1, "ret": 17, "stage": S.RETURNED, "picked_up": 1, "returned": 17})
        s = stmt(booking, today=dec(18))
        self.assertEqual((s.early_days_total, s.late_fee), (12, D("2400.00")))
        self.assertEqual((s.refundable_total, s.refund_available, s.extra_owed, s.overflow), (D("0.00"), D("0.00"), D("400.00"), D("400.00")))
        pay(booking, P.EXTRA, "400")
        s = stmt(booking, today=dec(18))
        self.assertEqual((s.extra_owed, s.balance_due), (D("0.00"), D("3500.00")))


class CancelledAndRejectedBookingsChargeNothingTests(TestCase):
    """The Terms: a cancelled booking keeps its deposit and the extra days fee is not charged on top of it."""

    def test_no_fee_line_deduction_or_extra_amount_once_cancelled_or_rejected(self):
        for status in (Reservation.Status.CANCELLED, Reservation.Status.REJECTED):
            with self.subTest(status=status):
                booking = make({"pickup": 1, "ret": 19}, status=status)             # 12 early + 2 later days = 2,800 if it were running
                Reservation.objects.filter(pk=booking.pk).update(reviewed_at="2026-12-01T10:00:00+08:00")
                s = stmt(booking)
                self.assertEqual((s.late_fee, s.fee_lines, s.late_lines, s.total_deduction, s.extra_owed, s.balance_due),
                                 (D("0.00"), [], [], D("0.00"), D("0.00"), D("0.00")))
                item = ReservationItem.objects.select_related("reservation").get(reservation=booking)
                self.assertEqual((item.extra_days_count, item.extra_days_fee), (0, D("0")))

    def test_a_customer_cancelling_on_the_website_sees_no_extra_days_anywhere(self):
        customer = User.objects.create_user("fd_canceller", password="x")
        booking = make({"pickup": 20, "ret": 29, "event": date(2026, 12, 24)}, status=Reservation.Status.PENDING, paid="")
        ReservationItem.objects.filter(reservation=booking).update(free_pickup_date=date(2026, 12, 22), free_return_date=date(2026, 12, 26))
        Reservation.objects.filter(pk=booking.pk).update(customer=customer)
        item = booking.items.get()
        self.client.force_login(customer)
        before = self.client.get(reverse("gowns:reservation_item_detail", args=[item.pk])).content.decode()
        self.assertIn("off your security deposit", before)
        self.assertEqual(self.client.post(reverse("gowns:reservation_item_cancel", args=[item.pk])).status_code, 200)
        after = self.client.get(reverse("gowns:reservation_item_detail", args=[item.pk])).content.decode()
        self.assertNotIn("off your security deposit", after)
        self.assertNotIn("Extra days", after)


class FreeDaysMoneyTests(TestCase):
    def test_the_whole_story_ends_settled_with_1200_handed_back(self):
        booking = make({"pickup": 11, "ret": 19, "stage": S.RETURNED, "picked_up": 11, "returned": 19}, paid="")
        pay(booking, P.FULL, "5500")
        s = stmt(booking, today=dec(20))
        self.assertEqual((s.refund_available, s.state, s.balance_due), (D("1200.00"), ledger.REFUND, D("0.00")))
        self.assertEqual(ledger.deposit_stage(Reservation.objects.get(pk=booking.pk), s), ledger.DEPOSIT_RETURN_DUE)
        pay(booking, P.DEPOSIT_RETURN, "1200", direction=OUT)
        s = stmt(booking, today=dec(20))
        self.assertEqual((s.refund_available, s.state), (D("0.00"), ledger.SETTLED))
        self.assertEqual(ledger.deposit_stage(Reservation.objects.get(pk=booking.pk), s), ledger.DEPOSIT_RETURNED)
        self.assertTrue(ledger.is_settled(Reservation.objects.get(pk=booking.pk), s))

    def test_booked_days_on_an_unpaid_deposit_are_not_asked_for_twice(self):
        booking = make({"pickup": 11, "ret": 19}, paid="")                     # a walk-in who has paid nothing yet
        s = stmt(booking)
        self.assertEqual((s.late_fee, s.extra_owed, s.balance_due), (D("800.00"), D("0.00"), D("5500.00")))
        pay(booking, P.DEPOSIT, "2000")
        s = stmt(booking)
        self.assertEqual((s.extra_owed, s.balance_due, s.refundable_total), (D("0.00"), D("3500.00"), D("1200.00")))

    def test_an_unpaid_deposit_still_owes_what_goes_past_it(self):
        s = stmt(make({"pickup": 1, "ret": 17}, paid=""))                     # 2,400 of days against a 2,000 deposit
        self.assertEqual((s.extra_owed, s.balance_due), (D("400.00"), D("5900.00")))

    def test_every_gown_has_its_own_days(self):
        booking = make({"pickup": 11, "ret": 19}, {"pickup": 13, "ret": 17}, {"pickup": 12, "ret": 18}, deposit="6000", paid="6000")
        s = stmt(booking)
        self.assertEqual((s.early_days_total, s.later_days_total, s.late_fee), (3, 3, D("1200.00")))
        self.assertEqual([line["gown"] for line in s.late_lines], ["Gown 1", "Gown 3"])
        self.assertEqual(s.refundable_total, D("4800.00"))

    def test_a_cancelled_gown_costs_nothing(self):
        booking = make({"pickup": 11, "ret": 19, "stage": S.CANCELLED}, {"pickup": 12, "ret": 17}, deposit="2000")
        s = stmt(booking)
        self.assertEqual((s.late_fee, [line["gown"] for line in s.late_lines]), (D("200.00"), ["Gown 2"]))

    def test_a_cancelled_booking_asks_for_nothing(self):
        booking = make({"pickup": 11, "ret": 19}, status=Reservation.Status.CANCELLED)
        Reservation.objects.filter(pk=booking.pk).update(reviewed_at="2026-12-01T10:00:00+08:00")
        s = stmt(booking)
        self.assertEqual((s.state, s.balance_due, s.refund_available), (ledger.CANCELLED, D("0.00"), D("0.00")))

    def test_a_booking_made_with_no_fee_charges_nothing(self):
        s = stmt(make({"pickup": 11, "ret": 19}, rate=0))
        self.assertEqual((s.late_fee, s.fee_lines), (D("0.00"), []))

    def test_the_booking_keeps_the_fee_it_was_made_with(self):
        s = stmt(make({"pickup": 11, "ret": 19}, rate=350))
        self.assertEqual((s.late_rate, s.late_fee), (D("350"), D("1400.00")))

    def test_the_item_says_how_many_extra_days_and_money_it_has(self):
        item = make({"pickup": 11, "ret": 19}).items.get()
        self.assertEqual((item.uses_free_days, item.extra_days_count, item.extra_days_fee), (True, 4, D("800")))

    def test_the_customers_planned_return_shows_only_while_it_still_fits_the_dates(self):
        item = make({"pickup": 13, "ret": 17}).items.get()
        item.planned_return_date = dec(16)                                   # the customer said: the day after the event
        self.assertEqual(item.shown_return_date, dec(16))
        item.return_date = dec(20)                                           # staff moved it later: the old plan is not shown
        self.assertEqual(item.shown_return_date, dec(20))
        item.return_date = dec(17)
        item.event_date = dec(16)                                            # or the event moved to that day
        self.assertEqual(item.shown_return_date, dec(17))
        item.event_date, item.rental_date = dec(17), dec(17)                 # or the pick-up moved past it
        self.assertEqual(item.shown_return_date, dec(17))


class ExtraChargeNoLongerNeededTests(TestCase):
    """A customer pays the part the fees went past the deposit, then the shop moves the dates back: that money goes back with the deposit."""

    def paid_extra(self, pickup):
        booking = make({"pickup": 1, "ret": 17, "stage": S.RETURNED, "picked_up": 1, "returned": 17}, paid="")     # 12 days = 2,400
        pay(booking, P.FULL, "5500")
        pay(booking, P.EXTRA, "400")
        ReservationItem.objects.filter(reservation=booking).update(rental_date=dec(pickup))
        return booking

    def test_the_numbers_follow_however_far_the_dates_move_back(self):
        # new pick-up day, fee, still owed, credit going back, deposit money to hand back
        for pickup, fee, owed, credit, back in ((1, "2400", "0", "0", "0"), (2, "2200", "0", "200", "200"), (3, "2000", "0", "400", "400"),
                                                (8, "1000", "0", "400", "1400"), (13, "0", "0", "400", "2400")):
            with self.subTest(pickup=pickup):
                s = stmt(self.paid_extra(pickup), today=dec(18))
                self.assertEqual((s.late_fee, s.extra_owed, s.extra_credit, s.refundable_total, s.refund_available, s.balance_due),
                                 (D(fee), D(owed), D(credit), D(back), D(back), D("0")))

    def test_handing_it_all_back_settles_the_booking(self):
        booking = self.paid_extra(8)
        self.assertEqual(stmt(booking, today=dec(18)).state, ledger.REFUND)
        pay(booking, P.DEPOSIT_RETURN, "1400", direction=OUT)
        s = stmt(booking, today=dec(18))
        self.assertEqual((s.refund_available, s.state), (D("0.00"), ledger.SETTLED))

    def test_a_cancelled_booking_keeps_what_it_has(self):
        booking = self.paid_extra(8)
        Reservation.objects.filter(pk=booking.pk).update(status=Reservation.Status.CANCELLED, reviewed_at="2026-12-01T10:00:00+08:00")
        s = stmt(booking, today=dec(18))
        self.assertEqual((s.extra_credit, s.refund_available), (D("0.00"), D("0.00")))

    def test_the_money_that_goes_back_is_not_counted_as_kept(self):
        from arabela_admin import dashboard_money
        for pickup, kept in ((1, "2400"), (2, "2200"), (8, "1000"), (13, "0")):
            with self.subTest(pickup=pickup):
                Reservation.objects.all().delete()
                self.paid_extra(pickup)
                self.assertEqual(dashboard_money.money_between(today=dec(18))["kept"], D(kept))


class OlderBookingsAreUnchangedTests(TestCase):
    def test_an_older_booking_charges_only_what_it_always_did(self):
        booking = make({"pickup": 8, "ret": 19, "booked": 17, "old": True})     # picked up long before the event: no early fee under the old rule
        item = booking.items.get()
        self.assertEqual((item.uses_free_days, item.extra_days_count), (False, 0))
        s = stmt(booking)
        self.assertEqual((s.late_fee, s.early_fee, s.later_fee, s.fee_lines), (D("0.00"), D("0.00"), D("0.00"), []))    # not out yet
        booking = make({"pickup": 8, "ret": 19, "booked": 17, "old": True, "stage": S.RETURN, "picked_up": 8})
        s = stmt(booking, today=dec(20))
        self.assertEqual((s.change_days_total, s.overdue_days_total, s.late_fee), (2, 1, D("600.00")))

    def test_a_moved_return_on_an_older_booking_still_counts_from_the_original(self):
        booking = make({"pickup": 13, "ret": 19, "booked": 17, "old": True, "stage": S.RETURNED, "picked_up": 13, "returned": 18})
        s = stmt(booking, today=dec(20))
        self.assertEqual((s.late_fee, s.change_days_total, s.overdue_days_total), (D("200.00"), 1, 0))
