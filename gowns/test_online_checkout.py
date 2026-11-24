"""The online checkout through PayMongo (gowns/online_checkout.py, gowns/online_views.py), driven through the REAL views with a stand-in PayMongo.

The stand-in (`FakePayMongo`) replaces the one HTTP function of reservations/paymongo.py, so nothing reaches the network, and answers like the
documentation says PayMongo does. What these tests prove: nothing is booked until PayMongo says the money is in; the booking is confirmed with its
receipt in one piece; the same payment can never book twice, whoever tells us first; a gown taken while the customer paid sends the whole payment
back; a payment that is wrong is never booked; and the older checkout is untouched while no key is set."""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomerMessage, UserProfile
from accounts.test_support import agreed
from gowns import online_checkout
from gowns.models import Gown, GownUnavailability
from reservations import paymongo
from reservations.models import Payment, PaymentAttempt, Reservation, ReservationItem, ReservationStatusEvent

User = get_user_model()
KEY = "sk_test_unit"
SECRET = "whsk_unit"


class FakePayMongo:
    """What PayMongo does, in memory. `paid(session)` is the customer pressing Authorize on the test page."""

    def __init__(self):
        self.sessions = {}
        self.refunds = {}
        self.calls = []
        self.fail = {}                 # name of a call -> PaymongoError to raise from it
        self.n = 0

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        name = self._name(method, path)
        if name in self.fail:
            raise self.fail[name]
        if name == "create_session":
            self.n += 1
            attributes = body["data"]["attributes"]
            sid = f"cs_{self.n:04d}"
            self.sessions[sid] = {"attributes": attributes, "status": "active", "payments": [], "amount": attributes["line_items"][0]["amount"]}
            return {"data": {"id": sid, "type": "checkout_session", "attributes": {"checkout_url": f"https://checkout.paymongo.com/{sid}", "status": "active"}}}
        if name == "read_session":
            sid = path.rsplit("/", 1)[-1]
            return self._session_json(sid)
        if name == "expire_session":
            sid = path.split("/")[-2]
            session = self.sessions[sid]
            if session["status"] == "expired":
                raise paymongo.PaymongoError("The session is already expired", status=400)
            if any(p["attributes"]["status"] == "paid" for p in session["payments"]):
                raise paymongo.PaymongoError("The session already has a paid payment", status=400)
            session["status"] = "expired"
            return {}
        if name == "create_refund":
            attributes = body["data"]["attributes"]
            rid = f"ref_{len(self.refunds) + 1:04d}"
            self.refunds[rid] = {"payment_id": attributes["payment_id"], "amount": attributes["amount"], "notes": attributes["notes"], "status": "pending"}
            return {"data": {"id": rid, "type": "refund", "attributes": {"status": "pending", "amount": attributes["amount"], "payment_id": attributes["payment_id"]}}}
        raise AssertionError(f"unexpected PayMongo call {method} {path}")

    @staticmethod
    def _name(method, path):
        if method == "POST" and path == "/v2/checkout_sessions":
            return "create_session"
        if method == "GET" and path.startswith("/v1/checkout_sessions/"):
            return "read_session"
        if method == "POST" and path.endswith("/expire"):
            return "expire_session"
        if method == "POST" and path == "/v1/refunds":
            return "create_refund"
        return "other"

    def _session_json(self, sid):
        session = self.sessions[sid]
        return {"data": {"id": sid, "type": "checkout_session", "attributes": {
            "status": session["status"], "reference_number": session["attributes"]["reference_number"], "metadata": session["attributes"].get("metadata", {}),
            "payments": session["payments"], "livemode": False}}}

    def paid(self, sid, *, channel="gcash", amount=None, pay_id=None):
        session = self.sessions[sid]
        pay_id = pay_id or f"pay_{sid}"
        session["payments"] = [{"id": pay_id, "type": "payment", "attributes": {
            "status": "paid", "amount": session["amount"] if amount is None else amount, "source": {"type": channel}, "paid_at": int(timezone.now().timestamp())}}]
        return pay_id

    def count(self, name):
        return sum(1 for method, path in self.calls if self._name(method, path) == name)


