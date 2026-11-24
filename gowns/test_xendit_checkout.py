"""The online checkout through XENDIT (gowns/online_checkout.py through reservations/gateway.py), driven through the REAL views with a stand-in Xendit.

The stand-in (`FakeXendit`) replaces the one HTTP function of reservations/xendit.py, so nothing reaches the network, and answers with the shapes Xendit's test
mode really sent (checked with real calls on 2026-11-24). What these tests prove, the same as the PayMongo tests prove for PayMongo: nothing is booked until Xendit
says the money is in; the booking is confirmed with its receipt in one piece; the same payment can never book twice; a gown taken while the customer paid sends the
whole payment back; a payment that is wrong is never booked; a webhook is believed only with the shop's callback token and is only a prompt; and QR Ph, which Xendit
cannot refund, is never offered an automatic refund."""
import json
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomerMessage, UserProfile
from accounts.test_support import agreed
from gowns import online_checkout
from gowns.models import Gown
from reservations import gateway, payments, xendit
from reservations.models import Payment, PaymentAttempt, Reservation, ReservationItem

User = get_user_model()
KEY = "xnd_development_unit"
TOKEN = "callback_token_unit"
SETTINGS = dict(XENDIT_SECRET_KEY=KEY, XENDIT_WEBHOOK_TOKEN=TOKEN, PAYMENT_PROVIDER="xendit", USING_LAPTOP_DB=False, SITE_BASE_URL="")


class FakeXendit:
    """What Xendit does, in memory. `paid(session_id)` is the customer pressing Proceed to Pay on Xendit's test page."""

    def __init__(self):
        self.sessions, self.payments, self.refunds, self.calls, self.fail = {}, {}, {}, [], {}
        self.n = 0

    def __call__(self, method, path, body=None, headers=None):
        self.calls.append((method, path, headers or {}))
        name = self._name(method, path)
        if name in self.fail:
            raise self.fail[name]
        if name == "create_session":
            self.n += 1
            sid = f"ps-{self.n:024d}"
            self.sessions[sid] = {"body": body, "status": "ACTIVE", "payment_id": ""}
            return self._session(sid)
        if name == "read_session":
            return self._session(path.rsplit("/", 1)[-1])
        if name == "cancel_session":
            sid = path.split("/")[-2]
            if self.sessions[sid]["status"] == "COMPLETED":
                raise xendit.XenditError("The session is already completed", status=400)
            self.sessions[sid]["status"] = "CANCELED"
            return self._session(sid)
        if name == "read_payment":
            return self.payments[path.rsplit("/", 1)[-1]]
        if name == "create_refund":
            rid = f"rfd-{len(self.refunds) + 1:04d}"
            self.refunds[rid] = body
            return {"refund_id": rid, "reference_id": body["reference_id"], "payment_id": body["payment_id"], "status": "PENDING", "amount": body["amount"],
                    "currency": "PHP", "channel_code": "GCASH", "reason": body["reason"]}
        raise AssertionError(f"unexpected Xendit call {method} {path}")

    @staticmethod
    def _name(method, path):
        if method == "POST" and path == "/sessions":
            return "create_session"
        if method == "GET" and path.startswith("/sessions/"):
            return "read_session"
        if method == "POST" and path.endswith("/cancel"):
            return "cancel_session"
        if method == "GET" and path.startswith("/v3/payments/"):
            return "read_payment"
        if method == "POST" and path == "/v3/refunds":
            return "create_refund"
        return "other"

    def _session(self, sid):
        s = self.sessions[sid]
        out = {"payment_session_id": sid, "status": s["status"], "reference_id": s["body"]["reference_id"], "currency": "PHP", "amount": s["body"]["amount"],
               "country": "PH", "mode": "PAYMENT_LINK", "session_type": "PAY", "metadata": s["body"].get("metadata", {}),
               "payment_link_url": f"https://checkout-staging.xendit.co/session/session-{sid[3:]}"}
        if s["payment_id"]:
            out["payment_id"] = s["payment_id"]
            out["payment_request_id"] = "pr-" + s["payment_id"][3:]
        return out

    def paid(self, sid, *, channel="GCASH", amount=None, status="SUCCEEDED", pay_id=None):
        s = self.sessions[sid]
        pay_id = pay_id or f"py-{sid[3:]}"
        amount = s["body"]["amount"] if amount is None else amount
        s["status"], s["payment_id"] = "COMPLETED", pay_id
        self.payments[pay_id] = {"payment_id": pay_id, "reference_id": s["body"]["reference_id"], "status": status, "channel_code": channel, "request_amount": amount,
                                 "currency": "PHP", "captures": [{"capture_id": "cptr-1", "capture_timestamp": timezone.now().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                                                  "capture_amount": amount}] if status == "SUCCEEDED" else []}
        return pay_id

    def count(self, name):
        return sum(1 for method, path, _ in self.calls if self._name(method, path) == name)


