"""Handing the security deposit back AUTOMATICALLY through PayMongo (reservations/payments.py return_deposit_online), for a booking the customer
paid on PayMongo's page. The rule that matters most: money never leaves without a record, and a refund PayMongo refuses leaves nothing behind."""
import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal

from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomerMessage, UserProfile
from arabela_admin import test_online_payments_admin as helpers
from reservations import ledger, payments, paymongo
from reservations.models import Deduction, Payment, ReservationItem, ReservationStatusEvent


class Base(helpers.Base):
    def returned_booking(self, option="deposit", channel="gcash"):
        """A booking paid on PayMongo whose gown is back."""
        booking = self.booked(option, channel)
        today = timezone.localdate()
        ReservationItem.objects.filter(reservation=booking).update(stage=ReservationItem.Stage.RETURNED, returned_on=today, return_date=today, original_return_date=today)
        return booking

    def refund(self, booking, amount, **kwargs):
        return payments.return_deposit_online(reservation_id=booking.pk, amount=Decimal(amount), user=self.owner, **kwargs)

    def age_payment(self, booking, days):
        Payment.objects.filter(reservation=booking, direction="In").update(paid_at=timezone.now() - timedelta(days=days))


class AutomaticRefundTests(Base):
    def test_the_form_offers_it_for_a_booking_paid_on_paymongo_and_not_for_one_paid_at_the_shop(self):
        booking = self.returned_booking()
        choice = payments.online_refund_choice(booking, ledger.statement(booking))
        self.assertEqual(choice, {"offered": True, "available": True, "label": "GCash", "reason": ""})
        cash = helpers.Reservation.objects.create(customer=self.ana, customer_name="Cash Customer", status="Confirmed", channel="Walk-in",
                                                  rental_subtotal=Decimal("3500"), security_deposit=Decimal("2000"), total_amount=Decimal("5500"), reviewed_at=timezone.now())
        payments.record_payment(reservation_id=cash.pk, purpose="Deposit", amount=Decimal("2000"), method="Cash", user=self.owner)
        cash_choice = payments.online_refund_choice(cash, ledger.statement(cash))
        self.assertEqual((cash_choice["offered"], cash_choice["available"]), (False, False))
        self.assertIn("not paid online", cash_choice["reason"])

    def test_it_is_not_offered_when_automatic_refunds_are_switched_off_or_there_is_no_key(self):
        booking = self.returned_booking()
        with override_settings(PAYMONGO_AUTO_REFUND=False):
            self.assertFalse(payments.online_refund_choice(booking, ledger.statement(booking))["offered"])
            with self.assertRaises(payments.PaymentError):
                self.refund(booking, "2000")
        with override_settings(PAYMONGO_SECRET_KEY=""):
            self.assertIn("switched off", payments.online_refund_choice(booking, ledger.statement(booking))["reason"])
        self.assertEqual(self.fake.refunds, {})

    def test_the_deposit_goes_back_with_a_receipt_a_refund_a_message_and_a_timeline_line(self):
        booking = self.returned_booking("deposit", "paymaya")
        self.age_payment(booking, 2)
        paid = Payment.objects.get(reservation=booking, direction="In")
        payment = self.refund(booking, "2000", request_id="ret-0001-aaaa")
        self.assertEqual((payment.direction, payment.purpose, payment.method, payment.amount), ("Out", "Deposit return", Payment.Method.PM_MAYA, Decimal("2000.00")))
        self.assertEqual((payment.provider, payment.provider_ref), ("paymongo", "ref_0001"))
        self.assertRegex(payment.receipt_number, r"^PR-\d{4}-\d{6}$")
        self.assertEqual(self.fake.refunds["ref_0001"]["payment_id"], paid.provider_ref)
        self.assertEqual(self.fake.refunds["ref_0001"]["amount"], 200000)
        self.assertIn(booking.reference_code, self.fake.refunds["ref_0001"]["notes"])
        statement = ledger.statement(helpers.Reservation.objects.get(pk=booking.pk))
        self.assertEqual((statement.refunded, statement.refund_available), (Decimal("2000.00"), Decimal("0.00")))
        message = CustomerMessage.objects.filter(recipient=self.ana, category="Payment").latest("id")
        self.assertIn("returned (Maya online)", message.body)
        self.assertIn("up to a day", message.body)
        self.assertTrue(ReservationStatusEvent.objects.filter(reservation=booking, label="Security deposit returned").exists())
        self.assertEqual(payment.snapshot["return"]["returned_now"], "2000.00")

    def test_only_what_the_system_says_is_refundable_goes_back_a_deduction_makes_it_a_partial_refund(self):
        booking = self.returned_booking("full", "qrph")
        Deduction.objects.create(reservation=booking, amount=Decimal("300"), reason="Stain")
        with self.assertRaises(payments.PaymentError):
            self.refund(booking, "2000")                                                  # more than the 1,700 that is refundable
        payment = self.refund(booking, "1700")
        self.assertEqual(self.fake.refunds["ref_0001"]["amount"], 170000)                 # a PART of the 5,500 payment: only the deposit's share
        self.assertEqual(payment.method, Payment.Method.PM_QRPH)

    def test_the_deposit_cannot_go_back_before_every_gown_is_back(self):
        booking = self.booked()
        with self.assertRaises(payments.PaymentError) as caught:
            self.refund(booking, "2000")
        self.assertIn("every gown", str(caught.exception.message))
        self.assertEqual(self.fake.refunds, {})

    def test_a_refund_paymongo_refuses_leaves_nothing_behind_and_can_be_tried_again(self):
        booking = self.returned_booking()
        self.age_payment(booking, 2)
        self.fake.fail["create_refund"] = paymongo.PaymongoError("payment_not_refundable", status=400)
        with self.assertRaises(payments.PaymentError) as caught:
            self.refund(booking, "2000")
        self.assertEqual(caught.exception.status, 502)
        self.assertIn("Nothing was sent or recorded", caught.exception.message)
        rows = Payment.objects.filter(reservation=booking, direction="Out")
        self.assertEqual([(p.status, p.provider_ref) for p in rows], [("Void", "")])
        self.assertEqual(rows[0].voided_by_name, "System")
        self.assertEqual(ledger.statement(helpers.Reservation.objects.get(pk=booking.pk)).refund_available, Decimal("2000.00"))      # the deposit is still to hand back
        self.assertFalse(CustomerMessage.objects.filter(recipient=self.ana, body__contains="returned").exists())                       # the customer was never told
        del self.fake.fail["create_refund"]
        self.refund(booking, "2000")
        self.assertEqual(len(self.fake.refunds), 1)

    def test_pressing_twice_with_the_same_request_sends_the_money_once(self):
        booking = self.returned_booking()
        self.age_payment(booking, 2)
        first = self.refund(booking, "2000", request_id="ret-0002-bbbb")
        second = self.refund(booking, "2000", request_id="ret-0002-bbbb")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(len(self.fake.refunds), 1)

    def test_a_row_that_never_reached_paymongo_is_never_sent_again_by_itself(self):
        booking = self.returned_booking()
        self.age_payment(booking, 2)
        Payment.objects.create(reservation=booking, direction="Out", purpose="Deposit return", method=Payment.Method.PM_GCASH, amount=Decimal("2000"),
                               provider="paymongo", provider_ref="", request_id="ret-0003-cccc", receipt_number="PR-2026-009999")
        with self.assertRaises(payments.PaymentError) as caught:
            self.refund(booking, "2000", request_id="ret-0003-cccc")
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("did not finish", caught.exception.message)
        self.assertEqual(self.fake.refunds, {})                                           # PayMongo was not asked again: it could send the money twice

    def test_a_second_refund_after_the_deposit_is_back_is_refused(self):
        booking = self.returned_booking()
        self.age_payment(booking, 2)
        self.refund(booking, "2000")
        with self.assertRaises(payments.PaymentError):
            self.refund(booking, "2000")
        self.assertEqual(len(self.fake.refunds), 1)

    def test_it_can_only_be_asked_inside_the_time_limit_of_the_way_the_customer_paid(self):
        for channel, days_ok, days_late in (("gcash", 169, 171), ("paymaya", 349, 351), ("qrph", 27, 29)):
            with self.subTest(channel=channel):
                booking = self.returned_booking("deposit", channel)
                self.age_payment(booking, days_late)
                choice = payments.online_refund_choice(booking, ledger.statement(booking))
                self.assertFalse(choice["available"])
                self.assertIn("too long", choice["reason"])
                with self.assertRaises(payments.PaymentError):
                    self.refund(booking, "2000")
                self.age_payment(booking, days_ok)
                self.assertTrue(payments.online_refund_choice(booking, ledger.statement(booking))["available"])
        self.assertEqual(self.fake.refunds, {})

    def test_maya_sends_back_part_of_a_payment_only_from_the_next_day(self):
        full = self.returned_booking("full", "paymaya")                                   # a partial refund (the deposit of a 5,500 payment), paid today
        choice = payments.online_refund_choice(full, ledger.statement(full))
        self.assertFalse(choice["available"])
        self.assertIn("day after", choice["reason"])
        with self.assertRaises(payments.PaymentError):
            self.refund(full, "2000")
        self.age_payment(full, 1)
        self.assertTrue(payments.online_refund_choice(full, ledger.statement(full))["available"])
        self.refund(full, "2000")
        deposit_only = self.returned_booking("deposit", "paymaya")                        # the WHOLE payment back on the same day is allowed
        self.assertTrue(payments.online_refund_choice(deposit_only, ledger.statement(deposit_only))["available"])