@override_settings(PAYMONGO_SECRET_KEY=KEY, PAYMONGO_WEBHOOK_SECRET=SECRET, USING_LAPTOP_DB=False, SITE_BASE_URL="")
class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ana = User.objects.create_user("oc_ana", "ana@gmail.com", "x", first_name="Ana")
        cls.ben = User.objects.create_user("oc_ben", "ben@gmail.com", "x", first_name="Ben")
        UserProfile.objects.update_or_create(user=cls.ana, defaults={"display_name": "Ana Cruz"})
        cls.gown = Gown.objects.create(gown_id="Wedding Gown-OC-001", name="Online Gown One", category=Gown.Category.WEDDING_GOWN, color_name="White",
                                       color_code="WH", size=Gown.Size.MEDIUM, rental_price=Decimal("3500"), status=Gown.Status.AVAILABLE)
        cls.gown2 = Gown.objects.create(gown_id="Wedding Gown-OC-002", name="Online Gown Two", category=Gown.Category.WEDDING_GOWN, color_name="Ivory",
                                        color_code="IV", size=Gown.Size.MEDIUM, rental_price=Decimal("2500"), status=Gown.Status.AVAILABLE)

    def setUp(self):
        self.fake = FakePayMongo()
        patcher = patch("reservations.paymongo._request", side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.event = timezone.localdate() + timedelta(days=20)
        self.client = Client()
        self.client.force_login(self.ana)
        agreed(self.ana)

    def item(self, gown=None, event=None):
        gown = gown or self.gown
        event = event or self.event
        return {"gown_name": gown.name, "gown_slug": gown.slug, "size": "Medium", "rental_date": (event - timedelta(days=2)).isoformat(),
                "return_date": (event + timedelta(days=2)).isoformat()}

    def start(self, items=None, *, option="deposit", request_id=None, client=None, **extra):
        items = [self.item()] if items is None else items
        data = {"items": json.dumps(items), "pay_option": option, "first_name": "Ana", "last_name": "Cruz", "phone": "09171234567",
                "address": "1 Test St", "city": "Antipolo", "postal_code": "1870", "expected_deposit": str(2000 * len(items))}
        if request_id:
            data["request_id"] = request_id
        data.update(extra)
        return (client or self.client).post(reverse("gowns:pay_start"), data=data)

    def started(self, **kwargs):
        response = self.start(**kwargs)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        return PaymentAttempt.objects.get(token=body["token"]), body

    def poll(self, attempt, client=None):
        return (client or self.client).get(reverse("gowns:payment_status", args=[attempt.token]))

    def pay_and_confirm(self, attempt, client=None, **kwargs):
        self.fake.paid(attempt.session_id, **kwargs)
        PaymentAttempt.objects.filter(pk=attempt.pk).update(last_checked_at=None)
        response = self.poll(attempt, client)
        self.assertEqual(response.status_code, 200)
        attempt.refresh_from_db()
        return response.json()


class StartTests(Base):
    def test_the_deposit_choice_opens_a_checkout_for_exactly_the_deposit_and_books_nothing(self):
        attempt, body = self.started()
        self.assertTrue(body["checkout_url"].startswith("https://checkout.paymongo.com/cs_"))
        self.assertEqual((attempt.purpose, attempt.amount, attempt.status), ("deposit", Decimal("2000.00"), PaymentAttempt.Status.CREATED))
        session = self.fake.sessions[attempt.session_id]
        self.assertEqual(session["amount"], 200000)
        attributes = session["attributes"]
        self.assertEqual(attributes["payment_method_types"], ["gcash", "paymaya", "qrph"])
        self.assertEqual(attributes["reference_number"], attempt.token)
        self.assertTrue(attributes["success_url"].endswith(reverse("gowns:payment_return", args=[attempt.token])))
        self.assertTrue(attributes["cancel_url"].endswith(reverse("gowns:payment_cancel", args=[attempt.token])))
        self.assertTrue(attributes["success_url"].startswith("http"))
        self.assertFalse(Reservation.objects.exists())                                 # nothing is booked until the money is in
        self.assertFalse(GownUnavailability.objects.exists())
        self.assertEqual(attempt.payload["details"]["customer_name"], "Ana Cruz")
        self.assertEqual(attempt.payload["items"][0]["price"], "3500.00")
        self.assertGreater(len(attempt.token), 25)

    def test_the_full_choice_is_the_rental_plus_the_deposit(self):
        attempt, _ = self.started(option="full")
        self.assertEqual(attempt.amount, Decimal("5500.00"))
        self.assertEqual(self.fake.sessions[attempt.session_id]["amount"], 550000)

    def test_two_gowns_add_up_and_the_deposit_is_per_gown(self):
        attempt, _ = self.started(items=[self.item(self.gown), self.item(self.gown2)], option="full")
        self.assertEqual(attempt.amount, Decimal("3500") + Decimal("2500") + Decimal("4000"))

    def test_nothing_the_browser_says_about_money_is_believed(self):
        attempt, _ = self.started(option="deposit", amount="1", security_deposit="1", total_amount="1", rental_subtotal="1")
        self.assertEqual(attempt.amount, Decimal("2000.00"))

    def test_a_choice_that_is_not_deposit_or_full_is_refused(self):
        for option in ("", "half", "FULL", "deposit;full"):
            with self.subTest(option=option):
                response = self.start(option=option)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(PaymentAttempt.objects.count(), 0)
        self.assertEqual(self.fake.calls, [])

    def test_the_older_checks_still_apply(self):
        self.assertEqual(self.start(items=[]).status_code, 400)                                           # an empty bag
        bad = self.client.post(reverse("gowns:pay_start"), data={"items": "not json", "pay_option": "deposit"})
        self.assertEqual(bad.status_code, 400)
        undated = self.start(items=[{"gown_name": "x", "return_date": "", "rental_date": ""}])
        self.assertEqual(undated.status_code, 400)
        self.assertEqual(PaymentAttempt.objects.count(), 0)

    def test_a_deposit_that_changed_since_the_page_opened_is_told_and_nothing_is_opened(self):
        response = self.start(expected_deposit="1500")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["deposit_changed"])
        self.assertEqual(PaymentAttempt.objects.count(), 0)
        self.assertEqual(self.fake.calls, [])

    def test_terms_not_agreed_stops_it_with_the_popup_signal(self):
        UserProfile.objects.filter(user=self.ana).update(terms_accepted_at=None)
        response = self.start()
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.json()["terms_required"])
        self.assertEqual(self.fake.calls, [])

    def test_a_signed_out_visitor_gets_the_signed_out_message_not_a_login_page(self):
        response = self.start(client=Client())
        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.json()["signed_out"])

    def test_the_page_was_opened_for_another_account(self):
        response = self.start(expected_user=str(self.ben.pk))
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["account_changed"])

    def test_a_gown_that_is_already_taken_is_refused_before_anything_is_paid(self):
        other = Client()
        other.force_login(self.ben)
        agreed(self.ben)
        self.pay_and_confirm(self.started(client=other)[0], client=other)                                  # Ben pays and books it first
        response = self.start()
        self.assertEqual(response.status_code, 400)
        self.assertIn("no longer available", response.json()["error"])
        self.assertEqual(PaymentAttempt.objects.filter(customer=self.ana).count(), 0)

    def test_a_date_that_has_passed_is_refused(self):
        response = self.start(items=[self.item(event=timezone.localdate() - timedelta(days=3))])
        self.assertEqual(response.status_code, 400)
        self.assertIn("already passed", response.json()["error"])

    def test_no_key_means_the_online_payment_is_not_offered(self):
        with override_settings(PAYMONGO_SECRET_KEY=""):
            response = self.start()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(PaymentAttempt.objects.count(), 0)

    def test_a_live_key_is_never_used_on_a_laptop_copy_of_the_database(self):
        with override_settings(PAYMONGO_SECRET_KEY="sk_live_x", USING_LAPTOP_DB=True):
            self.assertEqual(self.start().status_code, 503)

    def test_paymongo_refusing_to_open_a_checkout_charges_nothing_and_says_so(self):
        self.fake.fail["create_session"] = paymongo.PaymongoError("down", status=503, retryable=True)
        response = self.start()
        self.assertEqual(response.status_code, 502)
        self.assertIn("Nothing was charged", response.json()["error"])
        attempt = PaymentAttempt.objects.get()
        self.assertEqual(attempt.status, PaymentAttempt.Status.EXPIRED)
        self.assertFalse(Reservation.objects.exists())

    def test_pressing_pay_twice_with_the_same_request_is_one_checkout(self):
        first = self.start(request_id="req-0001-aaaa")
        second = self.start(request_id="req-0001-aaaa")
        self.assertEqual(first.json()["checkout_url"], second.json()["checkout_url"])
        self.assertEqual(PaymentAttempt.objects.count(), 1)
        self.assertEqual(self.fake.count("create_session"), 1)

    def test_the_same_request_with_another_choice_opens_a_new_checkout_for_that_choice(self):
        first, _ = self.started(request_id="req-0002-bbbb", option="deposit")
        second, _ = self.started(request_id="req-0002-bbbb", option="full")
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual((first.amount, second.amount), (Decimal("2000.00"), Decimal("5500.00")))
        first.refresh_from_db()
        self.assertEqual(first.request_id, "")                                          # the old one gave its id up; it cannot be resumed by mistake

    def test_a_request_id_that_looks_wrong_is_ignored_not_trusted(self):
        attempt, _ = self.started(request_id="x; DROP TABLE")
        self.assertEqual(attempt.request_id, "")

    def test_one_account_cannot_open_checkouts_without_limit(self):
        statuses = [self.start().status_code for _ in range(12)]
        self.assertEqual(statuses[:10], [200] * 10)
        self.assertEqual(statuses[10:], [429, 429])


