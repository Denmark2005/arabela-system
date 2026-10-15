"""The event-date rules: the customer picks an EVENT date, the gown comes back 2 days later and rests 5 days; the pick-up is the earliest
free day up to 2 days before the event, or the event day itself (1 PM - 5 PM only) when those days are taken. One planner
(gowns.availability) answers for the gown page, checkout and the admin's "release blocked dates" reminder -- these tests pin
the rules themselves, prove the page and checkout agree, and cover the staff reminder."""
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from gowns import availability
from gowns.models import COOLDOWN_DAYS, Gown, GownUnavailability
from gowns.views import _UNIT_ASSIGNED, _UNIT_DATE_PASSED, _UNIT_TODAY_CLOSED, _UNIT_UNAVAILABLE, _plan_pickup
from reservations.models import Reservation, ReservationItem

User = get_user_model()
_TINY_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
D = timedelta


def at(day, hour, minute=0):
    """A shop-time moment on `day` -- the fake 'now' the clock is set to."""
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


def clock(now):
    return patch("gowns.availability.now_local", return_value=now)


def make_unit(n, size=Gown.Size.MEDIUM, name="Event Gown", status=Gown.Status.AVAILABLE):
    return Gown.objects.create(
        gown_id=f"EVENT-{n:04d}", name=name, category=Gown.Category.WEDDING_GOWN, color_name="White", color_code="WH",
        size=size, rental_price=Decimal("5000"), status=status)


def book(unit, start, end, username=None, status=Reservation.Status.CONFIRMED, stage=None):
    """A customer booking of `unit` from `start` to `end` (pick-up .. return), no cooldown block."""
    customer = User.objects.create_user(username=username or f"booker_{unit.id}_{start:%m%d}_{end:%m%d}", password="x")
    reservation = Reservation.objects.create(customer=customer, customer_name="Booked", status=status)
    extra = {"stage": stage} if stage else {}
    return ReservationItem.objects.create(
        reservation=reservation, gown=unit, gown_name=unit.name, rental_date=start, event_date=end - D(days=2),
        return_date=end, overdue_date=end, **extra)


def block(unit, start, end, reason=GownUnavailability.Reason.CLEANING):
    return GownUnavailability.objects.create(gown=unit, start_date=start, end_date=end, reason=reason)


