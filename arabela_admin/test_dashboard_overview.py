"""The Dashboard's top half: the greeting that follows the shop's clock, the quick buttons, the warning line, Rental Income (what the shop EARNED this month,
never the security deposits) and Security Deposits Held (counted by the Security Deposits page's own rules).

The money rules themselves are reservations/ledger.py's (tested in reservations/test_ledger.py); here: that the Dashboard adds them up right for the
month -- a payment covers the deposit first, then the rental -- and draws them."""
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from arabela_admin import dashboard_money
from arabela_admin.views import _deposits_held
from reservations import payments
from reservations.models import Payment, Reservation, ReservationItem
from reservations.test_ledger import make_booking, pay

User = get_user_model()
D = Decimal
P = Payment.Purpose
S = ReservationItem.Stage


def last_month_noon():
    """A moment in the month before this one (the day before the 1st, at noon, shop time)."""
    day = timezone.localdate().replace(day=1) - timedelta(days=1)
    return timezone.make_aware(datetime.combine(day, time(12, 0)))


def days_to_last_month():
    """The day offset (from today) of the last day of the month before this one."""
    today = timezone.localdate()
    return (today.replace(day=1) - timedelta(days=1) - today).days


class GreetingTests(SimpleTestCase):
    def test_the_three_steps_of_the_day(self):
        for hour, words in ((0, "Good evening"), (4, "Good evening"), (5, "Good morning"), (11, "Good morning"), (12, "Good afternoon"),
                            (17, "Good afternoon"), (18, "Good evening"), (23, "Good evening")):
            with self.subTest(hour=hour):
                self.assertEqual(dashboard_money.greeting(hour), words)


class AlertTests(SimpleTestCase):
    def test_only_what_needs_a_person_and_each_piece_opens_its_page(self):
        self.assertEqual(dashboard_money.alerts(None), [])
        self.assertEqual(dashboard_money.alerts({"late_gowns": 0, "pickups_owing": 0, "online_attention": 0}), [])
        one = dashboard_money.alerts({"late_gowns": 1, "pickups_owing": 1, "online_attention": 1})
        self.assertEqual([a["text"] for a in one], ["1 gown is late coming back", "1 pick-up in the next 2 days still has a balance", "1 online payment needs a look"])
        self.assertEqual([a["url"] for a in one], [reverse("arabela_admin:gown_board") + "#gb-overdue", reverse("arabela_admin:payments"),
                                                   reverse("arabela_admin:payment_online")])
        two = dashboard_money.alerts({"late_gowns": 2, "pickups_owing": 3, "online_attention": 2})
        self.assertEqual([a["text"] for a in two], ["2 gowns are late coming back", "3 pick-ups in the next 2 days still have a balance", "2 online payments need a look"])


class RentalIncomeTests(TestCase):
    """A booking of ₱3,500 rental + ₱2,000 deposit (make_booking)."""

    def money(self):
        return dashboard_money.month_money(timezone.localdate())

    def test_paid_in_full_this_month_counts_only_the_rental(self):
        pay(make_booking(), P.FULL, "5500")
        self.assertEqual(self.money(), {"income": "3,500.00", "bookings": 1, "kept": "0.00"})

    def test_a_deposit_alone_is_never_income(self):
        pay(make_booking(), P.DEPOSIT, "2000")
        self.assertEqual(self.money(), {"income": "0.00", "bookings": 0, "kept": "0.00"})

    def test_the_rental_counts_in_the_month_it_was_paid(self):
        early = make_booking()
        Payment.objects.filter(pk=pay(early, P.DEPOSIT, "2000").pk).update(paid_at=last_month_noon())
        pay(early, P.BALANCE, "1500")
        pay(early, P.BALANCE, "2000")                                                      # the rental in two parts, both this month
        before = make_booking()
        Payment.objects.filter(pk=pay(before, P.FULL, "5500").pk).update(paid_at=last_month_noon())   # all of it last month: nothing this month
        self.assertEqual(self.money(), {"income": "3,500.00", "bookings": 1, "kept": "0.00"})

    def test_a_voided_payment_and_a_booking_still_waiting_for_approval_count_nothing(self):
        pay(make_booking(), P.FULL, "5500", status=Payment.Status.VOID)
        pay(make_booking(status=Reservation.Status.PENDING), P.FULL, "5500")
        pay(make_booking(status=Reservation.Status.REJECTED), P.FULL, "5500")
        self.assertEqual(self.money()["income"], "0.00")

    def test_the_late_fee_taken_from_the_deposit_is_kept_in_the_month_the_gown_came_back(self):
        back_today = make_booking(stage=S.RETURNED, ret=-3, returned_on=0)                 # 3 days late: ₱600 out of the deposit
        pay(back_today, P.FULL, "5500")
        back_before = make_booking(stage=S.RETURNED, ret=days_to_last_month() - 2, returned_on=days_to_last_month(), pickup=days_to_last_month() - 6)
        Payment.objects.filter(pk=pay(back_before, P.FULL, "5500").pk).update(paid_at=last_month_noon())   # its ₱400 was last month's
        self.assertEqual(self.money(), {"income": "3,500.00", "bookings": 1, "kept": "600.00"})

    def test_a_late_fee_bigger_than_the_deposit_counts_the_deposit_part_and_the_extra_paid(self):
        booking = make_booking(stage=S.RETURNED, ret=-15, returned_on=0, pickup=-20)        # 15 days x ₱200 = ₱3,000: ₱1,000 over the deposit
        pay(booking, P.FULL, "5500")
        pay(booking, P.EXTRA, "1000")
        self.assertEqual(self.money(), {"income": "3,500.00", "bookings": 1, "kept": "3,000.00"})

    def test_a_deduction_counts_once_every_gown_is_back(self):
        booking = make_booking(stage=S.RETURNED, ret=0, returned_on=0)
        pay(booking, P.FULL, "5500")
        payments.add_deduction(reservation_id=booking.pk, amount=D("300"), reason="Stain on the skirt")
        self.assertEqual(self.money()["kept"], "300.00")
        out = make_booking(stage=S.RESERVED, pickup=-2, ret=3)                              # still out: its deduction is not final yet
        pay(out, P.FULL, "5500")
        payments.add_deduction(reservation_id=out.pk, amount=D("500"), reason="Torn hem")
        self.assertEqual(self.money()["kept"], "300.00")


