"""The Gown Board: pick one exact day and see what the shop has to get ready, what comes back, what is late and what is in a hurry.

It only READS (marking a pick-up or a return stays in Active Reservations), so what is checked here is that it shows the right gowns under the right
heading, that "Rush" means what the shop means by it, that a date typed or linked wrongly can never break the page, that nothing a customer typed can
reach the page as markup, that it asks the database the same number of questions however many gowns are on the day, and that it is closed to everyone
who is not staff."""
import re
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from arabela_admin.gown_board import board_for, parse_day
from arabela_admin.tests import _make_gown
from gowns.models import Gown, GownUnavailability
from reservations.models import Reservation, ReservationItem
from reservations.test_ledger import make_booking, pay
from reservations.models import Payment

User = get_user_model()
URL = reverse("arabela_admin:gown_board")
S = ReservationItem.Stage


class BoardBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("gb_staff", password="x", is_staff=True)

    def setUp(self):
        self.today = timezone.localdate()
        self.client.force_login(self.staff)
        self.n = 0

    def day(self, offset):
        return self.today + timedelta(days=offset)

    def book(self, pickup, ret, stage=S.PICKUP, *, gown=None, **kw):
        booking = make_booking(pickup=pickup, ret=ret, stage=stage, **kw)
        if gown is not None:
            ReservationItem.objects.filter(reservation=booking).update(gown=gown, gown_name=gown.name)
        return booking

    def gown(self, **kw):
        self.n += 1
        return _make_gown(self.n, **kw)

    def page(self, query=""):
        response = self.client.get(URL + query)
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()


class ParseDayTests(TestCase):
    def test_a_good_date_is_used(self):
        self.assertEqual(parse_day("2026-12-25", date(2026, 11, 24)), date(2026, 12, 25))

    def test_nothing_or_nonsense_means_today(self):
        today = date(2026, 11, 24)
        for raw in (None, "", "   ", "tomorrow", "2026-13-40", "2026-02-30", "11/24/2026", "<script>", "2026-12-25; DROP", "0000-00-00"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_day(raw, today), today)

    def test_a_date_years_away_means_today(self):
        today = date(2026, 11, 24)
        self.assertEqual(parse_day("2099-01-01", today), today)
        self.assertEqual(parse_day("1999-01-01", today), today)
        self.assertEqual(parse_day("2028-11-24", today), date(2028, 11, 24))                 # two years out is fine


