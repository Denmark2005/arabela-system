"""Changing a gown's pick-up and return dates in Active Reservations (arabela_admin/views._change_item_dates).

What the shop decided:
  * The pick-up can move EARLIER or LATER (today up to 60 days ahead); a pick-up after the current return date needs the new return date in the same request, and both
    are saved together or not at all. The return can only move later.
  * The customer's event day stays as they chose it, except when it is no longer between the pick-up and the return: a pick-up moved past it comes with the new
    event day (in the same request), and a booking already like that can be given one on its own.
  * The late fee (₱200 per gown per day) always counts the days after the ORIGINAL return date. Neither the pick-up nor the event date changes it, and early pick-up is free.
  * Never a double booking: the new days must be free of every other booking and block, and when the RETURN moves, the gown's rest days after it (5) must stay clear of a
    later booking; the gown's rest block moves with the return unless an admin released it, which is respected.
  * Active Reservations shows the fee: a chip on the booking, a labeled line in the gown, the cost in the date window, all linking to Payments.
Day numbers below are offsets from today; in the shop's example the original booking is pick-up Nov 25 (day 10), event Nov 27 (day 12), return Nov 29 (day 14)."""
import json
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditLog
from arabela_admin import views as views_module
from gowns.models import COOLDOWN_DAYS, Gown, GownUnavailability
from reservations import ledger
from reservations.models import Payment, Reservation, ReservationItem, ReservationStatusEvent
from reservations.test_ledger import pay

User = get_user_model()
D = Decimal
STAGE = ReservationItem.Stage


def make_gown(n):
    return Gown.objects.create(
        gown_id=f"DATECHG-{n:04d}", name=f"Date Gown {n}", category=Gown.Category.GUEST_GOWN, color_name="Red", color_code="RD",
        size=Gown.Size.MEDIUM, rental_price=D("3000.00"), status=Gown.Status.AVAILABLE,
    )


class DateBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="dc_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="dc_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)
        self.today = timezone.localdate()
        self.count = 0

    def day(self, offset):
        return self.today + timedelta(days=offset)

    def gown(self):
        self.count += 1
        return make_gown(self.count)

    def item(self, start, end, *, gown=None, stage=STAGE.PICKUP, rest=False, name="Date Customer", **extra):
        """One booking with one gown (a fresh gown unless one is given), dates as day offsets. rest=True gives it its rest block (the cooldown after the return)."""
        gown = gown or self.gown()
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name=name, status=Reservation.Status.CONFIRMED, phone="09171234567", late_fee_per_day=200,
            security_deposit=D("2000"), rental_subtotal=D("3000"), total_amount=D("5000"), reviewed_at=timezone.now(),
        )
        item = ReservationItem.objects.create(
            reservation=reservation, gown=gown, gown_name=gown.name, stage=stage, rental_price=D("3000"),
            rental_date=self.day(start), return_date=self.day(end), **extra,
        )
        if rest:
            GownUnavailability.objects.create(
                gown=gown, start_date=self.day(end + 1), end_date=self.day(end + COOLDOWN_DAYS), reason=GownUnavailability.Reason.COOLDOWN, auto_for_item=item,
            )
        return item

    def change(self, item, pickup=None, ret=None, client=None, event=None):
        body = {}
        if pickup is not None:
            body["pickup"] = pickup.isoformat()
        if ret is not None:
            body["return"] = ret.isoformat()
        if event is not None:
            body["event"] = event.isoformat()
        return (client or self.client).post(reverse("arabela_admin:reservation_item_change_dates", args=[item.id]), data=json.dumps(body), content_type="application/json")

    def rest_block(self, item):
        return GownUnavailability.objects.filter(auto_for_item=item).first()


