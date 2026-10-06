"""Emails to customers about their own booking (accounts/customer_emails.py, accounts/email_backends.py).

The hook tests drive the REAL views and services (checkout, approve, reject, mark returned, the reminder sweep, the
flag actions...) and count the emails, so a reworded timeline label or a broken hook is caught here. Django's test
runner swaps in the in-memory mail backend, so nothing is ever really sent.
"""
import base64
import json
import os
import re
import smtplib
import threading
import time
from datetime import timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import customer_emails as ce
from accounts.email_backends import AppsScriptRelayBackend, EmailRelayError
from accounts.models import CustomerMessage, UserProfile
from gowns.models import Gown, SiteSettings
from reservations import reminders
from reservations.models import Reservation, ReservationItem, ReservationStatusEvent

User = get_user_model()

BASE = "https://arabela.example.org"
SHOP = {"phone": "09635215485", "facebook_url": "https://www.facebook.com/arabela", "address": "3rd Flr Park Place, Antipolo"}
ON = dict(
    CUSTOMER_EMAILS_ENABLED=True, CUSTOMER_EMAILS_BACKGROUND=False,
    SITE_BASE_URL=BASE, DEFAULT_FROM_EMAIL="Arabela <shop@gmail.com>",
)
_TINY_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


def _built(kind, **changes):
    data = ce.sample_data(kind)
    data.update(changes)
    return ce.build_email(kind, data, base_url=BASE, shop=SHOP)


# =====================================================================================================================
# How the emails are built (no database)
# =====================================================================================================================
@override_settings(**ON)
class EmailRenderingTests(SimpleTestCase):

    def test_every_kind_renders_cleanly(self):
        for kind in ce.KIND_LABELS:
            with self.subTest(kind=kind):
                built = _built(kind)
                for text in (built.subject, built.text, built.html):
                    self.assertNotIn("{{", text)
                    self.assertNotIn("{%", text)
                self.assertTrue(built.subject.isascii() and "\n" not in built.subject, built.subject)
                self.assertIn('alt="Arabela"', built.html)
                self.assertIn("This is an automatic message", built.html)
                self.assertIn("Message us on Facebook", built.html)
                self.assertIn(SHOP["phone"], built.html)
                self.assertNotRegex(built.text, r"</?(p|td|table|div|span|a)\b")      # the text version is plain
                links = re.findall(r'href="([^"]*)"', built.html)
                self.assertTrue(links)
                for link in links:
                    self.assertTrue(link.startswith("https://"), f"{kind}: {link}")

    def test_the_button_goes_to_the_right_page(self):
        self.assertIn(f'href="{BASE}/reservations/"', _built(ce.APPROVED).html)
        self.assertIn(f'href="{BASE}/messages/"', _built(ce.ACCOUNT_FLAGGED).html)
        self.assertIn(f"{BASE}/reservations/", _built(ce.APPROVED).text)

    def test_the_booking_summary_shows_code_gowns_and_money(self):
        for kind in (ce.RECEIVED, ce.APPROVED):
            built = _built(kind)
            for needle in ("RSV-2026-0054", "Wedding Gown 17", "Evening Gown 4", "Oct 13", "₱4,000", "₱26,500"):
                self.assertIn(needle, built.html, kind)
                self.assertIn(needle, built.text, kind)
        self.assertNotIn("Total", _built(ce.REJECTED).html)            # no money on a rejection
        self.assertNotIn("Booking code", _built(ce.REMINDER).html)     # reminders are just their message

    def test_the_subject_names_the_booking(self):
        self.assertEqual(_built(ce.RECEIVED).subject, "We received your reservation RSV-2026-0054")
        self.assertEqual(_built(ce.APPROVED).subject, "Your reservation RSV-2026-0054 is approved")
        self.assertEqual(_built(ce.REJECTED).subject, "Update on your reservation RSV-2026-0054")

    def test_a_rejection_shows_the_reason_or_a_default(self):
        self.assertIn("match the GCash payment to your receipt", _built(ce.REJECTED).html)
        self.assertIn("Please contact the shop for details.", _built(ce.REJECTED, reason="").html)

    def test_customer_typed_text_is_escaped_in_the_html(self):
        built = _built(
            ce.REJECTED, first_name="<script>alert(1)</script>", reason='<b>bold</b> & "quoted"',
            items=[("<i>Gown</i>", "x")],
        )
        for raw in ("<script>", "<b>bold</b>", "<i>Gown</i>"):
            self.assertNotIn(raw, built.html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", built.html)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt; &amp; &quot;quoted&quot;", built.html)
        self.assertIn('<b>bold</b> & "quoted"', built.text)            # plain text has nothing to escape

    def test_a_booking_code_never_wraps_across_lines(self):
        html = _built(ce.REMINDER).html
        self.assertIn('<span style="white-space:nowrap;">RSV-2026-0054</span>', html)
        self.assertNotIn("<span", _built(ce.REMINDER).text)

    def test_line_breaks_in_a_message_are_kept(self):
        html = _built(ce.REMINDER, text="First line\nSecond line").html
        self.assertIn("First line<br>Second line", html)

    def test_the_greeting_falls_back_when_there_is_no_name(self):
        self.assertIn("Hi there,", _built(ce.APPROVED, first_name="").html)
        self.assertIn("Hi Rainer,", _built(ce.APPROVED).html)

    def test_the_footer_adapts_to_missing_shop_details(self):
        built = ce.build_email(ce.APPROVED, ce.sample_data(ce.APPROVED), base_url=BASE, shop={"phone": "", "facebook_url": "", "address": ""})
        self.assertIn("This is an automatic message", built.html)
        self.assertNotIn("Need help?", built.html)
        self.assertNotIn("None", built.html)
        phone_only = ce.build_email(ce.APPROVED, ce.sample_data(ce.APPROVED), base_url=BASE, shop={"phone": "0917", "facebook_url": "", "address": ""})
        self.assertIn("Need help? call 0917.", phone_only.html)

    def test_the_logo_is_attached_inside_the_email(self):
        inline = ce.build_email(ce.APPROVED, ce.sample_data(ce.APPROVED), base_url=BASE, shop=SHOP, inline_logo=True)
        self.assertTrue(inline.inline_logo)
        self.assertIn('src="cid:arabela-logo"', inline.html)
        hosted = _built(ce.APPROVED)                                    # the admin preview uses the website's copy
        self.assertFalse(hosted.inline_logo)
        self.assertIn(f'src="{BASE}/static/images/email/arabela-logo-white.png"', hosted.html)

    def test_a_missing_logo_file_falls_back_to_the_hosted_copy(self):
        with patch.dict(ce._logo_cache, {"bytes": None}):
            built = ce.build_email(ce.APPROVED, ce.sample_data(ce.APPROVED), base_url=BASE, shop=SHOP, inline_logo=True)
        self.assertFalse(built.inline_logo)
        self.assertNotIn("cid:", built.html)

    def test_the_real_message_is_multipart_with_the_logo_inline(self):
        message = ce._make_message(ce.build_email(ce.APPROVED, ce.sample_data(ce.APPROVED), base_url=BASE, shop=SHOP, inline_logo=True), "rainer@gmail.com")
        raw = message.message()                                         # what is actually put on the wire
        self.assertEqual(raw.get_content_type(), "multipart/alternative")
        types = [part.get_content_type() for part in raw.walk()]
        for expected in ("text/plain", "multipart/related", "text/html", "image/png"):
            self.assertIn(expected, types)
        related = next(part for part in raw.walk() if part.get_content_type() == "multipart/related")
        self.assertEqual([part.get_content_type() for part in related.iter_parts()], ["text/html", "image/png"])
        image = next(part for part in raw.walk() if part.get_content_type() == "image/png")
        self.assertEqual(image["Content-ID"], "<arabela-logo>")
        self.assertEqual(image.get_content_disposition(), "inline")
        self.assertTrue(image.get_payload(decode=True).startswith(b"\x89PNG"))
        self.assertEqual(message.from_email, "Arabela <shop@gmail.com>")
        self.assertEqual(message.to, ["rainer@gmail.com"])
        self.assertEqual(message.extra_headers["Auto-Submitted"], "auto-generated")

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            ce.build_email("nonsense", {}, base_url=BASE, shop=SHOP)


