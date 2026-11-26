"""The Reservation Report inside Rental History and its PDF / Excel downloads.

What is checked here: which dates each choice means (All, Today, Last 7 days, This month, Custom dates, by pick-up, return or event day), that the four
figures say exactly what the Dashboard and Security Deposits say, that money counts on the day it was paid (and the note adds up when that differs from
the list), cancelled gowns, balances once per booking, the top rented gowns and categories, the list filters, who may download (the shop owner only, every
download written in the Audit Log), and that both files open, hold the same numbers, and never turn a typed name into a formula."""
import io
import json
import re
import zipfile
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from accounts.models import AuditLog, UserProfile
from arabela_admin import dashboard_money, rental_history, rental_report
from arabela_admin.views import _deposit_totals, _deposits_held
from gowns.models import Gown
from reservations.models import Payment, Reservation, ReservationItem
from reservations.test_ledger import pay

try:
    import pymupdf
except ImportError:          # reading the PDF's words back is an extra check; the build itself is always tested
    pymupdf = None

User = get_user_model()
D = Decimal
S = ReservationItem.Stage
P = Payment.Purpose
URL = reverse("arabela_admin:rental_history")


def pdf_url():
    return reverse("arabela_admin:rental_report_download", args=["pdf"])


def excel_url():
    return reverse("arabela_admin:rental_report_download", args=["excel"])


def noon(day):
    return timezone.make_aware(datetime.combine(day, time(12, 0)))


# ---------------------------------------------------------------------------------------------------------------------------------
class PeriodTests(SimpleTestCase):
    today = date(2026, 11, 26)

    def read(self, **params):
        return rental_report.read_period(params, self.today)

    def test_nothing_or_nonsense_means_all_by_pickup(self):
        for params in ({}, {"period": "forever", "by": "birthday"}, {"period": "", "by": ""}):
            period = rental_report.read_period(params, self.today)
            self.assertEqual((period.key, period.kind, period.start, period.end, period.error), ("all", "pickup", None, None, ""))
            self.assertEqual((period.label, period.tag, period.file_part), ("All dates", "All dates", "all dates"))
            self.assertTrue(period.holds(date(2019, 1, 1)) and period.holds(date(2031, 1, 1)))

    def test_today_last_seven_days_and_this_month(self):
        today = self.read(period="today")
        self.assertEqual((today.start, today.end, today.label, today.tag, today.file_part), (self.today, self.today, "Today, Nov 26, 2026", "Today", "2026-11-26"))
        week = self.read(period="week", by="return")
        self.assertEqual((week.start, week.end, week.kind_label), (date(2026, 11, 20), self.today, "Return"))
        self.assertEqual((week.label, week.tag), ("Last 7 days, Nov 20 to Nov 26, 2026", "Last 7 days"))
        self.assertTrue(week.holds(date(2026, 11, 20)) and week.holds(self.today))
        self.assertFalse(week.holds(date(2026, 11, 19)) or week.holds(date(2026, 11, 27)))
        month = self.read(period="month", by="event")
        self.assertEqual((month.start, month.end, month.label, month.file_part), (date(2026, 11, 1), date(2026, 11, 30), "November 2026", "2026-11-01 to 2026-11-30"))
        self.assertEqual(rental_report.read_period({"period": "month"}, date(2026, 12, 5)).end, date(2026, 12, 31))
        self.assertEqual(rental_report.read_period({"period": "month"}, date(2028, 2, 5)).end, date(2028, 2, 29))
        across = rental_report.read_period({"period": "week"}, date(2027, 1, 3))
        self.assertEqual(across.label, "Last 7 days, Dec 28, 2026 to Jan 3, 2027")

    def test_custom_dates_of_any_length(self):
        period = self.read(period="custom", **{"from": "2026-06-01", "to": "2027-06-30"})
        self.assertEqual((period.start, period.end, period.error), (date(2026, 6, 1), date(2027, 6, 30), ""))
        self.assertEqual((period.label, period.file_part), ("Jun 1, 2026 to Jun 30, 2027", "2026-06-01 to 2027-06-30"))
        self.assertEqual(period.params(), {"period": "custom", "by": "pickup", "from": "2026-06-01", "to": "2027-06-30"})
        one_day = self.read(period="custom", **{"from": "2026-11-05", "to": "2026-11-05"})
        self.assertEqual((one_day.label, one_day.file_part), ("Nov 5, 2026", "2026-11-05"))
        self.assertEqual(self.read(period="custom", **{"from": "2026-03-01", "to": "2026-03-31"}).label, "Mar 1 to Mar 31, 2026")
        long = self.read(period="custom", **{"from": "2000-01-01", "to": "2100-12-31"})
        self.assertEqual(long.error, "")

    def test_custom_dates_that_cannot_be_used_hold_nothing_and_say_why(self):
        cases = [({}, rental_report.MISSING_DATES), ({"from": "2026-11-01"}, rental_report.MISSING_DATES),
                 ({"to": "2026-11-01"}, rental_report.MISSING_DATES), ({"from": "abc", "to": "2026-11-01"}, rental_report.MISSING_DATES),
                 ({"from": "2026-02-30", "to": "2026-03-01"}, rental_report.MISSING_DATES), ({"from": "0202-06-01", "to": "2026-06-30"}, rental_report.MISSING_DATES),
                 ({"from": "2026-11-30", "to": "2026-11-01"}, rental_report.WRONG_ORDER)]
        for params, message in cases:
            with self.subTest(params=params):
                period = self.read(period="custom", **params)
                self.assertEqual(period.error, message)
                self.assertFalse(period.holds(self.today))
                self.assertEqual(period.label, "Custom dates")
        kept = self.read(period="custom", **{"from": "2026-11-30", "to": "2026-11-01"})
        self.assertEqual((kept.typed_from, kept.typed_to), ("2026-11-30", "2026-11-01"))           # the boxes keep what was typed
        half = self.read(period="custom", **{"from": "2026-11-30", "to": "junk"})
        self.assertEqual((half.typed_from, half.typed_to), ("2026-11-30", ""))

    def test_the_filters_only_take_real_values(self):
        filters = rental_report.read_filters({"status": "Overdue", "q": "  Maria  ", "category": " Suit ", "channel": "Walk-in", "gown": "Suit|black suit 1"})
        self.assertEqual((filters.status, filters.q, filters.category, filters.channel, filters.gown), ("Overdue", "Maria", "Suit", "Walk-in", "Suit|black suit 1"))
        self.assertEqual(filters.words("Black Suit 1"),
                         ["Status: Overdue", 'Search: "Maria"', "Category: Suit", "Walk-in bookings only", "Gown: Black Suit 1"])
        odd = rental_report.read_filters({"status": "Lost", "channel": "Carrier pigeon", "q": "x" * 500, "category": "y" * 500, "gown": "z" * 500})
        self.assertEqual((odd.status, odd.channel, len(odd.q), len(odd.category), len(odd.gown)), ("All", "", 100, 60, 200))
        self.assertFalse(rental_report.read_filters({}).active)
        self.assertEqual(rental_report.read_filters({}).words(), [])