@override_settings(**SETTINGS)
class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ana = User.objects.create_user("xo_ana", "xo.ana@gmail.com", "x", first_name="Ana")
        cls.ben = User.objects.create_user("xo_ben", "xo.ben@gmail.com", "x", first_name="Ben")
        UserProfile.objects.update_or_create(user=cls.ana, defaults={"display_name": "Ana Cruz"})
        cls.gown = Gown.objects.create(gown_id="Wedding Gown-XO-001", name="Xendit Gown One", category=Gown.Category.WEDDING_GOWN, color_name="White",
                                       color_code="WH", size=Gown.Size.MEDIUM, rental_price=Decimal("3500"), status=Gown.Status.AVAILABLE)
        cls.gown2 = Gown.objects.create(gown_id="Wedding Gown-XO-002", name="Xendit Gown Two", category=Gown.Category.WEDDING_GOWN, color_name="Ivory",
                                        color_code="IV", size=Gown.Size.MEDIUM, rental_price=Decimal("2500"), status=Gown.Status.AVAILABLE)

    def setUp(self):
        self.fake = FakeXendit()
        patcher = patch("reservations.xendit._request", side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.event = timezone.localdate() + timedelta(days=20)
        self.client = Client()
        self.client.force_login(self.ana)
        agreed(self.ana)

    def item(self, gown=None):
        gown = gown or self.gown
        return {"gown_name": gown.name, "gown_slug": gown.slug, "size": "Medium", "rental_date": (self.event - timedelta(days=2)).isoformat(),
                "return_date": (self.event + timedelta(days=2)).isoformat()}

    def ben_client(self):
        """Ben, signed in and past the Terms pop-up (a second customer)."""
        client = Client()
        client.force_login(self.ben)
        agreed(self.ben)
        return client

    def start(self, items=None, *, option="deposit", client=None, **extra):
        items = [self.item()] if items is None else items
        data = {"items": json.dumps(items), "pay_option": option, "first_name": "Ana", "last_name": "Cruz", "phone": "09171234567",
                "address": "1 Test St", "city": "Antipolo", "postal_code": "1870", "expected_deposit": str(2000 * len(items))}
        data.update(extra)
        return (client or self.client).post(reverse("gowns:pay_start"), data=data)

    def started(self, **kwargs):
        response = self.start(**kwargs)
        self.assertEqual(response.status_code, 200, response.content)
        return PaymentAttempt.objects.get(token=response.json()["token"]), response.json()

    def pay_and_confirm(self, attempt, client=None, **kwargs):
        self.fake.paid(attempt.session_id, **kwargs)
        PaymentAttempt.objects.filter(pk=attempt.pk).update(last_checked_at=None)
        response = (client or self.client).get(reverse("gowns:payment_status", args=[attempt.token]))
        self.assertEqual(response.status_code, 200)
        attempt.refresh_from_db()
        return response.json()


class BackButtonTests(Base):
    """The Back button, a reloaded checkout, a second tab and Pay pressed again must never leave two payable pages, double a payment or lose one."""

    def open_attempts(self):
        return PaymentAttempt.objects.filter(customer=self.ana, status=PaymentAttempt.Status.CREATED).count()

    def test_pay_pressed_again_with_nothing_changed_is_the_same_open_page(self):
        first, body1 = self.started(request_id="req-back-0001")
        again, body2 = self.started(request_id="req-back-0001")
        self.assertEqual((again.pk, body2["checkout_url"]), (first.pk, body1["checkout_url"]))
        self.assertEqual((PaymentAttempt.objects.count(), self.fake.count("create_session")), (1, 1))

    def test_a_freshly_loaded_checkout_closes_the_page_left_open_behind_the_back_button(self):
        first, _ = self.started(request_id="req-back-0002")                          # the customer went to the payment page and came Back...
        second, _ = self.started(request_id="req-back-0003")                         # ...the checkout was reloaded (a new id), and Pay pressed again
        first.refresh_from_db()
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual((first.status, second.status), (PaymentAttempt.Status.CANCELLED, PaymentAttempt.Status.CREATED))
        self.assertEqual(self.fake.sessions[first.session_id]["status"], "CANCELED")        # the old page can no longer be paid
        self.assertEqual(self.open_attempts(), 1)

    def test_a_second_tab_pressing_pay_also_leaves_only_one_open_page(self):
        self.started(request_id="req-back-0004")
        self.started(request_id="req-back-0005", option="full")
        self.started(request_id="req-back-0006")
        self.assertEqual(self.open_attempts(), 1)

    def test_pay_pressed_again_after_the_page_ran_out_opens_a_new_one_not_the_dead_one(self):
        first, body1 = self.started(request_id="req-back-0007")
        PaymentAttempt.objects.filter(pk=first.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        again, body2 = self.started(request_id="req-back-0007")
        first.refresh_from_db()
        self.assertNotEqual(again.pk, first.pk)
        self.assertNotEqual(body2["checkout_url"], body1["checkout_url"])
        self.assertEqual((first.status, again.status), (PaymentAttempt.Status.CANCELLED, PaymentAttempt.Status.CREATED))

    def test_pay_pressed_again_after_the_customer_came_back_cancelled_opens_a_new_page(self):
        first, _ = self.started(request_id="req-back-0008")
        self.client.get(reverse("gowns:payment_cancel", args=[first.token]))                  # the payment page's "back to the shop" link
        first.refresh_from_db()
        self.assertEqual(first.status, PaymentAttempt.Status.CANCELLED)
        again, body = self.started(request_id="req-back-0008")                               # a page restored by Back still has the same id
        self.assertNotEqual(again.pk, first.pk)
        self.assertTrue(body["checkout_url"].startswith("https://checkout-staging.xendit.co/session/"))

    def test_a_restored_page_with_a_changed_choice_never_doubles_a_booked_payment(self):
        first, _ = self.started(request_id="req-back-0009")
        self.assertEqual(self.pay_and_confirm(first)["state"], "booked")
        body = self.start(request_id="req-back-0009", option="full").json()                  # Back to the old checkout, choice switched, Pay
        self.assertTrue(body["success"])
        self.assertEqual(body["checkout_url"], reverse("gowns:payment_return", args=[first.token]))
        self.assertEqual((PaymentAttempt.objects.count(), Reservation.objects.count(), Payment.objects.count()), (1, 1, 1))

    def test_if_the_old_page_was_paid_in_the_meantime_the_customer_is_shown_it_not_a_second_payment(self):
        first, _ = self.started(request_id="req-back-0010")
        self.fake.paid(first.session_id)                                                     # paid on the old page, the return page never opened
        body = self.start(request_id="req-back-0011").json()                                 # a fresh checkout, Pay pressed
        self.assertTrue(body["success"])
        self.assertEqual(body["checkout_url"], reverse("gowns:payment_return", args=[first.token]))
        first.refresh_from_db()
        self.assertEqual(first.status, PaymentAttempt.Status.BOOKED)
        self.assertEqual((PaymentAttempt.objects.count(), Reservation.objects.count(), self.fake.count("create_session")), (1, 1, 1))

    def test_a_payment_that_is_being_turned_into_a_booking_blocks_a_second_one(self):
        first, _ = self.started(request_id="req-back-0012")
        with patch("gowns.online_checkout._create_booking", side_effect=RuntimeError("boom")):
            self.pay_and_confirm(first)
        first.refresh_from_db()
        self.assertEqual(first.status, PaymentAttempt.Status.PAID)
        body = self.start(items=[self.item(self.gown2)], request_id="req-back-0013").json()
        self.assertEqual(body["checkout_url"], reverse("gowns:payment_return", args=[first.token]))
        self.assertEqual(PaymentAttempt.objects.count(), 1)

    def test_another_customers_open_page_is_never_touched(self):
        ben_client = self.ben_client()
        theirs, _ = self.started(items=[self.item(self.gown2)], request_id="req-back-0014", client=ben_client)
        self.started(request_id="req-back-0015")
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, PaymentAttempt.Status.CREATED)

    def test_spamming_pay_never_opens_more_than_one_page(self):
        for _ in range(6):
            self.start(request_id="req-back-0016")
        self.assertEqual((PaymentAttempt.objects.count(), self.fake.count("create_session")), (1, 1))

    def test_an_old_page_that_cannot_be_reached_does_not_stop_the_new_one_from_opening(self):
        first, _ = self.started(request_id="req-back-0017")
        self.fake.fail["read_session"] = xendit.XenditError("down", status=503)
        try:
            self.start(request_id="req-back-0018")
        finally:
            self.fake.fail.pop("read_session", None)
        first.refresh_from_db()
        self.assertIn(first.status, (PaymentAttempt.Status.CREATED, PaymentAttempt.Status.CANCELLED))


class HoldBarAfterPayingTests(Base):
    """The "Your selection is held" bar goes as soon as the customer's money is in, wherever they are on the site."""

    def hold(self, minutes_left=15):
        from gowns.context_processors import RESERVATION_HOLD_SESSION_KEY
        session = self.client.session
        session[RESERVATION_HOLD_SESSION_KEY] = (timezone.now() + timedelta(minutes=minutes_left)).isoformat()
        session.save()
        return RESERVATION_HOLD_SESSION_KEY

    def page(self):
        return self.client.get(reverse("gowns:homepage")).content.decode()

    def test_the_bar_shows_while_nothing_is_paid(self):
        self.hold()
        self.started()
        self.assertIn('id="hold-banner"', self.page())

    def test_a_customer_who_paid_and_never_came_back_to_the_return_page_sees_no_bar(self):
        key = self.hold()
        attempt, _ = self.started()
        self.fake.paid(attempt.session_id)
        online_checkout.confirm_attempt(attempt.pk, source="webhook")                        # the webhook books it; the browser never returned
        html = self.page()
        self.assertNotIn('id="hold-banner"', html)
        self.assertNotIn(key, self.client.session)
        self.assertEqual(UserProfile.objects.get(user=self.ana).hold_abandon_count, 0)

    def test_a_payment_the_shop_has_to_check_also_ends_the_bar(self):
        key = self.hold()
        attempt, _ = self.started()
        self.fake.paid(attempt.session_id, amount=1000)                                      # the wrong amount: flagged for a person
        online_checkout.confirm_attempt(attempt.pk, source="webhook")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.FLAGGED)
        self.assertNotIn('id="hold-banner"', self.page())
        self.assertNotIn(key, self.client.session)

    def test_the_status_poll_ends_the_hold_for_a_flagged_payment_too(self):
        key = self.hold()
        attempt, _ = self.started()
        self.fake.paid(attempt.session_id, amount=1000)
        PaymentAttempt.objects.filter(pk=attempt.pk).update(last_checked_at=None)
        state = self.client.get(reverse("gowns:payment_status", args=[attempt.token])).json()
        self.assertEqual(state["state"], "flagged")
        self.assertNotIn(key, self.client.session)

    def test_an_older_payment_does_not_end_a_hold_that_began_after_it(self):
        attempt, _ = self.started()
        self.pay_and_confirm(attempt)
        PaymentAttempt.objects.filter(pk=attempt.pk).update(paid_at=timezone.now() - timedelta(minutes=40))
        self.hold(minutes_left=15)                                                           # a new selection, held after that payment
        self.assertIn('id="hold-banner"', self.page())

    def test_someone_who_never_paid_keeps_the_bar_after_closing_a_page(self):
        self.hold()
        first, _ = self.started()
        self.client.get(reverse("gowns:payment_cancel", args=[first.token]))
        self.assertIn('id="hold-banner"', self.page())


