"""Amounts OVER the security deposit are paid right away (decided with the owner, 2026-11-27), driven through the real views with a stand-in Xendit.

  * Booking: booked extra days that cost more than the deposit are paid together with it (all gowns of the booking pooled).
  * A date change that costs more than the deposit covers saves nothing: staff send a payment request, the new days are held for 24 hours,
    and the dates change by themselves once it is paid (online, QR or cash). Unpaid, cancelled or replaced: nothing changes, late money goes back.
  * Walk-in: a later return can be chosen in New Booking; what is over the deposit is taken before the booking saves.
  * A cancelled booking gets back what it paid over the deposit; late fees past the deposit are warned about.
Bookings made before this keep working as they did."""
import json
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import terms
from accounts.models import AuditLog, CustomerMessage, UserProfile
from accounts.test_support import id_rule_off
from arabela_admin import dashboard_money
from gowns import availability, online_checkout
from gowns.models import GownUnavailability
from gowns.test_xendit_checkout import Base
from reservations import date_requests, ledger, payments, reminders
from reservations.models import DateChangeRequest, Payment, PaymentAttempt, Reservation, ReservationItem
from reservations.test_ledger import pay

User = get_user_model()
D = Decimal
R = DateChangeRequest.Status


class OverBase(Base):
    """Helpers on top of the stand-in Xendit base: bookings made through the real checkout, and the staff's side."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.staff = User.objects.create_user("od_staff", password="x", is_staff=True, first_name="Maria")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.STAFF})

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.admin = Client()
        self.admin.force_login(self.staff)

    def day(self, offset):
        return self.event + timedelta(days=offset)

    def dated(self, gown=None, pickup=-2, ret=2):
        """A checkout line with the event and the days the customer chose (offsets from the event)."""
        line = self.item(gown)
        line.update(event_date=self.event.isoformat(), rental_date=self.day(pickup).isoformat(), return_date=self.day(ret).isoformat())
        return line

    def booked(self, lines=None, *, option="deposit", **extra):
        """A paid booking made through the checkout."""
        attempt, _ = self.started(items=lines or [self.dated()], option=option, **extra)
        data = self.pay_and_confirm(attempt)
        self.assertEqual(data["state"], "booked", data)
        return Reservation.objects.get(pk=PaymentAttempt.objects.get(pk=attempt.pk).reservation_id)

    def old_booking(self):
        """A booking made before amounts over the deposit were paid right away."""
        reservation = self.booked()
        Reservation.objects.filter(pk=reservation.pk).update(pays_over_deposit=False)
        return Reservation.objects.get(pk=reservation.pk)

    def statement(self, reservation):
        return ledger.statement(Reservation.objects.get(pk=reservation.pk))

    def post(self, name, *args, body=None, client=None):
        return (client or self.admin).post(reverse(f"arabela_admin:{name}", args=args), data=json.dumps(body or {}), content_type="application/json")

    def change(self, reservation, *, pickup=None, ret=None, event=None, item=None):
        item = item or reservation.items.order_by("id").first()
        body = {key: self.day(value).isoformat() for key, value in (("pickup", pickup), ("return", ret), ("event", event)) if value is not None}
        return self.post("reservation_item_change_dates", item.id, body=body)

    def request(self, reservation, *, pickup=None, ret=None, item=None):
        item = item or reservation.items.order_by("id").first()
        body = {key: self.day(value).isoformat() for key, value in (("pickup", pickup), ("return", ret)) if value is not None}
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post("reservation_item_request_payment", item.id, body=body)
        self.assertEqual(response.status_code, 200, response.content)
        return DateChangeRequest.objects.get(pk=response.json()["request"]["id"])

    def item_of(self, reservation):
        return ReservationItem.objects.get(pk=reservation.items.order_by("id").first().pk)

    def pay_online(self, row):
        """The customer presses Pay on the booking page and pays on the payment page. -> (attempt, what the return page is told)."""
        response = self.client.post(reverse("gowns:date_pay_start", args=[row.pk]))
        self.assertEqual(response.status_code, 200, response.content)
        attempt = PaymentAttempt.objects.filter(date_change=row).order_by("-id").first()
        self.assertEqual(response.json()["checkout_url"], attempt.checkout_url)
        return attempt, self.pay_and_confirm(attempt)


# ---------------------------------------------------------------- booking
class BookingOverTheDepositTests(OverBase):
    def test_the_part_over_the_deposit_is_paid_when_booking(self):
        lines = [self.dated(ret=13)]                                  # 11 extra days x 200 = 2,200 against a 2,000 deposit
        refused = self.start(items=lines, expected_extra_fee="2200")
        self.assertEqual(refused.status_code, 409)                    # a page that did not show the amount over the deposit
        self.assertIn("200.00 more than the security deposit", refused.json()["error"])
        self.assertFalse(PaymentAttempt.objects.exists())
        wrong = self.start(items=lines, expected_extra_fee="2200", expected_over="400")
        self.assertEqual(wrong.status_code, 409)

        attempt, _ = self.started(items=lines, expected_extra_fee="2200", expected_over="200")
        self.assertEqual(attempt.amount, D("2200"))
        self.assertEqual(self.fake.sessions[attempt.session_id]["body"]["amount"], 2200)
        data = self.pay_and_confirm(attempt)
        self.assertEqual(data["state"], "booked", data)
        self.assertEqual((data["details"]["over"], data["details"]["paid"], data["details"]["total"], data["details"]["deposit_back"]),
                         ("200.00", "2,200.00", "5,700.00", "0.00"))
        reservation = Reservation.objects.get()
        self.assertEqual((reservation.pays_over_deposit, reservation.upfront_extra, reservation.security_deposit), (True, D("200"), D("2000")))
        payment = reservation.payments.get()
        self.assertEqual((payment.purpose, payment.amount, payment.extra_part), (Payment.Purpose.DEPOSIT, D("2200"), D("200")))
        s = self.statement(reservation)
        self.assertEqual((s.deposit_paid, s.rental_paid, s.extra_received, s.extra_owed, s.balance_due, s.refundable_total),
                         (D("2000"), D("0"), D("200"), D("0"), D("3500"), D("0")))
        self.assertEqual((payment.snapshot["extra_part"], payment.snapshot["charges"]["over"], payment.snapshot["charges"]["total"]),
                         ("200.00", "200.00", "5700.00"))
        self.assertEqual(payment.snapshot["deduct"]["back"], "0.00")
        self.assertIn("It includes ₱200.00 for the extra days", CustomerMessage.objects.filter(category="Payment").latest("id").body)
        page = self.client.get(reverse("gowns:reservation_item_detail", args=[reservation.items.get().pk]))
        self.assertEqual(page.context["paid_over"], D("200"))
        self.assertContains(page, "Extra Days Over Deposit")
        receipt = self.client.get(reverse("gowns:receipt_view", args=[payment.pk])).content.decode()
        self.assertIn("Extra days over the deposit", receipt)
        self.assertIn("&#8369;5,700.00", receipt)

    def test_paid_in_full_includes_it_too(self):
        reservation = self.booked([self.dated(ret=13)], option="full", expected_over="200")
        payment = reservation.payments.get()
        self.assertEqual((payment.purpose, payment.amount, payment.extra_part), (Payment.Purpose.FULL, D("5700"), D("200")))
        s = self.statement(reservation)
        self.assertEqual((s.balance_due, s.extra_received, s.rental_paid), (D("0"), D("200"), D("3500")))
        money = dashboard_money.money_between()
        self.assertEqual((money["income"], money["collected"], money["kept"]), (D("3500"), D("2000"), D("200")))

    def test_ten_days_is_exactly_the_deposit_so_nothing_more_is_paid(self):
        reservation = self.booked([self.dated(ret=12)])
        payment = reservation.payments.get()
        self.assertEqual((payment.amount, payment.extra_part, reservation.upfront_extra), (D("2000"), D("0"), D("0")))
        self.assertEqual(self.statement(reservation).refundable_total, D("0"))

    def test_the_deposits_of_all_gowns_are_pooled(self):
        reservation = self.booked([self.dated(ret=13), self.dated(self.gown2)])
        self.assertEqual((reservation.security_deposit, reservation.upfront_extra, reservation.payments.get().amount), (D("4000"), D("0"), D("4000")))
        self.assertEqual(self.statement(reservation).refundable_total, D("1800"))

    def test_a_checkout_opened_before_the_update_books_the_old_way(self):
        attempt, _ = self.started(items=[self.dated(ret=7)])
        payload = {k: v for k, v in attempt.payload.items() if k not in ("pays_over_deposit", "upfront_extra")}
        PaymentAttempt.objects.filter(pk=attempt.pk).update(payload=payload)
        self.assertEqual(self.pay_and_confirm(attempt)["state"], "booked")
        self.assertFalse(Reservation.objects.get().pays_over_deposit)


# ---------------------------------------------------------------- changing dates
class DateChangeRequestTests(OverBase):
    def test_under_the_deposit_saves_at_once(self):
        reservation = self.booked()
        response = self.change(reservation, ret=12)                   # 10 days = 2,000: the deposit covers it
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.item_of(reservation).return_date, self.day(12))
        self.assertFalse(DateChangeRequest.objects.exists())

    def test_over_the_deposit_saves_nothing_and_a_request_holds_the_days(self):
        reservation = self.booked()
        item = self.item_of(reservation)
        response = self.change(reservation, ret=13)
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual((body["needsPayment"], body["amount"], body["hasAccount"]), (True, "200.00", True))
        self.assertEqual(self.item_of(reservation).return_date, item.return_date)
        self.assertFalse(DateChangeRequest.objects.exists())
        CustomerMessage.objects.all().delete()
        with override_settings(CUSTOMER_EMAILS_ENABLED=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"), \
                patch("accounts.customer_emails._enqueue", side_effect=lambda message, kind: mail.outbox.append(message)):
            row = self.request(reservation, ret=13)
        self.assertEqual((row.status, row.amount, row.item_id, row.hold_start, row.hold_end),
                         (R.WAITING, D("200"), item.id, self.day(-2), self.day(18)))       # the new days and the 5 cleaning days after them
        self.assertLessEqual(row.expires_at - row.created_at, timedelta(hours=24))
        self.assertEqual(self.item_of(reservation).return_date, self.day(2))               # still the old dates
        message = CustomerMessage.objects.get()
        self.assertIn("Please pay ₱200.00", message.body)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Pay PHP 200.00", mail.outbox[0].body)
        self.assertIn(reverse("gowns:reservation_item_detail", args=[item.id]) + "#pay-request", mail.outbox[0].body)
        # the days are held: nobody else can book or be moved onto them
        later = availability.check_unit(self.gown, self.day(12))
        self.assertFalse(later["ok"])
        self.assertIn("held for a date change", later["conflicts"][0]["label"])
        bookings, _blocks = availability.load_busy_ranges([self.gown.id], self.today, 200)
        self.assertIn((self.day(-2), self.day(18)), bookings[self.gown.id])
        taken = self.start(items=[self.dated(pickup=13, ret=19)], client=self.ben_client(), event_date=self.day(15).isoformat())
        self.assertNotEqual(taken.status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action="item.request_payment").exists())
        # the account message carries a Pay button to the booking page
        inbox = self.client.get(reverse("gowns:messages"))
        self.assertContains(inbox, "Pay PHP 200.00")
        self.assertContains(inbox, reverse("gowns:reservation_item_detail", args=[item.id]) + "#pay-request")
        # the booking page shows the Pay button, Active Reservations shows the waiting request
        page = self.client.get(reverse("gowns:reservation_item_detail", args=[item.id]))
        self.assertContains(page, "Payment needed for new dates")
        self.assertContains(page, reverse("gowns:date_pay_start", args=[row.pk]))
        admin = self.admin.get(reverse("arabela_admin:active_reservations")).content.decode()
        self.assertIn("Waiting for payment: &#8369;200.00", admin)
        for words in ("Show QR", "Cancel request", "Resend"):
            self.assertIn(words, admin)

    def test_paying_online_changes_the_dates_by_itself(self):
        reservation = self.booked()
        row = self.request(reservation, ret=13)
        attempt, data = self.pay_online(row)
        self.assertEqual((attempt.amount, attempt.purpose, attempt.source, attempt.date_change_id),
                         (D("200"), PaymentAttempt.Purpose.EXTRA, PaymentAttempt.Source.ONLINE, row.pk))
        self.assertEqual(data["state"], "dates_changed", data)
        self.assertEqual((data["date_change"], data["amount"]), (True, "200.00"))
        item = self.item_of(reservation)
        self.assertEqual((item.return_date, item.original_return_date), (self.day(13), self.day(2)))
        block = GownUnavailability.objects.get(auto_for_item=item)
        self.assertEqual((block.start_date, block.end_date), (self.day(14), self.day(18)))
        row.refresh_from_db()
        payment = row.payment
        self.assertEqual((row.status, payment.purpose, payment.amount, payment.method), (R.APPLIED, Payment.Purpose.EXTRA, D("200"), Payment.Method.PM_GCASH))
        s = self.statement(reservation)
        self.assertEqual((s.extra_owed, s.extra_received, s.late_fee), (D("0"), D("200"), D("2200")))
        self.assertEqual(payment.snapshot["extra"]["over"], "200.00")
        note = CustomerMessage.objects.filter(category="Payment").latest("id").body
        self.assertIn(payment.receipt_number, note)
        self.assertIn("Your new dates for", note)
        self.assertTrue(AuditLog.objects.filter(action="item.date_paid").exists())
        self.assertEqual(Payment.objects.filter(reservation=reservation).count(), 2)
        # asking again (the return page reloads, the webhook arrives) changes nothing
        online_checkout.confirm_attempt(attempt.pk, source="webhook")
        self.assertEqual(Payment.objects.filter(reservation=reservation).count(), 2)

    def test_only_the_customer_of_the_booking_can_pay_it(self):
        reservation = self.booked()
        row = self.request(reservation, ret=13)
        response = self.ben_client().post(reverse("gowns:date_pay_start", args=[row.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(PaymentAttempt.objects.filter(date_change=row).exists())

    def test_pressing_pay_twice_opens_one_page(self):
        reservation = self.booked()
        row = self.request(reservation, ret=13)
        first = self.client.post(reverse("gowns:date_pay_start", args=[row.pk])).json()["checkout_url"]
        again = self.client.post(reverse("gowns:date_pay_start", args=[row.pk])).json()["checkout_url"]
        self.assertEqual(first, again)
        self.assertEqual(PaymentAttempt.objects.filter(date_change=row).count(), 1)

    def test_money_for_a_cancelled_request_goes_back(self):
        reservation = self.booked()
        row = self.request(reservation, ret=13)
        self.client.post(reverse("gowns:date_pay_start", args=[row.pk]))
        attempt = PaymentAttempt.objects.get(date_change=row)
        response = self.post("date_request_cancel", row.pk)                  # (the page is closed after the save: not run here, as if it raced)
        self.assertEqual(response.status_code, 200, response.content)
        data = self.pay_and_confirm(attempt)
        self.assertEqual(data["state"], "refunded", data)
        self.assertIn("cancelled this payment request", data["message"])
        self.assertEqual(len(self.fake.refunds), 1)
        self.assertEqual(self.item_of(reservation).return_date, self.day(2))
        self.assertEqual(Payment.objects.filter(reservation=reservation).count(), 1)
        self.assertIn("is being sent back", CustomerMessage.objects.latest("id").body)

    def test_cancelling_closes_the_open_payment_page(self):
        reservation = self.booked()
        row = self.request(reservation, ret=13)
        self.client.post(reverse("gowns:date_pay_start", args=[row.pk]))
        attempt = PaymentAttempt.objects.get(date_change=row)
        with self.captureOnCommitCallbacks(execute=True):
            self.post("date_request_cancel", row.pk)
        attempt.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual((attempt.status, row.status), (PaymentAttempt.Status.CANCELLED, R.CANCELLED))
        self.assertEqual(self.client.post(reverse("gowns:date_pay_start", args=[row.pk])).status_code, 409)
        self.assertTrue(availability.check_unit(self.gown, self.day(15))["ok"])      # the held days are open again

    def test_money_after_the_time_ran_out_goes_back(self):
        reservation = self.booked()
        row = self.request(reservation, ret=13)
        self.client.post(reverse("gowns:date_pay_start", args=[row.pk]))
        attempt = PaymentAttempt.objects.get(date_change=row)
        DateChangeRequest.objects.filter(pk=row.pk).update(expires_at=timezone.now() - timedelta(minutes=5))
        data = self.pay_and_confirm(attempt)
        self.assertEqual(data["state"], "refunded", data)
        self.assertEqual(self.item_of(reservation).return_date, self.day(2))

    def test_money_paid_in_time_still_counts_when_it_is_heard_of_late(self):
        reservation = self.booked()
        row = self.request(reservation, ret=13)
        self.client.post(reverse("gowns:date_pay_start", args=[row.pk]))
        attempt = PaymentAttempt.objects.get(date_change=row)
        DateChangeRequest.objects.filter(pk=row.pk).update(status=R.EXPIRED, expires_at=timezone.now() + timedelta(hours=1))
        self.assertEqual(self.pay_and_confirm(attempt)["state"], "dates_changed")
        self.assertEqual(self.item_of(reservation).return_date, self.day(13))

    def test_money_goes_back_when_the_days_were_blocked_meanwhile(self):
        reservation = self.booked()
        row = self.request(reservation, ret=13)
        self.client.post(reverse("gowns:date_pay_start", args=[row.pk]))
        attempt = PaymentAttempt.objects.get(date_change=row)
        GownUnavailability.objects.create(gown=self.gown, start_date=self.day(8), end_date=self.day(9), reason=GownUnavailability.Reason.REPAIR)
        data = self.pay_and_confirm(attempt)
        self.assertEqual(data["state"], "refunded", data)
        self.assertEqual(data["message"], "The new dates are no longer free for this gown.")
        row.refresh_from_db()
        self.assertEqual((row.status, self.item_of(reservation).return_date, len(self.fake.refunds)), (R.WAITING, self.day(2), 1))
        # pressing Pay again takes no more money; staff are told why when they press Show QR
        again = self.client.post(reverse("gowns:date_pay_start", args=[row.pk]))
        self.assertEqual((again.status_code, again.json()["error"]),
                         (409, "These new dates can't be saved right now, so nothing was charged. Please contact the shop."))
        counter = self.post("date_request_qr", row.pk)
        self.assertEqual(counter.status_code, 409)
        self.assertIn("blocked for repair", counter.json()["error"])
        self.assertEqual(PaymentAttempt.objects.filter(date_change=row).count(), 1)

    def test_the_customer_is_never_told_who_else_has_the_gown(self):
        reservation = self.booked()
        row = self.request(reservation, ret=13)
        self.client.post(reverse("gowns:date_pay_start", args=[row.pk]))
        attempt = PaymentAttempt.objects.get(date_change=row)
        other = Reservation.objects.create(                          # a booking that slipped onto the held days
            channel=Reservation.Channel.WALK_IN, status=Reservation.Status.CONFIRMED, customer_name="Secret Person",
            rental_subtotal=D("1"), security_deposit=D("2000"), total_amount=D("2001"))
        ReservationItem.objects.create(reservation=other, gown=self.gown, gown_name=self.gown.name, rental_price=D("1"),
                                       rental_date=self.day(8), return_date=self.day(9))
        data = self.pay_and_confirm(attempt)
        self.assertEqual(data["state"], "refunded", data)
        told = " ".join([data["message"], *CustomerMessage.objects.filter(recipient=reservation.customer).values_list("body", flat=True),
                         self.client.get(reverse("gowns:payment_return", args=[attempt.token])).content.decode()])
        self.assertNotIn("Secret", told)
        self.assertNotIn(other.reference_code, told)

    def test_unpaid_in_time_the_dates_stay_and_the_days_open_again(self):
        reservation = self.booked()
        row = self.request(reservation, ret=13)
        DateChangeRequest.objects.filter(pk=row.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertTrue(availability.check_unit(self.gown, self.day(15))["ok"])       # an out-of-time request holds nothing, swept or not
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(date_requests.expire_due(), 1)
        row.refresh_from_db()
        self.assertEqual((row.status, self.item_of(reservation).return_date), (R.EXPIRED, self.day(2)))
        self.assertIn("not paid in time", CustomerMessage.objects.latest("id").body)
        self.assertEqual(date_requests.expire_due(), 0)

    def test_a_request_sent_while_an_old_one_is_being_closed_stays(self):
        reservation = self.booked()
        old = self.request(reservation, ret=13)
        self.client.post(reverse("gowns:date_pay_start", args=[old.pk]))
        DateChangeRequest.objects.filter(pk=old.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        newer = []

        def closing_the_old_page(attempt_id):                      # staff send a new request while the payment service is answering
            newer.append(date_requests.create(old.item_id, want_pickup=None, want_return=self.day(14), user=self.staff))

        with patch("gowns.online_checkout.cancel_attempt", side_effect=closing_the_old_page):
            date_requests.expire_due()
        self.assertEqual(DateChangeRequest.objects.get(pk=newer[0].pk).status, R.WAITING)
        self.assertEqual(DateChangeRequest.objects.get(pk=old.pk).status, R.REPLACED)

    def test_somebody_else_is_not_told_whether_a_closed_request_exists(self):
        reservation = self.booked()
        row = self.request(reservation, ret=13)
        self.post("date_request_cancel", row.pk)
        self.assertEqual(self.ben_client().post(reverse("gowns:date_pay_start", args=[row.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("gowns:date_pay_start", args=[row.pk])).status_code, 409)

    def test_the_signed_in_check_and_the_form_token_guard_the_pay_address(self):
        reservation = self.booked()
        row = self.request(reservation, ret=13)
        address = reverse("gowns:date_pay_start", args=[row.pk])
        self.assertEqual(Client().post(address).status_code, 401)
        careless = Client(enforce_csrf_checks=True)
        careless.force_login(reservation.customer)
        self.assertEqual(careless.post(address).status_code, 403)
        self.assertEqual(self.client.get(address).status_code, 405)
        for name in ("date_request_resend", "date_request_cancel", "date_request_cash", "date_request_qr"):
            self.assertEqual(self.post(name, row.pk, client=Client()).status_code, 401, name)
            self.assertEqual(self.post(name, row.pk, client=self.client).status_code, 401, name)          # a customer is not staff
            forged = Client(enforce_csrf_checks=True)
            forged.force_login(self.staff)
            self.assertEqual(self.post(name, row.pk, client=forged).status_code, 403, name)
        for who in (Client(), self.client):
            self.assertEqual(self.post("reservation_item_request_payment", row.item_id, body={"return": self.day(14).isoformat()}, client=who).status_code, 401)
        self.assertEqual(DateChangeRequest.objects.get(pk=row.pk).status, R.WAITING)
        self.assertEqual(Payment.objects.filter(reservation=reservation).count(), 1)

    def test_cash_at_the_counter(self):
        reservation = self.booked()
        row = self.request(reservation, ret=13)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post("date_request_cash", row.pk, body={"request_id": "cash-press-0001"})
        self.assertEqual(response.status_code, 200, response.content)
        again = self.post("date_request_cash", row.pk, body={"request_id": "cash-press-0001"})
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(again.json()["paymentId"], response.json()["paymentId"])
        payment = Payment.objects.get(pk=response.json()["paymentId"])
        self.assertEqual((payment.amount, payment.method, payment.purpose, Payment.objects.filter(reservation=reservation).count()),
                         (D("200"), Payment.Method.CASH, Payment.Purpose.EXTRA, 2))
        self.assertEqual(self.item_of(reservation).return_date, self.day(13))
        self.assertEqual(self.statement(reservation).extra_owed, D("0"))
        self.assertEqual(self.post("date_request_cash", row.pk, body={"request_id": "another-press-02"}).status_code, 409)
        self.assertTrue(AuditLog.objects.filter(action="date_request.cash").exists())

    def test_the_counter_qr_code(self):
        reservation = self.booked()
        row = self.request(reservation, ret=13)
        response = self.post("date_request_qr", row.pk)
        self.assertEqual(response.status_code, 200, response.content)
        shown = response.json()
        self.assertEqual((shown["state"], shown["amount"]), ("waiting", "200.00"))
        self.assertTrue(shown["qr"].startswith("data:image/png"))
        self.assertEqual(self.post("date_request_qr", row.pk).json()["token"], shown["token"])       # the same code again
        self.assertNotIn("pay", {k: v for k, v in self.admin.get(reverse("arabela_admin:payment_detail", args=[reservation.pk])).json().items() if v})
        attempt = PaymentAttempt.objects.get(token=shown["token"])
        self.fake.paid(attempt.session_id)
        PaymentAttempt.objects.filter(pk=attempt.pk).update(last_checked_at=None)
        status = self.admin.get(shown["statusUrl"]).json()
        self.assertEqual(status["state"], "recorded", status)
        self.assertEqual(self.item_of(reservation).return_date, self.day(13))

    def test_a_newer_request_replaces_the_older_one(self):
        reservation = self.booked()
        first = self.request(reservation, ret=13)
        second = self.request(reservation, ret=14)
        first.refresh_from_db()
        self.assertEqual((first.status, second.status, second.amount), (R.REPLACED, R.WAITING, D("400")))
        self.assertEqual(DateChangeRequest.objects.filter(status=R.WAITING).count(), 1)

    def test_resend_sends_the_message_again(self):
        reservation = self.booked()
        row = self.request(reservation, ret=13)
        before = CustomerMessage.objects.count()
        self.assertEqual(self.post("date_request_resend", row.pk).status_code, 200)
        self.assertEqual(CustomerMessage.objects.count(), before + 1)
        self.assertIn("Reminder: your new dates", CustomerMessage.objects.latest("id").body)

    def test_saving_other_dates_closes_the_request(self):
        reservation = self.booked()
        row = self.request(reservation, ret=13)
        self.assertEqual(self.change(reservation, ret=5).status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.status, R.CLOSED)

    def test_the_gown_coming_back_closes_the_request(self):
        reservation = self.booked()
        row = self.request(reservation, ret=13)
        item = self.item_of(reservation)
        ReservationItem.objects.filter(pk=item.pk).update(stage=ReservationItem.Stage.RESERVED)
        response = self.post("reservation_item_mark_returned", item.pk, body={"condition": "Good"})
        self.assertEqual(response.status_code, 200, response.content)
        row.refresh_from_db()
        self.assertEqual(row.status, R.CLOSED)

    def test_cancelling_the_booking_closes_the_request(self):
        reservation = self.booked()
        row = self.request(reservation, ret=13)
        response = self.client.post(reverse("gowns:reservation_item_cancel", args=[row.item_id]))
        self.assertEqual(response.status_code, 200, response.content)
        row.refresh_from_db()
        self.assertEqual(row.status, R.CLOSED)

    def test_an_earlier_pick_up_is_paid_before_it_changes(self):
        reservation = self.booked()
        response = self.change(reservation, pickup=-13)                # 11 days early
        self.assertEqual((response.status_code, response.json()["amount"]), (409, "200.00"))
        row = self.request(reservation, pickup=-13)
        self.assertLessEqual(row.expires_at, timezone.make_aware(datetime.combine(self.day(-13), time(23, 59))))
        self.assertEqual(self.pay_online(row)[1]["state"], "dates_changed")
        self.assertEqual(self.item_of(reservation).rental_date, self.day(-13))

    def test_more_days_after_paying_ask_only_for_what_is_new(self):
        reservation = self.booked()
        self.pay_online(self.request(reservation, ret=13))
        response = self.change(reservation, ret=15)                    # 13 days = 2,600; 2,200 is covered
        self.assertEqual((response.status_code, response.json()["amount"]), (409, "400.00"))

    def test_shorter_dates_give_the_extra_back_with_the_deposit(self):
        reservation = self.booked()
        self.pay_online(self.request(reservation, ret=13))
        self.assertEqual(self.change(reservation, ret=4).status_code, 200)       # 2 extra days = 400
        item = self.item_of(reservation)
        ReservationItem.objects.filter(pk=item.pk).update(stage=ReservationItem.Stage.RETURNED, returned_on=item.return_date)
        pay(Reservation.objects.get(pk=reservation.pk), Payment.Purpose.BALANCE, "3500")
        s = self.statement(reservation)
        self.assertEqual((s.refund_available, s.extra_credit), (D("1800"), D("200")))

    def test_an_older_booking_changes_as_before(self):
        reservation = self.old_booking()
        self.assertEqual(self.change(reservation, ret=20).status_code, 200)
        self.assertFalse(DateChangeRequest.objects.exists())
        self.assertEqual(self.statement(reservation).extra_owed, D("1600"))

    def test_no_request_when_nothing_needs_paying(self):
        reservation = self.booked()
        response = self.post("reservation_item_request_payment", self.item_of(reservation).pk, body={"return": self.day(5).isoformat()})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(DateChangeRequest.objects.exists())


# ---------------------------------------------------------------- money going back
class CancelledBookingTests(OverBase):
    def test_what_was_paid_over_the_deposit_goes_back_to_the_account_it_came_from(self):
        reservation = self.booked([self.dated(ret=13)], expected_over="200")
        response = self.client.post(reverse("gowns:reservation_item_cancel", args=[reservation.items.get().pk]))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("₱200.00 you paid besides it", CustomerMessage.objects.latest("id").body)
        s = self.statement(reservation)
        self.assertEqual((s.state, s.refund_available, s.extra_credit, s.balance_due, s.deposit_paid), (ledger.REFUND, D("200"), D("200"), D("0"), D("2000")))
        self.assertEqual(dashboard_money.money_between()["kept"], D("0"))
        listed = self.admin.get(reverse("arabela_admin:payments")).content.decode()
        self.assertIn("Deposit kept (cancelled), give back ₱200.00", listed)
        with self.assertRaises(payments.PaymentError):
            payments.return_deposit(reservation_id=reservation.pk, amount=D("2000"), method=Payment.Method.CASH, user=self.staff)
        with override_settings(PAYMONGO_AUTO_REFUND=True):
            payment = payments.return_deposit_online(reservation_id=reservation.pk, amount=D("200"), user=self.staff)
        self.assertEqual((payment.title, payment.get_purpose_display(), payment.amount, payment.direction), ("REFUND", "Refund", D("200"), "Out"))
        self.assertEqual(self.fake.refunds[next(iter(self.fake.refunds))]["amount"], 200)
        s = self.statement(reservation)
        self.assertEqual((s.state, s.refund_available), (ledger.CANCELLED, D("0")))
        paper = self.client.get(reverse("gowns:receipt_view", args=[payment.pk])).content.decode()
        for words in ("REFUND", "Kept by the shop (not refunded)", "RETURNED NOW", "&#8369;200.00"):
            self.assertIn(words, paper)
        with self.assertRaises(payments.PaymentError):
            payments.return_deposit(reservation_id=reservation.pk, amount=D("1"), method=Payment.Method.CASH, user=self.staff)

    def paid_twice(self, second):
        """A booking that paid 200 over the deposit with GCash, then 200 more for new dates with `second`."""
        reservation = self.booked([self.dated(ret=13)], expected_over="200")
        row = self.request(reservation, ret=14)
        self.client.post(reverse("gowns:date_pay_start", args=[row.pk]))
        self.assertEqual(self.pay_and_confirm(PaymentAttempt.objects.get(date_change=row), channel=second)["state"], "dates_changed")
        return reservation

    def cancel_it(self, reservation):
        self.client.post(reverse("gowns:reservation_item_cancel", args=[reservation.items.get().pk]))

    def choice(self, reservation):
        with override_settings(PAYMONGO_AUTO_REFUND=True):
            return payments.online_refund_choice(Reservation.objects.get(pk=reservation.pk), self.statement(reservation))

    def send_back(self, reservation, amount):
        with override_settings(PAYMONGO_AUTO_REFUND=True):
            return payments.return_deposit_online(reservation_id=reservation.pk, amount=D(amount), user=self.staff)

    def refunded_to(self):
        return sorted((r["payment_id"], r["amount"]) for r in self.fake.refunds.values())

    def paid_in(self, reservation):
        return sorted((p.provider_ref, int(p.extra_part or p.amount)) for p in Payment.objects.filter(reservation=reservation, direction="In"))

    def test_money_from_two_online_payments_goes_back_in_parts(self):
        reservation = self.paid_twice("PAYMAYA")
        self.cancel_it(reservation)
        first = self.choice(reservation)
        self.assertEqual((first["offered"], first["available"], first.get("part"), first["label"]), (True, True, "200.00", "Maya"), first)
        self.assertIn("goes back in parts", first["reason"])
        self.assertIn("Cash hands back the whole ₱400.00", first["reason"])
        with self.assertRaises(payments.PaymentError):                       # the whole 400 still can't go to one payment
            self.send_back(reservation, "400")
        detail = self.admin.get(reverse("arabela_admin:payment_detail", args=[reservation.pk])).json()
        self.assertTrue(detail["canReturn"])
        self.send_back(reservation, first["part"])
        second = self.choice(reservation)
        self.assertEqual((second["available"], second.get("part"), second["label"], second["reason"]), (True, None, "GCash", ""), second)
        self.send_back(reservation, "200")
        self.assertEqual(self.refunded_to(), self.paid_in(reservation))      # each payment got its own money back
        s = self.statement(reservation)
        self.assertEqual((s.state, s.refund_available, s.refunded), (ledger.CANCELLED, D("0"), D("400")))

    def test_a_part_that_cannot_go_back_online_is_left_for_cash(self):
        reservation = self.paid_twice("QRPH")
        self.cancel_it(reservation)
        first = self.choice(reservation)
        self.assertEqual((first["available"], first.get("part"), first["label"]), (True, "200.00", "GCash"), first)
        self.send_back(reservation, "200")
        rest = self.choice(reservation)
        self.assertEqual((rest["available"], rest.get("part")), (False, None), rest)
        self.assertIn("cannot send this kind of payment back", rest["reason"])
        payments.return_deposit(reservation_id=reservation.pk, amount=D("200"), method=Payment.Method.CASH, user=self.staff)
        self.assertEqual((self.statement(reservation).refund_available, len(self.fake.refunds)), (D("0"), 1))

    def test_the_deposit_and_extras_of_a_returned_gown_go_back_in_parts_too(self):
        reservation = self.paid_twice("PAYMAYA")
        self.assertEqual(self.change(reservation, ret=2).status_code, 200)      # shortened again: no fees left
        ReservationItem.objects.filter(reservation=reservation).update(
            stage=ReservationItem.Stage.RETURNED, picked_up_on=self.today, returned_on=self.today)
        self.assertEqual(self.statement(reservation).refund_available, D("2400"))
        first = self.choice(reservation)
        self.assertEqual((first["available"], first.get("part"), first["label"]), (True, "2200.00", "GCash"), first)
        self.send_back(reservation, "2200")
        second = self.choice(reservation)
        self.assertEqual((second["available"], second.get("part"), second["label"]), (True, None, "Maya"), second)
        self.send_back(reservation, "200")
        self.assertEqual((self.statement(reservation).refund_available, len(self.fake.refunds)), (D("0"), 2))

    def test_cancelled_by_the_shop_says_so(self):
        reservation = self.booked([self.dated(ret=13)], expected_over="200")
        response = self.post("reservation_cancel", reservation.pk, body={"reason": "customer_asked", "typed_code": reservation.reference_code})
        self.assertIn(response.status_code, (200, 403))
        if response.status_code == 403:                               # staff need the owner's permission here
            from reservations import cancellation
            cancellation.cancel_by_staff(reservation.pk, staff_name="Owner", reason_key="customer_asked", note="", typed_code=reservation.reference_code)
        self.assertIn("The ₱200.00 you paid besides it will be returned to you", CustomerMessage.objects.latest("id").body)

    def test_an_older_cancelled_booking_keeps_everything(self):
        reservation = self.old_booking()
        Reservation.objects.filter(pk=reservation.pk).update(status=Reservation.Status.CANCELLED)
        pay(reservation, Payment.Purpose.EXTRA, "200")
        s = self.statement(reservation)
        self.assertEqual((s.refund_available, s.state), (D("0"), ledger.CANCELLED))
        with self.assertRaises(payments.PaymentError):
            payments.return_deposit(reservation_id=reservation.pk, amount=D("200"), method=Payment.Method.CASH, user=self.staff)


# ---------------------------------------------------------------- late without a new date
class LateFeeWarningTests(OverBase):
    def overdue(self, reservation, days):
        item = self.item_of(reservation)
        ReservationItem.objects.filter(pk=item.pk).update(
            stage=ReservationItem.Stage.RESERVED, rental_date=self.today - timedelta(days=days + 4), return_date=self.today - timedelta(days=days),
            original_return_date=self.today - timedelta(days=days), event_date=self.today - timedelta(days=days + 2),
            free_pickup_date=self.today - timedelta(days=days + 4), free_return_date=self.today - timedelta(days=days))

    def messages(self):
        return list(CustomerMessage.objects.filter(category=CustomerMessage.Category.RETURN_OVERDUE).values_list("body", flat=True))

    def test_the_warning_goes_with_the_overdue_reminder(self):
        reservation = self.booked()
        self.overdue(reservation, 11)
        reminders.send_due_reminders(self.today)
        body, = self.messages()
        self.assertIn("late fees are now more than your security deposit: ₱200.00", body)
        self.assertTrue(body.endswith(f"Reference: {reservation.reference_code}."))
        self.assertEqual(reminders.send_due_reminders(self.today), 0)                  # once

    def test_it_goes_out_as_soon_as_the_fees_pass_the_deposit(self):
        reservation = self.booked()
        self.overdue(reservation, 10)
        reminders.send_due_reminders(self.today)                                      # exactly the deposit: no warning yet
        self.assertNotIn("more than your security deposit", self.messages()[0])
        tomorrow = self.today + timedelta(days=1)
        self.assertEqual(reminders.send_due_reminders(tomorrow), 1)                    # not waiting for the three-day repeat
        self.assertIn("more than your security deposit", self.messages()[0])
        self.assertEqual(reminders.send_due_reminders(tomorrow), 0)

    def test_older_bookings_get_the_reminder_without_it(self):
        reservation = self.old_booking()
        self.overdue(reservation, 15)
        reminders.send_due_reminders(self.today)
        self.assertNotIn("more than your security deposit", self.messages()[0])

    def test_marking_it_returned_offers_to_collect(self):
        reservation = self.booked()
        self.overdue(reservation, 11)
        pay(Reservation.objects.get(pk=reservation.pk), Payment.Purpose.BALANCE, "3500")
        response = self.post("reservation_item_mark_returned", self.item_of(reservation).pk, body={"condition": "Good"})
        money = response.json()["money"]
        self.assertEqual(money["stillOwed"], "200.00")
        self.assertTrue(money["cashUrl"].endswith(f"open={reservation.pk}&pay=cash"))
        self.assertTrue(money["qrUrl"].endswith(f"open={reservation.pk}&pay=qr"))


# ---------------------------------------------------------------- walk-in
class WalkInLaterReturnTests(OverBase):
    def setUp(self):
        super().setUp()
        id_rule_off()

    def form(self, *, ret=13, amount="2200", payment="deposit", **extra):
        line = {"gown": self.gown.id, "event": self.event.isoformat(), "takes_now": False, "picked_up_on": "", "return": self.day(ret).isoformat()}
        data = {"channel": "Walk-in", "name": "Lorna Reyes", "phone": "09171234567", "address": "Antipolo", "lines": json.dumps([line]),
                "payment": payment, "method": "Cash", "amount": amount, "note": ""}
        data.update(extra)
        return data

    def test_the_calendar_check_says_how_late_it_can_be(self):
        url = reverse("arabela_admin:availability_check")
        params = {"gown": self.gown.id, "event": self.event.isoformat()}
        plain = self.admin.get(url, params).json()
        self.assertEqual((plain["returnMin"], plain["returnDate"], plain["extraFee"], plain["returnOpen"]),
                         (self.day(2).isoformat(),) * 2 + (0, True))
        later = self.admin.get(url, {**params, "return": self.day(13).isoformat()}).json()
        self.assertEqual((later["ok"], later["returnDate"], later["extraDays"], later["extraFee"]), (True, self.day(13).isoformat(), 11, 2200))
        self.assertIn("Later return fee: 11 days", later["extraText"])
        GownUnavailability.objects.create(gown=self.gown, start_date=self.day(10), end_date=self.day(11), reason=GownUnavailability.Reason.REPAIR)
        taken = self.admin.get(url, {**params, "return": self.day(13).isoformat()}).json()
        self.assertEqual((taken["ok"], taken["reason"], taken["returnMax"], taken["returnOpen"]), (False, "return_taken", self.day(9).isoformat(), False))
        early = self.admin.get(url, {**params, "return": self.day(1).isoformat()}).json()
        self.assertEqual((early["ok"], early["reason"]), (False, "return_early"))

    def test_cash_includes_what_is_over_the_deposit(self):
        short = self.admin.post(reverse("arabela_admin:booking_create"), self.form(amount="2000"))
        self.assertEqual(short.status_code, 400, short.content)
        self.assertIn("₱2,200.00", short.json()["error"])
        self.assertFalse(Reservation.objects.exists())
        response = self.admin.post(reverse("arabela_admin:booking_create"), self.form())
        self.assertEqual(response.status_code, 200, response.content)
        reservation = Reservation.objects.get()
        item = reservation.items.get()
        self.assertEqual((reservation.pays_over_deposit, reservation.upfront_extra, item.return_date, item.original_return_date),
                         (True, D("200"), self.day(13), self.day(13)))
        payment = reservation.payments.get()
        self.assertEqual((payment.amount, payment.extra_part), (D("2200"), D("200")))
        self.assertEqual(GownUnavailability.objects.get(auto_for_item=item).start_date, self.day(14))
        s = self.statement(reservation)
        self.assertEqual((s.extra_owed, s.balance_due, s.later_fee), (D("0"), D("3500"), D("2200")))

    def test_the_qr_code_asks_for_it_too(self):
        data = self.form(expected_amount="2200")
        response = self.admin.post(reverse("arabela_admin:booking_pay_start"), data)
        self.assertEqual(response.status_code, 200, response.content)
        attempt = PaymentAttempt.objects.get(token=response.json()["token"])
        self.assertEqual(attempt.amount, D("2200"))
        self.fake.paid(attempt.session_id)
        PaymentAttempt.objects.filter(pk=attempt.pk).update(last_checked_at=None)
        status = self.admin.get(reverse("arabela_admin:booking_pay_status", args=[attempt.token])).json()
        self.assertEqual(status["state"], "booked", status)
        reservation = Reservation.objects.get()
        self.assertEqual((reservation.upfront_extra, reservation.payments.get().extra_part, reservation.items.get().return_date),
                         (D("200"), D("200"), self.day(13)))

    def test_a_deposit_voided_later_is_asked_for_with_the_extra_days_again(self):
        self.assertEqual(self.admin.post(reverse("arabela_admin:booking_create"), self.form()).status_code, 200)
        reservation = Reservation.objects.get()
        payments.void_payment(payment_id=reservation.payments.get().pk, reason="typed by mistake", user=self.staff)
        choices = payments.choices(self.statement(reservation))
        self.assertEqual([(o["amount"], o["over"]) for o in choices["options"]], [("2200.00", "200.00"), ("5700.00", "200.00")])
        with self.assertRaises(payments.PaymentError):                      # the deposit alone is no longer enough
            payments.record_payment(reservation_id=reservation.pk, purpose=Payment.Purpose.DEPOSIT, amount=D("2000"), method=Payment.Method.CASH, user=self.staff)
        again = payments.record_payment(reservation_id=reservation.pk, purpose=Payment.Purpose.DEPOSIT, amount=D("2200"), method=Payment.Method.CASH,
                                        user=self.staff)
        self.assertEqual(again.extra_part, D("200"))
        self.assertEqual(self.statement(reservation).extra_owed, D("0"))


# ---------------------------------------------------------------- the terms
class TermsSameDayTests(OverBase):
    def test_agreeing_earlier_on_the_day_of_a_change_is_asked_again(self):
        profile, _ = UserProfile.objects.get_or_create(user=self.ana)
        moment = timezone.make_aware(datetime.combine(terms.TERMS_UPDATED_ON, terms.TERMS_UPDATED_TIME))
        UserProfile.objects.filter(pk=profile.pk).update(terms_accepted_at=moment - timedelta(minutes=1))
        self.assertTrue(terms.needs_to_accept(User.objects.get(pk=self.ana.pk)))
        UserProfile.objects.filter(pk=profile.pk).update(terms_accepted_at=moment)
        self.assertFalse(terms.needs_to_accept(User.objects.get(pk=self.ana.pk)))