# ---------------------------------------------------------------------------------------------------------------------------------
class ReportBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("rr_owner", "rr.owner@gmail.com", "x", first_name="Olive", last_name="Owner")
        UserProfile.objects.update_or_create(user=cls.owner, defaults={"role": UserProfile.Role.OWNER})
        cls.staff = User.objects.create_user("rr_staff", "rr.staff@gmail.com", "x", is_staff=True, first_name="Sam", last_name="Staff")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.STAFF})
        cls.manager = User.objects.create_user("rr_manager", "rr.manager@gmail.com", "x", is_staff=True, first_name="Mina", last_name="Manager")
        UserProfile.objects.update_or_create(user=cls.manager, defaults={"role": UserProfile.Role.MANAGER})
        cls.customer = User.objects.create_user("rr_customer", "rr.customer@gmail.com", "x")

    def setUp(self):
        self.today = timezone.localdate()
        self.month_start = self.today.replace(day=1)
        self.numbers = iter(range(1, 999))
        self.receipts = iter(range(1, 9999))
        self.client.force_login(self.owner)

    def gown(self, name, category=Gown.Category.WEDDING_GOWN, price="3000", size=Gown.Size.MEDIUM):
        number = next(self.numbers)
        return Gown.objects.create(gown_id=f"RR-{number:04d}", name=name, category=category, color_name="White", color_code="WH", size=size,
                                   rental_price=D(price), status=Gown.Status.AVAILABLE)

    def booking(self, name="Ana Cruz", gowns=None, *, pickup=2, ret=6, stage=S.PICKUP, status=Reservation.Status.CONFIRMED,
                channel=Reservation.Channel.ONLINE, deposit="2000", reviewed=True, rate=200, **item):
        """One booking; `gowns` = [Gown, ...] (one 3,000 wedding gown by default); pickup / ret are day offsets from today, or dates."""
        gowns = gowns or [self.gown(f"Gown for {name}")]
        rental = sum(g.rental_price for g in gowns)
        reservation = Reservation.objects.create(
            customer_name=name, phone="09171234567", status=status, channel=channel, late_fee_per_day=rate, rental_subtotal=rental,
            security_deposit=D(deposit), total_amount=rental + D(deposit), reviewed_at=timezone.now() if reviewed else None)
        as_day = lambda value: value if isinstance(value, date) else self.today + timedelta(days=value)
        for gown in gowns:
            ReservationItem.objects.create(
                reservation=reservation, gown=gown, gown_name=gown.name, size=gown.size, rental_price=gown.rental_price, rental_date=as_day(pickup),
                return_date=as_day(ret), original_return_date=as_day(ret), stage=stage, **item)
        return reservation

    def paid(self, reservation, purpose, amount, *, on=None, **kw):
        payment = pay(reservation, purpose, amount, **kw)
        if on is not None:
            Payment.objects.filter(pk=payment.pk).update(paid_at=noon(on))
        return payment

    def report(self, **params):
        response = self.client.get(URL, params)
        self.assertEqual(response.status_code, 200)
        return response.context["report"]

    def codes(self, **params):
        return sorted(line["code"] for line in self.report(**params)["listed"])

    def custom(self, start, end, **more):
        return dict(period="custom", **{"from": start.isoformat(), "to": end.isoformat()}, **more)


