"""Phase 3, the admin side in the PayMongo era:

  * the Receipt window (one address that lists a booking's receipts, the paper choice, the "look without counting a print" rule) and the buttons that open it,
  * the "On this date" bar on Active Reservations, Reservation Records and Rental History,
  * the Dashboard: "Today at the shop" and "Latest transactions" (what each line says, who "View all" goes to, a failure never costs the dashboard),
  * what is HIDDEN once customers pay on PayMongo's page (Payment Verification, Pending Approval while nothing is pending, Quick Verify, the GCash cards on
    Profile) and that nothing is hidden while no PayMongo key is set (the shop still on screenshots keeps every page it has).

The money rules themselves are tested in reservations/test_ledger.py and test_payments_service.py; Gown Monitoring and Security Deposits have their own files."""
import json
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from arabela_admin import dashboard_money
from reservations import payments
from reservations.models import Payment, PaymentAttempt, Reservation, ReservationItem
from reservations.test_ledger import make_booking, pay

User = get_user_model()
D = Decimal
P = Payment.Purpose
M = Payment.Method
S = ReservationItem.Stage
ROOT = Path(settings.BASE_DIR)
PAYMONGO_ON = dict(PAYMONGO_SECRET_KEY="sk_test_unit", PAYMONGO_WEBHOOK_SECRET="whsk_unit", USING_LAPTOP_DB=False, SITE_BASE_URL="")


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("p3_owner", "p3.owner@gmail.com", "x", first_name="Olive", last_name="Owner")
        UserProfile.objects.update_or_create(user=cls.owner, defaults={"role": UserProfile.Role.OWNER})
        cls.staff = User.objects.create_user("p3_staff", "p3.staff@gmail.com", "x", is_staff=True, first_name="Sam", last_name="Staff")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.STAFF})
        cls.customer = User.objects.create_user("p3_customer", "p3.customer@gmail.com", "x")

    def setUp(self):
        self.today = timezone.localdate()

    def as_user(self, user):
        self.client.force_login(user)
        return self

    def record(self, booking, purpose, amount, **kw):
        return payments.record_payment(reservation_id=booking.pk, purpose=purpose, amount=D(amount), method=kw.pop("method", M.CASH), user=self.owner, **kw)

    def receipts(self, booking):
        return self.client.get(reverse("arabela_admin:receipt_list", args=[booking.pk]))


