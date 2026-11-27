"""The admin side of the free-days rule: staff may move a saved return back (never before the free days end), the fee follows the saved dates,
the third change of one gown's return tells the owner, the walk-in form says what taking a gown early costs, and the pages show the numbers.

Dates are offsets from today: event day 20 (free: day 18 to day 22), ₱200 per day per gown."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse

from accounts.models import AuditLog, UserProfile
from arabela_admin.notifications import build_items
from arabela_admin.test_date_changes import STAGE, DateBase
from arabela_admin.test_new_booking import Base as NewBookingBase, make_gown as make_wedding_gown
from gowns.models import SiteSettings
from reservations import ledger
from reservations.models import Payment, Reservation, ReservationItem, ReservationStatusEvent
from reservations.test_ledger import pay

User = get_user_model()
D = Decimal


class FreeDaysDateBase(DateBase):
    EVENT = 20

    def booked(self, pickup=16, ret=24, **extra):
        """A free-days booking: by default 2 days early and 2 days later than the free days (₱800)."""
        extra.setdefault("original_rental_date", self.day(pickup))
        extra.setdefault("original_return_date", self.day(ret))
        extra.setdefault("event_date", self.day(self.EVENT))
        return self.item(pickup, ret, free_pickup_date=self.day(self.EVENT - 2), free_return_date=self.day(self.EVENT + 2), rest=True, **extra)

    def fee(self, item):
        return ledger.statement(Reservation.objects.get(pk=item.reservation_id), self.today).late_fee


class MoveTheReturnTests(FreeDaysDateBase):
    def test_a_return_saved_too_late_can_go_back_to_the_free_day_and_the_fee_goes_down(self):
        it = self.booked()
        self.assertEqual(self.fee(it), D("800.00"))
        response = self.change(it, ret=self.day(22))
        self.assertEqual(response.status_code, 200, response.content)
        it.refresh_from_db()
        self.assertEqual((it.return_date, it.original_return_date, it.planned_return_date), (self.day(22), self.day(24), None))
        self.assertEqual(self.fee(it), D("400.00"))
        self.assertEqual([(c["from"], c["to"]) for c in it.return_changes], [(self.day(24).isoformat(), self.day(22).isoformat())])
        self.assertTrue(it.return_changes[0]["by"])
        rest = self.rest_block(it)
        self.assertEqual((rest.start_date, rest.end_date), (self.day(23), self.day(27)))          # the cleaning days move back with it
        line = ReservationStatusEvent.objects.filter(item=it, label__endswith="return date changed").latest("id")
        self.assertIn("Extra days now: 2 × ₱200 = ₱400", line.detail)
        self.assertIn("2 days earlier than before", line.detail)

    def test_moving_later_again_after_it_was_moved_back_charges_up_to_the_booked_day_as_later_return(self):
        it = self.booked()
        self.change(it, ret=self.day(22))
        self.assertEqual(self.change(it, ret=self.day(27)).status_code, 200)
        s = ledger.statement(Reservation.objects.get(pk=it.reservation_id), self.today)
        self.assertEqual((s.early_days_total, s.later_days_total, s.change_days_total, s.late_fee), (2, 2, 3, D("1400.00")))

    def test_the_return_never_goes_before_the_free_days_end_or_before_today(self):
        it = self.booked()
        response = self.change(it, ret=self.day(21))
        self.assertEqual(response.status_code, 400)
        self.assertIn("returning by then is free", response.json()["error"])
        for offset, words in ((-1, "already passed"), (24, "already on"), (800, "too far")):
            with self.subTest(offset=offset):
                response = self.change(it, ret=self.day(offset))
                self.assertEqual(response.status_code, 400)
                self.assertIn(words, response.json()["error"])
        it.refresh_from_db()
        self.assertEqual((it.return_date, it.return_changes), (self.day(24), []))

    def test_an_older_booking_goes_back_only_to_the_day_first_booked(self):
        it = self.item(10, 14, rest=True)
        self.assertEqual(self.change(it, ret=self.day(17)).status_code, 200)
        self.assertEqual(self.change(it, ret=self.day(15)).status_code, 200)
        response = self.change(it, ret=self.day(13))
        self.assertEqual(response.status_code, 400)
        self.assertIn("first booked", response.json()["error"])
        self.assertEqual(self.change(it, ret=self.day(14)).status_code, 200)
        self.assertEqual(self.fee(it), D("0.00"))

    def test_the_pick_up_moved_earlier_costs_and_moved_back_to_the_free_day_costs_nothing(self):
        it = self.booked(pickup=18, ret=22)
        self.assertEqual(self.change(it, self.day(15)).status_code, 200)
        self.assertEqual(self.fee(it), D("600.00"))
        self.assertEqual(self.change(it, self.day(18)).status_code, 200)
        self.assertEqual(self.fee(it), D("0.00"))
        line = ReservationStatusEvent.objects.filter(item=it, label__endswith="pick-up date changed").latest("id")
        self.assertIn("No extra days", line.detail)

    def test_the_new_return_still_may_not_run_into_the_next_booking(self):
        it = self.booked()
        gown = it.gown
        self.item(35, 39, gown=gown, name="Next Customer")
        response = self.change(it, ret=self.day(30))                                    # 5 cleaning days would reach day 35
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.change(it, ret=self.day(29)).status_code, 200)


class OwnerSeesTheThirdChangeTests(FreeDaysDateBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.owner = User.objects.create_superuser("fd_owner", "fd.owner@gmail.com", "x")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.STAFF})
        cls.staff.first_name, cls.staff.last_name = "Maria", "Cruz"
        cls.staff.save(update_fields=["first_name", "last_name"])

    def lines(self):
        return list(AuditLog.objects.filter(action__in=("item.change_dates", "item.change_return")).order_by("id"))

    def bell(self, user):
        return [n for n in build_items(user) if "return date" in str(n.get("text", ""))]

    def test_the_third_change_is_marked_important_names_who_and_rings_the_owner_only(self):
        it = self.booked()
        for offset in (25, 26):
            self.assertEqual(self.change(it, ret=self.day(offset)).status_code, 200)
        self.assertEqual([line.sensitive for line in self.lines()], [False, False])
        self.assertEqual(self.bell(self.owner), [])
        self.assertEqual(self.change(it, ret=self.day(24)).status_code, 200)
        third = self.lines()[-1]
        self.assertTrue(third.sensitive)
        self.assertIn("for the 3rd time", third.summary)
        self.assertEqual(third.summary.count("by Maria Cruz"), 3)
        self.assertEqual(len(third.details["return date changes"]), 3)
        notes = self.bell(self.owner)
        self.assertEqual(len(notes), 1)
        self.assertIn("3rd time", notes[0]["text"])
        self.assertEqual(self.bell(self.staff), [])                                      # staff are not told; they are not blocked either
        self.assertEqual(self.change(it, ret=self.day(23)).status_code, 200)
        self.assertIn("for the 4th time", self.lines()[-1].summary)

    def test_the_owners_own_changes_do_not_ring_the_owner(self):
        it = self.booked()
        self.client.force_login(self.owner)
        for offset in (25, 26, 27):
            self.assertEqual(self.change(it, ret=self.day(offset)).status_code, 200)
        self.assertTrue(self.lines()[-1].sensitive)
        self.assertEqual(self.bell(self.owner), [])

    def test_changing_the_pick_up_many_times_tells_nobody(self):
        it = self.booked()
        for offset in (15, 14, 13, 12):
            self.assertEqual(self.change(it, self.day(offset)).status_code, 200)
        self.assertFalse(any(line.sensitive for line in self.lines()))

    def test_a_refused_change_is_not_counted(self):
        it = self.booked()
        self.change(it, ret=self.day(25))
        self.change(it, ret=self.day(26))
        self.assertEqual(self.change(it, ret=self.day(21)).status_code, 400)
        it.refresh_from_db()
        self.assertEqual(len(it.return_changes), 2)
        self.assertFalse(any(line.sensitive for line in self.lines() if line.outcome == "ok"))


class ActiveReservationsShowsTheExtraDaysTests(FreeDaysDateBase):
    def test_the_page_shows_the_early_and_later_days_and_what_the_calendar_needs(self):
        it = self.booked()
        response = self.client.get(reverse("arabela_admin:active_reservations"))
        self.assertEqual(response.status_code, 200)
        row = next(r for r in response.context["reservations"] if r.pk == it.reservation_id)
        shown = row.items.all()[0]
        self.assertEqual((shown.early_days_planned, shown.later_days_planned, shown.change_days_planned, shown.late_fee_planned), (2, 2, 0, D("800")))
        self.assertEqual((row.extra_days_fee_planned, row.extra_days_planned, shown.return_floor), (D("800"), 4, self.day(22)))
        html = response.content.decode()
        self.assertIn(f'data-free-return="{self.day(22):%Y-%m-%d}"', html)
        self.assertIn(f'data-free-pickup="{self.day(18):%Y-%m-%d}"', html)
        self.assertIn(f'data-return-floor="{self.day(22):%Y-%m-%d}"', html)
        self.assertIn("Extra days", html)

    def test_security_deposits_and_payments_take_the_same_amount_off(self):
        it = self.booked(pickup=-10, ret=-4, event_date=None)
        ReservationItem.objects.filter(pk=it.pk).update(
            event_date=self.day(-8), free_pickup_date=self.day(-10), free_return_date=self.day(-6),
            stage=STAGE.RETURNED, picked_up_on=self.day(-10), returned_on=self.day(-4))
        booking = Reservation.objects.get(pk=it.reservation_id)
        pay(booking, Payment.Purpose.FULL, "5000")
        s = ledger.statement(Reservation.objects.get(pk=booking.pk), self.today)
        self.assertEqual((s.later_days_total, s.late_fee, s.refund_available), (2, D("400.00"), D("1600.00")))
        owner = User.objects.create_superuser("fd_owner2", "fd.owner2@gmail.com", "x")
        self.client.force_login(owner)
        page = self.client.get(reverse("arabela_admin:security_deposits")).content.decode()
        self.assertIn("1,600.00", page)
        data = self.client.get(reverse("arabela_admin:payments")).content.decode()
        self.assertIn("1600.00", data.replace(",", ""))


class WalkInTakingEarlyTests(NewBookingBase):
    def check(self, gown, event, takes_now=True):
        params = {"gown": gown.id, "event": event.isoformat()}
        if takes_now:
            params["takes_now"] = "1"
        return self.client.get(reverse("arabela_admin:availability_check"), data=params).json()

    def test_taking_it_before_the_free_days_says_what_it_costs(self):
        gown = make_wedding_gown(301)
        SiteSettings.objects.update_or_create(pk=1, defaults={"late_fee_per_day": 200})
        data = self.check(gown, self.today + timedelta(days=10))
        self.assertTrue(data["ok"], data)
        self.assertEqual((data["extraDays"], data["extraFee"]), (8, 1600))
        self.assertIn("Early pick-up fee: 8 days × ₱200 = ₱1,600", data["extraText"])
        data = self.check(gown, self.today + timedelta(days=2))
        self.assertEqual((data["extraDays"], data["extraText"]), (0, ""))
        data = self.check(gown, self.today + timedelta(days=10), takes_now=False)
        self.assertEqual(data["extraDays"], 0)

    def test_the_saved_walk_in_keeps_the_free_days_and_the_early_fee(self):
        gown = make_wedding_gown(302)
        event = self.today + timedelta(days=7)
        response = self.post_booking([self.line(gown, event=event, takes_now=True)])
        self.assertEqual(response.status_code, 200, response.content)
        item = ReservationItem.objects.get(gown=gown)
        self.assertEqual((item.rental_date, item.event_date, item.return_date), (self.today, event, event + timedelta(days=2)))
        self.assertEqual((item.free_pickup_date, item.free_return_date), (event - timedelta(days=2), event + timedelta(days=2)))
        s = ledger.statement(item.reservation, self.today)
        self.assertEqual((s.early_days_total, s.early_fee), (5, D("1000.00")))

    def test_a_walk_in_for_a_later_day_has_no_extra_days(self):
        gown = make_wedding_gown(303)
        response = self.post_booking([self.line(gown)])
        self.assertEqual(response.status_code, 200, response.content)
        item = ReservationItem.objects.get(gown=gown)
        self.assertEqual(ledger.booked_extra_days(item), (0, 0, 0))
        self.assertTrue(item.uses_free_days)