class PlannerRulesTests(SimpleTestCase):
    """The pure planning functions -- plain dates, no database."""
    today = date(2026, 11, 2)

    def plan(self, event, bookings=(), blocks=(), now=None):
        now = now or at(self.today, 10)
        return availability.plan_unit(event, list(bookings), list(blocks), availability.earliest_pickup_day(now))

    def test_a_rental_needs_eight_days_in_a_row(self):
        self.assertEqual(COOLDOWN_DAYS, 5)
        self.assertEqual(availability.RETURN_DAYS, 2)
        self.assertEqual(availability.rental_days_needed(), 8)       # the event day + 2 return days + 5 cleaning days

    def test_the_shop_closes_for_pickups_at_five(self):
        self.assertEqual(availability.earliest_pickup_day(at(self.today, 16, 59)), self.today)
        self.assertEqual(availability.earliest_pickup_day(at(self.today, 17, 0)), self.today + D(days=1))
        self.assertEqual(availability.earliest_pickup_day(at(self.today, 23, 59)), self.today + D(days=1))

    def test_why_a_date_can_never_be_booked(self):
        morning, evening = at(self.today, 10), at(self.today, 17, 0)
        self.assertEqual(availability.day_problem(self.today - D(days=1), morning), "past")
        self.assertIsNone(availability.day_problem(self.today, morning))
        self.assertEqual(availability.day_problem(self.today, evening), "closed")
        self.assertIsNone(availability.day_problem(self.today + D(days=1), evening))   # tomorrow, same-day pick-up

    def test_a_free_gown_is_picked_up_two_days_before_the_event(self):
        event = self.today + D(days=20)
        self.assertEqual(self.plan(event), (event - D(days=2), None))

    def test_pickup_never_starts_before_today(self):
        self.assertEqual(self.plan(self.today), (self.today, None))                       # event today: pick up today
        self.assertEqual(self.plan(self.today + D(days=1)), (self.today, None))           # event tomorrow: today is allowed
        evening = at(self.today, 18)
        self.assertEqual(self.plan(self.today + D(days=1), now=evening), (self.today + D(days=1), None))

    def test_taken_days_before_the_event_push_the_pickup_later_up_to_the_event_day(self):
        event = self.today + D(days=30)
        for last_taken, pickup in ((event - D(days=3), event - D(days=2)), (event - D(days=2), event - D(days=1)),
                                   (event - D(days=1), event)):
            with self.subTest(last_taken=last_taken):
                self.assertEqual(self.plan(event, blocks=[(event - D(days=12), last_taken)]), (pickup, None))

    def test_a_rental_that_touches_a_booking_or_a_block_is_refused(self):
        event = self.today + D(days=30)
        self.assertEqual(self.plan(event, bookings=[(event, event + D(days=1))]), (None, "booked"))     # the event day is taken
        self.assertEqual(self.plan(event, blocks=[(event + D(days=2), event + D(days=2))]), (None, "booked"))   # the return day is
        self.assertEqual(self.plan(event, blocks=[(event - D(days=10), event)]), (None, "booked"))

    def test_the_first_event_after_a_rental_is_eight_days_after_the_earlier_event(self):
        first = self.today + D(days=10)
        # the earlier customer: pick-up first-2, return first+2, then 5 cleaning days (the automatic cooldown block)
        bookings = [(first - D(days=2), first + D(days=2))]
        blocks = [(first + D(days=3), first + D(days=2 + COOLDOWN_DAYS))]
        for later in range(1, 8):
            self.assertEqual(self.plan(first + D(days=later), bookings, blocks)[1], "booked", later)
        self.assertEqual(self.plan(first + D(days=8), bookings, blocks), (first + D(days=8), None))   # same-day pick-up
        self.assertEqual(self.plan(first + D(days=9), bookings, blocks), (first + D(days=8), None))   # earliest free day

    def test_rest_days_may_not_run_into_the_next_customers_booking(self):
        later = self.today + D(days=40)
        bookings = [(later, later + D(days=4))]
        self.assertEqual(self.plan(later - D(days=8), bookings), (later - D(days=10), None))          # 8 free days: fits
        self.assertEqual(self.plan(later - D(days=7), bookings), (None, "rest"))                      # 7 free days: one short
        self.assertEqual(self.plan(later - D(days=1), bookings), (None, "booked"))

    def test_rest_days_may_overlap_a_staff_block(self):
        event = self.today + D(days=20)
        self.assertEqual(self.plan(event, blocks=[(event + D(days=4), event + D(days=6))]), (event - D(days=2), None))

    def test_the_calendar_is_per_size_with_a_free_unit_count(self):
        small, large = make_unit_stub(1, "S"), make_unit_stub(2, "L")
        event = self.today + D(days=12)
        bookings = {large.id: [(event - D(days=2), event + D(days=2))]}
        plans = availability.plan_calendar([small, large], bookings, {}, at(self.today, 10), 30, lambda unit: unit.code)
        self.assertEqual(set(plans), {"*", "S", "L"})
        self.assertEqual(plans["S"][event.isoformat()], [(event - D(days=2)).isoformat(), 1])
        self.assertEqual(plans["L"][event.isoformat()], "booked")
        self.assertEqual(plans["*"][event.isoformat()], [(event - D(days=2)).isoformat(), 1])   # one of the two is free
        self.assertEqual(plans["*"][self.today.isoformat()][1], 2)

    def test_today_is_open_until_five_and_closed_after(self):
        unit = make_unit_stub(1, "")
        before = availability.plan_calendar([unit], {}, {}, at(self.today, 16, 59), 3, lambda u: "")
        after = availability.plan_calendar([unit], {}, {}, at(self.today, 17, 0), 3, lambda u: "")
        today, tomorrow = self.today.isoformat(), (self.today + D(days=1)).isoformat()
        self.assertEqual(before["*"][today], [today, 1])
        self.assertEqual(after["*"][today], "closed")
        self.assertEqual(after["*"][tomorrow], [tomorrow, 1])                      # event tomorrow, picked up tomorrow

    def test_open_event_dates_lists_only_the_bookable_ones(self):
        unit = make_unit_stub(1, "")
        event = self.today + D(days=15)
        open_days = availability.open_event_dates(
            [unit], {unit.id: [(event - D(days=2), event + D(days=2))]}, {}, at(self.today, 10), 20)
        self.assertNotIn(event.isoformat(), open_days)
        self.assertIn((event + D(days=3)).isoformat(), open_days)   # the cleaning days are not in the booking itself


