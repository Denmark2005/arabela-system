"""Gown Monitoring (Inventory Management): what to prepare for pick-up and what is coming back, on the days chosen; the Ready mark and the customer's
news; the old Gown Board address; and the customer's own pages saying "Ready" only once staff marked it.

The money and booking rules are tested where they live (Active Reservations, the ledger); here: that the page lists the right gowns under the right
status, says why a gown can't be ready, never lets a gown be marked ready wrongly, tells the customer once, and stays safe and quick."""
import re
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditLog, CustomerMessage, UserProfile
from arabela_admin import dashboard_money
from arabela_admin.monitoring import gown_problems, prepare_rows, return_rows, selection_for
from arabela_admin.tests import _make_gown
from gowns.models import Gown, GownUnavailability
from reservations import reminders
from reservations.models import Reservation, ReservationItem
from reservations.test_ledger import make_booking

User = get_user_model()
URL = reverse("arabela_admin:gown_monitoring")
S = ReservationItem.Stage
EMAILS_ON = dict(CUSTOMER_EMAILS_ENABLED=True, CUSTOMER_EMAILS_BACKGROUND=False, SITE_BASE_URL="https://arabela.example.org",
                 DEFAULT_FROM_EMAIL="Arabela <shop@gmail.com>")


def long_day(day):
    return f"{day:%A}, {day:%B} {day.day}, {day.year}"


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("mo_staff", password="x", is_staff=True, first_name="Maria", last_name="Cruz")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.STAFF})
        cls.customer = User.objects.create_user("mo_juan", "mo.juan@gmail.com", "x", first_name="Juan")

    def setUp(self):
        self.today = timezone.localdate()
        self.client.force_login(self.staff)
        self.n = 0

    def day(self, offset):
        return self.today + timedelta(days=offset)

    def gown(self, **kw):
        self.n += 1
        return _make_gown(self.n, **kw)

    def book(self, pickup, ret, stage=S.PICKUP, *, gown=None, name=None, customer=None, **kw):
        """One booking with one gown, dates as day offsets. Returns the gown line."""
        booking = make_booking(pickup=pickup, ret=ret, stage=stage, customer=customer, **kw)
        if name:
            Reservation.objects.filter(pk=booking.pk).update(customer_name=name)
        if gown is not None:
            ReservationItem.objects.filter(reservation=booking).update(gown=gown, gown_name=gown.name)
        return ReservationItem.objects.select_related("reservation").get(reservation=booking)

    def rest_block(self, item, start, end, reason=GownUnavailability.Reason.COOLDOWN, own=True):
        return GownUnavailability.objects.create(gown=item.gown, start_date=self.day(start), end_date=self.day(end), reason=reason,
                                                 auto_for_item=item if own else None)

    def lines(self, tab="prepare", **params):
        selection = selection_for(params, self.today)
        return prepare_rows(selection, self.today, {}) if tab == "prepare" else return_rows(selection, self.today, {})

    def ready(self, item, **kw):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse("arabela_admin:reservation_item_ready", args=[item.pk]), data="{}", content_type="application/json", **kw)

    def unready(self, item):
        return self.client.post(reverse("arabela_admin:reservation_item_unready", args=[item.pk]), data="{}", content_type="application/json")

    def page(self, query=""):
        response = self.client.get(URL + query)
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def row_html(self, html, item):
        start = html.index(f'data-item-id="{item.pk}"')
        end = html.find('<div class="mo-row', start + 10)
        return html[start:end if end != -1 else html.index('</main>', start)]


