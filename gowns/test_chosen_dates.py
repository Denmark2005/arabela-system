"""The customer chooses the event, the pick-up and the return (gowns/availability.py unit_window, gowns/views.resolve_item_dates): the days the gown
page offers are exactly the days checkout accepts, the chosen days are booked as chosen (never moved behind the customer's back), and both ways of
paying keep them.

Free days: 2 days before the event and 2 days after it. Every other booked day costs the shop's fee per gown (₱200 here), from the deposit."""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.test_support import agreed
from gowns import availability, views
from gowns.models import COOLDOWN_DAYS, Gown, GownUnavailability, SiteSettings
from gowns.sample_images import unique_jpeg
from gowns.sample_payment import TYPED_AMOUNT, typed_reference
from gowns.test_event_dates import User, at, block, book, clock, make_unit
from gowns.test_xendit_checkout import Base as XenditBase
from reservations import ledger
from reservations.models import PaymentAttempt, Reservation, ReservationItem

D = timedelta


class WindowRulesTests(SimpleTestCase):
    """unit_window / unit_fits on plain dates."""
    today = availability.date(2026, 12, 1)
    event = availability.date(2026, 12, 15)

    def window(self, bookings=(), blocks=(), earliest=None, last=None):
        return availability.unit_window(self.event, list(bookings), list(blocks), earliest or self.today,
                                        last or availability.last_return_day(self.today, self.event))

    def test_the_free_days_are_two_before_and_two_after_the_event(self):
        self.assertEqual(availability.free_days(self.event), (self.event - D(2), self.event + D(2)))
        self.assertEqual(availability.extra_days(self.event, self.event - D(4), self.event + D(5)), (2, 3))
        self.assertEqual(availability.extra_days(self.event, self.event - D(2), self.event + D(2)), (0, 0))
        self.assertEqual(availability.extra_days(self.event, self.event, self.event + D(1)), (0, 0))
        self.assertEqual(availability.kept_until(self.event, self.event + D(1)), self.event + D(2))
        self.assertEqual(availability.kept_until(self.event, self.event + D(6)), self.event + D(6))

    def test_a_free_gown_may_be_picked_up_from_today_and_returned_within_a_year(self):
        (pickup, lowest, highest), reason = self.window()
        self.assertEqual((pickup, lowest, reason), (self.event - D(2), self.today, None))
        self.assertEqual(highest, self.today + D(availability.LONGEST_RETURN_DAYS))

    def test_an_earlier_booking_stops_the_early_pick_up_the_day_after_it(self):
        (pickup, lowest, _highest), _ = self.window(bookings=[(self.event - D(10), self.event - D(5))])
        self.assertEqual((pickup, lowest), (self.event - D(2), self.event - D(4)))

    def test_a_later_booking_keeps_the_five_cleaning_days(self):
        later = self.event + D(20)
        (_p, _l, highest), _ = self.window(bookings=[(later, later + D(4))])
        self.assertEqual(highest, later - D(COOLDOWN_DAYS + 1))                       # returned then, it rests the 5 days before the next pick-up
        (_p, _l, highest), _ = self.window(blocks=[(later, later + D(4))])
        self.assertEqual(highest, later - D(1))                                      # a staff block only has to stay clear of the rental itself

    def test_the_event_day_pick_up_is_offered_only_when_no_earlier_day_is(self):
        (pickup, lowest, _h), _ = self.window(bookings=[(self.event - D(6), self.event - D(1))])
        self.assertEqual((pickup, lowest), (self.event, self.event))
        self.assertTrue(availability.pickup_allowed(self.event, self.event, lowest))
        self.assertFalse(availability.pickup_allowed(self.event, self.event - D(1), lowest))
        (_p, lowest, _h), _ = self.window()
        self.assertFalse(availability.pickup_allowed(self.event, self.event, lowest))
        self.assertTrue(availability.pickup_allowed(self.event, self.event - D(1), lowest))

    def test_fits_accepts_exactly_the_days_inside_the_window(self):
        bookings = [(self.event - D(10), self.event - D(5)), (self.event + D(20), self.event + D(22))]
        earliest, last = self.today, availability.last_return_day(self.today, self.event)
        fits = lambda p, r: availability.unit_fits(self.event, p, r, bookings, [], earliest, last)
        self.assertTrue(fits(self.event - D(4), self.event + D(14)))
        self.assertFalse(fits(self.event - D(5), self.event + D(2)))                 # the day the earlier booking still holds
        self.assertFalse(fits(self.event - D(2), self.event + D(15)))                # its cleaning days would run into the next booking
        self.assertTrue(fits(self.event - D(1), self.event + D(1)))                  # one day after is allowed too
        self.assertFalse(fits(self.event - D(1), self.event))                        # the return is after the event
        self.assertFalse(fits(self.event + D(1), self.event + D(2)))