# ---------------------------------------------------------------------------------------------------------------------------------
class DatesTests(ReportBase):
    def test_the_dates_choose_by_pickup_return_or_event_day(self):
        june = date(self.today.year + 1, 6, 10)
        a = self.booking("A", pickup=june, ret=june + timedelta(days=25))
        b = self.booking("B", pickup=june - timedelta(days=30), ret=june + timedelta(days=2))
        ReservationItem.objects.filter(reservation=a).update(event_date=june + timedelta(days=24))
        window = self.custom(june - timedelta(days=1), june + timedelta(days=3))
        self.assertEqual(self.codes(**window), [a.reference_code])
        self.assertEqual(self.codes(**dict(window, by="return")), [b.reference_code])
        self.assertEqual(self.codes(**dict(window, by="event")), [b.reference_code])             # B's event: its return day less 2

    def test_the_real_days_count_once_a_gown_went_out_and_came_back(self):
        out = self.booking("Out", pickup=-5, ret=-1, stage=S.RETURNED, picked_up_on=self.today - timedelta(days=3), returned_on=self.today)
        for params, wanted in ((dict(period="today", by="return"), [out.reference_code]), (dict(period="today"), []),
                               (self.custom(self.today - timedelta(days=3), self.today - timedelta(days=3)), [out.reference_code]),
                               (self.custom(self.today - timedelta(days=5), self.today - timedelta(days=5)), [])):
            with self.subTest(params=params):
                self.assertEqual(self.codes(**params), wanted)

    def test_a_long_custom_range_holds_this_year_and_next(self):
        here = self.booking("Here", pickup=1, ret=4)
        far = self.booking("Far", pickup=date(self.today.year + 1, 6, 30), ret=date(self.today.year + 1, 7, 3))
        later = self.booking("Later", pickup=date(self.today.year + 1, 7, 1), ret=date(self.today.year + 1, 7, 4))
        start, end = date(self.today.year, 6, 1), date(self.today.year + 1, 6, 30)
        if here.items.get().rental_date < start:
            start = here.items.get().rental_date
        self.assertEqual(self.codes(**self.custom(start, end)), sorted([here.reference_code, far.reference_code]))
        self.assertIn(later.reference_code, self.codes(period="all"))

    def test_the_list_is_newest_first_by_the_chosen_day_and_the_downloads_oldest_first(self):
        first = self.booking("First", pickup=1, ret=30)
        second = self.booking("Second", pickup=5, ret=8)
        self.assertEqual([l["code"] for l in self.report()["listed"]], [second.reference_code, first.reference_code])
        self.assertEqual([l["code"] for l in self.report(by="return")["listed"]], [first.reference_code, second.reference_code])
        report = self.report()
        self.assertEqual([l["code"] for l in rental_report.listed_for(report, rental_report.Filters())], [first.reference_code, second.reference_code])

    def test_no_rentals_in_the_dates_says_so_and_the_boxes_are_zero(self):
        self.booking("Someone", pickup=40, ret=44)
        response = self.client.get(URL, {"period": "today"})
        html = response.content.decode()
        self.assertIn("No rentals in this period.", html)
        self.assertNotIn("No rentals recorded yet", html)
        report = response.context["report"]
        self.assertEqual((report["rentals"], report["owing"], report["income"]), (0, D("0"), D("0")))
        self.assertIn("No gown was rented in these dates.", html)

    def test_dates_that_cannot_be_used_show_why_and_nothing_else(self):
        self.booking("Someone", pickup=1, ret=4)
        response = self.client.get(URL, {"period": "custom", "from": "2026-11-30", "to": "2026-11-01"})
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("The To date can&#x27;t be before the From date.", html)
        self.assertIn("Fix the dates above to see the rentals.", html)
        self.assertNotIn('class="rh-boxes"', html)
        self.assertNotIn(pdf_url(), html.split('id="rh-view-data"')[0])                    # no download of dates that cannot be used
        self.assertEqual(response.context["report"]["listed"], [])
        self.assertEqual(response.context["report"]["income"], D("0"))


