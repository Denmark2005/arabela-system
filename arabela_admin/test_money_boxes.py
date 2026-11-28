"""The money boxes on Payments and Security Deposits, the All receipts totals and the "See rental income" permission (asked for 2026-11-28).

Money people count by eye, so every figure is checked to the centavo on one small shop with known amounts: each booking is ₱3,500 of gown
rental plus a ₱2,000 security deposit, and the overdue fee is ₱200 a day."""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from accounts.roles import PERMISSION_KEYS, PERMISSIONS, PRESETS, join_permissions, parse_permissions
from arabela_admin import dashboard_money
from arabela_admin.money_views import period_words, read_dates
from reservations.models import Payment, Reservation, ReservationItem
from reservations.test_ledger import make_booking, pay

User = get_user_model()
D = Decimal
P, IN, OUT = Payment.Purpose, Payment.Direction.IN, Payment.Direction.OUT
Stage = ReservationItem.Stage
CANCELLED = Reservation.Status.CANCELLED
PAYMENTS = reverse("arabela_admin:payments")
DEPOSITS = reverse("arabela_admin:security_deposits")
RECEIPTS = reverse("arabela_admin:payment_receipts")
DASHBOARD = reverse("arabela_admin:dashboard")
HISTORY = reverse("arabela_admin:rental_history")
BOXES = reverse("arabela_admin:money_boxes")


def money_of_month(today):
    first = today.replace(day=1)
    return dashboard_money.money_between(first, (first + timedelta(days=32)).replace(day=1) - timedelta(days=1), today)


def give_back(booking, amount, *, rental="0", snapshot=None, method=Payment.Method.CASH):
    row = pay(booking, P.DEPOSIT_RETURN, amount, direction=OUT, method=method)
    Payment.objects.filter(pk=row.pk).update(rental_part=D(rental), snapshot=snapshot or {})
    return row