# =====================================================================================================================
# Who may be emailed, and how problems are explained (no database)
# =====================================================================================================================
class EmailHelpersTests(SimpleTestCase):

    def _user(self, email, **flags):
        return User(username="x", email=email, **flags)

    def test_only_real_customer_addresses_are_used(self):
        self.assertEqual(ce._deliverable_address(self._user("rainer@gmail.com")), "rainer@gmail.com")
        self.assertEqual(ce._deliverable_address(self._user("  rainer@gmail.com ")), "rainer@gmail.com")
        for email in ("", "no-at-sign", "demo@example.invalid", "a@example.com", "a@mail.test", "a@shop.example", "a@x.localhost"):
            self.assertEqual(ce._deliverable_address(self._user(email)), "", email)
        self.assertEqual(ce._deliverable_address(self._user("owner@gmail.com", is_staff=True)), "")
        self.assertEqual(ce._deliverable_address(self._user("owner@gmail.com", is_superuser=True)), "")
        self.assertEqual(ce._deliverable_address(None), "")

    def test_addresses_are_masked_in_the_logs(self):
        self.assertEqual(ce._mask("rainer@gmail.com"), "r***@gmail.com")

    def test_send_problems_are_explained(self):
        self.assertIn("App Password", ce.explain_send_error(smtplib.SMTPAuthenticationError(535, b"bad")))
        self.assertIn("FREE plan", ce.explain_send_error(OSError("Network is unreachable")))
        self.assertIn("FREE plan", ce.explain_send_error(TimeoutError("timed out")))
        self.assertIn("FREE plan", ce.explain_send_error(smtplib.SMTPServerDisconnected("gone")))
        self.assertIn("daily sending limit", ce.explain_send_error(smtplib.SMTPDataError(550, b"quota")))
        self.assertEqual(ce.explain_send_error(EmailRelayError("The relay said no.")), "The relay said no.")
        self.assertEqual(ce.explain_send_error(KeyError("k")), "KeyError: 'k'")

    @override_settings(SITE_BASE_URL=BASE, DEFAULT_FROM_EMAIL="Arabela <shop@gmail.com>")
    def test_delivery_status_describes_each_way_of_sending(self):
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
                               EMAIL_HOST_USER="shop@gmail.com", EMAIL_HOST_PASSWORD="app-password"):
            status = ce.delivery_status()
            self.assertEqual((status["method"], status["configured"]), ("Gmail over SMTP", True))
            self.assertTrue(any("FREE plan" in w for w in status["warnings"]))
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend", EMAIL_HOST_USER="", EMAIL_HOST_PASSWORD=""):
            self.assertFalse(ce.delivery_status()["configured"])
        with override_settings(EMAIL_BACKEND="accounts.email_backends.AppsScriptRelayBackend",
                               EMAIL_RELAY_URL="https://script.google.com/x/exec", EMAIL_RELAY_SECRET="s"):
            status = ce.delivery_status()
            self.assertEqual((status["method"], status["configured"]), ("Google relay (over HTTPS)", True))
            self.assertEqual(status["warnings"], [])
        with override_settings(EMAIL_BACKEND="accounts.email_backends.AppsScriptRelayBackend", EMAIL_RELAY_URL="", EMAIL_RELAY_SECRET=""):
            self.assertFalse(ce.delivery_status()["configured"])
        self.assertIn("Test mode", ce.delivery_status()["method"])      # the in-memory backend of the test runner

    def test_delivery_status_warns_about_wrong_links(self):
        with override_settings(SITE_BASE_URL="http://127.0.0.1:8000"):
            self.assertTrue(any("local/test address" in w for w in ce.delivery_status()["warnings"]))
        with override_settings(SITE_BASE_URL="http://real-shop.example"):
            self.assertTrue(any("http://" in w for w in ce.delivery_status()["warnings"]))
        with override_settings(SITE_BASE_URL="https://arabela-gown-rental.onrender.com"):
            self.assertEqual(ce.delivery_status()["warnings"], [])


