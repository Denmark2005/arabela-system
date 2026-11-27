"""The security deposit handed back, and the paper that says why it is that amount.

At the counter (cash) and automatically to the customer's online account (PayMongo), the deposit goes back LESS the late fee and any deductions --
never more -- and the receipt (the shop's print and the customer's own copy) says which gowns came back, the day each was due back (the ORIGINAL
return date the late fee counts from), the day it came back, the late fee line by line, each deduction, the amount returned and one plain sentence
saying why. With no late fee and no deductions it says the deposit is returned in full. The customer's bell message says it too.

When the late fee is MORE than the deposit, nothing goes back: the customer pays the rest, and that receipt shows why it is owed."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomerMessage, UserProfile
from arabela_admin import test_online_payments_admin as online_helpers
from reservations import ledger, payments
from reservations.models import Deduction, Payment, Reservation, ReservationItem
from reservations.test_ledger import make_booking

User = get_user_model()
D = Decimal
P = Payment.Purpose
M = Payment.Method


def day(offset):
    """'Nov 29, 2026' for today + offset days: how the receipt writes a day."""
    return ledger.long_day(timezone.localdate() + timedelta(days=offset))


def paper(html):
    """Just the receipt paper of a page (the print page and the customer's page wrap it in buttons and help text)."""
    start = html.index('id="rc-paper"')
    ends = [i for i in (html.find("<script", start), html.find("</body>", start)) if i != -1]
    return html[start:min(ends)] if ends else html[start:]


class CashReturnTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("dr_owner", "dr.owner@gmail.com", "x", first_name="Olive", last_name="Owner")
        UserProfile.objects.update_or_create(user=cls.owner, defaults={"role": UserProfile.Role.OWNER})
        cls.juan = User.objects.create_user("dr_juan", "dr.juan@gmail.com", "x", first_name="Juan")

    def returned(self, *, ret, returned_on, **kw):
        """A walk-in booking, paid in full (₱3,500 rental + ₱2,000 deposit), whose gown is back."""
        booking = make_booking(stage=ReservationItem.Stage.RETURNED, ret=ret, returned_on=returned_on, customer=self.juan, **kw)
        payments.record_payment(reservation_id=booking.pk, purpose=P.FULL, amount=D("5500"), method=M.CASH, user=self.owner)
        return booking

    def give_back(self, booking, amount, **kw):
        return payments.return_deposit(reservation_id=booking.pk, amount=D(amount), method=kw.pop("method", M.CASH), user=self.owner, **kw)

    def printed(self, payment):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("arabela_admin:payment_receipt_print", args=[payment.pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def customer_copy(self, payment):
        self.client.force_login(self.juan)
        response = self.client.get(reverse("gowns:receipt_view", args=[payment.pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def notice(self):
        return CustomerMessage.objects.filter(recipient=self.juan).latest("id").body

    def test_a_late_gown_the_deposit_goes_back_less_the_late_fee_and_the_receipt_says_why(self):
        booking = self.returned(ret=-6, returned_on=-3)                     # due back 6 days ago, came back 3 days ago: 3 days x ₱200 = ₱600
        with self.assertRaises(payments.PaymentError):
            self.give_back(booking, "1400.01")                                # never more than the deposit less the late fee
        payment = self.give_back(booking, "1400")
        r = payment.snapshot["return"]
        self.assertEqual((r["deposit_paid"], r["late_fee"], r["deductions_total"], r["returned_now"], r["still_to_return"]),
                         ("2000.00", "600.00", "0.00", "1400.00", "0.00"))
        self.assertEqual(r["gowns"], [{"name": "Wedding Gown 1", "size": "Large", "due": day(-6), "back": day(-3), "late_days": 3, "agreed": day(-6), "overdue_days": 3}])
        self.assertEqual((r["change_fee"], r["overdue_fee"]), ("0.00", "600.00"))
        self.assertEqual((r["late_lines"][0]["due"], r["late_lines"][0]["returned"], r["late_lines"][0]["amount"]), (day(-6), day(-3), "600.00"))
        self.assertEqual(r["why"], "The ₱2,000.00 security deposit less the ₱600.00 overdue fee leaves ₱1,400.00 to give back.")

        for html in (paper(self.printed(payment)), paper(self.customer_copy(payment))):
            for piece in ("GOWN RETURNED", "Wedding Gown 1 (Large)", f"Due back {day(-6)}", f"Returned {day(-3)}", "3 days overdue", "Overdue fee",
                          "SECURITY DEPOSIT", "Security deposit paid", "&#8369;2,000.00", "-&#8369;600.00", "3 days x &#8369;200 = &#8369;600.00",
                          "DEPOSIT RETURNED", "&#8369;1,400.00", "Returned by", "Cash", r["why"]):
                with self.subTest(piece=piece):
                    self.assertIn(piece, html)
            self.assertNotIn("Pick-up ", html)                                # the booked dates list gives way to the gown returned
            self.assertNotIn("Sent back to the account", html)                # cash: nothing was sent online
            self.assertNotIn("Late fee", html)
            self.assertNotIn("Date change fee", html)                         # nothing was moved
        self.assertIn("CUSTOMER COPY", self.customer_copy(payment))
        self.assertIn("Your ₱2,000.00 deposit less the ₱600.00 overdue fee comes to ₱1,400.00. The receipt shows the details.", self.notice())

    def test_on_time_with_no_deductions_the_whole_deposit_goes_back_and_the_receipt_says_so(self):
        booking = self.returned(ret=-3, returned_on=-4)                     # came back a day early: no late fee
        payment = self.give_back(booking, "2000")
        r = payment.snapshot["return"]
        self.assertEqual((r["late_fee"], r["deductions_total"], r["returned_now"]), ("0.00", "0.00", "2000.00"))
        self.assertEqual(r["why"], "No fees and no deductions: the security deposit is returned in full.")
        html = paper(self.printed(payment))
        for piece in ("GOWN RETURNED", f"Due back {day(-3)}", f"Returned {day(-4)}", "On time", r["why"], "&#8369;2,000.00"):
            with self.subTest(piece=piece):
                self.assertIn(piece, html)
        self.assertNotIn("-&#8369;0.00", html)                                # a zero is not taken off anything
        self.assertNotIn("comes to", self.notice())
        self.assertIn("Your security deposit of ₱2,000.00", self.notice())

    def test_a_late_fee_and_a_deduction_both_come_off_and_both_are_listed(self):
        booking = self.returned(ret=-5, returned_on=-3)                     # 2 days late: ₱400
        payments.add_deduction(reservation_id=booking.pk, amount=D("300"), reason="Stain on the skirt", user=self.owner)
        with self.assertRaises(payments.PaymentError):
            self.give_back(booking, "1400")                                   # 2,000 - 400 - 300 = 1,300 at most
        payment = self.give_back(booking, "1300")
        r = payment.snapshot["return"]
        self.assertEqual((r["late_fee"], r["deductions_total"], r["returned_now"]), ("400.00", "300.00", "1300.00"))
        self.assertEqual(r["deductions"], [{"reason": "Stain on the skirt", "amount": "300.00"}])
        self.assertEqual(r["why"], "The ₱2,000.00 security deposit less the ₱400.00 overdue fee and ₱300.00 of deductions leaves ₱1,300.00 to give back.")
        for html in (paper(self.printed(payment)), paper(self.customer_copy(payment))):
            for piece in ("2 days overdue", "2 days x &#8369;200 = &#8369;400.00", "-&#8369;400.00", "Stain on the skirt: -&#8369;300.00",
                          "-&#8369;300.00", "&#8369;1,300.00", r["why"]):
                with self.subTest(piece=piece):
                    self.assertIn(piece, html)
        self.assertIn("Your ₱2,000.00 deposit less the ₱400.00 overdue fee and ₱300.00 of deductions comes to ₱1,300.00.", self.notice())

    def test_a_deposit_handed_back_in_two_parts_keeps_every_sentence_true(self):
        booking = self.returned(ret=-6, returned_on=-3)                     # ₱600 late fee, ₱1,400 to give back in all
        first = self.give_back(booking, "1000")
        self.assertEqual((first.snapshot["return"]["returned_now"], first.snapshot["return"]["still_to_return"]), ("1000.00", "400.00"))
        self.assertIn("leaves ₱1,400.00 to give back", first.snapshot["return"]["why"])
        self.assertIn("Your security deposit of ₱1,000.00", self.notice())
        self.assertIn("comes to ₱1,400.00", self.notice())
        second = self.give_back(booking, "400", method=M.GCASH, reference="5046933479702")
        r = second.snapshot["return"]
        self.assertEqual((r["returned_before"], r["returned_now"], r["still_to_return"]), ("1000.00", "400.00", "0.00"))
        html = paper(self.printed(second))
        for piece in ("Handed back before", "&#8369;1,000.00", "DEPOSIT RETURNED", "&#8369;400.00", "GCash ref no."):
            with self.subTest(piece=piece):
                self.assertIn(piece, html)
        with self.assertRaises(payments.PaymentError):
            self.give_back(booking, "1")                                      # nothing is left

        on_time = self.returned(ret=-3, returned_on=-3)
        part = self.give_back(on_time, "500")
        self.assertEqual(part.snapshot["return"]["why"], "No fees and no deductions: the whole ₱2,000.00 security deposit goes back to the customer.")

    def test_a_return_date_moved_later_still_counts_the_fee_from_the_day_first_booked(self):
        booking = self.returned(ret=-2, returned_on=-2, original_return=-5)   # booked back 5 days ago, moved to 2 days ago, came back then
        payment = self.give_back(booking, "1400")                             # 3 days x ₱200 = ₱600 off
        r = payment.snapshot["return"]
        self.assertEqual((r["gowns"][0]["due"], r["gowns"][0]["late_days"], r["late_fee"]), (day(-5), 3, "600.00"))
        self.assertEqual((r["change_fee"], r["overdue_fee"], r["gowns"][0]["agreed"], r["gowns"][0]["overdue_days"]), ("600.00", "0.00", day(-2), 0))
        self.assertEqual(r["why"], "The ₱2,000.00 security deposit less the ₱600.00 date change fee leaves ₱1,400.00 to give back.")
        html = paper(self.printed(payment))
        for piece in (f"Booked return {day(-5)}", f"Moved to {day(-2)}", "On time", "Date change fee", "-&#8369;600.00", "3 days x &#8369;200 = &#8369;600.00"):
            with self.subTest(piece=piece):
                self.assertIn(piece, html)
        self.assertNotIn("Overdue fee", html)
        self.assertNotIn(f"Due back {day(-5)}", html)

    def test_a_receipt_saved_before_this_change_still_prints_the_way_it_did(self):
        booking = self.returned(ret=-6, returned_on=-3)
        payment = self.give_back(booking, "1400")
        old = {k: v for k, v in payment.snapshot.items() if k != "fee_names"}
        old["return"] = {k: v for k, v in old["return"].items() if k not in ("gowns", "why", "fee_lines", "change_fee", "overdue_fee")}
        old["return"]["late_lines"] = [{k: v for k, v in line.items() if k in ("gown", "days", "rate", "amount")} for line in old["return"]["late_lines"]]
        old["return"]["late_fee"] = "600.00"
        Payment.objects.filter(pk=payment.pk).update(snapshot=old)
        payment.refresh_from_db()
        html = paper(self.printed(payment))
        for piece in ("Pick-up ", "Late fee", "3 days x &#8369;200</div>", "-&#8369;0.00", "DEPOSIT RETURNED", "&#8369;1,400.00"):
            with self.subTest(piece=piece):
                self.assertIn(piece, html)
        self.assertNotIn("GOWN RETURNED", html)

    def test_a_late_fee_bigger_than_the_deposit_gives_nothing_back_and_the_extra_receipt_says_why(self):
        booking = self.returned(ret=-15, returned_on=-3, pickup=-20)        # 12 days x ₱200 = ₱2,400: ₱400 over the ₱2,000 deposit
        s = ledger.statement(Reservation.objects.get(pk=booking.pk))
        self.assertEqual((s.late_fee, s.refund_available, s.extra_owed), (D("2400.00"), D("0.00"), D("400.00")))
        with self.assertRaises(payments.PaymentError):
            self.give_back(booking, "1")
        extra = payments.record_payment(reservation_id=booking.pk, purpose=P.EXTRA, amount=D("400"), method=M.CASH, user=self.owner)
        x = extra.snapshot["extra"]
        self.assertEqual((x["late_fee"], x["deposit_used"], x["over"]), ("2400.00", "2000.00", "400.00"))
        html = paper(self.printed(extra))
        for piece in ("WHY THIS IS OWED", "12 days x &#8369;200 = &#8369;2,400.00", f"Due back {day(-15)}", f"Returned {day(-3)}",
                      "Overdue fee", "Taken from the deposit", "-&#8369;2,000.00", "Over the deposit", "PAID NOW: Extra charge", "&#8369;400.00",
                      "This receipt is for the part the deposit did not cover."):
            with self.subTest(piece=piece):
                self.assertIn(piece, html)
        self.assertNotIn("Deductions", html)                                  # none: no zero row

    def test_the_return_window_carries_the_due_day_of_each_late_line(self):
        booking = self.returned(ret=-5, returned_on=-3)
        payments.add_deduction(reservation_id=booking.pk, amount=D("300"), reason="Stain on the skirt", user=self.owner)
        self.client.force_login(self.owner)
        data = self.client.get(reverse("arabela_admin:payment_detail", args=[booking.pk])).json()
        self.assertEqual((data["feeLines"][0]["kind"], data["feeLines"][0]["due"]), ("overdue", day(-5)))
        self.assertEqual((data["peso"]["deposit_paid"], data["peso"]["refund_available"], data["canReturn"]), ("2,000.00", "1,300.00", True))
        page = self.client.get(reverse("arabela_admin:payments")).content.decode()
        for piece in ("How the amount to hand back is worked out", "To hand back now", "Already handed back", "No fees and no deductions: the whole deposit goes back.",
                      "retShown()", "workingText()", "Send deposit back"):
            with self.subTest(piece=piece):
                self.assertIn(piece, page)


class OnlineReturnTests(online_helpers.Base):
    """The customer paid the deposit on PayMongo (GCash): the deposit goes back to that account, less the late fee."""

    def late_online_booking(self, *, booked_back=-3, moved_to=-1, came_back=0):
        booking = self.booked("deposit", "gcash")
        today = timezone.localdate()
        Reservation.objects.filter(pk=booking.pk).update(late_fee_per_day=200)
        # A booking made before the free-days rule (no free days): its fee counts from the return first booked, as these tests describe.
        ReservationItem.objects.filter(reservation=booking).update(
            stage=ReservationItem.Stage.RETURNED, original_return_date=today + timedelta(days=booked_back), return_date=today + timedelta(days=moved_to),
            returned_on=today + timedelta(days=came_back), free_pickup_date=None, free_return_date=None)
        return Reservation.objects.get(pk=booking.pk)

    def refund(self, booking, amount):
        return payments.return_deposit_online(reservation_id=booking.pk, amount=D(amount), user=self.owner)

    def test_the_online_refund_sends_the_deposit_less_the_late_fee_and_the_receipt_says_why(self):
        booking = self.late_online_booking()                                # booked back 3 days ago (moved to yesterday), back today: ₱600
        s = ledger.statement(booking)
        self.assertEqual((s.deposit_paid, s.late_fee, s.refund_available), (D("2000.00"), D("600.00"), D("1400.00")))
        with self.assertRaises(payments.PaymentError):
            self.refund(booking, "2000")                                      # the whole deposit is refused...
        self.assertEqual(self.fake.refunds, {})                               # ...and nothing was sent
        payment = self.refund(booking, "1400")
        self.assertEqual(self.fake.refunds["ref_0001"]["amount"], 140000)     # PayMongo is asked for exactly ₱1,400.00
        r = payment.snapshot["return"]
        self.assertEqual((r["late_fee"], r["returned_now"], r["gowns"][0]["due"], r["gowns"][0]["late_days"]), ("600.00", "1400.00", day(-3), 3))
        self.assertEqual((r["change_fee"], r["overdue_fee"], r["gowns"][0]["overdue_days"]), ("400.00", "200.00", 1))     # moved 2 days, back 1 day after that
        self.assertEqual(r["why"], "The ₱2,000.00 security deposit less the ₱400.00 date change fee and the ₱200.00 overdue fee leaves ₱1,400.00 to give back.")

        self.client.force_login(self.owner)
        shop = paper(self.client.get(reverse("arabela_admin:payment_receipt_print", args=[payment.pk])).content.decode())
        self.client.force_login(self.ana)
        mine = self.client.get(reverse("gowns:receipt_view", args=[payment.pk])).content.decode()
        self.assertIn("CUSTOMER COPY", mine)
        for html in (shop, paper(mine)):
            for piece in ("GOWN RETURNED", f"Booked return {day(-3)}", f"Moved to {day(-1)}", "1 day overdue", "-&#8369;400.00", "-&#8369;200.00", "DEPOSIT RETURNED", "&#8369;1,400.00",
                          "Sent back to the account the customer paid with. It can take up to a day to show.", r["why"]):
                with self.subTest(piece=piece):
                    self.assertIn(piece, html)
        body = CustomerMessage.objects.filter(recipient=self.ana, category="Payment").latest("id").body
        self.assertIn("Your security deposit of ₱1,400.00", body)
        self.assertIn("Your ₱2,000.00 deposit less the ₱400.00 date change fee and the ₱200.00 overdue fee comes to ₱1,400.00.", body)
        self.assertIn("up to a day", body)

    def test_on_time_the_whole_deposit_goes_back_online_and_the_receipt_says_so(self):
        booking = self.late_online_booking(booked_back=0, moved_to=0, came_back=0)
        payment = self.refund(booking, "2000")
        self.assertEqual(self.fake.refunds["ref_0001"]["amount"], 200000)
        self.assertEqual(payment.snapshot["return"]["why"], "No fees and no deductions: the security deposit is returned in full.")
        body = CustomerMessage.objects.filter(recipient=self.ana, category="Payment").latest("id").body
        self.assertNotIn("comes to", body)

    def test_a_refund_the_service_refuses_leaves_a_void_receipt_without_the_sent_line(self):
        from reservations import paymongo
        booking = self.late_online_booking()
        self.fake.fail["create_refund"] = paymongo.PaymongoError("payment_not_refundable", status=400)
        with self.assertRaises(payments.PaymentError):
            self.refund(booking, "1400")
        voided = Payment.objects.get(reservation=booking, direction=Payment.Direction.OUT)
        self.assertTrue(voided.is_void)
        self.client.force_login(self.owner)
        html = paper(self.client.get(reverse("arabela_admin:payment_receipt_print", args=[voided.pk])).content.decode())
        self.assertIn("VOID", html)
        self.assertNotIn("Sent back to the account", html)