class SwitchTests(TestCase):
    """Which service is used."""

    def test_the_chosen_service_wins_else_xendit_when_its_key_is_set_else_paymongo(self):
        with override_settings(PAYMENT_PROVIDER="", XENDIT_SECRET_KEY="", PAYMONGO_SECRET_KEY=""):
            self.assertEqual(gateway.provider(), "paymongo")
            self.assertFalse(gateway.enabled())                                       # no key at all: online payment is off, the older checkout works
        with override_settings(PAYMENT_PROVIDER="", XENDIT_SECRET_KEY=KEY, PAYMONGO_SECRET_KEY="sk_test_x"):
            self.assertEqual((gateway.provider(), gateway.enabled(), gateway.is_test(), gateway.label()), ("xendit", True, True, "Xendit"))
        with override_settings(PAYMENT_PROVIDER="paymongo", XENDIT_SECRET_KEY=KEY, PAYMONGO_SECRET_KEY="sk_test_x", USING_LAPTOP_DB=False):
            self.assertEqual((gateway.provider(), gateway.label()), ("paymongo", "PayMongo"))
        with override_settings(PAYMENT_PROVIDER="nonsense", XENDIT_SECRET_KEY=KEY):
            self.assertEqual(gateway.provider(), "xendit")                           # an unknown name is ignored, never trusted

    def test_a_key_that_is_not_a_xendit_secret_key_switches_nothing_on(self):
        for bad in ("sk_test_x", "FYWFG6JEKJRVWMKAFYXCKI3TGA5XSV3P", "xnd_public_development_abc", "  "):
            with self.subTest(key=bad), override_settings(PAYMENT_PROVIDER="xendit", XENDIT_SECRET_KEY=bad):
                self.assertFalse(gateway.enabled())

    def test_a_live_key_is_never_used_on_a_laptop_copy_of_the_database(self):
        with override_settings(PAYMENT_PROVIDER="xendit", XENDIT_SECRET_KEY="xnd_production_abc", USING_LAPTOP_DB=True):
            self.assertFalse(gateway.enabled())
        with override_settings(PAYMENT_PROVIDER="xendit", XENDIT_SECRET_KEY="xnd_production_abc", USING_LAPTOP_DB=False):
            self.assertEqual((gateway.enabled(), gateway.mode(), gateway.is_test()), (True, "live", False))

    def test_xendit_has_no_practice_gateway_and_the_error_types_are_both_caught(self):
        with override_settings(PAYMENT_PROVIDER="xendit", XENDIT_SECRET_KEY=KEY):
            self.assertFalse(gateway.is_demo())
        self.assertTrue(issubclass(xendit.XenditError, gateway.GatewayError) or xendit.XenditError in gateway.GatewayError)

    def test_the_largest_online_payment_is_the_lowest_limit_among_the_ways_to_pay(self):
        with override_settings(PAYMENT_PROVIDER="xendit", XENDIT_SECRET_KEY=KEY):
            self.assertEqual(gateway.max_amount(), 50000)                             # QR Ph tops out at 50,000
        with override_settings(PAYMENT_PROVIDER="paymongo"):
            self.assertEqual(gateway.max_amount(), 100000)


