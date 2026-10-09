"""A request the CSRF check refuses (an old copy of a page, a browser that dropped its cookie) gets a clear page or a
clear JSON answer and a log line -- never Django's bare 403 -- and a normal sign-in still passes the check."""
import re

from django.test import Client, TestCase
from django.urls import reverse

from arabela_system import csrf

LOGIN = "/admin-panel/admin-login/"


class CsrfFailureTests(TestCase):
    def setUp(self):
        self.strict = Client(enforce_csrf_checks=True)   # the way a real browser is treated

    def post_without_cookie(self, path=LOGIN, **extra):
        return self.strict.post(path, {"username": "nobody", "password": "x"}, **extra)

    def test_a_sign_in_sent_without_the_pages_cookie_gets_a_page_that_says_what_to_do(self):
        response = self.post_without_cookie()
        self.assertEqual(response.status_code, 403)
        html = response.content.decode()
        self.assertIn("Please try again", html)
        self.assertIn(f'href="{reverse("arabela_admin:admin_login")}"', html)
        self.assertIn("Open the sign-in page", html)
        self.assertNotIn("CSRF verification failed", html)         # not Django's bare page
        self.assertIn("Nothing was changed", html)

    def test_outside_the_admin_panel_the_button_goes_to_the_home_page(self):
        response = self.post_without_cookie("/cart/save/")
        self.assertEqual(response.status_code, 403)
        self.assertIn('href="/"', response.content.decode())
        self.assertIn("Back to the website", response.content.decode())

    def test_a_page_s_own_button_gets_json_it_can_show(self):
        for extra in ({"HTTP_X_CSRFTOKEN": "stale"}, {"HTTP_ACCEPT": "application/json"}, {"HTTP_SEC_FETCH_MODE": "cors"}):
            with self.subTest(extra=extra):
                response = self.post_without_cookie("/admin-panel/api/staff/", **extra)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(), {"error": csrf.JSON_MESSAGE})

    def test_a_form_that_navigated_gets_the_page_not_json(self):
        response = self.post_without_cookie(HTTP_SEC_FETCH_MODE="navigate")
        self.assertIn("text/html", response["Content-Type"])

    def test_the_reason_is_logged_so_the_next_time_is_not_a_guess(self):
        with self.assertLogs("arabela.csrf", level="WARNING") as logged:
            self.post_without_cookie(HTTP_USER_AGENT="Mozilla/5.0 (Linux; Android 14)")
        line = logged.output[0]
        self.assertIn("POST /admin-panel/admin-login/", line)
        self.assertIn("CSRF cookie not set", line)             # the reason Django gave
        self.assertIn("csrf cookie sent: False", line)
        self.assertIn("Android", line)
        # a refusal because of the address the form came from says so, and names that address
        with self.assertLogs("arabela.csrf", level="WARNING") as logged:
            self.post_without_cookie(HTTP_ORIGIN="https://some-other-site.example")
        self.assertIn("Origin checking failed", logged.output[0])
        self.assertIn("origin: https://some-other-site.example", logged.output[0])

    def test_the_log_never_contains_the_secret_values(self):
        with self.assertLogs("arabela.csrf", level="WARNING") as logged:
            self.strict.cookies["csrftoken"] = "A" * 32
            self.strict.post(LOGIN, {"username": "nobody", "password": "hunter2-secret", "csrfmiddlewaretoken": "B" * 64})
        text = " ".join(logged.output)
        self.assertNotIn("hunter2-secret", text)
        self.assertNotIn("A" * 32, text)
        self.assertNotIn("B" * 64, text)


class NormalSignInStillWorksTests(TestCase):
    def test_a_sign_in_page_loaded_normally_passes_the_check_with_it_switched_on(self):
        browser = Client(enforce_csrf_checks=True)
        page = browser.get(LOGIN)
        self.assertEqual(page.status_code, 200)
        self.assertIn("csrftoken", browser.cookies)                       # the page sets the cookie by itself
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
        answer = browser.post(LOGIN, {"csrfmiddlewaretoken": token, "username": "nobody", "password": "wrong"})
        self.assertEqual(answer.status_code, 200)                         # the normal "invalid username or password" page, not a 403
        self.assertIn("Invalid username or password", answer.content.decode())

    def test_the_sign_in_page_is_never_kept_by_the_browser(self):
        control = Client().get(LOGIN)["Cache-Control"]
        for part in ("no-cache", "no-store", "max-age=0"):
            self.assertIn(part, control)