# ---------------------------------------------------------------- the fee
class FeeScenarioTests(DateBase):
    # (new pick-up day, new return day, days after the original return day 14): the table agreed with the shop
    SCENARIOS = [
        (10, 14, 0), (11, 14, 0), (12, 14, 0), (13, 14, 0),            # a later pick-up inside the original window: nothing to pay
        (13, 15, 1), (12, 15, 1), (12, 16, 2), (13, 16, 2), (10, 16, 2),
        (10, 20, 6), (13, 20, 6),                                       # a later pick-up never lowers the fee
        (9, 14, 0), (9, 15, 1),                                         # an earlier pick-up is free
        (16, 17, 3), (16, 19, 5), (18, 21, 7),                          # a pick-up after the original return date: still counted from day 14
    ]

    def test_the_fee_counts_only_the_days_after_the_original_return_date(self):
        for pickup, ret, days in self.SCENARIOS:
            with self.subTest(pickup=pickup, ret=ret):
                it = self.item(10, 14, event_date=self.day(12))
                event = pickup if pickup > 12 else None                                    # past the event day: it moves too (to the new pick-up day here)
                if (pickup, ret) != (10, 14):
                    response = self.change(it, self.day(pickup) if pickup != 10 else None, self.day(ret) if ret != 14 else None,
                                           event=self.day(event) if event else None)
                    self.assertEqual(response.status_code, 200, response.content)
                it.refresh_from_db()
                self.assertEqual(it.original_return_date or it.return_date, self.day(14))
                self.assertEqual(ledger.planned_late_days(it, self.today), days)           # the event day never changes the fee
                self.assertEqual(it.effective_event_date, self.day(event or 12))           # otherwise the customer's own event day never moves
                # the preview and the real fee agree: once it comes back on its planned return day, the Payments statement says the same
                ReservationItem.objects.filter(pk=it.pk).update(stage=STAGE.RETURNED, returned_on=it.return_date)
                it.refresh_from_db()
                self.assertEqual(ledger.late_days(it, self.today), days)
                booking = Reservation.objects.get(pk=it.reservation_id)
                self.assertEqual(ledger.statement(booking, self.today).late_fee, D(days * 200))

    def test_moving_only_the_pick_up_never_changes_the_fee(self):
        it = self.item(10, 14)
        self.assertEqual(self.change(it, None, self.day(20)).status_code, 200)
        before = ledger.planned_late_days(ReservationItem.objects.get(pk=it.pk), self.today)
        self.assertEqual(before, 6)
        for pickup, event in ((12, None), (18, 18), (9, None)):
            with self.subTest(pickup=pickup):
                self.assertEqual(self.change(it, self.day(pickup), event=self.day(event) if event else None).status_code, 200)
                self.assertEqual(ledger.planned_late_days(ReservationItem.objects.get(pk=it.pk), self.today), 6)

    def test_the_planned_fee_follows_the_gowns_stage(self):
        out = self.item(-6, 3, stage=STAGE.RESERVED, picked_up_on=self.day(-6), original_return_date=self.day(0))
        self.assertEqual(ledger.planned_late_days(out, self.today), 3)                        # out, due back in 3 days: planned up to that day
        late = self.item(-8, -2, stage=STAGE.RESERVED, picked_up_on=self.day(-8), original_return_date=self.day(-4))
        self.assertEqual(ledger.planned_late_days(late, self.today), 4)                       # out and past its return day: counted up to today
        self.assertEqual(ledger.late_days(late, self.today), 4)                               # (what Payments says today)
        waiting = self.item(5, 12, original_return_date=self.day(9))
        self.assertEqual(ledger.planned_late_days(waiting, self.today), 3)                    # not out yet, return moved later: planned
        self.assertEqual(ledger.late_days(waiting, self.today), 0)                            # (nothing has happened yet)
        back = self.item(-8, -2, stage=STAGE.RETURNED, picked_up_on=self.day(-8), returned_on=self.day(-1), original_return_date=self.day(-4))
        self.assertEqual(ledger.planned_late_days(back, self.today), 3)                       # back: exactly what late_days says
        cancelled = self.item(5, 12, stage=STAGE.CANCELLED, original_return_date=self.day(9))
        self.assertEqual(ledger.planned_late_days(cancelled, self.today), 0)
        self.assertEqual(ledger.planned_late_days(self.item(5, 9), self.today), 0)            # never changed: nothing late


