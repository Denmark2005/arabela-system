"""Admin panel sign-in with Google (owner + staff): the sign-in page, the Gmail check, the hand-over to the admin area, staff created from a
Gmail, and the owner changing her own sign-in Gmail (confirmed with Google). No real Google call is made: the Google answer is faked."""
import json
from types import SimpleNamespace
from unittest import mock

from allauth.account.models import EmailAddress
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.providers.base import AuthError
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from accounts import admin_google
from accounts.adapters import ArabelaSocialAccountAdapter
from accounts.models import UserProfile
from accounts.tests import _ensure_google_social_app
from arabela_system.middleware import admin_cookie_name

User = get_user_model()
OWNER_GMAIL = "owner.shop@gmail.com"


def google_answer(email, verified=True, next_url=None):
    """What allauth hands to the adapter once Google has answered (only the parts the adapter reads)."""
    return SimpleNamespace(
        state={"next": admin_google.finish_path() if next_url is None else next_url},
        email_addresses=[EmailAddress(email=email, verified=verified, primary=True)],
        account=SimpleNamespace(extra_data={"email": email}),
        is_existing=False,
        user=User(email=email),
    )


def run_adapter(sociallogin, cookies=None):
    """Run the adapter exactly as allauth does. Returns the response it raised (it always stops the normal sign-in)."""
    request = RequestFactory().get("/accounts/google/login/callback/")
    request.user, request.session = AnonymousUser(), {}
    if cookies:
        request.COOKIES.update(cookies)
    with mock.patch("django.contrib.messages.get_messages", return_value=[]):
        try:
            ArabelaSocialAccountAdapter().pre_social_login(request, sociallogin)
        except ImmediateHttpResponse as stop:
            return stop.response
    raise AssertionError("the adapter let an admin Google answer through to the normal sign-in")


@override_settings(ADMIN_OWNER_GMAIL=OWNER_GMAIL)
class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        _ensure_google_social_app()          # the sign-in pages draw their "Continue with Google" link from this row
        cls.owner = User.objects.create_user(username="shop_owner", password="x", is_staff=True, is_superuser=True, email="old.owner@example.com")
        UserProfile.objects.create(user=cls.owner, role=UserProfile.Role.OWNER)
        cls.staff = User.objects.create_user(username="maria", password="x", is_staff=True, email="maria.staff@gmail.com")
        UserProfile.objects.create(user=cls.staff, role=UserProfile.Role.STAFF)

    def finish(self, client, response):
        """Follow the hand-over redirect the way the browser does."""
        self.assertEqual(response.status_code, 302, getattr(response, "content", b"")[:300])
        self.assertTrue(response.url.startswith(admin_google.finish_path() + "?t="), response.url)
        return client.get(response.url)


