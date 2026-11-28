"""One list sorted by buttons (asked for 2026-11-28): the Payments page is sent each booking's money once, and its boxes, its "Money" list and
its totals only add those amounts up. So the one rule here: what the rows carry adds up, to the centavo, to the same sums the Dashboard and
Rental History make (dashboard_money.money_between), whatever the dates -- and staff without "See rental income" are never sent earnings.
Uses the small shop of test_money_boxes."""
import html as html_text
import json
import re
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from accounts.roles import PERMISSION_KEYS, join_permissions
from arabela_admin import dashboard_money, money_views, views
from arabela_admin.gown_inventory import box_of
from arabela_admin.test_cancel_booking import make_gown
from arabela_admin.test_money_boxes import DASHBOARD, DEPOSITS, PAYMENTS, Shop, give_back
from gowns.models import Gown, GownUnavailability
from reservations.models import Deduction, Payment, Reservation, ReservationItem
from reservations.test_ledger import make_booking, pay

User = get_user_model()
D = Decimal
P = Payment.Purpose


def cents(value):
    return int(value * 100)


class Rich(Shop):
    def messy(self):
        """More of everything: an older day, an extra charge going back, a deduction bigger than the deposit, several gowns, small payments."""
        today = timezone.localdate()
        old = today - timedelta(days=40)
        Payment.objects.filter(reservation=self.running).update(paid_at=timezone.make_aware(datetime.combine(old, time(10, 0))))
        extra = make_booking(stage=ReservationItem.Stage.RETURNED, pickup=-10, ret=-6, returned_on=-6)
        pay(extra, P.FULL, "5500")
        pay(extra, P.EXTRA, "300", method=Payment.Method.PM_MAYA)
        give_back(extra, "2300", method=Payment.Method.GCASH)
        broken = make_booking(stage=ReservationItem.Stage.RETURNED, pickup=-10, ret=-6, returned_on=-5, gowns=2)
        pay(broken, P.FULL, "5500", method=Payment.Method.PM_QRPH)
        Deduction.objects.create(reservation=broken, amount=D("2500.75"), reason="Torn <hem> & stain")
        pay(broken, P.EXTRA, "901")
        for _ in range(20):
            pay(self.partly, P.BALANCE, "10.05")
        return old

    def rows(self, url=PAYMENTS):
        """{booking id: what its row was sent}, read from the page the way the browser reads it."""
        page = self.client.get(url).content.decode()
        found = re.findall(r'data-booking-id="(\d+)"[^>]*?data-money="([^"]*)"', page)
        return {int(pk): json.loads(html_text.unescape(data)) for pk, data in found}

    def add(self, rows, kind, start="", end=""):
        return sum(value for row in rows.values() for day, what, value in row["e"]
                   if what == kind and (not start or day >= start) and (not end or day <= end))


class TheRowsAddUpTests(Rich):
    def test_every_kind_on_every_date_choice(self):
        old = self.messy()
        today = timezone.localdate()
        rows = self.rows()
        self.assertEqual(len(rows), Reservation.objects.count())
        for start, end in ((None, None), (today, today), (old, old), (today - timedelta(days=6), today), (None, old), (today, None),
                           (today - timedelta(days=500), today - timedelta(days=400))):
            money = dashboard_money.money_between(start, end, today)
            low, high = start.isoformat() if start else "", end.isoformat() if end else ""
            with self.subTest(start=start, end=end):
                for kind, key in (("i", "income"), ("k", "kept"), ("c", "kept_cancelled"), ("d", "collected"), ("r", "returned_deposit"),
                                  ("x", "returned_extra")):
                    self.assertEqual(self.add(rows, kind, low, high), cents(money[key]), key)
                for name, amount in money["by_method"].items():
                    self.assertEqual(self.add(rows, f"m:{name}", low, high), cents(amount), name)
            # the page opened on those dates draws the same figures before its script runs
            params = {**({"from": low} if low else {}), **({"to": high} if high else {}), "by": "paid"}
            box = self.client.get(PAYMENTS, params).context["money"]
            self.assertEqual((box["income"], box["kept"], box["collected"]),
                             tuple(f"{money[k]:,.2f}" for k in ("income", "kept", "collected")))

    def test_what_is_held_owed_and_to_give_back(self):
        self.messy()
        today = timezone.localdate()
        rows = self.rows()
        self.assertEqual(sum(r["h"] for r in rows.values()), cents(views._deposit_totals(today)["held"]))
        owing, _n, give, _g = money_views.owing_and_giveback(None, None, "return", today)
        self.assertEqual((sum(r["o"] for r in rows.values()), sum(r["g"] for r in rows.values())), (cents(owing), cents(give)))
        for kind in money_views.KINDS:
            start, end = today - timedelta(days=7), today + timedelta(days=3)
            expected = money_views.owing_and_giveback(start, end, kind, today)
            box = self.client.get(PAYMENTS, {"from": start.isoformat(), "to": end.isoformat(), "by": kind}).context["money"]
            self.assertEqual((box["owing"], box["owing_bookings"]), (f"{expected[0]:,.2f}", expected[1]), kind)

    def test_handing_money_back_changes_the_row(self):
        before = self.rows()[self.late.pk]
        self.assertEqual((before["h"], before["g"]), (140000, 140000))
        give_back(self.late, "1400")
        after = self.rows()[self.late.pk]
        self.assertEqual((after["h"], after["g"]), (0, 0))
        self.assertIn([timezone.localdate().isoformat(), "r", 140000], after["e"])

    def test_one_load_of_the_bookings_gives_the_same_sums(self):
        self.messy()
        today = timezone.localdate()
        loaded = list(money_views.tracked_bookings())
        a, b = dashboard_money.money_between(None, None, today), dashboard_money.money_between(None, None, today, reservations=loaded)
        for key in ("income", "kept", "kept_cancelled", "collected", "returned_deposit", "returned_extra", "by_method"):
            self.assertEqual(a[key], b[key], key)

    def test_the_page_does_not_ask_the_database_per_booking(self):
        def count():
            with CaptureQueriesContext(connection) as seen:
                self.assertEqual(self.client.get(PAYMENTS).status_code, 200)
            return len(seen)

        count()                                                  # the first visit also saves the sign-in
        first = count()
        self.messy()
        for _ in range(6):
            booking = make_booking(pickup=2, ret=6)
            pay(booking, P.FULL, "5500")
        self.assertEqual(count(), first)


