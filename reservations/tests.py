import random
import threading
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from accounts.models import CustomerMessage, UserProfile
from reservations import reminders, timeline
from reservations.models import (
    ReminderRun, Reservation, ReservationItem, ReservationSequence, ReservationStatusEvent,
)

User = get_user_model()


class ReservationDisplayNameTests(TestCase):
    """`display_customer_name`/`booked_as_name` -- customer_name is a frozen snapshot
    taken at submission time; these two properties are what keeps admin screens
    showing a customer's CURRENT name after they rename themselves in Profile,
    without losing the fact that a booking happened under a different name."""

    def setUp(self):
        self.customer = User.objects.create_user(username="rename_test", password="x")

    def test_display_name_prefers_the_live_profile_name(self):
        UserProfile.objects.create(user=self.customer, display_name="New Name")
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Old Name")
        self.assertEqual(reservation.display_customer_name, "New Name")

    def test_display_name_falls_back_to_the_frozen_snapshot_with_no_profile_name(self):
        UserProfile.objects.create(user=self.customer, display_name="")
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Snapshot Name")
        self.assertEqual(reservation.display_customer_name, "Snapshot Name")

    def test_booked_as_name_is_empty_when_names_match(self):
        UserProfile.objects.create(user=self.customer, display_name="Same Name")
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Same Name")
        self.assertEqual(reservation.booked_as_name, "")

    def test_booked_as_name_shows_the_frozen_name_when_it_differs(self):
        UserProfile.objects.create(user=self.customer, display_name="Renamed Later")
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Original Booking Name")
        self.assertEqual(reservation.booked_as_name, "Original Booking Name")


class ReservationPaymentStateTests(TestCase):
    """`payment_state` derives one label from status + payment_proof_url -- must
    never drift, since there's deliberately no separate is_paid column."""

    def setUp(self):
        self.customer = User.objects.create_user(username="payment_state_test", password="x")

    def _reservation(self, **overrides):
        defaults = dict(customer=self.customer, customer_name="Test")
        defaults.update(overrides)
        return Reservation.objects.create(**defaults)

    def test_pending_with_no_proof_is_not_paid(self):
        r = self._reservation(status=Reservation.Status.PENDING, payment_proof_url="")
        self.assertEqual(r.payment_state, "Not Paid")

    def test_pending_with_proof_is_under_review(self):
        r = self._reservation(status=Reservation.Status.PENDING, payment_proof_url="https://example.test/proof.jpg")
        self.assertEqual(r.payment_state, "Payment Under Review")

    def test_confirmed_is_verified(self):
        r = self._reservation(status=Reservation.Status.CONFIRMED)
        self.assertEqual(r.payment_state, "Payment Verified")

    def test_rejected_is_payment_rejected(self):
        r = self._reservation(status=Reservation.Status.REJECTED)
        self.assertEqual(r.payment_state, "Payment Rejected")

    def test_cancelled_has_no_payment_label(self):
        r = self._reservation(status=Reservation.Status.CANCELLED)
        self.assertEqual(r.payment_state, "")

    def test_can_upload_proof_only_while_pending_and_unproven(self):
        pending_no_proof = self._reservation(status=Reservation.Status.PENDING, payment_proof_url="")
        pending_with_proof = self._reservation(status=Reservation.Status.PENDING, payment_proof_url="https://example.test/x.jpg")
        confirmed = self._reservation(status=Reservation.Status.CONFIRMED)
        self.assertTrue(pending_no_proof.can_upload_proof)
        self.assertFalse(pending_with_proof.can_upload_proof)
        self.assertFalse(confirmed.can_upload_proof)