class Shop(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("mb_owner", "mb.owner@gmail.com", "x", first_name="Olive", last_name="Owner")
        UserProfile.objects.update_or_create(user=cls.owner, defaults={"role": UserProfile.Role.OWNER})

        def booking(**kw):
            return make_booking(**{"pickup": 2, "ret": 6, **kw})

        cls.running = booking()                                                         # paid in full, not picked up
        pay(cls.running, P.FULL, "5500")
        cls.partly = booking()                                                          # only the deposit, by GCash; ₱3,500 to collect
        pay(cls.partly, P.DEPOSIT, "2000", method=Payment.Method.GCASH)
        pay(cls.partly, P.BALANCE, "999", status=Payment.Status.VOID)                   # voided: counts nowhere
        cls.late = booking(stage=Stage.RETURNED, pickup=-10, ret=-6, returned_on=-3)    # 3 days overdue, deposit not handed back yet
        pay(cls.late, P.FULL, "5500")
        cls.done = booking(stage=Stage.RETURNED, pickup=-10, ret=-6, returned_on=-6)    # back on time, deposit handed back
        pay(cls.done, P.FULL, "5500")
        give_back(cls.done, "2000")
        cls.cancelled = booking(status=CANCELLED, refunds_rental=True)                  # rental not given back yet
        pay(cls.cancelled, P.FULL, "5500")
        cls.refunded = booking(status=CANCELLED, refunds_rental=True)                   # rental already given back
        pay(cls.refunded, P.FULL, "5500")
        give_back(cls.refunded, "3500", rental="3500", snapshot={"return": {"cancelled": True}})
        cls.shop_fault = booking(status=CANCELLED, refunds_rental=True, deposit_refund_reason="Our mistake")      # everything goes back
        pay(cls.shop_fault, P.FULL, "5500")

    def setUp(self):
        self.client.force_login(self.owner)

    def box(self, **params):
        response = self.client.get(BOXES, params)
        self.assertEqual(response.status_code, 200)
        return response.context["money"]


class TheSumsTests(Shop):
    def test_every_box_to_the_centavo(self):
        box = self.box()
        self.assertEqual(box["income"], "17,500.00")             # running, late, done, cancelled, shop_fault: 5 x 3,500 (refunded: 3,500 - 3,500)
        self.assertEqual(box["income_bookings"], 5)
        self.assertEqual(box["kept"], "600.00")                  # the overdue fee, out of the deposit
        self.assertEqual(box["kept_cancelled"], "4,000.00")      # cancelled + refunded keep their deposits; shop_fault does not
        self.assertEqual((box["collected"], box["returned_deposit"], box["has_returned_extra"]), ("14,000.00", "2,000.00", False))
        self.assertEqual((box["held"], box["held_bookings"]), ("7,400.00", 4))     # running 2,000 + partly 2,000 + late 1,400 + shop_fault 2,000
        self.assertEqual((box["give_back"], box["give_back_bookings"]), ("10,400.00", 3))      # late 1,400 + cancelled 3,500 + shop_fault 5,500
        self.assertEqual((box["owing"], box["owing_bookings"]), ("3,500.00", 1))
        self.assertEqual(box["methods"], [("Cash", "27,500.00"), ("GCash", "2,000.00"), ("Maya", "0.00"), ("QR Ph", "0.00")])

    def test_every_deposit_peso_is_somewhere(self):
        money = dashboard_money.money_between()
        held = D(self.box()["held"].replace(",", ""))
        self.assertEqual(money["collected"], held + money["returned_deposit"] + D("600") + money["kept_cancelled"])

    def test_the_drawer_adds_up_to_every_payment(self):
        money = dashboard_money.money_between()
        stored = Payment.objects.exclude(status=Payment.Status.VOID)
        net = sum(p.amount if p.direction == IN else -p.amount for p in stored)
        self.assertEqual(sum(money["by_method"].values()), net)
        self.assertEqual(net, D("29500.00"))

    def test_the_two_pages_show_the_same_boxes(self):
        keys = ("income", "kept", "kept_cancelled", "held", "give_back", "owing", "collected", "returned_deposit")
        first = self.client.get(PAYMENTS).context["money"]
        second = self.client.get(DEPOSITS).context["money"]
        self.assertEqual([first[k] for k in keys], [second[k] for k in keys])
        self.assertEqual([first[k] for k in keys], [self.box()[k] for k in keys])

    def test_the_other_pages_agree_with_the_boxes(self):
        month = self.client.get(DASHBOARD).context
        self.assertEqual(month["deposits_held"]["held"], "7,400.00")
        today = timezone.localdate()
        money = dashboard_money.money_between(today.replace(day=1), None, today)
        self.assertEqual(month["month_money"]["kept_cancelled"], f"{money['kept_cancelled']:,.2f}")
        report = self.client.get(HISTORY).context["report"]
        period = report["period"]
        money = dashboard_money.money_between(period.start, period.end, today)
        self.assertEqual(report["text"]["held"], "7,400.00")
        for key in ("income", "kept", "kept_cancelled", "returned_deposit", "returned_extra"):
            self.assertEqual(report["text"][key], f"{money[key]:,.2f}", key)
        self.assertEqual(month["month_money"]["income"], f"{money_of_month(today)['income']:,.2f}")
        for kind in ("pdf", "excel"):
            download = self.client.get(reverse("arabela_admin:rental_report_download", args=[kind]))
            self.assertEqual(download.status_code, 200, kind)

    def test_extra_money_going_back_is_not_called_deposit(self):
        booking = make_booking(stage=Stage.RETURNED, pickup=-10, ret=-6, returned_on=-6)
        pay(booking, P.FULL, "5500")
        pay(booking, P.EXTRA, "300")                             # an extra charge that is no longer owed
        before = dashboard_money.money_between()
        give_back(booking, "2300")
        after = dashboard_money.money_between()
        self.assertEqual(after["returned_deposit"] - before["returned_deposit"], D("2000.00"))
        self.assertEqual(after["returned_extra"] - before["returned_extra"], D("300.00"))
        self.assertEqual(after["kept"], D("600.00"))             # the 300 was never the shop's
        self.assertTrue(self.box()["has_returned_extra"])

    def test_handing_money_back_moves_it_at_once(self):
        give_back(self.late, "1400")
        box = self.box()
        self.assertEqual((box["held"], box["give_back"], box["returned_deposit"]), ("6,000.00", "9,000.00", "3,400.00"))
        self.assertEqual((box["income"], box["kept"]), ("17,500.00", "600.00"))

    def test_the_owner_giving_a_deposit_back_is_listed_on_security_deposits(self):
        html = self.client.get(DEPOSITS).content.decode()
        self.assertIn(f'data-booking-id="{self.shop_fault.pk}"', html)
        self.assertIn("booking cancelled, the deposit goes back", html)
        self.assertNotIn(f'data-booking-id="{self.cancelled.pk}"', html)
        give_back(self.shop_fault, "5500", rental="3500", snapshot={"return": {"cancelled": True}})
        box = self.box()
        self.assertEqual((box["held"], box["income"], box["returned_deposit"], box["kept_cancelled"]),
                         ("5,400.00", "14,000.00", "4,000.00", "4,000.00"))


class TheDatesTests(Shop):
    def yesterday(self, booking):
        day = timezone.localdate() - timedelta(days=1)
        when = timezone.make_aware(datetime.combine(day, time(12, 0)))
        Payment.objects.filter(reservation=booking).update(paid_at=when)
        return day

    def test_money_is_counted_on_the_day_it_was_paid(self):
        day = self.yesterday(self.running)
        today = timezone.localdate()
        one = self.box(**{"from": day.isoformat(), "to": day.isoformat(), "by": "paid"})
        self.assertEqual((one["income"], one["collected"], one["income_bookings"]), ("3,500.00", "2,000.00", 1))
        rest = self.box(**{"from": today.isoformat(), "to": today.isoformat(), "by": "paid"})
        self.assertEqual((rest["income"], rest["collected"]), ("14,000.00", "12,000.00"))
        both = self.box(**{"from": day.isoformat(), "to": today.isoformat()})
        self.assertEqual((both["income"], both["collected"]), ("17,500.00", "14,000.00"))
        # what is held and to give back is "now": no date changes it
        self.assertEqual((one["held"], one["give_back"]), (both["held"], both["give_back"]))

    def test_a_day_with_no_money(self):
        box = self.box(**{"from": "2020-01-01", "to": "2020-01-31"})
        self.assertEqual((box["income"], box["collected"], box["returned_deposit"], box["kept"], box["kept_cancelled"], box["owing"]),
                         ("0.00",) * 6)
        self.assertEqual(box["tag"], "January 2020")

    def test_wrong_order_and_junk(self):
        self.assertIn("can&#x27;t be before", self.client.get(BOXES, {"from": "2026-02-02", "to": "2026-02-01"}).content.decode())
        self.assertEqual(read_dates({"from": "junk", "to": "9999-01-01", "by": "<x>"}), (None, None, "return", ""))
        today = timezone.localdate()
        self.assertEqual(period_words(today, today, today), "Today")
        self.assertEqual(period_words(None, None, today), "All dates")
        for url in (PAYMENTS, DEPOSITS):
            self.assertEqual(self.client.get(url, {"from": "'\"><script>", "to": "x", "by": "z"}).status_code, 200)

    def test_paid_days_are_on_the_rows(self):
        day = self.yesterday(self.running)
        for url in (PAYMENTS,):
            html = self.client.get(url).content.decode()
            row = html[html.index(f'data-booking-id="{self.running.pk}"'):]
            self.assertIn(f'data-d-paid="{day.isoformat()}"', row[:2500])


class ThePermissionTests(Shop):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        def staff(name, permissions):
            user = User.objects.create_user(name, password="x", is_staff=True, first_name="Sam")
            UserProfile.objects.update_or_create(user=user, defaults={"role": UserProfile.Role.STAFF,
                                                                      "permissions": join_permissions(permissions)})
            return user

        cls.without = staff("mb_without", [k for k in PERMISSION_KEYS if k != "see_income"])
        cls.allowed = staff("mb_allowed", ["see_income"])

    def pages(self):
        out = {}
        for name, url in (("payments", PAYMENTS), ("deposits", DEPOSITS), ("dashboard", DASHBOARD), ("history", HISTORY), ("boxes", BOXES)):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, name)
            out[name] = response
        return out

    def test_staff_without_it_are_never_sent_the_earnings(self):
        self.client.force_login(self.without)
        pages = self.pages()
        for name, response in pages.items():
            html = response.content.decode()
            for words in ("Rental Income", "Kept from cancellations", "Fees &amp; deductions kept", "17,500.00"):
                self.assertNotIn(words, html, f"{name}: {words}")
        for name in ("payments", "deposits", "boxes"):
            box = pages[name].context["money"]
            self.assertFalse({"income", "kept", "kept_cancelled", "methods"} & set(box), name)
            self.assertEqual(box["today_money"]["net"], "29,500.00")
            self.assertEqual((box["held"], box["give_back"], box["owing"]), ("7,400.00", "10,400.00", "3,500.00"))
        self.assertFalse(pages["dashboard"].context["month_money"])
        for name in ("dashboard", "history"):
            self.assertIn("Today's Money", pages[name].content.decode(), name)
        for kind in ("pdf", "excel"):                                         # the report downloads stay the owner's
            self.assertNotEqual(self.client.get(reverse("arabela_admin:rental_report_download", args=[kind])).status_code, 200, kind)

    def test_staff_with_it_and_the_owner_see_them(self):
        for user in (self.allowed, self.owner):
            self.client.force_login(user)
            pages = self.pages()
            for name in ("payments", "deposits", "dashboard", "history", "boxes"):
                self.assertIn("Rental Income", pages[name].content.decode(), f"{user.username} {name}")
            self.assertEqual(pages["boxes"].context["money"]["income"], "17,500.00")

    def test_signed_out_and_posting(self):
        self.client.logout()
        self.assertEqual(self.client.get(BOXES).status_code, 401)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(BOXES).status_code, 405)
        self.assertIn("no-store", self.client.get(BOXES)["Cache-Control"])

    def test_it_is_off_unless_the_owner_ticks_it(self):
        self.assertEqual(PERMISSIONS["see_income"]["label"], "See rental income")
        for preset in PRESETS.values() if isinstance(PRESETS, dict) else PRESETS:
            keys = preset["permissions"] if isinstance(preset, dict) else preset[-1]
            self.assertNotIn("see_income", keys)
        self.assertIn("see_income", parse_permissions("see_income"))


