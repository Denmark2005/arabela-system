"""The Payments page and everything behind it: the list, one booking's window, the actions (record a payment, hand the deposit back, a deduction,
voiding, sending a receipt again), who may do what, the printed Payment Receipt, the owner's list of all receipts, and the late fee setting.

The money rules themselves are tested in reservations/test_ledger.py and test_payments_service.py; here: that the web side asks them and answers right."""
import json
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditLog, CustomerMessage, UserProfile
from gowns.models import SiteSettings
from reservations import payments
from reservations.models import Deduction, Payment, Reservation, ReservationItem
from reservations.test_ledger import make_booking

User = get_user_model()
D = Decimal
P = Payment.Purpose
M = Payment.Method
ADMIN = Path(settings.BASE_DIR) / "templates" / "arabela_admin"
REF = "5046933479702"


def staff_user(name, role=UserProfile.Role.STAFF, permissions=""):
    user = User.objects.create_user(name, f"{name}@gmail.com", "x", is_staff=True, first_name=name.title(), last_name="Test")
    UserProfile.objects.update_or_create(user=user, defaults={"role": role, "permissions": permissions})
    return user


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("pg_owner", "pg.owner@gmail.com", "x", first_name="Olive", last_name="Owner")
        UserProfile.objects.update_or_create(user=cls.owner, defaults={"role": UserProfile.Role.OWNER})
        cls.staff = staff_user("pg_staff")
        cls.allowed = staff_user("pg_allowed", permissions="void_receipts")
        cls.manager = staff_user("pg_manager", role=UserProfile.Role.MANAGER)
        cls.customer = User.objects.create_user("pg_customer", "pg.customer@gmail.com", "x", first_name="Juan")

    def as_user(self, user):
        self.client.force_login(user)
        return self

    def post(self, name, body=None, **kwargs):
        return self.client.post(reverse(f"arabela_admin:{name}", kwargs=kwargs), data=json.dumps(body or {}), content_type="application/json")

    def record(self, booking, purpose, amount, **kw):
        return payments.record_payment(reservation_id=booking.pk, purpose=purpose, amount=D(amount), method=kw.pop("method", M.CASH), user=self.owner, **kw)


