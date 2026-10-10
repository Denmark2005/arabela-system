"""Smaller gown photos (gowns/photos.py): the customer pages ask Cloudinary for a resized copy, anything that is not a plain Cloudinary
upload is left exactly as it was, and the admin still sees the original."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounts.models import UserProfile
from gowns.models import Gown
from gowns.photos import GRID_WIDTH, PAGE_WIDTH, sized

PHOTO = "https://res.cloudinary.com/shop/image/upload/v1/media/gown_photos/abc_Wedding_Gown_02_nbs67l"
GRID = "https://res.cloudinary.com/shop/image/upload/f_auto,q_auto,c_limit,w_800/v1/media/gown_photos/abc_Wedding_Gown_02_nbs67l"
PAGE = "https://res.cloudinary.com/shop/image/upload/f_auto,q_auto,c_limit,w_1600/v1/media/gown_photos/abc_Wedding_Gown_02_nbs67l"


class SizedTests(SimpleTestCase):
    def test_a_cloudinary_photo_gets_the_resize_instructions(self):
        self.assertEqual(sized(PHOTO, GRID_WIDTH), GRID)
        self.assertEqual(sized(PHOTO, PAGE_WIDTH), PAGE)
        self.assertEqual(sized(PHOTO + ".jpg", GRID_WIDTH), GRID + ".jpg")

    def test_anything_else_is_returned_unchanged(self):
        for url in ("", None, "/static/images/placeholder.jpg", "https://example.test/gowns/tall.jpg", "/media/gown_photos/x.jpg",
                    GRID,                                                            # already resized: never doubled
                    "https://res.cloudinary.com/shop/video/upload/v1/clip.mp4",      # not an image
                    "https://res.cloudinary.com/shop/image/fetch/v1/https://x.test/a.jpg"):
            with self.subTest(url=url):
                self.assertEqual(sized(url, GRID_WIDTH), url)


class PagesUseSmallerPhotosTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.gown = Gown.objects.create(
            gown_id="PHOTO-0001", name="Photo Test Gown", category="Wedding Gown", color_name="White", color_code="WH",
            size=Gown.Size.MEDIUM, rental_price=Decimal("5000"), status=Gown.Status.AVAILABLE, photo_url=PHOTO)
        Gown.objects.create(
            gown_id="PHOTO-0002", name="Plain Address Gown", category="Wedding Gown", color_name="Ivory", color_code="IV",
            size=Gown.Size.MEDIUM, rental_price=Decimal("4000"), status=Gown.Status.AVAILABLE,
            photo_url="https://example.test/gowns/plain.jpg")

    def test_the_grid_search_and_suggestions_use_the_grid_size(self):
        for url in ("/collections/wedding/", "/collections/all/", "/search/?q=photo"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertContains(response, GRID)
                self.assertNotContains(response, PHOTO)
                self.assertContains(response, "https://example.test/gowns/plain.jpg")   # not Cloudinary: untouched

    def test_the_product_page_uses_the_large_size(self):
        response = self.client.get(f"/collections/wedding/products/{self.gown.slug}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["product"]["image"], PAGE)
        self.assertNotContains(response, PHOTO)

    def test_the_stored_photo_and_the_admin_catalog_keep_the_original(self):
        owner = get_user_model().objects.create_user(username="photo_owner", password="x" * 12, is_staff=True)
        UserProfile.objects.create(user=owner, role=UserProfile.Role.OWNER)
        self.client.force_login(owner)
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertContains(response, PHOTO)
        self.assertNotContains(response, "f_auto,q_auto")
        self.gown.refresh_from_db()
        self.assertEqual(self.gown.photo_url, PHOTO)