# ------------------------------------------------------------------------------------------------------------------ the days
class SelectionTests(SimpleTestCase):
    TODAY = date(2026, 11, 26)

    def pick(self, **params):
        s = selection_for(params, self.TODAY)
        return s["mode"], s["start"], s["end"]

    def test_today_tomorrow_and_the_next_three_days(self):
        t = self.TODAY
        self.assertEqual(self.pick(), ("today", t, t))
        self.assertEqual(self.pick(when="tomorrow"), ("tomorrow", t + timedelta(1), t + timedelta(1)))
        self.assertEqual(self.pick(when="next3"), ("next3", t, t + timedelta(2)))

    def test_one_exact_day(self):
        t = self.TODAY
        self.assertEqual(self.pick(date="2026-12-05"), ("date", date(2026, 12, 5), date(2026, 12, 5)))
        self.assertEqual(self.pick(date=t.isoformat()), ("today", t, t))
        self.assertEqual(self.pick(date="2026-11-27"), ("tomorrow", t + timedelta(1), t + timedelta(1)))
        self.assertEqual(self.pick(date="2026-12-05", when="next3")[0], "date")                   # the exact day wins

    def test_a_wrong_past_or_far_away_day_shows_today(self):
        for raw in ("nonsense", "2026-13-40", "11/26/2026", "<script>", "2026-11-25", "2040-01-01", "0002-11-26", "   "):
            with self.subTest(raw=raw):
                self.assertEqual(self.pick(date=raw), ("today", self.TODAY, self.TODAY))
        self.assertEqual(self.pick(when="yesterday"), ("today", self.TODAY, self.TODAY))

    def test_the_words_the_page_says_and_what_the_links_keep(self):
        labels = {k: selection_for(p, self.TODAY) for k, p in
                  (("today", {}), ("tomorrow", {"when": "tomorrow"}), ("next3", {"when": "next3"}), ("date", {"date": "2027-01-02"}))}
        self.assertEqual(labels["today"]["label"], "Today · Thursday, November 26, 2026")
        self.assertEqual(labels["tomorrow"]["label"], "Tomorrow · Friday, November 27, 2026")
        self.assertEqual(labels["next3"]["label"], "Next 3 days · Nov 26 – Nov 28")
        self.assertEqual(labels["date"]["label"], "Saturday, January 2, 2027")
        self.assertEqual([labels[k]["words"] for k in labels], ["today", "tomorrow", "in the next 3 days", "on Jan 2, 2027"])
        self.assertEqual([labels[k]["query"] for k in labels], [{}, {"when": "tomorrow"}, {"when": "next3"}, {"date": "2027-01-02"}])


