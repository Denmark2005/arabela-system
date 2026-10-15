"""The one label a gown shows customers about when it can be booked (gowns.availability.booking_badge).

It replaced "Limited Availability" / "Available Now", which the collection card and the gown page decided in two different ways (the
card read the saved "Reserved" status, the page read the dates), so a card could say "Limited Availability" while its page said
"Available Now". Now:
  * bookable now (or tomorrow)       -> no label at all, + works
  * next bookable event date later   -> "Available from Oct 26", + works
  * nothing bookable in the calendar -> "Fully booked for now", + greyed out
and the card and the page always show the same thing."""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from gowns import availability
from gowns.models import Gown, GownUnavailability
from gowns.test_event_dates import at, make_unit_stub
from reservations.models import Reservation, ReservationItem

User = get_user_model()
D = timedelta


def label_day(day, today):
    return f"{day:%b} {day.day}" + (f", {day.year}" if day.year != today.year else "")


class BadgeRulesTests(SimpleTestCase):
    """The rule itself, on plain dates (no database)."""
    today = date(2026, 10, 16)

    def badge(self, units, bookings=None, blocks=None, now=None):
        return availability.booking_badge(units, bookings or {}, blocks or {}, now or at(self.today, 10))

    def test_a_free_gown_has_no_label(self):
        self.assertEqual(self.badge([make_unit_stub(1, "")]), {"state": "now", "date": self.today, "text": ""})

    def test_a_gown_booked_later_but_free_now_has_no_label(self):
        unit = make_unit_stub(1, "")
        later = self.today + D(days=25)
        result = self.badge([unit], {1: [(later, later + D(days=4))]}, {1: [(later + D(days=5), later + D(days=9))]})
        self.assertEqual((result["state"], result["text"]), ("now", ""))

    def test_a_gown_out_now_shows_the_first_date_it_can_be_booked(self):
        # the worked example: event Oct 18, returned Oct 20, cleaned Oct 21-25 -> first bookable event Oct 26
        unit = make_unit_stub(1, "")
        event = date(2026, 10, 18)
        bookings = {1: [(event - D(days=2), event + D(days=2))]}
        blocks = {1: [(event + D(days=3), event + D(days=7))]}
        result = self.badge([unit], bookings, blocks)
        self.assertEqual(result, {"state": "from", "date": date(2026, 10, 26), "text": "Available from Oct 26"})

    def test_one_free_unit_is_enough(self):
        busy, free = make_unit_stub(1, "M"), make_unit_stub(2, "L")
        result = self.badge([busy, free], {1: [(self.today - D(days=1), self.today + D(days=10))]})
        self.assertEqual(result["state"], "now")

    def test_after_five_today_is_closed_but_tomorrow_still_counts_as_now(self):
        result = self.badge([make_unit_stub(1, "")], now=at(self.today, 17, 30))
        self.assertEqual((result["state"], result["date"], result["text"]), ("now", self.today + D(days=1), ""))

    def test_free_from_the_day_after_tomorrow_shows_that_date(self):
        unit = make_unit_stub(1, "")
        result = self.badge([unit], blocks={1: [(self.today, self.today + D(days=1))]})
        self.assertEqual(result["text"], f"Available from {label_day(self.today + D(days=2), self.today)}")

    def test_nothing_bookable_in_the_whole_calendar_is_fully_booked(self):
        unit = make_unit_stub(1, "")
        result = self.badge([unit], blocks={1: [(self.today, self.today + D(days=availability.HORIZON_DAYS + 10))]})
        self.assertEqual(result, {"state": "full", "date": None, "text": "Fully booked for now"})

    def test_a_date_in_another_year_says_the_year(self):
        today = date(2026, 12, 20)
        result = availability.booking_badge([make_unit_stub(1, "")], {}, {1: [(today, date(2027, 1, 5))]}, at(today, 10))
        self.assertEqual(result["text"], "Available from Jan 6, 2027")

    def test_the_label_date_is_always_the_first_date_the_calendar_offers(self):
        units = [make_unit_stub(1, "M"), make_unit_stub(2, "M")]
        t = self.today
        scenarios = [
            ({}, {}),
            ({1: [(t, t + D(days=4))], 2: [(t - D(days=1), t + D(days=6))]}, {1: [(t + D(days=5), t + D(days=9))]}),
            ({1: [(t + D(days=5), t + D(days=9))], 2: [(t + D(days=3), t + D(days=12))]}, {}),          # rest days run into bookings
            ({}, {1: [(t, t + D(days=40))], 2: [(t, t + D(days=33))]}),
        ]
        for bookings, blocks in scenarios:
            for now in (at(t, 10), at(t, 17, 0)):
                with self.subTest(bookings=bookings, blocks=blocks, now=now):
                    calendar = availability.plan_calendar(units, bookings, blocks, now, availability.HORIZON_DAYS, lambda u: "")
                    first = next((date.fromisoformat(day) for day, value in sorted(calendar["*"].items()) if isinstance(value, list)), None)
                    self.assertEqual(availability.booking_badge(units, bookings, blocks, now)["date"], first)


