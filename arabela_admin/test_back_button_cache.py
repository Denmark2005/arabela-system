"""After Sign out, the Back button must not bring the admin panel back.

Every admin page already sends a signed-out visitor to the sign-in page -- but only when the SERVER is asked. The browser kept its own copy
of the last page (no cache header was sent) and put it back on Back without asking, so the panel looked signed in until the first click.
Two things close that: every admin-area response says "keep no copy" (`AdminNoStoreMiddleware`), and a page that still comes back from the
browser's history proves its sign-in first (notifications-live.js). These tests hold both."""
import pathlib

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from arabela_system.middleware import AdminNoStoreMiddleware, is_admin_area

User = get_user_model()

PAGES = ["dashboard", "gown_catalog", "active_reservations", "admin_notifications_feed"]


def keeps_nothing(response):
    cache_control = response.get("Cache-Control", "")
    return "no-store" in cache_control and "no-cache" in cache_control and "must-revalidate" in cache_control


class MiddlewareOnlyMarksTheAdminArea(SimpleTestCase):
    def run_through(self, path):
        middleware = AdminNoStoreMiddleware(lambda request: HttpResponse("page"))
        return middleware(RequestFactory().get(path))

    def test_every_admin_path_is_marked(self):
        for path in ("/admin-panel", "/admin-panel/", "/admin-panel/gown-catalog/", "/admin-panel/api/notifications/", "/admin-panel/admin-login/", "/admin/", "/admin/login/"):
            with self.subTest(path=path):
                self.assertTrue(is_admin_area(path))
                self.assertTrue(keeps_nothing(self.run_through(path)), self.run_through(path).headers)

    def test_the_customer_site_and_static_files_are_left_alone(self):
        for path in ("/", "/products/", "/reservations/", "/static/css/style.css", "/media/x.jpg", "/admin-panelx/", "/administrator/"):
            with self.subTest(path=path):
                self.assertFalse(is_admin_area(path))
                self.assertNotIn("no-store", self.run_through(path).get("Cache-Control", ""))

    def test_the_rule_is_switched_on_and_sits_after_the_admin_session(self):
        names = list(settings.MIDDLEWARE)
        self.assertIn("arabela_system.middleware.AdminNoStoreMiddleware", names)
        self.assertGreater(names.index("arabela_system.middleware.AdminNoStoreMiddleware"), names.index("arabela_system.middleware.SplitSessionMiddleware"))


class BackAfterSignOut(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("bb_admin", "bb_admin@example.com", "Pass-12345!", is_staff=True)

    def test_every_admin_page_keeps_no_copy(self):
        self.client.force_login(self.admin)
        for name in PAGES:
            with self.subTest(page=name):
                response = self.client.get(reverse(f"arabela_admin:{name}"))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(keeps_nothing(response), response.headers)

    def test_the_sign_in_page_and_the_profile_page_keep_no_copy_either(self):
        self.assertTrue(keeps_nothing(self.client.get(reverse("arabela_admin:admin_login"))))
        self.client.force_login(self.admin)
        self.assertTrue(keeps_nothing(self.client.get(reverse("arabela_admin:page", kwargs={"page": "profile"}))))

    def test_signing_out_then_asking_for_a_page_goes_to_the_sign_in_page(self):
        """What the browser now does on Back: it asks the server, and the server sends the visitor to sign in."""
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("arabela_admin:dashboard")).status_code, 200)
        out = self.client.post(reverse("arabela_admin:admin_logout"))
        self.assertEqual(out.status_code, 302)
        self.assertTrue(keeps_nothing(out))
        for name in ("dashboard", "gown_catalog", "active_reservations"):
            with self.subTest(page=name):
                back = self.client.get(reverse(f"arabela_admin:{name}"))
                self.assertEqual(back.status_code, 302)
                self.assertEqual(back["Location"], reverse("arabela_admin:admin_login"))
                self.assertTrue(keeps_nothing(back))

    def test_after_signing_out_the_live_alerts_feed_answers_unauthorised(self):
        """notifications-live.js treats 401/403 from this feed as 'the sign-in ended' -- the page that came back from history relies on it."""
        self.client.force_login(self.admin)
        self.client.post(reverse("arabela_admin:admin_logout"))
        response = self.client.get(reverse("arabela_admin:admin_notifications_feed"), HTTP_ACCEPT="application/json")
        self.assertIn(response.status_code, (401, 403))

    def test_the_customer_home_page_is_not_changed(self):
        self.assertNotIn("no-store", self.client.get("/").get("Cache-Control", ""))


class ThePageThatComesBackProvesItsSignInFirst(SimpleTestCase):
    """The browser half, as a guard on the script text (the real Back-button run is the browser test)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.js = (pathlib.Path(settings.BASE_DIR) / "static" / "arabela_admin" / "notifications-live.js").read_text(encoding="utf-8")

    def test_a_page_restored_from_the_back_forward_cache_is_checked(self):
        self.assertIn("addEventListener('pageshow'", self.js)
        self.assertIn("e.persisted", self.js)

    def test_a_page_reloaded_by_the_back_button_is_checked_too(self):
        self.assertIn("'back_forward'", self.js)
        self.assertIn("if (cameBackFromHistory()) { verifySignIn(); }", self.js)

    def test_it_is_covered_until_the_server_answers_and_never_forever(self):
        self.assertIn("style.visibility = 'hidden'", self.js)
        self.assertIn("COVER_MAX_MS", self.js)
        self.assertIn("if (restored) { restored = false; uncoverPage(); }", self.js)

    def test_a_signed_out_answer_sends_the_browser_to_the_sign_in_page(self):
        self.assertIn("if (restored) { window.location.replace(cfg.login); return; }", self.js)
        self.assertIn("res.status === 401 || res.status === 403", self.js)