# ------------------------------------------------------------------------------------------------------------------------------------------------
class ReceiptListTests(Base):
    def test_only_staff_may_ask(self):
        booking = make_booking()
        response = self.receipts(booking)
        self.assertEqual((response.status_code, response.json()), (401, {"error": "Unauthorized"}))
        self.client.force_login(self.customer)
        self.assertEqual(self.receipts(booking).status_code, 401)

    def test_a_booking_that_is_not_there_says_so(self):
        self.as_user(self.staff)
        response = self.client.get(reverse("arabela_admin:receipt_list", args=[999999]))
        self.assertEqual((response.status_code, response.json()["error"]), (404, "That booking was not found."))

    def test_a_booking_without_receipts_gives_an_empty_list(self):
        booking = make_booking()
        data = self.as_user(self.staff).receipts(booking).json()
        self.assertEqual((data["receipts"], data["booking"]["code"], data["booking"]["customer"]), ([], booking.reference_code, "Juan Tolentino"))

    def test_the_receipts_come_newest_first_with_what_the_window_needs(self):
        booking = make_booking(pickup=5, ret=9)
        first = self.record(booking, P.DEPOSIT, "2000")
        second = self.record(booking, P.BALANCE, "3500")
        data = self.as_user(self.staff).receipts(booking).json()
        self.assertEqual([r["id"] for r in data["receipts"]], [second.pk, first.pk])
        newest = data["receipts"][0]
        self.assertEqual((newest["number"], newest["label"], newest["direction"], newest["signed"], newest["amount"], newest["method"]),
                         (second.receipt_number, "Balance", "In", "+₱3,500.00", "3500.00", "Cash"))
        self.assertEqual(newest["by"], "Olive Owner")
        self.assertFalse(newest["void"])
        base = reverse("arabela_admin:payment_receipt_print", args=[second.pk])
        self.assertEqual((newest["lookUrl"], newest["printUrl"]), (base + "?look=1&embed=1", base + "?embed=1"))

    def test_the_status_pill_says_paid_partly_paid_void_or_returned(self):
        booking = make_booking(pickup=5, ret=9)
        deposit = self.record(booking, P.DEPOSIT, "2000")
        pill = {r["id"]: r["pill"] for r in self.as_user(self.staff).receipts(booking).json()["receipts"]}
        self.assertEqual(pill[deposit.pk], {"kind": "partly", "text": "Partly paid · ₱3,500.00 to pay"})
        balance = self.record(booking, P.BALANCE, "3500")
        pill = {r["id"]: r["pill"] for r in self.receipts(booking).json()["receipts"]}
        self.assertEqual(pill[balance.pk], {"kind": "paid", "text": "Paid"})
        payments.void_payment(payment_id=balance.pk, reason="typed on the wrong booking", user=self.owner)
        pill = {r["id"]: r["pill"] for r in self.receipts(booking).json()["receipts"]}
        self.assertEqual(pill[balance.pk], {"kind": "void", "text": "Void"})

    def test_a_deposit_handed_back_has_its_own_pill_and_a_minus_sign(self):
        booking = make_booking(stage=S.RETURNED, pickup=-6, ret=-3, returned_on=-3)
        self.record(booking, P.FULL, "5500")
        back = payments.return_deposit(reservation_id=booking.pk, amount=D("2000"), method=M.CASH, user=self.owner)
        newest = self.as_user(self.staff).receipts(booking).json()["receipts"][0]
        self.assertEqual((newest["id"], newest["label"], newest["direction"], newest["signed"], newest["pill"]),
                         (back.pk, "Deposit return", "Out", "-₱2,000.00", {"kind": "returned", "text": "Deposit returned"}))

    def test_a_payment_from_before_the_payments_page_has_no_receipt_to_open(self):
        booking = make_booking()
        Payment.objects.create(reservation=booking, direction=Payment.Direction.IN, purpose=P.DEPOSIT, method=M.OLD, amount=D("2000"), is_legacy=True)
        self.assertEqual(self.as_user(self.staff).receipts(booking).json()["receipts"], [])

    def test_the_window_looks_without_counting_a_print_and_the_print_button_counts(self):
        booking = make_booking(pickup=5, ret=9)
        payment = self.record(booking, P.DEPOSIT, "2000")
        item = self.as_user(self.staff).receipts(booking).json()["receipts"][0]
        for _ in range(3):
            self.assertEqual(self.client.get(item["lookUrl"]).status_code, 200)
        self.assertEqual(Payment.objects.get(pk=payment.pk).reprint_count, 0)
        self.assertNotIn("REPRINT", self.client.get(item["printUrl"]).content.decode())          # the first real print is the original
        self.assertIn("*** REPRINT 1 ***", self.client.get(item["printUrl"]).content.decode())


class PrintedPaperTests(Base):
    def paper(self, query=""):
        booking = make_booking(pickup=5, ret=9)
        payment = self.record(booking, P.DEPOSIT, "2000")
        return self.as_user(self.staff).client.get(reverse("arabela_admin:payment_receipt_print", args=[payment.pk]) + query).content.decode()

    def test_the_full_page_has_the_toolbar_and_the_paper_choice(self):
        html = self.paper()
        self.assertIn("Print receipt", html)
        self.assertIn('id="rp-paper"', html)
        for option in ('<option value="80">80 mm roll</option>', '<option value="58">58 mm roll</option>', '<option value="normal">Normal page</option>'):
            self.assertIn(option, html)
        self.assertIn("localStorage.getItem('arabela_receipt_paper')", html)
        self.assertIn("The print window decides the final paper size", html)

    def test_inside_the_window_it_is_just_the_paper(self):
        html = self.paper("?embed=1&look=1")
        self.assertNotIn('class="rp-bar"', html)
        self.assertNotIn('id="rp-paper"', html)
        self.assertIn("PAYMENT RECEIPT", html)
        self.assertIn("localStorage.getItem('arabela_receipt_paper')", html)                      # the paper still follows the remembered choice

    def test_only_this_site_may_show_the_receipt_in_a_frame_and_no_other_page_is_loosened(self):
        # The Receipt window shows the paper inside a frame of the SAME site. Without this the browser draws an empty frame (Django's default is to refuse every frame).
        booking = make_booking(pickup=5, ret=9)
        payment = self.record(booking, P.DEPOSIT, "2000")
        self.as_user(self.staff)
        receipt = self.client.get(reverse("arabela_admin:payment_receipt_print", args=[payment.pk]) + "?look=1&embed=1")
        self.assertEqual(receipt.headers["X-Frame-Options"], "SAMEORIGIN")
        for name in ("dashboard", "payments", "gown_monitoring", "security_deposits"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(f"arabela_admin:{name}")).headers["X-Frame-Options"], "DENY")

    def test_the_paper_widths_are_set_by_the_choice(self):
        css = (ROOT / "templates" / "partials" / "receipt_paper.html").read_text(encoding="utf8")
        self.assertIn("--rc-w: 80mm;", css)
        self.assertIn('html[data-paper="58"] .rc-paper { --rc-w: 58mm;', css)
        self.assertIn('html[data-paper="normal"] .rc-paper { --rc-w: 132mm;', css)
        self.assertIn("width: var(--rc-w)", css)

    def test_the_customers_own_receipt_page_has_its_choice_too_and_starts_on_a_normal_page(self):
        text = (ROOT / "templates" / "receipt_view.html").read_text(encoding="utf8")
        self.assertIn("arabela_receipt_paper_customer", text)
        self.assertIn("'normal'", text)
        for option in ('value="80"', 'value="58"', 'value="normal"'):
            self.assertIn(option, text)