class ConfirmTests(Base):
    def test_a_paid_deposit_makes_a_confirmed_booking_with_its_receipt(self):
        attempt, _ = self.started()
        state = self.pay_and_confirm(attempt)
        self.assertEqual(state["state"], "booked")
        reservation = Reservation.objects.get()
        self.assertEqual((reservation.status, reservation.channel, reservation.customer), (Reservation.Status.CONFIRMED, Reservation.Channel.ONLINE, self.ana))
        self.assertEqual((reservation.rental_subtotal, reservation.security_deposit, reservation.total_amount), (Decimal("3500"), Decimal("2000"), Decimal("5500")))
        self.assertEqual((reservation.customer_name, reservation.phone, reservation.city, reservation.payment_method), ("Ana Cruz", "09171234567", "Antipolo", "GCash"))
        self.assertIsNotNone(reservation.reviewed_at)
        self.assertEqual(state["reference_code"], reservation.reference_code)
        item = reservation.items.get()
        self.assertEqual((item.gown, item.rental_price, item.stage), (self.gown, Decimal("3500"), ReservationItem.Stage.PICKUP))
        self.assertEqual(item.return_date, self.event + timedelta(days=2))
        self.assertTrue(GownUnavailability.objects.filter(auto_for_item=item, reason=GownUnavailability.Reason.COOLDOWN).exists())
        self.gown.refresh_from_db()
        self.assertEqual(self.gown.status, Gown.Status.RESERVED)
        attempt.refresh_from_db()
        self.assertEqual((attempt.status, attempt.reservation_id, attempt.paid_amount), (PaymentAttempt.Status.BOOKED, reservation.pk, Decimal("2000.00")))

    def test_the_payment_is_on_the_ledger_with_a_numbered_receipt_the_customer_can_open(self):
        attempt, _ = self.started()
        state = self.pay_and_confirm(attempt, channel="paymaya")
        payment = Payment.objects.get()
        self.assertEqual((payment.direction, payment.purpose, payment.method, payment.amount), ("In", "Deposit", Payment.Method.PM_MAYA, Decimal("2000.00")))
        self.assertEqual((payment.provider, payment.provider_ref, payment.recorded_by), ("paymongo", f"pay_{attempt.session_id}", None))
        self.assertRegex(payment.receipt_number, r"^PR-\d{4}-\d{6}$")
        self.assertEqual(payment.request_id, f"pm-{attempt.pk}")
        self.assertIn("TEST PAYMENT", payment.note)
        self.assertEqual(payment.snapshot["method"], "Maya (PayMongo)")
        self.assertEqual(payment.snapshot["balance_after"], "3500.00")
        self.assertEqual(payment.snapshot["paid_before"], "0.00")
        self.assertEqual(Reservation.objects.get().payment_method, "Maya")
        self.assertEqual(state["receipt_number"], payment.receipt_number)
        page = self.client.get(reverse("gowns:receipt_view", args=[payment.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, payment.receipt_number)

    def test_a_paid_in_full_booking_has_nothing_left_to_pay(self):
        from reservations import ledger
        attempt, _ = self.started(option="full")
        self.pay_and_confirm(attempt, channel="qrph")
        payment = Payment.objects.get()
        self.assertEqual((payment.purpose, payment.amount, payment.method), ("Paid in full", Decimal("5500.00"), Payment.Method.PM_QRPH))
        statement = ledger.statement(Reservation.objects.get())
        self.assertEqual((statement.balance_due, statement.deposit_paid, statement.received), (Decimal("0.00"), Decimal("2000.00"), Decimal("5500.00")))

    def test_a_deposit_only_booking_owes_the_rental_at_the_shop(self):
        from reservations import ledger
        self.pay_and_confirm(self.started()[0])
        statement = ledger.statement(Reservation.objects.get())
        self.assertEqual((statement.balance_due, statement.deposit_paid), (Decimal("3500.00"), Decimal("2000.00")))

    def test_the_timeline_has_the_payment_and_the_approval_but_not_the_checking_your_payment_line(self):
        self.pay_and_confirm(self.started()[0])
        labels = list(ReservationStatusEvent.objects.values_list("label", "staff_only").order_by("id"))
        names = [label for label, _ in labels]
        self.assertIn("Payment received", names)
        self.assertIn("Reservation approved", names)
        self.assertNotIn("Reservation submitted", names)                               # that line sends the "we are checking your GCash payment" email
        self.assertIn(("Booked and paid online", True), labels)
        self.assertIn(("Terms and Conditions accepted", True), labels)

    @override_settings(CUSTOMER_EMAILS_ENABLED=True, CUSTOMER_EMAILS_BACKGROUND=False)
    def test_the_customer_gets_the_approved_email_and_the_payment_email_and_bell_messages(self):
        attempt, _ = self.started()
        with self.captureOnCommitCallbacks(execute=True):
            self.pay_and_confirm(attempt)
        subjects = [m.subject for m in mail.outbox]
        self.assertEqual(len(subjects), 2, subjects)
        self.assertTrue(any("is approved" in s for s in subjects))
        self.assertTrue(any("Payment received PHP 2,000.00" in s for s in subjects))
        self.assertFalse(any("We received your reservation" in s for s in subjects))
        kinds = set(CustomerMessage.objects.filter(recipient=self.ana).values_list("category", flat=True))
        self.assertEqual(kinds, {CustomerMessage.Category.GENERAL, CustomerMessage.Category.PAYMENT})

    def test_asking_twice_books_once(self):
        attempt, _ = self.started()
        self.pay_and_confirm(attempt)
        online_checkout.confirm_attempt(attempt.pk, source="webhook")
        online_checkout.confirm_attempt(attempt.pk, source="sweep")
        self.assertEqual((Reservation.objects.count(), Payment.objects.count(), ReservationItem.objects.count()), (1, 1, 1))

    def test_an_unpaid_checkout_stays_waiting_and_books_nothing(self):
        attempt, _ = self.started()
        response = self.poll(attempt)
        self.assertEqual(response.json()["state"], "waiting")
        self.assertFalse(Reservation.objects.exists())

    def test_the_return_page_asks_paymongo_at_most_once_every_two_seconds(self):
        attempt, _ = self.started()
        self.poll(attempt)
        self.poll(attempt)
        self.poll(attempt)
        self.assertEqual(self.fake.count("read_session"), 1)

    def test_paymongo_being_unreachable_is_waiting_not_a_failure(self):
        attempt, _ = self.started()
        self.fake.paid(attempt.session_id)
        self.fake.fail["read_session"] = paymongo.PaymongoError("down", status=503, retryable=True)
        response = self.poll(attempt)
        self.assertEqual((response.status_code, response.json()["state"]), (200, "waiting"))
        del self.fake.fail["read_session"]
        PaymentAttempt.objects.filter(pk=attempt.pk).update(last_checked_at=None)
        self.assertEqual(self.poll(attempt).json()["state"], "booked")

    def test_a_session_paymongo_says_is_expired_ends_the_attempt(self):
        attempt, _ = self.started()
        self.fake.sessions[attempt.session_id]["status"] = "expired"
        self.assertEqual(self.poll(attempt).json()["state"], "ended")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.EXPIRED)

    def test_a_payment_of_the_wrong_amount_is_flagged_and_never_booked(self):
        attempt, _ = self.started()
        state = self.pay_and_confirm(attempt, amount=100000)
        self.assertEqual(state["state"], "flagged")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.FLAGGED)
        self.assertIn("1,000.00", attempt.note)
        self.assertTrue(attempt.needs_a_look)
        self.assertFalse(Reservation.objects.exists())

    def test_a_session_with_another_reference_is_flagged(self):
        attempt, _ = self.started()
        self.fake.sessions[attempt.session_id]["attributes"]["reference_number"] = "someone-elses"
        self.assertEqual(self.pay_and_confirm(attempt)["state"], "flagged")
        self.assertFalse(Reservation.objects.exists())

    def test_one_paymongo_payment_can_never_book_two_attempts(self):
        first, _ = self.started()
        self.pay_and_confirm(first, pay_id="pay_same")
        second, _ = self.started(items=[self.item(self.gown2)])
        self.fake.paid(second.session_id, pay_id="pay_same", amount=200000)
        PaymentAttempt.objects.filter(pk=second.pk).update(last_checked_at=None)
        self.poll(second)
        second.refresh_from_db()
        self.assertEqual(second.status, PaymentAttempt.Status.FLAGGED)
        self.assertEqual(Reservation.objects.count(), 1)

    def test_only_the_customer_who_paid_can_see_or_poll_their_attempt(self):
        attempt, _ = self.started()
        other = Client()
        other.force_login(self.ben)
        self.assertEqual(other.get(reverse("gowns:payment_status", args=[attempt.token])).status_code, 404)
        self.assertEqual(other.get(reverse("gowns:payment_return", args=[attempt.token])).status_code, 404)
        self.assertEqual(other.get(reverse("gowns:payment_cancel", args=[attempt.token])).status_code, 404)
        anonymous = Client()
        self.assertEqual(anonymous.get(reverse("gowns:payment_status", args=[attempt.token])).status_code, 302)
        self.assertEqual(self.client.get(reverse("gowns:payment_return", args=[attempt.token])).status_code, 200)

    def test_a_booking_that_errors_unexpectedly_stays_paid_and_is_tried_again(self):
        attempt, _ = self.started()
        with patch("gowns.online_checkout._create_booking", side_effect=RuntimeError("boom")):
            state = self.pay_and_confirm(attempt)
        self.assertEqual(state["state"], "confirming")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.PAID)
        self.assertFalse(Reservation.objects.exists())
        self.assertEqual(self.fake.refunds, {})                                           # an unexpected error is never answered by sending money back
        self.assertEqual(self.poll(attempt).json()["state"], "booked")                   # the next look finishes the job
        self.assertEqual(Reservation.objects.count(), 1)

    def test_a_paid_attempt_that_stays_unbooked_for_minutes_is_flagged_for_a_person(self):
        attempt, _ = self.started()
        with patch("gowns.online_checkout._create_booking", side_effect=RuntimeError("boom")):
            self.pay_and_confirm(attempt)
        attempt.refresh_from_db()
        self.assertFalse(attempt.needs_a_look)
        PaymentAttempt.objects.filter(pk=attempt.pk).update(paid_at=timezone.now() - timedelta(minutes=6))
        attempt.refresh_from_db()
        self.assertTrue(attempt.needs_a_look)


