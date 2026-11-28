"""The calmer Payments list (asked for 2026-11-28): one status label per booking, a Fees column, "paid of total" in one column, and the
"On this date" bar with status numbers that follow the chosen date. The filtering itself runs in the browser; here: what the page gives it."""
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from reservations.models import Payment, Reservation, ReservationItem
from reservations.test_ledger import make_booking, pay

User = get_user_model()
P = Payment.Purpose
Stage = ReservationItem.Stage
PAYMENTS = reverse("arabela_admin:payments")


def iso(offset):
    return (timezone.localdate() + timedelta(days=offset)).isoformat()


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("pf_owner", "pf.owner@gmail.com", "x", first_name="Olive", last_name="Owner")
        UserProfile.objects.update_or_create(user=cls.owner, defaults={"role": UserProfile.Role.OWNER})

    def setUp(self):
        self.client.force_login(self.owner)

    def page(self, url=PAYMENTS):
        return self.client.get(url).content.decode()

    def row(self, html, booking):
        """One booking's row: from its opening tag to the next row (or the end of the list)."""
        start = html.index(f'data-booking-id="{booking.pk}"')
        end = html.find('class="ar-booking pm-row"', start)
        return html[start:end if end > 0 else html.index("shownCount() === 0", start)]

    def mark(self, row, name):
        return re.search(rf'data-{name}="([^"]*)"', row).group(1).split()

    def fees(self, row):
        """(the Fees cell's words, its hover text)."""
        cell = row[row.index('class="pm-fees"'):row.index('class="pm-stat"')]
        title = re.search(r'title="([^"]*)"', cell)
        words = re.sub(r"<[^>]+>", " ", cell[cell.index(">") + 1:]).replace("&middot;", "·").replace("&#8369;", "₱").replace("&mdash;", "—")
        return " ".join(words.replace("<div", "").split()), title.group(1) if title else ""


class TheCalmerRowTests(Base):
    def test_one_status_label_per_booking_and_the_fees_in_their_own_column(self):
        booking = make_booking(stage=Stage.RESERVED, pickup=-12, ret=-2, original_return=-9)       # moved 7 days, then 2 days overdue
        pay(booking, P.FULL, "5500")
        row = self.row(self.page(), booking)
        self.assertEqual(len(re.findall(r'class="pm-chip ', row)), 1)
        words, title = self.fees(row)
        self.assertEqual(words, "₱1,800.00 Overdue · Date change")
        self.assertEqual(title, "Overdue ₱400.00 so far · Date change ₱1,400.00")
        self.assertIn('<span class="pm-fee-late">Overdue</span>', row)
        self.assertIn("<span>Date change</span>", row)

    def test_no_fee_is_a_dash(self):
        booking = make_booking(stage=Stage.RESERVED, pickup=-1, ret=3)
        words, title = self.fees(self.row(self.page(), booking))
        self.assertEqual((words, title), ("—", ""))

    def test_a_gown_that_came_back_late_still_shows_its_fee_in_grey(self):
        booking = make_booking(stage=Stage.RETURNED, pickup=-12, ret=-6, returned_on=-3)
        pay(booking, P.FULL, "5500")
        row = self.row(self.page(), booking)
        self.assertEqual(self.fees(row), ("₱600.00 Overdue", "Overdue ₱600.00"))
        self.assertNotIn("pm-fee-late", row)

    def test_three_kinds_of_fee_stay_on_two_lines(self):
        today = timezone.localdate()
        # picked up 2 days before the free days, booked 2 days past them, moved 3 more, and 2 days overdue: 9 days x 200
        booking = make_booking(stage=Stage.RESERVED, pickup=-12, ret=-2, original_return=-5)
        booking.items.update(free_pickup_date=today - timedelta(days=10), free_return_date=today - timedelta(days=7))
        words, title = self.fees(self.row(self.page(), booking))
        self.assertEqual(words, "₱1,800.00 Overdue · +2 more")
        self.assertEqual(title, "Overdue ₱400.00 so far · Date change ₱600.00 · Extra days ₱800.00")

    def test_paid_and_total_share_one_column(self):
        booking = make_booking()
        pay(booking, P.DEPOSIT, "2000")
        html = self.page()
        row = self.row(html, booking)
        self.assertIn('data-label="Paid">&#8369;2,000.00<span class="pm-of">of &#8369;5,500.00</span></div>', row)
        head = html[html.index('<div class="pm-head">'):html.index('data-booking-id="')]
        self.assertEqual(re.findall(r"<span[^>]*>([^<]*)</span>", head),
                         ["Customer", "Booking no.", "Paid", "Balance", "Deposit", "Fees", "", "Status", ""])     # "": the money choice's column
        self.assertNotIn('data-label="Total"', html)

    def test_a_cancelled_bookings_deposit_words_take_two_short_lines(self):
        booking = make_booking(status=Reservation.Status.CANCELLED, refunds_rental=True)
        pay(booking, P.FULL, "5500")
        row = self.row(self.page(), booking)
        self.assertIn('title="Deposit kept (cancelled), give back ₱3,500.00"><span class="pm-dep-main">Give back ₱3,500.00</span>'
                      '<span class="pm-of">Kept (cancelled)</span></div>', row)

    def test_the_gown_name_is_shortened_but_its_dates_are_always_there(self):
        booking = make_booking(gowns=2)
        row = self.row(self.page(), booking)
        self.assertIn('<span class="pm-sub-g" title="Wedding Gown 1 + 1 more">Wedding Gown 1 + 1 more</span><span class="pm-sub-d">', row)