# ---------------------------------------------------------------- the rules of a change
class DateRuleTests(DateBase):
    def test_a_pick_up_can_move_up_to_60_days_ahead_and_not_beyond(self):
        it = self.item(10, 100)
        response = self.change(it, self.day(61))
        self.assertEqual(response.status_code, 400)
        self.assertIn("up to 60 days from today", response.json()["error"])
        self.assertEqual(self.change(it, self.day(60)).status_code, 200)
        it.refresh_from_db()
        self.assertEqual((it.rental_date, it.original_rental_date), (self.day(60), self.day(10)))

    def test_a_pick_up_after_the_return_date_needs_the_return_too_and_both_save_together(self):
        it = self.item(10, 14)
        refused = self.change(it, self.day(16))
        self.assertEqual(refused.status_code, 400)
        self.assertIn("Choose the new return date too", refused.json()["error"])
        it.refresh_from_db()
        self.assertEqual((it.rental_date, it.return_date), (self.day(10), self.day(14)))
        no_event = self.change(it, self.day(16), self.day(17))
        self.assertEqual(no_event.status_code, 400)
        self.assertIn("Choose the new event date too", no_event.json()["error"])
        it.refresh_from_db()
        self.assertEqual((it.rental_date, it.return_date), (self.day(10), self.day(14)))
        response = self.change(it, self.day(16), self.day(17), event=self.day(16))
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body["rental_date"], body["return_date"], body["event_date"], body["days_early"], body["days_late"]),
                         (self.day(16).isoformat(), self.day(17).isoformat(), self.day(16).isoformat(), -6, 3))
        it.refresh_from_db()
        self.assertEqual((it.rental_date, it.return_date, it.original_rental_date, it.original_return_date), (self.day(16), self.day(17), self.day(10), self.day(14)))
        self.assertEqual(it.event_date, self.day(16))

    def test_the_return_cannot_be_before_the_pick_up_and_a_refused_change_saves_nothing(self):
        it = self.item(10, 14)
        response = self.change(it, self.day(20), self.day(19))
        self.assertEqual(response.status_code, 400)
        self.assertIn("can't be before the pick-up", response.json()["error"])
        it.refresh_from_db()
        self.assertEqual((it.rental_date, it.return_date, it.original_rental_date, it.original_return_date, it.event_date), (self.day(10), self.day(14), None, None, None))
        for ret in (14, 12):                                                                     # no early return, even together with a later pick-up
            with self.subTest(ret=ret):
                self.assertEqual(self.change(it, self.day(12), self.day(ret)).status_code, 400)
        it.refresh_from_db()
        self.assertEqual(it.rental_date, self.day(10))

    def test_a_pick_up_change_freezes_the_event_date_so_it_cannot_drift(self):
        it = self.item(10, 14)                                                                  # event never saved: worked out from the dates (return - 2 = day 12)
        self.assertEqual(it.effective_event_date, self.day(12))
        self.assertEqual(self.change(it, self.day(11), self.day(20)).status_code, 200)
        it.refresh_from_db()
        self.assertEqual(it.effective_event_date, self.day(12))                                  # it would have drifted to day 18 if it had stayed worked out

    def test_a_gown_that_is_out_cannot_change_its_pick_up_but_can_still_extend_its_return(self):
        out = self.item(-2, 3, stage=STAGE.RESERVED, picked_up_on=self.day(-2))
        response = self.change(out, self.day(1), self.day(6))
        self.assertEqual(response.status_code, 400)
        self.assertIn("already been picked up", response.json()["error"])
        self.assertEqual(self.change(out, None, self.day(6)).status_code, 200)
        returned = self.item(-9, -5, stage=STAGE.RETURNED, picked_up_on=self.day(-9), returned_on=self.day(-5))
        self.assertEqual(self.change(returned, None, self.day(3)).status_code, 400)

    def test_every_change_is_written_to_the_booking_history_and_the_audit_log(self):
        it = self.item(10, 14)
        self.assertEqual(self.change(it, self.day(16), self.day(17), event=self.day(17)).status_code, 200)
        details = list(ReservationStatusEvent.objects.filter(reservation=it.reservation, item=it).order_by("id").values_list("detail", flat=True))
        self.assertEqual(len(details), 3)
        self.assertIn("Pick-up moved back from", details[0])
        self.assertIn("6 days later than originally booked", details[0])
        self.assertIn("Return moved from", details[1])
        self.assertEqual(details[2], f"Event date moved from {self.day(12):%b %d} to {self.day(17):%b %d, %Y} at the customer's request.")
        row = AuditLog.objects.get(action="item.change_dates")
        self.assertIn("changed the dates of", row.summary)
        self.assertIn("event date from", row.summary)
        self.assertEqual(row.details["pick-up date"], [self.day(10).isoformat(), self.day(16).isoformat()])
        self.assertEqual(row.details["return date"], [self.day(14).isoformat(), self.day(17).isoformat()])
        self.assertEqual(row.details["event date"], [self.day(12).isoformat(), self.day(17).isoformat()])

    def test_a_change_that_leaves_the_event_alone_is_not_logged_as_an_event_change(self):
        it = self.item(10, 14)
        self.assertEqual(self.change(it, self.day(11)).status_code, 200)
        row = AuditLog.objects.get(action="item.change_dates")
        self.assertNotIn("event date", row.details)
        self.assertNotIn("event date", row.summary)

    def test_only_signed_in_staff_can_change_dates_and_a_bad_request_is_refused(self):
        it = self.item(10, 14)
        url = reverse("arabela_admin:reservation_item_change_dates", args=[it.id])
        self.assertEqual(self.change(it, self.day(12), client=Client()).status_code, 401)
        customer = Client()
        customer.force_login(self.customer)
        self.assertEqual(self.change(it, self.day(12), client=customer).status_code, 401)
        self.assertEqual(self.client.get(url).status_code, 405)
        for body in (b"not json", b"[]", b"{}", json.dumps({"pickup": 5}).encode(), json.dumps({"pickup": "2026-13-45"}).encode(), json.dumps({"return": ["x"]}).encode()):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(url, data=body, content_type="application/json").status_code, 400)
        it.refresh_from_db()
        self.assertEqual((it.rental_date, it.return_date), (self.day(10), self.day(14)))

    def test_a_gown_that_is_back_or_cancelled_is_refused(self):
        it = self.item(10, 14)
        ReservationItem.objects.filter(pk=it.pk).update(stage=STAGE.RETURNED)
        self.assertEqual(self.change(it, self.day(12)).status_code, 400)
        ReservationItem.objects.filter(pk=it.pk).update(stage=STAGE.CANCELLED)
        self.assertEqual(self.change(it, self.day(12)).status_code, 409)

    def test_the_calendar_markers_stay_inside_the_booking_when_the_pick_up_is_after_the_event_day(self):
        it = self.item(16, 17, event_date=self.day(12))                                          # a booking saved before the event day moved with the pick-up
        self.assertEqual(it.effective_event_date, self.day(12))                                  # the Event column keeps the customer's day
        self.assertEqual(views_module._pickup_span(it), (self.day(16), self.day(16)))
        self.assertEqual(views_module._reserved_span(it), (self.day(16), self.day(16)))
        self.assertEqual(views_module._return_span(it), (self.day(17), self.day(17)))