class StartTests(Base):
    def test_the_deposit_choice_opens_a_session_for_exactly_the_deposit_and_books_nothing(self):
        attempt, body = self.started()
        self.assertTrue(body["checkout_url"].startswith("https://checkout-staging.xendit.co/session/"))
        self.assertEqual((attempt.purpose, attempt.amount, attempt.status, attempt.livemode), ("deposit", Decimal("2000.00"), PaymentAttempt.Status.CREATED, False))
        self.assertTrue(attempt.session_id.startswith("ps-"))
        sent = self.fake.sessions[attempt.session_id]["body"]
        self.assertEqual((sent["amount"], sent["currency"], sent["country"], sent["session_type"], sent["mode"]), (2000, "PHP", "PH", "PAY", "PAYMENT_LINK"))
        self.assertEqual(sent["allowed_payment_channels"], ["GCASH", "PAYMAYA", "QRPH"])
        self.assertEqual(sent["reference_id"], attempt.token)
        self.assertTrue(sent["success_return_url"].endswith(reverse("gowns:payment_return", args=[attempt.token])))
        self.assertTrue(sent["cancel_return_url"].endswith(reverse("gowns:payment_cancel", args=[attempt.token])))
        self.assertFalse(Reservation.objects.exists())

    def test_the_page_closes_by_itself_when_the_customers_hold_ends(self):
        attempt, _ = self.started()
        sent = self.fake.sessions[attempt.session_id]["body"]
        expires = datetime.fromisoformat(sent["expires_at"].replace("Z", "+00:00"))
        minutes = (expires - timezone.now()).total_seconds() / 60
        self.assertTrue(9 < minutes <= 61, minutes)

    def test_the_customers_name_and_email_are_not_sent_to_xendit(self):
        attempt, _ = self.started()
        text = json.dumps(self.fake.sessions[attempt.session_id]["body"])
        for private in ("Ana", "Cruz", "xo.ana@gmail.com", "09171234567", "1 Test St"):
            self.assertNotIn(private, text)

    def test_the_full_choice_is_the_rental_plus_the_deposit_in_pesos(self):
        attempt, _ = self.started(option="full")
        self.assertEqual((attempt.amount, self.fake.sessions[attempt.session_id]["body"]["amount"]), (Decimal("5500.00"), 5500))

    def test_a_booking_over_the_largest_online_payment_is_refused_and_nothing_is_opened(self):
        with patch("reservations.gateway.MAX_AMOUNT", {"xendit": 5000, "paymongo": 100000}):
            too_big = self.start(items=[self.item(self.gown)], option="full")                 # 3,500 + 2,000 = 5,500 > 5,000
        self.assertEqual(too_big.status_code, 400)
        self.assertIn("too large to pay online", too_big.json()["error"])
        self.assertEqual((PaymentAttempt.objects.count(), self.fake.calls), (0, []))

    def test_xendit_refusing_to_open_a_session_charges_nothing_and_says_so(self):
        self.fake.fail["create_session"] = xendit.XenditError("down", retryable=True)
        response = self.start()
        self.assertEqual(response.status_code, 502)
        self.assertIn("Nothing was charged", response.json()["error"])
        attempt = PaymentAttempt.objects.get()
        self.assertEqual((attempt.status, attempt.note), (PaymentAttempt.Status.EXPIRED, "Xendit would not open a checkout."))