class ReceiptWindowTests(Base):
    def test_every_admin_page_carries_the_window_and_the_date_helper_once(self):
        self.as_user(self.staff)
        for name in ("dashboard", "active_reservations", "reservation_records", "rental_history", "security_deposits", "payments", "gown_monitoring", "calendar"):
            with self.subTest(page=name):
                html = self.client.get(reverse(f"arabela_admin:{name}")).content.decode()
                self.assertEqual(html.count('id="rcm-root"'), 1)
                self.assertEqual(html.count("window.arabelaReceipt = "), 1)
                self.assertEqual(html.count("window.arabelaDF = {"), 1)
                self.assertIn(f'data-list-url="{reverse("arabela_admin:receipt_list", args=[999999])}"', html)

    def test_the_window_is_a_dialog_that_closes_and_keeps_the_paper_choice(self):
        html = self.as_user(self.staff).client.get(reverse("arabela_admin:dashboard")).content.decode()
        for piece in ('role="dialog"', 'aria-modal="true"', "arabela_receipt_paper", "Print Receipt", "Close", 'value="58"', 'value="normal"'):
            with self.subTest(piece=piece):
                self.assertIn(piece, html)

    def test_the_window_moves_itself_out_of_the_header_so_a_phone_can_show_it(self):
        # It is included inside the page header, whose buttons fold into a menu on a phone: a window left in there is invisible there.
        text = (ROOT / "templates" / "arabela_admin" / "partials" / "receipt_preview.html").read_text(encoding="utf8")
        self.assertIn("if (root.parentElement !== document.body) { document.body.appendChild(root); }", text)

    def test_what_the_server_sends_is_written_as_text_not_markup(self):
        text = (ROOT / "templates" / "arabela_admin" / "partials" / "receipt_preview.html").read_text(encoding="utf8")
        self.assertIn("textContent", text)
        self.assertNotIn("innerHTML", text)


class DateBarWiringTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def test_active_reservations_has_the_bar_and_each_booking_carries_its_days(self):
        booking = make_booking(stage=S.RESERVED, pickup=-1, ret=3)
        html = self.client.get(reverse("arabela_admin:active_reservations")).content.decode()
        self.assertIn('class="df-bar"', html)
        self.assertIn("dateFocus: '', dateKind: 'pickup'", html)
        self.assertIn("arabelaDF.has($el, dateFocus, dateKind)", html)
        self.assertIn(f"{self.today + timedelta(days=3):%Y-%m-%d}", html[html.index(f'data-booking-id="{booking.id}"'):][:4000])

    def test_reservation_records_carries_the_days_and_the_receipt_flag_of_every_booking(self):
        paid = make_booking(pickup=5, ret=9)
        self.record(paid, P.DEPOSIT, "2000")
        unpaid = make_booking(pickup=6, ret=10)
        cancelled = make_booking(status=Reservation.Status.CANCELLED)
        response = self.client.get(reverse("arabela_admin:reservation_records"))
        by_id = {r["id"]: r for r in response.context["records"]}
        self.assertTrue(by_id[paid.id]["hasReceipt"])
        self.assertFalse(by_id[unpaid.id]["hasReceipt"])
        self.assertEqual(by_id[paid.id]["datesPickup"], [f"{self.today + timedelta(days=5):%Y-%m-%d}"])
        self.assertEqual(by_id[paid.id]["datesReturn"], [f"{self.today + timedelta(days=9):%Y-%m-%d}"])
        self.assertEqual((by_id[cancelled.id]["datesPickup"], by_id[cancelled.id]["datesReturn"], by_id[cancelled.id]["datesEvent"]), ([], [], []))
        html = response.content.decode()
        self.assertIn('class="df-bar"', html)
        self.assertIn("arabelaDF.hit(this.focusDays(r), this.dateFocus)", html)

    def test_a_gown_the_shop_cancelled_has_no_day_in_records(self):
        booking = make_booking(gowns=2, pickup=5, ret=9)
        last = ReservationItem.objects.filter(reservation=booking).order_by("-id").first()
        ReservationItem.objects.filter(pk=last.pk).update(stage=S.CANCELLED, rental_date=self.today + timedelta(days=40), return_date=self.today + timedelta(days=44))
        response = self.client.get(reverse("arabela_admin:reservation_records"))
        record = next(r for r in response.context["records"] if r["id"] == booking.id)
        self.assertEqual(record["datesPickup"], [f"{self.today + timedelta(days=5):%Y-%m-%d}"])
        self.assertNotIn(f"{self.today + timedelta(days=44):%Y-%m-%d}", record["datesReturn"])

    def test_rental_history_has_the_bar_and_each_gown_carries_its_days(self):
        booking = make_booking(stage=S.RETURNED, pickup=-6, ret=-3, returned_on=-3)
        self.record(booking, P.FULL, "5500")
        response = self.client.get(reverse("arabela_admin:rental_history"))
        row = response.context["rental_records"][0]
        self.assertEqual(row["dReturn"], [f"{self.today - timedelta(days=3):%Y-%m-%d}"])
        self.assertEqual(row["dPickup"], [f"{self.today - timedelta(days=6):%Y-%m-%d}"])
        html = response.content.decode()
        self.assertIn('class="df-bar"', html)
        self.assertIn(f"arabelaReceipt.open({booking.id})", html)                              # a gown with a receipt has its Receipt button

    def record(self, booking, purpose, amount):
        return payments.record_payment(reservation_id=booking.pk, purpose=purpose, amount=D(amount), method=M.CASH, user=self.owner)

    def test_the_helper_treats_an_empty_or_odd_date_as_any_date(self):
        text = (ROOT / "templates" / "arabela_admin" / "partials" / "date_focus_lib.html").read_text(encoding="utf8")
        self.assertIn("if (!parse(value)) { return true; }", text)                             # a half-typed date can never hide the whole list
        self.assertIn("iso(d) !== value", text)                                                # 2026-02-30 is not a date


