"""The printed receipts, the payment notices and the customer's own pages of a booking with days outside its free days: the same dates and money
everywhere (event Dec 15 in the numbers below: free Dec 13 - Dec 17, ₱200 per day)."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts import customer_emails
from accounts.models import CustomerMessage
from reservations import ledger, payments
from reservations.models import Payment, Reservation, ReservationItem
from reservations.test_ledger import pay

User = get_user_model()
D = Decimal
P = Payment.Purpose
S = ReservationItem.Stage


def d(value):
    return f"{value:%b} {value.day}"


def long(value):
    return f"{value:%b} {value.day}, {value.year}"


class ReceiptBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_superuser("rc_owner", "rc.owner@gmail.com", "x")
        cls.customer = User.objects.create_user("rc_customer", email="rc.customer@gmail.com", password="x")

    def setUp(self):
        self.today = timezone.localdate()
        self.event = self.today + timedelta(days=10)

    def day(self, offset):
        return self.event + timedelta(days=offset)

    def booking(self, *, pickup=-4, ret=4, booked=None, rate=200, **item_extra):
        """Picked up 2 days before the free days start and returned 2 days after they end, unless told otherwise (₱800)."""
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Ana Reyes", phone="09171234567", status=Reservation.Status.CONFIRMED,
            channel=Reservation.Channel.WALK_IN, late_fee_per_day=rate, rental_subtotal=D("3500"), security_deposit=D("2000"),
            total_amount=D("5500"), reviewed_at=timezone.now())
        fields = dict(
            rental_date=self.day(pickup), return_date=self.day(ret), event_date=self.event, original_rental_date=self.day(pickup),
            original_return_date=self.day(booked if booked is not None else ret), free_pickup_date=self.day(-2), free_return_date=self.day(2))
        fields.update(item_extra)
        ReservationItem.objects.create(reservation=reservation, gown_name="Ivory Gown", size="Medium", rental_price=D("3500"), **fields)
        return reservation

    def record(self, booking, purpose, amount):
        return payments.record_payment(reservation_id=booking.pk, purpose=purpose, amount=D(amount), method="Cash", user=self.staff)

    def print_page(self, payment):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("arabela_admin:payment_receipt_print", args=[payment.pk]))
        self.assertEqual(response.status_code, 200)
        return " ".join(response.content.decode().split())

    def customer_page(self, payment):
        self.client.force_login(self.customer)
        response = self.client.get(reverse("gowns:receipt_view", args=[payment.pk]))
        self.assertEqual(response.status_code, 200)
        return " ".join(response.content.decode().split())


class PaymentReceiptTests(ReceiptBase):
    def test_the_first_receipt_already_shows_what_comes_out_of_the_deposit(self):
        booking = self.booking()
        payment = self.record(booking, P.DEPOSIT, "2000")
        snap = payment.snapshot
        self.assertEqual(snap["gowns"][0], {"name": "Ivory Gown", "size": "Medium", "pickup": d(self.day(-4)), "return": d(self.day(4)),
                                            "event": d(self.event)})
        self.assertEqual((snap["deduct"]["total"], snap["deduct"]["back"], snap["deduct"]["over"]), ("800.00", "1200.00", "0.00"))
        self.assertEqual(snap["balance_after"], "3500.00")
        for page in (self.print_page(payment), self.customer_page(payment)):
            with self.subTest():
                self.assertIn(f"Event {d(self.event)}", page)
                self.assertIn(f"Pick-up {d(self.day(-4))} &nbsp; Return {d(self.day(4))}", page)
                self.assertIn("FROM THE DEPOSIT AFTER RETURN", page)
                self.assertIn("Early pick-up fee", page)
                self.assertIn(f"Pick-up {long(self.day(-4))}, free from {long(self.day(-2))}", page)
                self.assertIn("Later return fee", page)
                self.assertIn(f"Return {long(self.day(4))}, free until {long(self.day(2))}", page)
                self.assertIn("TOTAL DEDUCTION</span><span>-&#8369;800.00", page)
                self.assertIn("Deposit back after return</span><span>&#8369;1,200.00", page)
                self.assertIn("BALANCE TO PAY AT SHOP</span><span>&#8369;3,500.00", page)
                self.assertIn("2 days before and 2 days after the event are free", page)
                self.assertIn(f"To change your dates, call Arabela", page)
                self.assertIn(booking.reference_code, page)
        notice = CustomerMessage.objects.filter(recipient=self.customer).latest("id").body
        self.assertIn("₱800.00 comes out of your security deposit", notice)
        self.assertIn("₱1,200.00 comes back", notice)
        self.assertIn("Balance still to pay at the shop: ₱3,500.00", notice)

    def test_a_booking_inside_the_free_days_has_no_deduction_block(self):
        booking = self.booking(pickup=-2, ret=2)
        payment = self.record(booking, P.FULL, "5500")
        self.assertNotIn("deduct", payment.snapshot)
        page = self.print_page(payment)
        self.assertNotIn("FROM THE DEPOSIT", page)
        self.assertIn(f"Event {d(self.event)}", page)
        self.assertIn("PAID IN FULL", page)

    def test_the_planned_return_a_day_after_the_event_is_shown_with_the_free_day(self):
        booking = self.booking(pickup=-2, ret=2, planned_return_date=self.day(1))
        payment = self.record(booking, P.DEPOSIT, "2000")
        self.assertIn(f"Planned return {d(self.day(1))} (free until {d(self.day(2))})", self.print_page(payment))

    def test_a_return_staff_moved_back_prints_the_day_the_fee_is_counted_to(self):
        booking = self.booking(pickup=-2, ret=3, booked=5)                     # booked 3 days later, moved back to 1 day later
        payment = self.record(booking, P.DEPOSIT, "2000")
        page = self.print_page(payment)
        self.assertIn("Ivory Gown: 1 day x &#8369;200", page)
        self.assertIn(f"Return {long(self.day(3))}, free until {long(self.day(2))}", page)

    def test_the_approval_receipt_of_an_online_booking_says_nothing_was_paid_before(self):
        booking = self.booking()
        Reservation.objects.filter(pk=booking.pk).update(channel=Reservation.Channel.ONLINE, gcash_reference="1234567890123")
        payment = payments.record_approval_payment(booking.pk, user=self.staff)
        snap = payment.snapshot
        self.assertEqual((snap["amount"], snap["paid_before"], snap["balance_after"]), ("2000.00", "0.00", "3500.00"))
        self.assertEqual((snap["deduct"]["total"], snap["deduct"]["back"]), ("800.00", "1200.00"))
        page = self.print_page(payment)
        self.assertNotIn("Paid before", page)
        self.assertIn("BALANCE TO PAY AT SHOP</span><span>&#8369;3,500.00", page)

    def test_more_than_the_deposit_says_what_is_left_to_pay(self):
        booking = self.booking(pickup=-9, ret=7)                              # 7 + 5 = 12 days = ₱2,400
        payment = self.record(booking, P.DEPOSIT, "2000")
        self.assertEqual((payment.snapshot["deduct"]["back"], payment.snapshot["deduct"]["over"]), ("0.00", "400.00"))
        self.assertIn("&#8369;400.00 more than the deposit: paid at the shop.", self.print_page(payment))


class ReturnAndExtraReceiptTests(ReceiptBase):
    def returned(self, booking, back=4):
        ReservationItem.objects.filter(reservation=booking).update(stage=S.RETURNED, picked_up_on=self.day(-4), returned_on=self.day(back))

    def test_the_deposit_return_receipt_names_each_fee_and_what_goes_back(self):
        booking = self.booking()
        self.record(booking, P.FULL, "5500")
        self.returned(booking)
        s = ledger.statement(Reservation.objects.get(pk=booking.pk))
        self.assertEqual(s.refund_available, D("1200.00"))
        payment = payments.return_deposit(reservation_id=booking.pk, amount=D("1200"), method="Cash", user=self.staff)
        page = self.print_page(payment)
        self.assertIn("Security deposit paid</span><span>&#8369;2,000.00", page)
        self.assertIn("Early pick-up fee</span><span>-&#8369;400.00", page)
        self.assertIn("Later return fee</span><span>-&#8369;400.00", page)
        self.assertIn("DEPOSIT RETURNED</span><span>&#8369;1,200.00", page)
        self.assertIn("less the ₱400.00 early pick-up fee and the ₱400.00 later return fee leaves ₱1,200.00 to give back", page)
        self.assertNotIn("To change your dates", page)                            # nothing left to change
        notice = CustomerMessage.objects.filter(recipient=self.customer).latest("id").body
        self.assertIn("Your ₱2,000.00 deposit less the ₱400.00 early pick-up fee and the ₱400.00 later return fee comes to ₱1,200.00", notice)

    def test_a_late_return_adds_the_overdue_fee(self):
        booking = self.booking()
        self.record(booking, P.FULL, "5500")
        self.returned(booking, back=5)
        payment = payments.return_deposit(reservation_id=booking.pk, amount=D("1000"), method="Cash", user=self.staff)
        page = self.print_page(payment)
        self.assertIn("Overdue fee</span><span>-&#8369;200.00", page)
        self.assertIn("1 day overdue", page)
        self.assertEqual(payment.snapshot["return"]["still_to_return"], "0.00")

    def test_the_extra_charge_receipt_says_why(self):
        booking = self.booking(pickup=-9, ret=7)
        self.record(booking, P.FULL, "5500")
        self.returned(booking, back=7)
        payment = self.record(booking, P.EXTRA, "400")
        page = self.print_page(payment)
        self.assertIn("WHY THIS IS OWED", page)
        self.assertIn("Early pick-up fee</span><span>&#8369;1,400.00", page)
        self.assertIn("Later return fee</span><span>&#8369;1,000.00", page)
        self.assertIn("Over the deposit</span><span>&#8369;400.00", page)


class ExtraChargeGoesBackTests(ReceiptBase):
    def moved_back_after_paying_the_extra(self):
        booking = self.booking(pickup=-9, ret=7)                              # 12 days = ₱2,400, ₱400 more than the deposit
        self.record(booking, P.FULL, "5500")
        self.record(booking, P.EXTRA, "400")
        ReservationItem.objects.filter(reservation=booking).update(               # the shop shortens it: 2 days early, 2 days later = ₱800
            rental_date=self.day(-4), return_date=self.day(4), stage=S.RETURNED, picked_up_on=self.day(-4), returned_on=self.day(4))
        return booking

    def test_the_return_receipt_the_notice_and_the_pages_add_it_back(self):
        booking = self.moved_back_after_paying_the_extra()
        s = ledger.statement(Reservation.objects.get(pk=booking.pk))
        self.assertEqual((s.extra_credit, s.refund_available), (D("400.00"), D("1600.00")))
        self.client.force_login(self.staff)
        deposits = " ".join(self.client.get(reverse("arabela_admin:security_deposits")).content.decode().split())
        self.assertIn("Extra charge paid before &#8369;400", deposits)
        data = self.client.get(reverse("arabela_admin:payment_detail", args=[booking.pk])).json()
        self.assertEqual((data["s"]["extra_credit"], data["peso"]["extra_received"], data["peso"]["refund_available"]), ("400.00", "400.00", "1,600.00"))
        self.client.force_login(self.customer)
        page = " ".join(self.client.get(reverse("gowns:reservation_item_detail", args=[booking.items.get().pk])).content.decode().split())
        self.assertIn("Extra charge you paid before</span><span>+ PHP 400", page)
        self.assertIn("Deposit back after the return: PHP 1600.00", page)
        payment = payments.return_deposit(reservation_id=booking.pk, amount=D("1600"), method="Cash", user=self.staff)
        paper = self.print_page(payment)
        self.assertIn("Extra charge paid before</span><span>+&#8369;400.00", paper)
        self.assertIn("DEPOSIT RETURNED</span><span>&#8369;1,600.00", paper)
        self.assertIn("The ₱2,000.00 security deposit and the ₱400.00 extra charge paid before less the ₱400.00 early pick-up fee and the "
                      "₱400.00 later return fee leaves ₱1,600.00 to give back.", paper)
        notice = CustomerMessage.objects.filter(recipient=self.customer).latest("id").body
        self.assertIn("Your ₱2,000.00 deposit and the ₱400.00 extra charge you paid before less", notice)
        self.assertIn("comes to ₱1,600.00", notice)

    def test_an_ordinary_return_receipt_has_no_extra_line(self):
        booking = self.booking()
        self.record(booking, P.FULL, "5500")
        ReservationItem.objects.filter(reservation=booking).update(stage=S.RETURNED, picked_up_on=self.day(-4), returned_on=self.day(4))
        payment = payments.return_deposit(reservation_id=booking.pk, amount=D("1200"), method="Cash", user=self.staff)
        self.assertEqual(payment.snapshot["return"]["extra_paid"], "0.00")
        self.assertNotIn("Extra charge paid before", self.print_page(payment))


class CustomerPageTests(ReceiptBase):
    def test_the_booking_page_shows_event_free_days_deduction_and_who_to_call(self):
        booking = self.booking()
        self.record(booking, P.DEPOSIT, "2000")
        item = booking.items.get()
        self.client.force_login(self.customer)
        response = self.client.get(reverse("gowns:reservation_item_detail", args=[item.pk]))
        self.assertEqual(response.status_code, 200)
        page = " ".join(response.content.decode().split())
        self.assertIn(f"{self.event:%b %d, %Y}", page)
        self.assertIn("Free Days", page)
        self.assertIn("PHP 800", page)
        self.assertIn("4 booked days outside the free days", page)
        self.assertIn("Pick-up 2 days early.", page)
        self.assertIn("Return 2 days later.", page)
        self.assertIn("To change your dates, call Arabela", page)
        self.assertIn(f"Pick-up {long(self.day(-4))}; the free days start {long(self.day(-2))}.", page)
        self.assertIn(f"Return {long(self.day(4))}; the free days end {long(self.day(2))}.", page)

    def test_the_booking_email_names_the_event_and_what_comes_off_the_deposit(self):
        booking = self.booking()
        data = customer_emails._reservation_data(Reservation.objects.get(pk=booking.pk))
        built = customer_emails.build_email(customer_emails.APPROVED, data)
        for text in (built.text, " ".join(built.html.split())):
            self.assertIn(f", event {self.event:%b} {self.event.day}", text)
            self.assertIn("Off your deposit", text)
            self.assertIn("- ₱800", text)
        quiet = self.booking(pickup=-2, ret=2)
        built = customer_emails.build_email(customer_emails.APPROVED, customer_emails._reservation_data(Reservation.objects.get(pk=quiet.pk)))
        self.assertNotIn("Off your deposit", built.text)

    def test_an_older_booking_page_says_nothing_about_free_days(self):
        booking = self.booking(free_pickup_date=None, free_return_date=None)
        item = booking.items.get()
        self.client.force_login(self.customer)
        page = self.client.get(reverse("gowns:reservation_item_detail", args=[item.pk])).content.decode()
        self.assertNotIn("Free Days", page)
        self.assertNotIn("off your security deposit", page)

    def test_an_older_receipt_note_keeps_its_words(self):
        booking = self.booking(free_pickup_date=None, free_return_date=None)
        payment = self.record(booking, P.DEPOSIT, "2000")
        self.assertNotIn("deduct", payment.snapshot)
        page = self.print_page(payment)
        self.assertNotIn("2 days before and 2 days after the event are free", page)
        self.assertIn("Overdue or moved return: &#8369;200 a day", page)
        pay(booking, P.BALANCE, "10")                                            # (the helper used above keeps working)