class ConfirmTests(Base):
    def test_a_paid_deposit_makes_a_confirmed_booking_with_its_receipt_and_the_xendit_provider(self):
        attempt, _ = self.started()
        self.assertEqual(self.pay_and_confirm(attempt, channel="GCASH")["state"], "booked")
        attempt.refresh_from_db()
        self.assertEqual((attempt.status, attempt.channel, attempt.paid_amount), (PaymentAttempt.Status.BOOKED, "gcash", Decimal("2000.00")))
        self.assertTrue(attempt.provider_payment_id.startswith("py-"))
        reservation = Reservation.objects.get()
        self.assertEqual((reservation.status, reservation.customer_id), (Reservation.Status.CONFIRMED, self.ana.pk))
        payment = reservation.payments.get()
        self.assertEqual((payment.provider, payment.method, payment.provider_ref, payment.amount), ("xendit", Payment.Method.PM_GCASH, attempt.provider_payment_id, Decimal("2000.00")))
        self.assertTrue(payment.receipt_number.startswith("PR-"))
        self.assertIn("Xendit", payment.note)
        self.assertIn("TEST PAYMENT", payment.note)
        self.assertEqual(payment.snapshot["method"], "GCash (online payment)")

    def test_each_way_of_paying_is_named_as_the_shops_pages_know_it(self):
        for channel, method in (("GCASH", Payment.Method.PM_GCASH), ("PAYMAYA", Payment.Method.PM_MAYA), ("QRPH", Payment.Method.PM_QRPH)):
            with self.subTest(channel=channel):
                Reservation.objects.all().delete()
                PaymentAttempt.objects.all().delete()
                attempt, _ = self.started()
                self.pay_and_confirm(attempt, channel=channel)
                self.assertEqual(Payment.objects.get().method, method)

    def test_an_unpaid_session_stays_waiting_and_books_nothing(self):
        attempt, _ = self.started()
        PaymentAttempt.objects.filter(pk=attempt.pk).update(last_checked_at=None)
        self.assertEqual(self.client.get(reverse("gowns:payment_status", args=[attempt.token])).json()["state"], "waiting")
        self.assertFalse(Reservation.objects.exists())

    def test_a_completed_session_whose_payment_did_not_succeed_is_not_booked(self):
        attempt, _ = self.started()
        self.fake.paid(attempt.session_id, status="FAILED")
        PaymentAttempt.objects.filter(pk=attempt.pk).update(last_checked_at=None)
        self.assertEqual(self.client.get(reverse("gowns:payment_status", args=[attempt.token])).json()["state"], "waiting")
        self.assertFalse(Reservation.objects.exists())

    def test_xendit_being_unreachable_is_waiting_not_a_failure(self):
        attempt, _ = self.started()
        self.fake.fail["read_session"] = xendit.XenditError("down", retryable=True)
        PaymentAttempt.objects.filter(pk=attempt.pk).update(last_checked_at=None)
        self.assertEqual(self.client.get(reverse("gowns:payment_status", args=[attempt.token])).json()["state"], "waiting")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.CREATED)

    def test_a_session_xendit_says_is_expired_or_cancelled_ends_the_attempt(self):
        for status in ("EXPIRED", "CANCELED"):
            with self.subTest(status=status):
                PaymentAttempt.objects.all().delete()
                attempt, _ = self.started()
                self.fake.sessions[attempt.session_id]["status"] = status
                PaymentAttempt.objects.filter(pk=attempt.pk).update(last_checked_at=None)
                self.assertEqual(self.client.get(reverse("gowns:payment_status", args=[attempt.token])).json()["state"], "ended")

    def test_a_payment_of_the_wrong_amount_is_flagged_and_never_booked(self):
        attempt, _ = self.started()
        state = self.pay_and_confirm(attempt, amount=1000)
        self.assertEqual(state["state"], "flagged")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.FLAGGED)
        self.assertIn("Xendit says", attempt.note)
        self.assertFalse(Reservation.objects.exists())

    def test_a_session_with_another_reference_is_flagged(self):
        attempt, _ = self.started()
        self.fake.sessions[attempt.session_id]["body"]["reference_id"] = "someone-elses-reference"
        self.assertEqual(self.pay_and_confirm(attempt)["state"], "flagged")
        self.assertFalse(Reservation.objects.exists())

    def test_asking_twice_books_once(self):
        attempt, _ = self.started()
        self.pay_and_confirm(attempt)
        online_checkout.confirm_attempt(attempt.pk, source="webhook")
        online_checkout.confirm_attempt(attempt.pk, source="sweep")
        self.assertEqual((Reservation.objects.count(), Payment.objects.count()), (1, 1))

    def test_the_booking_timeline_names_xendit(self):
        attempt, _ = self.started()
        self.pay_and_confirm(attempt)
        text = " ".join(e.detail for e in Reservation.objects.get().status_events.all())
        self.assertIn("on Xendit", text)
        self.assertNotIn("PayMongo", text)


