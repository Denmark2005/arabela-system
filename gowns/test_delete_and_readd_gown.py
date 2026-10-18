"""Delete a gown and add "the same thing" again (in another size): what do customers and staff see?

The owner asked: delete "Black Bridesmaid Dress 4", add it again but as a Small -- any problem on the customer site or in the admin?
These tests do exactly that through the real admin delete / Add Gown views, then read the real customer pages and checkout."""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from gowns.models import Gown, GownRemoval
from reservations.models import Reservation, ReservationItem

User = get_user_model()
CAT = Gown.Category.BRIDESMAID_DRESSES
NAME = "Black Bridesmaid Dress 4"
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


class DeleteAndReaddTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("readd_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user("readd_customer", password="x")

    def setUp(self):
        self.admin = Client()
        self.admin.force_login(self.staff)
        self.large = self.make(Gown.Size.LARGE, 1)
        self.xl = self.make(Gown.Size.EXTRA_LARGE, 2)
        self.other = self.make(Gown.Size.LARGE, 3, name="Black Bridesmaid Dress 3")      # a neighbouring design that must never be touched

    def make(self, size, number, name=NAME):
        return Gown.objects.create(
            gown_id=f"{CAT}-BK-{number:03d}", name=name, category=CAT, color_name="Black", color_code="BK", size=size,
            rental_price=Decimal("1800"), condition=Gown.Condition.GOOD, status=Gown.Status.AVAILABLE, photo_url=f"https://photos.test/{number}.jpg")

    def delete(self, gown):
        return self.admin.post(reverse("arabela_admin:gown_delete", args=[gown.id]), data=json.dumps({"reason": "Retired"}),
                               content_type="application/json")

    def add(self, size, name=NAME, choice="same", price="1800", photo=None):
        data = {"name": name, "category": CAT, "color_name": "Black", "color_code": "BK", "size": size, "rental_price": price,
                "name_choice": choice}
        if photo:
            data["photo"] = SimpleUploadedFile("new.jpg", _JPEG, content_type="image/jpeg")
        with patch("arabela_admin.views._save_gown_photo", return_value="https://photos.test/new.jpg"):
            return self.admin.post(reverse("arabela_admin:gown_create"), data=data)

    def collection(self):
        return self.client.get("/collections/bridesmaid-dresses/")

    def page(self, slug):
        return self.client.get(f"/collections/bridesmaid-dresses/products/{slug}/")

    def sizes_on(self, html):
        import re
        return re.findall(r'data-product-size="([A-Z]+)"', html), re.findall(r'data-size-not-offered="([A-Z]+)"', html)

    # ---- delete the WHOLE design, add it back as a Small ----
    def test_delete_the_whole_design_then_add_it_again_as_small(self):
        old_slug = self.large.slug
        self.assertEqual(self.delete(self.large).status_code, 200)
        self.assertEqual(self.delete(self.xl).status_code, 200)
        self.assertEqual(self.page(old_slug).status_code, 404)                           # the old product link is gone
        self.assertNotContains(self.collection(), NAME)                                  # and so is the card

        created = self.add("Small")
        self.assertEqual(created.status_code, 200, created.content)
        new = Gown.objects.get(name=NAME)
        self.assertEqual(new.size, "Small")
        self.assertNotIn(new.gown_id, {self.large.gown_id, self.xl.gown_id})             # the ID is NEW: retired numbers are never reused
        self.assertGreater(new.tracking_number, 3)
        self.assertNotEqual(new.slug, old_slug)                                          # so is the web address

        self.assertContains(self.collection(), NAME)                                     # one card again
        self.assertEqual(self.sizes_on(self.page(new.slug).content.decode()), (["S"], ["M", "L", "XL", "XXL"]))   # only Small clickable
        self.assertEqual(self.page(old_slug).status_code, 404)                           # the old address does NOT come back to life

    def test_the_removal_log_keeps_both_old_gowns_and_the_neighbour_is_untouched(self):
        self.delete(self.large)
        self.delete(self.xl)
        self.add("Small")
        removed = GownRemoval.objects.filter(name=NAME)
        self.assertEqual(sorted(removed.values_list("gown_id", flat=True)), [self.large.gown_id, self.xl.gown_id])
        self.assertTrue(all(r.reason == "Retired" for r in removed))
        self.other.refresh_from_db()
        self.assertEqual((self.other.name, self.other.size, self.other.status), ("Black Bridesmaid Dress 3", "Large", "Available"))
        self.assertContains(self.collection(), "Black Bridesmaid Dress 3")

    # ---- delete ONE size, add a different size ----
    def test_delete_only_the_xl_then_add_a_small_gives_one_product_with_l_and_s(self):
        self.assertEqual(self.delete(self.xl).status_code, 200)
        self.assertEqual(self.add("Small").status_code, 200)
        self.assertEqual(Gown.objects.filter(name=NAME).count(), 2)
        html = self.page(self.large.slug).content.decode()
        self.assertEqual(self.sizes_on(html), (["S", "L"], ["M", "XL", "XXL"]))
        cards = [h for h in self.collection().content.decode().split('"') if "/products/" in h and "black-bridesmaid-dress-4" in h]
        self.assertEqual(len(set(cards)), 1)                                             # still ONE card, not two
        self.assertContains(self.collection(), "2 available")

    def test_a_retyped_name_with_other_capitals_still_joins_the_same_product(self):
        self.assertEqual(self.add("Small", name="black bridesmaid dress 4  ").status_code, 200)
        self.assertEqual(Gown.objects.filter(category=CAT, name__iexact=NAME).count(), 3)
        cards = {h for h in self.collection().content.decode().split('"') if h.startswith("/collections/bridesmaid-dresses/products/") and "dress-4" in h}
        self.assertEqual(len(cards), 1)

    def test_adding_the_same_name_asks_first_and_a_wrong_answer_makes_a_separate_product(self):
        asked = self.add("Small", choice="")
        self.assertEqual(asked.status_code, 409)                                         # the admin asks "same dress or a different one?"
        self.assertEqual(asked.json()["code"], "name_conflict")
        self.assertEqual(Gown.objects.filter(name__iexact=NAME).count(), 2)
        self.add("Small", choice="different")
        names = set(Gown.objects.filter(category=CAT).values_list("name", flat=True))
        self.assertEqual(len(names), 3)                                                  # it got its own free name instead of vanishing into the other

    # ---- the one real trap: the new unit has no photo / another price ----
    def test_a_new_size_added_without_a_photo_does_not_take_over_the_card_from_the_old_ones(self):
        self.add("Small")                                                                # no photo uploaded
        card_image = [g for g in Gown.objects.filter(name=NAME).order_by("id")][0].photo_url
        self.assertTrue(card_image)                                                      # the card still uses an older gown's photo
        self.assertContains(self.collection(), "https://photos.test/1.jpg")

    # ---- a customer who had the old gown in the bag ----
    def test_a_stale_bag_for_the_deleted_size_is_refused_in_plain_words_and_books_nothing(self):
        old_slug = self.large.slug
        self.delete(self.large)
        self.delete(self.xl)
        self.add("Small")
        start = timezone.localdate() + timedelta(days=20)
        item = {"gown_name": NAME, "gown_slug": old_slug, "size": "L", "rental_date": start.isoformat(), "return_date": (start + timedelta(days=4)).isoformat()}
        client = Client()
        client.force_login(self.customer)
        with patch("gowns.views._save_proof_file", return_value="https://example.test/p.jpg"):
            response = client.post(reverse("gowns:reservation_submit"), data={
                "items": json.dumps([item]), "first_name": "A", "last_name": "B", "phone": "09171234567", "address": "1 St", "city": "C",
                "postal_code": "1000", "payment_method": "GCash", "proof_of_payment": SimpleUploadedFile("p.jpg", _JPEG, content_type="image/jpeg")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("doesn't come in Large", response.json()["error"])
        self.assertFalse(Reservation.objects.exists())

    def test_the_same_stale_bag_with_the_size_that_still_exists_books_the_new_gown(self):
        old_slug = self.large.slug
        self.delete(self.large)
        self.delete(self.xl)
        self.add("Small")
        start = timezone.localdate() + timedelta(days=20)
        item = {"gown_name": NAME, "gown_slug": old_slug, "size": "S", "rental_date": start.isoformat(), "return_date": (start + timedelta(days=4)).isoformat()}
        client = Client()
        client.force_login(self.customer)
        with patch("gowns.views._save_proof_file", return_value="https://example.test/p.jpg"):
            response = client.post(reverse("gowns:reservation_submit"), data={
                "items": json.dumps([item]), "first_name": "A", "last_name": "B", "phone": "09171234567", "address": "1 St", "city": "C",
                "postal_code": "1000", "payment_method": "GCash", "proof_of_payment": SimpleUploadedFile("p.jpg", _JPEG, content_type="image/jpeg")})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(ReservationItem.objects.get().gown.size, "Small")

    # ---- staff side: history and live bookings ----
    def test_a_gown_on_an_active_booking_cannot_be_deleted_and_a_returned_one_leaves_its_history_readable(self):
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Readd Customer", status=Reservation.Status.CONFIRMED)
        day = timezone.localdate() + timedelta(days=15)
        item = ReservationItem.objects.create(reservation=reservation, gown=self.large, gown_name=NAME, size="Large", rental_date=day,
                                              event_date=day + timedelta(days=2), return_date=day + timedelta(days=4), overdue_date=day + timedelta(days=4))
        blocked = self.delete(self.large)
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("still on an active reservation", blocked.json()["error"])
        self.assertTrue(Gown.objects.filter(id=self.large.id).exists())
        item.stage = ReservationItem.Stage.RETURNED
        item.save(update_fields=["stage"])
        self.assertEqual(self.delete(self.large).status_code, 200)
        item.refresh_from_db()
        self.assertIsNone(item.gown_id)                                                  # the link is cleared ...
        self.assertEqual((item.gown_name, item.size), (NAME, "Large"))                   # ... but the booking still says what it was
        for name in ("reservation_records", "reservations", "gown_catalog"):
            with self.subTest(page=name):
                try:
                    url = reverse(f"arabela_admin:{name}")
                except Exception:
                    continue
                self.assertEqual(self.admin.get(url).status_code, 200)
