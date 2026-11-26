"""An existing booking paid ON THE CUSTOMER'S PHONE from the Payments page's QR code (gowns/counter_payment.py): the same amount rules as cash, recorded only
once Xendit confirms the money, never twice, closed before cash is taken, and sent back when it is no longer needed. Staff take only Cash or Pay online
anywhere (no typed GCash), and Mark as returned says the deposit's next step. Xendit is the in-memory FakeXendit of gowns/test_xendit_checkout.py."""
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditLog, CustomerMessage, UserProfile
from arabela_admin.notifications import build_items
from gowns import online_checkout
from gowns.test_xendit_checkout import SETTINGS, TOKEN, FakeXendit
from reservations import ledger, payments, xendit
from reservations.models import Payment, PaymentAttempt, Reservation, ReservationItem
from reservations.test_ledger import make_booking

User = get_user_model()
D = Decimal
P = Payment.Purpose
S = PaymentAttempt.Status
ADMIN = Path(settings.BASE_DIR) / "templates" / "arabela_admin"


@override_settings(**SETTINGS)
class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("cp_staff", password="x", is_staff=True, first_name="Maria", last_name="Cruz")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.STAFF})
        cls.other = User.objects.create_user("cp_other", password="x", is_staff=True, first_name="Pia", last_name="Reyes")
        UserProfile.objects.update_or_create(user=cls.other, defaults={"role": UserProfile.Role.STAFF})
        cls.customer = User.objects.create_user("cp_customer", email="cp.customer@gmail.com", password="x", first_name="Juan")

    def setUp(self):
        self.fake = FakeXendit()
        patcher = patch("reservations.xendit._request", side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_login(self.staff)

    def booking(self, **kwargs):
        kwargs.setdefault("customer", self.customer)
        return make_booking(**kwargs)

    def cash(self, booking, purpose, amount):
        return payments.record_payment(reservation_id=booking.pk, purpose=purpose, amount=D(amount), method="Cash", user=self.staff)

    def fresh(self, booking):
        return Reservation.objects.get(pk=booking.pk)

    def stmt(self, booking):
        return ledger.statement(self.fresh(booking))

    def start(self, booking, purpose=P.FULL, amount="5500", *, rid="cp-request-0001", client=None):
        return (client or self.client).post(reverse("arabela_admin:payment_pay_start", args=[booking.pk]),
                                            data=json.dumps({"purpose": purpose, "amount": amount, "request_id": rid}), content_type="application/json")

    def started(self, booking, purpose=P.FULL, amount="5500", **kwargs):
        response = self.start(booking, purpose, amount, **kwargs)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        return PaymentAttempt.objects.get(token=body["token"]), body

    def poll(self, attempt):
        PaymentAttempt.objects.filter(pk=attempt.pk).update(last_checked_at=None)
        response = self.client.get(reverse("arabela_admin:payment_pay_status", args=[attempt.token]))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def pay(self, attempt, **kwargs):
        self.fake.paid(attempt.session_id, **kwargs)
        return self.poll(attempt)

    def window(self, booking):
        response = self.client.get(reverse("arabela_admin:payment_detail", args=[booking.pk]))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def record_cash(self, booking, purpose, amount):
        return self.client.post(reverse("arabela_admin:payment_record", args=[booking.pk]),
                                data=json.dumps({"purpose": purpose, "amount": amount, "method": "Cash"}), content_type="application/json")

    def online_rows(self, booking):
        return list(Payment.objects.filter(reservation=booking, provider="xendit"))


class StartTests(Base):
    def test_show_payment_qr_opens_a_page_for_exactly_the_amount_and_records_nothing(self):
        booking = self.booking()
        attempt, body = self.started(booking)
        self.assertEqual((body["state"], body["amount"], body["minutes"], body["bookingId"]), ("waiting", "5,500.00", 15, booking.pk))
        self.assertTrue(body["qr"].startswith("data:image/png;base64,"))
        self.assertTrue(body["checkoutUrl"].startswith("https://checkout-staging.xendit.co/session/"))
        self.assertIn(reverse("arabela_admin:payment_pay_status", args=[attempt.token]), body["statusUrl"])
        self.assertEqual((attempt.source, attempt.staff, attempt.customer, attempt.reservation, attempt.purpose, attempt.amount, attempt.status),
                         ("shop", self.staff, None, booking, "full", D("5500.00"), S.CREATED))
        sent = self.fake.sessions[attempt.session_id]["body"]
        self.assertEqual((sent["amount"], sent["reference_id"]), (5500, attempt.token))
        self.assertTrue(sent["success_return_url"].endswith(reverse("gowns:walkin_pay_done", args=[attempt.token])))
        self.assertIn(booking.reference_code, sent["description"])
        self.assertFalse(Payment.objects.filter(reservation=booking).exists())
        row = AuditLog.objects.get(action="payment.pay_start")
        self.assertIn(f"showed a payment QR code for ₱5,500.00 on booking {booking.reference_code}", row.summary)

    def test_the_same_amount_rules_as_cash(self):
        booking = self.booking()
        cases = [(P.DEPOSIT, "1000", "exactly"), (P.FULL, "5000", "exactly"), (P.BALANCE, "500", "deposit"), (P.EXTRA, "100", "Nothing extra"),
                 ("Nonsense", "100", "Choose"), (P.FULL, "abc", "numbers"), (P.FULL, "0", "amount")]
        for purpose, amount, words in cases:
            with self.subTest(purpose=purpose, amount=amount):
                response = self.start(booking, purpose, amount)
                self.assertGreaterEqual(response.status_code, 400)
                self.assertIn(words, response.json()["error"])
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_a_cancelled_or_pending_booking_cannot_be_paid(self):
        for status in (Reservation.Status.CANCELLED, Reservation.Status.PENDING):
            booking = self.booking()
            Reservation.objects.filter(pk=booking.pk).update(status=status, reviewed_at=None)
            with self.subTest(status=status):
                self.assertEqual(self.start(booking).status_code, 409)
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_the_online_limit_is_said_with_what_to_do(self):
        booking = self.booking(rental="60000")
        self.cash(booking, P.DEPOSIT, "2000")
        response = self.start(booking, P.BALANCE, "60000")
        self.assertEqual(response.status_code, 400)
        self.assertIn("At most ₱50,000.00", response.json()["error"])
        self.assertIn("in parts", response.json()["error"])
        self.assertEqual(self.start(booking, P.BALANCE, "50000").status_code, 200)

    def test_only_admin_accounts_and_only_while_online_payment_is_on(self):
        booking = self.booking()
        self.assertEqual(self.start(booking, client=Client()).status_code, 401)
        shopper = Client()
        shopper.force_login(self.customer)
        self.assertEqual(self.start(booking, client=shopper).status_code, 401)
        with override_settings(XENDIT_SECRET_KEY="", PAYMENT_PROVIDER="xendit"):
            response = self.start(booking)
            self.assertEqual(response.status_code, 503)
            self.assertIn("Take cash", response.json()["error"])
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_a_payment_page_that_will_not_open_charges_nothing(self):
        booking = self.booking()
        self.fake.fail["create_session"] = xendit.XenditError("down", status=500)
        response = self.start(booking)
        self.assertEqual(response.status_code, 502)
        self.assertIn("Nothing was charged", response.json()["error"])
        self.assertEqual(PaymentAttempt.objects.get().status, S.EXPIRED)

    def test_pressing_twice_gives_one_page_and_a_new_press_closes_the_old_one(self):
        booking = self.booking()
        first, _ = self.started(booking)
        again, _ = self.started(booking)
        self.assertEqual(first.pk, again.pk)
        other, _ = self.started(booking, P.DEPOSIT, "2000", rid="cp-request-0002")
        self.assertNotEqual(first.pk, other.pk)
        first.refresh_from_db()
        self.assertEqual(first.status, S.CANCELLED)
        self.assertEqual(self.fake.sessions[first.session_id]["status"], "CANCELED")

    def test_an_older_page_that_was_paid_is_shown_instead_of_opening_another(self):
        booking = self.booking()
        first, _ = self.started(booking)
        self.fake.paid(first.session_id)
        response = self.start(booking, P.DEPOSIT, "2000", rid="cp-request-0002")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()["state"], response.json()["token"]), ("recorded", first.token))     # the money that came in, not a new page
        self.assertEqual(len(self.online_rows(booking)), 1)
        self.assertEqual(PaymentAttempt.objects.count(), 1)

    def test_a_code_another_staff_opens_at_the_same_instant_is_never_doubled(self):
        booking = self.booking()
        self.started(booking, P.DEPOSIT, "2000")
        real_cancel = online_checkout.cancel_attempt

        def cancel_while_someone_else_opens(attempt_id):
            closed = real_cancel(attempt_id)
            PaymentAttempt.objects.create(token="the-other-staff-code", source="shop", staff=self.other, reservation=booking, purpose="full",
                                          amount=D("5500"), expires_at=timezone.now() + timedelta(minutes=15))
            return closed

        with patch("gowns.online_checkout.cancel_attempt", side_effect=cancel_while_someone_else_opens):
            response = self.start(booking, rid="cp-request-0002")
        self.assertEqual(response.status_code, 409)
        self.assertIn("just being opened", response.json()["error"])
        self.assertEqual(list(PaymentAttempt.objects.filter(status=S.CREATED).values_list("token", flat=True)), ["the-other-staff-code"])