class GownTakenWhilePayingTests(Base):
    def other(self):
        client = Client()
        client.force_login(self.ben)
        agreed(self.ben)
        return client

    def test_the_whole_payment_goes_back_through_xendit_and_the_customer_is_told(self):
        ana, _ = self.started(option="full")
        ben_client = self.other()
        ben, _ = self.started(client=ben_client)
        self.assertEqual(self.pay_and_confirm(ben, client=ben_client)["state"], "booked")
        state = self.pay_and_confirm(ana, channel="PAYMAYA")
        self.assertEqual(state["state"], "refunded")
        ana.refresh_from_db()
        self.assertEqual((ana.status, ana.refund_status, ana.refund_amount), (PaymentAttempt.Status.REFUNDED, "pending", Decimal("5500.00")))
        refund = list(self.fake.refunds.values())[0]
        self.assertEqual((refund["payment_id"], refund["amount"], refund["reason"]), (ana.provider_payment_id, 5500, "OTHERS"))
        self.assertTrue(refund["reference_id"].startswith("rf-"))
        self.assertEqual(Reservation.objects.count(), 1)
        self.assertIn("being sent back to your Maya", CustomerMessage.objects.get(recipient=self.ana, category="Payment").body)
        sent_headers = [h for m, p, h in self.fake.calls if p == "/v3/refunds"][0]
        self.assertEqual(sent_headers.get("api-version"), xendit.API_VERSION)

    def test_asking_again_never_sends_the_money_back_twice(self):
        ana, _ = self.started()
        ben_client = self.other()
        ben, _ = self.started(client=ben_client)
        self.pay_and_confirm(ben, client=ben_client)
        self.pay_and_confirm(ana)
        online_checkout.refund_attempt(ana.pk, "again")
        self.assertEqual(self.fake.count("create_refund"), 1)

    def test_a_refund_xendit_refuses_such_as_qr_ph_is_flagged_so_the_money_is_never_forgotten(self):
        ana, _ = self.started()
        ben_client = self.other()
        ben, _ = self.started(client=ben_client)
        self.pay_and_confirm(ben, client=ben_client)
        self.fake.fail["create_refund"] = xendit.XenditError("This channel does not support refunds", status=400)
        state = self.pay_and_confirm(ana, channel="QRPH")
        self.assertEqual(state["state"], "flagged")
        ana.refresh_from_db()
        self.assertEqual((ana.status, ana.refund_status), (PaymentAttempt.Status.FLAGGED, "failed"))
        self.assertTrue(ana.needs_a_look)

    def test_a_refund_that_later_fails_is_kept(self):
        ana, _ = self.started()
        ben_client = self.other()
        ben, _ = self.started(client=ben_client)
        self.pay_and_confirm(ben, client=ben_client)
        self.pay_and_confirm(ana)
        ana.refresh_from_db()
        online_checkout.on_refund_updated({"refund_id": ana.refund_id, "status": "FAILED", "amount": 2000})
        ana.refresh_from_db()
        self.assertEqual(ana.refund_status, "failed")


class CancelAndSweepTests(Base):
    def test_coming_back_without_paying_cancels_the_session(self):
        attempt, _ = self.started()
        response = self.client.get(reverse("gowns:payment_cancel", args=[attempt.token]))
        self.assertRedirects(response, reverse("gowns:reservation") + "?payment=cancelled", fetch_redirect_response=False)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.CANCELLED)
        self.assertEqual(self.fake.sessions[attempt.session_id]["status"], "CANCELED")

    def test_cancelling_a_payment_that_actually_went_through_shows_what_happened(self):
        attempt, _ = self.started()
        self.fake.paid(attempt.session_id)
        response = self.client.get(reverse("gowns:payment_cancel", args=[attempt.token]))
        self.assertRedirects(response, reverse("gowns:payment_return", args=[attempt.token]), fetch_redirect_response=False)
        self.assertEqual(Reservation.objects.count(), 1)

    def test_the_sweep_closes_old_unpaid_attempts_and_books_one_that_was_paid(self):
        old, _ = self.started(request_id="req-xo-old-0001")
        paid, _ = self.started(items=[self.item(self.gown2)], request_id="req-xo-new-0001", client=self.ben_client())          # another customer: one customer has one open page
        self.fake.paid(paid.session_id)
        PaymentAttempt.objects.filter(pk__in=[old.pk, paid.pk]).update(expires_at=timezone.now() - timedelta(minutes=5))
        self.assertEqual(online_checkout.expire_stale(), 2)
        old.refresh_from_db()
        paid.refresh_from_db()
        self.assertEqual((old.status, paid.status), (PaymentAttempt.Status.EXPIRED, PaymentAttempt.Status.BOOKED))
        self.assertEqual(self.fake.sessions[old.session_id]["status"], "CANCELED")


