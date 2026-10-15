"""One browser, two sign-ins: the admin panel and the customer site keep their sign-in in separate cookies, a Google
sign-in can never land in a staff account, a checkout page refuses to book for an account it was not opened for, and a
skipped customer email says why.

Why: a browser keeps ONE session cookie per web address, so signing in to the admin panel in one tab used to replace the
customer's sign-in in every other tab -- the customer tab "changed account" on refresh, and a booking made in it was saved
(and emailed) under the wrong account."""
import json
from datetime import timedelta
from unittest.mock import patch

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts import customer_emails as ce
from accounts.adapters import STAFF_GOOGLE_MESSAGE, ArabelaSocialAccountAdapter
from accounts.tests import _ensure_google_social_app
from arabela_system.middleware import admin_cookie_name
from gowns.test_event_dates import make_unit
from reservations.models import Reservation

User = get_user_model()
STAFF_PASSWORD = "Staff-Pass-12345"
_TINY_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


class SeparateSessionsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="split_customer", email="split.customer@gmail.com", password="x")
        cls.staff = User.objects.create_user(username="split_staff", password=STAFF_PASSWORD, is_staff=True)

    def setUp(self):
        self.badges = reverse("gowns:notification_badges")
        self.dashboard = reverse("arabela_admin:dashboard")
        self.admin_login = reverse("arabela_admin:admin_login")

    def sign_in_customer(self):
        self.client.force_login(self.customer)

    def sign_in_admin(self, **extra):
        response = self.client.post(self.admin_login, {"username": "split_staff", "password": STAFF_PASSWORD, **extra})
        self.assertEqual(response.status_code, 302, "the admin sign-in itself should have worked")

    def customer_now(self):
        """Who the customer site thinks is signed in: the user id the badges endpoint reports, or None when signed out."""
        response = self.client.get(self.badges)
        return response.json().get("user") if response.status_code == 200 else None

    def test_signing_in_to_the_admin_panel_does_not_replace_the_customers_sign_in(self):
        self.sign_in_customer()
        self.assertEqual(self.customer_now(), self.customer.pk)
        self.sign_in_admin()
        self.assertEqual(self.customer_now(), self.customer.pk)                 # the customer tab is still the customer
        self.assertEqual(self.client.get(self.dashboard).status_code, 200)      # and the admin is signed in too
        self.assertNotEqual(self.client.cookies["sessionid"].value, self.client.cookies[admin_cookie_name()].value)

    def test_the_customer_site_never_sees_the_admins_sign_in(self):
        self.sign_in_admin()
        self.assertEqual(self.client.get(self.badges).status_code, 401)         # still a visitor on the customer site
        self.assertEqual(self.client.get(self.dashboard).status_code, 200)

    def test_the_admin_panel_never_uses_an_ordinary_customers_sign_in(self):
        self.sign_in_customer()
        response = self.client.get(self.dashboard)
        self.assertRedirects(response, self.admin_login, fetch_redirect_response=False)
        self.assertEqual(self.customer_now(), self.customer.pk)                 # and looking did not sign the customer out

    def test_the_admin_signing_out_leaves_the_customer_signed_in(self):
        self.sign_in_customer()
        self.sign_in_admin()
        self.client.post(reverse("arabela_admin:admin_logout"))
        self.assertEqual(self.customer_now(), self.customer.pk)
        self.assertEqual(self.client.get(self.dashboard).status_code, 302)

    def test_the_customer_signing_out_leaves_the_admin_signed_in(self):
        self.sign_in_customer()
        self.sign_in_admin()
        self.client.get(reverse("accounts:logout"))
        self.assertIsNone(self.customer_now())
        self.assertEqual(self.client.get(self.dashboard).status_code, 200)

    def test_keep_me_signed_in_only_affects_the_admin_cookie(self):
        self.sign_in_customer()
        self.sign_in_admin()                                                     # no "keep me signed in": ends with the browser
        self.assertFalse(self.client.cookies[admin_cookie_name()]["max-age"])
        self.client.post(reverse("arabela_admin:admin_logout"))
        self.sign_in_admin(remember_me="on")
        self.assertEqual(int(self.client.cookies[admin_cookie_name()]["max-age"]), settings.REMEMBER_ME_AGE)
        self.assertFalse(self.client.cookies["sessionid"]["max-age"])            # the customer's own cookie was never touched

    def test_a_staff_sign_in_made_before_the_split_still_works_in_the_admin_panel(self):
        self.client.force_login(self.staff)                                      # the old single cookie
        self.assertEqual(self.client.get(self.dashboard).status_code, 200)
        self.assertEqual(admin_cookie_name() in self.client.cookies, False)      # nothing new was needed

    def test_a_carried_over_staff_sign_in_stops_working_when_the_account_is_switched_off(self):
        self.client.force_login(self.staff)
        User.objects.filter(pk=self.staff.pk).update(is_active=False)
        self.assertEqual(self.client.get(self.dashboard).status_code, 302)

    def test_the_admin_session_is_kept_in_its_own_session_row(self):
        self.sign_in_admin()
        admin_session = SessionStore(self.client.cookies[admin_cookie_name()].value)
        self.assertEqual(str(admin_session["_auth_user_id"]), str(self.staff.pk))
        self.assertNotIn("sessionid", self.client.cookies)


class GoogleNeverSignsInAStaffAccountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _ensure_google_social_app()

    def login_for(self, email, user=None, uid="gid-1"):
        return SocialLogin(user=user or User(email=email), account=SocialAccount(provider="google", uid=uid))

    def request(self):
        return self.client.get(reverse("gowns:homepage")).wsgi_request

    def refused(self, sociallogin):
        with self.assertRaises(ImmediateHttpResponse) as caught:
            ArabelaSocialAccountAdapter().pre_social_login(self.request(), sociallogin)
        return caught.exception.response

    def test_a_google_address_that_belongs_to_a_staff_account_is_refused_with_a_clear_message(self):
        owner = User.objects.create_user(username="shop_owner", email="shop.owner@gmail.com", password="x", is_staff=True)
        response = self.refused(self.login_for("Shop.Owner@gmail.com"))
        self.assertEqual(response.status_code, 403)
        self.assertIn(STAFF_GOOGLE_MESSAGE.split(",")[0], response.content.decode())
        self.assertFalse(SocialAccount.objects.filter(user=owner).exists())      # nothing was linked

    def test_a_superuser_is_refused_too(self):
        User.objects.create_superuser(username="boss", email="boss@gmail.com", password="x")
        self.refused(self.login_for("boss@gmail.com"))

    def test_a_google_sign_in_already_linked_to_a_staff_account_is_refused(self):
        owner = User.objects.create_user(username="linked_owner", email="linked.owner@gmail.com", password="x", is_staff=True)
        SocialAccount.objects.create(user=owner, provider="google", uid="old-link")
        sociallogin = self.login_for("linked.owner@gmail.com", user=owner, uid="old-link")
        sociallogin.account = SocialAccount.objects.get(uid="old-link")
        self.assertTrue(sociallogin.is_existing)
        self.refused(sociallogin)

    def test_a_customer_with_the_same_address_as_a_staff_account_is_still_connected(self):
        User.objects.create_user(username="staff_twin", email="twin@gmail.com", password="x", is_staff=True)
        customer = User.objects.create_user(username="customer_twin", email="twin@gmail.com", password="x")
        sociallogin = self.login_for("twin@gmail.com")
        ArabelaSocialAccountAdapter().pre_social_login(self.request(), sociallogin)
        self.assertEqual(sociallogin.user.pk, customer.pk)

    def test_ordinary_customers_are_unaffected(self):
        customer = User.objects.create_user(username="plain_customer", email="plain@gmail.com", password="x")
        sociallogin = self.login_for("plain@gmail.com")
        ArabelaSocialAccountAdapter().pre_social_login(self.request(), sociallogin)
        self.assertEqual(sociallogin.user.pk, customer.pk)
        fresh = self.login_for("brand.new@gmail.com")
        ArabelaSocialAccountAdapter().pre_social_login(self.request(), fresh)
        self.assertIsNone(fresh.user.pk)


class CheckoutRefusesTheWrongAccountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="guard_customer", email="guard@gmail.com", password="x")

    def setUp(self):
        patcher = patch("gowns.views._save_proof_file", return_value="https://example.test/fake-proof.jpg")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.client.force_login(self.customer)
        self.unit = make_unit(1)

    def submit(self, **extra):
        event = timezone.localdate() + timedelta(days=20)
        items = [{"gown_name": self.unit.name, "gown_slug": self.unit.slug, "size": "M",
                  "rental_date": (event - timedelta(days=2)).isoformat(), "return_date": (event + timedelta(days=2)).isoformat()}]
        return self.client.post(reverse("gowns:reservation_submit"), data={
            "items": json.dumps(items), "first_name": "Guard", "last_name": "Test", "phone": "09171234567", "address": "1 Test St",
            "city": "Test City", "postal_code": "1000", "payment_method": "GCash",
            "proof_of_payment": SimpleUploadedFile("proof.jpg", _TINY_JPEG, content_type="image/jpeg"), **extra})

    def test_a_page_opened_for_another_account_books_nothing(self):
        response = self.submit(expected_user=str(self.customer.pk + 1000))
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertTrue(body["account_changed"])
        self.assertIn("nothing was booked", body["error"])
        self.assertFalse(Reservation.objects.exists())

    def test_the_right_account_books_as_usual(self):
        response = self.submit(expected_user=str(self.customer.pk))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Reservation.objects.get().customer, self.customer)

    def test_an_older_page_that_sends_no_account_is_still_accepted(self):
        self.assertEqual(self.submit().status_code, 200)

    def test_the_checkout_page_sends_the_account_it_was_opened_for(self):
        html = self.client.get(reverse("gowns:reservation")).content.decode()
        self.assertIn("formData.append('expected_user'", html)
        self.assertIn(f'data-user-key="{self.customer.pk}"', html)


class PagesKnowWhichAccountTheyAreForTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="notice_customer", email="notice@gmail.com", password="x")

    def setUp(self):
        self.client.force_login(self.customer)

    def test_the_badges_endpoint_says_who_is_signed_in(self):
        self.assertEqual(self.client.get(reverse("gowns:notification_badges")).json()["user"], self.customer.pk)

    def test_every_page_that_loads_the_live_badges_says_which_account_it_is_for(self):
        for name in ("gowns:homepage", "gowns:messages", "gowns:orders"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn("customer-badges-live.js", html, name)
            self.assertTrue(f'data-user-key="{self.customer.pk}"' in html or f'data-user-id="{self.customer.pk}"' in html, name)


class SkippedEmailsSayWhyTests(TestCase):
    def test_a_staff_account_is_skipped_with_a_warning_that_names_the_reason(self):
        owner = User.objects.create_user(username="mail_owner", email="owner@gmail.com", password="x", is_staff=True)
        with self.assertLogs("accounts.customer_emails", level="WARNING") as logs:
            sent = ce._deliver(owner, ce.RECEIVED, {})
        self.assertFalse(sent)
        text = "\n".join(logs.output)
        self.assertIn("NOT sent", text)
        self.assertIn("staff account", text)
        self.assertIn(f"#{owner.pk}", text)
        self.assertNotIn("owner@gmail.com", text)                                # the address itself is never logged

    def test_the_other_reasons_are_named_too(self):
        self.assertIn("no email address", ce._why_not_deliverable(User(username="a", email="")))
        self.assertIn("placeholder", ce._why_not_deliverable(User(username="b", email="b@example.com")))
        self.assertIn("no account", ce._why_not_deliverable(None))
