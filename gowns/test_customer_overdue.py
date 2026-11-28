"""The customer's own pages say "Return Overdue" by themselves once a gown that is out passes its return date (asked for 2026-11-28).
Before this, they said "Currently Rented" unless someone had set the status to "Overdue" by hand, which no button does."""
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from arabela_admin.views import _days_late
from reservations.models import Reservation, ReservationItem

Stage = ReservationItem.Stage
Status = Reservation.Status


class CustomerOverdueTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = get_user_model().objects.create_user("od_cust", "od_cust@example.com", "x")

    def setUp(self):
        self.client.force_login(self.customer)
        self.today = timezone.localdate()

    def booking(self, *gowns, status=Status.CONFIRMED):
        """gowns: (days from today to the return date, stage) or a dict of ReservationItem fields on top of those."""
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Ana", status=status, rental_subtotal=Decimal("1000"), security_deposit=Decimal("2000"),
            total_amount=Decimal("3000"), late_fee_per_day=200, reviewed_at=timezone.now())
        for number, gown in enumerate(gowns, 1):
            ret, stage, *extra = gown
            back = self.today + timedelta(days=ret)
            values = {
                "gown_name": f"Gown {number}", "rental_price": Decimal("1000"), "rental_date": back - timedelta(days=4),
                "event_date": back - timedelta(days=2), "return_date": back, "stage": stage,
                "picked_up_on": None if stage == Stage.PICKUP else back - timedelta(days=4),
            }
            values.update(extra[0] if extra else {})
            ReservationItem.objects.create(reservation=reservation, **values)
        return reservation

    def list_page(self):
        return " ".join(self.client.get(reverse("gowns:orders")).content.decode().split())

    def cards(self, listing, key):
        """How many gown cards carry this status (the page's own style rules name the statuses too, so only the card attribute counts)."""
        return len(re.findall(rf'data-status-key="{key}"(?!\])', listing))

    def detail_page(self, item):
        return " ".join(self.client.get(reverse("gowns:reservation_item_detail", args=[item.pk])).content.decode().split())

    def assert_state(self, reservation, expected, days=0):
        """Both customer pages and the day count for a one-gown booking."""
        item = reservation.items.get()
        self.assertEqual(item.overdue_days_now, days)
        listing, detail = self.list_page(), self.detail_page(item)
        self.assertEqual(self.cards(listing, "overdue"), 1 if days else 0)
        for page in (listing, detail):
            self.assertIn(expected, page)
            self.assertEqual("Return Overdue" in page, bool(days))
        self.assertEqual('id="overdue-note"' in detail, bool(days))
        if days:
            for page in (listing, detail):
                self.assertNotIn("Currently Rented", page)

    def test_a_gown_past_its_return_date_is_overdue_without_anyone_setting_it(self):
        reservation = self.booking((-25, Stage.RESERVED))
        self.assert_state(reservation, "Return Overdue", days=25)
        item = reservation.items.get()
        due = f"{item.return_date:%b %d, %Y}"
        self.assertIn(f"Was due:</span> {due} &middot; 25 days overdue", self.list_page())
        detail = self.detail_page(item)
        self.assertIn("25 days overdue", detail)
        self.assertIn(f"It was due back on {due}.", detail)
        self.assertIn("overdue fee of PHP 200 a day", detail)

    def test_one_day_is_written_as_one_day(self):
        reservation = self.booking((-1, Stage.RESERVED))
        self.assert_state(reservation, "Return Overdue", days=1)
        self.assertIn("1 day overdue", self.list_page())
        self.assertNotIn("1 days overdue", self.list_page() + self.detail_page(reservation.items.get()))

    def test_no_fee_sentence_when_the_booking_has_no_overdue_fee(self):
        reservation = self.booking((-3, Stage.RESERVED))
        Reservation.objects.filter(pk=reservation.pk).update(late_fee_per_day=0)
        detail = self.detail_page(reservation.items.get())
        self.assertIn("3 days overdue", detail)
        self.assertNotIn("keeps growing until it is returned", detail)

    def test_due_today_or_later_is_still_currently_rented(self):
        for ret in (0, 1, 9):
            with self.subTest(ret=ret):
                Reservation.objects.all().delete()
                reservation = self.booking((ret, Stage.RESERVED))
                self.assert_state(reservation, "Currently Rented")
                self.assertIn("Return by:", self.list_page())

    def test_the_older_out_stages_follow_the_dates_too(self):
        for stage in (Stage.RETURN, Stage.OVERDUE):
            with self.subTest(stage=stage):
                Reservation.objects.all().delete()
                self.assert_state(self.booking((-2, stage)), "Return Overdue", days=2)

    def test_marked_overdue_by_hand_before_the_date_is_simply_still_out_like_in_the_admin(self):
        reservation = self.booking((3, Stage.OVERDUE))
        self.assert_state(reservation, "Currently Rented")
        self.assertIn("Return by:", self.list_page())
        self.assertNotIn("Pick-up Date:", self.list_page())

    def test_a_gown_never_picked_up_is_not_overdue(self):
        self.assert_state(self.booking((-5, Stage.PICKUP)), "Awaiting Pick-up")

    def test_a_gown_that_came_back_late_is_completed_not_overdue(self):
        reservation = self.booking((-8, Stage.RETURNED, {"returned_on": self.today - timedelta(days=2)}))
        self.assert_state(reservation, "Reservation Completed")

    def test_cancelled_rejected_and_pending_bookings_are_never_overdue(self):
        for status, words in ((Status.CANCELLED, "Reservation Cancelled"), (Status.REJECTED, "Reservation Rejected"), (Status.PENDING, "Reservation Pending")):
            with self.subTest(status=status):
                Reservation.objects.all().delete()
                self.assert_state(self.booking((-5, Stage.RESERVED), status=status), words)

    def test_a_cancelled_gown_is_not_overdue(self):
        self.assert_state(self.booking((-5, Stage.CANCELLED)), "Gown Cancelled")

    def test_only_the_late_gown_of_a_booking_turns_red(self):
        reservation = self.booking((-4, Stage.RESERVED), (3, Stage.RESERVED), (-4, Stage.RETURNED, {"returned_on": self.today - timedelta(days=4)}))
        late, out, back = reservation.items.order_by("gown_name")
        listing = self.list_page()
        self.assertEqual(self.cards(listing, "overdue"), 1)
        self.assertEqual(self.cards(listing, "rented"), 1)
        self.assertEqual(self.cards(listing, "returned"), 1)
        self.assertIn("4 days overdue", self.detail_page(late))
        for other in (out, back):
            page = self.detail_page(other)
            self.assertNotIn("Return Overdue", page)
            self.assertNotIn('id="overdue-note"', page)

    def test_the_customer_sees_the_same_number_of_days_as_the_admin(self):
        today = self.today
        cases = [
            (-6, {}),
            # a free-days booking
            (-6, {"free_pickup_date": today - timedelta(days=10), "free_return_date": today - timedelta(days=6),
                  "original_return_date": today - timedelta(days=6)}),
            # an older booking whose return the shop moved later: counted from the new date
            (-2, {"original_return_date": today - timedelta(days=9)}),
        ]
        for ret, extra in cases:
            with self.subTest(ret=ret, extra=sorted(extra)):
                item = self.booking((ret, Stage.RESERVED, extra)).items.get()
                self.assertEqual(item.overdue_days_now, -ret)
                self.assertEqual(item.overdue_days_now, _days_late(item, today))

    def test_asking_costs_no_extra_database_query_on_the_list(self):
        self.booking((-4, Stage.RESERVED), (3, Stage.RESERVED))
        self.booking((-1, Stage.RESERVED))
        loaded = list(self.customer.reservations.prefetch_related("items"))
        with self.assertNumQueries(0):
            days = sorted(item.overdue_days_now for reservation in loaded for item in reservation.items.all())
        self.assertEqual(days, [0, 1, 4])
