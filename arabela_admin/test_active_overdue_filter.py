"""Active Reservations is for rentals that are still running.

  * a booking whose every gown is back is finished: it is not listed (it lives in Reservation Records and Rental History);
  * every listed booking is Confirmed (waiting for the first pick-up), Active (a gown has gone out) or Overdue (a gown is past its return
    date), and the page's colour key is the filter for those three, with the counts;
  * late bookings come first, the most days late on top, and say how many days late they are;
  * "days late" is ONE rule (arabela_admin.views._days_late): counted from the return date the customer originally booked, the same
    number the gown's remarks print;
  * Rental History shows the same number for every gown, and calls a gown nobody picked up "Not picked up", not "Overdue";
  * all of it is the same for the owner, a Manager and a Staff account.
"""
import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.urls import reverse

from accounts.models import UserProfile
from arabela_admin.test_cancel_booking import Base
from arabela_admin.views import _days_late, _effective_stage, _item_remarks
from reservations.models import Reservation, ReservationItem

User = get_user_model()
Stage = ReservationItem.Stage
ACTIVE_URL = reverse("arabela_admin:active_reservations")
HISTORY_URL = reverse("arabela_admin:rental_history")


class RunningRentals(Base):
    """days_late=N means the pick-up was N days ago; the return is 4 days after the pick-up (see Base.booking)."""

    def setUp(self):
        super().setUp()
        self.late6, _ = self.booking(days_late=10, number_from=1, stage=Stage.RESERVED)      # return was 6 days ago
        self.late4, _ = self.booking(days_late=8, number_from=2, stage=Stage.RESERVED)       # return was 4 days ago
        self.late1, _ = self.booking(days_late=5, number_from=3, stage=Stage.RESERVED)       # return was yesterday
        self.out, _ = self.booking(days_late=1, number_from=4, stage=Stage.RESERVED)         # out, due back in 3 days
        self.waiting, _ = self.booking(days_late=-3, number_from=5)                          # pick-up in 3 days
        self.finished, [done] = self.booking(days_late=10, number_from=6, stage=Stage.RETURNED)
        ReservationItem.objects.filter(pk=done.pk).update(returned_on=self.today - timedelta(days=5))

    def page(self, user=None):
        if user:
            self.client.force_login(user)
        return self.client.get(ACTIVE_URL)

    def refs(self, response):
        return [r.reference_code for r in response.context["reservations"]]

    # ------------------------------------------------------------------------------------------------------------------ what is listed
    def test_a_finished_booking_is_not_listed_but_every_running_one_is(self):
        response = self.page()
        html = response.content.decode()
        self.assertNotIn(self.finished.reference_code, html)
        self.assertNotIn(">Completed</p>", html)
        for booking in (self.late6, self.late4, self.late1, self.out, self.waiting):
            self.assertIn(booking.reference_code, html)
        self.assertEqual(len(self.refs(response)), 5)

    def test_the_counts_are_right(self):
        counts = self.page().context["state_counts"]
        self.assertEqual(counts, {"all": 5, "confirmed": 1, "active": 1, "overdue": 3})

    def test_late_bookings_come_first_the_most_days_late_on_top(self):
        refs = self.refs(self.page())
        self.assertEqual(refs[:3], [self.late6.reference_code, self.late4.reference_code, self.late1.reference_code])
        self.assertEqual(set(refs[3:]), {self.out.reference_code, self.waiting.reference_code})

    def test_each_booking_carries_its_state_and_the_badge_matches(self):
        response = self.page()
        states = {r.reference_code: r.state for r in response.context["reservations"]}
        self.assertEqual(states[self.late6.reference_code], "overdue")
        self.assertEqual(states[self.out.reference_code], "active")
        self.assertEqual(states[self.waiting.reference_code], "confirmed")
        html = response.content.decode()
        self.assertEqual(html.count(">Overdue</p>"), 3)
        self.assertEqual(html.count(">Active</p>"), 1)
        self.assertEqual(html.count(">Confirmed</p>"), 1)                 # a gown that is out no longer says "Confirmed"
        self.assertIn('data-state="overdue"', html)
        self.assertIn("ar-booking is-overdue", html)

    def test_a_late_booking_says_how_many_days_late(self):
        html = self.page().content.decode()
        self.assertIn(">6 days late</span>", html)
        self.assertIn(">4 days late</span>", html)
        self.assertIn(">1 day late</span>", html)                         # singular
        self.assertEqual(html.count("ar-chip ar-chip--late"), 3)

    def test_the_colour_key_is_the_filter_with_counts(self):
        html = self.page().content.decode()
        for label, count in (("All", 5), ("Confirmed", 1), ("Active", 1), ("Overdue", 3)):
            self.assertIn(f'{label} <span class="ar-n">{count}</span></button>', html)
        self.assertIn("ar-filter--hot", html)                             # the Overdue button is marked while something is late
        self.assertIn("stateFilter = 'overdue'", html)
        self.assertIn("(stateFilter === 'all' || stateFilter === 'overdue')", html)
        self.assertIn("matchCount() === 0", html)                         # the message for "nothing here"
        for leak in ("{%", "{{", "{#"):
            self.assertNotIn(leak, html)

    def test_the_overdue_button_is_not_marked_when_nothing_is_late(self):
        for booking in (self.late6, self.late4, self.late1):
            booking.items.update(stage=Stage.RETURNED, returned_on=self.today)
        response = self.page()
        self.assertEqual(response.context["state_counts"]["overdue"], 0)
        self.assertNotIn('class="ar-filter ar-filter--hot"', response.content.decode())      # (the CSS rule itself stays in the style block)

    def test_a_gown_in_the_old_Return_stage_can_still_be_marked_returned(self):
        booking, [item] = self.booking(days_late=1, number_from=20, stage=Stage.RETURN)
        html = self.page().content.decode()
        self.assertIn("resv-mark-returned-btn", html)
        self.assertEqual({r.reference_code: r.state for r in self.page().context["reservations"]}[booking.reference_code], "active")

    def test_a_page_with_no_running_rentals_keeps_its_own_message(self):
        Reservation.objects.all().delete()
        html = self.page().content.decode()
        self.assertIn("No active reservations yet", html)
        self.assertNotIn("matchCount() === 0\" x-cloak", html)

    # ------------------------------------------------------------------------------------------------------------------- partly returned
    def test_partly_returned_and_waiting_counts_as_active_and_partly_returned_and_late_as_overdue(self):
        started, [first, second] = self.booking(days_late=2, gowns=2, number_from=30, stage=Stage.RETURNED)
        ReservationItem.objects.filter(pk=second.pk).update(stage=Stage.PICKUP, rental_date=self.today + timedelta(days=5),
                                                           return_date=self.today + timedelta(days=9))
        ReservationItem.objects.filter(pk=first.pk).update(returned_on=self.today)
        late, [a, b] = self.booking(days_late=9, gowns=2, number_from=40, stage=Stage.RETURNED)
        ReservationItem.objects.filter(pk=b.pk).update(stage=Stage.RESERVED)                  # one back, one still out and 5 days late
        states = {r.reference_code: (r.state, r.overdue_days) for r in self.page().context["reservations"]}
        self.assertEqual(states[started.reference_code][0], "active")
        self.assertEqual(states[late.reference_code], ("overdue", 5))

    def test_marking_the_last_gown_returned_takes_the_booking_off_the_page(self):
        item = self.out.items.get()
        before = self.page().context["state_counts"]["all"]
        response = self.client.post(reverse("arabela_admin:reservation_item_mark_returned", args=[item.pk]),
                                    data=json.dumps({"condition": "Good"}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        page = self.page()
        self.assertEqual(page.context["state_counts"]["all"], before - 1)
        self.assertNotIn(self.out.reference_code, page.content.decode())
        history = self.client.get(HISTORY_URL).content.decode()
        self.assertIn(item.gown_name, history)                                            # the finished rental is still on record there

    # ------------------------------------------------------------------------------------------------------------------- the same for everyone
    def test_the_owner_a_manager_and_a_staff_account_see_the_same_page(self):
        staff = User.objects.create_user("pv_staff_x", password="x", is_staff=True)
        UserProfile.objects.update_or_create(user=staff, defaults={"role": UserProfile.Role.STAFF})
        for label, user in (("manager", self.staff), ("owner", self.owner), ("staff", staff)):
            with self.subTest(account=label):
                response = self.page(user)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["state_counts"], {"all": 5, "confirmed": 1, "active": 1, "overdue": 3})
                html = response.content.decode()
                self.assertIn("ar-filters", html)
                self.assertNotIn(self.finished.reference_code, html)


class DaysLateRule(Base):
    """One rule, used by Active Reservations, Rental History and the remarks."""

    def item(self, **fields):
        self.n = getattr(self, "n", 0) + 1                                  # every gown needs its own number
        _, [item] = self.booking(days_late=10, number_from=self.n * 10, stage=fields.pop("stage", Stage.RESERVED))
        for key, value in fields.items():
            setattr(item, key, value)
        item.save()
        return item

    def test_a_returned_gown_counts_from_the_booked_return_date(self):
        item = self.item(stage=Stage.RETURNED, returned_on=None)
        due = item.return_date
        for offset, expected in ((2, 2), (0, 0), (-1, 0)):                 # late, on the day, early
            item.returned_on = due + timedelta(days=offset)
            self.assertEqual(_days_late(item, self.today), expected)

    def test_a_returned_gown_with_no_return_day_has_nothing_to_say(self):
        self.assertIsNone(_days_late(self.item(stage=Stage.RETURNED, returned_on=None), self.today))

    def test_a_gown_still_out_counts_the_days_so_far_and_not_before_it_is_due(self):
        late = self.item()                                                 # return was 6 days ago
        self.assertEqual(_days_late(late, self.today), 6)
        due_today = self.item(return_date=self.today)
        self.assertIsNone(_days_late(due_today, self.today))
        self.assertEqual(_days_late(due_today, self.today + timedelta(days=1)), 1)

    def test_nothing_to_say_before_pick_up_or_after_a_cancel(self):
        self.assertIsNone(_days_late(self.item(stage=Stage.PICKUP), self.today))
        self.assertIsNone(_days_late(self.item(stage=Stage.CANCELLED), self.today))

    def test_it_counts_from_the_ORIGINAL_return_date_when_the_customer_asked_for_longer(self):
        item = self.item()
        item.original_return_date = item.return_date - timedelta(days=2)    # the customer first booked it 2 days shorter
        item.save()
        self.assertEqual(_days_late(item, self.today), 8)                    # 6 past the new date + the 2 extra days, like the remarks

    def test_the_number_matches_the_one_in_the_remarks(self):
        late = self.item()
        text = " ".join(r["text"] for r in _item_remarks(late, self.today))
        self.assertIn(f"{_days_late(late, self.today)} days late", text)
        back = self.item(stage=Stage.RETURNED, returned_on=late.return_date + timedelta(days=3))
        text = " ".join(r["text"] for r in _item_remarks(back, self.today))
        self.assertIn(f"{_days_late(back, self.today)} days late", text)
        self.assertEqual(_effective_stage(late, self.today), Stage.OVERDUE)


class RentalHistoryDaysLate(Base):
    def setUp(self):
        super().setUp()
        _, [self.late_back] = self.booking(days_late=12, number_from=1, stage=Stage.RETURNED)            # due 8 days ago
        _, [self.on_time] = self.booking(days_late=12, number_from=2, stage=Stage.RETURNED)
        ReservationItem.objects.filter(pk=self.late_back.pk).update(returned_on=self.today - timedelta(days=6))     # 2 days late
        ReservationItem.objects.filter(pk=self.on_time.pk).update(returned_on=self.today - timedelta(days=8))       # on the day
        _, [self.overdue] = self.booking(days_late=9, number_from=3, stage=Stage.RESERVED)             # due 5 days ago, still out
        _, [self.no_show] = self.booking(days_late=9, number_from=4)                                    # never picked up, dates passed
        _, [self.ongoing] = self.booking(days_late=1, number_from=5, stage=Stage.RESERVED)             # out, due in 3 days
        self.items = {}
        response = self.client.get(HISTORY_URL)
        self.assertEqual(response.status_code, 200)
        self.html = response.content.decode()
        for item in response.context["rental_items"]:
            self.items[item.pk] = item

    def test_every_gown_gets_the_right_status_and_days_late(self):
        expected = {
            self.late_back.pk: ("Completed", 2), self.on_time.pk: ("Completed", 0), self.overdue.pk: ("Overdue", 5),
            self.no_show.pk: ("Not picked up", None), self.ongoing.pk: ("Ongoing", None),
        }
        for pk, (status, late) in expected.items():
            with self.subTest(item=pk):
                self.assertEqual((self.items[pk].history_status, self.items[pk].late_days), (status, late))

    def test_the_page_words_it_for_people(self):
        self.assertIn("2 days late", self.html)
        self.assertIn("On time", self.html)
        self.assertIn("5 days late so far", self.html)
        self.assertIn("Not picked up", self.html)
        self.assertIn("Days late", self.html)
        self.assertIn(f"Returned {(self.today - timedelta(days=6)):%b} {(self.today - timedelta(days=6)).day}, ", self.html)

    def test_the_empty_and_timeline_rows_still_span_the_whole_table(self):
        self.assertNotIn('colspan="7"', self.html)
        self.assertIn('colspan="8"', self.html)

    def test_the_same_number_as_active_reservations(self):
        page = self.client.get(ACTIVE_URL)
        by_ref = {r.reference_code: r.overdue_days for r in page.context["reservations"]}
        self.assertEqual(by_ref[self.overdue.reservation.reference_code], self.items[self.overdue.pk].late_days)


class SidebarLinkToRentalHistory(Base):
    def test_every_admin_page_links_to_it_right_below_active_reservations(self):
        import re
        names = ("dashboard", "new_booking", "rental_schedule", "active_reservations", "pending_approval", "rental_history", "gown_catalog",
                 "clients", "payment_verification", "security_deposits", "reservation_records")
        for name in names:
            with self.subTest(page=name):
                html = self.client.get(reverse(f"arabela_admin:{name}")).content.decode()
                nav = re.search(r"<nav x-data=\"\{selected: \$persist\('Dashboard'\)\}\".*?</nav>", html, re.S).group(0)
                hrefs = re.findall(r'<a\s+href="([^"]+)"\s+class="menu-dropdown-item', nav)
                book = hrefs[hrefs.index(reverse("arabela_admin:new_booking")):][:5]
                self.assertEqual([book[0], book[2], book[3], book[4]],
                                 [reverse("arabela_admin:new_booking"), ACTIVE_URL, HISTORY_URL, reverse("arabela_admin:pending_approval")])
                self.assertIn(book[1], (reverse("arabela_admin:rental_schedule"), reverse("arabela_admin:calendar")))     # (some pages link the calendar's other address)
                self.assertIn("page === 'active' || page === 'rental-history' || page === 'pending'", html)
