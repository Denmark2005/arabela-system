"""The owner's Customer emails page shows what actually happened to each email (sent / failed with the reason / skipped with the
reason), and can send a real test to a customer's address -- so "the customer never got it" can be answered from the site itself."""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts import customer_emails as ce
from accounts.email_backends import EmailRelayError

User = get_user_model()


class EmailActivityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("activity_owner", "owner@gmail.com", "x")
        cls.staff = User.objects.create_user("activity_staff", email="staffer@gmail.com", password="x", is_staff=True)
        cls.customer = User.objects.create_user("activity_france", email="france.test@gmail.com", password="x")

    def setUp(self):
        ce._RECENT.clear()
        self.addCleanup(ce._RECENT.clear)
        self.client.force_login(self.owner)
        self.page = reverse("arabela_admin:email_preview")
        self.test_url = reverse("arabela_admin:email_test_send")

    def send_test(self, **body):
        return self.client.post(self.test_url, data=json.dumps({"kind": "approved", **body}), content_type="application/json")

    # ---- what gets recorded --------------------------------------------------------------------------------------------
    def test_a_sent_email_is_recorded_with_a_masked_address(self):
        message = ce._make_message(ce.build_email(ce.RECEIVED, ce.sample_data(ce.RECEIVED)), "france.test@gmail.com")
        self.assertTrue(ce._send_now(message, ce.RECEIVED))
        [row] = ce.recent_attempts()
        self.assertEqual((row["outcome"], row["kind"], row["to"]), ("sent", ce.RECEIVED, "f***@gmail.com"))
        self.assertEqual(row["label"], ce.KIND_LABELS[ce.RECEIVED])

    @patch("accounts.customer_emails.time.sleep")
    def test_a_failed_email_is_recorded_with_the_reason_the_owner_can_act_on(self, _sleep):
        message = ce._make_message(ce.build_email(ce.APPROVED, ce.sample_data(ce.APPROVED)), "france.test@gmail.com")
        with patch.object(type(message), "send", side_effect=EmailRelayError("The mail relay refused the email: Service invoked too many times.")):
            with self.assertLogs("accounts.customer_emails", level="WARNING"):
                self.assertFalse(ce._send_now(message, ce.APPROVED))
        [row] = ce.recent_attempts()
        self.assertEqual((row["outcome"], row["to"]), ("failed", "f***@gmail.com"))
        self.assertIn("Service invoked too many times", row["reason"])

    def test_a_skipped_email_is_recorded_with_why(self):
        with self.assertLogs("accounts.customer_emails", level="WARNING"):
            ce._deliver(self.staff, ce.APPROVED, {})
        [row] = ce.recent_attempts()
        self.assertEqual(row["outcome"], "skipped")
        self.assertIn("staff account", row["reason"])
        self.assertTrue(row["reason"].startswith("It is a staff account"))
        self.assertEqual(row["to"], "s***@gmail.com")

    def test_only_the_latest_forty_are_kept_newest_first(self):
        for number in range(45):
            ce._note("approved", f"person{number}@gmail.com", "sent")
        rows = ce.recent_attempts()
        self.assertEqual(len(rows), 40)
        self.assertEqual(rows[0]["to"], "p***@gmail.com")
        ce._note("reminder", "last@gmail.com", "failed", "x")
        self.assertEqual(ce.recent_attempts()[0]["kind"], "reminder")

    # ---- the page ----------------------------------------------------------------------------------------------------------
    def test_the_page_lists_what_happened_and_never_shows_a_full_address(self):
        ce._note(ce.RECEIVED, "france.test@gmail.com", "sent")
        ce._note(ce.APPROVED, "other@gmail.com", "failed", "The mail relay refused the email: secret does not match.")
        ce._note(ce.REMINDER, "owner2@gmail.com", "skipped", "It is a staff account (staff accounts are never emailed).")
        html = self.client.get(self.page).content.decode()
        self.assertIn("What happened to recent emails", html)
        self.assertIn("Sent to Gmail", html)
        self.assertIn("Failed", html)
        self.assertIn("secret does not match", html)
        self.assertIn("Not sent", html)
        self.assertIn("f***@gmail.com", html)
        self.assertNotIn("france.test@gmail.com", html)

    def test_the_page_explains_an_empty_list(self):
        self.assertContains(self.client.get(self.page), "Nothing has been sent since the site last started")

    def test_only_the_owner_sees_the_list(self):
        ce._note(ce.RECEIVED, "france.test@gmail.com", "sent")
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.page).status_code, 302)

    # ---- a real test to a customer's address ---------------------------------------------------------------------------------
    def test_a_test_can_go_to_an_existing_customers_address_and_shows_in_the_list(self):
        response = self.send_test(to="  France.Test@gmail.com ")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([m.to for m in mail.outbox], [["france.test@gmail.com"]])
        self.assertTrue(mail.outbox[0].subject.startswith("[TEST]"))
        [row] = ce.recent_attempts()
        self.assertEqual((row["kind"], row["outcome"], row["to"], row["label"]), ("test", "sent", "f***@gmail.com", "Test email"))

    def test_a_test_to_the_owner_still_works_as_before(self):
        self.assertEqual(self.send_test().status_code, 200)
        self.assertEqual([m.to for m in mail.outbox], [["owner@gmail.com"]])

    def test_a_test_can_not_go_to_a_stranger_or_a_staff_address(self):
        for address in ("stranger@gmail.com", "staffer@gmail.com", "owner@gmail.com"):
            response = self.send_test(to=address)
            self.assertEqual(response.status_code, 400, address)
            self.assertIn("not a customer account", response.json()["error"])
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.dummy.EmailBackend")
    def test_a_failed_test_is_listed_with_its_reason(self):
        with patch("accounts.customer_emails.EmailMultiAlternatives.send", side_effect=EmailRelayError("The mail relay did not answer properly.")):
            with self.assertLogs("accounts.customer_emails", level="WARNING"):
                response = self.send_test(to="france.test@gmail.com")
        self.assertEqual(response.status_code, 502)
        [row] = ce.recent_attempts()
        self.assertEqual((row["kind"], row["outcome"]), ("test", "failed"))
        self.assertIn("did not answer properly", row["reason"])