class DepositsHeldTests(TestCase):
    def test_a_gown_still_out_holds_what_was_paid_and_a_returned_one_holds_what_is_still_to_hand_back(self):
        out = make_booking(stage=S.RESERVED, pickup=-2, ret=3)
        pay(out, P.DEPOSIT, "2000")
        back = make_booking(stage=S.RETURNED, ret=-3, returned_on=0)                         # ₱600 late fee: ₱1,400 goes back
        pay(back, P.FULL, "5500")
        make_booking(stage=S.RESERVED, pickup=-2, ret=3)                                     # nothing paid: nothing held
        done = make_booking(stage=S.RETURNED, ret=0, returned_on=0)
        pay(done, P.FULL, "5500")
        payments.return_deposit(reservation_id=done.pk, amount=D("2000"), method=Payment.Method.CASH)   # handed back: not held any more
        self.assertEqual(_deposits_held(timezone.localdate()), {"held": "3,400.00", "held_bookings": 2, "due": "1,400.00", "due_bookings": 1})

    def test_it_counts_the_same_bookings_as_the_security_deposits_page(self):
        pay(make_booking(stage=S.RESERVED, pickup=-2, ret=3), P.DEPOSIT, "2000")
        pay(make_booking(stage=S.RETURNED, ret=-3, returned_on=0), P.FULL, "5500")
        owner = User.objects.create_superuser("dh_owner", "dh.owner@gmail.com", "x")
        self.client.force_login(owner)
        page = self.client.get(reverse("arabela_admin:security_deposits")).context
        held = _deposits_held(timezone.localdate())
        self.assertEqual((held["held_bookings"] - held["due_bookings"], held["due_bookings"]), (page["held_count"], page["due_count"]))


class DashboardTopTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("dt_owner", "dt.owner@gmail.com", "x", first_name="Denmark")
        UserProfile.objects.update_or_create(user=cls.owner, defaults={"role": UserProfile.Role.OWNER})

    def page(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("arabela_admin:dashboard"))
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def test_the_greeting_uses_the_shop_clock_and_the_first_name_and_keeps_up_by_itself(self):
        response, html = self.page()
        today = timezone.localdate()
        self.assertIn(f'>{dashboard_money.greeting(timezone.localtime().hour)}, Denmark</h1>', html)
        self.assertIn('data-zone="Asia/Manila"', html)
        self.assertIn(f'<p class="db-date" id="db-date">{today:%A}, {today:%B} {today.day}, {today.year}</p>', html)
        for piece in ("hourCycle: 'h23'", "window.setInterval(tick, 60000)", "hour >= 5 && hour < 12", "hour >= 12 && hour < 18"):
            self.assertIn(piece, html)

    def test_the_quick_buttons_open_todays_pick_ups_and_returns(self):
        _, html = self.page()
        today = timezone.localdate().isoformat()
        active = reverse("arabela_admin:active_reservations")
        self.assertIn(f'href="{active}?date={today}&amp;kind=pickup"', html)
        self.assertIn(f'href="{active}?date={today}&amp;kind=return"', html)
        self.assertIn("Pick-ups today", html)
        self.assertIn("Returns today", html)

    def test_the_money_cards_say_what_they_count_and_open_their_pages(self):
        pay(make_booking(), P.FULL, "5500")
        _, html = self.page()
        for piece in ("Rental Income", "&#8369;3,500.00", "from 1 booking &middot; deposits not included", "Late fees &amp; deductions kept: <b>&#8369;0.00</b>",
                      "Security Deposits Held", "&#8369;2,000.00", "for 1 booking &middot; goes back to the customers", "Nothing to hand back right now",
                      'href="%s"' % reverse("arabela_admin:payments"), 'href="%s"' % reverse("arabela_admin:security_deposits")):
            with self.subTest(piece=piece):
                self.assertIn(piece, html)

    def test_with_nothing_yet_the_cards_say_so(self):
        _, html = self.page()
        for piece in ("&#8369;0.00", "No rental paid yet this month", "No deposit is held right now", "Nothing to hand back right now"):
            with self.subTest(piece=piece):
                self.assertIn(piece, html)

    def test_a_deposit_waiting_to_go_back_is_called_out(self):
        pay(make_booking(stage=S.RETURNED, ret=-3, returned_on=0), P.FULL, "5500")
        _, html = self.page()
        self.assertIn('<span class="db-due">&#8369;1,400.00 to hand back now</span> (1 booking)', html)

    def test_a_failing_money_card_is_left_out_and_the_rest_stays(self):
        with mock.patch("arabela_admin.views._deposits_held", side_effect=RuntimeError("boom")):
            with self.assertLogs("arabela_admin.views", level="ERROR"):
                _, html = self.page()
        self.assertNotIn("Security Deposits Held", html)
        self.assertIn("Rental Income", html)
        self.assertIn("Bookings", html)