class ReferenceCodeTests(TestCase):
    """`next_reference_code`/`save()` -- every reservation gets a unique, sequential,
    year-scoped reference code auto-assigned exactly once."""

    def setUp(self):
        self.customer = User.objects.create_user(username="refcode_test", password="x")

    def test_reference_code_is_auto_assigned_on_save(self):
        r = Reservation.objects.create(customer=self.customer, customer_name="Test")
        self.assertTrue(r.reference_code)
        self.assertTrue(r.reference_code.startswith(f"RSV-{timezone.now().year}-"))

    def test_sequential_reservations_get_sequential_codes(self):
        r1 = Reservation.objects.create(customer=self.customer, customer_name="A")
        r2 = Reservation.objects.create(customer=self.customer, customer_name="B")
        n1 = int(r1.reference_code.split("-")[2])
        n2 = int(r2.reference_code.split("-")[2])
        self.assertEqual(n2, n1 + 1)

    def test_an_explicitly_provided_reference_code_is_not_overwritten(self):
        r = Reservation.objects.create(customer=self.customer, customer_name="Test", reference_code="RSV-CUSTOM-0001")
        self.assertEqual(r.reference_code, "RSV-CUSTOM-0001")

    def test_a_new_year_starts_its_own_sequence_at_one(self):
        with patch("reservations.models.timezone.now",
                  return_value=timezone.now().replace(year=2951)):
            r = Reservation.objects.create(customer=self.customer, customer_name="Test")
        self.assertEqual(r.reference_code, "RSV-2951-0001")

    def test_a_collision_from_corrupted_data_is_survived_not_crashed(self):
        """The counter itself can never hand out a duplicate number (see
        ReservationSequenceTests) -- this simulates the one thing that could still
        collide: a hand-edited or restored-from-backup row occupying the exact code
        the counter is about to hand out next."""
        taken = Reservation.objects.create(customer=self.customer, customer_name="First")
        real = Reservation.next_reference_code
        calls = {"n": 0}

        def flaky(cls):
            calls["n"] += 1
            return taken.reference_code if calls["n"] == 1 else real.__func__(cls)

        with patch.object(Reservation, "next_reference_code", classmethod(flaky)):
            second = Reservation.objects.create(customer=self.customer, customer_name="Second")

        self.assertEqual(calls["n"], 2)
        self.assertNotEqual(second.reference_code, taken.reference_code)

    def test_persistent_corruption_raises_instead_of_looping_forever(self):
        taken = Reservation.objects.create(customer=self.customer, customer_name="First")
        with patch.object(Reservation, "next_reference_code",
                          classmethod(lambda cls: taken.reference_code)):
            with self.assertRaises(IntegrityError):
                Reservation.objects.create(customer=self.customer, customer_name="AlwaysCollides")

    def test_updating_an_existing_reservation_never_touches_code_generation(self):
        """Every later save -- approve, reject, release the deposit -- must be a
        complete no-op for this machinery: no re-read, no lock, no risk."""
        r = Reservation.objects.create(customer=self.customer, customer_name="Test")
        with patch.object(Reservation, "next_reference_code", side_effect=AssertionError(
                "next_reference_code() must not run when a code already exists")):
            r.notes = "reviewed"
            r.save(update_fields=["notes"])