# ------------------------------------------------------------------------------------------------------------------------------------------------
class LatestTransactionsTests(Base):
    def test_nothing_recorded_gives_an_empty_list(self):
        self.assertEqual(dashboard_money.latest_transactions(), [])

    def test_the_newest_five_come_first_and_from_before_the_payments_page_are_left_out(self):
        booking = make_booking(pickup=5, ret=9)
        Payment.objects.create(reservation=booking, direction=Payment.Direction.IN, purpose=P.DEPOSIT, method=M.OLD, amount=D("2000"), is_legacy=True)
        made = []
        for n in range(7):
            made.append(payments.record_payment(reservation_id=make_booking(pickup=5, ret=9).pk, purpose=P.DEPOSIT, amount=D("2000"), method=M.CASH, user=self.owner,
                                                now=timezone.now() - timedelta(hours=10 - n)))
        rows = dashboard_money.latest_transactions()
        self.assertEqual([r["id"] for r in rows], [p.pk for p in reversed(made)][:5])
        self.assertEqual(len(dashboard_money.latest_transactions(limit=2)), 2)

    def test_what_a_line_says(self):
        booking = make_booking(pickup=5, ret=9, gowns=2)
        payment = self.record(booking, P.DEPOSIT, "2000")
        [row] = dashboard_money.latest_transactions()
        self.assertEqual((row["what"], row["purpose"], row["method"], row["method_kind"]), ("Payment received", "Deposit", "Cash", "cash"))
        self.assertEqual((row["amount"], row["sign"], row["is_out"], row["status"], row["void"], row["test"]), ("2,000.00", "+", False, "Success", False, False))
        self.assertEqual((row["id"], row["booking_id"], row["code"], row["customer"], row["receipt"]), (payment.pk, booking.pk, booking.reference_code, "Juan Tolentino", payment.receipt_number))
        self.assertEqual(row["gown"], "Wedding Gown 1 + 1 more")
        first, last = self.today + timedelta(days=5), self.today + timedelta(days=9)
        self.assertEqual(row["dates"], f"{first:%b} {first.day} - {last:%b} {last.day}, {last.year}")

    def test_each_way_of_paying_has_its_own_name_and_colour(self):
        cases = ((M.CASH, "Cash", "cash"), (M.GCASH, "GCash", "gcash"), (M.ONLINE_GCASH, "GCash", "gcash"), (M.PM_GCASH, "GCash", "gcash"),
                 (M.PM_MAYA, "Maya", "maya"), (M.PM_QRPH, "QR Ph", "qrph"), (M.PM_OTHER, "Online", "other"))
        for method, name, kind in cases:
            with self.subTest(method=method):
                self.assertEqual(dashboard_money._METHOD_BADGE[method], (name, kind))
        self.assertEqual(set(dashboard_money._METHOD_BADGE), {m for m, _ in M.choices})        # a way of paying added later must get its badge here too

    def test_a_deposit_handed_back_and_a_late_fee_have_their_own_words(self):
        out = Payment(direction=Payment.Direction.OUT, purpose=P.DEPOSIT_RETURN)
        late = Payment(direction=Payment.Direction.IN, purpose=P.EXTRA)
        self.assertEqual(dashboard_money._kind(out), ("Deposit returned", ""))
        self.assertEqual(dashboard_money._kind(late), ("Late fee received", ""))
        self.assertEqual(dashboard_money._kind(Payment(direction=Payment.Direction.IN, purpose=P.FULL)), ("Payment received", "Paid in full"))

    def test_a_voided_payment_stays_in_the_list_marked_void(self):
        booking = make_booking(pickup=5, ret=9)
        payment = self.record(booking, P.DEPOSIT, "2000")
        payments.void_payment(payment_id=payment.pk, reason="typed on the wrong booking", user=self.owner)
        [row] = dashboard_money.latest_transactions()
        self.assertEqual((row["status"], row["void"]), ("Void", True))

    def test_a_practice_online_payment_is_marked_test_and_a_real_one_is_not(self):
        practice = make_booking(pickup=5, ret=9, channel=Reservation.Channel.ONLINE)
        payments.record_online_payment(reservation=practice, purpose=P.FULL, amount=D("5500"), channel="paymaya", provider_ref="pay_practice", livemode=False)
        real = make_booking(pickup=5, ret=9, channel=Reservation.Channel.ONLINE)
        payments.record_online_payment(reservation=real, purpose=P.FULL, amount=D("5500"), channel="qrph", provider_ref="pay_real", livemode=True)
        rows = {r["code"]: r for r in dashboard_money.latest_transactions()}
        self.assertEqual((rows[practice.reference_code]["test"], rows[practice.reference_code]["method"], rows[practice.reference_code]["method_kind"]), (True, "Maya", "maya"))
        self.assertEqual((rows[real.reference_code]["test"], rows[real.reference_code]["method"], rows[real.reference_code]["method_kind"]), (False, "QR Ph", "qrph"))

    def test_a_deposit_handed_back_shows_a_minus_and_its_way(self):
        booking = make_booking(stage=S.RETURNED, pickup=-6, ret=-3, returned_on=-3)
        self.record(booking, P.FULL, "5500")
        payments.return_deposit(reservation_id=booking.pk, amount=D("2000"), method=M.CASH, user=self.owner)
        row = dashboard_money.latest_transactions()[0]
        self.assertEqual((row["what"], row["sign"], row["is_out"], row["amount"]), ("Deposit returned", "-", True, "2,000.00"))


class ShopTodayTests(Base):
    def test_a_quiet_day_is_all_zero(self):
        self.assertEqual(dashboard_money.shop_today(self.today), {"new_bookings": 0, "pickups_owing": 0, "late_gowns": 0, "online_attention": 0, "needs_look": 0})

    def test_only_a_real_booking_made_today_is_new(self):
        make_booking()
        make_booking(status=Reservation.Status.PENDING)
        make_booking(status=Reservation.Status.REJECTED)
        old = make_booking()
        Reservation.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=2))
        self.assertEqual(dashboard_money.shop_today(self.today)["new_bookings"], 1)

    def test_a_pickup_in_the_next_two_days_with_a_balance_is_counted_once_a_booking(self):
        owing = make_booking(pickup=1, ret=4, gowns=2)
        paid = make_booking(pickup=2, ret=5)
        pay(paid, P.FULL, "5500")
        make_booking(pickup=3, ret=6)                                                           # not in the next 2 days
        make_booking(pickup=-1, ret=2)                                                          # the day passed: not "next"
        today_pickup = make_booking(pickup=0, ret=3)
        shown = dashboard_money.shop_today(self.today)
        self.assertEqual(shown["pickups_owing"], 2)
        pay(today_pickup, P.FULL, "5500")
        pay(owing, P.FULL, "5500")
        self.assertEqual(dashboard_money.shop_today(self.today)["pickups_owing"], 0)

    def test_a_gown_out_past_its_return_date_is_late(self):
        make_booking(stage=S.RESERVED, pickup=-9, ret=-3)
        make_booking(stage=S.RESERVED, pickup=-9, ret=3)
        make_booking(stage=S.RETURNED, pickup=-9, ret=-3, returned_on=-3)
        make_booking(stage=S.PICKUP, pickup=-9, ret=-3)
        self.assertEqual(dashboard_money.shop_today(self.today)["late_gowns"], 1)

    def test_an_online_payment_that_needs_a_person_is_counted(self):
        PaymentAttempt.objects.create(token="t" * 30, customer=self.customer, customer_name="Ana Cruz", purpose="deposit", amount=D("2000"),
                                      expires_at=timezone.now() + timedelta(minutes=20), status=PaymentAttempt.Status.FLAGGED)
        shown = dashboard_money.shop_today(self.today)
        self.assertEqual((shown["online_attention"], shown["needs_look"]), (1, 1))