class RefundUpdateTests(Base):
    def make(self):
        booking = self.returned_booking()
        self.age_payment(booking, 2)
        return booking, self.refund(booking, "2000")

    def test_a_refund_that_succeeds_changes_nothing(self):
        booking, payment = self.make()
        self.assertTrue(payments.on_online_refund_updated(payment.provider_ref, "succeeded"))
        payment.refresh_from_db()
        self.assertFalse(payment.is_void)

    def test_a_refund_that_fails_cancels_the_return_tells_the_customer_and_leaves_a_staff_note(self):
        booking, payment = self.make()
        self.assertTrue(payments.on_online_refund_updated(payment.provider_ref, "failed"))
        payment.refresh_from_db()
        self.assertTrue(payment.is_void)
        self.assertEqual(payment.voided_by_name, "PayMongo")
        self.assertIn("Hand the deposit back another way", payment.void_reason)
        self.assertEqual(ledger.statement(helpers.Reservation.objects.get(pk=booking.pk)).refund_available, Decimal("2000.00"))
        self.assertIn("could not send your security deposit", CustomerMessage.objects.filter(recipient=self.ana).latest("id").body)
        self.assertTrue(ReservationStatusEvent.objects.filter(reservation=booking, label="Automatic deposit refund failed", staff_only=True).exists())
        self.assertTrue(payments.on_online_refund_updated(payment.provider_ref, "failed"))                     # a repeat changes nothing more
        self.assertEqual(CustomerMessage.objects.filter(recipient=self.ana, body__contains="could not send your security deposit").count(), 1)

    def test_a_failed_refund_rings_the_staff_bell_until_the_deposit_is_handed_back_another_way(self):
        from arabela_admin import notifications
        booking, payment = self.make()
        payments.on_online_refund_updated(payment.provider_ref, "failed")
        key = f"refundfailed:{payment.pk}"
        items = [i for i in notifications.build_items(self.owner) if i["key"] == key]
        self.assertEqual(len(items), 1)
        self.assertIn(booking.reference_code, items[0]["title"])
        self.assertIn("could not send back", items[0]["title"])
        payments.return_deposit(reservation_id=booking.pk, amount=Decimal("2000"), method="Cash", user=self.owner)
        self.assertFalse([i for i in notifications.build_items(self.owner) if i["key"] == key])      # handed back by hand: nothing left to do

    def test_a_refund_that_is_not_ours_is_ignored(self):
        self.make()
        self.assertFalse(payments.on_online_refund_updated("ref_unknown", "failed"))

    def test_the_webhook_carries_it_end_to_end(self):
        booking, payment = self.make()
        body = json.dumps({"data": {"id": "evt_9", "type": "event", "attributes": {"type": "payment.refund.updated", "livemode": False, "data": {
            "id": payment.provider_ref, "type": "refund", "attributes": {"status": "failed", "amount": 200000, "payment_id": "pay_x"}}}}}).encode()
        digest = hmac.new(b"whsk_unit", b"1700000000." + body, hashlib.sha256).hexdigest()
        response = Client().post(reverse("gowns:paymongo_webhook"), data=body, content_type="application/json", HTTP_PAYMONGO_SIGNATURE=f"t=1700000000,te={digest},li=")
        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.assertTrue(payment.is_void)