class ReservationSequenceTests(TestCase):
    """`ReservationSequence.next_value_for` -- the row-locked counter that replaced
    "read the highest reference_code, add one". See the model's own docstring for why
    that older shape is not safe enough for a customer-facing checkout path.
    gowns.models.GownSequence is the identical fix applied to Gown.gown_id."""

    def test_first_call_for_a_year_returns_one(self):
        self.assertEqual(ReservationSequence.next_value_for(2952), 1)

    def test_consecutive_calls_increment_by_one(self):
        first = ReservationSequence.next_value_for(2953)
        second = ReservationSequence.next_value_for(2953)
        third = ReservationSequence.next_value_for(2953)
        self.assertEqual([first, second, third], [1, 2, 3])

    def test_different_years_have_independent_counters(self):
        self.assertEqual(ReservationSequence.next_value_for(2954), 1)
        self.assertEqual(ReservationSequence.next_value_for(2955), 1)
        self.assertEqual(ReservationSequence.next_value_for(2954), 2)

    def test_it_persists_a_row_per_year(self):
        ReservationSequence.next_value_for(2956)
        row = ReservationSequence.objects.get(year=2956)
        self.assertEqual(row.next_value, 2)

    def test_ten_real_concurrent_callers_never_receive_the_same_number(self):
        """The actual property this whole fix exists for, proven with real threads and
        real, separately-committed database transactions -- not simulated. Kept to 10
        threads (this dev database's Supabase pooler caps at 15 simultaneous session
        connections; going higher fails the CONNECTION itself before any application
        code runs, which would test the pool, not the fix).

        Each thread opens its OWN connection and genuinely commits (that is the whole
        point -- it is what makes this a real test of the row lock) -- unlike every
        other write in this TestCase, these rows are NOT rolled back by the normal
        per-test transaction, because they were never part of it. A fixed year would
        make this test fail on its second run against this project's persisted
        --keepdb database (the counter would already be past 1). A fresh random year
        sidesteps that with no cleanup bookkeeping needed."""
        from django.db import connections as db_connections

        year = random.randint(90000, 999999)
        n_threads = 10
        results = [None] * n_threads
        barrier = threading.Barrier(n_threads)

        def worker(index):
            db_connections.close_all()
            try:
                barrier.wait(timeout=5)
                results[index] = ReservationSequence.next_value_for(year)
            except Exception as exc:  # noqa: BLE001 -- surfaced via the assertion below
                results[index] = exc
            finally:
                db_connections.close_all()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        # Deliberately NOT closing the main thread's own connection here (a `close_all()`
        # call used to sit on this line): that connection is the one Django's TestCase
        # has wrapped in an open, uncommitted atomic block/savepoint stack for the whole
        # test, and force-closing it out from under that bookkeeping leaves it unable to
        # reconnect cleanly -- every query for the REST OF THIS TEST CLASS (not just this
        # test) then fails with "the connection is closed". Each worker already closes
        # only its OWN thread-local connection above; the main thread's is never this
        # test's to close.

        errors = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(errors, [], "a concurrent caller raised instead of queueing")
        self.assertEqual(sorted(results), list(range(1, n_threads + 1)),
                         "every thread must get a distinct, contiguous number")


class CanCustomerCancelTests(TestCase):
    """`ReservationItem.can_customer_cancel` -- self-cancel is a whole-reservation
    action; must require every sibling item to still be un-picked-up, and the
    reservation itself to be in an active (not terminal) status."""

    def setUp(self):
        self.customer = User.objects.create_user(username="cancel_test", password="x")

    def _item(self, stage=ReservationItem.Stage.PICKUP, reservation_status=Reservation.Status.PENDING):
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Test", status=reservation_status)
        return ReservationItem.objects.create(
            reservation=reservation, gown_name="Test Gown", stage=stage,
            rental_date=date.today(), return_date=date.today() + timedelta(days=3),
        )

    def test_pending_pickup_item_can_be_cancelled(self):
        item = self._item()
        self.assertTrue(item.can_customer_cancel)

    def test_already_picked_up_item_cannot_be_cancelled(self):
        item = self._item(stage=ReservationItem.Stage.RESERVED)
        self.assertFalse(item.can_customer_cancel)

    def test_rejected_reservations_item_cannot_be_cancelled_even_at_pickup_stage(self):
        item = self._item(reservation_status=Reservation.Status.REJECTED)
        self.assertFalse(item.can_customer_cancel)

    def test_cannot_cancel_if_a_sibling_item_already_picked_up(self):
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Test")
        item = ReservationItem.objects.create(
            reservation=reservation, gown_name="Item One", stage=ReservationItem.Stage.PICKUP,
            rental_date=date.today(), return_date=date.today() + timedelta(days=3),
        )
        ReservationItem.objects.create(
            reservation=reservation, gown_name="Item Two", stage=ReservationItem.Stage.RESERVED,
            rental_date=date.today(), return_date=date.today() + timedelta(days=3),
        )
        self.assertFalse(item.can_customer_cancel)