class GownTakenWhilePayingTests(Base):
    def take_it(self):
        """Another customer pays and books the same gown on the same day while Ana is on PayMongo's page."""
        other = Client()
        other.force_login(self.ben)
        agreed(self.ben)
        # Ben's checkout opened BEFORE Ana's booking exists, so it passes the start checks; Ana's did too.
        return other

    def test_the_whole_payment_goes_back_and_the_customer_is_told(self):
        ana_attempt, _ = self.started(option="full")
        ben = self.take_it()
        ben_attempt, _ = self.started(client=ben)
        self.assertEqual(self.pay_and_confirm(ben_attempt, client=ben, channel="gcash")["state"], "booked")           # Ben's money arrives first
        state = self.pay_and_confirm(ana_attempt, channel="paymaya")                                       # Ana's arrives second: the gown is gone
        self.assertEqual(state["state"], "refunded")
        self.assertIn("no longer available", state["message"])
        ana_attempt.refresh_from_db()
        self.assertEqual((ana_attempt.status, ana_attempt.refund_status, ana_attempt.refund_amount), (PaymentAttempt.Status.REFUNDED, "pending", Decimal("5500.00")))
        refund = list(self.fake.refunds.values())[0]
        self.assertEqual((refund["payment_id"], refund["amount"]), (ana_attempt.provider_payment_id, 550000))
        self.assertIn("no longer available", refund["notes"])
        self.assertEqual(Reservation.objects.count(), 1)                                                   # only Ben's booking exists
        self.assertFalse(Payment.objects.filter(reservation__customer=self.ana).exists())
        message = CustomerMessage.objects.filter(recipient=self.ana, category="Payment").get()
        self.assertIn("being sent back to your Maya", message.body)
        self.assertIn("5,500.00", message.body)

    def test_asking_again_never_sends_the_money_back_twice(self):
        ana_attempt, _ = self.started()
        ben = self.take_it()
        ben_attempt, _ = self.started(client=ben)
        self.pay_and_confirm(ben_attempt, client=ben)
        self.pay_and_confirm(ana_attempt)
        online_checkout.refund_attempt(ana_attempt.pk, "again")
        online_checkout.confirm_attempt(ana_attempt.pk, source="webhook")
        self.assertEqual(self.fake.count("create_refund"), 1)

    def test_a_refund_paymongo_refuses_is_flagged_so_the_money_is_never_forgotten(self):
        ana_attempt, _ = self.started()
        ben = self.take_it()
        ben_attempt, _ = self.started(client=ben)
        self.pay_and_confirm(ben_attempt, client=ben)
        self.fake.fail["create_refund"] = paymongo.PaymongoError("payment_not_refundable", status=400)
        state = self.pay_and_confirm(ana_attempt)
        self.assertEqual(state["state"], "flagged")
        ana_attempt.refresh_from_db()
        self.assertEqual((ana_attempt.status, ana_attempt.refund_status), (PaymentAttempt.Status.FLAGGED, "failed"))
        self.assertTrue(ana_attempt.needs_a_look)
        self.assertIn("Refund not sent", ana_attempt.note)
        self.assertIn("shop has been told", CustomerMessage.objects.get(recipient=self.ana, category="Payment").body)

    def test_a_refund_that_later_fails_is_visible_to_staff(self):
        ana_attempt, _ = self.started()
        ben = self.take_it()
        ben_attempt, _ = self.started(client=ben)
        self.pay_and_confirm(ben_attempt, client=ben)
        self.pay_and_confirm(ana_attempt)
        ana_attempt.refresh_from_db()
        online_checkout.on_refund_updated({"id": ana_attempt.refund_id, "type": "refund", "attributes": {"status": "failed", "amount": 200000}})
        ana_attempt.refresh_from_db()
        self.assertEqual(ana_attempt.refund_status, "failed")
        self.assertTrue(ana_attempt.needs_a_look)


