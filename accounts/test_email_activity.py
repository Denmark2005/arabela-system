"""The owner's Email history (Customer emails page): every email the site tries to send is saved -- sent, failed with the
reason, or not sent with the reason -- shown 25 to a page with a filter and a search, and a real test can be sent to a customer's
address. Saved in the database so it is still there after the site restarts."""
import json
import threading
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import ProgrammingError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import customer_emails as ce
from accounts.email_backends import EmailRelayError
from accounts.models import CustomerEmailLog

User = get_user_model()


def log(outcome="sent", to="person@gmail.com", kind="approved", reference="", reason="", age_days=0):
    row = CustomerEmailLog.objects.create(kind=kind, reference_code=reference, to_address=to, outcome=outcome, reason=reason)
    if age_days:
        CustomerEmailLog.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(days=age_days))
    return row


class EmailHistorySavingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user("history_france", email="france.test@gmail.com", password="x")

    def setUp(self):
        ce._RECENT.clear()
        self.addCleanup(ce._RECENT.clear)

    def message(self, kind=ce.APPROVED, to="france.test@gmail.com", reference=""):
        message = ce._make_message(ce.build_email(kind, ce.sample_data(kind)), to)
        message.history_reference = reference
        return message

    def test_a_sent_email_is_saved_with_the_full_address_and_the_booking_code(self):
        self.assertTrue(ce._send_now(self.message(reference="RSV-2026-0058"), ce.APPROVED))
        row = CustomerEmailLog.objects.get()
        self.assertEqual((row.outcome, row.kind, row.to_address, row.reference_code), ("sent", "approved", "france.test@gmail.com", "RSV-2026-0058"))

    @patch("accounts.customer_emails.time.sleep")
    def test_a_failed_email_is_saved_with_the_reason(self, _sleep):
        message = self.message()
        with patch.object(type(message), "send", side_effect=EmailRelayError("The mail relay refused the email: Service invoked too many times.")):
            with self.assertLogs("accounts.customer_emails", level="WARNING"):
                self.assertFalse(ce._send_now(message, ce.APPROVED))
        row = CustomerEmailLog.objects.get()
        self.assertEqual((row.outcome, row.to_address), ("failed", "france.test@gmail.com"))
        self.assertIn("Service invoked too many times", row.reason)

    def test_a_skipped_email_is_saved_with_why_and_the_code_found_in_the_notice(self):
        demo = User.objects.create_user("history_demo", email="demo@example.com", password="x")
        with self.assertLogs("accounts.customer_emails", level="WARNING"):
            ce._deliver(demo, ce.REMINDER, {"text": "Wedding Gown 3 is due back tomorrow. Reference: RSV-2026-0058."})
        row = CustomerEmailLog.objects.get()
        self.assertEqual((row.outcome, row.reference_code, row.to_address), ("skipped", "RSV-2026-0058", "demo@example.com"))
        self.assertTrue(row.reason.startswith("The address is a placeholder"))

    @override_settings(CUSTOMER_EMAILS_BACKGROUND=False)
    def test_a_booking_email_carries_its_booking_code_into_the_history(self):
        ce._deliver(self.customer, ce.APPROVED, {**ce.sample_data(ce.APPROVED), "reference_code": "RSV-2026-0099"})
        row = CustomerEmailLog.objects.get()
        self.assertEqual((row.outcome, row.reference_code, row.to_address), ("sent", "RSV-2026-0099", "france.test@gmail.com"))

    def test_saving_the_history_can_never_stop_an_email(self):
        with patch.object(CustomerEmailLog.objects, "create", side_effect=RuntimeError("database is down")):
            with self.assertLogs("accounts.customer_emails", level="WARNING") as logs:
                self.assertTrue(ce._send_now(self.message(), ce.APPROVED))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Could not save the email history row", "\n".join(logs.output))

    def test_rows_older_than_about_six_months_are_removed_and_newer_ones_kept(self):
        old = log(age_days=ce.HISTORY_KEEP_DAYS + 5)
        recent = log(age_days=ce.HISTORY_KEEP_DAYS - 5)
        ce._prune_history()
        self.assertEqual(list(CustomerEmailLog.objects.values_list("pk", flat=True)), [recent.pk])
        self.assertFalse(CustomerEmailLog.objects.filter(pk=old.pk).exists())

    @override_settings(CUSTOMER_EMAIL_HISTORY_IN_WORKER=False)
    def test_the_sending_thread_can_be_told_not_to_touch_the_database(self):
        worker = threading.Thread(name="customer-email-worker", target=ce._save_history, args=("approved", "x@gmail.com", "sent", "", ""))
        worker.start()
        worker.join(10)
        self.assertEqual(CustomerEmailLog.objects.count(), 0)

    def test_the_in_memory_list_keeps_addresses_masked_and_only_the_latest_forty(self):
        for number in range(45):
            ce._note("approved", f"person{number}@gmail.com", "sent")
        rows = ce.recent_attempts()
        self.assertEqual(len(rows), 40)
        self.assertEqual(rows[0]["to"], "p***@gmail.com")
        self.assertEqual(CustomerEmailLog.objects.count(), 45)               # the table keeps them all


class EmailHistoryPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("history_owner", "owner@gmail.com", "x")
        cls.staff = User.objects.create_user("history_staff", email="staffer@gmail.com", password="x", is_staff=True)
        cls.customer = User.objects.create_user("history_customer", email="france.test@gmail.com", password="x")

    def setUp(self):
        ce._RECENT.clear()
        self.addCleanup(ce._RECENT.clear)
        self.client.force_login(self.owner)
        self.page = reverse("arabela_admin:email_preview")
        self.test_url = reverse("arabela_admin:email_test_send")

    def get(self, **params):
        return self.client.get(self.page, params)

    def test_the_page_lists_every_result_with_full_addresses_and_booking_codes(self):
        log("sent", "france.test@gmail.com", "received", "RSV-2026-0058")
        log("failed", "other@gmail.com", "approved", "RSV-2026-0059", "The mail relay refused the email: secret does not match.")
        log("skipped", "demo@example.com", "reminder", "", "The address is a placeholder (example.com, .invalid, .test ...).")
        html = self.get().content.decode()
        self.assertIn("Email history", html)
        for text in ("Sent to Gmail", "Failed", "Not sent", "france.test@gmail.com", "other@gmail.com", "RSV-2026-0058",
                     "secret does not match", "The address is a placeholder"):
            self.assertIn(text, html)

    def test_it_shows_25_to_a_page_with_newer_and_older_links(self):
        for number in range(60):
            log(to=f"person{number:02d}@gmail.com")
        first = self.get().content.decode()
        self.assertIn("Page 1 of 3", first)
        self.assertIn("60 emails", first)
        self.assertEqual(first.count("@gmail.com</td>"), 25)
        self.assertIn("person59@gmail.com", first)                      # newest first
        self.assertNotIn("person34@gmail.com", first)
        self.assertIn("?page=2#history", first)                         # Older
        last = self.get(page=3).content.decode()
        self.assertEqual(last.count("@gmail.com</td>"), 10)
        self.assertIn("person00@gmail.com", last)
        self.assertIn("?page=2#history", last)                          # Newer
        self.assertIn("Page 3 of 3", self.get(page=99).content.decode())  # past the end shows the last page

    def test_the_result_filter_shows_only_those_and_counts_each(self):
        for _ in range(3):
            log("sent")
        log("failed", "bad@gmail.com", reason="relay down")
        log("skipped", "none@example.com", reason="placeholder")
        html = self.get(show="failed").content.decode()
        self.assertIn("bad@gmail.com", html)
        self.assertNotIn("none@example.com", html)
        self.assertNotIn("person@gmail.com", html)
        for chip in ("All &middot; 5", "Sent &middot; 3", "Failed &middot; 1", "Not sent &middot; 1"):
            self.assertIn(chip, html)
        self.assertIn("1 email found", html)
        self.assertEqual(self.get(show="nonsense").status_code, 200)    # an unknown filter is just "all"

    def test_the_search_finds_an_address_or_a_booking_code(self):
        log(to="france.test@gmail.com", reference="RSV-2026-0058")
        log(to="someone.else@gmail.com", reference="RSV-2026-0070")
        by_address = self.get(q="FRANCE").content.decode()
        self.assertIn("france.test@gmail.com", by_address)
        self.assertNotIn("someone.else@gmail.com", by_address)
        by_code = self.get(q="0070").content.decode()
        self.assertIn("someone.else@gmail.com", by_code)
        self.assertNotIn("france.test@gmail.com", by_code)
        self.assertIn("No email matches that", self.get(q="zzzz").content.decode())

    def test_search_and_filter_stay_when_paging(self):
        for number in range(30):
            log("failed", f"bad{number}@gmail.com")
        html = self.get(show="failed", q="bad").content.decode()
        self.assertIn("show=failed&amp;q=bad&amp;page=2#history", html)

    def test_an_empty_history_says_so(self):
        self.assertContains(self.get(), "No email has been sent yet")

    def test_without_the_table_the_page_still_works_and_explains(self):
        ce._note(ce.RECEIVED, "france.test@gmail.com", "sent")
        with patch.object(CustomerEmailLog.objects, "values", side_effect=ProgrammingError("relation does not exist")):
            response = self.get()
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("saved history starts once the database update", html)
        self.assertIn("f***@gmail.com", html)                           # the short in-memory list, addresses masked

    def test_only_the_owner_sees_the_history(self):
        log()
        self.client.force_login(self.staff)
        self.assertEqual(self.get().status_code, 302)

    # ---- a real test to a customer's address -----------------------------------------------------------------------------
    def send_test(self, **body):
        return self.client.post(self.test_url, data=json.dumps({"kind": "approved", **body}), content_type="application/json")

    def test_a_test_can_go_to_an_existing_customers_address_and_is_saved_in_the_history(self):
        response = self.send_test(to="  France.Test@gmail.com ")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([m.to for m in mail.outbox], [["france.test@gmail.com"]])
        self.assertTrue(mail.outbox[0].subject.startswith("[TEST]"))
        row = CustomerEmailLog.objects.get()
        self.assertEqual((row.kind, row.outcome, row.to_address), ("test", "sent", "france.test@gmail.com"))
        self.assertIn("Test email", self.get().content.decode())

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
    def test_a_failed_test_is_saved_with_its_reason(self):
        with patch("accounts.customer_emails.EmailMultiAlternatives.send", side_effect=EmailRelayError("The mail relay did not answer properly.")):
            with self.assertLogs("accounts.customer_emails", level="WARNING"):
                response = self.send_test(to="france.test@gmail.com")
        self.assertEqual(response.status_code, 502)
        row = CustomerEmailLog.objects.get()
        self.assertEqual((row.kind, row.outcome), ("test", "failed"))
        self.assertIn("did not answer properly", row.reason)