class StatusEventRecordTests(TestCase):
    """`ReservationStatusEvent.record` -- the single write path for history. It must
    never be able to break the action it is describing, which is the whole reason it
    swallows its own errors behind a savepoint."""

    def setUp(self):
        self.customer = User.objects.create_user(username="evt_record", password="x")
        self.reservation = Reservation.objects.create(customer=self.customer, customer_name="Test")

    def test_record_creates_an_event_with_the_given_label_and_actor(self):
        event = ReservationStatusEvent.record(
            self.reservation, "Reservation approved",
            actor=ReservationStatusEvent.Actor.STAFF,
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.label, "Reservation approved")
        self.assertEqual(event.actor, "Staff")
        self.assertTrue(event.time_known)
        self.assertIsNone(event.item_id)

    def test_detail_longer_than_the_column_is_truncated_not_rejected(self):
        event = ReservationStatusEvent.record(self.reservation, "Rejected", detail="x" * 500)
        self.assertEqual(len(event.detail), 300)

    def test_a_failing_write_returns_none_and_leaves_the_caller_usable(self):
        """The savepoint is the point: a failed history INSERT inside a transaction
        must not poison it, or a missing timeline row would break the approval itself."""
        with transaction.atomic():
            # label is max_length=200; 5000 chars is a hard DataError from Postgres.
            self.assertIsNone(ReservationStatusEvent.record(self.reservation, "y" * 5000))
            # If the savepoint did not contain that failure, this next query raises
            # TransactionManagementError instead of returning a number.
            self.assertEqual(self.reservation.status_events.count(), 0)

    def test_events_come_back_oldest_first_by_default(self):
        now = timezone.now()
        ReservationStatusEvent.objects.create(
            reservation=self.reservation, label="Second", occurred_at=now)
        ReservationStatusEvent.objects.create(
            reservation=self.reservation, label="First", occurred_at=now - timedelta(hours=1))
        self.assertEqual(
            [e.label for e in self.reservation.status_events.all()], ["First", "Second"])

    def test_deleting_a_reservation_takes_its_history_with_it(self):
        ReservationStatusEvent.record(self.reservation, "Reservation submitted")
        self.reservation.delete()
        self.assertEqual(ReservationStatusEvent.objects.count(), 0)