# ---------------------------------------------------------------------------------------------------------------------------------
class FigureTests(ReportBase):
    def scenario(self):
        """A mix of everything the money can do."""
        today, last_month = self.today, self.month_start - timedelta(days=3)
        full = self.booking("Full Payer", pickup=2, ret=6)
        self.paid(full, P.FULL, "5000")
        ahead = self.booking("Next Year", pickup=date(today.year + 1, 3, 1), ret=date(today.year + 1, 3, 4))
        self.paid(ahead, P.DEPOSIT, "2000")
        self.paid(ahead, P.BALANCE, "1500")
        old = self.booking("Paid Last Month", pickup=3, ret=7)
        self.paid(old, P.FULL, "5000", on=last_month)
        deposit_only = self.booking("Deposit Only", pickup=4, ret=8)
        self.paid(deposit_only, P.DEPOSIT, "2000")
        two = self.booking("Two Gowns", [self.gown("Twin A"), self.gown("Twin B", price="1000")], pickup=1, ret=5)
        self.paid(two, P.DEPOSIT, "2000")
        voided = self.booking("Voided", pickup=1, ret=5)
        self.paid(voided, P.FULL, "5000", status=Payment.Status.VOID)
        late = self.booking("Late One", pickup=-10, ret=-6, stage=S.RETURNED, picked_up_on=today - timedelta(days=10), returned_on=today)
        self.paid(late, P.FULL, "5000", on=today - timedelta(days=10))
        self.paid(late, P.DEPOSIT_RETURN, "800", direction=Payment.Direction.OUT)
        cancelled = self.booking("Cancelled Later", pickup=6, ret=9, status=Reservation.Status.CANCELLED)
        self.paid(cancelled, P.FULL, "5000")
        pending = self.booking("Pending", pickup=7, ret=10, status=Reservation.Status.PENDING, reviewed=False)
        rejected = self.booking("Rejected", pickup=7, ret=10, status=Reservation.Status.REJECTED, reviewed=False)
        extra = self.booking("Extra Charge", pickup=-9, ret=-8, stage=S.RETURNED, picked_up_on=today - timedelta(days=9), returned_on=today - timedelta(days=8))
        self.paid(extra, P.FULL, "5000", on=today - timedelta(days=9))
        self.paid(extra, P.EXTRA, "300")
        return {"full": full, "ahead": ahead, "old": old, "deposit_only": deposit_only, "two": two, "voided": voided, "late": late,
                "cancelled": cancelled, "pending": pending, "rejected": rejected, "extra": extra}

    def test_this_month_says_what_the_dashboard_says(self):
        self.scenario()
        report = self.report(period="month")
        dashboard = dashboard_money.month_money(self.today)
        self.assertEqual(report["text"]["income"], dashboard["income"])
        self.assertEqual(report["income_bookings"], dashboard["bookings"])
        self.assertEqual(report["text"]["kept"], dashboard["kept"])
        self.assertGreater(report["income"], 0)
        page = self.client.get(reverse("arabela_admin:dashboard")).content.decode()
        self.assertIn(f"&#8369;{report['text']['income']}", page)

    def test_deposits_held_are_the_security_deposits_pages_figure(self):
        self.scenario()
        report = self.report()
        self.assertEqual(report["held"], _deposit_totals(self.today))
        self.assertEqual(report["text"]["held"], _deposits_held(self.today)["held"])
        self.assertEqual(report["text"]["due"], _deposits_held(self.today)["due"])

    def test_deposits_are_never_income_and_are_counted_apart(self):
        booking = self.booking("Depositor", pickup=2, ret=6)
        self.paid(booking, P.DEPOSIT, "2000")
        report = self.report(period="today")
        self.assertEqual((report["income"], report["collected"], report["returned"], report["income_bookings"]), (D("0.00"), D("2000.00"), D("0.00"), 0))

    def test_money_counts_on_the_day_it_was_paid(self):
        booking = self.booking("Early Payer", pickup=date(self.today.year + 1, 5, 1), ret=date(self.today.year + 1, 5, 4))
        self.paid(booking, P.FULL, "5000")
        today = self.report(period="today")
        self.assertEqual((today["income"], today["rentals"]), (D("3000.00"), 0))                 # paid today, picked up next year
        later = self.report(**self.custom(date(self.today.year + 1, 5, 1), date(self.today.year + 1, 5, 31)))
        self.assertEqual((later["income"], later["rentals"], later["listed_value"]), (D("0.00"), 1, D("3000.00")))
        for report in (today, later):
            self.assertEqual(report["income"], report["listed_value"] + report["other_paid"] - report["not_paid"])

    def test_the_note_adds_up_for_every_choice_of_dates(self):
        self.scenario()
        far = date(self.today.year + 1, 1, 1)
        choices = [{}, {"period": "today"}, {"period": "week"}, {"period": "month"}, {"period": "month", "by": "return"},
                   {"period": "month", "by": "event"}, self.custom(far, far + timedelta(days=200)),
                   self.custom(self.today - timedelta(days=400), self.today + timedelta(days=400))]
        for params in choices:
            with self.subTest(params=params):
                report = self.report(**params)
                self.assertEqual(report["income"], report["listed_value"] + report["other_paid"] - report["not_paid"])
                self.assertEqual(report["listed_value"], sum((l["price"] for l in report["listed"]), D("0")))
                totals = report["payment_totals"]
                self.assertEqual(totals["rental"], report["income"])
                self.assertEqual(totals["deposit"], report["collected"])
                self.assertEqual(totals["handed_back"], report["returned"])
                self.assertEqual(totals["received"], totals["rental"] + totals["deposit"] + totals["extra"] + totals["other"])
                text = " ".join(rental_report.explain(report))
                if report["income"] != report["listed_value"]:
                    self.assertIn(f"Rental income PHP {report['income']:,.2f} = the price of all rentals in these dates PHP {report['listed_value']:,.2f}", text)
                    self.assertEqual("plus rental money" in text, report["other_paid"] > 0)
                    self.assertEqual("minus the part" in text, report["not_paid"] > 0)
                    self.assertIn("(not only the filtered list)", " ".join(rental_report.explain(report, filtered=True)))
                else:
                    self.assertIn("are both PHP", text)

    def test_the_rentals_box_counts_gowns_by_status_and_cancelled_ones_apart(self):
        made = self.scenario()
        report = self.report()
        listed = report["listed"]
        self.assertEqual(report["rentals"], len(listed))
        self.assertEqual(sum(entry["count"] for entry in report["by_status"]), report["rentals"])
        self.assertNotIn(made["cancelled"].reference_code, [l["code"] for l in listed])
        self.assertNotIn(made["rejected"].reference_code, [l["code"] for l in listed])
        self.assertEqual(report["cancelled"], 1)                                    # the cancelled booking's gown; a rejected request is not counted
        one_gown = ReservationItem.objects.filter(reservation=made["two"]).first()
        ReservationItem.objects.filter(pk=one_gown.pk).update(stage=S.CANCELLED)
        report = self.report()
        self.assertEqual(report["cancelled"], 2)
        self.assertEqual(self.report(period="today")["cancelled"], 0)               # neither was to be picked up today
        statuses = {l["status"] for l in report["listed"]}
        self.assertIn(rental_history.PENDING, statuses)

    def test_still_to_collect_is_each_listed_bookings_balance_once(self):
        two = self.booking("Two Gowns", [self.gown("X"), self.gown("Y")], pickup=1, ret=5)
        self.paid(two, P.DEPOSIT, "2000")                                          # 6,000 rental left
        paid = self.booking("All Paid", pickup=1, ret=5)
        self.paid(paid, P.FULL, "5000")
        self.booking("Request", pickup=1, ret=5, status=Reservation.Status.PENDING, reviewed=False)
        report = self.report()
        self.assertEqual((report["owing"], report["owing_bookings"]), (D("6000.00"), 1))
        lines = rental_report.listed_for(report, rental_report.Filters())
        written = [(l["code"], l["balance_shown"]) for l in lines if l["balance_shown"] is not None and l["balance_shown"] > 0]
        self.assertEqual(written, [(two.reference_code, D("6000.00"))])
        self.assertEqual([l["balance"] for l in lines if l["status"] == rental_history.PENDING], [None])

    def test_top_gowns_add_up_one_design_and_leave_out_requests_and_no_shows(self):
        medium, large = self.gown("Rose Gown"), self.gown("Rose Gown", size=Gown.Size.LARGE, price="3500")
        blue = self.gown("Blue Suit", category=Gown.Category.SUIT, price="9000")
        self.booking("One", [medium], pickup=1, ret=5)
        self.booking("Two", [large], pickup=2, ret=6)
        self.booking("Three", [blue], pickup=3, ret=7)
        self.booking("Request", [blue], pickup=3, ret=7, status=Reservation.Status.PENDING, reviewed=False)
        self.booking("No Show", [blue], pickup=-9, ret=-5)                          # nobody picked it up
        report = self.report()
        self.assertEqual([(g["name"], g["times"], g["amount"], g["share"]) for g in report["top"]],
                         [("Rose Gown", 2, D("6500.00"), 100), ("Blue Suit", 1, D("9000.00"), 50)])
        self.assertEqual([(c["name"], c["times"], c["amount"]) for c in report["categories"]], [("Wedding Gown", 2, D("6500.00")), ("Suit", 1, D("9000.00"))])
        self.assertEqual(report["top"][0]["key"], "Wedding Gown|rose gown")
        self.assertEqual(len([l for l in report["listed"] if l["gown_key"] == "Wedding Gown|rose gown"]), 2)

    def test_only_five_gowns_are_top_and_ties_go_by_money_then_name(self):
        for index, price in enumerate(("1000", "3000", "2000", "5000", "4000", "2000"), 1):
            self.booking(f"C{index}", [self.gown(f"Design {index}", price=price)], pickup=index, ret=index + 3)
        report = self.report()
        self.assertEqual([g["name"] for g in report["top"]], ["Design 4", "Design 5", "Design 2", "Design 3", "Design 6"])
        self.assertEqual(len(report["gowns"]), 6)

    def test_a_gown_removed_from_the_catalog_still_counts(self):
        booking = self.booking("Gone", pickup=1, ret=5)
        ReservationItem.objects.filter(reservation=booking).update(gown=None)
        report = self.report()
        self.assertEqual(report["listed"][0]["category"], rental_report.REMOVED_GOWN)
        self.assertEqual(report["categories"][0]["name"], rental_report.REMOVED_GOWN)

    def test_the_fees_of_each_gown_are_the_ones_the_opened_row_names(self):
        late = self.booking("Late", pickup=-9, ret=-2, stage=S.RESERVED, picked_up_on=self.today - timedelta(days=9))
        self.booking("No Show", pickup=-9, ret=-5)
        report = self.report()
        fees = {l["code"]: l["fees"] for l in report["listed"]}
        self.assertEqual(fees[late.reference_code], D("400.00"))
        self.assertEqual(sorted(fees.values()), [D("0.00"), D("400.00")])