class PageAndCheckoutAgreeTests(TestCase):
    """For every event date the page offers, and the edges of each gown's window, checkout accepts what the page offers and refuses one day beyond."""

    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        a, b = make_unit(1), make_unit(2)
        large = make_unit(3, Gown.Size.LARGE)
        book(a, today + D(days=9), today + D(days=12))
        block(a, today + D(days=13), today + D(days=17), GownUnavailability.Reason.COOLDOWN)
        block(b, today + D(days=3), today + D(days=4))
        book(b, today + D(days=24), today + D(days=27))
        block(large, today + D(days=16), today + D(days=18))
        book(large, today + D(days=40), today + D(days=43))

    def try_dates(self, event, pickup, ret, size):
        item = {"gown_name": "Event Gown", "gown_slug": "", "size": size, "event_date": event, "rental_date": pickup, "return_date": ret}
        from django.db import transaction
        with transaction.atomic():
            return views.resolve_item_dates(item, [item], already_taken=frozenset(), now=self.now), item

    def test_every_offered_window_edge_is_accepted_and_one_day_beyond_is_refused(self):
        self.now = at(timezone.localdate(), 10)
        units, bookings, blocks = views._bookable_units_and_busy_ranges(Gown.Category.WEDDING_GOWN, "Event Gown")
        code_of = lambda unit: {Gown.Size.MEDIUM: "M", Gown.Size.LARGE: "L"}[unit.size]
        plans = availability.plan_calendar(units, bookings, blocks, self.now, 45, code_of)
        checked = refused = 0
        with clock(self.now):
            for code in ("M", "L"):
                for iso, value in plans[code].items():
                    if not isinstance(value, list):
                        continue
                    event = availability.date.fromisoformat(iso)
                    default_pickup, count, windows = availability.date.fromisoformat(value[0]), value[1], value[2]
                    self.assertTrue(1 <= count <= 2 and windows)
                    # the default (what choosing the event fills in) is always bookable
                    problem, item = self.try_dates(event, default_pickup, event + D(days=2), code)
                    self.assertIsNone(problem, (code, iso, problem))
                    self.assertEqual((item["rental_date"], item["return_date"], item["planned_return_date"]), (default_pickup, event + D(days=2), None))
                    for lowest_off, highest_off in windows:
                        lowest = event + D(days=lowest_off)
                        pickup = event if lowest_off >= 0 else lowest
                        highest = event + D(days=min(highest_off, 60))
                        problem, item = self.try_dates(event, pickup, highest, code)
                        self.assertIsNone(problem, (code, iso, pickup, highest, problem))
                        self.assertEqual((item["rental_date"], item["return_date"]), (pickup, highest))
                        checked += 1
                        problem, item = self.try_dates(event, pickup, event + D(days=1), code)
                        self.assertIsNone(problem, (code, iso, "day after", problem))
                        self.assertEqual((item["return_date"], item["planned_return_date"]), (event + D(days=2), event + D(days=1)))
                    # one day outside every window is refused: before the lowest pick-up of all free gowns, and after the highest return of all
                    low = min(w[0] for w in windows)
                    high = max(w[1] for w in windows)
                    before = event + D(days=low - 1)
                    if low < 0 and before >= timezone.localdate():
                        problem, _ = self.try_dates(event, before, event + D(days=2), code)
                        self.assertIsNotNone(problem, (code, iso, "before", before))
                        refused += 1
                    if high < 60:
                        problem, _ = self.try_dates(event, default_pickup, event + D(days=high + 1), code)
                        self.assertIsNotNone(problem, (code, iso, "after", high + 1))
                        refused += 1
        self.assertGreater(checked, 40)
        self.assertGreater(refused, 20)


class ResolveItemDatesTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.unit = make_unit(1)
        self.event = self.today + D(days=20)

    def resolve(self, *, pickup=None, ret=None, event="default", now=None, items=None):
        from django.db import transaction
        item = {"gown_name": self.unit.name, "gown_slug": self.unit.slug, "size": "M",
                "event_date": self.event if event == "default" else event,
                "rental_date": pickup or self.event - D(days=2), "return_date": ret or self.event + D(days=2)}
        now = now or at(self.today, 10)
        with clock(now), transaction.atomic():
            return views.resolve_item_dates(item, items or [item], already_taken=frozenset(), now=now), item

    def test_the_chosen_days_are_kept_with_the_free_days_of_the_event(self):
        problem, item = self.resolve(pickup=self.event - D(days=6), ret=self.event + D(days=5))
        self.assertIsNone(problem)
        self.assertEqual((item["rental_date"], item["return_date"], item["event_date"]), (self.event - D(days=6), self.event + D(days=5), self.event))
        self.assertEqual((item["free_pickup_date"], item["free_return_date"]), (self.event - D(days=2), self.event + D(days=2)))
        self.assertEqual(views.extra_days_fee([item], 200), Decimal("1400"))          # 4 early + 3 late

    def test_days_taken_meanwhile_are_refused_with_what_to_do_not_moved(self):
        book(self.unit, self.event + D(days=15), self.event + D(days=18))
        problem, _ = self.resolve(ret=self.event + D(days=12))
        self.assertIsNotNone(problem)
        self.assertIn("were just taken", problem[0])
        book(self.unit, self.event - D(days=2), self.event + D(days=1), username="gone")
        problem, _ = self.resolve()
        self.assertIn("is no longer available for", problem[0])

    def test_dates_that_make_no_sense_are_refused(self):
        for pickup, ret in ((self.event + D(days=1), self.event + D(days=3)), (self.event - D(days=1), self.event),
                            (self.event - D(days=1), self.event + D(days=800))):
            with self.subTest(pickup=pickup, ret=ret):
                problem, _ = self.resolve(pickup=pickup, ret=ret)
                self.assertIn("not valid", problem[0])
        problem, _ = self.resolve(pickup=self.today - D(days=1))
        self.assertIn("has already passed", problem[0])

    def test_a_pick_up_chosen_for_today_is_refused_from_five(self):
        self.event = self.today + D(days=5)
        problem, _ = self.resolve(pickup=self.today, now=at(self.today, 16, 59))
        self.assertIsNone(problem)
        problem, _ = self.resolve(pickup=self.today, now=at(self.today, 17, 0))
        self.assertIn("Today's pick-up closed at 5:00 PM", problem[0])

    def test_a_bag_line_without_an_event_is_booked_the_old_way_only_when_its_days_look_old(self):
        problem, item = self.resolve(event=None, pickup=self.event - D(days=1), ret=self.event + D(days=2))
        self.assertIsNone(problem)
        self.assertEqual((item["event_date"], item["rental_date"], item["return_date"]), (self.event, self.event - D(days=2), self.event + D(days=2)))
        self.assertEqual(views.extra_days_fee([item], 200), Decimal("0"))
        problem, _ = self.resolve(event=None, pickup=self.event - D(days=9), ret=self.event + D(days=2))
        self.assertIn("choose your dates again", problem[0])