class TimelineAssemblyTests(TestCase):
    """`reservations.timeline` -- what belongs in one gown's history, in what order,
    and how rows get flagged for display."""

    def setUp(self):
        self.customer = User.objects.create_user(username="evt_timeline", password="x")
        self.reservation = Reservation.objects.create(customer=self.customer, customer_name="Test")
        self.gown_a = self._item("Gown A")
        self.gown_b = self._item("Gown B")

        base = timezone.now() - timedelta(days=1)
        self._event("Reservation submitted", base, actor="Customer")
        self._event("Reservation approved", base + timedelta(hours=1), actor="Staff")
        self._event("Gown A picked up", base + timedelta(hours=2), item=self.gown_a)
        self._event("Gown B picked up", base + timedelta(hours=3), item=self.gown_b)

    def _item(self, name):
        return ReservationItem.objects.create(
            reservation=self.reservation, gown_name=name,
            rental_date=date.today(), return_date=date.today() + timedelta(days=3),
        )

    def _event(self, label, when, item=None, actor="Staff"):
        return ReservationStatusEvent.objects.create(
            reservation=self.reservation, item=item, label=label,
            occurred_at=when, actor=actor,
        )

    def test_for_item_includes_reservation_wide_events(self):
        labels = [e.label for e in timeline.for_item(self.gown_a)]
        self.assertIn("Reservation submitted", labels)
        self.assertIn("Reservation approved", labels)

    def test_for_item_includes_its_own_item_events(self):
        labels = [e.label for e in timeline.for_item(self.gown_a)]
        self.assertIn("Gown A picked up", labels)

    def test_for_item_excludes_a_sibling_gowns_events(self):
        """The one thing a per-gown timeline must never do: show another gown's
        history as if it were this one's."""
        labels = [e.label for e in timeline.for_item(self.gown_a)]
        self.assertNotIn("Gown B picked up", labels)

    def test_for_item_is_newest_first(self):
        labels = [e.label for e in timeline.for_item(self.gown_a)]
        self.assertEqual(labels[0], "Gown A picked up")
        self.assertEqual(labels[-1], "Reservation submitted")

    def test_only_the_newest_row_is_flagged_latest(self):
        events = timeline.for_item(self.gown_a)
        self.assertTrue(events[0].is_latest)
        self.assertEqual(sum(1 for e in events if e.is_latest), 1)

    def test_negative_events_are_flagged_for_the_template(self):
        self._event("Reservation cancelled", timezone.now())
        events = timeline.for_item(self.gown_a)
        self.assertTrue(events[0].is_negative)
        self.assertFalse(events[1].is_negative)

    def test_attach_to_items_matches_for_item_exactly(self):
        """The bulk path exists purely for query count -- if it ever disagrees with the
        single-object path, admin and customer would show different histories."""
        items = list(ReservationItem.objects.filter(reservation=self.reservation).order_by("id"))
        timeline.attach_to_items(items)
        for item in items:
            self.assertEqual(
                [e.label for e in item.timeline],
                [e.label for e in timeline.for_item(item)],
            )

    def test_attach_to_items_uses_a_constant_number_of_queries(self):
        for index in range(6):
            self._item("Extra %d" % index)
        items = list(ReservationItem.objects.filter(reservation=self.reservation))
        with self.assertNumQueries(1):
            timeline.attach_to_items(items)

    def test_attach_to_reservations_returns_the_whole_bookings_history(self):
        reservations = timeline.attach_to_reservations(
            Reservation.objects.filter(id=self.reservation.id))
        labels = [e.label for e in reservations[0].timeline]
        self.assertIn("Gown A picked up", labels)
        self.assertIn("Gown B picked up", labels)
        self.assertEqual(labels[-1], "Reservation submitted")

    def test_attach_handles_an_empty_input_without_querying(self):
        with self.assertNumQueries(0):
            self.assertEqual(timeline.attach_to_items([]), [])
            self.assertEqual(timeline.attach_to_reservations(Reservation.objects.none()), [])

    def test_a_reservation_with_no_events_gets_an_empty_timeline_not_an_error(self):
        empty = Reservation.objects.create(customer=self.customer, customer_name="Empty")
        item = ReservationItem.objects.create(
            reservation=empty, gown_name="Lonely",
            rental_date=date.today(), return_date=date.today() + timedelta(days=1),
        )
        self.assertEqual(timeline.for_item(item), [])
        timeline.attach_to_items([item])
        self.assertEqual(item.timeline, [])


