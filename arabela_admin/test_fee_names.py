"""The one fee for days past the booked return date, under the two names people read (reservations/ledger.py):

  Date change fee   the days the shop moved the return date (agreed with the customer)
  Overdue fee       the days past the date agreed now, while the booking shows as Overdue; "days overdue" counts these

The money is the same per gown per day and nothing else changes. Receipts issued before keep their own words (reservations/test_deposit_return_receipts.py).
Numbers: ₱3,500 rental + ₱2,000 deposit, ₱200 a day (reservations/test_ledger.make_booking; days are offsets from today)."""
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditLog, UserProfile
from reservations import ledger, payments
from reservations.models import Payment, ReservationItem
from reservations.test_ledger import make_booking, stmt

User = get_user_model()
D = Decimal
STAGE = ReservationItem.Stage
P = Payment.Purpose
M = Payment.Method
TEMPLATES = Path(settings.BASE_DIR) / "templates"


def day(offset):
    return ledger.long_day(timezone.localdate() + timedelta(days=offset))


def parts(statement):
    return [(line["kind"], line["days"], line["growing"]) for line in statement.fee_lines]


class SplitTests(TestCase):
    def test_a_return_moved_later_is_a_date_change_fee_while_the_new_date_has_not_passed(self):
        s = stmt(make_booking(stage=STAGE.RESERVED, pickup=-6, ret=1, original_return=-3))          # booked back 3 days ago, moved to tomorrow, still out
        self.assertEqual((s.late_fee, s.change_fee, s.overdue_fee, s.overdue_now, s.late_now), (D("600"), D("600"), D("0"), False, True))
        self.assertEqual(parts(s), [("change", 3, True)])
        self.assertEqual((s.fee_lines[0]["label"], s.fee_lines[0]["booked"], s.fee_lines[0]["agreed"]), ("Date change fee", day(-3), day(1)))

    def test_the_date_change_part_stops_growing_on_the_new_date(self):
        s = stmt(make_booking(stage=STAGE.RESERVED, ret=0, original_return=-2))
        self.assertEqual(parts(s), [("change", 2, False)])

    def test_past_the_new_date_the_extra_days_are_the_overdue_fee(self):
        # moved 7 days later than booked, and now 2 days past that new date
        s = stmt(make_booking(stage=STAGE.OVERDUE, pickup=-12, ret=-2, original_return=-9))
        self.assertEqual((s.change_fee, s.overdue_fee, s.late_fee, s.overdue_now), (D("1400"), D("400"), D("1800"), True))
        self.assertEqual(parts(s), [("change", 7, False), ("overdue", 2, True)])
        self.assertEqual([line["due"] for line in s.fee_lines], [day(-9), day(-2)])
        self.assertEqual((s.change_days_total, s.overdue_days_total, s.late_days_total), (7, 2, 9))

    def test_a_date_never_moved_is_all_overdue(self):
        s = stmt(make_booking(stage=STAGE.OVERDUE, ret=-2))
        self.assertEqual((s.change_fee, s.overdue_fee, s.overdue_now), (D("0"), D("400"), True))

    def test_back_before_the_new_date_pays_only_for_the_days_kept(self):
        s = stmt(make_booking(stage=STAGE.RETURNED, pickup=-12, ret=-1, original_return=-8, returned_on=-4))
        self.assertEqual((s.change_fee, s.overdue_fee, s.overdue_now, s.late_now), (D("800"), D("0"), False, False))

    def test_back_after_the_new_date_has_both_final(self):
        s = stmt(make_booking(stage=STAGE.RETURNED, pickup=-12, ret=-3, original_return=-8, returned_on=-1))
        self.assertEqual((s.change_fee, s.overdue_fee, s.overdue_now, s.late_now), (D("1000"), D("400"), False, False))

    def test_the_two_names_always_add_up_to_the_same_money_as_before(self):
        today = timezone.localdate()
        for original in (-6, -3, 0, 2):
            for moved in (0, 2, 5):
                for stage, back in ((STAGE.PICKUP, None), (STAGE.RESERVED, None), (STAGE.OVERDUE, None), (STAGE.RETURNED, -1), (STAGE.RETURNED, 0)):
                    with self.subTest(original=original, moved=moved, stage=stage, back=back):
                        booking = make_booking(stage=stage, pickup=-10, ret=original + moved, original_return=original, returned_on=back)
                        s = stmt(booking)
                        item = booking.items.get()
                        self.assertEqual(s.change_fee + s.overdue_fee, s.late_fee)
                        self.assertEqual(s.late_fee, D("200") * ledger.late_days(item, today))          # the same total the old single fee had
                        self.assertLessEqual(ledger.overdue_days(item, today), ledger.late_days(item, today))

    def test_an_old_row_whose_current_date_is_before_the_booked_one_is_never_a_date_change(self):
        s = stmt(make_booking(stage=STAGE.RETURNED, pickup=-12, ret=-6, original_return=-3, returned_on=-1))
        self.assertEqual((s.change_fee, s.overdue_fee), (D("0"), D("400")))

    def test_the_extra_payment_reads_extra_charge(self):
        self.assertEqual(Payment(purpose=P.EXTRA).get_purpose_display(), "Extra charge")
        self.assertEqual(Payment(purpose=P.BALANCE).get_purpose_display(), "Balance")
        self.assertEqual(Payment(purpose=P.DEPOSIT_RETURN).get_purpose_display(), "Deposit return")


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("fn_owner", "fn.owner@gmail.com", "x", first_name="Olive")
        UserProfile.objects.update_or_create(user=cls.owner, defaults={"role": UserProfile.Role.OWNER})
        cls.juan = User.objects.create_user("fn_juan", "fn.juan@gmail.com", "x", first_name="Juan")

    def page(self, name, user=None):
        self.client.force_login(user or self.owner)
        response = self.client.get(reverse(f"arabela_admin:{name}"))
        self.assertEqual(response.status_code, 200)
        return response

    def pay(self, booking, purpose, amount):
        return payments.record_payment(reservation_id=booking.pk, purpose=purpose, amount=D(amount), method=M.CASH, user=self.owner)

    def customer_section(self, booking):
        self.client.force_login(self.juan)
        response = self.client.get(reverse("gowns:reservation_item_detail", args=[booking.items.first().pk]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        section = html[html.index('id="payments"'):]
        return section[:section.index("</section>")]


class PageTests(Base):
    def test_active_reservations_shows_both_labels_for_a_moved_gown_that_is_now_overdue(self):
        make_booking(stage=STAGE.OVERDUE, pickup=-12, ret=-2, original_return=-9)
        html = self.page("active_reservations").content.decode()
        self.assertIn("Date change fee &#8369;1,400</a>", html)
        self.assertIn('class="ar-feechip ar-feechip--over"', html)
        self.assertIn("Overdue fee &#8369;400</a>", html)
        self.assertIn("7 days &times; &#8369;200, return moved to", html)
        self.assertIn("2 days &times; &#8369;200 so far, growing each day it is out", html)
        self.assertIn('title="2 days overdue"', html)
        self.assertNotIn("Late fee", html)

    def test_a_gown_not_picked_up_yet_shows_only_the_date_change_fee(self):
        make_booking(stage=STAGE.PICKUP, pickup=1, ret=5, original_return=3)
        html = self.page("active_reservations").content.decode()
        self.assertIn("Date change fee &#8369;400</a>", html)
        self.assertNotIn("Overdue fee &#8369;", html)

    def test_the_date_window_knows_which_gown_is_out(self):
        out = make_booking(stage=STAGE.RESERVED, pickup=-1, ret=3).items.get()
        waiting = make_booking(stage=STAGE.PICKUP, pickup=2, ret=6).items.get()
        html = self.page("active_reservations").content.decode()
        self.assertIn(f'data-kind="return" data-item-id="{out.pk}" data-out="1"', html)
        self.assertIn(f'data-kind="return" data-item-id="{waiting.pk}" data-out="0"', html)

    def test_the_payments_overdue_filter_counts_only_gowns_past_their_agreed_date(self):
        moved = make_booking(stage=STAGE.RESERVED, pickup=-6, ret=2, original_return=-3)        # a date change fee only
        late = make_booking(stage=STAGE.OVERDUE, ret=-2)
        response = self.page("payments")
        self.assertEqual(response.context["counts"]["late"], 1)
        html = response.content.decode()
        self.assertRegex(html, rf'data-booking-id="{moved.pk}" data-state="[a-z]+" data-late="0"')
        self.assertRegex(html, rf'data-booking-id="{late.pk}" data-state="[a-z]+" data-late="1"')
        self.assertIn('class="pm-chip pm-chip--change"', html)
        self.assertIn("Date&nbsp;change &#8369;600</span>", html)
        self.assertIn("Overdue &#8369;400</span>", html)
        self.assertNotIn("Late fee", html)

    def test_the_payments_window_lists_each_fee_with_its_reason(self):
        booking = make_booking(stage=STAGE.OVERDUE, pickup=-12, ret=-2, original_return=-9)
        self.client.force_login(self.owner)
        data = self.client.get(reverse("arabela_admin:payment_detail", args=[booking.pk])).json()
        self.assertEqual([(l["label"], l["days"], l["amount"], l["booked"], l["agreed"], l["growing"]) for l in data["feeLines"]],
                         [("Date change fee", 7, "1,400.00", day(-9), day(-2), False), ("Overdue fee", 2, "400.00", day(-9), day(-2), True)])
        self.assertEqual((data["s"]["change_fee"], data["s"]["overdue_fee"], data["peso"]["change_fee"], data["peso"]["overdue_fee"]),
                         ("1400.00", "400.00", "1,400.00", "400.00"))
        self.assertNotIn("fee_lines", data["s"])

    def test_security_deposits_names_both_fees(self):
        booking = make_booking(stage=STAGE.RETURNED, pickup=-12, ret=-3, original_return=-8, returned_on=-1)
        self.pay(booking, P.FULL, "5500")
        html = self.page("security_deposits").content.decode()
        self.assertIn("<span>Date change fee &#8369;1,000</span>", html)
        self.assertIn("<span>Overdue fee &#8369;400</span>", html)
        self.assertNotIn("Late fee", html)

    def test_rental_history_calls_a_gown_returned_on_its_new_date_on_time(self):
        booking = make_booking(stage=STAGE.RETURNED, pickup=-12, ret=-3, original_return=-8, returned_on=-3)
        response = self.page("rental_history")
        item = next(i for i in response.context["rental_items"] if i.reservation_id == booking.pk)
        self.assertEqual(item.late_days, 0)
        self.assertEqual(stmt(booking).change_fee, D("1000"))                                   # the fee is still charged, as the date change fee

    def test_the_customer_page_explains_a_moved_date_without_asking_to_return_it_early(self):
        booking = make_booking(customer=self.juan, stage=STAGE.RESERVED, pickup=-6, ret=2, original_return=-3)
        self.pay(booking, P.FULL, "5500")
        section = self.customer_section(booking)
        for piece in ("Date change fee", "Wedding Gown 1: 3 days x PHP 200 = <strong>PHP 600.00</strong> so far",
                      f"Your return date was moved from {day(-3)} to {day(2)}.", "A date change fee is taken from your security deposit."):
            with self.subTest(piece=piece):
                self.assertIn(piece, section)
        self.assertNotIn("Overdue fee so far", section)
        self.assertNotIn("Return the gown to stop it growing.", section)
        self.assertNotIn("Late fee", section)

    def test_the_customer_page_shows_both_fees_once_overdue(self):
        booking = make_booking(customer=self.juan, stage=STAGE.OVERDUE, pickup=-12, ret=-2, original_return=-9)
        self.pay(booking, P.FULL, "5500")
        section = self.customer_section(booking)
        for piece in ("Date change fee", "PHP 1,400.00</strong></p>", "Overdue fee so far", "PHP 400.00</strong> so far", f"It was due back {day(-2)}.",
                      "Return the gown to stop it growing."):
            with self.subTest(piece=piece):
                self.assertIn(piece, section)

    def test_a_receipt_for_money_over_the_deposit_lists_both_fees(self):
        booking = make_booking(stage=STAGE.RETURNED, pickup=-30, ret=-9, original_return=-20, returned_on=-2)     # 11 moved + 7 overdue days = ₱3,600
        self.pay(booking, P.FULL, "5500")
        extra = self.pay(booking, P.EXTRA, "1600")
        x = extra.snapshot["extra"]
        self.assertEqual((x["change_fee"], x["overdue_fee"], x["late_fee"], x["over"]), ("2200.00", "1400.00", "3600.00", "1600.00"))
        self.client.force_login(self.owner)
        html = self.client.get(reverse("arabela_admin:payment_receipt_print", args=[extra.pk])).content.decode()
        paper = " ".join(html[html.index('id="rc-paper"'):].split())
        for piece in ("WHY THIS IS OWED", "Date change fee", "11 days x &#8369;200 = &#8369;2,200.00", f"Booked return {day(-20)}", f"Moved to {day(-9)}",
                      "Overdue fee", "7 days x &#8369;200 = &#8369;1,400.00", f"Due back {day(-9)}", f"Returned {day(-2)}", "PAID NOW: Extra charge",
                      "The fees and deductions were more than the security deposit."):
            with self.subTest(piece=piece):
                self.assertIn(piece, paper)
        self.assertNotIn("Late fee", paper)

    def test_the_profile_card_and_the_audit_log_name_both(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("arabela_admin:page", kwargs={"page": "profile"})).content.decode()
        for piece in ("Overdue &amp; date change fee", "Fee for each gown, each day (pesos)", "'Save Fee'", "3 days overdue at"):
            with self.subTest(piece=piece):
                self.assertIn(piece, html)
        self.assertNotIn("Late fee", html)
        response = self.client.post(reverse("arabela_admin:late_fee_setting_update"), data='{"amount": "250"}', content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("changed the overdue / date change fee per gown per day from ₱200 to ₱250", AuditLog.objects.get(action="settings.late_fee").summary)

    def test_the_monitoring_picture_fills_a_square_box_like_the_catalog(self):
        css = (TEMPLATES / "arabela_admin" / "gown-monitoring.html").read_text(encoding="utf-8")
        box = re.search(r"\.mo-thumb \{[^}]*\}", css).group(0)
        self.assertIn("width: 3.5rem; height: 3.5rem;", box)
        self.assertIn(".mo-thumb img { display: block; width: 100%; height: 100%; object-fit: cover; }", css)