class GcashCheckoutKeepsTheChosenDaysTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="chosen_customer", password="x")

    def setUp(self):
        patcher = patch("gowns.views._save_proof_file", return_value="https://example.test/fake-proof.jpg")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.today = timezone.localdate()
        self.unit = make_unit(1)
        self.event = self.today + D(days=20)

    def submit(self, *, pickup, ret, expected_extra=None):
        self.client.force_login(self.customer)
        agreed(self.customer)
        items = [{"gown_name": self.unit.name, "gown_slug": self.unit.slug, "size": "M", "event_date": self.event.isoformat(),
                  "rental_date": pickup.isoformat(), "return_date": ret.isoformat()}]
        data = {"items": json.dumps(items), "first_name": "Chosen", "last_name": "Buyer", "phone": "09171234567", "address": "1 Test St",
                "city": "Test City", "postal_code": "1000", "payment_method": "GCash", "gcash_reference": typed_reference(), "gcash_amount": TYPED_AMOUNT,
                "proof_of_payment": SimpleUploadedFile("proof.jpg", unique_jpeg(), content_type="image/jpeg")}
        if expected_extra is not None:
            data["expected_extra_fee"] = str(expected_extra)
        with clock(at(self.today, 10)):
            return self.client.post(reverse("gowns:reservation_submit"), data=data)

    def test_the_booking_keeps_the_days_the_free_days_and_the_cleaning_after_the_chosen_return(self):
        response = self.submit(pickup=self.event - D(days=4), ret=self.event + D(days=4), expected_extra=800)
        self.assertEqual(response.status_code, 200, response.content)
        item = ReservationItem.objects.get()
        self.assertEqual((item.rental_date, item.event_date, item.return_date), (self.event - D(days=4), self.event, self.event + D(days=4)))
        self.assertEqual((item.original_rental_date, item.original_return_date), (item.rental_date, item.return_date))
        self.assertEqual((item.free_pickup_date, item.free_return_date, item.planned_return_date), (self.event - D(days=2), self.event + D(days=2), None))
        rest = GownUnavailability.objects.get(auto_for_item=item)
        self.assertEqual((rest.start_date, rest.end_date), (self.event + D(days=5), self.event + D(days=9)))
        booking = item.reservation
        self.assertEqual((booking.security_deposit, booking.total_amount - booking.rental_subtotal), (Decimal("2000.00"), Decimal("2000.00")))   # paid amount unchanged
        s = ledger.statement(booking)
        self.assertEqual((s.early_fee, s.later_fee, s.late_fee), (Decimal("400.00"), Decimal("400.00"), Decimal("800.00")))

    def test_returning_one_day_after_is_free_and_the_gown_is_kept_until_the_second_day(self):
        response = self.submit(pickup=self.event - D(days=1), ret=self.event + D(days=1), expected_extra=0)
        self.assertEqual(response.status_code, 200, response.content)
        item = ReservationItem.objects.get()
        self.assertEqual((item.return_date, item.planned_return_date, item.shown_return_date), (self.event + D(days=2), self.event + D(days=1), self.event + D(days=1)))
        self.assertEqual(ledger.statement(item.reservation).late_fee, Decimal("0.00"))

    def test_a_page_that_showed_another_amount_is_told_and_nothing_is_booked(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"late_fee_per_day": 300})
        response = self.submit(pickup=self.event - D(days=3), ret=self.event + D(days=2), expected_extra=200)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["extra_fee_changed"])
        self.assertIn("₱300", response.json()["error"])
        self.assertFalse(Reservation.objects.exists())
        response = self.submit(pickup=self.event - D(days=3), ret=self.event + D(days=2), expected_extra=300)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Reservation.objects.get().late_fee_per_day, 300)