def event_body(name, data):
    return json.dumps({"event": name, "business_id": "biz", "created": "2026-11-24T11:41:17.366Z", "data": data}).encode()


class WebhookTests(Base):
    def post(self, name, data, *, token=TOKEN, url="gowns:xendit_webhook"):
        headers = {"HTTP_X_CALLBACK_TOKEN": token} if token is not None else {}
        return Client().post(reverse(url), data=event_body(name, data), content_type="application/json", **headers)

    def test_a_completed_session_event_books_the_attempt(self):
        attempt, _ = self.started()
        self.fake.paid(attempt.session_id)
        response = self.post("payment_session.completed", {"payment_session_id": attempt.session_id, "status": "COMPLETED"})
        self.assertEqual(response.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.BOOKED)
        self.assertIsNotNone(attempt.webhook_at)

    def test_the_session_named_id_as_in_xendits_own_sample_books_the_attempt_too(self):
        attempt, _ = self.started()
        self.fake.paid(attempt.session_id)
        self.assertEqual(self.post("payment_session.completed", {"id": attempt.session_id, "status": "COMPLETED"}).status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.BOOKED)

    def test_both_addresses_reach_the_same_view(self):
        attempt, _ = self.started()
        self.fake.paid(attempt.session_id)
        self.assertEqual(self.post("payment_session.completed", {"payment_session_id": attempt.session_id}, url="gowns:paymongo_webhook").status_code, 200)

    def test_the_event_is_only_a_prompt_the_money_is_read_back_from_xendit(self):
        attempt, _ = self.started()                                                       # NOT paid at Xendit
        response = self.post("payment_session.completed", {"payment_session_id": attempt.session_id, "status": "COMPLETED", "payment_id": "py-forged"})
        self.assertEqual(response.status_code, 503)                                       # we could not confirm: Xendit is asked to try again
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.CREATED)
        self.assertFalse(Reservation.objects.exists())

    def test_a_wrong_or_missing_token_is_refused_and_does_nothing(self):
        attempt, _ = self.started()
        self.fake.paid(attempt.session_id)
        for token in (None, "", "wrong", TOKEN + "x", TOKEN[:-1]):
            with self.subTest(token=token):
                self.assertEqual(self.post("payment_session.completed", {"payment_session_id": attempt.session_id}, token=token).status_code, 400)
        self.assertFalse(Reservation.objects.exists())

    def test_without_a_callback_token_set_nothing_is_believed(self):
        attempt, _ = self.started()
        self.fake.paid(attempt.session_id)
        with override_settings(XENDIT_WEBHOOK_TOKEN=""):
            self.assertEqual(self.post("payment_session.completed", {"payment_session_id": attempt.session_id}, token="").status_code, 400)
            self.assertEqual(self.post("payment_session.completed", {"payment_session_id": attempt.session_id}, token="anything").status_code, 400)
        self.assertFalse(Reservation.objects.exists())

    def test_events_we_do_not_use_and_sessions_we_do_not_know_are_answered_ok(self):
        self.assertEqual(self.post("payment_session.expired", {"payment_session_id": "ps-x"}).status_code, 200)
        self.assertEqual(self.post("payment_session.completed", {"payment_session_id": "ps-unknown"}).status_code, 200)
        self.assertEqual(self.post("something.new", {}).status_code, 200)

    def test_a_payment_that_arrives_after_we_closed_the_attempt_is_still_honoured(self):
        attempt, _ = self.started()
        PaymentAttempt.objects.filter(pk=attempt.pk).update(status=PaymentAttempt.Status.EXPIRED)
        self.fake.paid(attempt.session_id)
        self.assertEqual(self.post("payment_session.completed", {"payment_session_id": attempt.session_id}).status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.BOOKED)

    def test_a_refund_event_is_kept_and_a_failed_one_voids_the_deposit_return(self):
        attempt, _ = self.started()
        PaymentAttempt.objects.filter(pk=attempt.pk).update(refund_id="rfd-9", refund_status="pending", status=PaymentAttempt.Status.REFUNDED)
        self.assertEqual(self.post("refund.succeeded", {"refund_id": "rfd-9", "payment_id": "py-1", "status": "SUCCEEDED", "amount": 2000}).status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.refund_status, "succeeded")
        self.assertEqual(self.post("refund.failed", {"refund_id": "rfd-9", "payment_id": "py-1", "status": "FAILED", "amount": 2000}).status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.refund_status, "failed")

    def test_the_address_needs_no_csrf_token_and_get_is_not_allowed(self):
        self.assertEqual(Client().get(reverse("gowns:xendit_webhook")).status_code, 405)
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse("gowns:xendit_webhook"), data=event_body("x", {}), content_type="application/json", HTTP_X_CALLBACK_TOKEN=TOKEN)
        self.assertEqual(response.status_code, 200)


