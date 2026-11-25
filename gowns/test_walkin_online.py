"""A walk-in customer pays ON THEIR PHONE from a QR code (gowns/walkin_payment.py, arabela_admin/new_booking.py): the booking is made only when the money is
confirmed, through the same function a cash booking uses, with a numbered receipt that prints like any other. Xendit is replaced by the in-memory FakeXendit of
gowns/test_xendit_checkout.py, so nothing leaves the test."""
import json
import sys
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditLog, UserProfile
from accounts.test_support import agreed, id_rule_off
from gowns import online_checkout, walkin_payment
from gowns.models import Gown, GownUnavailability
from gowns.test_xendit_checkout import SETTINGS, FakeXendit
from reservations import ledger
from reservations.models import Payment, PaymentAttempt, Reservation, ReservationItem

User = get_user_model()
S = PaymentAttempt.Status


def make_gown(number, *, name="Walkin Gown", price="3000"):
    return Gown.objects.create(
        gown_id=f"Wedding Gown-WO-{number:03d}", name=name, category=Gown.Category.WEDDING_GOWN, color_name="White", color_code="WH",
        size=Gown.Size.MEDIUM, rental_price=Decimal(price), status=Gown.Status.AVAILABLE)


@override_settings(**SETTINGS)
class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("wo_staff", password="x", is_staff=True, first_name="Maria", last_name="Cruz")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.STAFF})
        cls.owner = User.objects.create_superuser("wo_owner", "wo.owner@gmail.com", "x")
        cls.customer = User.objects.create_user("wo_customer", email="wo.customer@gmail.com", password="x", first_name="Ana")
        UserProfile.objects.update_or_create(user=cls.customer, defaults={"display_name": "Ana Cruz"})
        cls.gown = make_gown(1, name="Walkin Gown One")
        cls.gown2 = make_gown(2, name="Walkin Gown Two", price="2500")

    def setUp(self):
        id_rule_off()                                  # about the walk-in payment; the ID rule has its own tests (arabela_admin/test_identity_admin.py)
        self.fake = FakeXendit()
        patcher = patch("reservations.xendit._request", side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.event = timezone.localdate() + timedelta(days=20)
        self.client.force_login(self.staff)

    def line(self, gown=None, event=None, takes_now=False):
        return {"gown": (gown or self.gown).id, "event": (event or self.event).isoformat(), "takes_now": takes_now, "picked_up_on": ""}

    def fields(self, lines=None, *, payment="full", amount=None, **extra):
        lines = [self.line()] if lines is None else lines
        gowns = [Gown.objects.get(id=line["gown"]) for line in lines]
        deposit_total = Decimal("2000") * len(gowns)
        due = deposit_total + sum((g.rental_price for g in gowns), Decimal("0")) if payment == "full" else deposit_total
        data = {"channel": "Walk-in", "name": "Lorna Reyes", "phone": "0917 123 4567", "address": "12 Mabini St", "city": "Antipolo", "postal_code": "1870",
                "lines": json.dumps(lines), "payment": payment, "note": "", "request_id": "wo-req-00000001", "expected_amount": str(due if amount is None else amount)}
        data.update(extra)
        return data

    def start(self, lines=None, *, client=None, **kwargs):
        return (client or self.client).post(reverse("arabela_admin:booking_pay_start"), data=self.fields(lines, **kwargs))

    def started(self, lines=None, **kwargs):
        response = self.start(lines, **kwargs)
        self.assertEqual(response.status_code, 200, response.content)
        return PaymentAttempt.objects.get(token=response.json()["token"]), response.json()

    def poll(self, attempt, client=None):
        PaymentAttempt.objects.filter(pk=attempt.pk).update(last_checked_at=None)
        response = (client or self.client).get(reverse("arabela_admin:booking_pay_status", args=[attempt.token]))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def pay(self, attempt, **kwargs):
        self.fake.paid(attempt.session_id, **kwargs)
        return self.poll(attempt)

    def cash_booking(self, lines, *, name="Somebody Else"):
        """A cash booking made by staff in between (takes the gown while the QR code is open)."""
        gowns = [Gown.objects.get(id=line["gown"]) for line in lines]
        amount = Decimal("2000") * len(gowns) + sum((g.rental_price for g in gowns), Decimal("0"))
        response = self.client.post(reverse("arabela_admin:booking_create"), data={
            "channel": "Walk-in", "name": name, "phone": "09170000000", "lines": json.dumps(lines), "payment": "full", "method": "Cash", "amount": str(amount)})
        self.assertEqual(response.status_code, 200, response.content)


class StartTests(Base):
    def test_save_with_pay_online_opens_a_payment_page_for_exactly_the_amount_and_books_nothing(self):
        attempt, body = self.started()
        self.assertTrue(body["success"])
        self.assertEqual((body["state"], body["amount"], body["minutes"]), ("waiting", "5,000.00", 15))
        self.assertTrue(body["qr"].startswith("data:image/png;base64,"))
        self.assertTrue(body["checkoutUrl"].startswith("https://checkout-staging.xendit.co/session/"))
        self.assertEqual((attempt.source, attempt.staff, attempt.customer, attempt.status, attempt.purpose, attempt.amount),
                         ("walkin", self.staff, None, S.CREATED, "full", Decimal("5000.00")))
        self.assertEqual(attempt.customer_name, "Lorna Reyes")
        sent = self.fake.sessions[attempt.session_id]["body"]
        self.assertEqual(sent["amount"], 5000)
        self.assertTrue(sent["success_return_url"].endswith(reverse("gowns:walkin_pay_done", args=[attempt.token])))
        self.assertAlmostEqual((attempt.expires_at - timezone.now()).total_seconds(), 900, delta=30)
        self.assertFalse(Reservation.objects.exists())                                     # nothing is booked while the customer is paying
        self.assertFalse(GownUnavailability.objects.exists())

    def test_deposit_only_asks_for_the_deposit_alone(self):
        attempt, body = self.started(payment="deposit")
        self.assertEqual((body["amount"], attempt.purpose, attempt.amount), ("2,000.00", "deposit", Decimal("2000.00")))
        self.assertEqual(self.fake.sessions[attempt.session_id]["body"]["amount"], 2000)

    def test_two_gowns_ask_for_both_rentals_and_both_deposits(self):
        attempt, _ = self.started([self.line(self.gown), self.line(self.gown2)])
        self.assertEqual(attempt.amount, Decimal("9500.00"))                                   # 3,000 + 2,500 rental, 2 x 2,000 deposit

    def test_the_form_is_refused_exactly_as_a_cash_booking_would_be(self):
        cases = [
            (dict(name=""), 400), (dict(phone="123"), 400), (dict(postal_code="18!70"), 400), (dict(postal_code="1" * 11), 400),
            (dict(city="x" * 101), 400), (dict(payment="nonsense"), 400),
        ]
        for extra, status in cases:
            with self.subTest(extra=extra):
                response = self.client.post(reverse("arabela_admin:booking_pay_start"), data=self.fields(**extra))
                self.assertEqual(response.status_code, status, response.content)
        for bad_lines in ("[]", "not json"):
            with self.subTest(lines=bad_lines):
                response = self.client.post(reverse("arabela_admin:booking_pay_start"), data={**self.fields(), "lines": bad_lines})
                self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_a_gown_that_leaves_the_shop_must_be_paid_in_full(self):
        response = self.start([self.line(takes_now=True)], payment="deposit", amount="2000")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_a_gown_that_is_already_taken_is_refused_with_nothing_opened(self):
        self.cash_booking([self.line()])
        response = self.start()
        self.assertEqual(response.status_code, 409)
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertEqual(self.fake.count("create_session"), 0)

    def test_a_changed_price_is_caught_before_the_customer_pays(self):
        response = self.start(amount="4000")
        self.assertEqual(response.status_code, 409)
        self.assertIn("5,000.00", response.json()["error"])
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_a_booking_too_large_for_one_online_payment_is_refused(self):
        gowns = [make_gown(10 + n, name=f"Big {n}", price="9000") for n in range(6)]
        response = self.start([self.line(g) for g in gowns])
        self.assertEqual(response.status_code, 400)
        self.assertIn("too large", response.json()["error"])

    def test_a_notebook_number_already_used_is_refused(self):
        Reservation.objects.create(customer=None, customer_name="Old", phone="09170000001", channel=Reservation.Channel.WALK_IN, notebook_number="NB-7",
                                   status=Reservation.Status.CONFIRMED, rental_subtotal=Decimal("1"), security_deposit=Decimal("1"), total_amount=Decimal("2"))
        self.assertEqual(self.start(notebook_number="NB-7").status_code, 409)

    def test_it_is_off_when_online_payment_is_not_set_up(self):
        with override_settings(XENDIT_SECRET_KEY="", PAYMENT_PROVIDER="", PAYMONGO_SECRET_KEY=""):
            response = self.start()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_only_signed_in_staff_can_open_one(self):
        customer = Client()
        customer.force_login(self.customer)
        self.assertEqual(self.start(client=customer).status_code, 401)
        self.assertEqual(self.start(client=Client()).status_code, 401)
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_the_gateway_being_down_leaves_nothing_open(self):
        self.fake.fail["create_session"] = __import__("reservations.xendit", fromlist=["x"]).XenditError("down", status=503)
        response = self.start()
        self.assertEqual(response.status_code, 502)
        attempt = PaymentAttempt.objects.get()
        self.assertEqual(attempt.status, S.EXPIRED)
        self.assertFalse(Reservation.objects.exists())

    def test_the_qr_code_is_drawn_here_and_a_missing_library_only_hides_it(self):
        with patch.dict(sys.modules, {"segno": None}):
            attempt, body = self.started()
        self.assertEqual(body["qr"], "")
        self.assertTrue(body["checkoutUrl"])                                                 # the link is still offered on the screen


class BookingMadeTests(Base):
    def test_when_the_money_is_confirmed_the_booking_is_made_with_every_detail_and_a_receipt(self):
        attempt, _ = self.started(request_id="wo-req-00000002")
        state = self.pay(attempt, channel="PAYMAYA")
        self.assertEqual(state["state"], "booked")
        reservation = Reservation.objects.get()
        self.assertEqual((reservation.channel, reservation.status, reservation.customer, reservation.recorded_by),
                         (Reservation.Channel.WALK_IN, Reservation.Status.CONFIRMED, None, self.staff))
        self.assertEqual((reservation.customer_name, reservation.phone, reservation.address, reservation.city, reservation.postal_code),
                         ("Lorna Reyes", "09171234567", "12 Mabini St", "Antipolo", "1870"))
        self.assertEqual((reservation.rental_subtotal, reservation.security_deposit, reservation.total_amount), (Decimal("3000"), Decimal("2000"), Decimal("5000")))
        self.assertEqual(reservation.payment_method, Reservation.PaymentMethod.MAYA)
        item = reservation.items.get()
        self.assertEqual((item.gown, item.stage, item.return_date), (self.gown, ReservationItem.Stage.PICKUP, self.event + timedelta(days=2)))
        self.assertTrue(GownUnavailability.objects.filter(auto_for_item=item, reason=GownUnavailability.Reason.COOLDOWN).exists())
        self.gown.refresh_from_db()
        self.assertEqual(self.gown.status, Gown.Status.RESERVED)
        attempt.refresh_from_db()
        self.assertEqual((attempt.status, attempt.reservation_id), (S.BOOKED, reservation.pk))

    def test_the_receipt_is_numbered_names_the_staff_member_and_prints(self):
        attempt, _ = self.started(request_id="wo-req-00000003")
        state = self.pay(attempt, channel="GCASH")
        payment = Payment.objects.get()
        self.assertEqual((payment.direction, payment.purpose, payment.method, payment.amount, payment.provider), ("In", "Paid in full", Payment.Method.PM_GCASH, Decimal("5000.00"), "xendit"))
        self.assertEqual((payment.recorded_by, payment.recorded_by_name), (self.staff, "Maria Cruz"))
        self.assertRegex(payment.receipt_number, r"^PR-\d{4}-\d{6}$")
        self.assertEqual(payment.snapshot["served_by"], "Maria Cruz")
        self.assertEqual(payment.snapshot["customer"], "Lorna Reyes")
        self.assertEqual(state["receiptNumber"], payment.receipt_number)
        self.assertEqual(state["receiptUrl"], reverse("arabela_admin:payment_receipt_print", args=[payment.pk]))
        page = self.client.get(state["receiptUrl"])
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, payment.receipt_number)
        self.assertContains(page, "Lorna Reyes")
        self.assertContains(page, "Maria Cruz")
        owner = Client()
        owner.force_login(self.owner)
        self.assertIn(payment.receipt_number, owner.get(reverse("arabela_admin:payment_receipts")).content.decode())       # the owner's list of every receipt

    def test_the_screen_is_told_everything_it_shows_after_a_full_payment(self):
        attempt, _ = self.started(request_id="wo-req-00000004")
        state = self.pay(attempt)
        reservation = Reservation.objects.get()
        self.assertEqual((state["reference_code"], state["depositOnly"], state["balance"], state["paidWith"], state["paidAmount"]),
                         (reservation.reference_code, False, "0.00", "GCash", "5,000.00"))
        self.assertIn(reservation.reference_code, state["url"])
        self.assertIn(f"open={reservation.pk}", state["paymentsUrl"])

    def test_a_deposit_only_payment_leaves_the_rental_to_pay_at_the_shop(self):
        attempt, _ = self.started(payment="deposit", request_id="wo-req-00000005")
        state = self.pay(attempt)
        self.assertEqual((state["state"], state["depositOnly"], state["balance"]), ("booked", True, "3,000.00"))
        reservation = Reservation.objects.get()
        statement = ledger.statement(reservation)
        self.assertEqual((statement.balance_due, statement.deposit_paid, statement.received), (Decimal("3000.00"), Decimal("2000.00"), Decimal("2000.00")))
        self.assertEqual(Payment.objects.get().purpose, "Deposit")

    def test_a_gown_taken_home_now_is_recorded_as_picked_up(self):
        attempt, _ = self.started([self.line(event=timezone.localdate() + timedelta(days=2), takes_now=True)], request_id="wo-req-00000006")
        self.pay(attempt)
        item = ReservationItem.objects.get()
        self.assertEqual(item.stage, ReservationItem.Stage.RESERVED)
        self.assertEqual(item.picked_up_on, timezone.localdate())

    def test_the_webhook_makes_the_booking_even_if_nobody_is_looking_at_the_screen(self):
        attempt, _ = self.started(request_id="wo-req-00000007")
        self.fake.paid(attempt.session_id)
        online_checkout.confirm_attempt(attempt.pk, source="webhook")
        self.assertEqual(Reservation.objects.count(), 1)
        self.assertEqual(self.poll(attempt)["state"], "booked")                              # the screen, opened later, shows it

    def test_every_way_of_hearing_about_the_payment_books_it_once(self):
        attempt, _ = self.started(request_id="wo-req-00000008")
        self.fake.paid(attempt.session_id)
        self.poll(attempt)
        self.poll(attempt)
        online_checkout.confirm_attempt(attempt.pk, source="webhook")
        online_checkout.confirm_attempt(attempt.pk, source="sweep")
        self.assertEqual((Reservation.objects.count(), Payment.objects.count(), ReservationItem.objects.count()), (1, 1, 1))

    def test_the_audit_log_names_who_opened_it_and_what_was_booked(self):
        attempt, _ = self.started(request_id="wo-req-00000009")
        self.pay(attempt)
        self.assertEqual(AuditLog.objects.filter(action="booking.pay_start").count(), 1)
        line = AuditLog.objects.get(action="booking.create")
        self.assertEqual(line.actor, self.staff)
        self.assertIn(Reservation.objects.get().reference_code, line.summary)
        self.assertIn("paid online", line.summary)

    def test_the_booking_shows_who_started_the_payment_in_its_staff_timeline(self):
        attempt, _ = self.started(request_id="wo-req-00000010")
        self.pay(attempt)
        reservation = Reservation.objects.get()
        notes = " ".join(reservation.status_events.filter(staff_only=True).values_list("detail", flat=True))
        self.assertIn("Maria Cruz", notes)
        self.assertIn("paid online", notes.lower())

    def test_the_deposit_can_be_sent_back_through_xendit_like_any_online_payment(self):
        from reservations import payments
        attempt, _ = self.started(request_id="wo-req-00000011")
        self.pay(attempt, channel="GCASH")
        reservation = Reservation.objects.get()
        ReservationItem.objects.filter(reservation=reservation).update(stage=ReservationItem.Stage.RETURNED)
        refund = payments.return_deposit_online(reservation_id=reservation.pk, amount=Decimal("2000"), user=self.owner)
        self.assertEqual((refund.direction, refund.purpose, refund.method, refund.amount), ("Out", "Deposit return", Payment.Method.PM_GCASH, Decimal("2000.00")))
        self.assertEqual(len(self.fake.refunds), 1)
        self.assertEqual(list(self.fake.refunds.values())[0]["amount"], 2000)
        self.assertEqual(ledger.statement(Reservation.objects.get(pk=reservation.pk)).refund_available, Decimal("0.00"))