class CancelAndSweepTests(Base):
    def test_coming_back_without_paying_closes_the_session_and_returns_to_checkout(self):
        attempt, _ = self.started()
        response = self.client.get(reverse("gowns:payment_cancel", args=[attempt.token]))
        self.assertRedirects(response, reverse("gowns:reservation") + "?payment=cancelled", fetch_redirect_response=False)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.CANCELLED)
        self.assertEqual(self.fake.sessions[attempt.session_id]["status"], "expired")                   # it can no longer be paid

    def test_cancelling_a_payment_that_actually_went_through_shows_what_happened(self):
        attempt, _ = self.started()
        self.fake.paid(attempt.session_id)
        response = self.client.get(reverse("gowns:payment_cancel", args=[attempt.token]))
        self.assertRedirects(response, reverse("gowns:payment_return", args=[attempt.token]), fetch_redirect_response=False)
        self.assertEqual(Reservation.objects.count(), 1)

    def test_the_sweep_closes_old_unpaid_attempts_and_leaves_fresh_ones_alone(self):
        old, _ = self.started(request_id="req-old-0001")
        fresh, _ = self.started(items=[self.item(self.gown2)], request_id="req-new-0001")
        PaymentAttempt.objects.filter(pk=old.pk).update(expires_at=timezone.now() - timedelta(minutes=5))
        self.assertEqual(online_checkout.expire_stale(), 1)
        old.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual((old.status, fresh.status), (PaymentAttempt.Status.EXPIRED, PaymentAttempt.Status.CREATED))
        self.assertEqual(self.fake.sessions[old.session_id]["status"], "expired")

    def test_the_sweep_books_a_stale_attempt_that_turned_out_to_be_paid(self):
        attempt, _ = self.started()
        self.fake.paid(attempt.session_id)
        PaymentAttempt.objects.filter(pk=attempt.pk).update(expires_at=timezone.now() - timedelta(minutes=5))
        online_checkout.expire_stale()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.BOOKED)
        self.assertEqual(Reservation.objects.count(), 1)

    def test_an_attempt_that_never_got_a_checkout_is_closed_without_calling_paymongo(self):
        attempt = PaymentAttempt.objects.create(token="t" * 32, customer=self.ana, purpose="deposit", amount=Decimal("2000"), expires_at=timezone.now() - timedelta(minutes=5))
        self.assertEqual(online_checkout.expire_stale(), 1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.EXPIRED)
        self.assertEqual(self.fake.calls, [])

    def test_the_sweep_is_bounded(self):
        for n in range(8):
            PaymentAttempt.objects.create(token=f"{n}" * 32, customer=self.ana, purpose="deposit", amount=Decimal("2000"), expires_at=timezone.now() - timedelta(minutes=5))
        self.assertEqual(online_checkout.expire_stale(limit=3), 3)