class WhatIsOnTheDayTests(BoardBase):
    def test_a_pickup_waits_under_to_get_ready_with_what_the_customer_still_owes(self):
        self.book(0, 3)
        board = board_for(self.today, self.today)
        self.assertEqual((board["counts"]["pickups"], board["counts"]["returns"], board["counts"]["overdue"], board["counts"]["rush"]), (1, 0, 0, 0))
        [row] = board["pickups"]
        self.assertEqual((row["state"], row["state_text"], row["customer"]), ("waiting", "Waiting for pick-up", "Juan Tolentino"))
        self.assertEqual(row["balance"], "5,500.00")                                         # nothing paid yet: it is all still to collect

    def test_a_paid_booking_has_no_balance_chip(self):
        booking = self.book(0, 3)
        pay(booking, Payment.Purpose.FULL, "5500")
        [row] = board_for(self.today, self.today)["pickups"]
        self.assertEqual(row["balance"], "")

    def test_a_gown_picked_up_that_day_says_picked_up(self):
        self.book(-1, 3, S.RESERVED)
        item = ReservationItem.objects.get()
        ReservationItem.objects.filter(pk=item.pk).update(picked_up_on=self.today)
        [row] = board_for(self.today, self.today)["pickups"]
        self.assertEqual((row["state"], row["state_text"]), ("picked", "Picked up"))

    def test_a_pickup_nobody_came_for_says_not_picked_up_on_a_past_day(self):
        self.book(-2, 1)
        [row] = board_for(self.day(-2), self.today)["pickups"]
        self.assertEqual((row["state"], row["state_text"]), ("missed", "Not picked up"))

    def test_a_gown_due_back_that_day_is_under_to_come_back(self):
        self.book(-3, 0, S.RESERVED)
        board = board_for(self.today, self.today)
        [row] = board["returns"]
        self.assertEqual((row["state"], row["state_text"]), ("due", "Due back"))
        self.assertEqual(board["counts"]["returns"], 1)

    def test_a_gown_that_came_back_that_day_says_returned_and_the_day(self):
        self.book(-3, 0, S.RETURNED, returned_on=0)
        [row] = board_for(self.today, self.today)["returns"]
        self.assertEqual((row["state"], row["state_text"]), ("returned", f"Returned {self.today:%b} {self.today.day}"))

    def test_a_late_gown_is_listed_overdue_now_whatever_day_is_shown(self):
        self.book(-9, -3, S.RESERVED)
        for shown in (self.today, self.day(10), self.day(-30)):
            with self.subTest(shown=shown):
                board = board_for(shown, self.today)
                [row] = board["overdue"]
                self.assertEqual((row["state"], row["late_days"], row["state_text"]), ("late", 3, "3 days late"))
                self.assertEqual(board["counts"]["overdue"], 1)

    def test_one_day_late_is_not_pluralised(self):
        self.book(-9, -1, S.RESERVED)
        [row] = board_for(self.today, self.today)["overdue"]
        self.assertEqual(row["state_text"], "1 day late")

    def test_the_most_late_gown_is_first(self):
        self.book(-9, -2, S.RESERVED, customer=None)
        late = self.book(-12, -6, S.RESERVED)
        Reservation.objects.filter(pk=late.pk).update(customer_name="Most Late")
        rows = board_for(self.today, self.today)["overdue"]
        self.assertEqual([r["late_days"] for r in rows], [6, 2])
        self.assertEqual(rows[0]["customer"], "Most Late")

    def test_a_cancelled_gown_and_a_rejected_or_pending_booking_are_not_on_the_board(self):
        self.book(0, 3, S.CANCELLED)
        self.book(0, 3, status=Reservation.Status.PENDING)
        self.book(0, 3, status=Reservation.Status.REJECTED)
        self.book(0, 3, status=Reservation.Status.CANCELLED)
        board = board_for(self.today, self.today)
        self.assertEqual(board["counts"], {"pickups": 0, "returns": 0, "overdue": 0, "rush": 0})

    def test_another_day_is_not_mixed_in(self):
        self.book(5, 8)
        self.book(-2, 8, S.RESERVED)
        board = board_for(self.today, self.today)
        self.assertEqual((board["counts"]["pickups"], board["counts"]["returns"]), (0, 0))
        self.assertEqual(board_for(self.day(5), self.today)["counts"]["pickups"], 1)
        self.assertEqual(board_for(self.day(8), self.today)["counts"]["returns"], 1)             # only the gown that is OUT is due back: one still waiting for pick-up is not

    def test_a_gown_resting_until_the_day_before_is_ready_again(self):
        gown = self.gown()
        GownUnavailability.objects.create(gown=gown, start_date=self.day(-3), end_date=self.day(-1), reason=GownUnavailability.Reason.CLEANING)
        [ready] = board_for(self.today, self.today)["ready_again"]
        self.assertEqual((ready["gown"], ready["why"], ready["ended"]), (gown.name, "Cleaning", self.day(-1)))
        self.assertEqual(board_for(self.day(1), self.today)["ready_again"], [])