class WhileThePhoneIsPayingTests(Base):
    def test_the_status_says_waiting_with_the_seconds_left(self):
        attempt, _ = self.started(request_id="wo-req-00000012")
        state = self.poll(attempt)
        self.assertEqual(state["state"], "waiting")
        self.assertGreater(state["secondsLeft"], 800)

    def test_pressing_save_twice_is_the_same_payment_page(self):
        first, body1 = self.started(request_id="wo-req-00000013")
        again, body2 = self.started(request_id="wo-req-00000013")
        self.assertEqual((first.pk, body1["checkoutUrl"]), (again.pk, body2["checkoutUrl"]))
        self.assertEqual((PaymentAttempt.objects.count(), self.fake.count("create_session")), (1, 1))

    def test_a_refreshed_screen_closes_the_old_page_for_the_same_gowns_and_opens_one(self):
        first, _ = self.started(request_id="wo-req-00000014")
        second, _ = self.started(request_id="wo-req-00000015")
        first.refresh_from_db()
        self.assertEqual((first.status, second.status), (S.CANCELLED, S.CREATED))
        self.assertEqual(self.fake.sessions[first.session_id]["status"], "CANCELED")

    def test_a_refreshed_screen_after_the_old_page_was_paid_shows_the_booking_not_a_second_payment(self):
        first, _ = self.started(request_id="wo-req-00000016")
        self.fake.paid(first.session_id)
        body = self.start(request_id="wo-req-00000017").json()
        self.assertEqual((body["state"], body["token"]), ("booked", first.token))
        self.assertEqual((PaymentAttempt.objects.count(), Reservation.objects.count(), self.fake.count("create_session")), (1, 1, 1))

    def test_a_payment_page_for_another_gown_is_left_alone(self):
        first, _ = self.started(request_id="wo-req-00000018")
        self.started([self.line(self.gown2)], request_id="wo-req-00000019")
        first.refresh_from_db()
        self.assertEqual(first.status, S.CREATED)

    def test_pressing_save_again_after_the_page_closed_opens_a_new_one(self):
        first, _ = self.started(request_id="wo-req-00000020")
        self.client.post(reverse("arabela_admin:booking_pay_cancel", args=[first.token]), data="{}", content_type="application/json")
        again, body = self.started(request_id="wo-req-00000020")
        self.assertNotEqual(again.pk, first.pk)
        self.assertEqual(body["state"], "waiting")

    def test_staff_can_close_the_code_and_nothing_is_booked_or_charged(self):
        attempt, _ = self.started(request_id="wo-req-00000021")
        response = self.client.post(reverse("arabela_admin:booking_pay_cancel", args=[attempt.token]), data="{}", content_type="application/json")
        self.assertEqual((response.status_code, response.json()["state"]), (200, "ended"))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, S.CANCELLED)
        self.assertEqual(self.fake.sessions[attempt.session_id]["status"], "CANCELED")
        self.assertFalse(Reservation.objects.exists())
        self.assertEqual(self.poll(attempt)["state"], "ended")
        self.assertEqual(AuditLog.objects.filter(action="booking.pay_cancel").count(), 1)

    def test_closing_a_code_the_customer_already_paid_honours_the_payment(self):
        attempt, _ = self.started(request_id="wo-req-00000022")
        self.fake.paid(attempt.session_id)
        response = self.client.post(reverse("arabela_admin:booking_pay_cancel", args=[attempt.token]), data="{}", content_type="application/json")
        self.assertEqual(response.json()["state"], "booked")
        self.assertEqual(Reservation.objects.count(), 1)

    def test_a_page_that_ran_out_is_closed_by_the_screens_next_look(self):
        attempt, _ = self.started(request_id="wo-req-00000023")
        PaymentAttempt.objects.filter(pk=attempt.pk).update(expires_at=timezone.now() - timedelta(seconds=5))
        self.assertEqual(self.poll(attempt)["state"], "ended")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, S.CANCELLED)

    def test_the_sweep_closes_a_forgotten_one(self):
        attempt, _ = self.started(request_id="wo-req-00000024")
        PaymentAttempt.objects.filter(pk=attempt.pk).update(expires_at=timezone.now() - timedelta(minutes=5))
        self.assertEqual(online_checkout.expire_stale(), 1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, S.EXPIRED)

    def test_the_wrong_amount_is_flagged_for_a_person_and_never_booked(self):
        attempt, _ = self.started(request_id="wo-req-00000025")
        state = self.pay(attempt, amount=1000)
        self.assertEqual(state["state"], "flagged")
        self.assertFalse(Reservation.objects.exists())

    def test_a_dropped_gateway_read_is_just_still_waiting(self):
        attempt, _ = self.started(request_id="wo-req-00000026")
        self.fake.fail["read_session"] = __import__("reservations.xendit", fromlist=["x"]).XenditError("down", status=503)
        try:
            self.assertEqual(self.poll(attempt)["state"], "waiting")
        finally:
            self.fake.fail.pop("read_session", None)


