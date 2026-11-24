"""The staff side of online payments: the Payments page's "Online payments" tab, its two actions, the bell item, and -- most important -- that every
existing admin page still opens and says the right thing now that a booking can arrive CONFIRMED and PAID with no screenshot and no approval.
The online checkout itself is gowns/test_online_checkout.py."""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditLog, UserProfile
from gowns.models import Gown
from gowns.test_online_checkout import FakePayMongo
from reservations import paymongo
from reservations.models import Payment, PaymentAttempt, Reservation

User = get_user_model()


def table_of(html):
    """Only the Online payments table (the bell in the header lists the same customers)."""
    start = html.index('id="pm-online-table"')
    return html[start:html.index("</table>", start)]


def attempt(customer, **kw):
    values = dict(token=f"tok{PaymentAttempt.objects.count():030d}", customer=customer, customer_name="Ana Cruz", purpose="deposit", amount=Decimal("2000"),
                  expires_at=timezone.now() + timedelta(minutes=20))
    values.update(kw)
    return PaymentAttempt.objects.create(**values)


@override_settings(PAYMONGO_SECRET_KEY="sk_test_unit", PAYMONGO_WEBHOOK_SECRET="whsk_unit", USING_LAPTOP_DB=False, SITE_BASE_URL="")
class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("oa_owner", "owner@gmail.com", "x")
        cls.staff = User.objects.create_user("oa_staff", "staff@gmail.com", "x", is_staff=True, first_name="Maria", last_name="Cruz")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.STAFF})
        cls.allowed = User.objects.create_user("oa_allowed", "allowed@gmail.com", "x", is_staff=True)
        UserProfile.objects.update_or_create(user=cls.allowed, defaults={"role": UserProfile.Role.STAFF, "permissions": "void_receipts"})
        cls.ana = User.objects.create_user("oa_ana", "ana@gmail.com", "x", first_name="Ana")
        cls.gown = Gown.objects.create(gown_id="Wedding Gown-OA-001", name="Admin Online Gown", category=Gown.Category.WEDDING_GOWN, color_name="White",
                                       color_code="WH", size=Gown.Size.MEDIUM, rental_price=Decimal("3500"), status=Gown.Status.AVAILABLE)

    def setUp(self):
        self.fake = FakePayMongo()
        patcher = patch("reservations.paymongo._request", side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def booked(self, option="deposit", channel="gcash"):
        """A booking made the real way: through the online checkout, paid on the stand-in PayMongo."""
        from django.test import Client
        from accounts.test_support import agreed
        client = Client()
        client.force_login(self.ana)
        agreed(self.ana)
        event = timezone.localdate() + timedelta(days=25 + Reservation.objects.count() * 10)
        items = [{"gown_name": self.gown.name, "gown_slug": self.gown.slug, "size": "Medium", "rental_date": (event - timedelta(days=2)).isoformat(),
                  "return_date": (event + timedelta(days=2)).isoformat()}]
        response = client.post(reverse("gowns:pay_start"), data={"items": json.dumps(items), "pay_option": option, "first_name": "Ana", "last_name": "Cruz",
                                                                  "phone": "09171234567", "address": "1 Test St", "city": "Antipolo", "postal_code": "1870", "expected_deposit": "2000"})
        self.assertEqual(response.status_code, 200, response.content)
        token = response.json()["token"]
        found = PaymentAttempt.objects.get(token=token)
        self.fake.paid(found.session_id, channel=channel)
        self.assertEqual(client.get(reverse("gowns:payment_status", args=[token])).json()["state"], "booked")
        return Reservation.objects.get(payment_attempts=found)


class OnlineTabTests(Base):
    def test_every_staff_account_sees_the_tab_and_the_list_and_only_the_owner_sees_all_receipts(self):
        for user, sees_receipts in ((self.owner, True), (self.staff, False)):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                html = self.client.get(reverse("arabela_admin:payment_online")).content.decode()
                self.assertIn("Online payments", html)
                self.assertEqual(reverse("arabela_admin:payment_receipts") in html, sees_receipts or "All receipts" in html)
                self.assertIn('id="pm-online-table"', html)
        anonymous = self.client.logout() or self.client.get(reverse("arabela_admin:payment_online"))
        self.assertEqual(anonymous.status_code, 302)

    def test_nothing_needs_a_look_says_so_and_all_lists_everything(self):
        self.booked()
        self.client.force_login(self.owner)
        html = self.client.get(reverse("arabela_admin:payment_online")).content.decode()
        self.assertIn("Nothing needs a look", html)
        everything = self.client.get(reverse("arabela_admin:payment_online") + "?show=all").content.decode()
        self.assertIn("Ana Cruz", everything)
        self.assertIn("Booked", everything)
        self.assertIn("GCash", everything)
        self.assertIn("RSV-", everything)
        self.assertNotIn("Test mode", everything)                                       # the panel does not label practice payments

    def test_a_flagged_payment_is_listed_with_its_note_and_the_badge_counts_it(self):
        flagged = attempt(self.ana, status=PaymentAttempt.Status.FLAGGED, note="PayMongo says P1,000.00 was paid but P2,000.00 was asked.",
                          provider_payment_id="pay_x1", paid_amount=Decimal("1000"), paid_at=timezone.now(), channel="paymaya")
        self.client.force_login(self.owner)
        html = self.client.get(reverse("arabela_admin:payment_online")).content.decode()
        self.assertIn("Needs a look", html)
        self.assertIn("1,000.00 was paid", html)
        self.assertIn("pay_x1", html)
        self.assertIn("Maya", html)
        self.assertIn('class="pm-badge"', self.client.get(reverse("arabela_admin:payments")).content.decode())
        self.assertIn(f'data-id="{flagged.id}"', html)

    def test_the_search_finds_by_customer_booking_or_paymongo_number(self):
        attempt(self.ana, customer_name="Zed Finder", status=PaymentAttempt.Status.FLAGGED, provider_payment_id="pay_zed")
        self.client.force_login(self.owner)
        for q in ("Zed", "pay_zed"):
            self.assertIn("Zed Finder", table_of(self.client.get(reverse("arabela_admin:payment_online"), {"q": q}).content.decode()))
        self.assertNotIn("Zed Finder", table_of(self.client.get(reverse("arabela_admin:payment_online"), {"q": "nobody"}).content.decode()))

    def test_staff_without_the_permission_see_no_action_buttons_and_cannot_use_the_addresses(self):
        flagged = attempt(self.ana, status=PaymentAttempt.Status.FLAGGED, provider_payment_id="pay_a", paid_amount=Decimal("2000"), paid_at=timezone.now())
        self.client.force_login(self.staff)
        table = table_of(self.client.get(reverse("arabela_admin:payment_online")).content.decode())
        self.assertIn("pay_a", table)
        self.assertNotIn("data-online-action", table)
        refund = self.client.post(reverse("arabela_admin:payment_online_refund", args=[flagged.id]), data="{}", content_type="application/json")
        handled = self.client.post(reverse("arabela_admin:payment_online_handled", args=[flagged.id]), data=json.dumps({"note": "done by hand"}), content_type="application/json")
        self.assertEqual((refund.status_code, handled.status_code), (403, 403))
        flagged.refresh_from_db()
        self.assertEqual(flagged.status, PaymentAttempt.Status.FLAGGED)
        self.assertEqual(self.fake.refunds, {})

    def test_the_owner_and_an_allowed_staff_member_can_send_the_money_back(self):
        for user in (self.owner, self.allowed):
            with self.subTest(user=user.username):
                flagged = attempt(self.ana, status=PaymentAttempt.Status.FLAGGED, provider_payment_id=f"pay_{user.username}", paid_amount=Decimal("2000"),
                                  paid_at=timezone.now(), channel="gcash", note="The amount did not match.")
                self.client.force_login(user)
                self.assertIn("data-online-action=\"refund\"", self.client.get(reverse("arabela_admin:payment_online")).content.decode())
                response = self.client.post(reverse("arabela_admin:payment_online_refund", args=[flagged.id]), data=json.dumps({"reason": "wrong amount"}), content_type="application/json")
                self.assertEqual(response.status_code, 200, response.content)
                flagged.refresh_from_db()
                self.assertEqual((flagged.status, flagged.refund_status), (PaymentAttempt.Status.REFUNDED, "pending"))
                self.assertTrue(flagged.refund_id)
                self.assertFalse(flagged.needs_a_look)
        self.assertEqual(len(self.fake.refunds), 2)
        self.assertTrue(AuditLog.objects.filter(action="payment.online_refund").exists())

    def test_sending_it_back_twice_is_refused(self):
        flagged = attempt(self.ana, status=PaymentAttempt.Status.FLAGGED, provider_payment_id="pay_t", paid_amount=Decimal("2000"), paid_at=timezone.now())
        self.client.force_login(self.owner)
        url = reverse("arabela_admin:payment_online_refund", args=[flagged.id])
        self.assertEqual(self.client.post(url, data="{}", content_type="application/json").status_code, 200)
        self.assertEqual(self.client.post(url, data="{}", content_type="application/json").status_code, 409)
        self.assertEqual(len(self.fake.refunds), 1)

    def test_a_refund_paymongo_refuses_can_be_tried_again(self):
        flagged = attempt(self.ana, status=PaymentAttempt.Status.FLAGGED, provider_payment_id="pay_r", paid_amount=Decimal("2000"), paid_at=timezone.now())
        self.client.force_login(self.owner)
        url = reverse("arabela_admin:payment_online_refund", args=[flagged.id])
        self.fake.fail["create_refund"] = paymongo.PaymongoError("payment_not_refundable", status=400)
        self.assertEqual(self.client.post(url, data="{}", content_type="application/json").status_code, 502)
        flagged.refresh_from_db()
        self.assertEqual((flagged.status, flagged.refund_status), (PaymentAttempt.Status.FLAGGED, "failed"))
        del self.fake.fail["create_refund"]
        self.assertEqual(self.client.post(url, data="{}", content_type="application/json").status_code, 200)

    def test_a_payment_with_no_paymongo_number_cannot_be_sent_back(self):
        flagged = attempt(self.ana, status=PaymentAttempt.Status.FLAGGED)
        self.client.force_login(self.owner)
        html = self.client.get(reverse("arabela_admin:payment_online")).content.decode()
        self.assertNotIn('data-online-action="refund"', html)
        self.assertIn('data-online-action="handled"', html)
        self.assertEqual(self.client.post(reverse("arabela_admin:payment_online_refund", args=[flagged.id]), data="{}", content_type="application/json").status_code, 409)

    def test_marking_it_handled_needs_words_closes_it_and_is_logged(self):
        flagged = attempt(self.ana, status=PaymentAttempt.Status.FLAGGED, provider_payment_id="pay_h", paid_amount=Decimal("2000"), paid_at=timezone.now())
        self.client.force_login(self.owner)
        url = reverse("arabela_admin:payment_online_handled", args=[flagged.id])
        self.assertEqual(self.client.post(url, data=json.dumps({"note": " "}), content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post(url, data=json.dumps({"note": "Refunded in PayMongo's dashboard"}), content_type="application/json").status_code, 200)
        flagged.refresh_from_db()
        self.assertEqual(flagged.status, PaymentAttempt.Status.HANDLED)
        self.assertIn("Refunded in PayMongo's dashboard", flagged.note)
        self.assertFalse(flagged.needs_a_look)
        self.assertEqual(self.client.post(url, data=json.dumps({"note": "again please"}), content_type="application/json").status_code, 409)     # no longer needs a look
        log = AuditLog.objects.get(action="payment.online_handled")
        self.assertIn("marked the online payment", log.summary)

    def test_an_unknown_attempt_is_a_404_not_a_crash(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(reverse("arabela_admin:payment_online_refund", args=[99999]), data="{}", content_type="application/json").status_code, 404)

    def test_the_attention_rule_in_python_and_in_the_database_agree(self):
        now = timezone.now()
        cases = [
            attempt(self.ana, status=PaymentAttempt.Status.CREATED),
            attempt(self.ana, status=PaymentAttempt.Status.PAID, paid_at=now),
            attempt(self.ana, status=PaymentAttempt.Status.PAID, paid_at=now - timedelta(minutes=10)),
            attempt(self.ana, status=PaymentAttempt.Status.BOOKED),
            attempt(self.ana, status=PaymentAttempt.Status.FLAGGED),
            attempt(self.ana, status=PaymentAttempt.Status.REFUNDED, refund_status="pending"),
            attempt(self.ana, status=PaymentAttempt.Status.REFUNDED, refund_status="failed"),
            attempt(self.ana, status=PaymentAttempt.Status.HANDLED),
            attempt(self.ana, status=PaymentAttempt.Status.EXPIRED),
        ]
        in_db = set(PaymentAttempt.objects.filter(PaymentAttempt.attention_filter(now)).values_list("pk", flat=True))
        in_python = {a.pk for a in cases if a.needs_a_look}
        self.assertEqual(in_db, in_python)
        self.assertEqual(len(in_python), 3)


class BellTests(Base):
    def test_a_payment_that_needs_a_look_rings_the_bell_until_someone_deals_with_it(self):
        from arabela_admin import notifications
        flagged = attempt(self.ana, status=PaymentAttempt.Status.FLAGGED, paid_amount=Decimal("2000"), paid_at=timezone.now())
        items = [i for i in notifications.build_items(self.owner) if i["key"] == f"online:{flagged.id}"]
        self.assertEqual(len(items), 1)
        self.assertIn("Ana Cruz", items[0]["title"])
        self.assertIn("2,000.00", items[0]["title"])
        self.assertEqual(items[0]["url"], reverse("arabela_admin:payment_online") + "?show=attention")
        PaymentAttempt.objects.filter(pk=flagged.pk).update(status=PaymentAttempt.Status.HANDLED)
        self.assertFalse([i for i in notifications.build_items(self.owner) if i["key"] == f"online:{flagged.id}"])

    def test_a_booked_payment_never_rings_it(self):
        from arabela_admin import notifications
        self.booked()
        self.assertFalse([i for i in notifications.build_items(self.owner) if i["key"].startswith("online:")])


class EveryExistingPageStillWorksTests(Base):
    """A booking now arrives Confirmed and paid with no screenshot, no approval and no owner check. Every admin page that lists bookings must still
    open and tell the truth about it."""
    PAGES = ("dashboard", "payment_verification", "pending_approval", "security_deposits", "rental_history", "active_reservations", "reservation_records",
             "payments", "payment_online", "calendar", "clients", "rental_schedule")

    def test_the_pages_open_with_a_deposit_only_booking_and_a_paid_in_full_booking(self):
        deposit_only = self.booked("deposit")
        in_full = self.booked("full", channel="qrph")
        self.client.force_login(self.owner)
        for name in self.PAGES:
            with self.subTest(page=name):
                response = self.client.get(reverse(f"arabela_admin:{name}"))
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("{%", response.content.decode())
        self.assertNotEqual(deposit_only.pk, in_full.pk)

    def test_the_payment_screens_say_what_was_really_paid(self):
        deposit_only = self.booked("deposit")
        in_full = self.booked("full", channel="qrph")
        self.assertEqual(deposit_only.payment_breakdown["headline"], "₱2,000")
        self.assertIn("Deposit paid online (GCash)", deposit_only.payment_breakdown["paidLabel"])
        self.assertEqual(deposit_only.payment_breakdown["lines"][1]["note"], "to pay at the shop")
        self.assertEqual(in_full.payment_breakdown["headline"], "₱5,500")
        self.assertIn("Paid in full online (QR Ph)", in_full.payment_breakdown["paidLabel"])
        self.assertEqual(in_full.payment_breakdown["lines"][1]["note"], "paid online (QR Ph)")

    def test_the_rest_of_the_rental_paid_at_the_shop_after_a_deposit_online_reads_as_paid_in_full(self):
        from reservations import payments
        booking = self.booked("deposit")
        payments.record_payment(reservation_id=booking.pk, purpose=Payment.Purpose.BALANCE, amount=Decimal("3500"), method=Payment.Method.CASH, user=self.owner)
        booking = Reservation.objects.get(pk=booking.pk)
        self.assertEqual(booking.payment_breakdown["headline"], "₱5,500")
        self.assertIn("deposit online, rental at the shop", booking.payment_breakdown["paidLabel"])

    def test_a_paid_booking_is_not_in_pending_approval_or_waiting_for_the_owner(self):
        booking = self.booked()
        self.client.force_login(self.owner)
        html = self.client.get(reverse("arabela_admin:pending_approval")).content.decode()
        self.assertNotIn(booking.reference_code, html)
        self.assertEqual(booking.status, Reservation.Status.CONFIRMED)
        self.assertFalse(booking.waiting_for_owner)
        self.assertEqual(booking.owner_check, "")
        self.assertEqual(booking.payment_state, "Payment Verified")

    def test_the_booking_shows_on_active_reservations_and_the_payments_page_with_its_receipt(self):
        booking = self.booked()
        self.client.force_login(self.owner)
        self.assertIn(booking.reference_code, self.client.get(reverse("arabela_admin:active_reservations")).content.decode())
        self.assertIn(booking.reference_code, self.client.get(reverse("arabela_admin:payments")).content.decode())
        detail = self.client.get(reverse("arabela_admin:payment_detail", args=[booking.pk])).json()
        online = [p for p in detail["payments"] if p["method"].startswith("GCash")]
        self.assertTrue(online)
        self.assertRegex(online[0]["receiptNumber"], r"^PR-\d{4}-\d{6}$")
        receipts = self.client.get(reverse("arabela_admin:payment_receipts")).content.decode()
        self.assertIn("GCash (online payment)", receipts)

    def test_the_owner_check_and_the_approve_button_never_apply_to_an_online_paid_booking(self):
        booking = self.booked()
        self.client.force_login(self.owner)
        response = self.client.post(reverse("arabela_admin:reservation_approve", args=[booking.pk]), data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 409)                        # already confirmed: nothing to approve, nothing is changed
        self.assertEqual(Payment.objects.filter(reservation=booking).count(), 1)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Reservation.Status.CONFIRMED)

    def test_the_bell_query_count_stays_small_with_online_payments_in_the_system(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        def count():
            self.client.force_login(self.owner)
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(reverse("arabela_admin:admin_notifications_feed")).status_code, 200)
            return len(queries)

        self.booked()
        attempt(self.ana, status=PaymentAttempt.Status.FLAGGED)
        few = count()
        for _ in range(3):
            self.booked()
            attempt(self.ana, status=PaymentAttempt.Status.FLAGGED)
        self.assertEqual(count(), few)                  # more online payments cost no more queries