class DashboardPageTests(Base):
    def page(self, user=None):
        self.client.force_login(user or self.staff)
        response = self.client.get(reverse("arabela_admin:dashboard"))
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def card(self, html):
        start = html.index('data-live-region="latest-transactions"')
        return html[start:html.index("Latest transactions End", start)]

    def test_an_empty_card_says_so_and_still_has_view_all(self):
        _, html = self.page()
        card = self.card(html)
        self.assertIn("Latest transactions", card)
        self.assertIn("No payment has been recorded yet.", card)
        self.assertIn("View all", card)

    def test_a_line_shows_when_what_who_the_rental_the_amount_and_the_status(self):
        booking = make_booking(pickup=5, ret=9)
        payment = self.record(booking, P.DEPOSIT, "2000", method=M.GCASH, reference="5046933479702")
        _, html = self.page()
        card = self.card(html)
        for piece in ("Payment received", "Deposit", '<span class="lt-pill lt-pill--gcash">GCash</span>', "Juan Tolentino", booking.reference_code, "Wedding Gown 1",
                      "+&#8369;2,000.00", ">Success<", "Open the receipt " + payment.receipt_number):
            with self.subTest(piece=piece):
                self.assertIn(piece, card)
        for title in ("Date", "Type", "Details", "Rental", "Amount", "Status"):
            self.assertIn(f"<span>{title}</span>", card)

    def test_a_click_on_a_line_opens_its_receipt(self):
        booking = make_booking(pickup=5, ret=9)
        payment = self.record(booking, P.DEPOSIT, "2000")
        _, html = self.page()
        card = self.card(html)
        self.assertIn(f"arabelaReceipt.open({booking.pk}, {{ payment: {payment.pk} }})", card)
        self.assertIn('role="button" tabindex="0"', card)                                       # the keyboard opens it too
        self.assertIn("@keydown.enter=", card)

    def test_only_the_latest_five_are_listed(self):
        for _ in range(7):
            self.record(make_booking(pickup=5, ret=9), P.DEPOSIT, "2000")
        _, html = self.page()
        self.assertEqual(self.card(html).count('class="lt-line'), 5)

    def test_a_practice_payment_shows_no_test_tag(self):
        # The panel knows the payments are practice ones, so the list does not label them.
        booking = make_booking(pickup=5, ret=9, channel=Reservation.Channel.ONLINE)
        payments.record_online_payment(reservation=booking, purpose=P.FULL, amount=D("5500"), channel="gcash", provider_ref="pay_p", livemode=False)
        _, html = self.page()
        self.assertNotIn('class="lt-test"', self.card(html))
        self.assertNotIn("no real money moved", self.card(html))

    def test_a_voided_line_is_struck_and_says_void(self):
        booking = make_booking(pickup=5, ret=9)
        payments.void_payment(payment_id=self.record(booking, P.DEPOSIT, "2000").pk, reason="typed on the wrong booking", user=self.owner)
        card = self.card(self.page()[1])
        self.assertIn("lt-line is-void", card)
        self.assertIn('lt-st lt-st--void">Void<', card)

    def test_a_deposit_handed_back_is_a_minus(self):
        booking = make_booking(stage=S.RETURNED, pickup=-6, ret=-3, returned_on=-3)
        self.record(booking, P.FULL, "5500")
        payments.return_deposit(reservation_id=booking.pk, amount=D("2000"), method=M.CASH, user=self.owner)
        card = self.card(self.page()[1])
        self.assertIn("Deposit returned", card)
        self.assertIn("&minus;&#8369;2,000.00", card)
        self.assertIn("lt-amt is-out", card)

    def test_view_all_goes_to_all_receipts_for_the_owner_and_to_payments_for_staff(self):
        self.assertIn(f'href="{reverse("arabela_admin:payment_receipts")}"', self.card(self.page(self.owner)[1]))
        staff_card = self.card(self.page(self.staff)[1])
        self.assertIn(f'href="{reverse("arabela_admin:payments")}"', staff_card)
        self.assertNotIn(reverse("arabela_admin:payment_receipts"), staff_card)

    def test_what_a_customer_typed_cannot_become_markup(self):
        booking = make_booking(pickup=5, ret=9)
        Reservation.objects.filter(pk=booking.pk).update(customer_name='<img src=x onerror=alert(1)>')
        ReservationItem.objects.filter(reservation=booking).update(gown_name="<b>Gown</b>")
        self.record(booking, P.DEPOSIT, "2000")
        card = self.card(self.page()[1])
        self.assertNotIn("<img src=x", card)
        self.assertNotIn("<b>Gown</b>", card)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", card)

    def alert_line(self, html):
        start = html.index('data-live-region="overview-alerts">') + len('data-live-region="overview-alerts">')
        return html[start:html.index("</div></div>", start) + 6] if html[start:start + 6] != "</div>" else ""

    def test_one_part_of_the_money_overview_going_wrong_never_costs_staff_their_dashboard(self):
        with mock.patch("arabela_admin.dashboard_money.latest_transactions", side_effect=RuntimeError("boom")):
            with self.assertLogs("arabela_admin.views", level="ERROR"):
                _, html = self.page()
        self.assertIn("No payment has been recorded yet.", html)
        self.assertIn("Registered Customers", html)
        self.assertIn('data-live-region="overview-top"', html)                              # the money cards are built on their own
        with mock.patch("arabela_admin.dashboard_money.month_money", side_effect=RuntimeError("boom")):
            with self.assertLogs("arabela_admin.views", level="ERROR"):
                _, html = self.page()
        self.assertNotIn("Rental Income", html)
        self.assertIn("Security Deposits Held", html)

    def test_the_warning_line_says_what_needs_a_person_and_opens_the_page_with_the_details(self):
        make_booking(pickup=1, ret=4)                                                         # picks up tomorrow, nothing paid
        make_booking(stage=S.RESERVED, pickup=-9, ret=-3)                                     # out, 3 days late
        response, html = self.page()
        line = self.alert_line(html)
        self.assertEqual(response.context["shop_today"]["new_bookings"], 2)
        for piece in ("1 gown is late coming back", "1 pick-up in the next 2 days still has a balance"):
            with self.subTest(piece=piece):
                self.assertIn(piece, line)
        self.assertIn(f'href="{reverse("arabela_admin:payments")}"', line)
        self.assertIn(f'href="{reverse("arabela_admin:gown_monitoring")}?tab=return"', line)

    def test_the_warning_line_is_not_there_when_nothing_needs_a_person(self):
        _, html = self.page()
        self.assertIn('<div class="db-alerts" data-live-region="overview-alerts"></div>', html)

    def test_an_online_payment_that_needs_a_person_opens_the_online_list(self):
        PaymentAttempt.objects.create(token="u" * 30, customer=self.customer, customer_name="Ana Cruz", purpose="deposit", amount=D("2000"),
                                      expires_at=timezone.now() + timedelta(minutes=20), status=PaymentAttempt.Status.FLAGGED)
        _, html = self.page()
        line = self.alert_line(html)
        self.assertIn("1 online payment needs a look", line)
        self.assertIn(f'href="{reverse("arabela_admin:payment_online")}"', line)

    def test_the_new_parts_are_live_regions(self):
        _, html = self.page()
        for name in ("overview-alerts", "overview-top", "overview-bookings", "latest-transactions"):
            self.assertEqual(html.count(f'data-live-region="{name}"'), 1)