class RushTests(BoardBase):
    def test_a_gown_that_needs_repair_is_a_rush_pickup(self):
        gown = self.gown(condition=Gown.Condition.NEEDS_REPAIR)
        self.book(1, 4, gown=gown)
        board = board_for(self.day(1), self.today)
        [row] = board["rush"]
        self.assertEqual(row["reason"], "Needs repair before it can go out")
        self.assertEqual((board["counts"]["rush"], board["counts"]["pickups"], board["pickups"]), (1, 1, []))     # a rush pick-up is counted once, under Rush

    def test_a_gown_marked_out_of_stock_is_a_rush_pickup(self):
        gown = self.gown(status=Gown.Status.OUT_OF_STOCK)
        self.book(0, 3, gown=gown)
        [row] = board_for(self.today, self.today)["rush"]
        self.assertEqual(row["reason"], "Marked Out-of-Stock")

    def test_a_gown_still_out_with_the_last_customer_is_a_rush_pickup(self):
        gown = self.gown()
        self.book(-8, -1, S.RESERVED, gown=gown)                                            # was due back yesterday, still out
        self.book(1, 4, gown=gown)
        [row] = board_for(self.day(1), self.today)["rush"]
        self.assertEqual(row["reason"], f"Still out with the last customer (was due {self.day(-1):%b} {self.day(-1).day})")

    def test_a_gown_being_cleaned_that_day_is_a_rush_pickup(self):
        gown = self.gown()
        GownUnavailability.objects.create(gown=gown, start_date=self.day(-1), end_date=self.day(2), reason=GownUnavailability.Reason.CLEANING)
        self.book(1, 4, gown=gown)
        [row] = board_for(self.day(1), self.today)["rush"]
        self.assertEqual(row["reason"], f"Cleaning until {self.day(2):%b} {self.day(2).day}")

    def test_a_ready_gown_is_not_a_rush_pickup(self):
        self.book(1, 4, gown=self.gown())
        board = board_for(self.day(1), self.today)
        self.assertEqual((board["counts"]["rush"], len(board["pickups"])), (0, 1))

    def test_rush_is_only_checked_for_today_and_the_next_two_days(self):
        gown = self.gown(condition=Gown.Condition.NEEDS_REPAIR)
        self.book(6, 9, gown=gown)
        far = board_for(self.day(6), self.today)
        self.assertEqual((far["counts"]["rush"], far["rush_checked"]), (0, False))
        self.assertTrue(board_for(self.today, self.today)["rush_checked"])
        self.assertTrue(board_for(self.day(2), self.today)["rush_checked"])
        self.assertFalse(board_for(self.day(3), self.today)["rush_checked"])
        self.assertFalse(board_for(self.day(-1), self.today)["rush_checked"])

    def test_a_pickup_already_done_is_not_a_rush(self):
        gown = self.gown(condition=Gown.Condition.NEEDS_REPAIR)
        self.book(0, 3, S.RESERVED, gown=gown)
        ReservationItem.objects.update(picked_up_on=self.today)
        self.assertEqual(board_for(self.today, self.today)["counts"]["rush"], 0)


