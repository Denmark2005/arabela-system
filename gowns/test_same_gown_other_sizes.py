"""One gown, several sizes (Barong in Small / Medium / Large): when one size is rented on a date, are the other sizes still fine that
same date -- and what happens when a size is booked, rejected, or booked twice?

Each size is its own physical gown, so a booking only ever uses up THAT gown (its dates, and the 5 rest days after its return). These
tests run the real checkout (reservation_submit) and read the real gown-page calendar, so they prove what a customer would see."""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from gowns.models import Gown, GownUnavailability
from reservations.models import Reservation, ReservationItem

User = get_user_model()
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
NAME = "Scenario Barong"


class SameGownOtherSizesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.number = 0
        cls.small = cls.make(Gown.Size.SMALL)
        cls.medium = cls.make(Gown.Size.MEDIUM)
        cls.large = cls.make(Gown.Size.LARGE)
        cls.customers = {key: User.objects.create_user(f"scenario_{key}", password="x") for key in "abcd"}
        cls.staff = User.objects.create_user("scenario_staff", password="x", is_staff=True)

    @classmethod
    def make(cls, size, name=NAME):
        cls.number += 1
        return Gown.objects.create(
            gown_id=f"SCEN-{cls.number:04d}", name=name, category=Gown.Category.BARONG, color_name="Beige", color_code="BG", size=size,
            rental_price=Decimal("1000"), condition=Gown.Condition.GOOD, status=Gown.Status.AVAILABLE)

    def setUp(self):
        patcher = patch("gowns.views._save_proof_file", return_value="https://example.test/fake-proof.jpg")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.today = timezone.localdate()
        self.event = self.today + timedelta(days=10)

    # ---- helpers ----
    def book(self, who, sizes, event=None):
        """Check out one gown per size in `sizes` ('S', 'M', 'L') for an event date, as customer `who`."""
        event = event or self.event
        items = [{"gown_name": NAME, "gown_slug": self.small.slug, "size": size, "rental_date": (event - timedelta(days=2)).isoformat(),
                  "return_date": (event + timedelta(days=2)).isoformat()} for size in sizes]
        client = Client()
        client.force_login(self.customers[who])
        return client.post(reverse("gowns:reservation_submit"), data={
            "items": json.dumps(items), "first_name": "Scen", "last_name": who.upper(), "phone": "09171234567", "address": "1 Test St",
            "city": "Test City", "postal_code": "1000", "payment_method": "GCash",
            "proof_of_payment": SimpleUploadedFile("proof.jpg", _JPEG, content_type="image/jpeg")})

    def page(self):
        return self.client.get(reverse("gowns:product_detail", kwargs={"collection": "barong", "slug": self.small.slug}))

    def plans(self):
        return self.page().context["calendar_data"]["plans"]

    def free(self, code, day, plans=None):
        value = (plans or self.plans())[code][day.isoformat()]
        return isinstance(value, list)

    def units_booked(self, response):
        reservation = Reservation.objects.get(reference_code=response.json()["reference_code"])
        return sorted(reservation.items.values_list("gown__size", flat=True))

    # ---- the scenario ----
    def test_one_size_rented_leaves_the_other_sizes_free_that_same_date(self):
        response = self.book("a", ["M"])
        self.assertEqual(response.status_code, 200, response.content)
        plans = self.plans()
        self.assertEqual(plans["M"][self.event.isoformat()], "booked")                 # the rented size is taken
        self.assertTrue(self.free("S", self.event, plans))                              # the Small is free that date
        self.assertTrue(self.free("L", self.event, plans))                              # the Large is free that date
        self.assertEqual(plans["*"][self.event.isoformat()][1], 2)                      # "2 of 3 available" for that date
        self.assertNotIn(self.event.isoformat(), json.loads(self.page().context["blocked_dates_json"]))   # the date is NOT greyed for the design

    def test_the_rented_size_stays_taken_through_its_rest_days_and_is_back_on_the_eighth_day_while_the_others_never_notice(self):
        self.assertEqual(self.book("a", ["M"]).status_code, 200)
        plans = self.plans()
        for offset in range(0, 8):                                                      # the event day through 7 days after it
            day = self.event + timedelta(days=offset)
            self.assertFalse(self.free("M", day, plans), f"Medium should still be taken {offset} days after the event")
        self.assertTrue(self.free("M", self.event + timedelta(days=8), plans))          # the 8th day: Medium is rentable again
        for offset in range(-7, 13):                                                    # every date around it (from today on): Small and Large untouched
            day = self.event + timedelta(days=offset)
            self.assertTrue(self.free("S", day, plans), f"Small blocked {offset} days from the event")
            self.assertTrue(self.free("L", day, plans), f"Large blocked {offset} days from the event")

    def test_each_size_can_be_rented_by_a_different_customer_the_same_date_and_a_fourth_cannot_have_a_size_twice(self):
        for who, size in (("a", "M"), ("b", "S"), ("c", "L")):
            response = self.book(who, [size])
            self.assertEqual(response.status_code, 200, (who, size, response.content))
            self.assertEqual(self.units_booked(response), [{"S": "Small", "M": "Medium", "L": "Large"}[size]])
        used = {i.gown_id for i in ReservationItem.objects.all()}
        self.assertEqual(used, {self.small.id, self.medium.id, self.large.id})           # three bookings, three different physical gowns
        refused = self.book("d", ["M"])
        self.assertEqual(refused.status_code, 400)
        self.assertIn("no longer available", refused.json()["error"])
        self.assertEqual(Reservation.objects.count(), 3)                                # nothing was written for the refused one

    def test_when_every_size_is_taken_the_date_greys_out_for_the_whole_gown(self):
        for who, size in (("a", "M"), ("b", "S")):
            self.assertEqual(self.book(who, [size]).status_code, 200)
        self.assertNotIn(self.event.isoformat(), json.loads(self.page().context["blocked_dates_json"]))   # Large still free: date open
        self.assertEqual(self.book("c", ["L"]).status_code, 200)
        self.assertIn(self.event.isoformat(), json.loads(self.page().context["blocked_dates_json"]))      # now all three: date greyed
        self.assertEqual(self.plans()["*"][self.event.isoformat()], "booked")
        for code in ("S", "M", "L"):
            self.assertFalse(self.free(code, self.event))

    def test_the_example_date_tomorrow_works_the_same_way(self):
        tomorrow = self.today + timedelta(days=1)
        for who, size in (("a", "M"), ("b", "S"), ("c", "L")):
            self.assertEqual(self.book(who, [size], event=tomorrow).status_code, 200, (who, size))
        self.assertEqual(self.book("d", ["M"], event=tomorrow).status_code, 400)

    def test_one_customer_can_rent_two_sizes_in_one_booking_and_the_third_stays_free(self):
        response = self.book("a", ["S", "L"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.units_booked(response), ["Large", "Small"])
        reservation = Reservation.objects.get()
        self.assertEqual(reservation.security_deposit, Decimal("4000"))                  # a deposit for each gown
        self.assertTrue(self.free("M", self.event))
        self.assertEqual(self.book("b", ["M"]).status_code, 200)

    def test_the_same_size_twice_in_one_booking_is_refused_when_only_one_exists(self):
        response = self.book("a", ["M", "M"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("in your selection 2 times", response.json()["error"])
        self.assertFalse(Reservation.objects.exists())

    def test_two_gowns_of_one_size_each_customer_gets_their_own_and_the_third_is_refused(self):
        self.make(Gown.Size.MEDIUM)                                                      # a second Medium of the same design
        first, second = self.book("a", ["M"]), self.book("b", ["M"])
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        medium_ids = {i.gown_id for i in ReservationItem.objects.all()}
        self.assertEqual(len(medium_ids), 2)                                             # two different Mediums
        self.assertEqual(self.book("c", ["M"]).status_code, 400)
        self.assertEqual(self.book("c", ["S"]).status_code, 200)                         # the Small was never affected

    def test_a_size_booked_for_overlapping_dates_is_refused_but_other_sizes_are_not(self):
        self.assertEqual(self.book("a", ["M"]).status_code, 200)
        next_day = self.event + timedelta(days=1)
        self.assertEqual(self.book("b", ["M"], event=next_day).status_code, 400)         # same Medium, overlapping rental
        self.assertEqual(self.book("b", ["S"], event=next_day).status_code, 200)         # the Small is fine
        self.assertEqual(self.book("c", ["M"], event=self.event + timedelta(days=8)).status_code, 200)   # the Medium again after its rest

    def test_a_booked_gown_rests_alone_each_gown_gets_its_own_rest_and_the_others_get_none(self):
        self.assertEqual(self.book("a", ["M"]).status_code, 200)
        self.assertEqual(self.book("b", ["S"]).status_code, 200)
        blocks = GownUnavailability.objects.filter(reason=GownUnavailability.Reason.COOLDOWN)
        self.assertEqual(sorted(blocks.values_list("gown_id", flat=True)), sorted([self.medium.id, self.small.id]))
        self.assertFalse(blocks.filter(gown=self.large).exists())                        # the Large rests nowhere
        for block in blocks:
            self.assertEqual(block.start_date, self.event + timedelta(days=3))
            self.assertEqual(block.end_date, self.event + timedelta(days=7))

    # ---- what staff do to a booking ----
    def test_rejecting_a_booking_gives_its_size_back_including_the_rest_days(self):
        self.assertEqual(self.book("a", ["M"]).status_code, 200)
        reservation = Reservation.objects.get()
        staff = Client()
        staff.force_login(self.staff)
        response = staff.post(reverse("arabela_admin:reservation_reject", args=[reservation.id]), data=json.dumps({"reason": "Receipt unclear"}),
                              content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        plans = self.plans()
        for offset in range(-2, 9):                                                      # the whole span the rejected booking used to hold
            day = self.event + timedelta(days=offset)
            self.assertTrue(self.free("M", day, plans), f"Medium should be free again {offset} days from the event")
        self.assertFalse(GownUnavailability.objects.filter(gown=self.medium).exists())   # and no leftover rest block on that gown
        self.assertEqual(self.book("b", ["M"]).status_code, 200)

    def test_rejecting_one_booking_never_frees_another_customers_gown(self):
        self.assertEqual(self.book("a", ["M"]).status_code, 200)
        self.assertEqual(self.book("b", ["S"]).status_code, 200)
        first = Reservation.objects.get(customer=self.customers["a"])
        staff = Client()
        staff.force_login(self.staff)
        staff.post(reverse("arabela_admin:reservation_reject", args=[first.id]), data="{}", content_type="application/json")
        plans = self.plans()
        self.assertFalse(self.free("S", self.event, plans))                              # B's Small is still B's
        self.assertTrue(GownUnavailability.objects.filter(gown=self.small).exists())      # and still resting
        self.assertTrue(self.free("M", self.event, plans))                                # A's Medium is free again