def make_unit(n, name, size=Gown.Size.MEDIUM, status=Gown.Status.AVAILABLE, category=Gown.Category.WEDDING_GOWN):
    return Gown.objects.create(
        gown_id=f"BADGE-{n:04d}", name=name, category=category, color_name="White", color_code="WH", size=size,
        rental_price=Decimal("5000"), status=status)


def book(unit, start, end, *, stage=ReservationItem.Stage.PICKUP, username):
    customer = User.objects.create_user(username=username, password="x")
    reservation = Reservation.objects.create(customer=customer, customer_name="Booked", status=Reservation.Status.CONFIRMED,
                                             reviewed_at=timezone.now())
    item = ReservationItem.objects.create(reservation=reservation, gown=unit, gown_name=unit.name, stage=stage,
                                          rental_date=start, return_date=end)
    GownUnavailability.objects.create(gown=unit, start_date=end + D(days=1), end_date=end + D(days=5),
                                      reason=GownUnavailability.Reason.COOLDOWN, auto_for_item=item)
    return item


class CardAndPageAgreeTests(TestCase):
    """The real pages: the collection card and the gown page show the same label, worked out from the real bookings."""

    def setUp(self):
        self.today = timezone.localdate()
        self.morning = at(self.today, 10)
        patcher = patch("gowns.availability.now_local", return_value=self.morning)
        self.addCleanup(patcher.stop)
        patcher.start()

    def card(self, title):
        from gowns.views import _products_for_category
        return next(c for c in _products_for_category("wedding") if c["title"] == title)

    def page(self, unit):
        return self.client.get(reverse("gowns:product_detail", kwargs={"collection": "wedding", "slug": unit.slug}))

    def test_an_old_booking_never_picked_up_no_longer_makes_a_free_gown_look_unavailable(self):
        # Wedding Gown 3's case: booked weeks ago, never collected, saved status still "Reserved".
        unit = make_unit(1, "Badge Gown Three", status=Gown.Status.RESERVED)
        book(unit, self.today - D(days=21), self.today - D(days=17), username="old_booking")
        card = self.card("Badge Gown Three")
        self.assertEqual((card["badge"], card["can_add"]), ("", True))
        response = self.page(unit)
        self.assertEqual(response.context["product"]["availability"], "")
        self.assertNotContains(response, "Limited Availability")
        self.assertNotContains(response, "Available Now")

    def test_a_gown_booked_next_month_but_free_now_shows_nothing(self):
        unit = make_unit(2, "Badge Gown Five", status=Gown.Status.RESERVED)
        book(unit, self.today + D(days=25), self.today + D(days=29), username="next_month")
        self.assertEqual(self.card("Badge Gown Five")["badge"], "")
        self.assertEqual(self.page(unit).context["product"]["availability"], "")

    def test_a_gown_out_right_now_shows_the_date_on_the_card_and_the_page_and_the_calendar_agrees(self):
        unit = make_unit(3, "Badge Gown Out", status=Gown.Status.RESERVED)
        event = self.today + D(days=2)
        book(unit, self.today, event + D(days=2), stage=ReservationItem.Stage.RESERVED, username="out_now")
        first = event + D(days=8)                                            # 2 return days + 5 cleaning days later
        expected = f"Available from {label_day(first, self.today)}"
        card = self.card("Badge Gown Out")
        self.assertEqual((card["badge"], card["can_add"]), (expected, True))
        response = self.page(unit)
        self.assertEqual(response.context["product"]["availability"], expected)
        self.assertContains(response, expected)
        plans = response.context["calendar_data"]["plans"]["*"]
        first_open = min(day for day, value in plans.items() if isinstance(value, list))
        self.assertEqual(first_open, first.isoformat())                       # the first date the customer can pick

    def test_a_gown_with_no_open_date_is_fully_booked_and_cannot_be_quick_added(self):
        unit = make_unit(4, "Badge Gown Full")
        GownUnavailability.objects.create(gown=unit, start_date=self.today, end_date=self.today + D(days=availability.HORIZON_DAYS + 10))
        card = self.card("Badge Gown Full")
        self.assertEqual((card["badge"], card["can_add"]), ("Fully booked for now", False))
        self.assertEqual(self.page(unit).context["product"]["availability"], "Fully booked for now")
        html = self.client.get(reverse("gowns:collection_wedding")).content.decode()
        self.assertIn('title="Fully booked for now" aria-label="Fully booked for now" disabled', html)

    def test_a_removed_gown_says_currently_unavailable_on_its_page(self):
        unit = make_unit(5, "Badge Gown Gone", status=Gown.Status.OUT_OF_STOCK)
        self.assertEqual(self.page(unit).context["product"]["availability"], "Currently Unavailable")

    def test_one_free_size_is_enough_for_no_label(self):
        medium = make_unit(6, "Badge Gown Sizes", size=Gown.Size.MEDIUM)
        make_unit(7, "Badge Gown Sizes", size=Gown.Size.LARGE)
        book(medium, self.today - D(days=1), self.today + D(days=3), stage=ReservationItem.Stage.RESERVED, username="size_m")
        self.assertEqual(self.card("Badge Gown Sizes")["badge"], "")
        self.assertEqual(self.page(medium).context["product"]["availability"], "")

    def test_every_card_matches_its_own_page(self):
        cases = [
            (make_unit(10, "Match Free"), None),
            (make_unit(11, "Match Out"), (self.today - D(days=1), self.today + D(days=4), ReservationItem.Stage.RESERVED)),
            (make_unit(12, "Match Later"), (self.today + D(days=30), self.today + D(days=34), ReservationItem.Stage.PICKUP)),
            (make_unit(13, "Match Old"), (self.today - D(days=30), self.today - D(days=26), ReservationItem.Stage.PICKUP)),
        ]
        full = make_unit(14, "Match Full")
        GownUnavailability.objects.create(gown=full, start_date=self.today, end_date=self.today + D(days=200))
        for n, (unit, booking) in enumerate(cases):
            if booking:
                book(unit, booking[0], booking[1], stage=booking[2], username=f"match_{n}")
        for unit in [u for u, _ in cases] + [full]:
            with self.subTest(gown=unit.name):
                self.assertEqual(self.card(unit.name)["badge"], self.page(unit).context["product"]["availability"])

    def test_the_collection_page_shows_the_label_and_keeps_quick_add_for_a_dated_gown(self):
        unit = make_unit(20, "Badge Gown Shown")
        book(unit, self.today, self.today + D(days=4), stage=ReservationItem.Stage.RESERVED, username="shown")
        html = self.client.get(reverse("gowns:collection_wedding")).content.decode()
        self.assertIn("Available from", html)
        self.assertIn("addCollectionItem(", html)                                   # + still works...
        self.assertNotIn('aria-label="Available from', html)                         # ...it is not the greyed-out one
        self.assertNotIn("Limited Availability", html)

    def test_the_all_page_and_search_show_the_same_label(self):
        unit = make_unit(21, "Badge Gown Everywhere")
        GownUnavailability.objects.create(gown=unit, start_date=self.today, end_date=self.today + D(days=200))
        for url in (reverse("gowns:collection_all"), reverse("gowns:search_results") + "?q=Everywhere"):
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                self.assertIn("Fully booked for now", html)
                self.assertIn('aria-label="Fully booked for now" disabled', html)

    def test_a_whole_page_of_cards_reads_the_bookings_once_not_once_per_card(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from gowns.views import _products_for_category

        def queries():
            with CaptureQueriesContext(connection) as captured:
                _products_for_category("wedding")
            return len(captured)

        for n in range(2):
            unit = make_unit(30 + n, f"Query Gown {n}")
            book(unit, self.today, self.today + D(days=4), stage=ReservationItem.Stage.RESERVED, username=f"q{n}")
        few = queries()
        for n in range(2, 10):
            unit = make_unit(30 + n, f"Query Gown {n}")
            book(unit, self.today, self.today + D(days=4), stage=ReservationItem.Stage.RESERVED, username=f"q{n}")
        self.assertEqual(queries(), few)                     # 10 cards cost the same as 2