class TheDateMarkTests(Base):
    def test_each_booking_carries_its_gowns_days(self):
        booking = make_booking(stage=Stage.RETURNED, pickup=-8, ret=-4, returned_on=-3)
        row = self.row(self.page(), booking)
        self.assertEqual(self.mark(row, "d-pickup"), [iso(-8)])
        self.assertEqual(self.mark(row, "d-return"), [iso(-4), iso(-3)])               # the day it was due and the day it came back
        self.assertEqual(self.mark(row, "d-event"), [iso(-6)])
        self.assertEqual(self.mark(row, "late-return"), [])                            # it is back: not overdue any more

    def test_overdue_marks_hold_only_the_gowns_that_are_overdue(self):
        booking = make_booking(gowns=2, stage=Stage.RESERVED, pickup=-8, ret=-4)
        later = booking.items.order_by("id").last()
        ReservationItem.objects.filter(pk=later.pk).update(rental_date=timezone.localdate() - timedelta(days=2),
                                                           return_date=timezone.localdate() + timedelta(days=3),
                                                           original_return_date=timezone.localdate() + timedelta(days=3))
        row = self.row(self.page(), booking)
        self.assertEqual(self.mark(row, "d-return"), [iso(-4), iso(3)])
        self.assertEqual(self.mark(row, "late-return"), [iso(-4)])
        self.assertEqual(self.mark(row, "late-pickup"), [iso(-8)])
        self.assertIn('data-late="1"', row)

    def test_a_cancelled_gown_and_a_cancelled_booking_are_found_by_no_date(self):
        part = make_booking(gowns=2, pickup=2, ret=6)
        gone = part.items.order_by("id").last()
        ReservationItem.objects.filter(pk=gone.pk).update(stage=Stage.CANCELLED, rental_date=timezone.localdate() + timedelta(days=20),
                                                          return_date=timezone.localdate() + timedelta(days=24))
        cancelled = make_booking(status=Reservation.Status.CANCELLED, pickup=2, ret=6)
        html = self.page()
        self.assertEqual(self.mark(self.row(html, part), "d-pickup"), [iso(2)])
        for name in ("d-pickup", "d-return", "d-event", "late-return"):
            self.assertEqual(self.mark(self.row(html, cancelled), name), [], name)

    def test_still_to_collect_adds_up_to_the_centavo_and_follows_the_dates(self):
        make_booking(rental="3500.50", pickup=2, ret=6)
        paid = make_booking(rental="1200.25", pickup=10, ret=14)
        pay(paid, P.DEPOSIT, "2000")
        box = self.client.get(PAYMENTS).context["money"]
        self.assertEqual((box["owing"], box["owing_bookings"]), ("6,700.75", 2))          # 5,500.50 + 1,200.25
        one = self.client.get(PAYMENTS, {"from": iso(6), "to": iso(6), "by": "return"}).context["money"]
        self.assertEqual((one["owing"], one["owing_bookings"]), ("5,500.50", 1))


class TheDateBarTests(Base):
    def test_the_bookings_tab_has_the_date_bar_and_the_three_boxes(self):
        make_booking()
        html = self.page()
        self.assertEqual(html.count('class="md-bar"'), 1)
        self.assertIn("search: '', statusFilter: 'All', dateFrom: '', dateTo: '', datePick: 'any', dateKind: 'return',", html)
        self.assertIn("initFilters();", html)
        self.assertEqual(re.findall(r'data-box="([a-z]+)"', html), ["income", "held", "owing"])
        for words in ('x-text="count(\'All\')"', 'x-text="count(\'balance\')"', 'x-text="count(\'late\')"', 'x-text="count(\'refund\')"'):
            self.assertIn(words, html)

    def test_the_other_tabs_have_neither(self):
        make_booking()
        for name in ("payment_receipts", "payment_online"):
            with self.subTest(name=name):
                html = self.page(reverse(f"arabela_admin:{name}"))
                self.assertNotIn('class="md-bar"', html)
                self.assertNotIn("data-money-boxes", html)
                self.assertNotIn("initFilters();", html)

    def test_nothing_to_filter_yet_says_only_that(self):
        html = self.page()
        self.assertIn("nothing to collect or hand back", html)
        self.assertNotIn("No booking in this list", html)
