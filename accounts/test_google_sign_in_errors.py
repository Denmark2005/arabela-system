"""When Google's answer can't be used (a second tap on the account, a refresh, a cancelled sign-in), the visitor never sees
allauth's bare "Third-Party Login Failure" page: someone already signed in goes home, anyone else goes back to the login page
with a plain explanation. These drive the REAL allauth callback URL, not just the adapter hook."""
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.tests import _ensure_google_social_app
from accounts.views import SIGN_IN_PROBLEMS

User = get_user_model()


class GoogleSignInErrorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _ensure_google_social_app()
        cls.customer = User.objects.create_user(username="google_error_customer", email="google.error@gmail.com", password="x")

    def setUp(self):
        self.callback = reverse("google_callback")
        self.login = reverse("accounts:login")

    def test_a_repeated_google_answer_for_someone_already_signed_in_goes_to_the_homepage(self):
        self.client.force_login(self.customer)
        response = self.client.get(self.callback, {"state": "already-used", "code": "already-used"})
        self.assertRedirects(response, reverse("gowns:homepage"), fetch_redirect_response=False)

    def test_an_answer_that_cannot_be_used_sends_a_signed_out_visitor_back_to_the_login_page_with_a_reason(self):
        response = self.client.get(self.callback, {"state": "unknown", "code": "unknown"})
        self.assertRedirects(response, f"{self.login}?problem=incomplete", fetch_redirect_response=False)
        page = self.client.get(response["Location"])
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Sign-in with Google didn")
        self.assertNotContains(page, "Third-Party Login Failure")

    def test_a_cancelled_google_sign_in_says_so_instead_of_showing_allauths_page(self):
        started = self.client.get(reverse("google_login"), {"process": "login"})          # what "Continue with Google" does
        state = parse_qs(urlparse(started["Location"]).query)["state"][0]                  # the real state Google would hand back
        response = self.client.get(self.callback, {"error": "access_denied", "state": state})   # the person pressed Cancel at Google
        self.assertRedirects(response, f"{self.login}?problem=cancelled", fetch_redirect_response=False)
        self.assertContains(self.client.get(response["Location"]), "Sign-in was cancelled")

    def test_the_login_page_only_ever_shows_its_own_words(self):
        for problem, text in SIGN_IN_PROBLEMS.items():
            self.assertContains(self.client.get(self.login, {"problem": problem}), text.split("'")[0])
        page = self.client.get(self.login, {"problem": "<script>alert(1)</script>"})
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "alert(1)")
        self.assertNotContains(page, 'id="error_container"')

    def test_the_continue_with_google_link_ignores_a_second_tap_from_the_first_moment(self):
        page = self.client.get(self.login)
        self.assertContains(page, "window.__googleTapped")                       # in the link's own onclick, no waiting for scripts
        self.assertContains(page, 'id="google_auth_link"')

    def test_someone_already_signed_in_is_not_shown_the_login_page_at_all(self):
        self.client.force_login(self.customer)
        self.assertRedirects(self.client.get(self.login, {"problem": "incomplete"}), reverse("gowns:homepage"), fetch_redirect_response=False)


class RepeatedGoogleAnswerKeepsTheCheckoutTests(TestCase):
    """The video: a customer signs in from "Proceed to Reservation", is taken to the checkout, then Chrome replays the whole sign-in
    (switching "Desktop site" on or off re-requests the ORIGINAL Google address), so Google's answer reaches the site a second
    time. The repeat fails, but the customer is already signed in -- they must land back on the checkout, not on the homepage."""

    @classmethod
    def setUpTestData(cls):
        _ensure_google_social_app()
        cls.customer = User.objects.create_user(username="repeat_customer", email="repeat.customer@gmail.com", password="x")

    def setUp(self):
        self.callback = reverse("google_callback")
        self.login = reverse("accounts:login")
        self.checkout = reverse("gowns:reservation")

    def start_sign_in_for_checkout(self):
        self.client.get(self.login, {"next": self.checkout})                # the login page, opened by "Proceed to Reservation"

    def repeat_answer(self):
        return self.client.get(self.callback, {"state": "already-used", "code": "already-used"})

    def test_a_repeated_answer_after_signing_in_goes_back_to_the_checkout(self):
        self.start_sign_in_for_checkout()
        self.client.force_login(self.customer)                              # the first answer signed them in
        self.assertRedirects(self.repeat_answer(), self.checkout, fetch_redirect_response=False)

    def test_it_keeps_working_for_every_repeat_not_just_the_first(self):
        self.start_sign_in_for_checkout()
        self.client.force_login(self.customer)
        for _ in range(3):
            self.assertRedirects(self.repeat_answer(), self.checkout, fetch_redirect_response=False)

    def test_with_no_known_destination_it_still_goes_to_the_homepage(self):
        self.client.get(self.login)                                          # opened the login page directly
        self.client.force_login(self.customer)
        self.assertRedirects(self.repeat_answer(), reverse("gowns:homepage"), fetch_redirect_response=False)

    def test_an_old_destination_is_not_used(self):
        self.start_sign_in_for_checkout()
        self.client.force_login(self.customer)
        with patch("accounts.login_next.time.time", return_value=time.time() + 16 * 60):
            self.assertRedirects(self.repeat_answer(), reverse("gowns:homepage"), fetch_redirect_response=False)

    def test_a_destination_on_another_website_is_never_followed(self):
        self.client.get(self.login, {"next": "https://evil.example/steal"})
        self.client.force_login(self.customer)
        self.assertRedirects(self.repeat_answer(), reverse("gowns:homepage"), fetch_redirect_response=False)
        session = self.client.session
        session["arabela_login_next"] = {"url": "https://evil.example/steal", "at": time.time()}      # even if one got into the session
        session.save()
        self.assertRedirects(self.repeat_answer(), reverse("gowns:homepage"), fetch_redirect_response=False)

    def test_a_damaged_value_in_the_session_is_ignored(self):
        self.client.force_login(self.customer)
        for bad in ("not a dict", {"url": "/reservation/", "at": "yesterday"}, {"url": None, "at": time.time()}, {}):
            session = self.client.session
            session["arabela_login_next"] = bad
            session.save()
            self.assertRedirects(self.repeat_answer(), reverse("gowns:homepage"), fetch_redirect_response=False)

    def test_a_failed_sign_in_for_someone_not_signed_in_keeps_the_destination_for_the_retry(self):
        self.start_sign_in_for_checkout()
        response = self.repeat_answer()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{self.login}?problem=incomplete&next=%2Freservation%2F")
        retry = self.client.get(response["Location"])
        self.assertContains(retry, "Sign-in with Google didn")
        self.assertContains(retry, "next=%2Freservation%2F")                  # the Continue with Google link still carries it

    def test_opening_the_login_page_without_a_destination_forgets_the_old_one(self):
        self.start_sign_in_for_checkout()
        self.client.get(self.login)
        self.client.force_login(self.customer)
        self.assertRedirects(self.repeat_answer(), reverse("gowns:homepage"), fetch_redirect_response=False)