class OnlineCheckoutKeepsTheChosenDaysTests(XenditBase):
    def chosen(self, *, before=0, after=0):
        item = self.item()
        item.update(event_date=self.event.isoformat(), rental_date=(self.event - timedelta(days=2 + before)).isoformat(),
                    return_date=(self.event + timedelta(days=2 + after)).isoformat())
        return item

    def test_the_paid_booking_has_the_chosen_days_and_the_deduction_on_its_receipt(self):
        attempt, _ = self.started(items=[self.chosen(before=2, after=1)], expected_extra_fee="600")
        self.assertEqual(attempt.amount, Decimal("2000.00"))                                  # nothing extra is paid now
        data = self.pay_and_confirm(attempt)
        self.assertEqual(data["state"], "booked", data)
        item = ReservationItem.objects.get()
        self.assertEqual((item.rental_date, item.event_date, item.return_date), (self.event - timedelta(days=4), self.event, self.event + timedelta(days=3)))
        self.assertEqual((item.free_pickup_date, item.free_return_date), (self.event - timedelta(days=2), self.event + timedelta(days=2)))
        details = data["details"]
        self.assertEqual((details["deduction"], details["deposit_back"]), ("600.00", "1,400.00"))
        self.assertEqual((details["gowns"][0]["extra"], details["gowns"][0]["extra_days"]), ("600.00", 3))
        payment = item.reservation.payments.get()
        self.assertEqual((payment.snapshot["deduct"]["total"], payment.snapshot["deduct"]["back"]), ("600.00", "1400.00"))
        self.assertTrue(payment.snapshot["change_dates_note"])

    def test_changing_only_the_pick_up_is_another_payment_not_the_same_page(self):
        first, _ = self.started(items=[self.chosen(before=2)], request_id="req-chosen-0001")
        second, _ = self.started(items=[self.chosen(before=1)], request_id="req-chosen-0001")
        self.assertNotEqual(first.pk, second.pk)
        first.refresh_from_db()
        self.assertEqual(first.status, PaymentAttempt.Status.CANCELLED)

    def test_a_pick_up_today_paid_just_before_five_is_still_booked_after_five(self):
        today = timezone.localdate()
        self.event = today + timedelta(days=5)
        item = self.item()
        item.update(event_date=self.event.isoformat(), rental_date=today.isoformat(), return_date=(self.event + timedelta(days=2)).isoformat())
        with clock(at(today, 16, 55)):
            attempt, _ = self.started(items=[item])
        PaymentAttempt.objects.filter(pk=attempt.pk).update(payload={**attempt.payload, "started_at": at(today, 16, 55).isoformat()})
        with clock(at(today, 17, 5)):
            data = self.pay_and_confirm(attempt)
        self.assertEqual(data["state"], "booked", data)
        self.assertEqual(ReservationItem.objects.get().rental_date, today)

    def test_a_chosen_day_taken_while_the_customer_paid_sends_the_money_back(self):
        attempt, _ = self.started(items=[self.chosen(after=3)])
        book(self.gown, self.event + timedelta(days=4), self.event + timedelta(days=6), username="took_it")
        data = self.pay_and_confirm(attempt)
        self.assertEqual(data["state"], "refunded", data)
        self.assertEqual(ReservationItem.objects.filter(reservation__customer=self.ana).count(), 0)


class ProductPageDataTests(TestCase):
    def test_the_page_carries_the_windows_the_free_days_and_the_fee(self):
        today = timezone.localdate()
        unit = make_unit(1)
        book(unit, today + D(days=30), today + D(days=34))
        SiteSettings.objects.update_or_create(pk=1, defaults={"late_fee_per_day": 250})
        with clock(at(today, 10)):
            response = self.client.get(reverse("gowns:product_detail", kwargs={"collection": "wedding", "slug": unit.slug}))
        data = response.context["calendar_data"]
        self.assertEqual((data["feePerDay"], data["freeBefore"], data["freeAfter"]), (250, 2, 2))
        value = data["plans"]["*"][(today + D(days=10)).isoformat()]
        self.assertEqual(value[2], [[-10, 14]])                       # from today, back by day 24: it then rests 25-29, before the booking on day 30
        for piece in ('id="product-date-event"', 'id="product-date-pickup"', 'id="product-date-return"', 'id="product-fee-note"', "Select Rental Dates"):
            self.assertContains(response, piece)
        # Days further ahead than planned: free for good from the day after the last booking's cleaning, up to a year from today.
        self.assertEqual((data["openFrom"]["*"], data["lastReturn"]),
                         ((today + D(days=35)).isoformat(), (today + D(days=availability.LONGEST_RETURN_DAYS)).isoformat()))
        self.assertEqual(max(data["plans"]["*"]), (today + D(days=availability.HORIZON_DAYS)).isoformat())

    def test_a_booking_further_ahead_than_four_months_stretches_the_plan_to_it(self):
        today = timezone.localdate()
        unit = make_unit(1)
        book(unit, today + D(days=200), today + D(days=204))
        with clock(at(today, 10)):
            data = self.client.get(reverse("gowns:product_detail", kwargs={"collection": "wedding", "slug": unit.slug})).context["calendar_data"]
        plans = data["plans"]["*"]
        self.assertGreaterEqual(max(plans), (today + D(days=206)).isoformat())
        self.assertEqual(plans[(today + D(days=202)).isoformat()], "booked")                  # greyed although it is past four months
        self.assertIsInstance(plans[(today + D(days=150)).isoformat()], list)                  # and the days before it offer extra days
        self.assertEqual(plans[(today + D(days=150)).isoformat()][2], [[-150, 44]])
        self.assertEqual(data["openFrom"]["*"], (today + D(days=205)).isoformat())
