"""The admin panel signs in with Google only, and the old username + password features are gone for good.

Removed: the hidden password sign-in page, Forgot password / reset-password, the recovery-email code flow, the Account Settings page (change
username, change password, recovery email), the "Account settings" menu item on every admin page, and the OwnerRecovery table. Checked here that
every old address answers "not found" for everybody, that no page links to one, that the code and the table are really gone, and that what the
Account Settings page used to show everyone (when this account last signed in before this session) now lives on Edit Profile for owner and staff."""
from datetime import timedelta
from importlib import import_module, util

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.template.defaultfilters import date as date_filter
from django.test import Client, TestCase
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from accounts import admin_google
from accounts.models import UserProfile
from arabela_system.middleware import admin_cookie_name

User = get_user_model()

REMOVED_ADDRESSES = (
    "/admin-panel/admin-login/password/",
    "/admin-panel/forgot-password/",
    "/admin-panel/reset-password/Mg/abc-123/",
    "/admin-panel/account-settings.html",
    "/admin-panel/api/account/change-password/",
    "/admin-panel/api/account/change-username/",
    "/admin-panel/api/recovery-email/send-code/",
    "/admin-panel/api/recovery-email/verify/",
)
REMOVED_URL_NAMES = ("admin_password_login", "forgot_password", "reset_password", "recovery_send_code", "recovery_verify_code",
                     "change_own_password", "change_own_username")
WORDS_THAT_MEAN_A_PASSWORD_FEATURE = ("account-settings", "Account settings", "forgot-password", "Forgot password", "change-password", "change-username",
                                      "recovery-email", "Recovery email")
ADMIN_PAGES = ("dashboard", "calendar", "gown_catalog", "active_reservations", "pending_approval", "rental_history", "reservation_records",
               "new_booking", "payment_verification", "security_deposits", "clients", "categories", "staff_management", "audit_log")


def make_owner():
    owner = User.objects.create_user("npf_owner", password="x", is_staff=True, is_superuser=True, first_name="Ana", last_name="Cruz")
    UserProfile.objects.update_or_create(user=owner, defaults={"role": UserProfile.Role.OWNER})
    return owner


def make_staff():
    staff = User.objects.create_user("npf_staff", password="x", is_staff=True, first_name="Bo", last_name="Reyes")
    UserProfile.objects.update_or_create(user=staff, defaults={"role": UserProfile.Role.STAFF})
    return staff


def google_sign_in(user, client=None):
    """The hand-over every admin sign-in ends with: the signed link made for an allowed Gmail, opened once, signs the account in."""
    client = client or Client()
    response = client.get(admin_google.finish_path() + "?t=" + admin_google.make_signin_token(user))
    assert response.status_code == 302, response.status_code
    return client


class OldAddressesAreGoneTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_owner()
        cls.staff = make_staff()

    def browsers(self):
        owner, staff = Client(), Client()
        owner.force_login(self.owner)
        staff.force_login(self.staff)
        return {"signed out": Client(), "staff": staff, "owner": owner}

    def test_every_old_address_answers_not_found_for_everybody(self):
        for who, browser in self.browsers().items():
            for path in REMOVED_ADDRESSES:
                with self.subTest(who=who, path=path, method="GET"):
                    self.assertEqual(browser.get(path).status_code, 404)
                with self.subTest(who=who, path=path, method="POST"):
                    self.assertEqual(browser.post(path, {"username": "x", "password": "y", "current_password": "y", "new_password": "z"}).status_code, 404)

    def test_no_password_can_open_the_admin_panel_any_more(self):
        client = Client()
        client.post("/admin-panel/admin-login/password/", {"username": self.owner.username, "password": "x"})
        client.post("/admin-panel/admin-login/", {"username": self.owner.username, "password": "x"})          # the Google page ignores a typed password
        self.assertEqual(client.get(reverse("arabela_admin:dashboard")).status_code, 302)
        self.assertNotIn(admin_cookie_name(), client.cookies)

    def test_the_old_address_names_no_longer_exist(self):
        for name in REMOVED_URL_NAMES:
            with self.subTest(name=name):
                with self.assertRaises(NoReverseMatch):
                    reverse(f"arabela_admin:{name}")
                with self.assertRaises(NoReverseMatch):
                    reverse(f"arabela_admin:{name}", args=["a", "b"])

    def test_the_page_address_only_serves_edit_profile(self):
        self.assertEqual(self.browsers()["owner"].get(reverse("arabela_admin:page", args=["profile"])).status_code, 200)
        for slug in ("account-settings", "settings", "forgot-password", "calendar"):
            with self.subTest(slug=slug):
                self.assertEqual(self.browsers()["owner"].get(reverse("arabela_admin:page", args=[slug])).status_code, 404)

    def test_no_admin_page_links_to_a_password_feature(self):
        owner = self.browsers()["owner"]
        for name in ADMIN_PAGES + ("page",):
            url = reverse("arabela_admin:page", args=["profile"]) if name == "page" else reverse(f"arabela_admin:{name}")
            response = owner.get(url)
            self.assertEqual(response.status_code, 200, name)
            html = response.content.decode()
            for word in WORDS_THAT_MEAN_A_PASSWORD_FEATURE:
                with self.subTest(page=name, word=word):
                    self.assertNotIn(word, html)

    def test_the_sign_in_page_has_no_way_to_a_password(self):
        html = Client().get(reverse("arabela_admin:admin_login")).content.decode()
        for word in WORDS_THAT_MEAN_A_PASSWORD_FEATURE + ('type="password"', 'name="username"'):
            self.assertNotIn(word, html)