# ------------------------------------------------------------------------------------------------------------------ to prepare
class ToPrepareTests(Base):
    def test_a_gown_collected_today_is_to_prepare_and_can_be_marked(self):
        item = self.book(0, 3, gown=self.gown(), name="Ana Cruz")
        [row] = self.lines()
        self.assertEqual((row["id"], row["status"], row["status_word"], row["can_ready"], row["date_text"], row["customer"]),
                         (item.pk, "prepare", "To prepare", True, "Today", "Ana Cruz"))

    def test_only_gowns_waiting_on_a_running_booking_are_listed(self):
        self.book(0, 3, S.RESERVED)                                                           # picked up
        self.book(0, 3, S.CANCELLED)
        for status in (Reservation.Status.PENDING, Reservation.Status.REJECTED, Reservation.Status.CANCELLED):
            self.book(0, 3, status=status)
        self.assertEqual(self.lines(), [])

    def test_each_choice_of_days_lists_that_days_pick_ups(self):
        today, tomorrow, third, fourth = (self.book(n, n + 4) for n in (0, 1, 2, 3))
        ids = lambda rows: [r["id"] for r in rows]
        self.assertEqual(ids(self.lines()), [today.pk])
        self.assertEqual(ids(self.lines(when="tomorrow")), [tomorrow.pk])
        self.assertEqual(ids(self.lines(when="next3")), [today.pk, tomorrow.pk, third.pk])
        self.assertEqual(ids(self.lines(date=self.day(3).isoformat())), [fourth.pk])

    def test_a_gown_can_be_marked_from_two_days_before_its_pick_up(self):
        self.book(2, 5)
        far = self.book(3, 6)
        [near_row] = self.lines(date=self.day(2).isoformat())
        [far_row] = self.lines(date=self.day(3).isoformat())
        self.assertTrue(near_row["can_ready"])
        self.assertEqual((far_row["can_ready"], far_row["ready_from"]), (False, f"{self.day(1):%b} {self.day(1).day}" if self.day(1).year == self.today.year else far_row["ready_from"]))
        response, html = self.page(f"?date={self.day(3).isoformat()}")
        self.assertIn(f"Mark ready from {far_row['ready_from']}", self.row_html(html, far))
        self.assertNotIn("data-mo-ready", self.row_html(html, far))

    def test_a_gown_that_needs_repair_or_is_out_of_stock_is_a_problem(self):
        repair = self.book(0, 3, gown=self.gown(condition=Gown.Condition.NEEDS_REPAIR))
        gone = self.book(0, 3, gown=self.gown(status=Gown.Status.OUT_OF_STOCK))
        rows = {r["id"]: r for r in self.lines()}
        self.assertEqual((rows[repair.pk]["status"], rows[repair.pk]["detail"]), ("problem", "Needs repair"))
        self.assertEqual((rows[gone.pk]["status"], rows[gone.pk]["detail"]), ("problem", "Marked Out-of-Stock"))

    def test_a_gown_the_last_customer_has_not_returned_is_a_problem(self):
        gown = self.gown()
        self.book(-9, -2, S.RESERVED, gown=gown, name="Carlo Diaz")                            # was due 2 days ago, still out
        mine = self.book(1, 4, gown=gown)
        [row] = self.lines(when="tomorrow")
        self.assertEqual(row["id"], mine.pk)
        self.assertEqual(row["detail"], f"Not returned: Carlo Diaz was due {self.day(-2):%b} {self.day(-2).day}")

    def test_a_gown_another_customer_has_on_the_pick_up_day_is_a_problem_but_one_due_back_before_it_is_not(self):
        busy_gown, fine_gown = self.gown(), self.gown()
        self.book(-3, 2, S.RESERVED, gown=busy_gown, name="Liza Reyes")                        # still out on day 2
        self.book(-3, 1, S.RESERVED, gown=fine_gown)                                          # back (on time) before day 2
        busy = self.book(2, 5, gown=busy_gown)
        fine = self.book(2, 5, gown=fine_gown)
        rows = {r["id"]: r for r in self.lines(date=self.day(2).isoformat())}
        self.assertEqual(rows[busy.pk]["detail"], f"With Liza Reyes until {self.day(2):%b} {self.day(2).day}")
        self.assertEqual(rows[fine.pk]["status"], "prepare")

    def test_a_block_on_the_pick_up_day_is_a_problem_and_its_own_rest_days_are_not(self):
        cleaning_gown, own_gown, after_gown = self.gown(), self.gown(), self.gown()
        blocked = self.book(1, 4, gown=cleaning_gown)
        GownUnavailability.objects.create(gown=cleaning_gown, start_date=self.day(-1), end_date=self.day(2), reason=GownUnavailability.Reason.CLEANING)
        own = self.book(1, 4, gown=own_gown)
        self.rest_block(own, 0, 2)                                                           # a leftover block of this very booking never blocks it
        later = self.book(1, 4, gown=after_gown)
        GownUnavailability.objects.create(gown=after_gown, start_date=self.day(5), end_date=self.day(6), reason=GownUnavailability.Reason.REPAIR)
        rows = {r["id"]: r for r in self.lines(when="tomorrow")}
        self.assertEqual(rows[blocked.pk]["detail"], f"Being cleaned until {self.day(2):%b} {self.day(2).day}")
        self.assertEqual((rows[own.pk]["status"], rows[later.pk]["status"]), ("prepare", "prepare"))

    def test_a_booking_without_a_catalog_gown_is_simply_to_prepare(self):
        self.book(0, 3)
        self.assertEqual([r["status"] for r in self.lines()], ["prepare"])

    def test_the_earliest_day_first_then_problem_to_prepare_ready(self):
        later = self.book(1, 4, name="Zed")
        ready = self.book(0, 3, name="Bea")
        self.assertEqual(self.ready(ready).status_code, 200)
        waiting = self.book(0, 3, name="Cara")
        problem = self.book(0, 3, gown=self.gown(condition=Gown.Condition.NEEDS_REPAIR), name="Dan")
        self.assertEqual([r["id"] for r in self.lines(when="next3")], [problem.pk, waiting.pk, ready.pk, later.pk])

    def test_a_ready_gown_that_gets_a_problem_shows_the_problem(self):
        gown = self.gown()
        item = self.book(0, 3, gown=gown)
        self.assertEqual(self.ready(item).status_code, 200)
        Gown.objects.filter(pk=gown.pk).update(condition=Gown.Condition.NEEDS_REPAIR)
        [row] = self.lines()
        self.assertEqual((row["status"], row["detail"]), ("problem", "Needs repair (it was marked ready)"))

    def test_a_moved_pick_up_day_makes_it_to_prepare_again(self):
        item = self.book(0, 3, customer=self.customer)
        self.assertEqual(self.ready(item).status_code, 200)
        self.assertEqual(self.lines()[0]["status"], "ready")
        ReservationItem.objects.filter(pk=item.pk).update(rental_date=self.day(1))
        [row] = self.lines(when="tomorrow")
        self.assertEqual(row["status"], "prepare")
        self.assertFalse(ReservationItem.objects.get(pk=item.pk).is_ready_for_pickup)