class ReminderRuleTests(TestCase):
    """`reservations.reminders._due_kind` via `find_due_reminders` -- which gown gets
    which reminder, and (more importantly) which gowns must be left alone. A wrong
    reminder is worse than a missing one: telling someone their gown is overdue when
    they never collected it would embarrass the shop in front of its own customer."""

    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="rem_rules", password="x")

    def setUp(self):
        self.today = timezone.localdate()

    def _item(self, *, stage, rental_offset, return_offset,
              status=Reservation.Status.CONFIRMED, name="Gown"):
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Rem Cust", status=status)
        return ReservationItem.objects.create(
            reservation=reservation, gown_name=name, stage=stage,
            rental_date=self.today + timedelta(days=rental_offset),
            event_date=self.today + timedelta(days=rental_offset),
            return_date=self.today + timedelta(days=return_offset),
            overdue_date=self.today + timedelta(days=return_offset),
        )

    def _kinds(self, today=None):
        return {item.gown_name: kind
                for item, kind in reminders.find_due_reminders(today or self.today)}

    # ---------------------------------------------------------------- what fires
    def test_pick_up_tomorrow(self):
        self._item(stage=ReservationItem.Stage.PICKUP, rental_offset=1, return_offset=5)
        self.assertEqual(self._kinds()["Gown"], reminders.PICKUP_TOMORROW)

    def test_pick_up_today(self):
        self._item(stage=ReservationItem.Stage.PICKUP, rental_offset=0, return_offset=4)
        self.assertEqual(self._kinds()["Gown"], reminders.PICKUP_TODAY)

    def test_return_tomorrow(self):
        self._item(stage=ReservationItem.Stage.RESERVED, rental_offset=-3, return_offset=1)
        self.assertEqual(self._kinds()["Gown"], reminders.RETURN_TOMORROW)

    def test_return_today(self):
        self._item(stage=ReservationItem.Stage.RETURN, rental_offset=-4, return_offset=0)
        self.assertEqual(self._kinds()["Gown"], reminders.RETURN_TODAY)

    def test_overdue(self):
        self._item(stage=ReservationItem.Stage.OVERDUE, rental_offset=-9, return_offset=-2)
        self.assertEqual(self._kinds()["Gown"], reminders.OVERDUE)

    def test_an_active_reservation_is_reminded_as_well_as_a_confirmed_one(self):
        self._item(stage=ReservationItem.Stage.RESERVED, rental_offset=-3, return_offset=1,
                   status=Reservation.Status.ACTIVE)
        self.assertEqual(self._kinds()["Gown"], reminders.RETURN_TOMORROW)

    # ---------------------------------------------------------------- what must not
    def test_a_returned_gown_is_never_reminded(self):
        self._item(stage=ReservationItem.Stage.RETURNED, rental_offset=-9, return_offset=-1)
        self.assertEqual(self._kinds(), {})

    def test_an_unapproved_reservation_is_never_reminded(self):
        """Pending is waiting on STAFF. Nagging the customer to collect a gown nobody
        has approved would blame them for the shop's own queue."""
        self._item(stage=ReservationItem.Stage.PICKUP, rental_offset=1, return_offset=5,
                   status=Reservation.Status.PENDING)
        self.assertEqual(self._kinds(), {})

    def test_a_cancelled_reservation_is_never_reminded(self):
        self._item(stage=ReservationItem.Stage.PICKUP, rental_offset=1, return_offset=5,
                   status=Reservation.Status.CANCELLED)
        self.assertEqual(self._kinds(), {})

    def test_a_rejected_reservation_is_never_reminded(self):
        self._item(stage=ReservationItem.Stage.PICKUP, rental_offset=1, return_offset=5,
                   status=Reservation.Status.REJECTED)
        self.assertEqual(self._kinds(), {})

    def test_a_gown_never_collected_is_not_called_overdue(self):
        """Still at Pick-up with the date long past is a no-show -- a staff phone call,
        not an automated accusation that the customer is holding a gown they never got."""
        self._item(stage=ReservationItem.Stage.PICKUP, rental_offset=-5, return_offset=2)
        self.assertEqual(self._kinds(), {})

    def test_a_return_still_days_away_is_not_reminded_early(self):
        self._item(stage=ReservationItem.Stage.RESERVED, rental_offset=-1, return_offset=6)
        self.assertEqual(self._kinds(), {})

    def test_a_booking_overdue_past_the_cap_is_left_to_staff(self):
        """Past the cap this stops being a nudge and becomes collections. It is also what
        stops the very first sweep blasting every long-abandoned booking at once."""
        self._item(stage=ReservationItem.Stage.OVERDUE, rental_offset=-90,
                   return_offset=-(reminders.OVERDUE_MAX_DAYS + 1))
        self.assertEqual(self._kinds(), {})

    def test_a_booking_overdue_exactly_at_the_cap_still_gets_one(self):
        self._item(stage=ReservationItem.Stage.OVERDUE, rental_offset=-90,
                   return_offset=-reminders.OVERDUE_MAX_DAYS)
        self.assertEqual(self._kinds()["Gown"], reminders.OVERDUE)