# ---------------------------------------------------------------- no double booking, and the rest days
class DoubleBookingTests(DateBase):
    def test_new_days_cannot_run_into_another_booking_and_nothing_is_saved(self):
        gown = self.gown()
        it = self.item(10, 14, gown=gown)
        other = self.item(16, 18, gown=gown, name="Booked First")
        response = self.change(it, self.day(15), self.day(17), event=self.day(15))                # pick-up later AND return later, into the other booking's days
        self.assertEqual(response.status_code, 409)
        self.assertIn("Booked First", response.json()["error"])
        self.assertIn(other.reservation.reference_code, response.json()["error"])
        it.refresh_from_db()
        self.assertEqual((it.rental_date, it.return_date), (self.day(10), self.day(14)))          # both or neither
        self.assertEqual(self.change(it, self.day(12), self.day(15)).status_code, 200)             # days that are free are fine

    def test_the_rest_days_after_the_return_cannot_run_into_a_later_booking(self):
        gown = self.gown()
        it = self.item(2, 6, gown=gown, rest=True)                                                # rental days 2-6, rest block days 7-11
        self.item(14, 16, gown=gown, name="Next Customer")                                        # starts on day 14, so the gown must be back by day 8
        for ret in (9, 10, 12):
            with self.subTest(ret=ret):
                response = self.change(it, None, self.day(ret))
                self.assertEqual(response.status_code, 409, response.content)
                error = response.json()["error"]
                self.assertIn(f"needs {COOLDOWN_DAYS} rest days", error)
                self.assertIn("Next Customer", error)
                self.assertIn(f"The latest return date is {self.day(8):%b %d, %Y}", error)
        it.refresh_from_db()
        self.assertEqual(it.return_date, self.day(6))
        self.assertEqual((self.rest_block(it).start_date, self.rest_block(it).end_date), (self.day(7), self.day(11)))
        response = self.change(it, None, self.day(8))                                             # day 8 + 5 rest days = day 13: the day before the next booking
        self.assertEqual(response.status_code, 200, response.content)
        block = self.rest_block(it)
        self.assertEqual((block.start_date, block.end_date), (self.day(9), self.day(13)))         # the rest days moved with the return
        self.assertEqual(self.change(it, None, self.day(14)).status_code, 409)                     # and the rental itself still cannot touch the next booking

    def test_a_booking_right_after_the_rental_days_is_refused_by_the_rental_check_not_the_rest_one(self):
        gown = self.gown()
        it = self.item(2, 6, gown=gown, rest=True)
        self.item(10, 12, gown=gown, name="Right After")
        response = self.change(it, None, self.day(10))
        self.assertEqual(response.status_code, 409)
        self.assertIn("is already booked", response.json()["error"])

    def test_a_released_rest_block_is_respected_and_never_brought_back(self):
        gown = self.gown()
        it = self.item(2, 6, gown=gown, rest=False)                                               # an admin released the rest days (or it never had them)
        self.item(14, 16, gown=gown, name="Next Customer")
        self.assertEqual(self.change(it, None, self.day(9)).status_code, 200)                      # the rest days are not guarded, the rental days (2-9) are free
        self.assertIsNone(self.rest_block(it))                                                     # and nothing was created
        self.assertEqual(self.change(it, None, self.day(14)).status_code, 409)                      # the rental itself is still guarded

    def test_a_staff_block_inside_the_rest_days_is_not_a_clash_but_inside_the_rental_days_it_is(self):
        gown = self.gown()
        it = self.item(2, 6, gown=gown, rest=True)
        GownUnavailability.objects.create(gown=gown, start_date=self.day(10), end_date=self.day(12), reason=GownUnavailability.Reason.CLEANING)
        self.assertEqual(self.change(it, None, self.day(8)).status_code, 200)                      # the rest days 9-13 overlap the cleaning block: allowed (as on the website)
        response = self.change(it, None, self.day(11))                                             # the rental days 2-11 would now cover it
        self.assertEqual(response.status_code, 409)
        self.assertIn("blocked", response.json()["error"])

    def test_the_old_days_become_free_and_the_new_ones_are_held_when_the_whole_booking_moves_later(self):
        gown = self.gown()
        it = self.item(10, 14, gown=gown, rest=True)                                               # rental 10-14, rest 15-19
        probe = self.item(40, 41, gown=gown, name="Probe")
        for window in ((11, 13), (15, 19)):
            self.assertIsNotNone(views_module._schedule_conflict(probe, self.day(window[0]), self.day(window[1])))     # held today (rental, then rest block)
        self.assertEqual(self.change(it, self.day(20), self.day(22), event=self.day(21)).status_code, 200)
        for window in ((11, 13), (15, 19)):
            self.assertIsNone(views_module._schedule_conflict(probe, self.day(window[0]), self.day(window[1])))        # free for other customers now
        self.assertIn("Date Customer", views_module._schedule_conflict(probe, self.day(21), self.day(21)) or "")        # the new rental days are held
        self.assertIn("blocked", views_module._schedule_conflict(probe, self.day(25), self.day(26)) or "")              # and so are the new rest days
        block = self.rest_block(it)
        self.assertEqual((block.start_date, block.end_date), (self.day(23), self.day(22 + COOLDOWN_DAYS)))