# ------------------------------------------------------------------------------------------------------------------ for return
class ForReturnTests(Base):
    def test_a_late_gown_is_listed_on_every_day_with_its_days_late(self):
        item = self.book(-9, -3, S.RESERVED)
        for params in ({}, {"when": "tomorrow"}, {"date": self.day(20).isoformat()}):
            with self.subTest(params=params):
                [row] = self.lines("return", **params)
                self.assertEqual((row["id"], row["status"], row["detail"], row["date_label"]), (item.pk, "late", "3 days overdue", "Was due"))

    def test_days_overdue_count_from_the_return_day_agreed_now(self):
        item = self.book(-9, -1, S.RESERVED, original_return=-4)                            # moved 3 days later, still not back
        self.assertEqual(self.lines("return")[0]["detail"], "1 day overdue")
        self.assertEqual(self.lines("return")[0]["id"], item.pk)

    def test_due_back_on_the_chosen_days_only(self):
        today = self.book(-3, 0, S.RESERVED)
        tomorrow = self.book(-3, 1, S.RETURN)
        self.book(0, 1)                                                                      # not picked up yet: nothing to come back
        ids = lambda params: [(r["id"], r["status"]) for r in self.lines("return", **params)]
        self.assertEqual(ids({}), [(today.pk, "due")])
        self.assertEqual(ids({"when": "tomorrow"}), [(tomorrow.pk, "due")])
        self.assertEqual(ids({"when": "next3"}), [(today.pk, "due"), (tomorrow.pk, "due")])

    def test_a_returned_gown_is_cleaning_until_its_rest_days_end(self):
        item = self.book(-4, 0, S.RETURNED, gown=self.gown(), returned_on=0)
        self.rest_block(item, 1, 5)
        until = f"{self.day(5):%b} {self.day(5).day}"
        for params in ({}, {"when": "tomorrow"}, {"date": self.day(5).isoformat()}):
            with self.subTest(params=params):
                [row] = self.lines("return", **params)
                self.assertEqual((row["status"], row["detail"], row["date_label"]), ("cleaning", f"Resting until {until}", "Returned"))
        self.assertEqual(self.lines("return", date=self.day(6).isoformat()), [])

    def test_released_rest_days_take_it_off_the_list(self):
        item = self.book(-4, 0, S.RETURNED, gown=self.gown(), returned_on=0)
        self.rest_block(item, 1, 5).delete()
        self.assertEqual(self.lines("return"), [])

    def test_a_gown_returned_needing_repair_stays_until_it_is_fixed(self):
        gown = self.gown(condition=Gown.Condition.NEEDS_REPAIR, status=Gown.Status.OUT_OF_STOCK)
        item = self.book(-20, -12, S.RETURNED, gown=gown, returned_on=-12)
        ReservationItem.objects.filter(pk=item.pk).update(return_condition=ReservationItem.ReturnCondition.NEEDS_REPAIR)
        self.rest_block(item, -11, -7)
        [row] = self.lines("return")
        self.assertEqual((row["status"], row["detail"]), ("cleaning", "Needs repair"))
        self.assertEqual(self.lines("return", when="tomorrow"), [])                            # a repair is a fact about now, not a date
        Gown.objects.filter(pk=gown.pk).update(condition=Gown.Condition.GOOD, status=Gown.Status.AVAILABLE)
        self.assertEqual(self.lines("return"), [])

    def test_late_first_then_due_then_repair_then_resting(self):
        resting = self.book(-4, 0, S.RETURNED, gown=self.gown(), returned_on=0, name="Rest")
        self.rest_block(resting, 1, 5)
        gown = self.gown(condition=Gown.Condition.NEEDS_REPAIR)
        broken = self.book(-5, -1, S.RETURNED, gown=gown, returned_on=-1, name="Broke")
        ReservationItem.objects.filter(pk=broken.pk).update(return_condition=ReservationItem.ReturnCondition.NEEDS_REPAIR)
        due = self.book(-3, 0, S.RESERVED, name="Due")
        late = self.book(-9, -2, S.RESERVED, name="Late")
        later = self.book(-12, -6, S.RESERVED, name="Later")
        self.assertEqual([r["id"] for r in self.lines("return")], [later.pk, late.pk, due.pk, broken.pk, resting.pk])

    def test_a_finished_booking_opens_in_reservation_records_a_running_one_in_active_reservations(self):
        done = self.book(-4, 0, S.RETURNED, gown=self.gown(), returned_on=0)
        self.rest_block(done, 1, 5)
        running = self.book(-3, 0, S.RESERVED)
        rows = {r["id"]: r for r in self.lines("return")}
        self.assertEqual(rows[done.pk]["booking_url"], f'{reverse("arabela_admin:reservation_records")}?search={done.reservation.reference_code}')
        self.assertEqual(rows[running.pk]["booking_url"], f'{reverse("arabela_admin:active_reservations")}?search={running.reservation.reference_code}')