class ThePageTests(Base):
    def test_staff_see_the_list_with_the_balance_of_every_running_booking(self):
        owing = make_booking()
        paid = make_booking()
        self.record(paid, P.FULL, "5500")
        make_booking(status=Reservation.Status.PENDING)
        make_booking(status=Reservation.Status.REJECTED)
        html = self.as_user(self.staff).client.get(reverse("arabela_admin:payments")).content.decode()
        self.assertIn(owing.reference_code, html)
        self.assertIn(paid.reference_code, html)
        self.assertEqual(html.count('class="ar-booking pm-row"'), 2)               # the pending and the rejected one are not listed
        row = html[html.index(f'data-booking-id="{owing.id}"'):]
        row = row[:row.index("</div>\n                </div>")]
        self.assertIn("5,500.00", row)
        self.assertIn("Balance due", row)
        self.assertIn("Still to collect: &#8369;5,500.00", html)

    def test_the_filters_carry_the_counts(self):
        make_booking()                                                                 # balance due
        done = make_booking(stage=ReservationItem.Stage.RETURNED, ret=-3, returned_on=-3)
        self.record(done, P.FULL, "5500")                                              # refund due (deposit comes back)
        late = make_booking(stage=ReservationItem.Stage.OVERDUE, ret=-2)
        self.record(late, P.FULL, "5500")
        ctx = self.as_user(self.staff).client.get(reverse("arabela_admin:payments")).context
        self.assertEqual(ctx["counts"], {"all": 3, "balance": 1, "refund": 1, "late": 1})

    def test_a_cancelled_booking_that_was_approved_is_listed_with_its_deposit_kept(self):
        booking = make_booking(status=Reservation.Status.CANCELLED)
        self.record_ok = payments.ensure_rows(booking)
        html = self.as_user(self.staff).client.get(reverse("arabela_admin:payments")).content.decode()
        self.assertIn(booking.reference_code, html)
        self.assertIn("Cancelled - deposit kept", html)

    def test_nothing_yet_says_so(self):
        html = self.as_user(self.staff).client.get(reverse("arabela_admin:payments")).content.decode()
        self.assertIn("nothing to collect or hand back", html)

    def test_a_customer_and_a_visitor_are_turned_away(self):
        self.assertEqual(self.client.get(reverse("arabela_admin:payments")).status_code, 302)
        self.as_user(self.customer)
        self.assertEqual(self.client.get(reverse("arabela_admin:payments")).status_code, 302)
        for name, kw in (("payment_detail", {"pk": 1}),):
            self.assertEqual(self.client.get(reverse(f"arabela_admin:{name}", kwargs=kw)).status_code, 401)
        self.assertEqual(self.post("payment_record", {"purpose": "Deposit", "amount": "2000"}, pk=1).status_code, 401)

    def test_the_names_and_gowns_are_escaped(self):
        booking = make_booking()
        Reservation.objects.filter(pk=booking.pk).update(customer_name='<b>Evil</b> "Q"')
        html = self.as_user(self.staff).client.get(reverse("arabela_admin:payments")).content.decode()
        self.assertNotIn("<b>Evil</b>", html)
        self.assertIn("&lt;b&gt;Evil&lt;/b&gt;", html)

    def test_the_page_never_leaks_template_code_and_the_file_keeps_its_line_endings(self):
        make_booking()
        html = self.as_user(self.owner).client.get(reverse("arabela_admin:payments")).content.decode()
        for leak in ("{%", "{{", "{#"):
            self.assertNotIn(leak, html)
        self.assertEqual((ADMIN / "payments.html").read_bytes().count(b"\r\n"), 0)

    def test_the_open_link_carries_the_booking_id_into_the_page_script(self):
        booking = make_booking()
        html = self.as_user(self.staff).client.get(reverse("arabela_admin:payments") + f"?open={booking.pk}").content.decode()
        self.assertIn(f"var OPEN_ID = '{booking.pk}';", html)
        html = self.client.get(reverse("arabela_admin:payments") + "?open=<script>").content.decode()
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("var OPEN_ID = '<script>", html)

    def test_the_list_costs_a_fixed_number_of_queries_however_many_bookings(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.as_user(self.staff)
        self.client.get(reverse("arabela_admin:payments"))              # the once-a-day work does not count
        make_booking()
        with CaptureQueriesContext(connection) as few:
            self.client.get(reverse("arabela_admin:payments"))
        for _ in range(8):
            make_booking(gowns=2)
        with CaptureQueriesContext(connection) as many:
            self.client.get(reverse("arabela_admin:payments"))
        self.assertLessEqual(len(many), len(few) + 9)                   # a few more only for the customer names (no query per payment / gown / deduction)


class TheWindowTests(Base):
    def test_the_detail_is_everything_the_window_shows(self):
        booking = make_booking(stage=ReservationItem.Stage.OVERDUE, ret=-3)
        self.record(booking, P.DEPOSIT, "2000")
        data = self.as_user(self.staff).client.get(reverse("arabela_admin:payment_detail", args=[booking.pk])).json()
        self.assertEqual((data["code"], data["customer"], data["hasAccount"]), (booking.reference_code, "Juan Tolentino", False))
        self.assertEqual((data["peso"]["total"], data["peso"]["balance_due"], data["peso"]["late_fee"]), ("5,500.00", "3,500.00", "600.00"))
        self.assertEqual([(l["kind"], l["label"], l["days"], l["amount"]) for l in data["feeLines"]], [("overdue", "Overdue fee", 3, "600.00")])
        self.assertEqual(data["choices"], {"kind": "balance", "max": "3500.00"})
        self.assertEqual(len(data["payments"]), 1)
        self.assertTrue(data["payments"][0]["printUrl"].endswith(f"/payments/receipt/{data['payments'][0]['id']}/"))
        self.assertEqual((data["canVoid"], data["isOwner"], data["canDeduct"]), (False, False, True))

    def test_an_old_booking_shows_the_rows_its_old_data_stands_for(self):
        booking = make_booking(channel=Reservation.Channel.ONLINE)
        data = self.as_user(self.staff).client.get(reverse("arabela_admin:payment_detail", args=[booking.pk])).json()
        self.assertEqual([(p["purposeLabel"], p["legacy"]) for p in data["payments"]], [("Deposit", True)])
        self.assertTrue(data["s"]["legacy"])

    def test_a_booking_still_waiting_for_approval_has_no_window(self):
        booking = make_booking(status=Reservation.Status.PENDING)
        response = self.as_user(self.staff).client.get(reverse("arabela_admin:payment_detail", args=[booking.pk]))
        self.assertEqual(response.status_code, 404)

    def test_who_can_void_is_in_the_answer(self):
        booking = make_booking()
        for user, expect in ((self.owner, True), (self.manager, True), (self.allowed, True), (self.staff, False)):
            with self.subTest(user=user.username):
                self.assertEqual(self.as_user(user).client.get(reverse("arabela_admin:payment_detail", args=[booking.pk])).json()["canVoid"], expect)


class RecordingTests(Base):
    def test_recording_a_payment_answers_with_the_receipt_and_the_new_picture_and_writes_the_audit_line(self):
        booking = make_booking()
        response = self.as_user(self.staff).post("payment_record", {"purpose": "Deposit", "amount": "2,000.00", "method": "Cash", "request_id": "r1"}, pk=booking.pk)
        data = response.json()
        self.assertEqual(response.status_code, 200, data)
        self.assertTrue(data["payment"]["receiptNumber"].startswith("PR-"))
        self.assertEqual(data["detail"]["peso"]["balance_due"], "3,500.00")
        self.assertEqual(data["detail"]["choices"]["kind"], "balance")
        line = AuditLog.objects.get(action="payment.record")
        self.assertIn("recorded a payment of ₱2,000.00", line.summary)
        self.assertIn(booking.reference_code, line.summary)

    def test_a_double_click_is_one_payment(self):
        booking = make_booking()
        self.as_user(self.staff)
        for _ in range(2):
            self.assertEqual(self.post("payment_record", {"purpose": "Deposit", "amount": "2000", "method": "Cash", "request_id": "dbl"}, pk=booking.pk).status_code, 200)
        self.assertEqual(Payment.objects.count(), 1)

    def test_a_half_deposit_is_refused_with_the_exact_amount_and_nothing_is_saved_or_audited(self):
        booking = make_booking()
        response = self.as_user(self.staff).post("payment_record", {"purpose": "Deposit", "amount": "1000", "method": "Cash"}, pk=booking.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("received exactly: ₱2,000.00", response.json()["error"])
        self.assertEqual((Payment.objects.count(), AuditLog.objects.filter(action="payment.record").count()), (0, 0))

    def test_bad_input_is_answered_not_crashed(self):
        booking = make_booking()
        self.as_user(self.staff)
        for body in ({}, {"purpose": "Deposit"}, {"purpose": "Deposit", "amount": "abc", "method": "Cash"}, {"purpose": "Deposit", "amount": "-2000", "method": "Cash"},
                     {"purpose": "x" * 500, "amount": "2000", "method": "Cash"}, {"purpose": "Deposit", "amount": "2000", "method": "GCash", "reference": "1"}):
            with self.subTest(body=body):
                self.assertIn(self.post("payment_record", body, pk=booking.pk).status_code, (400, 409))
        self.assertEqual(self.client.post(reverse("arabela_admin:payment_record", args=[booking.pk]), data="not json", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.get(reverse("arabela_admin:payment_record", args=[booking.pk])).status_code, 405)
        self.assertEqual(self.post("payment_record", {"purpose": "Deposit", "amount": "2000", "method": "Cash"}, pk=999999).status_code, 404)
        self.assertEqual(Payment.objects.count(), 0)

    def test_a_typed_gcash_payment_is_refused_and_an_old_one_still_shows_its_reference_grouped(self):
        booking = make_booking()
        refused = self.as_user(self.staff).post("payment_record", {"purpose": "Paid in full", "amount": "5500", "method": "GCash", "reference": "5046-933-479702"}, pk=booking.pk)
        self.assertEqual(refused.status_code, 400)
        self.assertIn("Cash or Pay online", refused.json()["error"])
        self.assertEqual(Payment.objects.count(), 0)
        # A GCash payment recorded before typed GCash entry was removed.
        self.record(booking, P.FULL, "5500", method=M.GCASH, reference="5046-933-479702")
        data = self.client.get(reverse("arabela_admin:payment_detail", args=[booking.pk])).json()
        self.assertEqual((data["payments"][0]["method"], data["payments"][0]["reference"]), ("GCash", "5046 933 479702"))
        self.assertEqual(data["s"]["state"], "paid")

    def test_handing_the_deposit_back(self):
        booking = make_booking(stage=ReservationItem.Stage.RETURNED, ret=-6, returned_on=-3)
        self.record(booking, P.FULL, "5500")
        self.as_user(self.staff)
        too_much = self.post("deposit_return", {"amount": "1500", "method": "Cash"}, pk=booking.pk)
        self.assertEqual(too_much.status_code, 400)
        ok = self.post("deposit_return", {"amount": "1,400", "method": "Cash", "request_id": "ret1"}, pk=booking.pk)
        self.assertEqual(ok.status_code, 200, ok.json())
        self.assertEqual(ok.json()["detail"]["s"]["state"], "settled")
        line = AuditLog.objects.get(action="payment.return_deposit")
        self.assertIn("handed back ₱1,400.00", line.summary)

    def test_the_sale_never_happens_on_a_get(self):
        booking = make_booking()
        self.as_user(self.staff)
        for name in ("payment_record", "deposit_return", "deduction_add"):
            self.assertEqual(self.client.get(reverse(f"arabela_admin:{name}", args=[booking.pk])).status_code, 405)


class WhoMayDoWhatTests(Base):
    def booking_back(self):
        booking = make_booking(stage=ReservationItem.Stage.RETURNED, ret=-3, returned_on=-3)
        self.record(booking, P.FULL, "5500")
        return booking

    def test_a_deduction_and_a_void_need_the_owner_or_the_permission(self):
        booking = self.booking_back()
        payment = Payment.objects.get()
        self.as_user(self.staff)
        refused_a = self.post("deduction_add", {"amount": "500", "reason": "Stain"}, pk=booking.pk)
        refused_b = self.post("payment_void", {"reason": "mistake"}, payment_id=payment.pk)
        self.assertEqual((refused_a.status_code, refused_b.status_code), (403, 403))
        self.assertIn("don't have permission", refused_a.json()["error"])
        self.assertEqual((Deduction.objects.count(), Payment.objects.get().status), (0, Payment.Status.RECORDED))
        self.assertEqual(AuditLog.objects.filter(action__in=("payment.deduction", "payment.void"), outcome="refused").count(), 2)

    def test_the_owner_and_an_allowed_staff_and_a_manager_can(self):
        for user in (self.owner, self.allowed, self.manager):
            with self.subTest(user=user.username):
                booking = self.booking_back()
                self.as_user(user)
                self.assertEqual(self.post("deduction_add", {"amount": "500", "reason": "Stain on the skirt"}, pk=booking.pk).status_code, 200)
                deduction = Deduction.objects.filter(reservation=booking).get()
                self.assertEqual(self.post("deduction_void", {"reason": "it washed out"}, deduction_id=deduction.pk).status_code, 200)
                payment = Payment.objects.filter(reservation=booking).get()
                self.assertEqual(self.post("payment_void", {"reason": "typed on the wrong booking"}, payment_id=payment.pk).status_code, 200)
                payment.refresh_from_db()
                self.assertEqual((payment.status, payment.voided_by), (Payment.Status.VOID, user))

    def test_a_void_and_a_deduction_are_written_as_sensitive_audit_lines_with_the_reason(self):
        booking = self.booking_back()
        self.as_user(self.owner)
        self.post("deduction_add", {"amount": "500", "reason": "Stain on the skirt"}, pk=booking.pk)
        self.post("payment_void", {"reason": "wrong booking"}, payment_id=Payment.objects.get().pk)
        for action in ("payment.deduction", "payment.void"):
            line = AuditLog.objects.get(action=action)
            self.assertTrue(line.sensitive, action)
        self.assertIn("voided receipt", AuditLog.objects.get(action="payment.void").summary)

    def test_everyone_on_the_staff_can_record_print_and_hand_back(self):
        booking = self.booking_back()
        self.as_user(self.staff)
        self.assertEqual(self.post("deposit_return", {"amount": "2000", "method": "Cash"}, pk=booking.pk).status_code, 200)


class SendAgainTests(Base):
    def test_an_account_gets_it_again_and_a_walk_in_is_told_there_is_nobody_to_send_to(self):
        with_account = make_booking(customer=self.customer)
        walk_in = make_booking()
        a, b = self.record(with_account, P.DEPOSIT, "2000"), self.record(walk_in, P.DEPOSIT, "2000")
        self.as_user(self.staff)
        self.assertEqual(self.post("payment_send", payment_id=a.pk).status_code, 200)
        self.assertEqual(CustomerMessage.objects.filter(recipient=self.customer).count(), 2)
        refused = self.post("payment_send", payment_id=b.pk)
        self.assertEqual(refused.status_code, 409)
        self.assertIn("no website account", refused.json()["error"])

    def test_an_old_payment_has_no_receipt_to_send(self):
        booking = make_booking(customer=self.customer, channel=Reservation.Channel.ONLINE)
        payments.ensure_rows(booking)
        self.assertEqual(self.as_user(self.staff).post("payment_send", payment_id=Payment.objects.get().pk).status_code, 400)


class PrintedReceiptTests(Base):
    def test_the_paper_says_what_it_should(self):
        booking = make_booking(pickup=5, ret=9)
        payment = self.record(booking, P.DEPOSIT, "2000", method=M.GCASH, reference=REF)
        response = self.as_user(self.staff).client.get(reverse("arabela_admin:payment_receipt_print", args=[payment.pk]))
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        for piece in ("PAYMENT RECEIPT", payment.receipt_number, booking.reference_code, "Juan Tolentino", "Wedding Gown 1 (Large)", "Gown rental", "3,500.00",
                      "Security deposit", "2,000.00", "TOTAL", "5,500.00", "PAID NOW: Deposit", "GCash", "5046 933 479702", "BALANCE TO PAY AT SHOP", "Thank you!",
                      "Arabela Gown Rental", "Pick-up", "Print receipt"):
            with self.subTest(piece=piece):
                self.assertIn(piece, html)
        self.assertNotIn("REPRINT", html)
        self.assertNotIn("Official Receipt", html)
        self.assertNotIn("OFFICIAL RECEIPT", html)

    def test_the_first_open_is_the_original_and_every_later_one_a_reprint(self):
        payment = self.record(make_booking(), P.DEPOSIT, "2000")
        url = reverse("arabela_admin:payment_receipt_print", args=[payment.pk])
        self.as_user(self.staff)
        self.assertNotIn("REPRINT", self.client.get(url).content.decode())
        self.assertIn("*** REPRINT 1 ***", self.client.get(url).content.decode())
        self.assertIn("*** REPRINT 2 ***", self.client.get(url).content.decode())
        self.assertNotIn("REPRINT 3", self.client.get(url + "?look=1").content.decode())        # a look does not count as a print
        self.assertIn("*** REPRINT 3 ***", self.client.get(url).content.decode())

    def test_a_voided_receipt_prints_void_with_who_and_why(self):
        booking = make_booking()
        payment = self.record(booking, P.DEPOSIT, "2000")
        payments.void_payment(payment_id=payment.pk, reason="typed on the wrong booking", user=self.owner)
        html = self.as_user(self.staff).client.get(reverse("arabela_admin:payment_receipt_print", args=[payment.pk])).content.decode()
        for piece in ("VOID", "*** VOIDED ***", "typed on the wrong booking", "Olive Owner"):
            self.assertIn(piece, html)

    def test_a_deposit_return_prints_the_working(self):
        booking = make_booking(stage=ReservationItem.Stage.RETURNED, ret=-6, returned_on=-3)
        self.record(booking, P.FULL, "5500")
        payment = payments.return_deposit(reservation_id=booking.pk, amount=D("1400"), method=M.CASH, user=self.owner)
        html = self.as_user(self.staff).client.get(reverse("arabela_admin:payment_receipt_print", args=[payment.pk])).content.decode()
        for piece in ("DEPOSIT RETURN", "Security deposit paid", "2,000.00", "Overdue fee", "600.00", "3 days x &#8369;200", "DEPOSIT RETURNED", "1,400.00", "Returned by"):
            with self.subTest(piece=piece):
                self.assertIn(piece, html)

    def test_the_receipt_is_the_frozen_snapshot_not_todays_data(self):
        booking = make_booking()
        payment = self.record(booking, P.DEPOSIT, "2000")
        Reservation.objects.filter(pk=booking.pk).update(customer_name="Someone Else")
        html = self.as_user(self.staff).client.get(reverse("arabela_admin:payment_receipt_print", args=[payment.pk])).content.decode()
        self.assertIn("Juan Tolentino", html)
        self.assertNotIn("Someone Else", html)

    def test_names_on_the_paper_are_escaped(self):
        booking = make_booking()
        Reservation.objects.filter(pk=booking.pk).update(customer_name="<script>alert(1)</script>")
        payment = self.record(booking, P.DEPOSIT, "2000")
        html = self.as_user(self.staff).client.get(reverse("arabela_admin:payment_receipt_print", args=[payment.pk])).content.decode()
        self.assertNotIn("<script>alert(1)</script>", html)

    def test_an_old_payment_and_an_unknown_number_have_no_paper(self):
        booking = make_booking(channel=Reservation.Channel.ONLINE)
        payments.ensure_rows(booking)
        self.as_user(self.staff)
        self.assertEqual(self.client.get(reverse("arabela_admin:payment_receipt_print", args=[Payment.objects.get().pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("arabela_admin:payment_receipt_print", args=[999999])).status_code, 404)

    def test_a_customer_and_a_visitor_cannot_open_the_staff_print_page(self):
        payment = self.record(make_booking(customer=self.customer), P.DEPOSIT, "2000")
        url = reverse("arabela_admin:payment_receipt_print", args=[payment.pk])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(self.as_user(self.customer).client.get(url).status_code, 302)


class AllReceiptsTests(Base):
    def make_some(self):
        a = self.record(make_booking(), P.DEPOSIT, "2000")
        b = self.record(make_booking(), P.FULL, "5500", method=M.GCASH, reference=REF)
        payments.void_payment(payment_id=a.pk, reason="wrong booking", user=self.owner)
        return a, b

    def test_only_the_owner_sees_the_list(self):
        self.make_some()
        self.assertEqual(self.as_user(self.staff).client.get(reverse("arabela_admin:payment_receipts")).status_code, 302)
        self.assertEqual(self.as_user(self.owner).client.get(reverse("arabela_admin:payment_receipts")).status_code, 200)

    def test_the_list_search_and_filters(self):
        a, b = self.make_some()
        self.as_user(self.owner)
        url = reverse("arabela_admin:payment_receipts")
        html = self.client.get(url).content.decode()
        self.assertIn(a.receipt_number, html)
        self.assertIn(b.receipt_number, html)
        self.assertIn("VOID", html)
        only_void = self.client.get(url, {"status": "void"}).content.decode()
        self.assertIn(a.receipt_number, only_void)
        self.assertNotIn(b.receipt_number, only_void)
        self.assertIn(b.receipt_number, self.client.get(url, {"q": "5046 933 479702"}).content.decode())
        self.assertNotIn(a.receipt_number, self.client.get(url, {"q": b.receipt_number}).content.decode())
        self.assertIn(a.receipt_number, self.client.get(url, {"q": a.reservation.reference_code}).content.decode())
        today = timezone.localdate().isoformat()
        self.assertIn(a.receipt_number, self.client.get(url, {"from": today, "to": today}).content.decode())
        self.assertNotIn(a.receipt_number, self.client.get(url, {"from": (timezone.localdate() + timedelta(days=2)).isoformat()}).content.decode())
        self.assertEqual(self.client.get(url, {"from": "garbage", "to": "20-13-99", "page": "x"}).status_code, 200)

    def test_old_payments_without_a_receipt_are_not_in_the_list(self):
        booking = make_booking(channel=Reservation.Channel.ONLINE)
        payments.ensure_rows(booking)
        html = self.as_user(self.owner).client.get(reverse("arabela_admin:payment_receipts")).content.decode()
        self.assertIn("No receipt matches.", html)

    def test_the_list_pages_by_25(self):
        booking = make_booking()
        self.record(booking, P.DEPOSIT, "2000")
        for _ in range(26):
            self.record(booking, P.BALANCE, "10")
        html = self.as_user(self.owner).client.get(reverse("arabela_admin:payment_receipts")).content.decode()
        self.assertIn("Page 1 of 2", html)
        self.assertEqual(self.client.get(reverse("arabela_admin:payment_receipts"), {"page": 2}).status_code, 200)


class LateFeeSettingTests(Base):
    def test_the_owner_changes_it_and_it_is_audited(self):
        response = self.as_user(self.owner).post("late_fee_setting_update", {"amount": "250"})
        self.assertEqual((response.status_code, SiteSettings.load().late_fee_per_day), (200, 250))
        self.assertIn("from ₱200 to ₱250", AuditLog.objects.get(action="settings.late_fee").summary)

    def test_zero_means_no_late_fee_and_bad_numbers_are_refused(self):
        self.as_user(self.owner)
        self.assertEqual(self.post("late_fee_setting_update", {"amount": "0"}).status_code, 200)
        for bad in ("", "abc", "-1", "1.5", "10001", None):
            with self.subTest(bad=bad):
                self.assertEqual(self.post("late_fee_setting_update", {"amount": bad}).status_code, 400)
        self.assertEqual(SiteSettings.load().late_fee_per_day, 0)

    def test_staff_cannot(self):
        self.assertEqual(self.as_user(self.staff).post("late_fee_setting_update", {"amount": "999"}).status_code, 403)
        self.assertEqual(SiteSettings.load().late_fee_per_day, 200)

    def test_a_booking_keeps_the_rate_it_was_made_with(self):
        booking = make_booking(rate=200)
        self.as_user(self.owner).post("late_fee_setting_update", {"amount": "500"})
        self.assertEqual(Reservation.objects.get(pk=booking.pk).late_fee_per_day, 200)
        from gowns import late_fee
        self.assertEqual(late_fee.per_day(), 500)


class SidebarTests(TestCase):
    def test_every_admin_page_with_the_group_lists_payments_first_and_opens_the_group_on_it(self):
        found = 0
        for path in sorted(ADMIN.glob("*.html")):
            text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
            if "Menu Item Payments & Deposits" not in text:
                continue
            found += 1
            with self.subTest(page=path.name):
                # The Payments page links to itself (its own tabs); Active Reservations and Reservation Records also link to it from the Balance chips; the Dashboard
                # (Pick-ups with a balance) and Security Deposits (Open in Payments) send people into it too.
                if path.name not in ("payments.html", "active-reservations.html", "reservation-records.html", "dashboard.html", "security-deposits.html"):
                    self.assertEqual(text.count("{% url 'arabela_admin:payments' %}"), 1)
                self.assertLess(text.index("{% url 'arabela_admin:payments' %}"), text.index("{% url 'arabela_admin:payment_verification' %}"))
                self.assertIn("page === 'payments' || page === 'payment-verification' || page === 'security-deposits'", text)
        self.assertEqual(found, 18)                                       # the Gown Monitoring and ID Checks pages have the sidebar too
