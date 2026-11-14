"""Chrome on a phone replays the whole Google sign-in when "Desktop site" is switched on or off, so the SAME Google answer reaches the site a
second time. The first answer already signed the admin in; the repeat fails (Google's one-time code and the sign-in's state were used). That
repeat lands outside the admin area, where the admin sign-in cannot be seen, so it was taken for a customer's and sent to the customer pages.

Now the admin hand-over remembers WHICH answer it was (its "state" code, in a cookie). The same answer arriving again goes back to the admin
sign-in page, which sends a signed-in admin on to the dashboard. Everything else -- a customer's answer, a different answer, a failed admin
sign-in -- behaves exactly as before."""
from unittest import mock

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.providers.base import AuthError
from django.contrib.auth.models import AnonymousUser
from django.test import Client, RequestFactory
from django.urls import reverse

from accounts import admin_google
from accounts.adapters import ArabelaSocialAccountAdapter
from arabela_admin.test_google_admin_signin import Base, google_answer

STATE = "Qw3rTy9uIoPa"
CALLBACK = "/accounts/google/login/callback/"


def hand_over(email, state):
    """Run the admin hand-over for a Google answer that carried `state`; returns the response the adapter stopped with."""
    request = RequestFactory().get(CALLBACK, {"state": state} if state else {})
    request.user, request.session = AnonymousUser(), {}
    with mock.patch("django.contrib.messages.get_messages", return_value=[]):
        try:
            ArabelaSocialAccountAdapter().pre_social_login(request, google_answer(email))
        except ImmediateHttpResponse as stop:
            return stop.response
    raise AssertionError("the adapter let an admin Google answer through to the normal sign-in")


def failed_answer(state, cookies):
    request = RequestFactory().get(CALLBACK, {"state": state} if state else {})
    request.user, request.session = AnonymousUser(), {}
    request.COOKIES.update(cookies)
    with mock.patch("django.contrib.messages.get_messages", return_value=[]):
        try:
            ArabelaSocialAccountAdapter().on_authentication_error(request, "google", error=AuthError.UNKNOWN)
        except ImmediateHttpResponse as stop:
            return stop.response
    raise AssertionError("the adapter did not stop the failed answer")


class TheHandOverRemembersTheAnswer(Base):
    def test_the_state_of_an_admin_answer_is_kept_in_a_cookie(self):
        response = hand_over("maria.staff@gmail.com", STATE)
        self.assertTrue(response.url.startswith(admin_google.finish_path() + "?t="))
        cookie = response.cookies[admin_google.STATE_COOKIE]
        self.assertEqual(cookie.value, STATE)
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(int(cookie["max-age"]), admin_google.STATE_COOKIE_MAX_AGE)

    def test_no_state_means_no_cookie_and_the_hand_over_still_works(self):
        response = hand_over("maria.staff@gmail.com", "")
        self.assertNotIn(admin_google.STATE_COOKIE, response.cookies)
        self.assertEqual(Client().get(response.url).status_code, 302)

    def test_a_refused_google_account_leaves_no_cookie(self):
        request = RequestFactory().get(CALLBACK, {"state": STATE})
        request.user, request.session = AnonymousUser(), {}
        with mock.patch("django.contrib.messages.get_messages", return_value=[]):
            with self.assertRaises(ImmediateHttpResponse) as stop:
                ArabelaSocialAccountAdapter().pre_social_login(request, google_answer("stranger@gmail.com"))
        self.assertNotIn(admin_google.STATE_COOKIE, stop.exception.response.cookies)

    def test_only_a_plain_state_code_is_accepted(self):
        for bad in ("", "ab", "<script>alert(1)</script>", "a" * 200, "x y z w", "ok\nline"):
            with self.subTest(bad=bad):
                request = RequestFactory().get(CALLBACK, {"state": bad})
                self.assertEqual(admin_google.answer_state(request), "")
        self.assertEqual(admin_google.answer_state(RequestFactory().get(CALLBACK, {"state": STATE})), STATE)