# ------------------------------------------------------------------------------------------------------------------ the ready mark
class ReadyMarkTests(Base):
    def test_marking_ready_saves_who_and_when_and_tells_the_customer_once(self):
        item = self.book(1, 4, gown=self.gown(), customer=self.customer, name="Juan Tolentino")
        response = self.ready(item)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"success": True, "told": True, "message": f"{item.gown.name} is ready. Juan Tolentino was told."})
        item.refresh_from_db()
        self.assertEqual((item.ready_by, item.ready_for, item.ready_told_for), (self.staff, item.rental_date, item.rental_date))
        self.assertIsNotNone(item.ready_at)
        self.assertTrue(item.is_ready_for_pickup)
        [message] = CustomerMessage.objects.filter(recipient=self.customer)
        self.assertEqual(message.body, f"Good news! {item.gown.name} is ready for pick-up at Arabela on {long_day(self.day(1))} (1:00 PM to 5:00 PM only). "
                                       f"Reference: {item.reservation.reference_code}.")
        self.assertEqual((message.category, message.is_read), (CustomerMessage.Category.GENERAL, False))
        again = self.ready(item)
        self.assertEqual(again.status_code, 409)
        self.assertIn("already marked ready by Maria Cruz", again.json()["error"])
        self.assertEqual(CustomerMessage.objects.filter(recipient=self.customer).count(), 1)
        [row] = self.lines(when="tomorrow")
        self.assertEqual(row["status"], "ready")
        self.assertRegex(row["detail"], r"^by Maria Cruz, \d{1,2}:\d{2} [AP]M$")

    def test_the_gowns_of_one_booking_share_one_message_that_comes_back_unread(self):
        booking = make_booking(pickup=0, ret=3, gowns=2, customer=self.customer)
        first, second = booking.items.order_by("id")
        self.assertEqual(self.ready(first).status_code, 200)
        CustomerMessage.objects.update(is_read=True)
        self.assertEqual(self.ready(second).status_code, 200)
        [message] = CustomerMessage.objects.filter(recipient=self.customer)
        self.assertIn(f"{first.gown_name} and {second.gown_name} are ready for pick-up", message.body)
        self.assertFalse(message.is_read)

    def test_a_walk_in_without_a_website_account_is_not_messaged_and_staff_are_told_to_tell_them(self):
        item = self.book(0, 3)
        response = self.ready(item)
        self.assertEqual((response.status_code, response.json()["told"]), (200, False))
        self.assertIn("no website account, so let them know yourself", response.json()["message"])
        self.assertEqual(CustomerMessage.objects.count(), 0)
        self.assertIsNone(ReservationItem.objects.get(pk=item.pk).ready_told_for)

    def test_taking_it_back_and_marking_again_never_sends_the_news_twice(self):
        item = self.book(0, 3, customer=self.customer)
        self.ready(item)
        response = self.unready(item)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["told"])
        self.assertIn("already told it was ready, so let them know", response.json()["message"])
        item.refresh_from_db()
        self.assertEqual((item.ready_at, item.ready_by, item.ready_for, item.ready_told_for), (None, None, None, item.rental_date))
        self.assertEqual(CustomerMessage.objects.count(), 1)                                   # nothing is sent on taking it back
        again = self.ready(item)
        self.assertEqual((again.status_code, again.json()["told"]), (200, False))
        self.assertIn("already told about this pick-up", again.json()["message"])
        self.assertEqual(CustomerMessage.objects.count(), 1)

    def test_a_new_pick_up_day_sends_new_news(self):
        item = self.book(0, 3, customer=self.customer)
        self.ready(item)
        ReservationItem.objects.filter(pk=item.pk).update(rental_date=self.day(1))
        response = self.ready(item)
        self.assertEqual((response.status_code, response.json()["told"]), (200, True))
        self.assertEqual(CustomerMessage.objects.count(), 2)
        self.assertIn(long_day(self.day(1)), CustomerMessage.objects.latest("id").body)

    def test_the_marks_that_are_refused(self):
        problem = self.book(0, 3, gown=self.gown(condition=Gown.Condition.NEEDS_REPAIR))
        far = self.book(3, 6)
        past = self.book(-1, 3)
        out = self.book(0, 3, S.RESERVED)
        pending = self.book(0, 3, status=Reservation.Status.PENDING)
        cases = (
            (problem, 409, "It can't be marked ready yet: Needs repair."),
            (far, 400, "It can be marked ready from"),
            (past, 409, "pick-up day has passed"),
            (out, 409, "no longer waiting for pick-up"),
            (pending, 409, "no longer waiting for pick-up"),
        )
        for item, status, words in cases:
            with self.subTest(words=words):
                response = self.ready(item)
                self.assertEqual(response.status_code, status, response.content)
                self.assertIn(words, response.json()["error"])
                self.assertIsNone(ReservationItem.objects.get(pk=item.pk).ready_at)
        missing = self.client.post(reverse("arabela_admin:reservation_item_ready", args=[999999]), data="{}", content_type="application/json")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(self.unready(far).status_code, 409)
        self.assertFalse(AuditLog.objects.filter(action__in=("item.ready", "item.unready")).exists())      # a refused press is not an action

    def test_only_staff_may_press_the_buttons_and_only_by_post(self):
        item = self.book(0, 3, customer=self.customer)
        url = reverse("arabela_admin:reservation_item_ready", args=[item.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        for user in (None, self.customer):
            self.client.logout()
            if user:
                self.client.force_login(user)
            for name in ("reservation_item_ready", "reservation_item_unready"):
                response = self.client.post(reverse(f"arabela_admin:{name}", args=[item.pk]), data="{}", content_type="application/json")
                self.assertEqual(response.status_code, 401)
        self.assertIsNone(ReservationItem.objects.get(pk=item.pk).ready_at)

    def test_every_press_is_in_the_audit_log(self):
        item = self.book(0, 3, customer=self.customer)
        self.ready(item)
        self.unready(item)
        code = item.reservation.reference_code
        ready, unready = AuditLog.objects.filter(action__in=("item.ready", "item.unready")).order_by("id")
        self.assertEqual(ready.summary, f"Maria Cruz marked {item.gown_name} on booking {code} ready for pick-up")
        self.assertEqual(unready.summary, f"Maria Cruz took back the ready mark on {item.gown_name} on booking {code}")
        self.assertEqual(ready.category, "Bookings")

    def test_marking_ready_tells_the_customers_pages_there_is_news(self):
        item = self.book(0, 3, customer=self.customer)
        before = ReservationItem.objects.get(pk=item.pk).updated_at
        self.ready(item)
        self.assertGreater(ReservationItem.objects.get(pk=item.pk).updated_at, before)


@override_settings(**EMAILS_ON)
class ReadyEmailTests(Base):
    def test_the_customer_gets_an_email_that_says_which_gown_and_when(self):
        item = self.book(1, 4, gown=self.gown(), customer=self.customer)
        mail.outbox.clear()
        self.assertEqual(self.ready(item).status_code, 200)
        [email] = mail.outbox
        self.assertEqual(email.to, ["mo.juan@gmail.com"])
        self.assertEqual(email.subject, f"Your gown is ready for pick-up - {item.reservation.reference_code}")
        self.assertIn(f"{item.gown.name} is ready for pick-up at Arabela on {long_day(self.day(1))}", email.body)
        mail.outbox.clear()
        self.unready(item)
        self.ready(item)
        self.assertEqual(mail.outbox, [])


# ------------------------------------------------------------------------------------------------------------------ the page
class PageTests(Base):
    def test_it_opens_on_today_with_the_two_tiles_the_days_and_the_table(self):
        item = self.book(0, 3, gown=self.gown(), name="Ana Cruz", customer=self.customer)
        response, html = self.page()
        self.assertEqual((response.context["tab"], response.context["selection"]["mode"]), ("prepare", "today"))
        for piece in ("Gown Monitoring | Arabela System Admin Panel", "pageName: `Gown Monitoring`", "To prepare", "For return", "Date:", ">Today</a>", ">Tomorrow</a>",
                      ">Next 3 days</a>", f'min="{self.today.isoformat()}"', "Gown &amp; tag", "Schedule", "Customer", "Status", "Action", "Ana Cruz",
                      "Mark ready", f'tel:{item.reservation.phone}', item.gown.gown_id):
            with self.subTest(piece=piece):
                self.assertIn(piece, html)
        row = self.row_html(html, item)
        self.assertIn('<span class="mo-chip mo-chip--prepare">To prepare</span>', row)
        self.assertIn(f'data-mo-ready="{reverse("arabela_admin:reservation_item_ready", args=[item.pk])}"', row)
        self.assertIn(f'data-gpw-name="{item.gown.name}"', row)
        self.assertIn('id="gpw"', html)                                                      # the photo window is on the page

    def test_the_tiles_count_their_lines_and_keep_the_days_in_their_links(self):
        self.book(1, 4, name="Ana")
        self.book(-3, 1, S.RESERVED, name="Ben")
        response, html = self.page("?when=tomorrow")
        self.assertEqual((response.context["prepare_count"], response.context["return_count"]), (1, 1))
        self.assertIn(f'href="{URL}?tab=return&amp;when=tomorrow"', html)
        self.assertIn(f'href="{URL}?when=tomorrow"', html)
        self.assertIn("Ana", html)
        self.assertNotIn("Ben", html[html.index('class="mo-list"'):])                          # the other list is not mixed in
        _, returns = self.page("?tab=return&when=tomorrow")
        self.assertIn('<input type="hidden" name="tab" value="return">', returns)
        self.assertIn("Ben", returns)
        self.assertIn(f'href="{URL}?tab=return"', returns)                                     # Today keeps the For return list

    def test_a_ready_line_offers_undo_and_asks_first_when_the_customer_was_told(self):
        item = self.book(0, 3, customer=self.customer, name="Juan")
        self.ready(item)
        _, html = self.page()
        row = self.row_html(html, item)
        self.assertIn('<span class="mo-chip mo-chip--ready">Ready</span>', row)
        self.assertIn('data-mo-told="1"', row)
        self.assertIn(f'data-mo-undo="{reverse("arabela_admin:reservation_item_unready", args=[item.pk])}"', row)

    def test_problem_and_return_lines_open_the_booking_and_cleaning_has_no_button(self):
        problem = self.book(0, 3, gown=self.gown(condition=Gown.Condition.NEEDS_REPAIR))
        _, html = self.page()
        row = self.row_html(html, problem)
        self.assertIn(">View booking</a>", row)
        self.assertNotIn("<button", row)
        late = self.book(-9, -2, S.RESERVED)
        cleaning = self.book(-4, 0, S.RETURNED, gown=self.gown(), returned_on=0)
        self.rest_block(cleaning, 1, 5)
        _, returns = self.page("?tab=return")
        self.assertIn(">View booking</a>", self.row_html(returns, late))
        self.assertNotIn(">View booking</a>", self.row_html(returns, cleaning))
        self.assertIn("2 days overdue", returns)

    def test_empty_days_say_so(self):
        _, html = self.page()
        self.assertIn("Nothing to prepare today.", html)
        self.assertIn("No pick-up today", html)
        _, returns = self.page(f"?tab=return&date={self.day(9).isoformat()}")
        self.assertIn(f"Nothing for return on {self.day(9):%b} {self.day(9).day}", returns)

    def test_a_wrong_date_in_the_address_never_breaks_the_page(self):
        for bad in ("nonsense", "2026-99-99", "%3Cscript%3E", "", "2099-01-01"):
            with self.subTest(bad=bad):
                response, _ = self.page(f"?date={bad}&tab=nope&when=zzz")
                self.assertEqual((response.context["selection"]["mode"], response.context["tab"]), ("today", "prepare"))

    def test_what_a_customer_typed_cannot_become_markup(self):
        item = self.book(0, 3)
        Reservation.objects.filter(pk=item.reservation_id).update(customer_name='<script>alert("x")</script>', phone='"><img src=x onerror=alert(1)>')
        ReservationItem.objects.filter(pk=item.pk).update(gown_name='<b>Gown</b> "Q"')
        _, html = self.page()
        for raw in ('<script>alert("x")</script>', "<img src=x onerror=alert(1)>", "<b>Gown</b>"):
            self.assertNotIn(raw, html)
        self.assertIn("&lt;script&gt;", html)

    def test_no_template_syntax_leaks_into_the_page_and_the_lists_update_themselves(self):
        self.book(0, 3)
        _, html = self.page()
        for leak in ("{%", "{{", "{#"):
            self.assertNotIn(leak, html)
        for name in ("monitoring-tabs", "monitoring-list"):
            self.assertEqual(html.count(f'data-live-region="{name}"'), 1)
        self.assertIn('data-arpg=".mo-row" data-arpg-noun="gowns"', html)

    def test_the_sidebar_shows_it_under_inventory_management_as_the_current_page_and_no_gown_board(self):
        _, html = self.page()
        self.assertIn(":class=\"page === 'gown-monitoring' ? 'menu-dropdown-item-active' : 'menu-dropdown-item-inactive'\"", html)
        self.assertIn("page: 'gown-monitoring'", html)
        self.assertIn("page === 'gown' || page === 'gown-monitoring') { selected = 'InventoryManagement' }", html)
        self.assertNotIn("Gown Board", html)
        for name in ("dashboard", "active_reservations", "gown_catalog"):
            with self.subTest(page=name):
                other = self.client.get(reverse(f"arabela_admin:{name}")).content.decode()
                self.assertIn(f'href="{URL}"', other)
                self.assertNotIn("Gown Board", other)

    def test_every_admin_page_template_has_the_new_menu_line_once(self):
        from pathlib import Path
        from django.conf import settings
        found = 0
        for path in sorted((Path(settings.BASE_DIR) / "templates" / "arabela_admin").glob("*.html")):
            text = path.read_text(encoding="utf8")
            if "Menu Item Inventory Management" not in text:
                continue
            found += 1
            with self.subTest(page=path.name):
                self.assertEqual(text.count("href=\"{% url 'arabela_admin:gown_monitoring' %}\""), 1)        # the menu line (the page's date form uses it too)
                self.assertNotIn("gown_board", text)
                self.assertIn("page === 'gown-monitoring') { selected = 'InventoryManagement' }", text)
        self.assertEqual(found, 18)

    def test_the_old_gown_board_address_opens_gown_monitoring(self):
        response = self.client.get(reverse("arabela_admin:gown_board"))
        self.assertRedirects(response, URL, fetch_redirect_response=False)
        response = self.client.get(reverse("arabela_admin:gown_board") + "?date=2026-12-01")
        self.assertRedirects(response, URL + "?date=2026-12-01", fetch_redirect_response=False)

    def test_the_dashboard_late_warning_opens_the_for_return_list(self):
        [alert] = dashboard_money.alerts({"late_gowns": 2, "pickups_owing": 0, "online_attention": 0})
        self.assertEqual(alert["url"], URL + "?tab=return")

    def test_it_is_closed_to_everyone_but_staff(self):
        self.client.logout()
        self.assertEqual(self.client.get(URL).status_code, 302)
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(URL).status_code, 302)
        self.assertEqual(self.client.get(reverse("arabela_admin:gown_board")).status_code, 302)


class FewQuestionsTests(Base):
    def queries(self, query=""):
        self.client.get(URL + query)                                                          # the first visit also creates the shop-settings row
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(URL + query).status_code, 200)
        return len(captured.captured_queries)

    def fill(self):
        gown = self.gown()
        self.book(0, 3, gown=gown, customer=self.customer)
        ready = self.book(0, 3, gown=self.gown())
        self.ready(ready)
        self.book(-8, -2, S.RESERVED, gown=self.gown())
        returned = self.book(-4, 0, S.RETURNED, gown=self.gown(), returned_on=0)
        self.rest_block(returned, 1, 5)

    def test_more_gowns_do_not_mean_more_questions_of_the_database(self):
        self.fill()
        few = (self.queries(), self.queries("?tab=return&when=next3"))
        for _ in range(8):
            self.fill()
        self.assertEqual((self.queries(), self.queries("?tab=return&when=next3")), few)