class ReminderDeliveryTests(TestCase):
    """What the customer actually receives, and the guarantee that they never receive
    it twice."""

    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="rem_send", password="x")

    def setUp(self):
        self.today = timezone.localdate()
        self.reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Rem Cust",
            status=Reservation.Status.CONFIRMED)
        self.item = ReservationItem.objects.create(
            reservation=self.reservation, gown_name="Ivory Ballgown",
            stage=ReservationItem.Stage.RESERVED,
            rental_date=self.today - timedelta(days=3),
            event_date=self.today - timedelta(days=1),
            return_date=self.today + timedelta(days=1),
            overdue_date=self.today + timedelta(days=2),
        )

    def test_a_reminder_lands_in_the_inbox_unread(self):
        reminders.send_due_reminders(self.today)
        message = CustomerMessage.objects.get(recipient=self.customer)
        self.assertFalse(message.is_read)
        self.assertEqual(message.category, CustomerMessage.Category.RESERVATION_REMINDER)
        self.assertIn("Ivory Ballgown", message.body)
        self.assertIn("due back tomorrow", message.body)
        self.assertIn(self.reservation.reference_code, message.body)

    def test_an_overdue_reminder_uses_its_own_category(self):
        """So the Messages page can show it in red without reading the body text."""
        ReservationItem.objects.filter(id=self.item.id).update(
            stage=ReservationItem.Stage.OVERDUE,
            return_date=self.today - timedelta(days=2))
        reminders.send_due_reminders(self.today)
        message = CustomerMessage.objects.get(recipient=self.customer)
        self.assertEqual(message.category, CustomerMessage.Category.RETURN_OVERDUE)
        self.assertIn("2 days overdue", message.body)

    def test_one_day_overdue_reads_as_singular(self):
        ReservationItem.objects.filter(id=self.item.id).update(
            stage=ReservationItem.Stage.OVERDUE,
            return_date=self.today - timedelta(days=1))
        reminders.send_due_reminders(self.today)
        self.assertIn("1 day overdue", CustomerMessage.objects.get(recipient=self.customer).body)

    def test_every_reminder_is_written_to_the_timeline_as_a_system_event(self):
        """Not decoration: this is the shop's proof it warned the customer, and it is
        also the dedupe key."""
        reminders.send_due_reminders(self.today)
        event = self.reservation.status_events.get(
            label=reminders.EVENT_LABELS[reminders.RETURN_TOMORROW])
        self.assertEqual(event.actor, "System")
        self.assertEqual(event.item_id, self.item.id)

    def test_the_same_reminder_never_goes_out_twice(self):
        self.assertEqual(reminders.send_due_reminders(self.today), 1)
        self.assertEqual(reminders.send_due_reminders(self.today), 0)
        self.assertEqual(CustomerMessage.objects.filter(recipient=self.customer).count(), 1)

    def test_the_next_days_reminder_is_a_new_one_not_a_repeat(self):
        reminders.send_due_reminders(self.today)
        reminders.send_due_reminders(self.today + timedelta(days=1))
        bodies = [m.body for m in CustomerMessage.objects.filter(recipient=self.customer)]
        self.assertEqual(len(bodies), 2)
        self.assertTrue(any("due back tomorrow" in b for b in bodies), bodies)
        self.assertTrue(any("due back today" in b for b in bodies), bodies)

    def test_overdue_waits_the_full_cadence_before_repeating(self):
        ReservationItem.objects.filter(id=self.item.id).update(
            stage=ReservationItem.Stage.OVERDUE,
            return_date=self.today - timedelta(days=1))
        reminders.send_due_reminders(self.today)

        just_before = self.today + timedelta(days=reminders.OVERDUE_REPEAT_DAYS - 1)
        self.assertEqual(reminders.send_due_reminders(just_before), 0)

        on_cadence = self.today + timedelta(days=reminders.OVERDUE_REPEAT_DAYS)
        self.assertEqual(reminders.send_due_reminders(on_cadence), 1)

    def test_a_message_is_rolled_back_if_its_timeline_record_fails(self):
        """The nastiest failure this feature could have. The timeline entry is the dedupe
        key, and record() swallows its own errors by design -- so if the message were
        allowed to survive a failed record, the customer would get the SAME reminder
        every day forever with nothing to stop it."""
        with patch.object(ReservationStatusEvent, "record", return_value=None):
            sent = reminders.send_due_reminders(self.today)

        self.assertEqual(sent, 0)
        self.assertEqual(CustomerMessage.objects.count(), 0)

        # And because nothing was written, it is simply retried next time -- not lost.
        self.assertEqual(reminders.send_due_reminders(self.today), 1)
        self.assertEqual(CustomerMessage.objects.count(), 1)

    def test_one_broken_row_does_not_stop_everyone_elses_reminders(self):
        other = User.objects.create_user(username="rem_send_other", password="x")
        second = Reservation.objects.create(
            customer=other, customer_name="Other", status=Reservation.Status.CONFIRMED)
        ReservationItem.objects.create(
            reservation=second, gown_name="Second Gown",
            stage=ReservationItem.Stage.RESERVED,
            rental_date=self.today - timedelta(days=3), event_date=self.today,
            return_date=self.today + timedelta(days=1),
            overdue_date=self.today + timedelta(days=2))

        real_send = reminders.send_reminder
        calls = []

        def explode_on_first(item, kind, today=None):
            calls.append(item.gown_name)
            if len(calls) == 1:
                raise RuntimeError("simulated failure")
            return real_send(item, kind, today)

        with patch.object(reminders, "send_reminder", explode_on_first):
            sent = reminders.send_due_reminders(self.today)

        self.assertEqual(len(calls), 2)
        self.assertEqual(sent, 1)
        self.assertEqual(CustomerMessage.objects.count(), 1)


