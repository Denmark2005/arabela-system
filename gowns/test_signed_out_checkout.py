"""A checkout or a receipt upload sent after the sign-in ended (2026-11-18).

login_required answered such a request with the sign-in page's HTML, which the page cannot read as the booking's answer: the checkout showed
"Server busy", although nothing was busy. Now the page is told plainly that it was signed out, and nothing is booked."""
import json
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import get_template
from django.test import Client, TestCase
from django.urls import reverse

from gowns.sample_images import unique_jpeg
from gowns.sample_payment import TYPED_AMOUNT, typed_reference
from reservations.models import Reservation


class ASignedOutCheckoutIsToldSoTests(TestCase):
    def post(self, name, *args):
        return Client().post(reverse(name, args=args), data={
            "items": json.dumps([{"gown_name": "X", "rental_date": "2027-01-01", "return_date": "2027-01-04"}]), "first_name": "A", "last_name": "B",
            "payment_method": "GCash", "gcash_reference": typed_reference(), "gcash_amount": TYPED_AMOUNT,
            "proof_of_payment": SimpleUploadedFile("p.jpg", unique_jpeg(), content_type="image/jpeg")})

    def test_confirm_rental_answers_json_not_the_sign_in_page(self):
        response = self.post("gowns:reservation_submit")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["Content-Type"], "application/json")
        data = response.json()
        self.assertEqual((data["success"], data["signed_out"]), (False, True))
        self.assertEqual(data["login_url"], reverse("accounts:login"))
        self.assertIn("signed out", data["error"])
        self.assertIn("nothing was booked", data["error"])
        self.assertEqual(Reservation.objects.count(), 0)

    def test_uploading_a_receipt_later_answers_the_same_way(self):
        response = self.post("gowns:reservation_upload_proof", 7)
        self.assertEqual((response.status_code, response.json()["signed_out"]), (401, True))

    def test_a_signed_in_customer_is_not_affected(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user("so_customer", password="pw-12345-abc")
        client = Client()
        client.force_login(user)
        with patch("gowns.views._terms_refusal", return_value=None):
            response = client.post(reverse("gowns:reservation_submit"), data={"items": "[]"})
        self.assertNotEqual(response.status_code, 401)                       # it gets its normal answer ("Your bag is empty." and so on)
        self.assertNotIn("signed_out", response.json())

    def test_the_wrong_method_is_still_refused_for_a_signed_in_customer(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user("so_customer2", password="pw-12345-abc")
        client = Client()
        client.force_login(user)
        self.assertEqual(client.get(reverse("gowns:reservation_submit")).status_code, 405)


class ThePagesKnowWhatToDoTests(TestCase):
    def source(self, name):
        return get_template(name).template.source

    def test_every_page_that_sends_a_receipt_handles_a_signed_out_answer(self):
        for name in ("reservation.html", "reservations.html", "reservation_item_detail.html"):
            with self.subTest(page=name):
                source = self.source(name)
                self.assertIn("signed_out", source)
                self.assertIn("You were signed out", source)
                self.assertIn("?next=", source)                              # comes back to the same page after signing in

    def test_the_busy_dialog_tells_the_customer_to_look_in_my_reservations_before_trying_again(self):
        source = self.source("reservation.html")
        self.assertIn("If you are not sure, check My Reservations first.", source)
        self.assertIn("You will not be booked twice.", source)