def sign(body, *, t="1700000000", secret=SECRET, field="te"):
    import hashlib
    import hmac
    digest = hmac.new(secret.encode(), t.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={t},te={digest if field == 'te' else ''},li={digest if field == 'li' else ''}"


class WebhookTests(Base):
    def post(self, event_type, resource, *, signature=True, livemode=False):
        body = json.dumps({"data": {"id": "evt_1", "type": "event", "attributes": {"type": event_type, "livemode": livemode, "data": resource}}}).encode()
        headers = {"HTTP_PAYMONGO_SIGNATURE": sign(body)} if signature else {}
        return Client().post(reverse("gowns:paymongo_webhook"), data=body, content_type="application/json", **headers)

    def test_a_genuine_paid_event_books_the_attempt(self):
        attempt, _ = self.started()
        self.fake.paid(attempt.session_id)
        response = self.post("checkout_session.payment.paid", {"id": attempt.session_id, "type": "checkout_session"})
        self.assertEqual(response.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.BOOKED)
        self.assertIsNotNone(attempt.webhook_at)

    def test_the_webhook_is_only_a_prompt_the_money_is_read_back_from_paymongo(self):
        attempt, _ = self.started()                                                       # NOT paid at PayMongo
        response = self.post("checkout_session.payment.paid", {"id": attempt.session_id, "type": "checkout_session",
                                                              "attributes": {"payments": [{"id": "pay_fake", "attributes": {"status": "paid", "amount": 200000}}]}})
        self.assertEqual(response.status_code, 503)                                       # we could not confirm: PayMongo is asked to try again
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.CREATED)
        self.assertFalse(Reservation.objects.exists())

    def test_a_bad_or_missing_signature_is_refused_and_does_nothing(self):
        attempt, _ = self.started()
        self.fake.paid(attempt.session_id)
        for signature in (False,):
            response = self.post("checkout_session.payment.paid", {"id": attempt.session_id}, signature=signature)
            self.assertEqual(response.status_code, 400)
        body = b'{"data": {"attributes": {"type": "checkout_session.payment.paid", "livemode": false, "data": {"id": "x"}}}}'
        forged = Client().post(reverse("gowns:paymongo_webhook"), data=body, content_type="application/json", HTTP_PAYMONGO_SIGNATURE=sign(body, secret="wrong"))
        self.assertEqual(forged.status_code, 400)
        self.assertFalse(Reservation.objects.exists())

    def test_without_a_webhook_secret_nothing_is_believed(self):
        attempt, _ = self.started()
        self.fake.paid(attempt.session_id)
        with override_settings(PAYMONGO_WEBHOOK_SECRET=""):
            response = self.post("checkout_session.payment.paid", {"id": attempt.session_id})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Reservation.objects.exists())

    def test_events_we_do_not_use_and_sessions_we_do_not_know_are_answered_ok(self):
        self.assertEqual(self.post("payment.failed", {"id": "pay_1"}).status_code, 200)
        self.assertEqual(self.post("checkout_session.payment.paid", {"id": "cs_unknown"}).status_code, 200)
        self.assertEqual(self.post("something.new", {}).status_code, 200)

    def test_a_payment_that_arrives_after_we_closed_the_attempt_is_still_honoured(self):
        attempt, _ = self.started()
        PaymentAttempt.objects.filter(pk=attempt.pk).update(status=PaymentAttempt.Status.EXPIRED)
        self.fake.paid(attempt.session_id)
        response = self.post("checkout_session.payment.paid", {"id": attempt.session_id})
        self.assertEqual(response.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.BOOKED)

    def test_a_refund_update_is_kept(self):
        ana_attempt, _ = self.started()
        PaymentAttempt.objects.filter(pk=ana_attempt.pk).update(refund_id="ref_9", refund_status="pending", status=PaymentAttempt.Status.REFUNDED)
        self.assertEqual(self.post("payment.refund.updated", {"id": "ref_9", "type": "refund", "attributes": {"status": "succeeded", "amount": 200000}}).status_code, 200)
        ana_attempt.refresh_from_db()
        self.assertEqual(ana_attempt.refund_status, "succeeded")

    def test_get_is_not_allowed_and_the_address_needs_no_csrf_token(self):
        self.assertEqual(Client().get(reverse("gowns:paymongo_webhook")).status_code, 405)
        client = Client(enforce_csrf_checks=True)
        body = b"{}"
        response = client.post(reverse("gowns:paymongo_webhook"), data=body, content_type="application/json", HTTP_PAYMONGO_SIGNATURE=sign(body))
        self.assertEqual(response.status_code, 200)                                       # a correctly signed message needs no CSRF token (PayMongo has none)