class ReminderDailyGateTests(TestCase):
    """`run_daily_sweep_if_due` -- the stand-in for a cron job. Must fire once a day no
    matter how many times staff load the dashboard."""

    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="rem_gate", password="x")

    def setUp(self):
        self.today = timezone.localdate()
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Rem Cust",
            status=Reservation.Status.CONFIRMED)
        ReservationItem.objects.create(
            reservation=reservation, gown_name="Gate Gown",
            stage=ReservationItem.Stage.RESERVED,
            rental_date=self.today - timedelta(days=3), event_date=self.today,
            return_date=self.today + timedelta(days=1),
            overdue_date=self.today + timedelta(days=2))

    def test_the_first_call_of_the_day_sends(self):
        self.assertEqual(reminders.run_daily_sweep_if_due(self.today), 1)

    def test_later_calls_the_same_day_do_nothing_at_all(self):
        reminders.run_daily_sweep_if_due(self.today)
        self.assertIsNone(reminders.run_daily_sweep_if_due(self.today))
        self.assertIsNone(reminders.run_daily_sweep_if_due(self.today))
        self.assertEqual(CustomerMessage.objects.count(), 1)

    def test_a_new_day_unlocks_the_sweep_again(self):
        reminders.run_daily_sweep_if_due(self.today)
        self.assertIsNotNone(reminders.run_daily_sweep_if_due(self.today + timedelta(days=1)))

    def test_the_day_is_claimed_before_sending_so_a_crash_cannot_double_send(self):
        """Two staff opening the dashboard at the same instant must not both decide the
        sweep has not run. The claim is a conditional UPDATE only one can win, so even a
        sweep that blows up afterwards costs one skipped day rather than two of every
        message."""
        with patch.object(reminders, "send_due_reminders", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                reminders.run_daily_sweep_if_due(self.today)

        self.assertEqual(ReminderRun.load().last_run_on, self.today)
        self.assertIsNone(reminders.run_daily_sweep_if_due(self.today))
        self.assertEqual(CustomerMessage.objects.count(), 0)

    def test_the_run_marker_records_what_happened(self):
        reminders.run_daily_sweep_if_due(self.today)
        run = ReminderRun.load()
        self.assertEqual(run.last_run_on, self.today)
        self.assertEqual(run.last_sent_count, 1)
        self.assertIsNotNone(run.last_run_at)

    def test_reminder_run_stays_a_singleton(self):
        reminders.run_daily_sweep_if_due(self.today)
        ReminderRun(last_run_on=self.today).save()
        self.assertEqual(ReminderRun.objects.count(), 1)