class TheReceiptTotalsTests(Shop):
    def totals(self, **params):
        response = self.client.get(RECEIPTS, params)
        self.assertEqual(response.status_code, 200)
        return response.context["totals"], response

    def test_totals_leave_voided_receipts_out(self):
        totals, response = self.totals()
        self.assertEqual((totals["count"], totals["voided"]), (10, 1))
        self.assertEqual((totals["received"], totals["returned"], totals["net"]), ("35,000.00", "5,500.00", "29,500.00"))
        self.assertEqual(totals["methods"], [("Cash", "27,500.00"), ("GCash", "2,000.00"), ("Maya", "0.00"), ("QR Ph", "0.00")])
        html = response.content.decode()
        for words in ('data-total="received"', "&#8369;35,000.00", "&#8369;5,500.00", "&#8369;29,500.00", "VOID"):
            self.assertIn(words, html)

    def test_totals_follow_the_filters(self):
        self.assertEqual(self.totals(kind="out")[0]["returned"], "5,500.00")
        self.assertEqual(self.totals(kind="in")[0]["net"], "35,000.00")
        void = self.totals(status="void")[0]
        self.assertEqual((void["count"], void["net"]), (1, "0.00"))
        self.assertEqual(self.totals(q=self.partly.reference_code)[0]["net"], "2,000.00")
        self.assertEqual(self.totals(**{"from": "2020-01-01", "to": "2020-01-02"})[0]["count"], 0)
        wrong, response = self.totals(**{"from": "2026-02-02", "to": "2026-02-01"})
        self.assertEqual(wrong["count"], 0)
        self.assertIn("be before the From date", response.content.decode())

    def test_every_page_is_counted_not_only_the_one_shown(self):
        for _ in range(30):
            pay(self.partly, P.BALANCE, "10")
        totals, response = self.totals()
        self.assertEqual((totals["count"], totals["received"]), (40, "35,300.00"))
        self.assertEqual(self.totals(page=2)[0], totals)

    def test_a_refund_is_called_a_refund_and_a_deposit_return_a_deposit(self):
        html = self.totals()[1].content.decode()
        self.assertEqual(html.count("Deposit returned"), 1)
        self.assertIn("Refund", html)

    def test_staff_cannot_open_all_receipts(self):
        user = User.objects.create_user("mb_s", password="x", is_staff=True)
        UserProfile.objects.update_or_create(user=user, defaults={"role": UserProfile.Role.STAFF, "permissions": "see_income"})
        self.client.force_login(user)
        self.assertEqual(self.client.get(RECEIPTS).status_code, 302)