# ---------------------------------------------------------------------------------------------------------------------------------
class PageTests(ReportBase):
    def test_the_page_shows_the_four_figures_the_two_lists_and_the_dates(self):
        booking = self.booking("Maria Santos", [self.gown("Pearl Gown")], pickup=1, ret=5)
        self.paid(booking, P.FULL, "5000")
        html = self.client.get(URL, {"period": "month"}).content.decode()
        summary = html[html.index('data-live-region="history-summary"'):html.index('class="rh-card"')]
        for words in ("Rental Income", "Deposits Held", "Rentals", "Still to Collect", "Top rented gowns", "Rentals by category", "Pearl Gown",
                      "&#8369;3,000.00", "&#8369;2,000.00", f"{self.month_start:%B %Y}", "deposits not included", "Cancelled (not listed)"):
            self.assertIn(words, summary, words)
        self.assertIn('<a class="rh-period is-on" href=', html)
        self.assertIn('x-data="arabelaRentalHistory()"', html)
        self.assertIn("mx-auto rh-wide p-4 md:p-6", html)

    def test_a_figure_too_long_for_its_box_is_drawn_a_little_smaller(self):
        gowns = [self.gown("Million Gown", price="999999"), self.gown("Million Gown Too", price="999999")]
        booking = self.booking("Rich", gowns, pickup=1, ret=5)
        self.paid(booking, P.FULL, "2001998")
        html = self.client.get(URL).content.decode()
        self.assertIn('<p class="rh-num rh-num--long">&#8369;1,999,998.00</p>', html)
        self.assertIn('<p class="rh-num">&#8369;2,000.00</p>', html)                            # a short figure stays big

    def test_the_choices_keep_the_filters_in_their_addresses(self):
        self.booking("Someone", pickup=1, ret=5)
        response = self.client.get(URL, {"period": "month", "by": "return", "status": "Overdue", "q": "some", "channel": "Walk-in"})
        links = {link["key"]: link["url"] for link in response.context["period_links"]}
        self.assertIn("period=week&by=return&status=Overdue&q=some&channel=Walk-in", links["week"])
        self.assertIn("period=custom&by=return&from=", links["custom"])
        kinds = {link["key"]: link["url"] for link in response.context["kind_links"]}
        self.assertIn("period=month&by=event", kinds["event"])
        files = {link["kind"]: link["url"] for link in response.context["download_links"]}
        self.assertEqual(files["pdf"], pdf_url() + "?period=month&by=return&status=Overdue&q=some&channel=Walk-in")
        view = json.loads(re.search(r'<script id="rh-view-data" type="application/json">(.*?)</script>', response.content.decode(), re.S).group(1))
        self.assertEqual((view["status"], view["q"], view["channel"], view["period"], view["by"]), ("Overdue", "some", "Walk-in", "month", "return"))
        self.assertEqual(view["files"], {"pdf": pdf_url(), "excel": excel_url()})

    def test_custom_dates_have_the_two_boxes_with_this_month_to_start(self):
        html = self.client.get(URL, {"period": "custom"}).content.decode()
        self.assertIn('id="rh-from" name="from" value="%s"' % self.month_start.isoformat(), self.client.get(URL, {
            "period": "custom", "from": self.month_start.isoformat(), "to": self.today.isoformat()}).content.decode())
        self.assertIn(rental_report.MISSING_DATES, html)
        plain = self.client.get(URL).content.decode()
        self.assertNotIn('id="rh-from"', plain)
        self.assertIn("period=custom&amp;by=pickup&amp;from=%s&amp;to=%s" % (self.month_start.isoformat(), self.today.isoformat()), plain)

    def test_the_download_buttons_are_the_owners_only(self):
        html = self.client.get(URL).content.decode()
        self.assertIn("Download report:", html)
        self.assertIn(f'href="{pdf_url()}?period=all&amp;by=pickup"', html)
        self.assertIn(f'href="{excel_url()}?period=all&amp;by=pickup"', html)
        for user in (self.staff, self.manager):
            self.client.force_login(user)
            html = self.client.get(URL).content.decode()
            self.assertNotIn("Download report:", html)
            self.assertNotIn('class="rh-btn rh-dl"', html)

    def test_a_typed_name_never_becomes_page_code(self):
        nasty = 'Gown "<script>alert(1)</script>" & \'friends\''
        booking = self.booking('<b>Eve</b> "quoted"', [self.gown(nasty)], pickup=1, ret=5)
        self.paid(booking, P.FULL, "5000")
        response = self.client.get(URL, {"q": '"><script>alert(2)</script>', "category": "</select><script>x</script>"})
        html = response.content.decode()
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<b>Eve</b>", html)
        self.assertNotIn("<script>x</script>", html)
        view = json.loads(re.search(r'<script id="rh-view-data" type="application/json">(.*?)</script>', html, re.S).group(1))
        self.assertEqual(view["q"], '"><script>alert(2)</script>')

    def test_more_bookings_ask_the_database_nothing_more(self):
        def count():
            with CaptureQueriesContext(connection) as captured:
                self.assertEqual(self.client.get(URL, {"period": "month"}).status_code, 200)
            return len(captured.captured_queries)

        first = self.booking("One", pickup=1, ret=5)
        self.paid(first, P.FULL, "5000")
        count()                                 # the very first load also creates the shop's settings row
        before = count()
        for number in range(4):
            made = self.booking(f"More {number}", pickup=1, ret=5)
            self.paid(made, P.DEPOSIT, "2000")
        self.booking("Cancelled", pickup=1, ret=5, status=Reservation.Status.CANCELLED)
        self.assertEqual(count(), before)