class PaidTests(Base):
    def test_once_xendit_confirms_the_payment_is_recorded_with_a_receipt_and_the_customer_told(self):
        booking = self.booking()
        attempt, _ = self.started(booking)
        body = self.pay(attempt, channel="PAYMAYA")
        self.assertEqual((body["state"], body["amount"], body["paidWith"], body["test"]), ("recorded", "5,500.00", "Maya", True))
        [payment] = self.online_rows(booking)
        self.assertEqual((payment.purpose, payment.method, payment.amount, payment.recorded_by, payment.recorded_by_name),
                         (P.FULL, Payment.Method.PM_MAYA, D("5500.00"), self.staff, "Maria Cruz"))
        self.assertRegex(payment.receipt_number, r"^PR-\d{4}-\d{6}$")
        self.assertEqual((body["receiptNumber"], body["paymentId"]), (payment.receipt_number, payment.pk))
        self.assertIn("TEST PAYMENT", payment.note)
        self.assertEqual(body["detail"]["peso"]["balance_due"], "0.00")
        self.assertEqual(self.stmt(booking).balance_due, D("0"))
        self.assertIn("Payment received", CustomerMessage.objects.filter(recipient=self.customer).latest("id").body)
        attempt.refresh_from_db()
        self.assertEqual((attempt.status, attempt.get_status_display()), (S.BOOKED, "Paid, booking made"))
        self.assertIn("the customer paid online on their phone", AuditLog.objects.get(action="payment.record").summary)
        # asking again, or the webhook arriving late, records nothing more
        self.poll(attempt)
        online_checkout.confirm_attempt(attempt.pk, source="webhook")
        self.assertEqual(Payment.objects.filter(reservation=booking).count(), 1)

    def test_the_balance_can_be_paid_in_parts(self):
        booking = self.booking()
        self.cash(booking, P.DEPOSIT, "2000")
        attempt, _ = self.started(booking, P.BALANCE, "1000")
        self.assertEqual(self.pay(attempt)["state"], "recorded")
        self.assertEqual(self.stmt(booking).balance_due, D("2500"))

    def test_fees_beyond_the_deposit_can_be_paid_online(self):
        booking = self.booking(stage=ReservationItem.Stage.RETURNED, returned_on=-2, ret=-2, pickup=-6)
        self.cash(booking, P.FULL, "5500")
        payments.add_deduction(reservation_id=booking.pk, amount=D("2500"), reason="torn lace", user=self.staff)
        attempt, _ = self.started(booking, P.EXTRA, "500")
        self.assertEqual(self.pay(attempt)["state"], "recorded")
        self.assertEqual(self.stmt(booking).extra_owed, D("0"))

    def test_a_deposit_paid_this_way_can_be_sent_back_online(self):
        booking = self.booking(stage=ReservationItem.Stage.RETURNED, returned_on=-2, ret=-2, pickup=-6)
        attempt, _ = self.started(booking, P.DEPOSIT, "2000")
        self.pay(attempt)
        choice = payments.online_refund_choice(self.fresh(booking), self.stmt(booking))
        self.assertEqual((choice["offered"], choice["available"], choice["label"]), (True, True, "GCash"))

    def test_the_webhook_records_it_too(self):
        booking = self.booking()
        attempt, _ = self.started(booking)
        self.fake.paid(attempt.session_id)
        event = {"event": "payment_session.completed", "data": {"payment_session_id": attempt.session_id, "status": "COMPLETED"}}
        response = Client().post(reverse("gowns:xendit_webhook"), data=json.dumps(event), content_type="application/json", HTTP_X_CALLBACK_TOKEN=TOKEN)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(self.online_rows(booking)), 1)

    def test_the_thank_you_page_records_it_when_no_staff_screen_is_open(self):
        booking = self.booking()
        attempt, _ = self.started(booking)
        self.fake.paid(attempt.session_id)
        page = Client().get(reverse("gowns:walkin_pay_done", args=[attempt.token]))
        self.assertContains(page, "The shop will give you your receipt.")
        self.assertNotContains(page, booking.reference_code)
        self.assertEqual(len(self.online_rows(booking)), 1)

    def test_the_booking_window_shows_the_open_code_and_records_a_payment_nobody_saw(self):
        booking = self.booking()
        self.assertIsNone(self.window(booking)["pay"])
        attempt, _ = self.started(booking)
        shown = self.window(booking)["pay"]
        self.assertEqual((shown["state"], shown["token"]), ("waiting", attempt.token))
        self.assertTrue(shown["qr"].startswith("data:image/png"))
        self.fake.paid(attempt.session_id)
        PaymentAttempt.objects.filter(pk=attempt.pk).update(last_checked_at=None)
        data = self.window(booking)
        self.assertEqual(data["pay"]["state"], "recorded")
        self.assertEqual(data["peso"]["balance_due"], "0.00")

    def test_a_page_that_ran_out_ends_and_is_not_shown_again(self):
        booking = self.booking()
        attempt, _ = self.started(booking)
        PaymentAttempt.objects.filter(pk=attempt.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.poll(attempt)["state"], "ended")
        self.assertIsNone(self.window(booking)["pay"])

    def test_cancel_closes_the_code(self):
        booking = self.booking()
        attempt, _ = self.started(booking)
        response = self.client.post(reverse("arabela_admin:payment_pay_cancel", args=[attempt.token]), data="{}", content_type="application/json")
        self.assertEqual(response.json()["state"], "ended")
        self.assertEqual(self.fake.sessions[attempt.session_id]["status"], "CANCELED")
        self.assertIn(booking.reference_code, AuditLog.objects.get(action="payment.pay_cancel").summary)