class GownTakenWhilePayingTests(Base):
    def test_the_whole_payment_goes_back_and_the_screen_says_why(self):
        attempt, _ = self.started(request_id="wo-req-00000027")
        self.cash_booking([self.line()])                                                       # staff booked the same gown for someone else meanwhile
        state = self.pay(attempt, channel="PAYMAYA")
        self.assertEqual(state["state"], "refunded")
        self.assertEqual(state["amount"], "5,000.00")
        self.assertIn("already taken", state["message"])
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, S.REFUNDED)
        self.assertEqual(len(self.fake.refunds), 1)
        self.assertEqual(list(self.fake.refunds.values())[0]["amount"], 5000)
        self.assertEqual(Reservation.objects.filter(customer_name="Lorna Reyes").count(), 0)   # no booking for this customer
        self.assertEqual(Payment.objects.filter(provider="xendit").count(), 0)

    def test_another_booking_never_loses_its_gown_to_a_late_payment(self):
        attempt, _ = self.started(request_id="wo-req-00000028")
        self.cash_booking([self.line()])
        self.pay(attempt)
        self.assertEqual(ReservationItem.objects.filter(gown=self.gown).count(), 1)
        self.assertEqual(Reservation.objects.get(customer_name="Somebody Else").items.count(), 1)