class _Stub:
    def __init__(self, id, code):
        self.id, self.code = id, code


def make_unit_stub(id, code):
    return _Stub(id, code)


class CheckoutPlanTests(TestCase):
    """What the server books when the customer submits an event date."""

    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="event_customer", password="x")
        cls.other = User.objects.create_user(username="event_customer_two", password="x")

    def setUp(self):
        patcher = patch("gowns.views._save_proof_file", return_value="https://example.test/fake-proof.jpg")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.today = timezone.localdate()
        self.unit = make_unit(1)

    def submit(self, event, user=None, unit=None):
        self.client.force_login(user or self.customer)
        unit = unit or self.unit
        items = [{"gown_name": unit.name, "gown_slug": unit.slug, "size": "M",
                  "rental_date": (event - D(days=2)).isoformat(), "return_date": (event + D(days=2)).isoformat()}]
        return self.client.post(reverse("gowns:reservation_submit"), data={
            "items": json.dumps(items), "first_name": "Event", "last_name": "Buyer", "phone": "09171234567",
            "address": "1 Test St", "city": "Test City", "postal_code": "1000", "payment_method": "GCash",
            "proof_of_payment": SimpleUploadedFile("proof.jpg", _TINY_JPEG, content_type="image/jpeg")})

    def saved(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        return ReservationItem.objects.get(reservation__reference_code=response.json()["reference_code"])

    def test_a_free_gown_is_picked_up_two_days_before_and_rests_five_days_after_the_return(self):
        event = self.today + D(days=20)
        with clock(at(self.today, 10)):
            item = self.saved(self.submit(event))
        self.assertEqual((item.rental_date, item.return_date), (event - D(days=2), event + D(days=2)))
        rest = GownUnavailability.objects.get(auto_for_item=item)
        self.assertEqual((rest.start_date, rest.end_date), (event + D(days=3), event + D(days=7)))

    def test_the_next_customer_gets_a_same_day_pickup_on_the_first_free_day(self):
        first = self.today + D(days=20)
        with clock(at(self.today, 10)):
            self.saved(self.submit(first))
            refused = self.submit(first + D(days=7), user=self.other)                    # still resting
            self.assertEqual(refused.status_code, 400)
            self.assertIn("is no longer available for", refused.json()["error"])
            self.assertIn((first + D(days=7)).strftime("%b %d, %Y"), refused.json()["error"])
            item = self.saved(self.submit(first + D(days=8), user=self.other))
        self.assertEqual(item.rental_date, first + D(days=8))                             # picked up on the event day itself
        self.assertEqual(item.return_date, first + D(days=10))

    def test_a_later_event_still_picks_up_early_once_the_days_before_are_free(self):
        first = self.today + D(days=20)
        with clock(at(self.today, 10)):
            self.saved(self.submit(first))
            item = self.saved(self.submit(first + D(days=9), user=self.other))
        self.assertEqual(item.rental_date, first + D(days=8))                             # the earliest free day, 1 day early

    def test_the_rest_days_may_not_run_into_a_later_customers_booking(self):
        later = self.today + D(days=40)
        book(self.unit, later, later + D(days=4))
        with clock(at(self.today, 10)):
            too_late = self.submit(later - D(days=7))
            self.assertEqual(too_late.status_code, 400)                                    # 7 days free: one short
            self.assertFalse(ReservationItem.objects.filter(reservation__customer=self.customer).exists())
            item = self.saved(self.submit(later - D(days=8)))                              # 8 days free: fits
        self.assertEqual(item.rental_date, later - D(days=10))

    def test_the_rest_days_may_overlap_a_staff_block(self):
        event = self.today + D(days=20)
        block(self.unit, event + D(days=4), event + D(days=6))
        with clock(at(self.today, 10)):
            self.saved(self.submit(event))

    def test_a_cancelled_or_returned_booking_holds_nothing(self):
        event = self.today + D(days=20)
        book(self.unit, event - D(days=2), event + D(days=2), username="was_cancelled", status=Reservation.Status.CANCELLED)
        book(self.unit, event - D(days=2), event + D(days=2), username="was_returned", stage=ReservationItem.Stage.RETURNED)
        with clock(at(self.today, 10)):
            self.saved(self.submit(event))

    def test_a_date_that_has_already_passed_is_refused_with_a_clear_message(self):
        with clock(at(self.today, 10)):
            response = self.submit(self.today - D(days=1))
        self.assertEqual(response.status_code, 400)
        self.assertIn("has already passed", response.json()["error"])
        self.assertFalse(Reservation.objects.exists())

    def test_today_can_be_booked_until_five_and_is_refused_from_five(self):
        with clock(at(self.today, 16, 59)):
            item = self.saved(self.submit(self.today))
        self.assertEqual(item.rental_date, self.today)                                    # picked up today, same-day
        with clock(at(self.today, 17, 0)):
            response = self.submit(self.today, user=self.other)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Today's pick-up closed at 5:00 PM", response.json()["error"])

    def test_after_five_tomorrow_is_still_open_with_a_same_day_pickup(self):
        tomorrow = self.today + D(days=1)
        with clock(at(self.today, 18)):
            item = self.saved(self.submit(tomorrow))
        self.assertEqual(item.rental_date, tomorrow)

    def test_before_five_an_event_tomorrow_is_picked_up_today_not_yesterday(self):
        with clock(at(self.today, 10)):
            item = self.saved(self.submit(self.today + D(days=1)))
        self.assertEqual(item.rental_date, self.today)


class PlannerMatchesCheckoutTests(TestCase):
    """The page's calendar and the server's checkout must give the same answer for every event date and size, or a customer is
    shown a date that checkout then refuses (or the other way round)."""

    def setUp(self):
        self.today = timezone.localdate()
        t = self.today
        a, b, c = make_unit(1), make_unit(2), make_unit(3)
        self.large = make_unit(4, Gown.Size.LARGE)
        make_unit(5, Gown.Size.MEDIUM, status=Gown.Status.OUT_OF_STOCK)
        book(a, t + D(days=5), t + D(days=9))
        block(a, t + D(days=10), t + D(days=14), GownUnavailability.Reason.COOLDOWN)
        block(b, t + D(days=3), t + D(days=4))
        book(b, t + D(days=12), t + D(days=16))
        book(c, t + D(days=30), t + D(days=34))
        book(c, t + D(days=1), t + D(days=2), status=Reservation.Status.CANCELLED)
        book(self.large, t + D(days=8), t + D(days=12))
        block(self.large, t + D(days=13), t + D(days=17), GownUnavailability.Reason.COOLDOWN)
        block(self.large, t + D(days=50), t + D(days=60))

    def compare(self, now, size_codes, horizon):
        units, bookings, blocks = _bookable(self)
        code_of = lambda unit: {Gown.Size.MEDIUM: "M", Gown.Size.LARGE: "L"}[unit.size]
        plans = availability.plan_calendar(units, bookings, blocks, now, horizon, code_of)
        checked = 0
        with clock(now):
            for code in size_codes:
                for event, value in plans[code].items():
                    outcome, unit, pickup = _plan_pickup("Event Gown", date.fromisoformat(event), size=code if code != "*" else "", now=now)
                    with self.subTest(size=code, event=event, plan=value):
                        if isinstance(value, list):
                            self.assertEqual((outcome, pickup.isoformat()), (_UNIT_ASSIGNED, value[0]))
                        elif value == "closed":
                            self.assertEqual(outcome, _UNIT_TODAY_CLOSED)
                        else:
                            self.assertEqual(outcome, _UNIT_UNAVAILABLE)
                    checked += 1
        return checked

    def test_every_event_date_and_size_agree_during_the_day(self):
        self.assertEqual(self.compare(at(self.today, 10), ["M", "L"], 36), 74)

    def test_every_event_date_and_size_agree_after_closing_time(self):
        self.assertEqual(self.compare(at(self.today, 17, 0), ["M", "L"], 12), 26)

    def test_the_any_size_calendar_agrees_for_a_gown_with_one_size(self):
        self.large.delete()
        self.assertEqual(self.compare(at(self.today, 10), ["*"], 20), 21)

    def test_a_passed_date_is_never_a_calendar_value_but_checkout_says_so(self):
        with clock(at(self.today, 10)):
            self.assertEqual(_plan_pickup("Event Gown", self.today - D(days=1), size="M")[0], _UNIT_DATE_PASSED)


def _bookable(case):
    from gowns.views import _bookable_units_and_busy_ranges
    return _bookable_units_and_busy_ranges(Gown.Category.WEDDING_GOWN, "Event Gown")


class GownPageCalendarDataTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.unit = make_unit(1)

    def page(self, now, unit=None):
        with clock(now):
            return self.client.get(reverse("gowns:product_detail", kwargs={"collection": "wedding", "slug": (unit or self.unit).slug}))

    def test_the_page_carries_the_whole_calendar_and_the_shop_hours(self):
        response = self.page(at(self.today, 10))
        data = response.context["calendar_data"]
        self.assertContains(response, 'id="product-calendar-data"')
        self.assertEqual(data["today"], self.today.isoformat())
        self.assertEqual((data["hours"], data["closing"], data["restDays"]), ("1:00 PM to 5:00 PM", "5:00 PM", 5))
        self.assertEqual(data["seconds"], 10 * 3600)
        self.assertEqual(data["closes"], 17 * 3600)
        in_ten = (self.today + D(days=10)).isoformat()
        self.assertEqual(data["plans"]["M"][in_ten], [(self.today + D(days=8)).isoformat(), 1])

    def test_before_five_the_page_also_knows_what_today_and_tomorrow_become_at_five(self):
        data = self.page(at(self.today, 10)).context["calendar_data"]
        tomorrow = (self.today + D(days=1)).isoformat()
        self.assertIsInstance(data["plans"]["*"][self.today.isoformat()], list)
        self.assertEqual(data["late"]["*"][self.today.isoformat()], "closed")
        self.assertEqual(data["late"]["*"][tomorrow], [tomorrow, 1])
        # an event the day after tomorrow is normally picked up today; once the shop closes it is picked up tomorrow
        in_two = (self.today + D(days=2)).isoformat()
        self.assertEqual(data["plans"]["*"][in_two], [self.today.isoformat(), 1])
        self.assertEqual(data["late"]["*"][in_two], [tomorrow, 1])
        self.assertNotIn((self.today + D(days=3)).isoformat(), data["late"]["*"])      # later events don't change at 5 PM

    def test_after_five_today_is_already_closed_and_nothing_more_is_needed(self):
        data = self.page(at(self.today, 17, 30)).context["calendar_data"]
        self.assertEqual(data["plans"]["*"][self.today.isoformat()], "closed")
        self.assertEqual(data["late"], {})

    def test_the_rest_days_after_a_rental_show_as_not_bookable(self):
        event = self.today + D(days=20)
        book(self.unit, event - D(days=2), event + D(days=2))
        block(self.unit, event + D(days=3), event + D(days=7), GownUnavailability.Reason.COOLDOWN)
        plans = self.page(at(self.today, 10)).context["calendar_data"]["plans"]["M"]
        for later in range(-4, 8):
            self.assertEqual(plans[(event + D(days=later)).isoformat()], "booked", later)
        first_free = (event + D(days=8)).isoformat()
        self.assertEqual(plans[first_free], [first_free, 1])

    def test_the_days_just_before_a_later_booking_are_marked_as_rest_days(self):
        later = self.today + D(days=40)
        book(self.unit, later, later + D(days=4))
        plans = self.page(at(self.today, 10)).context["calendar_data"]["plans"]["M"]
        self.assertEqual(plans[(later - D(days=7)).isoformat()], "rest")
        self.assertIsInstance(plans[(later - D(days=8)).isoformat()], list)

    def test_a_withdrawn_gown_has_no_calendar(self):
        unit = make_unit(2, name="Gone Gown", status=Gown.Status.OUT_OF_STOCK)
        self.assertEqual(self.page(at(self.today, 10), unit).context["calendar_data"], {})


class ReleaseReminderTests(TestCase):
    """The reminder staff read before releasing blocked days: what it really opens for customers."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="release_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="release_customer", password="x")

    def setUp(self):
        self.today = timezone.localdate()
        self.unit = make_unit(1)

    def preview(self, blk, now=None, user=None):
        self.client.force_login(user or self.staff)
        with clock(now or at(self.today, 10)):
            return self.client.get(reverse("arabela_admin:gown_block_release_preview", args=[blk.id]))

    def day(self, n):
        return self.today + D(days=n)

    def test_only_staff_may_ask_and_a_missing_block_is_a_404(self):
        blk = block(self.unit, self.day(20), self.day(24))
        self.assertEqual(self.preview(blk, user=self.customer).status_code, 401)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("arabela_admin:gown_block_release_preview", args=[blk.id])).status_code, 401)
        self.assertEqual(self.client.get(reverse("arabela_admin:gown_block_release_preview", args=[blk.id + 999])).status_code, 401)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("arabela_admin:gown_block_release_preview", args=[blk.id + 999])).status_code, 404)

    def test_releasing_the_rest_days_opens_the_event_dates_they_were_holding_back(self):
        book(self.unit, self.day(10), self.day(14))
        rest = block(self.unit, self.day(15), self.day(19), GownUnavailability.Reason.COOLDOWN)
        response = self.preview(rest)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["opens"], [self.day(n).isoformat() for n in range(15, 20)])
        self.assertEqual(data["need"], 8)
        self.assertIn("will be able to choose these new event dates", data["message"])
        self.assertIn(f"{self.day(15):%b} {self.day(15).day} - {self.day(19):%b} {self.day(19).day}", data["message"])

    def test_a_gap_of_eight_days_opens_a_date(self):
        book(self.unit, self.day(10), self.day(14))
        gap = block(self.unit, self.day(15), self.day(17))
        book(self.unit, self.day(23), self.day(27))                 # free: day 15 .. day 22 = 8 days in a row
        data = self.preview(gap).json()
        self.assertEqual(data["opens"], [self.day(15).isoformat()])
        self.assertIn("new event dates", data["message"])

    def test_a_gap_of_seven_days_opens_nothing_and_says_why(self):
        book(self.unit, self.day(10), self.day(14))
        gap = block(self.unit, self.day(15), self.day(17))
        book(self.unit, self.day(22), self.day(26))                 # free: day 15 .. day 21 = 7 days in a row
        data = self.preview(gap).json()
        self.assertEqual(data["opens"], [])
        self.assertEqual(data["free_run_days"], 7)
        self.assertIn("only 7 days in a row", data["message"])
        self.assertIn(f"({self.day(15):%b} {self.day(15).day} - {self.day(21):%b} {self.day(21).day})", data["message"])
        self.assertIn("needs 8 days in a row", data["message"])
        self.assertIn("customers still won't be able to book these days", data["message"])

    def test_a_short_gap_between_two_rentals_is_called_out(self):
        book(self.unit, self.day(10), self.day(14))
        gap = block(self.unit, self.day(15), self.day(17))
        book(self.unit, self.day(18), self.day(22))
        data = self.preview(gap).json()
        self.assertEqual((data["opens"], data["free_run_days"]), ([], 3))
        self.assertIn("only 3 days in a row", data["message"])

    def test_one_day_is_worded_in_the_singular(self):
        book(self.unit, self.day(10), self.day(14))
        gap = block(self.unit, self.day(15), self.day(15))
        book(self.unit, self.day(16), self.day(20))
        self.assertIn("only 1 day in a row", self.preview(gap).json()["message"])

    def test_releasing_a_block_that_customers_could_already_work_around_opens_nothing_new(self):
        spare = make_unit(2)                                         # a second unit of the same gown is free all along
        blk = block(self.unit, self.day(20), self.day(24))
        data = self.preview(blk).json()
        self.assertEqual(data["opens"], [])
        self.assertIn("won't open any new event dates", data["message"])
        self.assertIsNone(data["free_run_days"])

    def test_an_out_of_stock_gown_stays_unbookable_after_the_release(self):
        gone = make_unit(3, status=Gown.Status.OUT_OF_STOCK)
        blk = block(gone, self.day(20), self.day(24))
        data = self.preview(blk).json()
        self.assertEqual(data["opens"], [])
        self.assertIn("Out of Stock", data["message"])

    def test_the_release_itself_is_unchanged(self):
        blk = block(self.unit, self.day(20), self.day(24))
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(reverse("arabela_admin:gown_block_delete", args=[blk.id])).status_code, 200)
        self.assertFalse(GownUnavailability.objects.filter(id=blk.id).exists())

    def test_the_catalog_hands_the_page_the_reminder_address_for_every_block(self):
        blk = block(self.unit, self.day(20), self.day(24))
        self.client.force_login(self.staff)
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("arabela_admin:gown_block_release_preview", args=[blk.id]))