# =====================================================================================================================
# The free Google relay backend, against a local stand-in for the Apps Script web app (no database)
# =====================================================================================================================
class _FakeAppsScript:
    """Answers like the real web app does: it receives the POST, does the work, then answers with a 302 to a
    URL where the JSON result is fetched with a GET."""

    def __init__(self, mode="ok", delay=0):
        self.mode, self.delay, self.received = mode, delay, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, body, content_type="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                outer.received.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))))
                if outer.delay:
                    time.sleep(outer.delay)
                if outer.mode == "html":                       # e.g. a Google sign-in page
                    return self._reply(b"<html><body>Sign in</body></html>", "text/html")
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{outer.port}/answer")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                if outer.mode == "wrong_secret":
                    return self._reply({"ok": False, "error": "secret does not match"})
                if outer.mode == "quota":
                    return self._reply({"ok": False, "error": "Service invoked too many times for one day: email."})
                self._reply({"ok": True, "remaining": 97})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/exec"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@override_settings(DEFAULT_FROM_EMAIL="Arabela <shop@gmail.com>", SITE_BASE_URL=BASE, EMAIL_RELAY_SECRET="s3cret-value", EMAIL_RELAY_TIMEOUT=1)
class AppsScriptRelayBackendTests(SimpleTestCase):

    def setUp(self):
        patcher = patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _message(self):
        built = ce.build_email(ce.APPROVED, ce.sample_data(ce.APPROVED), base_url=BASE, shop=SHOP, inline_logo=True)
        return ce._make_message(built, "rainer@gmail.com")

    def _server(self, **kwargs):
        server = _FakeAppsScript(**kwargs)
        self.addCleanup(server.close)
        return server

    def test_a_message_goes_through_apps_scripts_redirect(self):
        server = self._server()
        with override_settings(EMAIL_RELAY_URL=server.url):
            sent = AppsScriptRelayBackend().send_messages([self._message()])
        self.assertEqual(sent, 1)
        payload = server.received[0]
        self.assertEqual(payload["secret"], "s3cret-value")
        self.assertEqual(payload["to"], "rainer@gmail.com")
        self.assertEqual(payload["subject"], "Your reservation RSV-2026-0054 is approved")
        self.assertEqual(payload["name"], "Arabela")
        self.assertIn("Your reservation is approved", payload["text"])
        self.assertIn("<html", payload["html"])
        self.assertTrue(base64.b64decode(payload["inline"]["arabela-logo"]).startswith(b"\x89PNG"))

    def test_a_wrong_secret_is_explained_without_revealing_it(self):
        server = self._server(mode="wrong_secret")
        with override_settings(EMAIL_RELAY_URL=server.url):
            with self.assertRaises(EmailRelayError) as caught:
                AppsScriptRelayBackend().send_messages([self._message()])
        self.assertIn("does not match EMAIL_RELAY_SECRET", str(caught.exception))
        self.assertNotIn("s3cret-value", str(caught.exception))

    def test_a_refusal_such_as_the_daily_quota_is_passed_on(self):
        server = self._server(mode="quota")
        with override_settings(EMAIL_RELAY_URL=server.url):
            with self.assertRaises(EmailRelayError) as caught:
                AppsScriptRelayBackend().send_messages([self._message()])
        self.assertIn("too many times for one day", str(caught.exception))

    def test_an_answer_that_is_not_json_says_how_to_fix_the_deployment(self):
        server = self._server(mode="html")
        with override_settings(EMAIL_RELAY_URL=server.url):
            with self.assertRaises(EmailRelayError) as caught:
                AppsScriptRelayBackend().send_messages([self._message()])
        self.assertIn("Who has access: Anyone", str(caught.exception))

    def test_a_slow_relay_times_out_with_a_clear_message(self):
        server = self._server(delay=3)
        with override_settings(EMAIL_RELAY_URL=server.url):
            with self.assertRaises(EmailRelayError) as caught:
                AppsScriptRelayBackend().send_messages([self._message()])
        self.assertIn("took too long", str(caught.exception))

    def test_a_relay_that_cannot_be_reached_is_explained(self):
        with override_settings(EMAIL_RELAY_URL="http://127.0.0.1:9/exec"):
            with self.assertRaises(EmailRelayError) as caught:
                AppsScriptRelayBackend().send_messages([self._message()])
        self.assertIn("Could not reach the mail relay", str(caught.exception))

    def test_missing_settings_are_reported_and_fail_silently_is_respected(self):
        with override_settings(EMAIL_RELAY_URL="", EMAIL_RELAY_SECRET=""):
            with self.assertRaises(EmailRelayError) as caught:
                AppsScriptRelayBackend().send_messages([self._message()])
            self.assertIn("not set up", str(caught.exception))
            self.assertEqual(AppsScriptRelayBackend(fail_silently=True).send_messages([self._message()]), 0)

    def test_no_messages_means_nothing_is_sent(self):
        self.assertEqual(AppsScriptRelayBackend().send_messages([]), 0)

    def test_a_subject_with_a_line_break_is_refused_like_everywhere_else(self):
        built = ce.build_email(ce.APPROVED, ce.sample_data(ce.APPROVED), base_url=BASE, shop=SHOP, inline_logo=True)
        message = ce._make_message(built, "rainer@gmail.com", subject="Line one\nLine two")
        server = self._server()
        with override_settings(EMAIL_RELAY_URL=server.url):
            with self.assertRaises(ValueError):
                AppsScriptRelayBackend().send_messages([message])
        self.assertEqual(server.received, [])                       # nothing went out