# ---------------------------------------------------------------------------------------------------------------------------------
class DownloadBase(ReportBase):
    def download(self, url, **params):
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response

    def book_of(self, **params):
        return load_workbook(io.BytesIO(self.download(excel_url(), **params).content))

    def sheet_rows(self, sheet, columns):
        """The rows under the heading, up to the first blank line."""
        out = []
        for row in sheet.iter_rows(min_row=2, max_col=columns, values_only=True):
            if all(value is None for value in row):
                break
            out.append(row)
        return out

    def figures(self, book):
        return {row[0].strip(): row[1] for row in book["Summary"].iter_rows(values_only=True) if row and isinstance(row[0], str)}


class AccessTests(DownloadBase):
    def test_the_owner_gets_both_files_with_a_clear_name(self):
        pdf = self.download(pdf_url(), period="custom", **{"from": "2026-06-01", "to": "2027-06-30"})
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertEqual(pdf["Content-Disposition"], 'attachment; filename="Arabela Reservation Report 2026-06-01 to 2027-06-30.pdf"')
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertIn("no-store", pdf["Cache-Control"])
        excel = self.download(excel_url())
        self.assertEqual(excel["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertEqual(excel["Content-Disposition"], 'attachment; filename="Arabela Reservation Report all dates.xlsx"')
        self.assertEqual(excel["X-Content-Type-Options"], "nosniff")
        self.assertEqual(load_workbook(io.BytesIO(excel.content)).sheetnames, ["Summary", "Rentals", "Top Gowns", "Payments"])

    def test_every_download_is_written_in_the_audit_log(self):
        self.download(pdf_url(), period="custom", status="Returned", **{"from": "2026-06-01", "to": "2027-06-30"})
        line = AuditLog.objects.get(action="report.download")
        self.assertEqual(line.summary, "Olive Owner downloaded the Reservation Report (Jun 1, 2026 to Jun 30, 2027) as PDF")
        self.assertEqual((line.category, line.sensitive, line.outcome), ("Bookings", True, "ok"))
        self.assertEqual(line.details, {"dates": "Jun 1, 2026 to Jun 30, 2027", "dates by": "Pick-up", "list filters": "Status: Returned"})
        self.download(excel_url(), period="month", by="event")
        self.assertEqual(AuditLog.objects.filter(action="report.download").count(), 2)
        self.assertIn(f"({self.month_start:%B %Y}) as Excel", AuditLog.objects.filter(action="report.download").order_by("-id").first().summary)

    def test_staff_and_managers_are_refused_and_the_attempt_is_written(self):
        for user in (self.staff, self.manager):
            self.client.force_login(user)
            for url in (pdf_url(), excel_url()):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.content.decode(), "Only the shop owner can download the reservation report.")
        lines = AuditLog.objects.filter(action="report.download")
        self.assertEqual(lines.count(), 4)
        self.assertEqual({(l.outcome, l.sensitive) for l in lines}, {("refused", True)})
        self.assertIn("Sam Staff's attempt was refused: downloaded the Reservation Report (All dates) as PDF", [l.summary for l in lines])

    def test_a_visitor_or_a_customer_is_sent_to_sign_in_and_nothing_is_written(self):
        self.client.logout()
        for user in (None, self.customer):
            if user:
                self.client.force_login(user)
            response = self.client.get(pdf_url())
            self.assertEqual(response.status_code, 302)
            self.assertIn(reverse("arabela_admin:admin_login"), response["Location"])
        self.assertFalse(AuditLog.objects.filter(action="report.download").exists())

    def test_wrong_dates_or_kind_give_no_file_and_no_line(self):
        response = self.client.get(pdf_url(), {"period": "custom", "from": "2026-11-30", "to": "2026-11-01"})
        self.assertEqual((response.status_code, response.content.decode()), (400, rental_report.WRONG_ORDER))
        self.assertEqual(self.client.get(excel_url(), {"period": "custom"}).status_code, 400)
        self.assertEqual(self.client.get(reverse("arabela_admin:rental_report_download", args=["word"])).status_code, 404)
        self.assertEqual(self.client.post(pdf_url()).status_code, 405)
        self.assertFalse(AuditLog.objects.filter(action="report.download").exists())


class ExcelTests(DownloadBase):
    def test_the_rentals_sheet_lists_the_filtered_rentals_with_real_numbers_and_days(self):
        gown = self.gown("Pearl Gown", size=Gown.Size.LARGE)
        booking = self.booking("Maria Santos", [gown], pickup=1, ret=5, channel=Reservation.Channel.WALK_IN)
        self.paid(booking, P.DEPOSIT, "2000")
        other = self.booking("Other Person", pickup=1, ret=5)
        self.paid(other, P.FULL, "5000")
        book = self.book_of(channel="Walk-in")
        sheet = book["Rentals"]
        headings = [c.value for c in sheet[1]]
        self.assertEqual(headings, ["Booking no.", "Customer", "Gown", "Category", "Size", "Online / Walk-in", "Booked", "Pick-up", "Return", "Event",
                                    "Status", "Rental price", "Fees", "Balance"])
        [row] = self.sheet_rows(sheet, 14)
        pickup = self.today + timedelta(days=1)
        self.assertEqual(row[:6], (booking.reference_code, "Maria Santos", "Pearl Gown", "Wedding Gown", "Large", "Walk-in"))
        self.assertEqual((row[7].date(), row[10], row[11], row[12], row[13]), (pickup, "Waiting for pick-up", 3000, 0, 3000))
        self.assertEqual(sheet["L2"].number_format, "#,##0.00")
        self.assertEqual(sheet.freeze_panes, "C2")
        self.assertEqual(sheet.auto_filter.ref, "A1:N2")
        total = [r for r in sheet.iter_rows(values_only=True) if r[0] == "Total"][0]
        self.assertEqual((total[11], total[13]), (3000, 3000))
        facts = self.figures(book)
        self.assertEqual(facts["List filters"], "Walk-in bookings only")
        self.assertEqual(facts["Rentals (gowns)"], 2)                       # the figures are never narrowed by the list filters
        self.assertEqual(facts["Rental income (PHP)"], 3000)
        self.assertEqual(facts["Printed by"], "Olive Owner")

    def test_the_summary_top_gowns_and_payments_match_the_page(self):
        booking = self.booking("Maria Santos", [self.gown("Pearl Gown")], pickup=1, ret=5)
        self.paid(booking, P.FULL, "5000")
        late = self.booking("Late Person", pickup=-9, ret=-6, stage=S.RETURNED, picked_up_on=self.today - timedelta(days=9), returned_on=self.today)
        self.paid(late, P.FULL, "5000", on=self.today)
        self.paid(late, P.DEPOSIT_RETURN, "800", direction=Payment.Direction.OUT)
        report = self.report()
        book = self.book_of()
        facts = self.figures(book)
        self.assertEqual(D(str(facts["Rental income (PHP)"])), report["income"])
        self.assertEqual(D(str(facts["Security deposits held now (PHP)"])), report["held"]["held"])
        self.assertEqual(D(str(facts["Security deposits returned in these dates (PHP)"])), D("800"))
        self.assertEqual(D(str(facts["Still to collect (PHP)"])), report["owing"])
        self.assertEqual(facts["Overdue"], 0)
        self.assertEqual(facts["Returned"], 1)
        gowns = self.sheet_rows(book["Top Gowns"], 5)
        self.assertEqual([(g[0], g[1], g[3]) for g in gowns], [(i, e["name"], e["times"]) for i, e in enumerate(report["gowns"], 1)])
        self.assertEqual(book["Top Gowns"]["G2"].value, "Wedding Gown")
        payments = self.sheet_rows(book["Payments"], 11)
        self.assertEqual(len(payments), 3)
        self.assertEqual(sum(D(str(p[6] or 0)) for p in payments), D("10000"))
        self.assertEqual(sum(D(str(p[7] or 0)) for p in payments), D("800"))
        self.assertEqual(sum(D(str(p[8] or 0)) for p in payments), report["income"])
        handed = [p for p in payments if p[7]][0]
        self.assertEqual(handed[8:11], (None, None, None))
        self.assertEqual([c.value for c in book["Payments"][1]][:8], ["Date", "Receipt no.", "Booking no.", "Customer", "For", "Paid by", "Received", "Handed back"])

    def test_a_name_that_looks_like_a_formula_stays_text(self):
        booking = self.booking("=HYPERLINK(\"http://x\",\"y\")", [self.gown("=1+1")], pickup=1, ret=5)
        self.paid(booking, P.FULL, "5000")
        response = self.download(excel_url())
        book = load_workbook(io.BytesIO(response.content))
        sheet = book["Rentals"]
        self.assertEqual((sheet["B2"].value, sheet["B2"].data_type), ('=HYPERLINK("http://x","y")', "s"))
        self.assertEqual((sheet["C2"].value, sheet["C2"].data_type), ("=1+1", "s"))
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            for name in archive.namelist():
                if name.startswith("xl/worksheets/"):
                    self.assertNotIn(b"<f>", archive.read(name), name)

    def test_odd_letters_never_break_either_file(self):
        booking = self.booking("Peña Niño \x0b \U0001F600 <b>& co", [self.gown("Güna ₱ \x07 Gown")], pickup=1, ret=5)
        self.paid(booking, P.FULL, "5000")
        book = self.book_of(q="peña")
        self.assertIn("Peña Niño", book["Rentals"]["B2"].value)
        self.assertTrue(self.download(pdf_url()).content.startswith(b"%PDF"))

    def test_empty_dates_make_complete_empty_files(self):
        book = self.book_of(period="today")
        self.assertEqual(book["Rentals"]["A2"].value, "No rentals in this period.")
        self.assertEqual(book["Payments"]["A2"].value, "No payments in these dates.")
        self.assertEqual(book["Top Gowns"]["A2"].value, "No gown was rented in these dates.")
        self.assertTrue(self.download(pdf_url(), period="today").content.startswith(b"%PDF"))

    def test_the_top_gown_filter_names_the_gown(self):
        self.booking("A", [self.gown("Pearl Gown")], pickup=1, ret=5)
        self.booking("B", [self.gown("Ruby Gown")], pickup=1, ret=5)
        book = self.book_of(gown="Wedding Gown|pearl gown")
        self.assertEqual([r[2] for r in self.sheet_rows(book["Rentals"], 14)], ["Pearl Gown"])
        self.assertEqual(self.figures(book)["List filters"], "Gown: Pearl Gown")


@skipUnless(pymupdf, "PyMuPDF is not installed")
class PdfTests(DownloadBase):
    def words(self, **params):
        document = pymupdf.open(stream=self.download(pdf_url(), **params).content, filetype="pdf")
        pages = [page.get_text() for page in document]
        document.close()
        return pages

    def test_the_pdf_says_what_the_page_says(self):
        booking = self.booking("Maria Santos", [self.gown("Pearl Gown")], pickup=1, ret=5)
        payment = self.paid(booking, P.FULL, "5000")
        [text] = self.words(period="month")
        for words in ("Reservation Report", f"{self.month_start:%B %Y}", "Pick-up date", "Printed by", "Olive Owner", "Rental income", "Deposits held",
                      "Rentals", "Still to collect", "Top rented gowns", "Rentals by category", "Pearl Gown", "Maria Santos", booking.reference_code,
                      "PHP 3,000.00", "PHP 2,000.00", payment.receipt_number, "Payments in these dates (1)", "Rental income counts the money on the day it was paid",
                      "Page 1 of 1"):
            self.assertIn(words, text.replace("\n", " "), words)
        self.assertNotIn("₱", text)

    def test_a_long_list_runs_over_several_numbered_pages_with_the_headings_repeated(self):
        for number in range(70):
            self.booking(f"Customer {number:02d}", [self.gown(f"Gown {number:02d}")], pickup=1, ret=5)
        pages = self.words()
        self.assertGreater(len(pages), 2)
        for index, text in enumerate(pages, 1):
            self.assertIn(f"Page {index} of {len(pages)}", text)
            self.assertIn("Reservation Report", text)
        listed = " ".join(pages)
        for number in range(70):
            self.assertIn(f"Customer {number:02d}", listed)
        self.assertGreaterEqual(sum("Booking no." in text for text in pages), 2)
        self.assertIn("Total (70 gowns)", listed.replace("\n", " "))