class SignInPageTests(Base):
    def test_the_page_is_google_only_with_the_logo_and_no_password_or_forgot_link(self):
        response = self.client.get(reverse("arabela_admin:admin_login"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Continue with Google", html)
        self.assertIn("logo/logo.svg", html)                                    # the Arabela logo stays
        self.assertIn("/accounts/google/login/", html)
        self.assertIn("next=%2Fadmin-panel%2Fgoogle-finish%2F", html)
        for gone in ('type="password"', 'name="username"', "Forgot password", "Keep me logged in", "Remember", "change-password"):
            self.assertNotIn(gone, html)

    def test_it_looks_like_the_customer_sign_in_page(self):
        customer = self.client.get(reverse("accounts:login")).content.decode()
        admin = self.client.get(reverse("arabela_admin:admin_login")).content.decode()
        for shared in ("inline-flex items-center justify-center gap-3 w-full py-3.5 text-sm font-semibold text-gray-700 transition-colors bg-gray-100 rounded-lg px-7",
                       "Continue with Google", "max-w-md mx-auto", "text-title-sm"):
            self.assertIn(shared, customer)
            self.assertIn(shared, admin)

    def test_typing_a_username_and_password_does_nothing(self):
        response = self.client.post(reverse("arabela_admin:admin_login"), {"username": "maria", "password": "x"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(reverse("arabela_admin:staff_management")).status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_signed_in_admin_goes_straight_to_the_dashboard(self):
        self.client.force_login(self.staff)
        self.assertRedirects(self.client.get(reverse("arabela_admin:admin_login")), reverse("arabela_admin:dashboard"), fetch_redirect_response=False)

    def test_the_old_password_page_is_gone(self):
        html = self.client.get(reverse("arabela_admin:admin_login")).content.decode()
        self.assertNotIn("/admin-login/password/", html)
        self.assertEqual(self.client.get("/admin-panel/admin-login/password/").status_code, 404)                 # removed: no password opens the admin panel

    def test_a_failed_google_answer_comes_back_to_the_admin_page_when_it_was_open(self):
        request = RequestFactory().get("/accounts/google/login/callback/")
        request.user, request.session = AnonymousUser(), {}
        request.COOKIES[admin_google.TRY_COOKIE] = "1"
        with self.assertRaises(ImmediateHttpResponse) as stop:
            ArabelaSocialAccountAdapter().on_authentication_error(request, "google", error=AuthError.CANCELLED)
        self.assertEqual(stop.exception.response.url, reverse("arabela_admin:admin_login") + "?problem=cancelled")
        page = self.client.get(stop.exception.response.url)
        self.assertContains(page, "Google sign-in was cancelled")

    def test_a_failed_customer_google_answer_still_goes_to_the_customer_page(self):
        request = RequestFactory().get("/accounts/google/login/callback/")
        request.user, request.session = AnonymousUser(), {}
        with self.assertRaises(ImmediateHttpResponse) as stop:
            ArabelaSocialAccountAdapter().on_authentication_error(request, "google", error=AuthError.CANCELLED)
        self.assertTrue(stop.exception.response.url.startswith(reverse("accounts:login")))


class WhoMayOpenTheAdminPanelTests(Base):
    def test_the_owner_gmail_opens_the_owner_account_and_is_attached_to_it(self):
        client = Client()
        response = self.finish(client, run_adapter(google_answer(OWNER_GMAIL)))
        self.assertRedirects(response, reverse("arabela_admin:dashboard"), fetch_redirect_response=False)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.email, OWNER_GMAIL)
        self.assertEqual(client.get(reverse("arabela_admin:staff_management")).status_code, 200)       # an owner-only page opens

    def test_the_session_is_the_admin_one_and_ends_when_the_browser_closes(self):
        client = Client()
        response = self.finish(client, run_adapter(google_answer(OWNER_GMAIL)))
        cookie = response.cookies[admin_cookie_name()]
        self.assertEqual((cookie["max-age"], cookie["expires"]), ("", ""))                              # a browser-session cookie: gone when the browser closes
        self.assertNotIn("sessionid", response.cookies)                                                 # nothing created on the customer site

    def test_the_gmail_match_ignores_capital_letters(self):
        client = Client()
        self.finish(client, run_adapter(google_answer("Maria.Staff@GMAIL.com")))
        self.assertEqual(client.get(reverse("arabela_admin:pending_approval")).status_code, 200)

    def test_a_staff_gmail_opens_that_staff_account_but_not_the_owner_pages(self):
        client = Client()
        self.finish(client, run_adapter(google_answer("maria.staff@gmail.com")))
        self.assertEqual(client.get(reverse("arabela_admin:pending_approval")).status_code, 200)
        self.assertEqual(client.get(reverse("arabela_admin:staff_management")).status_code, 302)       # owner-only page: sent back

    def test_any_other_google_account_is_refused_and_no_account_is_created(self):
        count = User.objects.count()
        response = run_adapter(google_answer("stranger@gmail.com"))
        self.assertEqual(response.status_code, 403)
        self.assertIn("not allowed to open the admin panel", response.content.decode())
        self.assertEqual(User.objects.count(), count)

    def test_a_google_email_that_google_did_not_confirm_is_refused(self):
        response = run_adapter(google_answer("maria.staff@gmail.com", verified=False))
        self.assertEqual(response.status_code, 403)
        self.assertIn("did not confirm", response.content.decode())

    def test_a_switched_off_staff_account_is_refused(self):
        User.objects.filter(pk=self.staff.pk).update(is_active=False)
        response = run_adapter(google_answer("maria.staff@gmail.com"))
        self.assertEqual(response.status_code, 403)
        self.assertIn("switched off", response.content.decode())

    def test_a_customer_account_cannot_open_the_admin_panel_even_with_the_same_gmail(self):
        User.objects.create_user(username="cust", password="x", email="shopper@gmail.com")
        self.assertEqual(run_adapter(google_answer("shopper@gmail.com")).status_code, 403)

    def test_the_owner_gmail_is_refused_when_a_customer_already_uses_it(self):
        User.objects.create_user(username="cust2", password="x", email=OWNER_GMAIL)
        response = run_adapter(google_answer(OWNER_GMAIL))
        self.assertEqual(response.status_code, 403)
        self.assertIn("customer account", response.content.decode())
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.email, "old.owner@example.com")

    def test_the_owner_gmail_is_refused_when_it_is_not_clear_which_account_is_the_owner(self):
        User.objects.create_user(username="second_super", password="x", is_staff=True, is_superuser=True)
        self.assertEqual(run_adapter(google_answer(OWNER_GMAIL)).status_code, 403)

    def test_the_hand_over_link_works_once(self):
        response = run_adapter(google_answer("maria.staff@gmail.com"))
        link = response.url
        first = Client().get(link)
        self.assertEqual(first.status_code, 302)
        second = Client().get(link)
        self.assertEqual(second.status_code, 400)
        self.assertContains(second, "expired", status_code=400)

    def test_an_old_or_forged_link_is_refused(self):
        token = admin_google.make_signin_token(self.staff)
        with mock.patch.object(admin_google, "SIGNIN_MAX_AGE", -1):
            self.assertEqual(Client().get(admin_google.finish_path(), {"t": token}).status_code, 400)
        self.assertEqual(Client().get(admin_google.finish_path(), {"t": token + "x"}).status_code, 400)
        self.assertEqual(Client().get(admin_google.finish_path()).status_code, 400)

    def test_a_link_stops_working_when_the_account_changes_its_gmail(self):
        token = admin_google.make_signin_token(self.staff)
        User.objects.filter(pk=self.staff.pk).update(email="other@gmail.com")
        self.assertEqual(Client().get(admin_google.finish_path(), {"t": token}).status_code, 400)

    def test_the_customer_site_still_refuses_an_admin_gmail(self):
        # An ordinary customer sign-in (not the admin page) is untouched: a staff Gmail may not become a customer session.
        with self.assertRaises(ImmediateHttpResponse):
            ArabelaSocialAccountAdapter().pre_social_login(self._request(), google_answer("maria.staff@gmail.com", next_url="/"))

    def _request(self):
        request = RequestFactory().get("/accounts/google/login/callback/")
        request.user, request.session = AnonymousUser(), {}
        return request


class StaffFromAGmailTests(Base):
    def test_a_staff_member_added_in_staff_management_can_sign_in_with_that_gmail(self):
        client = Client()
        client.force_login(self.owner)
        response = client.post(reverse("arabela_admin:staff_create"), data=json.dumps({"name": "Rina Reyes", "email": "Rina.Reyes@gmail.com", "permissions": []}),
                               content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        staff_client = Client()
        self.finish(staff_client, run_adapter(google_answer("rina.reyes@gmail.com")))
        self.assertEqual(staff_client.get(reverse("arabela_admin:pending_approval")).status_code, 200)

    def test_the_owner_removing_the_staff_account_ends_that_gmails_access(self):
        client = Client()
        client.force_login(self.owner)
        client.post(reverse("arabela_admin:staff_delete", args=[self.staff.pk]))
        self.assertEqual(run_adapter(google_answer("maria.staff@gmail.com")).status_code, 403)

    def test_changing_a_staff_gmail_moves_the_access_to_the_new_gmail(self):
        client = Client()
        client.force_login(self.owner)
        client.post(reverse("arabela_admin:staff_update", args=[self.staff.pk]), data=json.dumps({"name": "Maria Staff", "email": "maria.new@gmail.com"}),
                    content_type="application/json")
        self.assertEqual(run_adapter(google_answer("maria.staff@gmail.com")).status_code, 403)
        self.finish(Client(), run_adapter(google_answer("maria.new@gmail.com")))

    def test_the_staff_account_has_no_password_so_nothing_can_open_it_with_one(self):
        client = Client()
        client.force_login(self.owner)
        client.post(reverse("arabela_admin:staff_create"), data=json.dumps({"name": "Pat Pass", "email": "pat.pass@gmail.com"}), content_type="application/json")
        user = User.objects.get(email="pat.pass@gmail.com")
        self.assertFalse(user.has_usable_password())
        self.assertIsNone(authenticate(username=user.username, password=""))                  # an empty password never matches an unusable one
        self.assertEqual(Client().post("/admin-panel/admin-login/password/", {"username": user.username, "password": ""}).status_code, 404)

    def test_the_profile_page_can_no_longer_change_the_sign_in_gmail(self):
        client = Client()
        client.force_login(self.staff)
        client.post(reverse("arabela_admin:save_profile"), data=json.dumps({"firstName": "Maria", "email": "someone.else@gmail.com"}), content_type="application/json")
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.email, "maria.staff@gmail.com")
        page = client.get("/admin-panel/profile.html").content.decode()
        self.assertIn("readonly", page[page.index('x-model="email"'):page.index('x-model="email"') + 200])
        self.assertNotIn("Change sign-in Gmail", page)                          # only the owner sees the change box


class OwnerChangesHerGmailTests(Base):
    def setUp(self):
        self.client.force_login(self.owner)

    def start(self, email):
        return self.client.post(reverse("arabela_admin:owner_gmail_change"), data=json.dumps({"email": email}), content_type="application/json")

    def test_only_the_owner_can_start_it(self):
        client = Client()
        client.force_login(self.staff)
        response = client.post(reverse("arabela_admin:owner_gmail_change"), data=json.dumps({"email": "new@gmail.com"}), content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_it_refuses_a_bad_a_taken_or_customer_gmail_and_changes_nothing(self):
        User.objects.create_user(username="cust3", password="x", email="shopper3@gmail.com")
        for email, fragment in (("nope", "valid Gmail"), ("maria.staff@gmail.com", "another admin account"), ("shopper3@gmail.com", "customer account"),
                                ("old.owner@example.com", "already your sign-in")):
            with self.subTest(email=email):
                response = self.start(email)
                self.assertEqual(response.status_code, 400)
                self.assertIn(fragment, response.json()["error"])
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.email, "old.owner@example.com")

    def test_the_new_gmail_is_saved_only_after_she_signs_in_to_it_with_google(self):
        response = self.start("New.Owner@gmail.com")
        self.assertEqual(response.status_code, 200, response.content)
        url = response.json()["url"]
        self.assertIn("/accounts/google/login/", url)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.email, "old.owner@example.com")                                  # nothing saved yet
        from urllib.parse import parse_qs, urlparse
        next_url = parse_qs(urlparse(url).query)["next"][0]
        stop = run_adapter(google_answer("new.owner@gmail.com", next_url=next_url))                    # Google confirms she owns the new Gmail
        client = Client()
        self.finish(client, stop)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.email, "new.owner@gmail.com")
        self.assertEqual(client.get(reverse("arabela_admin:staff_management")).status_code, 200)
        self.assertEqual(run_adapter(google_answer("old.owner@example.com")).status_code, 403)         # the old Gmail no longer opens the owner account

    def test_signing_in_with_a_different_google_account_changes_nothing(self):
        from urllib.parse import parse_qs, urlparse
        next_url = parse_qs(urlparse(self.start("new.owner@gmail.com").json()["url"]).query)["next"][0]
        response = run_adapter(google_answer("someone.else@gmail.com", next_url=next_url))
        self.assertEqual(response.status_code, 400)
        self.assertIn("Nothing was changed", response.content.decode())
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.email, "old.owner@example.com")

    def test_the_change_request_expires(self):
        from urllib.parse import parse_qs, urlparse
        next_url = parse_qs(urlparse(self.start("new.owner@gmail.com").json()["url"]).query)["next"][0]
        with mock.patch.object(admin_google, "CHANGE_MAX_AGE", -1):
            response = run_adapter(google_answer("new.owner@gmail.com", next_url=next_url))
        self.assertEqual(response.status_code, 400)
        self.assertIn("took too long", response.content.decode())

    def test_the_audit_log_records_the_change(self):
        from accounts.models import AuditLog
        from urllib.parse import parse_qs, urlparse
        next_url = parse_qs(urlparse(self.start("new.owner@gmail.com").json()["url"]).query)["next"][0]
        self.finish(Client(), run_adapter(google_answer("new.owner@gmail.com", next_url=next_url)))
        self.assertTrue(AuditLog.objects.filter(action="account.gmail", sensitive=True).exists())
        self.assertTrue(AuditLog.objects.filter(action="account.gmail_start").exists())

    def test_only_the_owner_sees_the_change_box_on_the_profile_page(self):
        page = self.client.get("/admin-panel/profile.html").content.decode()
        self.assertIn("Change sign-in Gmail", page)
        self.assertIn("Verify with Google", page)