# ------------------------------------------------------------------------------------------------------------------------------------------------
class HidingWhilePayMongoIsOnTests(Base):
    """Nothing is hidden while no PayMongo key is set; with one, the screenshot-era parts leave the menus and the cards (the pages still open by address)."""

    def link(self, name):
        return f'href="{reverse(f"arabela_admin:{name}")}"'

    def sidebar_page(self, user=None):
        self.client.force_login(user or self.owner)
        return self.client.get(reverse("arabela_admin:gown_monitoring")).content.decode()

    def pending(self):
        return Reservation.objects.create(customer=self.customer, customer_name="Waiting", phone="09170000001", status=Reservation.Status.PENDING,
                                          payment_method="GCash", total_amount=D("2500"))

    def test_without_a_paymongo_key_every_old_page_is_still_in_the_menu(self):
        html = self.sidebar_page()
        for name in ("pending_approval", "payment_verification", "payments", "security_deposits", "gown_monitoring", "reservation_records"):
            with self.subTest(link=name):
                self.assertIn(self.link(name), html)

    def test_without_a_paymongo_key_the_dashboard_keeps_quick_verify_and_profile_its_gcash_card(self):
        self.client.force_login(self.owner)
        self.assertIn('class="qv-card"', self.client.get(reverse("arabela_admin:dashboard")).content.decode())
        self.assertIn('id="gcash-card"', self.client.get(reverse("arabela_admin:page", kwargs={"page": "profile"})).content.decode())

    @override_settings(**PAYMONGO_ON)
    def test_with_paymongo_on_payment_verification_and_pending_approval_leave_the_menu_when_nothing_is_pending(self):
        html = self.sidebar_page()
        self.assertNotIn(self.link("payment_verification"), html)
        self.assertNotIn(self.link("pending_approval"), html)
        for name in ("payments", "security_deposits", "gown_monitoring", "reservation_records", "active_reservations", "rental_history"):
            with self.subTest(link=name):
                self.assertIn(self.link(name), html)

    @override_settings(**PAYMONGO_ON)
    def test_an_old_booking_still_waiting_keeps_pending_approval_and_quick_verify(self):
        self.pending()
        html = self.sidebar_page()
        self.assertIn(self.link("pending_approval"), html)
        self.assertNotIn(self.link("payment_verification"), html)
        self.assertIn('class="qv-card"', self.client.get(reverse("arabela_admin:dashboard")).content.decode())

    @override_settings(**PAYMONGO_ON)
    def test_with_paymongo_on_quick_verify_and_the_gcash_cards_are_left_out(self):
        self.client.force_login(self.owner)
        dashboard = self.client.get(reverse("arabela_admin:dashboard")).content.decode()
        self.assertNotIn('class="qv-card"', dashboard)
        self.assertIn('data-live-region="latest-transactions"', dashboard)
        profile = self.client.get(reverse("arabela_admin:page", kwargs={"page": "profile"})).content.decode()
        self.assertNotIn('id="gcash-card"', profile)
        self.assertIn("Security deposit", profile)                                              # the deposit setting stays

    @override_settings(**PAYMONGO_ON)
    def test_a_hidden_page_still_opens_by_its_address(self):
        self.client.force_login(self.owner)
        for name in ("payment_verification", "pending_approval"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(f"arabela_admin:{name}")).status_code, 200)

    @override_settings(**PAYMONGO_ON)
    def test_a_customer_or_a_visitor_never_gets_the_flags_or_the_pages(self):
        self.assertEqual(self.client.get(reverse("arabela_admin:gown_monitoring")).status_code, 302)
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(reverse("arabela_admin:gown_monitoring")).status_code, 302)