# ---------------------------------------------------------------- the event day
class EventDateTests(DateBase):
    def events(self, item):
        return list(ReservationStatusEvent.objects.filter(reservation=item.reservation, item=item).order_by("id").values_list("label", flat=True))

    def test_a_pick_up_past_the_event_day_needs_the_new_event_day(self):
        it = self.item(10, 14, event_date=self.day(12))
        refused = self.change(it, self.day(13))
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()["error"],
                         f"The new pick-up ({self.day(13):%b %d, %Y}) is after the event date ({self.day(12):%b %d, %Y}). Choose the new event date too.")
        it.refresh_from_db()
        self.assertEqual((it.rental_date, it.original_rental_date), (self.day(10), None))
        self.assertEqual(self.events(it), [])

    def test_a_pick_up_onto_the_event_day_needs_nothing_more(self):
        it = self.item(10, 14, event_date=self.day(12))
        self.assertEqual(self.change(it, self.day(12)).status_code, 200)
        it.refresh_from_db()
        self.assertEqual((it.rental_date, it.effective_event_date), (self.day(12), self.day(12)))

    def test_the_new_event_day_must_be_between_the_pick_up_and_the_return(self):
        it = self.item(10, 14, event_date=self.day(12))
        for event in (12, 15):
            with self.subTest(event=event):
                refused = self.change(it, self.day(13), event=self.day(event))
                self.assertEqual(refused.status_code, 400)
                self.assertIn("The new event date must be between the pick-up", refused.json()["error"])
        it.refresh_from_db()
        self.assertEqual((it.rental_date, it.event_date), (self.day(10), self.day(12)))
        response = self.change(it, self.day(13), event=self.day(14))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["event_date"], self.day(14).isoformat())
        it.refresh_from_db()
        self.assertEqual((it.rental_date, it.event_date, it.return_date), (self.day(13), self.day(14), self.day(14)))
        self.assertEqual(self.events(it), [f"{it.gown_name} pick-up date changed", f"{it.gown_name} event date changed"])

    def test_the_new_event_day_follows_the_new_return_date(self):
        it = self.item(10, 14, event_date=self.day(12))
        self.assertEqual(self.change(it, self.day(16), self.day(18), event=self.day(19)).status_code, 400)
        self.assertEqual(self.change(it, self.day(16), self.day(18), event=self.day(18)).status_code, 200)

    def test_an_event_day_inside_the_rental_stays_as_it_is(self):
        it = self.item(10, 14, event_date=self.day(12))
        for body in ({"event": self.day(13)}, {"pickup": self.day(11), "event": self.day(13)}):
            with self.subTest(body=sorted(body)):
                refused = self.change(it, body.get("pickup"), event=body["event"])
                self.assertEqual(refused.status_code, 400)
                self.assertIn("already between the pick-up and the return", refused.json()["error"])
        it.refresh_from_db()
        self.assertEqual((it.rental_date, it.event_date), (self.day(10), self.day(12)))

    def test_a_booking_already_like_that_can_be_given_a_new_event_day_on_its_own(self):
        it = self.item(16, 18, event_date=self.day(12))                      # the pick-up was moved past the event day before it moved with it
        fee_before = ledger.planned_late_days(it, self.today)
        refused = self.change(it, event=self.day(19))
        self.assertEqual(refused.status_code, 400)
        response = self.change(it, event=self.day(17))
        self.assertEqual(response.status_code, 200, response.content)
        it.refresh_from_db()
        self.assertEqual((it.rental_date, it.return_date, it.event_date), (self.day(16), self.day(18), self.day(17)))
        self.assertEqual((it.original_rental_date, it.original_return_date), (None, None))             # nothing else was changed
        self.assertEqual(ledger.planned_late_days(it, self.today), fee_before)
        self.assertEqual(self.events(it), [f"{it.gown_name} event date changed"])
        row = AuditLog.objects.get(action="item.change_dates")
        self.assertEqual(row.details["event date"], [self.day(12).isoformat(), self.day(17).isoformat()])
        self.assertEqual(self.change(it, event=self.day(18)).status_code, 400)                          # it is inside now, so it stays

    def test_a_gown_that_is_out_can_be_given_its_new_event_day(self):
        it = self.item(-3, 4, stage=STAGE.RESERVED, picked_up_on=self.day(-3), event_date=self.day(-5))
        self.assertEqual(self.change(it, event=self.day(0)).status_code, 200)
        it.refresh_from_db()
        self.assertEqual(it.event_date, self.day(0))

    def test_a_returned_or_cancelled_gown_keeps_its_event_day(self):
        for stage in (STAGE.RETURNED, STAGE.CANCELLED):
            with self.subTest(stage=stage):
                it = self.item(16, 18, stage=stage, event_date=self.day(12))
                self.assertIn(self.change(it, event=self.day(17)).status_code, (400, 409))
                it.refresh_from_db()
                self.assertEqual(it.event_date, self.day(12))

    def test_the_pick_up_only_button_address_cannot_leave_the_event_behind(self):
        it = self.item(10, 14, event_date=self.day(12))
        response = self.client.post(reverse("arabela_admin:reservation_item_change_pickup", args=[it.id]),
                                    data=json.dumps({"date": self.day(13).isoformat()}), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Choose the new event date too", response.json()["error"])

    def test_a_bad_event_date_is_refused(self):
        it = self.item(16, 18, event_date=self.day(12))
        for body in (json.dumps({"event": "soon"}), json.dumps({"event": 17}), json.dumps({"event": ""})):
            with self.subTest(body=body):
                response = self.client.post(reverse("arabela_admin:reservation_item_change_dates", args=[it.id]), data=body, content_type="application/json")
                self.assertEqual(response.status_code, 400)
        it.refresh_from_db()
        self.assertEqual(it.event_date, self.day(12))


# ---------------------------------------------------------------- what the page shows
class ActivePageTests(DateBase):
    def page(self):
        response = self.client.get(reverse("arabela_admin:active_reservations"))
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def booking(self, response, item):
        return next(r for r in response.context["reservations"] if r.id == item.reservation_id)

    def test_the_booking_shows_its_late_fee_labeled_and_linked_to_payments(self):
        it = self.item(-6, 6, stage=STAGE.RESERVED, picked_up_on=self.day(-6), original_return_date=self.day(2))        # return moved 4 days later
        response, html = self.page()
        booking = self.booking(response, it)
        self.assertEqual((booking.late_days_planned, booking.late_fee_planned, booking.fee_over_deposit), (4, D("800"), D("0")))
        payments = reverse("arabela_admin:payments") + f"?open={it.reservation_id}"
        self.assertIn(f'<a class="ar-feechip" href="{payments}"', html)
        self.assertIn("Date change fee &#8369;800</a>", html)
        self.assertNotIn("Overdue fee", html.split('<main>')[1].split('id="ar-more-')[0])
        self.assertIn("comes out of the security deposit", html)
        self.assertIn("Date change fee &#8369;800 &middot; 4 days &times; &#8369;200 if it comes back", html)             # inside the gown: amount, days, rate, and what it is
        self.assertNotIn("over the deposit", html)

    def test_a_fee_bigger_than_the_deposit_says_how_much_is_over(self):
        it = self.item(-14, 2, stage=STAGE.RESERVED, picked_up_on=self.day(-14), original_return_date=self.day(-10))   # 12 days x 200 = 2,400
        pay(it.reservation, Payment.Purpose.DEPOSIT, "2000")
        response, html = self.page()
        booking = self.booking(response, it)
        self.assertEqual((booking.late_fee_planned, booking.fee_deposit_paid, booking.fee_over_deposit), (D("2400"), D("2000"), D("400")))
        self.assertIn("Date change fee &#8369;2,400", html)
        self.assertIn("&#8369;400 over the deposit", html)

    def test_a_deposit_not_paid_yet_is_measured_against_the_deposit_the_booking_was_made_with(self):
        """A walk-in that pays at pick-up: nothing paid yet must not make every late fee read "over the deposit"."""
        small = self.item(3, 9, original_return_date=self.day(5))                                                       # 4 days x 200 = 800
        big = self.item(3, 20, original_return_date=self.day(8))                                                        # 12 days x 200 = 2,400
        Reservation.objects.filter(pk__in=[small.reservation_id, big.reservation_id]).update(channel=Reservation.Channel.WALK_IN)
        response, html = self.page()
        first, second = self.booking(response, small), self.booking(response, big)
        self.assertEqual((first.fee_deposit_paid, first.fee_deposit_basis, first.late_fee_planned, first.fee_over_deposit), (D("0"), D("2000.00"), D("800"), D("0")))
        self.assertEqual((second.late_fee_planned, second.fee_over_deposit), (D("2400"), D("400")))
        self.assertEqual(html.count("over the deposit</span>"), 1)
        self.assertIn("&#8369;400 over the deposit", html)
        self.assertIn("once it is paid", html)                                                                           # the calendar's note for an unpaid deposit

    def test_a_gown_that_is_out_past_its_return_counts_so_far_and_a_gown_back_late_says_when_it_came_back(self):
        late = self.item(-12, -3, stage=STAGE.RESERVED, picked_up_on=self.day(-12), original_return_date=self.day(-10))
        response, html = self.page()
        booking = self.booking(response, late)
        self.assertEqual(booking.late_days_planned, 10)                                                                 # up to today, not up to the old return day
        self.assertEqual((booking.change_days_planned, booking.overdue_days_planned), (7, 3))                           # moved 7 days, then 3 days past the new date
        self.assertIn("Date change fee &#8369;1,400</a>", html)
        self.assertIn("Overdue fee &#8369;600</a>", html)
        self.assertIn("7 days &times; &#8369;200, return moved to", html)
        self.assertIn("3 days &times; &#8369;200 so far, growing each day it is out", html)
        ReservationItem.objects.filter(pk=late.pk).update(stage=STAGE.RETURNED, returned_on=self.day(-5))
        other = ReservationItem.objects.create(reservation=late.reservation, gown=self.gown(), gown_name="Second Gown", stage=STAGE.PICKUP, rental_price=D("1000"),
                                               rental_date=self.day(3), return_date=self.day(7))
        response, html = self.page()
        self.assertEqual(self.booking(response, other).late_days_planned, 5)
        self.assertIn(f"(it came back {self.day(-5):%b} {self.day(-5).day})", html)

    def test_no_fee_chip_when_nothing_is_late(self):
        self.item(10, 14)
        response, html = self.page()
        self.assertNotIn('class="ar-feechip"', html)
        self.assertNotIn('class="ar-feeline"', html)

    def test_the_date_window_gets_what_it_needs_and_the_limits(self):
        gown = self.gown()
        it = self.item(10, 14, gown=gown, rest=True, original_return_date=self.day(14))
        other = self.item(20, 22, gown=gown, name="Someone Else")
        manual = GownUnavailability.objects.create(gown=gown, start_date=self.day(30), end_date=self.day(31), reason=GownUnavailability.Reason.REPAIR)
        response, html = self.page()
        for needle in (f'data-orig-pickup="{self.day(10).isoformat()}"', f'data-orig-return="{self.day(14).isoformat()}"', 'data-rest="1"', 'data-rate="200"',
                       'data-deposit-paid="', 'data-deposit="2000.00"', 'data-fee-others="', "var REST_DAYS = 5;", "var PICKUP_MAX_DAYS = 60;", "/change-dates/"):
            with self.subTest(needle=needle):
                self.assertIn(needle, html)
        holds = response.context["item_holds"][it.id]
        self.assertEqual(sorted((h["kind"], h["itemId"]) for h in holds if h["kind"] == "booking"), [("booking", other.id)])
        self.assertTrue(any(h["kind"] == "block" and h["itemId"] is None for h in holds))                # the staff block, which needs no rest days
        self.assertFalse([h for h in holds if h["itemId"] == it.id])                                      # its own rest block is not handed to its own calendar

    def test_an_event_day_outside_the_rental_is_flagged_with_its_own_button(self):
        outside = self.item(16, 18, event_date=self.day(12))
        inside = self.item(10, 14, event_date=self.day(12))
        response, html = self.page()
        self.assertTrue(self.booking(response, outside).event_off)
        self.assertFalse(self.booking(response, inside).event_off)
        self.assertIn(f'data-kind="event" data-item-id="{outside.id}" data-event-day="{self.day(12).isoformat()}"', re.sub(r"\s+", " ", html))
        self.assertNotIn(f'data-kind="event" data-item-id="{inside.id}"', re.sub(r"\s+", " ", html))
        self.assertEqual(html.count("Set new event date</button>"), 1)
        self.assertEqual(html.count(">needs a new date</small>"), 1)
        self.assertIn(f"The event date ({self.day(12):%b} {self.day(12).day}) is before the pick-up ({self.day(16):%b} {self.day(16).day}). Set the new event date.", html)
        for item in (outside, inside):                                                     # every date button knows the event day the window checks against
            self.assertRegex(re.sub(r"\s+", " ", html), rf'data-kind="pickup" data-item-id="{item.id}" data-event-day="{self.day(12).isoformat()}"')
        self.assertEqual(self.change(outside, event=self.day(17)).status_code, 200)
        _, html = self.page()
        self.assertNotIn("Set new event date</button>", html)
        self.assertNotIn(">needs a new date</small>", html)

    def test_a_gown_without_its_rest_block_says_so_to_the_window(self):
        self.item(10, 14, rest=False)
        response, html = self.page()
        self.assertIn('data-rest="0"', html)
        self.assertNotIn('data-rest="1"', html)