class RefundThroughThePaymentsPageTests(Base):
    def test_the_booking_window_offers_it_and_the_form_sends_it(self):
        booking = self.returned_booking()
        self.age_payment(booking, 2)
        self.client.force_login(self.staff)                                               # staff the owner allowed to return money
        UserProfile.objects.update_or_create(user=self.staff, defaults={"role": UserProfile.Role.STAFF, "permissions": "return_money"})
        detail = self.client.get(reverse("arabela_admin:payment_detail", args=[booking.pk])).json()
        self.assertEqual(detail["onlineRefund"], {"offered": True, "available": True, "label": "GCash", "reason": ""})
        response = self.client.post(reverse("arabela_admin:deposit_return", args=[booking.pk]), data=json.dumps(
            {"amount": "2000", "method": "PayMongo", "request_id": "ret-0009-zzzz"}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["payment"]["method"], "GCash (online payment)")
        self.assertEqual(len(self.fake.refunds), 1)
        self.assertEqual(self.client.get(reverse("arabela_admin:payment_detail", args=[booking.pk])).json()["canReturn"], False)

    def test_a_refusal_from_paymongo_is_shown_to_staff_not_swallowed(self):
        booking = self.returned_booking()
        self.age_payment(booking, 2)
        self.client.force_login(self.owner)
        self.fake.fail["create_refund"] = paymongo.PaymongoError("down", status=503, retryable=True)
        response = self.client.post(reverse("arabela_admin:deposit_return", args=[booking.pk]), data=json.dumps({"amount": "2000", "method": "PayMongo"}), content_type="application/json")
        self.assertEqual(response.status_code, 502)
        self.assertIn("Nothing was sent or recorded", response.json()["error"])

    def test_the_manual_methods_still_work_exactly_as_before(self):
        booking = self.returned_booking()
        self.age_payment(booking, 2)
        self.client.force_login(self.owner)
        response = self.client.post(reverse("arabela_admin:deposit_return", args=[booking.pk]), data=json.dumps({"amount": "2000", "method": "Cash"}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.fake.refunds, {})

    def test_the_form_markup_has_the_automatic_choice_and_an_are_you_sure(self):
        booking = self.returned_booking()
        self.client.force_login(self.owner)
        html = self.client.get(reverse("arabela_admin:payments")).content.decode()
        for needle in ('value="PayMongo"', "through PayMongo", "Send deposit back", "Send the deposit back now?", "d.onlineRefund"):
            with self.subTest(needle=needle):
                self.assertIn(needle, html)