class ThePageTests(BoardBase):
    def test_the_page_opens_on_today_with_its_date_controls(self):
        response, html = self.page()
        self.assertEqual(response.context["board"]["iso"], self.today.isoformat())
        self.assertIn(f'value="{self.today.isoformat()}"', html)
        self.assertIn(f'href="?date={self.day(-1).isoformat()}"', html)
        self.assertIn(f'href="?date={self.day(1).isoformat()}"', html)
        self.assertIn(">Today</a>", html)
        self.assertIn(">Tomorrow</a>", html)
        self.assertIn("Gown Board | Arabela System Admin Panel", html)
        self.assertIn("pageName: `Gown Board`", html)

    def test_the_page_follows_the_date_in_the_address(self):
        self.book(5, 8)
        response, html = self.page(f"?date={self.day(5).isoformat()}")
        self.assertEqual(response.context["board"]["counts"]["pickups"], 1)
        self.assertIn("Juan Tolentino", html)
        self.assertIn(f'value="{self.day(5).isoformat()}"', html)

    def test_a_wrong_date_in_the_address_shows_today_and_does_not_break(self):
        for bad in ("nonsense", "2026-99-99", "%3Cscript%3E", "", "2099-01-01"):
            with self.subTest(bad=bad):
                response, _ = self.page(f"?date={bad}")
                self.assertEqual(response.context["board"]["iso"], self.today.isoformat())

    def test_each_line_opens_the_booking_in_active_reservations(self):
        booking = self.book(0, 3)
        _, html = self.page()
        self.assertIn(f'href="{reverse("arabela_admin:active_reservations")}?search={booking.reference_code}"', html)
        self.assertIn(">Open booking</a>", html)

    def test_a_past_day_says_pickups_that_day_not_to_get_ready(self):
        self.book(-2, 1)
        _, html = self.page(f"?date={self.day(-2).isoformat()}")
        self.assertIn("Pick-ups that day", html)
        self.assertNotIn("To get ready", html)

    def test_the_headings_and_empty_words_are_there_for_an_empty_day(self):
        _, html = self.page()
        for words in ("To get ready", "To come back", "Overdue now", "Ready again", "No gown is picked up on this day.", "No gown is due back on this day.", "No gown is overdue right now."):
            self.assertIn(words, html)
        self.assertNotIn('id="gb-rush"', html)                                               # no Rush section when nothing is in a hurry

    def test_a_rush_pickup_shows_its_section_and_its_reason(self):
        gown = self.gown(condition=Gown.Condition.NEEDS_REPAIR)
        self.book(0, 3, gown=gown)
        _, html = self.page()
        self.assertIn('id="gb-rush"', html)
        self.assertIn("Rush &middot; Needs repair before it can go out", html)

    def test_the_balance_is_shown_as_a_chip(self):
        self.book(0, 3)
        _, html = self.page()
        self.assertIn("Balance &#8369;5,500.00", html)

    def test_the_gown_tag_colour_is_drawn_from_the_shops_own_fixed_colours(self):
        gown = self.gown()
        self.book(0, 3, gown=gown)
        _, html = self.page()
        self.assertRegex(html, r'<span class="gb-dot"( style="background: #[0-9A-Fa-f]{3,6};")? title="')

    def test_what_a_customer_typed_cannot_become_markup(self):
        booking = self.book(0, 3)
        Reservation.objects.filter(pk=booking.pk).update(customer_name='<script>alert("x")</script>', phone='"><img src=x onerror=alert(1)>')
        ReservationItem.objects.filter(reservation=booking).update(gown_name="<b>Gown</b>")
        _, html = self.page()
        for raw in ('<script>alert("x")</script>', "<img src=x onerror=alert(1)>", "<b>Gown</b>"):
            self.assertNotIn(raw, html)
        self.assertIn("&lt;script&gt;", html)

    def test_the_live_region_is_marked_so_the_page_updates_itself(self):
        _, html = self.page()
        self.assertIn('data-live-region="gown-board"', html)
        self.assertIn('data-live-hold="details[open]"', html)
        self.assertEqual(html.count('data-live-region="gown-board"'), 1)

    def test_the_sidebar_marks_the_board_as_the_current_page_and_opens_its_group(self):
        _, html = self.page()
        self.assertIn(":class=\"page === 'gown-board' ? 'menu-dropdown-item-active' : 'menu-dropdown-item-inactive'\"", html)
        self.assertIn("page: 'gown-board'", html)
        self.assertIn(f'href="{URL}"', html)

    def test_no_template_syntax_leaks_into_the_page(self):
        _, html = self.page()
        for leak in ("{%", "{{", "{#"):
            self.assertNotIn(leak, html)


class FewQuestionsTests(BoardBase):
    def queries(self):
        self.client.get(URL)                                                                 # the first visit also creates the shop-settings row
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(URL).status_code, 200)
        return len(captured.captured_queries)

    def test_more_gowns_on_the_day_do_not_mean_more_questions(self):
        for _ in range(3):
            self.book(0, 3, gown=self.gown(), gowns=1)
        self.book(-8, -2, S.RESERVED, gown=self.gown())
        short = self.queries()
        for _ in range(10):
            self.book(0, 3, gown=self.gown(), gowns=2)
            self.book(-8, -2, S.RESERVED, gown=self.gown())
            self.book(-3, 0, S.RESERVED, gown=self.gown())
        self.assertEqual(self.queries(), short)


class AccessTests(TestCase):
    def test_an_anonymous_visitor_is_sent_to_sign_in(self):
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_a_customer_is_turned_away(self):
        self.client.force_login(User.objects.create_user("gb_customer", password="x"))
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_staff_get_the_page(self):
        self.client.force_login(User.objects.create_user("gb_staff2", password="x", is_staff=True))
        self.assertEqual(self.client.get(URL).status_code, 200)

    def test_the_page_only_reads_so_a_post_changes_nothing(self):
        self.client.force_login(User.objects.create_user("gb_staff3", password="x", is_staff=True))
        before = (Reservation.objects.count(), ReservationItem.objects.count(), Payment.objects.count())
        self.client.post(URL, {"date": "2026-01-01"})
        self.assertEqual((Reservation.objects.count(), ReservationItem.objects.count(), Payment.objects.count()), before)