class IsolationTests(Base):
    def test_a_customer_can_never_open_a_shop_payment(self):
        attempt, _ = self.started(request_id="wo-req-00000029")
        customer = Client()
        customer.force_login(self.customer)
        agreed(self.customer)
        self.assertEqual(customer.get(reverse("gowns:payment_return", args=[attempt.token])).status_code, 404)
        self.assertEqual(customer.get(reverse("gowns:payment_status", args=[attempt.token])).status_code, 404)
        self.assertEqual(customer.get(reverse("gowns:payment_cancel", args=[attempt.token])).status_code, 404)
        self.assertEqual(customer.get(reverse("arabela_admin:booking_pay_status", args=[attempt.token])).status_code, 401)
        self.assertEqual(Client().get(reverse("arabela_admin:booking_pay_status", args=[attempt.token])).status_code, 401)

    def test_a_customers_own_checkout_never_closes_a_shop_payment(self):
        attempt, _ = self.started(request_id="wo-req-00000030")
        customer = Client()
        customer.force_login(self.customer)
        agreed(self.customer)
        response = customer.post(reverse("gowns:pay_start"), data={
            "items": json.dumps([{"gown_name": self.gown2.name, "gown_slug": self.gown2.slug, "size": "Medium", "rental_date": (self.event - timedelta(days=2)).isoformat(),
                                  "return_date": (self.event + timedelta(days=2)).isoformat()}]),
            "pay_option": "deposit", "first_name": "Ana", "last_name": "Cruz", "phone": "09171234567", "address": "1 St", "city": "Antipolo",
            "postal_code": "1870", "expected_deposit": "2000"})
        self.assertEqual(response.status_code, 200, response.content)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, S.CREATED)

    def test_a_website_checkout_token_is_not_a_shop_payment(self):
        customer = Client()
        customer.force_login(self.customer)
        agreed(self.customer)
        body = customer.post(reverse("gowns:pay_start"), data={
            "items": json.dumps([{"gown_name": self.gown2.name, "gown_slug": self.gown2.slug, "size": "Medium", "rental_date": (self.event - timedelta(days=2)).isoformat(),
                                  "return_date": (self.event + timedelta(days=2)).isoformat()}]),
            "pay_option": "deposit", "first_name": "Ana", "last_name": "Cruz", "phone": "09171234567", "address": "1 St", "city": "Antipolo",
            "postal_code": "1870", "expected_deposit": "2000"}).json()
        self.assertEqual(self.client.get(reverse("arabela_admin:booking_pay_status", args=[body["token"]])).status_code, 404)
        self.assertEqual(Client().get(reverse("gowns:walkin_pay_done", args=[body["token"]])).status_code, 404)
        self.assertEqual(self.client.get(reverse("arabela_admin:booking_pay_status", args=["nonsense"])).status_code, 404)

    def test_the_page_the_customers_phone_lands_on_needs_no_sign_in_and_shows_no_details(self):
        attempt, _ = self.started(request_id="wo-req-00000031")
        page = Client().get(reverse("gowns:walkin_pay_done", args=[attempt.token]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Thank you")
        for secret in ("Lorna", "5,000", "5000", "0917", "Mabini", attempt.token):
            self.assertNotContains(page, secret)
        self.assertEqual(Client().get(reverse("gowns:walkin_pay_done", args=["x" * 32])).status_code, 404)


class FormAndPageTests(Base):
    def test_the_customer_details_the_website_collects_are_on_the_form_and_saved_with_a_cash_booking_too(self):
        response = self.client.post(reverse("arabela_admin:booking_create"), data={
            "channel": "Walk-in", "name": "Cora Diaz", "phone": "09171234567", "address": "5 Rizal Ave", "city": "Cainta", "postal_code": "1900",
            "lines": json.dumps([self.line()]), "payment": "full", "method": "Cash", "amount": "5000"})
        self.assertEqual(response.status_code, 200, response.content)
        reservation = Reservation.objects.get()
        self.assertEqual((reservation.address, reservation.city, reservation.postal_code), ("5 Rizal Ave", "Cainta", "1900"))
        self.assertEqual(reservation.payment_method, "")                                       # unchanged: a counter payment names no online way

    def test_a_bad_postal_code_is_refused_for_cash_too(self):
        response = self.client.post(reverse("arabela_admin:booking_create"), data={
            "channel": "Walk-in", "name": "Cora Diaz", "phone": "09171234567", "postal_code": "19 00!", "lines": json.dumps([self.line()]),
            "payment": "full", "method": "Cash", "amount": "5000"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Reservation.objects.exists())

    def test_the_page_offers_city_postal_code_and_pay_online(self):
        html = self.client.get(reverse("arabela_admin:new_booking")).content.decode()
        for needle in ('id="nb-city"', 'id="nb-postal"', 'value="Online"', "Pay online", "Show payment QR", 'class="nb-qr"', "api/bookings/pay/start/"):
            self.assertIn(needle, html)

    def test_pay_online_is_not_offered_when_online_payment_is_off(self):
        with override_settings(XENDIT_SECRET_KEY="", PAYMENT_PROVIDER="", PAYMONGO_SECRET_KEY=""):
            html = self.client.get(reverse("arabela_admin:new_booking")).content.decode()
        self.assertNotIn('value="Online"', html)
        self.assertIn('id="nb-city"', html)

    def test_the_online_payments_list_marks_the_ones_started_at_the_shop(self):
        attempt, _ = self.started(request_id="wo-req-00000032")
        html = self.client.get(reverse("arabela_admin:payment_online") + "?show=all").content.decode()
        self.assertIn("Lorna Reyes", html)
        self.assertIn("At the shop", html)
        self.assertIn("Maria Cruz", html)
