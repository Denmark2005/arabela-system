"""Real sizes: a gown is offered only in the sizes staff gave its units in Gown Catalog, each size's availability is its own
(a rented Large does not block the Medium, and vice versa), and checkout books exactly the size chosen."""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from gowns.context_processors import _build_search_catalog
from gowns.models import Gown, GownUnavailability, size_codes_of
from gowns.views import (
    _UNIT_ASSIGNED, _UNIT_SIZE_NOT_OFFERED, _UNIT_SIZE_REQUIRED, _UNIT_UNAVAILABLE, _blocked_dates_for_category,
    _find_available_unit,
)
from reservations.models import Reservation, ReservationItem

User = get_user_model()
_TINY_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


def make_unit(n, size, name="Sized Gown One", category=Gown.Category.WEDDING_GOWN, status=Gown.Status.AVAILABLE):
    return Gown.objects.create(
        gown_id=f"SIZE-{n:04d}", name=name, category=category, color_name="White", color_code="WH", size=size,
        rental_price=Decimal("5000"), status=status)


def book(unit, start, end, username):
    reservation = Reservation.objects.create(customer=User.objects.create_user(username=username, password="x"),
                                             customer_name="Booked", status=Reservation.Status.CONFIRMED)
    return ReservationItem.objects.create(reservation=reservation, gown=unit, gown_name=unit.name,
                                          rental_date=start, event_date=start + timedelta(days=2), return_date=end,
                                          overdue_date=end)


class SizeCodesTests(TestCase):
    def test_codes_follow_button_order_and_appear_once(self):
        units = [make_unit(1, Gown.Size.EXTRA_LARGE), make_unit(2, Gown.Size.SMALL), make_unit(3, Gown.Size.SMALL),
                 make_unit(4, Gown.Size.FREE_SIZE), make_unit(5, Gown.Size.MEDIUM)]
        self.assertEqual(size_codes_of(units), ["S", "M", "XL", "FREE"])


class GownPageSizesTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()

    def page(self, unit):
        return self.client.get(reverse("gowns:product_detail", kwargs={"collection": "wedding", "slug": unit.slug}))

    def test_a_single_size_gown_offers_only_that_size(self):
        unit = make_unit(1, Gown.Size.LARGE)
        response = self.page(unit)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([o["code"] for o in response.context["size_options"]], ["L"])
        html = response.content.decode()
        self.assertIn('data-product-size="L"', html)
        for other in ("S", "M", "XL"):
            self.assertNotIn(f'data-product-size="{other}"', html)

    def test_four_units_in_four_sizes_offer_all_four_with_their_own_busy_dates(self):
        units = [make_unit(i, size) for i, size in enumerate(
            (Gown.Size.SMALL, Gown.Size.MEDIUM, Gown.Size.LARGE, Gown.Size.EXTRA_LARGE), start=1)]
        start, end = self.today + timedelta(days=10), self.today + timedelta(days=14)
        book(units[2], start, end, "rented_large")                      # the Large is rented
        response = self.page(units[0])
        self.assertEqual([o["code"] for o in response.context["size_options"]], ["S", "M", "L", "XL"])
        rows = {row["size"]: row["busy"] for row in response.context["size_units"]}
        self.assertEqual(rows["L"], [[start.isoformat(), end.isoformat()]])
        self.assertEqual(rows["S"], [])
        self.assertContains(response, 'id="product-size-units"')
        # one size rented is not a day when EVERY unit is taken
        self.assertEqual(_blocked_dates_for_category(Gown.Category.WEDDING_GOWN, "Sized Gown One"), [])

    def test_free_size_is_labelled_in_full(self):
        unit = make_unit(1, Gown.Size.FREE_SIZE)
        self.assertContains(self.page(unit), ">Free Size</button>")

    def test_an_out_of_stock_unit_is_not_offered_or_counted(self):
        unit = make_unit(1, Gown.Size.MEDIUM)
        make_unit(2, Gown.Size.LARGE, status=Gown.Status.OUT_OF_STOCK)
        response = self.page(unit)
        self.assertEqual([o["code"] for o in response.context["size_options"]], ["M"])
        self.assertEqual(len(response.context["size_units"]), 1)

    def test_blocked_dates_are_unchanged_when_every_unit_is_taken(self):
        unit = make_unit(1, Gown.Size.MEDIUM)
        start = self.today + timedelta(days=5)
        GownUnavailability.objects.create(gown=unit, start_date=start, end_date=start + timedelta(days=1))
        self.assertEqual(_blocked_dates_for_category(Gown.Category.WEDDING_GOWN, unit.name),
                         [start.isoformat(), (start + timedelta(days=1)).isoformat()])

    def test_the_search_catalog_tells_quick_add_the_sizes(self):
        make_unit(1, Gown.Size.MEDIUM)
        make_unit(2, Gown.Size.SMALL)
        row = next(r for r in _build_search_catalog() if r["title"] == "Sized Gown One")
        self.assertEqual(row["sizes"], ["S", "M"])


