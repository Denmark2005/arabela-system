"""One Large and TWO Smalls of the same design: two customers (or one customer twice) booking the two Smalls on the same date.

Every unit is its own physical gown, so two Smalls can be rented by two different people on the same dates; a third request for a Small is
refused in plain words, and the Large is never involved. Run through the real checkout and the real gown page."""
import json
import re
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from gowns.models import Gown
from reservations.models import Reservation, ReservationItem

User = get_user_model()
CAT = Gown.Category.BRIDESMAID_DRESSES
NAME = "Black Bridesmaid Dress 4"
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


class TwoSmallsAndALargeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        def unit(number, size):
            return Gown.objects.create(gown_id=f"{CAT}-BK-{number:03d}", name=NAME, category=CAT, color_name="Black", color_code="BK", size=size,
                                       rental_price=Decimal("1800"), status=Gown.Status.AVAILABLE, condition=Gown.Condition.GOOD)
        cls.large = unit(1, Gown.Size.LARGE)
        cls.small_a = unit(2, Gown.Size.SMALL)
        cls.small_b = unit(3, Gown.Size.SMALL)
        cls.users = {key: User.objects.create_user(f"twosmall_{key}", password="x") for key in "abc"}

    def setUp(self):
        patcher = patch("gowns.views._save_proof_file", return_value="https://example.test/p.jpg")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.event = timezone.localdate() + timedelta(days=12)

    def book(self, who, sizes):
        items = [{"gown_name": NAME, "gown_slug": self.large.slug, "size": s, "rental_date": (self.event - timedelta(days=2)).isoformat(),
                  "return_date": (self.event + timedelta(days=2)).isoformat()} for s in sizes]
        client = Client()
        client.force_login(self.users[who])
        return client.post(reverse("gowns:reservation_submit"), data={
            "items": json.dumps(items), "first_name": "T", "last_name": who.upper(), "phone": "09171234567", "address": "1 St", "city": "C",
            "postal_code": "1000", "payment_method": "GCash", "proof_of_payment": SimpleUploadedFile("p.jpg", _JPEG, content_type="image/jpeg")})

    def page(self):
        return self.client.get(reverse("gowns:product_detail", kwargs={"collection": "bridesmaid-dresses", "slug": self.large.slug}))

    def plans(self):
        return self.page().context["calendar_data"]["plans"]

    def day(self):
        return self.event.isoformat()

    def test_the_page_counts_two_smalls_and_one_large(self):
        html = self.page().content.decode()
        counts = dict(re.findall(r'data-product-size="([A-Z]+)"[^>]*data-size-count="(\d+)"', html))
        self.assertEqual(counts, {"S": "2", "L": "1"})
        self.assertEqual(self.plans()["S"][self.day()][1], 2)                       # "2 Smalls free" that date

    def test_two_customers_each_get_their_own_small_and_a_third_is_refused(self):
        first, second = self.book("a", ["S"]), self.book("b", ["S"])
        self.assertEqual((first.status_code, second.status_code), (200, 200), (first.content, second.content))
        units = sorted(ReservationItem.objects.values_list("gown_id", flat=True))
        self.assertEqual(units, sorted([self.small_a.id, self.small_b.id]))          # two different physical Smalls
        third = self.book("c", ["S"])
        self.assertEqual(third.status_code, 400)
        self.assertIn("no longer available", third.json()["error"])
        self.assertEqual(Reservation.objects.count(), 2)                              # nothing written for the refused one

    def test_after_the_first_small_one_is_still_free_and_after_the_second_none_is_but_the_large_is(self):
        self.assertEqual(self.book("a", ["S"]).status_code, 200)
        self.assertEqual(self.plans()["S"][self.day()][1], 1)                         # one Small left
        self.assertEqual(self.book("b", ["S"]).status_code, 200)
        plans = self.plans()
        self.assertEqual(plans["S"][self.day()], "booked")                            # no Small left that date
        self.assertIsInstance(plans["L"][self.day()], list)                           # the Large is untouched
        self.assertNotIn(self.day(), json.loads(self.page().context["blocked_dates_json"]))   # so the date is still open for the design
        self.assertEqual(self.book("c", ["L"]).status_code, 200)
        self.assertIn(self.day(), json.loads(self.page().context["blocked_dates_json"]))      # now every unit is taken

    def test_one_customer_can_book_both_smalls_in_one_booking(self):
        response = self.book("a", ["S", "S"])
        self.assertEqual(response.status_code, 200, response.content)
        reservation = Reservation.objects.get()
        self.assertEqual(sorted(reservation.items.values_list("gown_id", flat=True)), sorted([self.small_a.id, self.small_b.id]))
        self.assertEqual(reservation.security_deposit, Decimal("4000"))               # a deposit for each gown
        self.assertEqual(self.book("b", ["S"]).status_code, 400)                      # no Small left for anyone else
        self.assertEqual(self.book("b", ["L"]).status_code, 200)                      # the Large is fine

    def test_asking_for_three_smalls_when_two_exist_says_so_and_books_nothing(self):
        response = self.book("a", ["S", "S", "S"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("in your selection 3 times", response.json()["error"])
        self.assertFalse(Reservation.objects.exists())

    def test_two_smalls_and_the_large_in_one_booking_each_take_their_own_gown(self):
        response = self.book("a", ["S", "S", "L"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(sorted(ReservationItem.objects.values_list("gown_id", flat=True)),
                         sorted([self.large.id, self.small_a.id, self.small_b.id]))

    def test_the_two_smalls_can_be_booked_for_different_dates_by_different_people(self):
        later = self.event + timedelta(days=20)
        items = lambda event: [{"gown_name": NAME, "gown_slug": self.large.slug, "size": "S", "rental_date": (event - timedelta(days=2)).isoformat(),
                                "return_date": (event + timedelta(days=2)).isoformat()}]
        for who, event in (("a", self.event), ("b", later)):
            client = Client()
            client.force_login(self.users[who])
            response = client.post(reverse("gowns:reservation_submit"), data={
                "items": json.dumps(items(event)), "first_name": "T", "last_name": who, "phone": "09171234567", "address": "1 St", "city": "C",
                "postal_code": "1000", "payment_method": "GCash", "proof_of_payment": SimpleUploadedFile("p.jpg", _JPEG, content_type="image/jpeg")})
            self.assertEqual(response.status_code, 200, response.content)
        # both dates still have a Small free (each customer used one unit on a different date)
        plans = self.plans()
        self.assertIsInstance(plans["S"][self.day()], list)
        self.assertIsInstance(plans["S"][later.isoformat()], list)