class OldCodeAndTableAreGoneTests(TestCase):
    def test_the_modules_are_gone(self):
        for module in ("accounts.owner_recovery", "arabela_admin.recovery_views", "accounts.login_throttle"):
            with self.subTest(module=module):
                self.assertIsNone(util.find_spec(module))

    def test_the_templates_are_gone(self):
        from pathlib import Path
        folder = Path(settings.BASE_DIR) / "templates" / "arabela_admin"
        for name in ("account-settings.html", "admin-login-password.html", "forgot-password.html", "reset-password.html"):
            with self.subTest(template=name):
                self.assertFalse((folder / name).exists())

    def test_the_recovery_table_is_gone(self):
        with self.assertRaises(LookupError):
            apps.get_model("accounts", "OwnerRecovery")

    def test_the_counters_other_features_use_are_still_there(self):
        self.assertIsNotNone(apps.get_model("accounts", "LoginThrottle"))              # accounts/rate_limit.py keeps its counters in this table


class PreviousSignInTests(TestCase):
    """The sign-in BEFORE the current one is read just ahead of login() (Django overwrites last_login) and shown on Edit Profile, to the owner and to
    staff alike (it used to be on the Account Settings page too), so a sign-in nobody recognises can be noticed."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_owner()
        cls.staff = make_staff()
        cls.profile_page = reverse("arabela_admin:page", args=["profile"])

    def shown(self, moment):
        return date_filter(timezone.localtime(moment), "M j, Y, g:i A")

    def test_a_first_ever_sign_in_has_nothing_before_it(self):
        client = google_sign_in(self.owner)
        admin_session = import_module(settings.SESSION_ENGINE).SessionStore(client.cookies[admin_cookie_name()].value)
        self.assertEqual(admin_session["admin_previous_login"], "")
        self.assertContains(client.get(self.profile_page), "Not recorded for this session")

    def test_the_next_sign_in_shows_when_the_one_before_it_was(self):
        before = timezone.now() - timedelta(days=2, hours=3)
        User.objects.filter(pk=self.owner.pk).update(last_login=before)
        client = google_sign_in(User.objects.get(pk=self.owner.pk))
        html = client.get(self.profile_page).content.decode()
        self.assertIn(self.shown(before), html)
        self.assertNotIn("Not recorded for this session", html)
        self.owner.refresh_from_db()
        self.assertGreater(self.owner.last_login, before)            # Django did overwrite last_login with "now"

    def test_signing_in_again_moves_the_previous_time_forward(self):
        first = google_sign_in(self.owner)
        self.owner.refresh_from_db()
        first_login = self.owner.last_login
        second = google_sign_in(self.owner)
        self.assertContains(second.get(self.profile_page), self.shown(first_login))
        first.logout()

    def test_a_session_from_before_this_was_recorded_still_works(self):
        self.client.force_login(self.owner)           # no sign-in through the hand-over, so nothing was recorded
        response = self.client.get(self.profile_page)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Not recorded for this session")

    def test_staff_see_their_own_previous_sign_in_on_edit_profile(self):
        before = timezone.now() - timedelta(days=1)
        User.objects.filter(pk=self.staff.pk).update(last_login=before)
        client = google_sign_in(User.objects.get(pk=self.staff.pk))
        self.assertContains(client.get(self.profile_page), self.shown(before))


class LoginAndSecurityCardTests(TestCase):
    """Edit Profile's "Login & Security" card: how this account signs in and the previous sign-in. No username, no password buttons."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_owner()
        cls.staff = make_staff()
        cls.profile_page = reverse("arabela_admin:page", args=["profile"])

    def card_of(self, user):
        self.client.force_login(user)
        html = self.client.get(self.profile_page).content.decode()
        start = html.index('id="login-security"')
        return html[start:html.index("</div>\n              </div>", start)]

    def test_the_owner_and_staff_both_see_the_card_with_the_google_sentence(self):
        for user in (self.owner, self.staff):
            with self.subTest(user=user.username):
                card = self.card_of(user)
                self.assertIn("Login &amp; Security", card)
                self.assertIn("You sign in to this admin panel with your Google account. There is no username or password.", card)
                self.assertIn("Previous sign-in", card)

    def test_the_card_has_no_username_and_no_password_buttons(self):
        for user in (self.owner, self.staff):
            card = self.card_of(user)
            for gone in ("Username", "Change username", "Change password", "change-username", "change-password", "<a "):
                with self.subTest(user=user.username, gone=gone):
                    self.assertNotIn(gone, card)

    def test_no_template_code_leaks_into_the_page(self):
        for user in (self.owner, self.staff):
            self.client.force_login(user)
            html = self.client.get(self.profile_page).content.decode()
            for leak in ("{%", "{{", "{#"):
                with self.subTest(user=user.username, leak=leak):
                    self.assertNotIn(leak, html)