class TheSameAnswerTwice(Base):
    def test_the_repeat_goes_to_the_admin_sign_in_page_not_the_customer_pages(self):
        response = failed_answer(STATE, {admin_google.STATE_COOKIE: STATE})
        self.assertEqual(response.url, reverse("arabela_admin:admin_login"))

    def test_a_signed_in_admin_ends_up_on_the_dashboard(self):
        client = Client()
        client.get(hand_over("maria.staff@gmail.com", STATE).url)           # the first answer signs her in (admin cookie)
        client.cookies[admin_google.STATE_COOKIE] = STATE
        replay = client.get(CALLBACK, {"code": "already-used", "state": STATE})
        self.assertEqual(replay.status_code, 302)
        self.assertEqual(replay["Location"], reverse("arabela_admin:admin_login"))
        landing = client.get(replay["Location"])
        self.assertEqual(landing.status_code, 302)
        self.assertEqual(landing["Location"], reverse("arabela_admin:dashboard"))
        self.assertEqual(client.get(landing["Location"]).status_code, 200)

    def test_the_same_when_google_sends_an_error_with_the_repeat(self):
        client = Client()
        client.get(hand_over("maria.staff@gmail.com", STATE).url)
        client.cookies[admin_google.STATE_COOKIE] = STATE
        replay = client.get(CALLBACK, {"error": "access_denied", "state": STATE})
        self.assertEqual(replay["Location"], reverse("arabela_admin:admin_login"))

    def test_if_the_admin_session_has_ended_the_sign_in_page_is_shown(self):
        client = Client()
        client.cookies[admin_google.STATE_COOKIE] = STATE
        replay = client.get(CALLBACK, {"code": "already-used", "state": STATE})
        self.assertEqual(replay["Location"], reverse("arabela_admin:admin_login"))
        page = client.get(replay["Location"])
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Continue with Google")

    def test_a_different_answer_is_not_mistaken_for_the_repeat(self):
        response = failed_answer("AnotherState01", {admin_google.STATE_COOKIE: STATE})
        self.assertTrue(response.url.startswith(reverse("accounts:login")), response.url)

    def test_an_answer_without_a_state_is_not_mistaken_for_the_repeat(self):
        response = failed_answer("", {admin_google.STATE_COOKIE: STATE})
        self.assertTrue(response.url.startswith(reverse("accounts:login")), response.url)

    def test_a_customers_failed_answer_is_exactly_as_before(self):
        client = Client()
        replay = client.get(CALLBACK, {"code": "already-used", "state": STATE})
        self.assertEqual(replay.status_code, 302)
        self.assertTrue(replay["Location"].startswith(reverse("accounts:login")), replay["Location"])
        self.assertIn("problem=incomplete", replay["Location"])
        refused = client.get(CALLBACK, {"error": "access_denied", "state": STATE})
        self.assertTrue(refused["Location"].startswith(reverse("accounts:login") + "?problem="), refused["Location"])

    def test_a_failed_admin_sign_in_while_its_page_was_open_still_comes_back_to_it(self):
        client = Client()
        client.cookies[admin_google.TRY_COOKIE] = "1"
        replay = client.get(CALLBACK, {"error": "access_denied", "state": "NewStateAbc12"})
        self.assertTrue(replay["Location"].startswith(reverse("arabela_admin:admin_login") + "?problem="), replay["Location"])


class TheHandOverLinkTwice(Base):
    def test_a_signed_in_admin_who_opens_the_used_link_again_goes_to_the_dashboard(self):
        link = hand_over("maria.staff@gmail.com", STATE).url
        client = Client()
        first = client.get(link)
        self.assertEqual(first["Location"], reverse("arabela_admin:dashboard"))
        again = client.get(link)                                              # the browser replays it: she is already signed in
        self.assertEqual(again.status_code, 302)
        self.assertEqual(again["Location"], reverse("arabela_admin:dashboard"))

    def test_someone_who_is_not_signed_in_still_sees_the_expired_message(self):
        link = hand_over("maria.staff@gmail.com", STATE).url
        Client().get(link)
        second = Client().get(link)
        self.assertEqual(second.status_code, 400)
        self.assertContains(second, "expired", status_code=400)

    def test_a_forged_link_is_never_forgiven_even_when_signed_in(self):
        client = Client()
        client.get(hand_over("maria.staff@gmail.com", STATE).url)
        forged = client.get(admin_google.finish_path(), {"t": "not-a-real-link"})
        self.assertEqual(forged.status_code, 400)

    def test_a_used_gmail_change_link_keeps_its_own_message(self):
        token = admin_google.make_signin_token(self.staff, change_to="new.staff@gmail.com")
        client = Client()
        self.assertEqual(client.get(admin_google.finish_path(), {"t": token}).status_code, 302)    # the change goes through once
        again = client.get(admin_google.finish_path(), {"t": token})
        self.assertEqual(again.status_code, 400)
