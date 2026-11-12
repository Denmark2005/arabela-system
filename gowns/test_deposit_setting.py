"""The security deposit is the OWNER's setting (Edit Profile > Security deposit): whole pesos per gown, 1 to 10,000, default 2,000.

One number drives the checkout, the walk-in New Booking, the page scripts, the FAQ / Terms / How-it-works wording and the AI chat. These
tests prove: who may change it, what it refuses, that 5 gowns at 1 peso is 5 pesos everywhere, that a booking keeps the deposit it was
made with, and that a checkout page opened before the owner changed it is told so instead of charging a different amount."""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from accounts.test_support import agreed
from gowns import deposit
from gowns.models import Gown, SiteSettings
from gowns.sample_images import unique_jpeg
from reservations import staff_booking
from reservations.models import Reservation

User = get_user_model()
SAVE = reverse("arabela_admin:deposit_setting_update")


def make_admin(username, role=None, **flags):
    user = User.objects.create_user(username, f"{username}@example.com", "Pass-12345!", is_staff=True, **flags)
    if role is not None:
        UserProfile.objects.update_or_create(user=user, defaults={"role": role})
    return user


def set_deposit(amount):
    shop = SiteSettings.load()
    shop.security_deposit_per_gown = amount
    shop.save()


def make_gown(number, name="Deposit Gown"):
    return Gown.objects.create(
        gown_id=f"Wedding Gown-DP-{number:03d}", name=f"{name} {number}", category=Gown.Category.WEDDING_GOWN, color_name="White",
        color_code="WH", size=Gown.Size.MEDIUM, rental_price=Decimal("3000"), status=Gown.Status.AVAILABLE)


class DepositHelperTests(TestCase):
    def test_the_default_is_two_thousand_and_the_range_is_one_to_ten_thousand(self):
        self.assertEqual(SiteSettings.load().security_deposit_per_gown, 2000)
        self.assertEqual((deposit.DEFAULT_PER_GOWN, deposit.MIN_PER_GOWN, deposit.MAX_PER_GOWN), (2000, 1, 10000))
        self.assertEqual(deposit.per_gown(), 2000)

    def test_only_whole_pesos_inside_the_range_are_accepted(self):
        for good in (1, 5, 2000, 10000, "1", " 250 ", "5,000", 7.0, Decimal("12")):
            self.assertIsNotNone(deposit.clean(good), good)
        for bad in (0, -3, 10001, 1.5, "abc", "", "1.5", None, True, "12 pesos", [], Decimal("1.5"), "-5"):
            self.assertIsNone(deposit.clean(bad), bad)

    def test_the_total_is_per_gown_times_the_number_of_gowns(self):
        set_deposit(1)
        self.assertEqual(deposit.for_gowns(5), Decimal("5"))
        set_deposit(2000)
        self.assertEqual(deposit.for_gowns(3), Decimal("6000"))

    def test_a_value_somehow_outside_the_range_falls_back_to_the_default(self):
        SiteSettings.objects.filter(pk=1).update(security_deposit_per_gown=0)
        self.assertEqual(deposit.per_gown(), 2000)
        SiteSettings.objects.filter(pk=1).update(security_deposit_per_gown=99999)
        self.assertEqual(deposit.per_gown(), 2000)

    def test_how_the_amount_is_written(self):
        self.assertEqual(deposit.php_text(2000), "Php 2,000.00")
        self.assertEqual(deposit.php_text(1), "Php 1.00")
        self.assertEqual(deposit.php_short(2000), "Php 2,000")
        self.assertEqual(deposit.peso_sign(2000), "P2,000")


class OnlyTheOwnerCanChangeItTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser("dp_super", "dp_super@example.com", "Pass-12345!")
        cls.owner = make_admin("dp_owner", UserProfile.Role.OWNER)
        cls.manager = make_admin("dp_manager", UserProfile.Role.MANAGER)
        cls.staff = make_admin("dp_staff", UserProfile.Role.STAFF)
        cls.no_profile = make_admin("dp_noprofile")                       # an admin with no profile row is not the owner for the server, so not for the page either
        cls.customer = User.objects.create_user("dp_customer", "dp_customer@example.com", "Pass-12345!")

    def save(self, user, amount):
        self.client.force_login(user)
        return self.client.post(SAVE, data=json.dumps({"amount": amount}), content_type="application/json")

    def test_the_owner_changes_it_and_it_is_stored(self):
        for user in (self.owner, self.superuser):
            response = self.save(user, 1)
            self.assertEqual(response.status_code, 200, user)
            self.assertEqual(response.json(), {"success": True, "amount": 1, "is_default": False})
            self.assertEqual(SiteSettings.load().security_deposit_per_gown, 1)
        self.assertTrue(self.save(self.owner, "2000").json()["is_default"])
        self.assertEqual(deposit.per_gown(), 2000)

    def test_staff_and_manager_are_refused_and_nothing_changes(self):
        for user in (self.staff, self.manager, self.no_profile):
            response = self.save(user, 5)
            self.assertEqual(response.status_code, 403, user)
            self.assertIn("Only the owner", response.json()["error"])
        self.assertEqual(SiteSettings.load().security_deposit_per_gown, 2000)

    def test_a_customer_or_a_signed_out_visitor_cannot_reach_it(self):
        self.assertEqual(self.save(self.customer, 5).status_code, 401)
        self.client.logout()
        self.assertEqual(self.client.post(SAVE, data=json.dumps({"amount": 5}), content_type="application/json").status_code, 401)
        self.assertEqual(SiteSettings.load().security_deposit_per_gown, 2000)

    def test_a_get_is_not_allowed(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(SAVE).status_code, 405)

    def test_amounts_that_are_not_whole_pesos_in_range_are_refused_and_nothing_changes(self):
        for bad in (0, -1, 10001, 1.5, "abc", "", None, True, [5], {"a": 1}):
            response = self.save(self.owner, bad)
            self.assertEqual(response.status_code, 400, bad)
            self.assertIn("whole number of pesos from 1 to 10,000", response.json()["error"])
        self.assertEqual(SiteSettings.load().security_deposit_per_gown, 2000)

    def test_the_edges_are_allowed(self):
        self.assertEqual(self.save(self.owner, 1).status_code, 200)
        self.assertEqual(self.save(self.owner, 10000).status_code, 200)
        self.assertEqual(SiteSettings.load().security_deposit_per_gown, 10000)

    def test_garbage_bodies_are_refused_cleanly(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(SAVE, data="{not json", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post(SAVE, data="[1, 2]", content_type="application/json").status_code, 400)

    def test_the_edit_profile_page_has_the_card_for_the_owner_only(self):
        page = reverse("arabela_admin:page", kwargs={"page": "profile"})
        self.client.force_login(self.owner)
        html = self.client.get(page).content.decode()
        self.assertIn('x-data="depositCard()"', html)
        self.assertIn('data-amount="2000"', html)
        self.assertIn("Test amount", html)                                # the yellow note exists (hidden until the amount is not 2,000)
        for user in (self.staff, self.manager, self.no_profile):
            self.client.force_login(user)
            self.assertNotIn('x-data="depositCard()"', self.client.get(page).content.decode(), user)

    def test_the_card_shows_the_saved_amount(self):
        set_deposit(7)
        self.client.force_login(self.owner)
        html = self.client.get(reverse("arabela_admin:page", kwargs={"page": "profile"})).content.decode()
        self.assertIn('data-amount="7"', html)


class WordsAndScriptsFollowTheSettingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user("dp_words", "dp_words@example.com", "Pass-12345!")

    def html(self, name):
        self.client.force_login(self.customer)
        agreed(self.customer)
        response = self.client.get(reverse(name))
        self.assertEqual(response.status_code, 200, name)
        return response.content.decode()

    def test_the_pages_say_2000_by_default(self):
        for name in ("gowns:faqs", "gowns:how_it_works", "gowns:terms_and_conditions"):
            self.assertIn("Php 2,000.00", self.html(name), name)

    def test_the_pages_say_the_owners_amount_and_never_the_old_one(self):
        set_deposit(1)
        for name in ("gowns:faqs", "gowns:how_it_works", "gowns:terms_and_conditions"):
            html = self.html(name)
            self.assertIn("Php 1.00", html, name)
            self.assertNotIn("Php 2,000.00", html, name)

    def test_the_bag_the_checkout_and_the_footer_scripts_use_the_amount(self):
        set_deposit(7)
        self.assertIn("const DEPOSIT_PER_ITEM = 7;", self.html("gowns:selection"))
        reservation_page = self.html("gowns:reservation")
        self.assertIn('data-deposit-per-gown="7"', reservation_page)
        self.assertIn("const SECURITY_DEPOSIT_PESOS = 7;", reservation_page)

    def test_the_terms_summary_uses_the_short_form(self):
        set_deposit(350)
        self.client.force_login(self.customer)
        response = self.client.get(reverse("gowns:selection"))
        self.assertIn("Php 350 for every booking", response.content.decode())

    def test_the_ai_chat_quotes_the_current_amount(self):
        from ai_recommendation import views as ai

        set_deposit(1500)
        prompt = ai._system_prompt()
        self.assertIn("P1,500 security deposit for each gown", prompt)
        self.assertNotIn("P2,000", prompt)


class CheckoutUsesTheSettingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customers = {key: User.objects.create_user(f"dp_buyer_{key}", f"dp_buyer_{key}@example.com", "Pass-12345!") for key in "ab"}
        cls.gowns = [make_gown(n) for n in range(1, 8)]

    def setUp(self):
        patcher = patch("gowns.views._save_proof_file", return_value="https://example.test/fake-proof.jpg")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.event = timezone.localdate() + timedelta(days=15)

    def checkout(self, who, gowns, *, expected=None, event=None):
        event = event or self.event
        items = [{"gown_name": g.name, "gown_slug": g.slug, "size": "Medium", "rental_date": (event - timedelta(days=2)).isoformat(),
                  "return_date": (event + timedelta(days=2)).isoformat()} for g in gowns]
        data = {
            "items": json.dumps(items), "first_name": "Dee", "last_name": "Buyer", "phone": "09171234567", "address": "1 Test St",
            "city": "Test City", "postal_code": "1000", "payment_method": "GCash",
            "proof_of_payment": SimpleUploadedFile("proof.jpg", unique_jpeg(), content_type="image/jpeg"),
        }
        if expected is not None:
            data["expected_deposit"] = str(expected)
        client = Client()
        client.force_login(self.customers[who])
        agreed(self.customers[who])
        return client.post(reverse("gowns:reservation_submit"), data=data)

    def booking(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        return Reservation.objects.get(reference_code=response.json()["reference_code"])

    def test_the_default_deposit_is_2000_per_gown(self):
        reservation = self.booking(self.checkout("a", self.gowns[:3], expected=6000))
        self.assertEqual(reservation.security_deposit, Decimal("6000.00"))
        self.assertEqual(reservation.total_amount, reservation.rental_subtotal + Decimal("6000.00"))

    def test_one_peso_per_gown_makes_five_gowns_five_pesos(self):
        set_deposit(1)
        reservation = self.booking(self.checkout("a", self.gowns[:5], expected=5))
        self.assertEqual(reservation.security_deposit, Decimal("5.00"))
        self.assertEqual(reservation.total_amount, reservation.rental_subtotal + Decimal("5.00"))

    def test_a_page_opened_before_the_owner_changed_it_is_told_and_nothing_is_booked(self):
        set_deposit(1)
        response = self.checkout("a", self.gowns[:2], expected=4000)       # the page still showed 2 x 2,000
        self.assertEqual(response.status_code, 409)
        data = response.json()
        self.assertTrue(data["deposit_changed"])
        self.assertEqual(data["deposit_per_gown"], 1)
        self.assertIn("changed to ₱1 per gown", data["error"])
        self.assertIn("now ₱2", data["error"])
        self.assertFalse(Reservation.objects.exists())

    def test_an_old_page_that_sends_no_expected_amount_is_still_served_the_current_deposit(self):
        set_deposit(3)
        reservation = self.booking(self.checkout("a", self.gowns[:2]))
        self.assertEqual(reservation.security_deposit, Decimal("6.00"))

    def test_a_booking_keeps_the_deposit_it_was_made_with(self):
        first = self.booking(self.checkout("a", self.gowns[:1], expected=2000))
        set_deposit(1)
        second = self.booking(self.checkout("b", self.gowns[1:2], expected=1, event=self.event + timedelta(days=10)))
        first.refresh_from_db()
        self.assertEqual(first.security_deposit, Decimal("2000.00"))
        self.assertEqual(second.security_deposit, Decimal("1.00"))

    def test_the_customer_cannot_send_their_own_deposit(self):
        set_deposit(1)
        self.client.force_login(self.customers["a"])
        response = self.checkout("a", self.gowns[:1], expected=1)
        reservation = self.booking(response)
        self.assertEqual(reservation.security_deposit, Decimal("1.00"))


class WalkInBookingUsesTheSettingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_admin("dp_walk_staff", UserProfile.Role.STAFF)
        cls.gown = make_gown(50, name="Walk In Gown")

    def setUp(self):
        for target in ("arabela_admin.new_booking._save_receipt_photo", "arabela_admin.views._save_receipt_photo"):
            patcher = patch(target, side_effect=lambda f: f"https://example.test/r/{f.name}")
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client.force_login(self.staff)

    def book(self):
        event = timezone.localdate() + timedelta(days=20)
        lines = [{"gown": self.gown.id, "event": event.isoformat(), "takes_now": False, "picked_up_on": ""}]
        data = {
            "channel": "Walk-in", "name": "Lorna Reyes", "phone": "0917 123 4567", "address": "Antipolo", "notebook_number": "", "account": "",
            "lines": json.dumps(lines), "payment": "full", "note": "",
            "receipt_1": SimpleUploadedFile("receipt1.jpg", unique_jpeg(), content_type="image/jpeg"),
        }
        return self.client.post(reverse("arabela_admin:booking_create"), data=data)

    def test_a_walk_in_booking_adds_the_owners_deposit(self):
        set_deposit(1)
        response = self.book()
        self.assertEqual(response.status_code, 200, response.content)
        reservation = Reservation.objects.get(channel=Reservation.Channel.WALK_IN)
        self.assertEqual(reservation.security_deposit, Decimal("1.00"))
        self.assertEqual(reservation.total_amount, reservation.rental_subtotal + Decimal("1.00"))

    def test_the_new_booking_page_tells_its_script_the_amount(self):
        set_deposit(9)
        page = self.client.get(reverse("arabela_admin:new_booking"))
        self.assertEqual(page.status_code, 200)
        self.assertIn("DEPOSIT: 9,", page.content.decode())

    def test_the_starting_default_in_the_module_is_only_a_default(self):
        self.assertEqual(staff_booking.DEPOSIT_PER_GOWN, Decimal("2000"))
        set_deposit(4)
        self.assertEqual(deposit.for_gowns(2), Decimal("8"))
