"""The shop's GCash QR: a bigger picture in the checkout's "View QR" window, and the full-QR page that window opens in a new tab.
(The optional reference-number box is gone from the checkout; the server side of it is tested in gowns/tests.py.)"""
import re
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from gowns.models import Gown, SiteSettings

User = get_user_model()
QR_URL = "https://files.example/gcash_qr/shop.png"


class FullQrPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="qr_customer", password="x")

    def set_qr(self, **extra):
        values = {"gcash_account_name": "Arabela Gown Rental", "gcash_number": "09171234567", "gcash_qr_url": QR_URL}
        values.update(extra)
        SiteSettings.objects.update_or_create(pk=1, defaults=values)

    def test_a_signed_out_visitor_is_sent_to_sign_in(self):
        response = self.client.get(reverse("gowns:gcash_qr"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_a_signed_in_customer_sees_the_whole_picture_with_save_copy_and_the_account(self):
        self.set_qr()
        self.client.force_login(self.customer)
        response = self.client.get(reverse("gowns:gcash_qr"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn(f'<img id="gcash-qr-full" class="qr-img" alt="Arabela GCash QR code" src="{QR_URL}">', html)
        self.assertIn("object-fit: contain", html)                          # shown whole, never cropped
        self.assertNotIn("object-fit: cover", html)
        self.assertIn('id="gcash-save-qr"', html)
        self.assertIn("Save QR image", html)
        self.assertIn("Arabela-GCash-QR.png", html)
        self.assertIn("Arabela Gown Rental", html)
        self.assertIn('id="gcash-number-text">09171234567<', html)
        self.assertIn('id="gcash-copy-number"', html)
        self.assertIn("close this tab to go back to your reservation", html)
        self.assertNotIn("QR code coming soon", html)

    def test_the_page_is_not_indexed_and_has_its_own_title(self):
        self.set_qr()
        self.client.force_login(self.customer)
        html = self.client.get(reverse("gowns:gcash_qr")).content.decode()
        self.assertIn('<meta name="robots" content="noindex" />', html)
        self.assertIn("<title>ARABELA | GCash QR</title>", html)

    def test_without_a_qr_it_says_coming_soon_and_offers_no_picture(self):
        self.set_qr(gcash_qr_url="")
        self.client.force_login(self.customer)
        html = self.client.get(reverse("gowns:gcash_qr")).content.decode()
        self.assertIn("QR code coming soon", html)
        self.assertNotIn("<img", html)
        self.assertNotIn('id="gcash-save-qr"', html)
        self.assertIn('id="qr-close"', html)

    def test_with_only_a_number_it_still_shows_the_number_to_send_to(self):
        self.set_qr(gcash_qr_url="", gcash_account_name="")
        self.client.force_login(self.customer)
        html = self.client.get(reverse("gowns:gcash_qr")).content.decode()
        self.assertIn("09171234567", html)
        self.assertNotIn("Account name", html)

    def test_a_shop_with_no_gcash_details_at_all_still_renders(self):
        self.set_qr(gcash_qr_url="", gcash_account_name="", gcash_number="")
        self.client.force_login(self.customer)
        response = self.client.get(reverse("gowns:gcash_qr"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('class="qr-box"', response.content.decode())

    def test_no_template_syntax_leaks_and_the_page_is_post_free(self):
        self.set_qr()
        self.client.force_login(self.customer)
        html = self.client.get(reverse("gowns:gcash_qr")).content.decode()
        for leak in ("{%", "{{", "{#"):
            self.assertNotIn(leak, html)
        self.assertNotIn("<form", html)                                     # nothing on it can change a reservation
        self.assertEqual(self.client.post(reverse("gowns:gcash_qr")).status_code, 200)    # a plain page: no state, nothing written


class CheckoutQrWindowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="qr_window_customer", password="x")

    def setUp(self):
        self.client.force_login(self.customer)
        SiteSettings.objects.update_or_create(pk=1, defaults={
            "gcash_account_name": "Arabela Gown Rental", "gcash_number": "09171234567", "gcash_qr_url": QR_URL})

    def html(self):
        return self.client.get(reverse("gowns:reservation")).content.decode()

    def test_the_picture_is_no_longer_squeezed_into_a_small_square(self):
        html = self.html()
        self.assertNotIn("w-56 h-56", html)
        self.assertRegex(html, r'<img id="gcash-qr-img"[^>]*class="block w-full h-auto object-contain"[^>]*style="max-height: 70vh;"')
        self.assertIn("max-w-[22rem]", html)
        self.assertIn("max-w-lg editorial-shadow", html)                    # the window itself is a little wider

    def test_the_picture_and_a_view_full_qr_link_open_the_full_page_in_a_new_tab(self):
        html = self.html()
        url = reverse("gowns:gcash_qr")
        self.assertEqual(len(re.findall(r'<a href="%s" target="_blank" rel="noopener"' % re.escape(url), html)), 2)
        self.assertIn('id="gcash-qr-open"', html)
        self.assertIn('id="gcash-qr-full-link"', html)
        self.assertIn("View full QR", html)

    def test_save_qr_image_and_the_old_script_hooks_are_still_there(self):
        html = self.html()
        self.assertIn('id="gcash-save-qr"', html)
        self.assertIn("Save QR image", html)
        self.assertIn("document.getElementById('gcash-save-qr')", html)      # the window's save script is untouched
        self.assertIn("document.getElementById('gcash-qr-img')", html)
        self.assertIn("Scan this with another phone, or save it and upload it in GCash.", html)

    def test_without_a_qr_there_is_no_link_to_the_full_page(self):
        SiteSettings.objects.filter(pk=1).update(gcash_qr_url="")
        html = self.html()
        self.assertIn("QR code coming soon", html)
        self.assertNotIn(reverse("gowns:gcash_qr"), html)
        self.assertNotIn('id="gcash-qr-img"', html)

    def test_the_reference_number_box_and_everything_that_fed_it_are_gone(self):
        html = self.html()
        for gone in ("gcash_reference", "GCash reference number", "gcashRefEl", "refInput"):
            self.assertNotIn(gone, html)
        # ...while the other checkout boxes and their remembered-typing list are untouched
        self.assertIn("var DRAFT_FIELD_IDS = ['first_name', 'last_name', 'address', 'city', 'postal', 'phone'];", html)
        for field_id in ("first_name", "last_name", "address", "city", "postal", "phone", "proof_of_payment"):
            self.assertIn(f'id="{field_id}"', html)

    def test_no_template_syntax_leaks_into_the_checkout_page(self):
        html = self.html()
        for leak in ("{%", "{{", "{#"):
            self.assertNotIn(leak, html)


class CheckoutWithoutTheReferenceTests(TestCase):
    """The page no longer sends gcash_reference; a booking made without it is saved exactly as before."""

    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="qr_submit_customer", password="x")

    def test_a_booking_is_saved_when_the_page_sends_no_reference(self):
        from gowns.tests import _make_proof
        import json
        from reservations.models import Reservation
        self.client.force_login(self.customer)
        gown = Gown.objects.create(
            gown_id="QRPAGE-0001", name="Qr Page Gown", category=Gown.Category.GUEST_GOWN, color_name="Red", color_code="RD",
            size=Gown.Size.MEDIUM, rental_price=Decimal("3000.00"), status=Gown.Status.AVAILABLE)
        payload = {
            "items": json.dumps([{"gown_name": gown.name, "gown_slug": gown.slug, "size": "Medium",
                                  "rental_date": "2027-05-10", "return_date": "2027-05-13"}]),
            "first_name": "Test", "last_name": "Buyer", "phone": "09171234567", "address": "1 St", "city": "City",
            "postal_code": "1000", "payment_method": "GCash", "proof_of_payment": _make_proof(),
        }
        self.assertNotIn("gcash_reference", payload)
        with patch("gowns.views._save_proof_file", return_value="https://example.test/fake-proof.jpg"):
            response = self.client.post(reverse("gowns:reservation_submit"), data=payload)
        self.assertEqual(response.status_code, 200, response.content)
        reservation = Reservation.objects.get(reference_code=response.json()["reference_code"])
        self.assertEqual(reservation.gcash_reference, "")