class BookingTheChosenSizeTests(TestCase):
    def setUp(self):
        self.start = timezone.localdate() + timedelta(days=20)
        self.end = self.start + timedelta(days=4)

    def test_the_chosen_size_is_booked_even_when_the_page_was_another_sizes(self):
        medium, large = make_unit(1, Gown.Size.MEDIUM), make_unit(2, Gown.Size.LARGE)
        outcome, unit = _find_available_unit(medium.name, self.start, self.end, gown_slug=medium.slug, size="L")
        self.assertEqual((outcome, unit), (_UNIT_ASSIGNED, large))

    def test_a_rented_size_is_refused_even_though_another_size_is_free(self):
        medium, large = make_unit(1, Gown.Size.MEDIUM), make_unit(2, Gown.Size.LARGE)
        book(large, self.start, self.end, "has_the_large")
        self.assertEqual(_find_available_unit(medium.name, self.start, self.end, size="L"), (_UNIT_UNAVAILABLE, None))
        self.assertEqual(_find_available_unit(medium.name, self.start, self.end, size="Medium"), (_UNIT_ASSIGNED, medium))

    def test_a_size_the_gown_does_not_come_in_is_refused(self):
        unit = make_unit(1, Gown.Size.MEDIUM)
        self.assertEqual(_find_available_unit(unit.name, self.start, self.end, size="XL"), (_UNIT_SIZE_NOT_OFFERED, None))

    def test_several_sizes_need_one_chosen_but_a_single_size_does_not(self):
        medium = make_unit(1, Gown.Size.MEDIUM)
        self.assertEqual(_find_available_unit(medium.name, self.start, self.end, size=""), (_UNIT_ASSIGNED, medium))
        make_unit(2, Gown.Size.LARGE)
        self.assertEqual(_find_available_unit(medium.name, self.start, self.end, size=""), (_UNIT_SIZE_REQUIRED, None))

    def test_two_units_of_one_size_the_free_one_is_given(self):
        first, second = make_unit(1, Gown.Size.MEDIUM), make_unit(2, Gown.Size.MEDIUM)
        make_unit(3, Gown.Size.LARGE)
        book(first, self.start, self.end, "has_the_first_medium")
        self.assertEqual(_find_available_unit(first.name, self.start, self.end, gown_slug=first.slug, size="M"),
                         (_UNIT_ASSIGNED, second))


class CheckoutSizeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="size_customer", password="x")

    def setUp(self):
        self.client.force_login(self.customer)
        patcher = patch("gowns.views._save_proof_file", return_value="https://example.test/fake-proof.jpg")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.start = timezone.localdate() + timedelta(days=30)

    def submit(self, unit, size):
        items = [{"gown_name": unit.name, "gown_slug": unit.slug, "size": size,
                  "rental_date": self.start.isoformat(), "return_date": (self.start + timedelta(days=4)).isoformat()}]
        return self.client.post(reverse("gowns:reservation_submit"), data={
            "items": json.dumps(items), "first_name": "Size", "last_name": "Buyer", "phone": "09171234567",
            "address": "1 Test St", "city": "Test City", "postal_code": "1000", "payment_method": "GCash",
            "proof_of_payment": SimpleUploadedFile("proof.jpg", _TINY_JPEG, content_type="image/jpeg"),
        })

    def test_the_booking_records_the_real_size_of_the_gown_given(self):
        make_unit(1, Gown.Size.MEDIUM)
        large = make_unit(2, Gown.Size.LARGE)
        response = self.submit(large, "L")
        self.assertEqual(response.status_code, 200, response.content)
        item = ReservationItem.objects.get(reservation__reference_code=response.json()["reference_code"])
        self.assertEqual((item.gown_id, item.size), (large.id, "Large"))

    def test_a_size_the_gown_does_not_come_in_is_refused_with_a_clear_message(self):
        unit = make_unit(1, Gown.Size.MEDIUM)
        response = self.submit(unit, "S")
        self.assertEqual(response.status_code, 400)
        self.assertIn("doesn't come in Small", response.json()["error"])
        self.assertFalse(Reservation.objects.exists())

    def test_a_gown_in_several_sizes_needs_one_chosen(self):
        unit = make_unit(1, Gown.Size.MEDIUM)
        make_unit(2, Gown.Size.LARGE)
        response = self.submit(unit, "-")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Please choose a size for Sized Gown One", response.json()["error"])

    def test_a_single_size_gown_with_no_size_sent_still_books_as_before(self):
        unit = make_unit(1, Gown.Size.MEDIUM)
        response = self.submit(unit, "")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(ReservationItem.objects.get().size, "Medium")