class DepositRefundTests(Base):
    """Handing a returned security deposit back through Xendit (reservations/payments.py)."""

    def booked(self, channel, *, days_ago=3):
        attempt, _ = self.started(option="full")
        self.pay_and_confirm(attempt, channel=channel)
        reservation = Reservation.objects.get()
        Payment.objects.filter(reservation=reservation).update(paid_at=timezone.now() - timedelta(days=days_ago))
        ReservationItem.objects.filter(reservation=reservation).update(stage=ReservationItem.Stage.RETURNED, returned_on=timezone.localdate(),
                                                                       return_date=timezone.localdate(), original_return_date=timezone.localdate())
        return Reservation.objects.get(pk=reservation.pk)

    def choice(self, reservation):
        from reservations import ledger
        return payments.online_refund_choice(Reservation.objects.get(pk=reservation.pk), ledger.statement(Reservation.objects.get(pk=reservation.pk)))

    def test_a_gcash_deposit_can_be_sent_back_and_the_receipt_names_xendit(self):
        booking = self.booked("GCASH")
        self.assertEqual(self.choice(booking), {"offered": True, "available": True, "label": "GCash", "reason": ""})
        payment = payments.return_deposit_online(reservation_id=booking.pk, amount=Decimal("2000"), user=None, request_id="ret-xo-0001")
        self.assertEqual((payment.provider, payment.direction, payment.amount), ("xendit", Payment.Direction.OUT, Decimal("2000.00")))
        self.assertTrue(payment.provider_ref.startswith("rfd-"))
        refund = list(self.fake.refunds.values())[0]
        self.assertEqual((refund["amount"], refund["payment_id"]), (2000, Payment.objects.filter(direction="In").get().provider_ref))
        self.assertIn("through Xendit", " ".join(e.detail for e in booking.status_events.all()))

    def test_a_maya_payment_can_be_part_refunded_only_from_the_day_after_it_was_paid(self):
        # Xendit's own answer (checked on its test mode, 2026-11-24): "Partial refund feature is not available for Paymaya on the day of transaction."
        booking = self.booked("PAYMAYA", days_ago=0)
        choice = self.choice(booking)
        self.assertFalse(choice["available"])                                             # the 2,000 deposit is a PART of the 5,500 paid, and it is the same day
        self.assertIn("only from the day after", choice["reason"])
        with self.assertRaises(payments.PaymentError) as caught:
            payments.return_deposit_online(reservation_id=booking.pk, amount=Decimal("1500"), user=None, request_id="ret-xo-0002")
        self.assertIn("Maya can send back part of a payment only from the day after", str(caught.exception))
        self.assertEqual(self.fake.count("create_refund"), 0)
        Payment.objects.filter(direction="In").update(paid_at=timezone.now() - timedelta(days=1, hours=1))
        payment = payments.return_deposit_online(reservation_id=booking.pk, amount=Decimal("1500"), user=None, request_id="ret-xo-0005")
        self.assertEqual(payment.amount, Decimal("1500.00"))

    def test_a_qr_ph_deposit_is_never_offered_an_automatic_refund(self):
        booking = self.booked("QRPH")
        choice = self.choice(booking)
        self.assertEqual((choice["offered"], choice["available"]), (True, False))
        self.assertIn("Xendit cannot send this kind of payment back automatically", choice["reason"])
        with self.assertRaises(payments.PaymentError) as caught:
            payments.return_deposit_online(reservation_id=booking.pk, amount=Decimal("2000"), user=None, request_id="ret-xo-0003")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.fake.count("create_refund"), 0)

    def test_a_refund_xendit_refuses_cancels_the_receipt_and_sends_nothing(self):
        booking = self.booked("GCASH")
        self.fake.fail["create_refund"] = xendit.XenditError("not allowed", status=400)
        with self.assertRaises(payments.PaymentError):
            payments.return_deposit_online(reservation_id=booking.pk, amount=Decimal("2000"), user=None, request_id="ret-xo-0004")
        row = Payment.objects.get(direction=Payment.Direction.OUT)
        self.assertTrue(row.is_void)
        self.assertIn("Xendit would not send the refund", row.void_reason)

    def test_a_gcash_payment_older_than_the_limit_is_not_offered(self):
        booking = self.booked("GCASH", days_ago=175)
        self.assertFalse(self.choice(booking)["available"])


class WordingTests(Base):
    """With Xendit on, nobody is told about PayMongo: the customer pages, the chat, the admin Payments pages and the receipts name Xendit."""

    def test_the_customer_pages_and_the_chat_name_xendit(self):
        for name in ("gowns:faqs", "gowns:how_it_works"):
            with self.subTest(page=name):
                page = self.client.get(reverse(name)).content.decode()
                self.assertIn("Xendit", page)
                self.assertNotIn("PayMongo", page)
        checkout = self.client.get(reverse("gowns:reservation")).content.decode()
        self.assertIn("Xendit", checkout)
        self.assertNotIn("PayMongo", checkout)
        self.assertIn("GCash, Maya or QR Ph", checkout)

    def test_the_admin_payments_pages_name_xendit(self):
        owner = User.objects.create_superuser("xo_owner", "xo.owner@gmail.com", "x")
        UserProfile.objects.update_or_create(user=owner, defaults={"role": UserProfile.Role.OWNER})
        self.client.force_login(owner)
        for name in ("arabela_admin:payments", "arabela_admin:payment_online", "arabela_admin:payment_receipts"):
            with self.subTest(page=name):
                page = self.client.get(reverse(name)).content.decode()
                self.assertIn("Xendit", page)
                visible = page.replace("'PayMongo'", "").replace('"PayMongo"', "")             # the form's internal key for "send it back online"
                self.assertNotIn("PayMongo", visible)

    def test_the_customer_receipt_never_names_paymongo(self):
        attempt, _ = self.started()
        self.pay_and_confirm(attempt, channel="GCASH")
        payment = Payment.objects.get()
        page = self.client.get(reverse("gowns:receipt_view", args=[payment.pk])).content.decode()
        self.assertNotIn("PayMongo", page)
        self.assertIn("GCash (online payment)", page)