class CashAndRefundTests(Base):
    def test_cash_closes_the_open_code_first(self):
        booking = self.booking()
        attempt, _ = self.started(booking)
        response = self.record_cash(booking, P.FULL, "5500")
        self.assertEqual(response.status_code, 200, response.content)
        attempt.refresh_from_db()
        self.assertEqual((attempt.status, self.fake.sessions[attempt.session_id]["status"]), (S.CANCELLED, "CANCELED"))
        self.assertEqual(Payment.objects.get(reservation=booking).method, "Cash")

    def test_cash_is_not_taken_on_top_of_an_online_payment_that_just_arrived(self):
        booking = self.booking()
        attempt, _ = self.started(booking)
        self.fake.paid(attempt.session_id)
        response = self.record_cash(booking, P.FULL, "5500")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["refresh"])
        self.assertEqual([p.method for p in Payment.objects.filter(reservation=booking)], [Payment.Method.PM_GCASH])

    def test_money_that_is_not_needed_any_more_goes_back_and_the_customer_is_told(self):
        booking = self.booking()
        attempt, _ = self.started(booking)
        self.cash(booking, P.FULL, "5500")                          # paid another way while the code was open
        body = self.pay(attempt)
        self.assertEqual(body["state"], "refunded")
        self.assertIn("no longer needs that payment", body["message"])
        self.assertEqual(Payment.objects.filter(reservation=booking).count(), 1)
        [refund] = self.fake.refunds.values()
        self.assertEqual(refund["amount"], 5500)
        message = CustomerMessage.objects.filter(recipient=self.customer).latest("id").body
        self.assertIn(f"We could not accept your payment for {booking.reference_code}", message)
        self.assertNotIn("reserve your gown", message)

    def test_a_cancelled_booking_gets_its_money_back(self):
        booking = self.booking()
        attempt, _ = self.started(booking)
        Reservation.objects.filter(pk=booking.pk).update(status=Reservation.Status.CANCELLED)
        self.assertEqual(self.pay(attempt)["state"], "refunded")
        self.assertFalse(Payment.objects.filter(reservation=booking).exists())

    def test_money_that_cannot_be_sent_back_waits_for_a_person(self):
        booking = self.booking()
        attempt, _ = self.started(booking)
        self.cash(booking, P.FULL, "5500")
        self.fake.fail["create_refund"] = xendit.XenditError("QR Ph payments cannot be refunded", status=400)
        self.assertEqual(self.pay(attempt, channel="QRPH")["state"], "flagged")
        attempt.refresh_from_db()
        self.assertEqual((attempt.status, attempt.refund_status), (S.FLAGGED, "failed"))
        items = [i for i in build_items(self.staff) if i["kind"] == "payment" and i["key"] == f"online:{attempt.pk}"]
        self.assertEqual(len(items), 1)
        self.assertIn("payment is not recorded yet", str(items[0]))
        page = self.client.get(reverse("arabela_admin:payment_online") + "?show=all")
        self.assertContains(page, "At the shop &middot; Maria Cruz")

    def test_the_online_payments_list_says_recorded_for_it(self):
        booking = self.booking()
        attempt, _ = self.started(booking)
        self.pay(attempt)
        page = self.client.get(reverse("arabela_admin:payment_online") + "?show=all")
        self.assertContains(page, "Recorded")
        self.assertContains(page, "Paid in full")
        self.assertContains(page, "At the shop &middot; Maria Cruz")


