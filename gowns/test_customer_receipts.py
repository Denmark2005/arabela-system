"""What the CUSTOMER sees of the payments (Phase 1): the Payments & Receipts section on their booking page, the receipt page in the shop's paper
look (their own receipts only), and the bell message. The rules behind the numbers: reservations/test_ledger.py and test_payments_service.py."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import CustomerMessage, UserProfile
from reservations import payments
from reservations.models import Deduction, Payment, Reservation, ReservationItem
from reservations.test_ledger import make_booking

User = get_user_model()
D = Decimal
P = Payment.Purpose
M = Payment.Method


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("cr_staff", "cr.staff@gmail.com", "x", is_staff=True, first_name="Maria", last_name="Cruz")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.STAFF})
        cls.juan = User.objects.create_user("cr_juan", "cr.juan@gmail.com", "x", first_name="Juan")
        cls.rita = User.objects.create_user("cr_rita", "cr.rita@gmail.com", "x", first_name="Rita")

    def record(self, booking, purpose, amount, **kw):
        return payments.record_payment(reservation_id=booking.pk, purpose=purpose, amount=D(amount), method=kw.pop("method", M.CASH), user=self.staff, **kw)

    def page(self, booking, user=None):
        self.client.force_login(user or self.juan)
        item = booking.items.first()
        response = self.client.get(reverse("gowns:reservation_item_detail", args=[item.pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class ReceiptPageTests(Base):
    def test_the_customer_opens_their_own_receipt_in_the_paper_look_with_the_customer_copy_label(self):
        booking = make_booking(customer=self.juan)
        payment = self.record(booking, P.DEPOSIT, "2000")
        self.client.force_login(self.juan)
        response = self.client.get(reverse("gowns:receipt_view", args=[payment.pk]))
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        for piece in ("PAYMENT RECEIPT", payment.receipt_number, booking.reference_code, "CUSTOMER COPY", "BALANCE TO PAY AT SHOP", "3,500.00", "Print or save as PDF",
                      "Back to my reservation", "Arabela Gown Rental"):
            with self.subTest(piece=piece):
                self.assertIn(piece, html)
        self.assertNotIn("REPRINT", html)
        self.assertIn("no-store", response["Cache-Control"])

    def test_the_customers_view_never_counts_as_a_print_by_the_shop(self):
        payment = self.record(make_booking(customer=self.juan), P.DEPOSIT, "2000")
        self.client.force_login(self.juan)
        for _ in range(3):
            self.client.get(reverse("gowns:receipt_view", args=[payment.pk]))
        payment.refresh_from_db()
        self.assertEqual((payment.first_printed_at, payment.reprint_count), (None, 0))

    def test_nobody_else_can_open_it_whatever_number_they_type(self):
        payment = self.record(make_booking(customer=self.juan), P.DEPOSIT, "2000")
        url = reverse("gowns:receipt_view", args=[payment.pk])
        self.assertEqual(self.client.get(url).status_code, 302)                          # a visitor is sent to sign in
        self.client.force_login(self.rita)
        self.assertEqual(self.client.get(url).status_code, 404)                          # another customer: nothing
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(url).status_code, 404)                          # a staff account is not the customer either (they print in the admin)
        self.client.force_login(self.juan)
        self.assertEqual(self.client.get(reverse("gowns:receipt_view", args=[999999])).status_code, 404)

    def test_a_payment_from_before_the_payments_page_has_no_paper(self):
        booking = make_booking(customer=self.juan, channel=Reservation.Channel.ONLINE)
        payments.ensure_rows(booking)
        self.client.force_login(self.juan)
        self.assertEqual(self.client.get(reverse("gowns:receipt_view", args=[Payment.objects.get().pk])).status_code, 404)

    def test_a_voided_receipt_still_opens_and_says_void(self):
        booking = make_booking(customer=self.juan)
        payment = self.record(booking, P.DEPOSIT, "2000")
        payments.void_payment(payment_id=payment.pk, reason="typed on the wrong booking", user=self.staff)
        self.client.force_login(self.juan)
        html = self.client.get(reverse("gowns:receipt_view", args=[payment.pk])).content.decode()
        for piece in ("VOID", "*** VOIDED ***", "typed on the wrong booking"):
            self.assertIn(piece, html)

    def test_the_receipt_is_the_frozen_snapshot(self):
        booking = make_booking(customer=self.juan)
        payment = self.record(booking, P.DEPOSIT, "2000")
        Reservation.objects.filter(pk=booking.pk).update(rental_subtotal=D("9999"))
        self.client.force_login(self.juan)
        html = self.client.get(reverse("gowns:receipt_view", args=[payment.pk])).content.decode()
        self.assertIn("3,500.00", html)
        self.assertNotIn("9,999", html)


class BookingPageTests(Base):
    def test_the_section_shows_the_total_what_is_paid_and_the_balance_with_the_receipts(self):
        booking = make_booking(customer=self.juan)
        payment = self.record(booking, P.DEPOSIT, "2000")
        html = self.page(booking)
        section = html[html.index('id="payments"'):]
        section = section[:section.index("</section>")]
        for piece in ("Payments &amp; Receipts", "Total to pay", "PHP 5,500.00", "Paid so far", "PHP 2,000.00", "Balance to pay at the shop", "PHP 3,500.00",
                      payment.receipt_number, "Deposit", "Cash", "View &amp; print", reverse("gowns:receipt_view", args=[payment.pk])):
            with self.subTest(piece=piece):
                self.assertIn(piece, section)
        self.assertNotIn("Overdue fee", section)
        self.assertNotIn("Date change fee", section)

    def test_nothing_left_to_pay_says_so(self):
        booking = make_booking(customer=self.juan)
        self.record(booking, P.FULL, "5500")
        section = self.page(booking)
        section = section[section.index('id="payments"'):]
        self.assertIn("Nothing left to pay", section)

    def test_the_overdue_fee_so_far_is_shown_while_the_gown_is_out(self):
        booking = make_booking(customer=self.juan, stage=ReservationItem.Stage.OVERDUE, ret=-3)
        self.record(booking, P.FULL, "5500")
        section = self.page(booking)
        section = section[section.index('id="payments"'):]
        for piece in ("Overdue fee so far", "Wedding Gown 1: 3 days x PHP 200 = <strong>PHP 600.00</strong> so far", "taken from your security deposit",
                      "Return the gown to stop it growing."):
            with self.subTest(piece=piece):
                self.assertIn(piece, section)

    def test_after_the_return_the_deposit_working_is_shown_and_then_the_amount_handed_back(self):
        booking = make_booking(customer=self.juan, stage=ReservationItem.Stage.RETURNED, ret=-6, returned_on=-3)
        self.record(booking, P.FULL, "5500")
        Deduction.objects.create(reservation=booking, amount=D("300"), reason="Stain")
        section = self.page(booking)
        section = section[section.index('id="payments"'):]
        for piece in ("Your security deposit", "Deposit paid", "- PHP 600.00", "- PHP 300.00", "To get back", "PHP 1,100.00"):
            with self.subTest(piece=piece):
                self.assertIn(piece, section)
        payments.return_deposit(reservation_id=booking.pk, amount=D("1100"), method=M.CASH, user=self.staff)
        section = self.page(booking)
        section = section[section.index('id="payments"'):]
        self.assertIn("Handed back to you", section)
        self.assertIn("Deposit returned", section)

    def test_a_voided_receipt_stays_in_the_list_marked_voided(self):
        booking = make_booking(customer=self.juan)
        payment = self.record(booking, P.DEPOSIT, "2000")
        payments.void_payment(payment_id=payment.pk, reason="wrong booking", user=self.staff)
        section = self.page(booking)
        section = section[section.index('id="payments"'):]
        self.assertIn("Voided", section)
        self.assertIn("Voided: wrong booking", section)

    def test_no_receipt_yet_says_when_they_will_appear(self):
        section = self.page(make_booking(customer=self.juan))
        section = section[section.index('id="payments"'):]
        self.assertIn("No receipt yet", section)

    def test_a_booking_still_waiting_for_approval_has_no_payments_section(self):
        html = self.page(make_booking(customer=self.juan, status=Reservation.Status.PENDING))
        self.assertNotIn('id="payments"', html)

    def test_old_payments_count_in_the_numbers_but_have_no_receipt_to_open(self):
        booking = make_booking(customer=self.juan, channel=Reservation.Channel.ONLINE)       # approved before the Payments page: deposit paid online
        section = self.page(booking)
        section = section[section.index('id="payments"'):]
        self.assertIn("Paid so far", section)
        self.assertIn("PHP 2,000.00", section)
        self.assertIn("No receipt yet", section)

    def test_the_customer_only_sees_their_own_booking(self):
        booking = make_booking(customer=self.juan)
        self.client.force_login(self.rita)
        item = booking.items.first()
        self.assertEqual(self.client.get(reverse("gowns:reservation_item_detail", args=[item.pk])).status_code, 404)

    def test_gown_names_on_the_page_are_escaped(self):
        booking = make_booking(customer=self.juan, stage=ReservationItem.Stage.OVERDUE, ret=-2)
        ReservationItem.objects.filter(reservation=booking).update(gown_name="<script>alert(1)</script>")
        self.record(booking, P.FULL, "5500")
        self.assertNotIn("<script>alert(1)</script>", self.page(booking))


class MessagesTests(Base):
    def test_a_payment_message_shows_with_its_own_label_and_icon(self):
        booking = make_booking(customer=self.juan)
        payment = self.record(booking, P.DEPOSIT, "2000")
        self.client.force_login(self.juan)
        html = self.client.get(reverse("gowns:messages")).content.decode()
        self.assertIn(payment.receipt_number, html)
        self.assertIn("receipt_long", html)
        self.assertIn(">Payment<", html)
        self.assertEqual(CustomerMessage.objects.get(recipient=self.juan).category, "Payment")