class StaffAreNotSentEarningsTests(Rich):
    def staff(self, permissions):
        user = User.objects.create_user("mf_staff", password="x", is_staff=True)
        UserProfile.objects.update_or_create(user=user, defaults={"role": UserProfile.Role.STAFF, "permissions": join_permissions(permissions)})
        return user

    def test_without_see_rental_income(self):
        self.messy()
        self.client.force_login(self.staff([k for k in PERMISSION_KEYS if k != "see_income"]))
        rows = self.rows()
        today = timezone.localdate().isoformat()
        kinds = {what.split(":")[0] for row in rows.values() for _day, what, _value in row["e"]}
        self.assertEqual(kinds - {"d", "r", "x", "t"}, set())
        self.assertEqual({day for row in rows.values() for day, what, _v in row["e"] if what.startswith("t:")}, {today})
        page = self.client.get(PAYMENTS).content.decode()
        for words in ('value="income"', 'value="kept"', 'value="cancelled"', 'value="method-', 'data-box="income"'):
            self.assertNotIn(words, page)
        for words in ('value="today"', 'value="today-Cash"', 'value="held"', 'data-box="today"'):
            self.assertIn(words, page)
        money = dashboard_money.today_money(timezone.localdate())
        self.assertEqual(sum(v for row in rows.values() for _d, what, v in row["e"] if what.startswith("t:")),
                         cents(D(money["net"].replace(",", ""))))


class OneListTests(Rich):
    def test_the_payments_page_sorts_its_own_list(self):
        page = self.client.get(PAYMENTS).content.decode()
        for words in ("money-source", "See list", "Back to the list", "mb-sub", "mb-line", "api/money", 'class="md-arrow"', ">This month<", ">One date<"):
            self.assertNotIn(words, page, words)
        for value in ("income", "kept", "cancelled", "method-Cash", "method-GCash", "method-Maya", "method-QR Ph", "held", "collected", "returned"):
            self.assertIn(f'<option value="{value}"', page, value)
        self.assertEqual(re.findall(r'data-box="([a-z]+)"', page), ["income", "held", "owing"])
        self.assertEqual(page.count('class="pm-total"'), 1)
        self.assertEqual(page.count('class="custom-scrollbar pm-list'), 1)

    def test_the_deposits_page_rows_add_up_to_its_buttons(self):
        self.messy()
        give_back(self.late, "1400")
        response = self.client.get(DEPOSITS)
        listed = response.context["reservations"]
        money = response.context["deposit_money"]
        number = lambda text: int(D(text.replace(",", "")) * 100)
        self.assertEqual(sum(r.cents_held for r in listed), number(money["held"]))
        for state, field, key in (("Held", "cents_held", "holding"), ("Return due", "cents_due", "due"), ("Returned", "cents_back", "back")):
            self.assertEqual(sum(getattr(r, field) for r in listed if r.dep["state"] == state), number(money[key]), state)
        page = response.content.decode()
        self.assertEqual(page.count('data-held="'), len(listed))
        self.assertEqual(page.count('class="sd-now"'), len(listed))
        for words in ("money-source", "See list", "Rental Income", "Still to Collect"):
            self.assertNotIn(words, page)
        self.assertNotIn(reverse("arabela_admin:payments") + "?money", page)

    def test_the_other_addresses_are_gone(self):
        for path in ("/admin-panel/api/money-boxes/", "/admin-panel/api/money-source/"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_the_gown_boxes_open_the_catalog_on_that_box(self):
        free, blocked = make_gown(1, status=Gown.Status.AVAILABLE), make_gown(2, status=Gown.Status.AVAILABLE, name="Blocked Gown")
        today = timezone.localdate()
        GownUnavailability.objects.create(gown=blocked, start_date=today, end_date=today)
        page = self.client.get(DASHBOARD).content.decode()
        catalog = reverse("arabela_admin:gown_catalog")
        for status in ("Available", "Blocked", "Reserved", "Out-of-Stock"):
            self.assertIn(f'href="{catalog}?status={status}"', page)
        response = self.client.get(catalog)
        self.assertIn('<option value="Blocked">Blocked today</option>', response.content.decode())
        filed = {m["id"]: m.get("box") or m["status"] for m in response.context["gowns_min"]}
        self.assertEqual((filed[free.id], filed[blocked.id]), ("Available", "Blocked"))
        self.assertEqual([box_of("Available", True), box_of("Available", False), box_of("Reserved", True), box_of("Out-of-Stock", True)],
                         ["Blocked", "Available", "Reserved", "Out-of-Stock"])