class CheckoutPageTests(Base):
    def test_with_a_key_the_page_offers_the_two_choices_and_no_screenshot_upload(self):
        page = self.client.get(reverse("gowns:reservation")).content.decode()
        for needed in ('id="pay-section"', 'name="pay_option" value="deposit"', 'name="pay_option" value="full"', "Pay Security Deposit Only", "Pay in Full",
                       "GCash, Maya or QR Ph", 'id="pay-btn-label"', "var PAYMONGO = true;", "Test mode: the payment page is a simulation"):
            with self.subTest(needed=needed):
                self.assertIn(needed, page)
        for gone in ('id="proof_of_payment"', 'id="gcash_reference"', 'id="gcash-modal"', "Upload Receipt"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, page)

    def test_with_no_key_the_older_screenshot_checkout_is_exactly_as_it_was(self):
        with override_settings(PAYMONGO_SECRET_KEY=""):
            page = self.client.get(reverse("gowns:reservation")).content.decode()
        for needed in ('id="proof_of_payment"', 'id="gcash_reference"', 'id="gcash-modal"', "Confirm Rental", "var PAYMONGO = false;"):
            with self.subTest(needed=needed):
                self.assertIn(needed, page)
        for gone in ('id="pay-section"', 'name="pay_option" value="deposit"', 'id="pay-btn-label"'):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, page)

    def test_a_live_key_hides_the_test_note(self):
        with override_settings(PAYMONGO_SECRET_KEY="sk_live_x"):
            page = self.client.get(reverse("gowns:reservation")).content.decode()
        self.assertIn('id="pay-section"', page)
        self.assertNotIn("pay-test-note", page)

    def test_coming_back_from_a_cancelled_payment_says_nothing_was_charged(self):
        page = self.client.get(reverse("gowns:reservation") + "?payment=cancelled").content.decode()
        self.assertIn("nothing was charged", page)
        self.assertNotIn("pay-cancelled-note", self.client.get(reverse("gowns:reservation")).content.decode())

    def test_the_return_page_renders_for_its_owner_and_never_caches(self):
        attempt, _ = self.started()
        response = self.client.get(reverse("gowns:payment_return", args=[attempt.token]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertContains(response, reverse("gowns:payment_status", args=[attempt.token]))
        for state in ("confirming", "booked", "slow", "refunded", "flagged", "ended"):
            self.assertContains(response, f'data-pay-state="{state}"')


class CustomerCopyTests(Base):
    """What the site tells a customer about paying must match the checkout that is really in use."""

    def test_the_faq_and_how_it_works_describe_paymongo_while_a_key_is_set(self):
        for name in ("gowns:faqs", "gowns:how_it_works"):
            with self.subTest(page=name):
                page = self.client.get(reverse(name)).content.decode()
                self.assertIn("PayMongo", page)
                self.assertIn("GCash, Maya or QR Ph", page)
                self.assertNotIn("upload a screenshot", page)
                self.assertNotIn("upload the screenshot", page)

    def test_with_no_key_they_say_exactly_what_they_always_said(self):
        with override_settings(PAYMONGO_SECRET_KEY=""):
            faqs = self.client.get(reverse("gowns:faqs")).content.decode()
            how = self.client.get(reverse("gowns:how_it_works")).content.decode()
        self.assertIn("The system accepts GCash and cash payments only.", faqs)
        self.assertIn("upload the screenshot so the admin can see and verify it", how)
        self.assertNotIn("PayMongo", faqs + how)

    def test_the_ai_chat_explains_the_checkout_that_is_in_use(self):
        from ai_recommendation import views as ai
        online = ai._system_prompt(("", set()))
        self.assertIn("PayMongo", online)
        self.assertIn("NOT to pay again", online)
        self.assertNotIn("screenshot", online)
        self.assertNotIn("Payment proof: only GCash receipts are accepted", online)
        with override_settings(PAYMONGO_SECRET_KEY=""):
            old = ai._system_prompt(("", set()))
        self.assertIn("upload the GCash receipt (the screenshot of the payment)", old)
        self.assertIn("Payment proof: only GCash receipts are accepted", old)
        self.assertIn("each reservation shows its payment status", old)
        self.assertNotIn("PayMongo", old)


class PaidCustomerHoldTests(Base):
    """A customer who paid on PayMongo's page and never came back to the site must not be counted as someone who abandoned their checkout."""

    def lapse(self, client):
        from gowns.context_processors import RESERVATION_HOLD_SESSION_KEY
        session = client.session
        session[RESERVATION_HOLD_SESSION_KEY] = (timezone.now() - timedelta(minutes=3)).isoformat()
        session.save()

    def abandoned(self):
        return UserProfile.objects.get(user=self.ana).hold_abandon_count

    def test_a_lapsed_hold_still_counts_for_someone_who_did_not_pay(self):
        before = self.abandoned()
        self.lapse(self.client)
        self.client.get(reverse("gowns:homepage"))
        self.assertEqual(self.abandoned(), before + 1)

    def test_it_does_not_count_for_someone_whose_payment_went_through(self):
        attempt, _ = self.started()
        self.pay_and_confirm(attempt)
        before = self.abandoned()
        self.lapse(self.client)
        self.client.get(reverse("gowns:homepage"))
        self.assertEqual(self.abandoned(), before)

    def test_the_status_page_ends_the_hold_when_the_booking_is_made(self):
        from gowns.context_processors import RESERVATION_HOLD_SESSION_KEY
        session = self.client.session
        session[RESERVATION_HOLD_SESSION_KEY] = (timezone.now() + timedelta(minutes=15)).isoformat()
        session.save()
        attempt, _ = self.started()
        self.pay_and_confirm(attempt)
        self.assertNotIn(RESERVATION_HOLD_SESSION_KEY, self.client.session)


class OlderCheckoutIsClosedWhilePayMongoIsOnTests(Base):
    def test_a_checkout_page_opened_before_the_switch_books_nothing_and_says_to_reload(self):
        response = self.client.post(reverse("gowns:reservation_submit"), data={"items": json.dumps([self.item()]), "payment_method": "GCash"})
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertTrue(body["reload_required"])
        self.assertIn("reload this page", body["error"])
        self.assertFalse(Reservation.objects.exists())

    def test_with_no_key_the_older_endpoint_answers_as_it_always_did(self):
        with override_settings(PAYMONGO_SECRET_KEY=""):
            response = self.client.post(reverse("gowns:reservation_submit"), data={"items": "[]"})
        self.assertEqual(response.status_code, 400)                                       # "Your bag is empty." -- the older checks run
        self.assertNotIn("reload_required", response.json())