class PageStateIsWellFormedTests(Base):
    """The page's whole Alpine state lives in two attributes of the <body> tag. A stray double quote inside a comment in them (the date bar once put the words
    "On this date" in one) ends the attribute early: the page still renders and every test of its text passes, but in the browser nothing on the page works.
    So the tag is read the way a browser reads it: it must have only the attributes a page state has."""

    ALLOWED = re.compile(r"^(x-[a-z:.-]+|:[a-z-]+|@[a-z.:-]+|class|style|id)$")

    def body_attributes(self, html):
        from html.parser import HTMLParser

        class Reader(HTMLParser):
            found = None

            def handle_starttag(self, tag, attrs):
                if tag == "body" and self.found is None:
                    self.found = [name for name, _ in attrs]

        reader = Reader()
        reader.feed(html[html.index("<body"):html.index("</aside>")] if "</aside>" in html else html[html.index("<body"):])
        return reader.found

    def test_every_admin_page_with_a_state_has_only_real_attributes_on_its_body_tag(self):
        self.client.force_login(self.owner)
        for name in ("dashboard", "active_reservations", "reservation_records", "rental_history", "security_deposits", "gown_monitoring", "payments", "calendar",
                     "pending_approval", "payment_verification", "new_booking", "clients", "gown_catalog"):
            with self.subTest(page=name):
                html = self.client.get(reverse(f"arabela_admin:{name}")).content.decode()
                names = self.body_attributes(html)
                self.assertIsNotNone(names)
                self.assertIn("x-data", names)
                self.assertEqual([n for n in names if not self.ALLOWED.match(n)], [])


class FlowFlagsAreInTheSettingsTests(SimpleTestCase):
    def test_the_context_processor_is_registered_and_never_raises_for_a_visitor(self):
        self.assertIn("arabela_admin.context_processors.admin_flow", settings.TEMPLATES[0]["OPTIONS"]["context_processors"])
        from arabela_admin.context_processors import admin_flow
        request = mock.Mock(user=mock.Mock(is_authenticated=False))
        self.assertEqual(admin_flow(request), {})
        self.assertEqual(admin_flow(mock.Mock(spec=[])), {})