# =====================================================================================================================
# The hooks: the real views and services produce the right emails
# =====================================================================================================================
@override_settings(**ON)
class CustomerEmailHookTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user("email_customer", email="rainer@gmail.com", first_name="Rainer")
        UserProfile.objects.create(user=cls.customer, display_name="Rainer Pepito")
        cls.other = User.objects.create_user("email_other_customer", email="other@gmail.com")
        cls.staff = User.objects.create_user("email_staff", email="staff@gmail.com", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    # ---- helpers ---------------------------------------------------------------------------------------------------
    def _reservation(self, customer=None, status=Reservation.Status.PENDING, items=1, **item_fields):
        today = timezone.localdate()
        reservation = Reservation.objects.create(
            customer=customer or self.customer, customer_name="Rainer Pepito", status=status,
            rental_subtotal=Decimal("20000"), security_deposit=Decimal(2000 * items), total_amount=Decimal(20000 + 2000 * items),
        )
        defaults = dict(rental_date=today + timedelta(days=10), return_date=today + timedelta(days=13))
        defaults.update(item_fields)
        for number in range(items):
            ReservationItem.objects.create(reservation=reservation, gown_name=f"Wedding Gown {17 + number}", **defaults)
        return reservation

    def _post(self, name, args, payload=None):
        kwargs = {"data": json.dumps(payload), "content_type": "application/json"} if payload is not None else {}
        return self.client.post(reverse(f"arabela_admin:{name}", args=args), **kwargs)

    def _the_one_email(self):
        self.assertEqual(len(mail.outbox), 1, [m.subject for m in mail.outbox])
        return mail.outbox[0]

    def _html(self, message):
        return message.alternatives[0][0]

    # ---- the receipt ---------------------------------------------------------------------------------------------------
    def test_checkout_sends_the_receipt_once(self):
        gown = Gown.objects.create(
            gown_id="EMAILTEST-0001", name="Email Test Gown", category=Gown.Category.GUEST_GOWN,
            color_name="Red", color_code="RD", size=Gown.Size.MEDIUM, rental_price=Decimal("4500.00"))
        self.client.force_login(self.customer)

        def submit():
            return self.client.post(reverse("gowns:reservation_submit"), data={
                "items": json.dumps([{"gown_name": gown.name, "gown_slug": gown.slug, "size": "Medium",
                                      "rental_date": "2027-03-10", "return_date": "2027-03-13"}]),
                "first_name": "Test", "last_name": "Buyer", "phone": "09171234567", "address": "123 Test St",
                "city": "Test City", "postal_code": "1000", "payment_method": "GCash",
                "proof_of_payment": SimpleUploadedFile("proof.jpg", _TINY_JPEG, content_type="image/jpeg"),
            })

        with patch("gowns.views._save_proof_file", return_value="https://example.test/fake-proof.jpg"):
            with self.captureOnCommitCallbacks(execute=True):
                first = submit()
            self.assertEqual(first.status_code, 200, first.content)
            with self.captureOnCommitCallbacks(execute=True):
                again = submit()                                  # the phone never saw the first answer
            self.assertTrue(again.json()["already_submitted"])
        email = self._the_one_email()
        code = first.json()["reference_code"]
        self.assertEqual(email.subject, f"We received your reservation {code}")
        self.assertEqual(email.to, ["rainer@gmail.com"])
        self.assertIn("Hi Test,", email.body)
        self.assertIn(code, self._html(email))
        self.assertIn("Email Test Gown", email.body)
        self.assertIn("₱6,500", email.body)                  # price from the database + the 2,000 deposit

    # ---- approved / rejected ---------------------------------------------------------------------------------------------
    def test_approving_emails_the_customer(self):
        reservation = self._reservation()
        with self.captureOnCommitCallbacks(execute=True):
            response = self._post("reservation_approve", [reservation.pk])
        self.assertEqual(response.status_code, 200)
        email = self._the_one_email()
        self.assertEqual(email.subject, f"Your reservation {reservation.reference_code} is approved")
        self.assertEqual(email.to, ["rainer@gmail.com"])
        self.assertEqual(email.from_email, "Arabela <shop@gmail.com>")
        self.assertIn("Hi Rainer,", email.body)
        self.assertIn("Wedding Gown 17", self._html(email))
        self.assertIn("₱22,000", self._html(email))
        self.assertIn('src="cid:arabela-logo"', self._html(email))
        self.assertTrue(email.logo_png.startswith(b"\x89PNG"))        # the logo travels inside the email
        self.assertIn("image/png", [part.get_content_type() for part in email.message().walk()])

    def test_a_double_clicked_approve_sends_one_email(self):
        reservation = self._reservation()
        with self.captureOnCommitCallbacks(execute=True):
            self._post("reservation_approve", [reservation.pk])
            self._post("reservation_approve", [reservation.pk])
        self.assertEqual(ReservationStatusEvent.objects.filter(reservation=reservation, label="Reservation approved").count(), 2)
        self._the_one_email()

    def test_rejecting_emails_the_reason(self):
        reservation = self._reservation()
        with self.captureOnCommitCallbacks(execute=True):
            response = self._post("reservation_reject", [reservation.pk], {"reason": "The GCash receipt is for another amount."})
        self.assertEqual(response.status_code, 200)
        email = self._the_one_email()
        self.assertEqual(email.subject, f"Update on your reservation {reservation.reference_code}")
        self.assertIn("The GCash receipt is for another amount.", email.body)
        self.assertIn("The GCash receipt is for another amount.", self._html(email))

    def test_rejecting_without_a_reason_still_emails(self):
        reservation = self._reservation()
        with self.captureOnCommitCallbacks(execute=True):
            self._post("reservation_reject", [reservation.pk], {})
        self.assertIn("Please contact the shop for details.", self._the_one_email().body)

    # ---- deposit settled ----------------------------------------------------------------------------------------------------
    def test_the_deposit_email_comes_when_the_last_gown_is_back(self):
        reservation = self._reservation(status=Reservation.Status.CONFIRMED, items=2, stage=ReservationItem.Stage.RETURN)
        first, second = reservation.items.order_by("id")
        with self.captureOnCommitCallbacks(execute=True):
            self._post("reservation_item_mark_returned", [first.id], {"condition": ReservationItem.ReturnCondition.GOOD})
        self.assertEqual(len(mail.outbox), 0, "one gown back is only a check-in, not an email")
        with self.captureOnCommitCallbacks(execute=True):
            self._post("reservation_item_mark_returned", [second.id], {"condition": ReservationItem.ReturnCondition.GOOD})
        email = self._the_one_email()
        self.assertEqual(email.subject, f"Your security deposit is settled - {reservation.reference_code}")
        self.assertIn("This reservation is complete", email.body)

    # ---- reminders -----------------------------------------------------------------------------------------------------------
    def test_the_reminder_sweep_emails_once_per_reminder(self):
        today = timezone.localdate()
        self._reservation(status=Reservation.Status.CONFIRMED, rental_date=today + timedelta(days=1), return_date=today + timedelta(days=4))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(reminders.send_due_reminders(today), 1)
        email = self._the_one_email()
        self.assertEqual(email.subject, "A reminder from Arabela")
        self.assertIn("starts tomorrow", email.body)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(reminders.send_due_reminders(today), 0)      # already sent: nothing, no second email
        self.assertEqual(len(mail.outbox), 1)

    def test_an_overdue_notice_is_emailed(self):
        today = timezone.localdate()
        self._reservation(status=Reservation.Status.CONFIRMED, stage=ReservationItem.Stage.RETURN,
                          rental_date=today - timedelta(days=6), return_date=today - timedelta(days=2))
        with self.captureOnCommitCallbacks(execute=True):
            reminders.send_due_reminders(today)
        email = self._the_one_email()
        self.assertEqual(email.subject, "Your Arabela gown is overdue")
        self.assertIn("2 days overdue", email.body)

    def test_a_reminder_staff_write_by_hand_is_emailed(self):
        reservation = self._reservation(status=Reservation.Status.CONFIRMED)
        item = reservation.items.get()
        with self.captureOnCommitCallbacks(execute=True):
            reminders.send_manual_reminder(item, "Please bring your valid ID when you pick up.", sent_by="Maria")
        email = self._the_one_email()
        self.assertEqual(email.subject, "A reminder from Arabela")
        self.assertIn("Please bring your valid ID when you pick up.", email.body)

    def test_a_reminder_that_rolls_back_sends_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    CustomerMessage.objects.create(recipient=self.customer, category=CustomerMessage.Category.RESERVATION_REMINDER, body="x")
                    raise RuntimeError("the reminder could not be recorded")
            except RuntimeError:
                pass
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(CustomerMessage.objects.filter(recipient=self.customer).exists())

    # ---- account notices -----------------------------------------------------------------------------------------------------
    def test_flagging_and_unflagging_are_emailed(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._post("customer_flag", [self.customer.id], {"flagged": True, "reason": "Repeated no-shows"})
        email = self._the_one_email()
        self.assertEqual(email.subject, "Important notice about your Arabela account")
        self.assertIn("Repeated no-shows", email.body)
        with self.captureOnCommitCallbacks(execute=True):
            self._post("customer_flag", [self.customer.id], {"flagged": False, "reason": "Issue Resolved"})
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[1].subject, "Your Arabela account is no longer flagged")
        self.assertIn("Issue Resolved", mail.outbox[1].body)

    def test_the_automatic_flag_is_emailed_but_the_lock_is_not(self):
        from accounts.services import record_abandoned_hold
        UserProfile.objects.filter(user=self.customer).update(hold_abandon_count=4)
        with self.captureOnCommitCallbacks(execute=True):
            record_abandoned_hold(self.customer)                  # the 5th: a 30-minute lock
        self.assertGreater(CustomerMessage.objects.filter(recipient=self.customer, category=CustomerMessage.Category.CANCELLATION_LOCKOUT).count(), 0)
        self.assertEqual(len(mail.outbox), 0, "the lock notice stays in the site")
        UserProfile.objects.filter(user=self.customer).update(hold_abandon_count=14)
        with self.captureOnCommitCallbacks(execute=True):
            record_abandoned_hold(self.customer)                  # the 15th: flagged
        self.assertTrue(UserProfile.objects.get(user=self.customer).is_flagged)
        self.assertEqual(self._the_one_email().subject, "Important notice about your Arabela account")

    def test_removing_a_lock_is_not_emailed(self):
        UserProfile.objects.filter(user=self.customer).update(
            hold_abandon_count=5, cancel_tier1_lockout_sent=True, cancel_lockout_until=timezone.now() + timedelta(minutes=30))
        with self.captureOnCommitCallbacks(execute=True):
            response = self._post("customer_unlock", [self.customer.id], {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CustomerMessage.objects.filter(recipient=self.customer, category=CustomerMessage.Category.GENERAL).count(), 1)
        self.assertEqual(len(mail.outbox), 0)

    # ---- what is never emailed -------------------------------------------------------------------------------------------------
    def test_other_notices_are_never_emailed(self):
        reservation = self._reservation(status=Reservation.Status.CONFIRMED)
        item = reservation.items.get()
        with self.captureOnCommitCallbacks(execute=True):
            ReservationStatusEvent.record(reservation, "Reservation approved", staff_only=True)             # shop-only note
            ReservationStatusEvent.record(reservation, "Reservation approved", item=item)                   # one gown's entry
            ReservationStatusEvent.record(reservation, "Reservation cancelled", actor=ReservationStatusEvent.Actor.CUSTOMER)
            ReservationStatusEvent.record(reservation, f"{item.gown_name} picked up", item=item)
            ReservationStatusEvent.record(reservation, f"{item.gown_name} return date changed", item=item)
            CustomerMessage.objects.create(recipient=self.customer, category=CustomerMessage.Category.GENERAL, body="hello")
            CustomerMessage.objects.create(recipient=self.customer, category=CustomerMessage.Category.CANCELLATION_LOCKOUT, body="locked")
        self.assertEqual(len(mail.outbox), 0)

    def test_only_the_bookings_own_customer_is_emailed(self):
        reservation = self._reservation(customer=self.customer)
        self._reservation(customer=self.other)
        with self.captureOnCommitCallbacks(execute=True):
            self._post("reservation_approve", [reservation.pk])
        self.assertEqual([m.to for m in mail.outbox], [["rainer@gmail.com"]])

    def test_accounts_without_a_usable_address_are_skipped_quietly(self):
        demo = User.objects.create_user("demo_account", email="demo@example.invalid")
        blank = User.objects.create_user("no_email_account", email="")
        admin_booker = User.objects.create_user("staff_booker", email="boss@gmail.com", is_staff=True)
        for person in (demo, blank, admin_booker):
            reservation = self._reservation(customer=person)
            with self.captureOnCommitCallbacks(execute=True):
                response = self._post("reservation_approve", [reservation.pk])
            self.assertEqual(response.status_code, 200)
            reservation.refresh_from_db()
            self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
        self.assertEqual(len(mail.outbox), 0)

    # ---- the switch, and failures that must never hurt the real work ---------------------------------------------------------------
    @override_settings(CUSTOMER_EMAILS_ENABLED=False)
    def test_with_the_switch_off_nothing_is_sent_and_everything_still_works(self):
        reservation = self._reservation()
        with self.captureOnCommitCallbacks(execute=True):
            response = self._post("reservation_approve", [reservation.pk])
        self.assertEqual(response.status_code, 200)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
        self.assertTrue(ReservationStatusEvent.objects.filter(reservation=reservation, label="Reservation approved").exists())
        self.assertEqual(len(mail.outbox), 0)

    @patch("accounts.customer_emails.time.sleep")
    @patch("django.core.mail.EmailMessage.send", side_effect=smtplib.SMTPAuthenticationError(535, b"bad credentials"))
    def test_a_mail_server_failure_never_breaks_approving(self, _send, _sleep):
        reservation = self._reservation()
        with self.assertLogs("accounts.customer_emails", level="WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                response = self._post("reservation_approve", [reservation.pk])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
        self.assertTrue(ReservationStatusEvent.objects.filter(reservation=reservation, label="Reservation approved").exists())
        self.assertEqual(_send.call_count, 2)                      # one retry
        joined = "\n".join(logs.output)
        self.assertIn("NOT sent", joined)
        self.assertIn("App Password", joined)                      # the log says what to fix
        self.assertNotIn("rainer@gmail.com", joined)               # addresses are masked in logs

    @patch("accounts.customer_emails.build_email", side_effect=RuntimeError("boom"))
    def test_a_bug_while_building_an_email_never_breaks_the_real_action(self, _build):
        reservation = self._reservation()
        with self.assertLogs("accounts.customer_emails", level="ERROR"):
            with self.captureOnCommitCallbacks(execute=True):
                response = self._post("reservation_approve", [reservation.pk])
        self.assertEqual(response.status_code, 200)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
        self.assertTrue(ReservationStatusEvent.objects.filter(reservation=reservation, label="Reservation approved").exists())

    @patch("accounts.customer_emails.time.sleep")
    @patch("django.core.mail.EmailMessage.send", side_effect=[OSError("temporary"), 1])
    def test_one_failed_attempt_is_retried(self, _send, _sleep):
        reservation = self._reservation()
        with self.captureOnCommitCallbacks(execute=True):
            self._post("reservation_approve", [reservation.pk])
        self.assertEqual(_send.call_count, 2)

    @override_settings(CUSTOMER_EMAILS_BACKGROUND=True)
    def test_the_background_worker_delivers_without_holding_up_the_request(self):
        reservation = self._reservation()
        with self.captureOnCommitCallbacks(execute=True):
            response = self._post("reservation_approve", [reservation.pk])
        self.assertEqual(response.status_code, 200)
        deadline = time.time() + 10
        while time.time() < deadline and (ce._QUEUE.unfinished_tasks or not mail.outbox):
            time.sleep(0.05)
        self._the_one_email()

    # ---- the shop details come from Edit Profile ----------------------------------------------------------------------------------------
    def test_shop_details_come_from_edit_profile(self):
        shop = SiteSettings.load()
        shop.phone, shop.facebook_url = "09171112222", "https://www.facebook.com/arabela-shop"
        shop.shop_street, shop.shop_city, shop.shop_country, shop.shop_postal_code = "1 Main St", "Antipolo", "Philippines", "1870"
        shop.save()
        info = ce.shop_info()
        self.assertEqual(info["phone"], "09171112222")
        self.assertEqual(info["facebook_url"], "https://www.facebook.com/arabela-shop")
        self.assertEqual(info["address"], "1 Main St, Antipolo, Philippines 1870")
        shop.facebook_url = "not a link"
        shop.save()
        self.assertEqual(ce.shop_info()["facebook_url"], "")

    def test_links_use_the_live_site_address_when_no_override_is_set(self):
        site, _ = Site.objects.update_or_create(id=settings.SITE_ID, defaults={"domain": "arabela-gown-rental.onrender.com", "name": "Arabela"})
        with override_settings(SITE_BASE_URL=""):
            Site.objects.clear_cache()
            self.assertEqual(ce.site_base_url(), "https://arabela-gown-rental.onrender.com")
            site.domain = "127.0.0.1:8000"
            site.save()
            Site.objects.clear_cache()
            self.assertEqual(ce.site_base_url(), "http://127.0.0.1:8000")
        Site.objects.clear_cache()


# =====================================================================================================================
# The owner's page: preview + "Send test to me"
# =====================================================================================================================
@override_settings(SITE_BASE_URL=BASE, DEFAULT_FROM_EMAIL="Arabela <shop@gmail.com>")
class CustomerEmailAdminPageTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("email_owner", "owner@gmail.com", "x")
        cls.staff = User.objects.create_user("email_page_staff", email="staff@gmail.com", password="x", is_staff=True)
        cls.customer = User.objects.create_user("email_page_customer", email="cust@gmail.com", password="x")

    def setUp(self):
        self.client.force_login(self.owner)
        self.page = reverse("arabela_admin:email_preview")
        self.test_url = reverse("arabela_admin:email_test_send")

    def _test(self, kind="approved"):
        return self.client.post(self.test_url, data=json.dumps({"kind": kind}), content_type="application/json")

    @override_settings(CUSTOMER_EMAILS_ENABLED=False)
    def test_the_owner_sees_the_status_and_every_email(self):
        response = self.client.get(self.page)
        self.assertEqual(response.status_code, 200)
        for label in ce.KIND_LABELS.values():
            self.assertContains(response, label)
        self.assertContains(response, "Send test to me")
        self.assertContains(response, "owner@gmail.com")
        self.assertContains(response, BASE)
        self.assertContains(response, "not</strong> being emailed yet")           # the switch is off by default

    @override_settings(CUSTOMER_EMAILS_ENABLED=True)
    def test_the_page_shows_when_the_switch_is_on(self):
        self.assertContains(self.client.get(self.page), "Customers are emailed when their reservation changes")

    def test_every_preview_renders_and_may_only_be_framed_by_this_site(self):
        for kind in ce.KIND_LABELS:
            response = self.client.get(reverse("arabela_admin:email_preview_render", args=[kind]))
            self.assertEqual(response.status_code, 200, kind)
            self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
            self.assertContains(response, 'alt="Arabela"')
            self.assertContains(response, f"{BASE}/static/images/email/arabela-logo-white.png")   # the website copy, not cid:
        self.assertEqual(self.client.get(reverse("arabela_admin:email_preview_render", args=["nonsense"])).status_code, 404)

    def test_only_the_owner_can_open_the_page_or_the_previews(self):
        self.client.force_login(self.staff)
        self.assertRedirects(self.client.get(self.page), reverse("arabela_admin:dashboard"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("arabela_admin:email_preview_render", args=["approved"])).status_code, 302)
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(self.page).status_code, 302)
        self.client.logout()
        self.assertRedirects(self.client.get(self.page), reverse("arabela_admin:admin_login"), fetch_redirect_response=False)

    def test_the_test_email_goes_only_to_the_owner(self):
        response = self._test("approved")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["success"])
        self.assertIn("owner@gmail.com", response.json()["message"])
        email = mail.outbox[0]
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(email.to, ["owner@gmail.com"])
        self.assertTrue(email.subject.startswith("[TEST] Your reservation RSV-2026-0054 is approved"))
        self.assertIn('src="cid:arabela-logo"', email.alternatives[0][0])      # a test is exactly what a customer gets
        self.assertTrue(email.logo_png.startswith(b"\x89PNG"))

    def test_every_kind_can_be_tested(self):
        for kind in ce.KIND_LABELS:
            session = self.client.session
            session.pop("email_test_sent_at", None)               # skip the few-seconds wait between tests
            session.save()
            self.assertEqual(self._test(kind).status_code, 200, kind)
        self.assertEqual(len(mail.outbox), len(ce.KIND_LABELS))

    def test_tests_cannot_be_spammed(self):
        self.assertEqual(self._test().status_code, 200)
        second = self._test()
        self.assertEqual(second.status_code, 429)
        self.assertEqual(len(mail.outbox), 1)

    def test_bad_requests_are_refused(self):
        self.assertEqual(self._test("nonsense").status_code, 400)
        self.assertEqual(self.client.post(self.test_url, data="{not json", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.get(self.test_url).status_code, 405)
        self.assertEqual(len(mail.outbox), 0)

    def test_an_owner_without_an_email_address_is_told_what_to_do(self):
        User.objects.filter(pk=self.owner.pk).update(email="")
        response = self._test()
        self.assertEqual(response.status_code, 400)
        self.assertIn("no email address", response.json()["error"])
        self.assertEqual(len(mail.outbox), 0)

    def test_only_the_owner_can_send_a_test(self):
        self.client.force_login(self.staff)
        self.assertEqual(self._test().status_code, 403)
        self.client.force_login(self.customer)
        self.assertEqual(self._test().status_code, 302)
        self.assertEqual(len(mail.outbox), 0)

    @patch("django.core.mail.EmailMessage.send", side_effect=OSError("Network is unreachable"))
    def test_a_failed_test_says_why(self, _send):
        response = self._test()
        self.assertEqual(response.status_code, 502)
        self.assertIn("FREE plan", response.json()["error"])

    @override_settings(CUSTOMER_EMAILS_ENABLED=False)
    def test_the_test_works_while_the_switch_is_off(self):
        self.assertEqual(self._test().status_code, 200)

    def test_edit_profile_links_to_the_page_for_the_owner_only(self):
        profile = reverse("arabela_admin:page", args=["profile"])
        response = self.client.get(profile)
        self.assertContains(response, "Preview and test emails")
        self.assertContains(response, self.page)
        self.client.force_login(self.staff)
        self.assertNotContains(self.client.get(profile), "Preview and test emails")