class NoTypedGCashTests(Base):
    def post(self, name, args, body):
        return self.client.post(reverse(f"arabela_admin:{name}", args=args), data=json.dumps(body), content_type="application/json")

    def test_a_payment_and_a_deposit_return_take_cash_or_online_only(self):
        booking = self.booking(stage=ReservationItem.Stage.RETURNED, returned_on=-2, ret=-2, pickup=-6)
        response = self.post("payment_record", [booking.pk], {"purpose": P.FULL, "amount": "5500", "method": "GCash", "reference": "5046933479702"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Cash or Pay online", response.json()["error"])
        self.cash(booking, P.FULL, "5500")
        response = self.post("deposit_return", [booking.pk], {"amount": "2000", "method": "GCash", "reference": "5046933479702"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post("deposit_return", [booking.pk], {"amount": "2000", "method": "Cash"}).status_code, 200)

    def test_new_booking_refuses_typed_gcash(self):
        from reservations import staff_booking
        with self.assertRaises(staff_booking.BookingError) as caught:
            staff_booking.record_booking(recorded_by=self.staff, is_owner=False, staff_name="Maria", channel="Walk-in", name="A", phone="09171234567",
                                         account=None, address="", notebook_number="", lines=[], payment="full", method="GCash", reference="5046933479702",
                                         amount=D("1"), note="")
        self.assertIn("Cash or Pay online", str(caught.exception.message))

    def test_a_cash_paid_deposit_says_why_it_goes_back_in_cash(self):
        booking = self.booking(stage=ReservationItem.Stage.RETURNED, returned_on=-2, ret=-2, pickup=-6)
        self.cash(booking, P.FULL, "5500")
        choice = self.window(booking)["onlineRefund"]
        self.assertEqual((choice["offered"], choice["available"]), (False, False))
        self.assertIn("not paid online", choice["reason"])

    def test_a_balance_paid_online_does_not_offer_to_send_a_cash_deposit_back(self):
        booking = self.booking(stage=ReservationItem.Stage.RETURNED, returned_on=-2, ret=-2, pickup=-6)
        self.cash(booking, P.DEPOSIT, "2000")
        attempt, _ = self.started(booking, P.BALANCE, "3500")
        self.pay(attempt)
        choice = payments.online_refund_choice(self.fresh(booking), self.stmt(booking))
        self.assertEqual((choice["offered"], choice["available"]), (False, False))

    def test_the_pages_offer_cash_and_pay_online_only(self):
        new_booking = self.client.get(reverse("arabela_admin:new_booking")).content.decode()
        payments_page = self.client.get(reverse("arabela_admin:payments")).content.decode()
        self.assertIn('name="nb-method" value="Online"', new_booking)
        self.assertNotIn('value="GCash"', new_booking)
        self.assertNotIn("nb-ref", new_booking)
        self.assertIn('name="pm-rec-method" value="Online"', payments_page)
        for page in (new_booking, payments_page):
            self.assertNotIn("GCash reference", page)
        self.assertNotIn('name="pm-ret-method" value="GCash"', payments_page)
        with override_settings(XENDIT_SECRET_KEY="", PAYMENT_PROVIDER="xendit"):
            self.assertNotIn('name="pm-rec-method" value="Online"', self.client.get(reverse("arabela_admin:payments")).content.decode())

    def test_the_qr_screens_keep_the_instructions_in_the_question_mark(self):
        new_booking = (ADMIN / "new-booking.html").read_text(encoding="utf8")
        self.assertNotIn("Ask the customer to scan this code'", new_booking.split("get payTitle()")[1].split("get accountVerified")[0])
        self.assertIn("The customer scans this code with their phone", new_booking)
        payments_source = (ADMIN / "payments.html").read_text(encoding="utf8")
        self.assertIn("The customer scans this code with their phone", payments_source)


class AfterReturnTests(Base):
    """The pop-up after Mark as returned says the deposit's real next step (the numbers come from the answer)."""

    def returned(self, booking, item=None):
        item = item or booking.items.get()
        response = self.client.post(reverse("arabela_admin:reservation_item_mark_returned", args=[item.pk]),
                                    data=json.dumps({"condition": "Good"}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["money"]

    def test_the_deposit_is_to_be_handed_back_when_every_gown_is_back(self):
        booking = self.booking(stage=ReservationItem.Stage.RESERVED, pickup=-2, ret=0)
        self.cash(booking, P.FULL, "5500")
        money = self.returned(booking)
        self.assertEqual((money["allReturned"], money["toHandBack"], money["stillOwed"], money["depositPaid"]), (True, "2,000.00", "", True))
        self.assertEqual(money["paymentsUrl"], reverse("arabela_admin:payments") + f"?open={booking.pk}")

    def test_it_stays_held_while_another_gown_is_out(self):
        booking = self.booking(gowns=2, stage=ReservationItem.Stage.RESERVED, pickup=-2, ret=0)
        self.cash(booking, P.FULL, "5500")
        first = booking.items.order_by("id").first()
        self.assertFalse(self.returned(booking, first)["allReturned"])

    def test_fees_that_use_the_deposit_leave_nothing_to_hand_back(self):
        booking = self.booking(stage=ReservationItem.Stage.RESERVED, pickup=-20, ret=-12, rate=200)
        self.cash(booking, P.FULL, "5500")
        money = self.returned(booking)
        self.assertEqual((money["toHandBack"], money["depositPaid"], money["stillOwed"]), ("", True, "400.00"))

    def test_no_deposit_paid_says_what_is_owed(self):
        booking = self.booking(stage=ReservationItem.Stage.RESERVED, pickup=-2, ret=0)
        money = self.returned(booking)
        self.assertEqual((money["toHandBack"], money["depositPaid"], money["stillOwed"]), ("", False, "5,500.00"))

    def test_the_page_words_each_case(self):
        source = (ADMIN / "active-reservations.html").read_text(encoding="utf8")
        for words in ("stays held until every gown is back", "Next: hand back the", "used up the deposit", "No deposit was paid", "Open Payments", "Later"):
            self.assertIn(words, source)
        self.assertNotIn("now shows as returned in Security Deposits", source)