# ------------------------------------------------------------------------------------------------------------------ the customer's side
class CustomerSideTests(Base):
    def test_the_customers_pages_say_ready_only_once_staff_marked_it(self):
        item = self.book(0, 3, customer=self.customer)
        self.client.force_login(self.customer)
        listing = self.client.get(reverse("gowns:orders")).content.decode()
        detail = self.client.get(reverse("gowns:reservation_item_detail", args=[item.pk])).content.decode()
        for html in (listing, detail):
            self.assertIn("Awaiting Pick-up", html)
            self.assertNotIn("Ready for Pick-up", html)
        self.client.force_login(self.staff)
        self.ready(item)
        self.client.force_login(self.customer)
        listing = self.client.get(reverse("gowns:orders")).content.decode()
        detail = self.client.get(reverse("gowns:reservation_item_detail", args=[item.pk])).content.decode()
        for html in (listing, detail):
            self.assertIn("Ready for Pick-up", html)
            self.assertNotIn("Awaiting Pick-up", html)

    def test_the_pick_up_day_reminder_no_longer_claims_the_gown_is_ready(self):
        one = self.book(0, 3)
        body = reminders._body_for(reminders.PICKUP_TODAY, one, self.today)
        self.assertIn(f"Please pick up your gown at Arabela today, until 5:00 PM.", body)
        self.assertNotIn("ready", body.lower())
        booking = make_booking(pickup=0, ret=3, gowns=2)
        grouped = reminders._group_body_for(reminders.PICKUP_TODAY, list(booking.items.all()), self.today)
        self.assertIn("Please pick up your gowns at Arabela today, until 5:00 PM.", grouped)
        self.assertNotIn("ready", grouped.lower())

    def test_the_problem_check_asks_nothing_for_gowns_that_are_not_in_the_catalog(self):
        item = self.book(0, 3)
        with self.assertNumQueries(0):
            self.assertEqual(gown_problems([item], self.today), {})
