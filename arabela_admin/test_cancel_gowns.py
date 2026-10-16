"""Cancel gown: staff cancel just the gowns nobody picked up in a booking where other gowns went out or came back (or a booking with
several gowns where only some were collected), one by one or all at once. The booking carries on with the rest; the cancelled gowns
stop holding anything; the customer is told which gowns and why; and the last gown left cancels the whole booking.

Also covers every page that has to treat a cancelled gown as "gone, not rented": availability, the Rental Schedule, Rental History,
the bell, the delete guard, Reservation Records, the deposit, the buttons on Active Reservations, the old actions on a stale page,
and what the CUSTOMER sees on their own pages."""
import json
import re
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomerMessage
from accounts.services import count_cancellations
from arabela_admin.notifications import build_items
from arabela_admin.test_cancel_booking import Base, EMAILS_ON, make_gown
from arabela_admin import views as admin_views
from gowns import availability
from gowns.models import Gown, GownUnavailability
from gowns.views import _UNIT_ASSIGNED, _UNIT_UNAVAILABLE, _find_available_unit
from reservations import cancellation
from reservations.models import Reservation, ReservationItem, ReservationStatusEvent

PICKUP, OUT, RETURNED, CANCELLED = (
    ReservationItem.Stage.PICKUP, ReservationItem.Stage.RESERVED, ReservationItem.Stage.RETURNED, ReservationItem.Stage.CANCELLED,
)


class GownBase(Base):
    def cancel_gowns(self, reservation, items, *, reason="no_show", note="", raw=None):
        body = {"reason": reason, "note": note, "items": [i.pk for i in items]} if raw is None else raw
        return self.client.post(
            reverse("arabela_admin:reservation_cancel_gowns", args=[reservation.pk]),
            data=json.dumps(body), content_type="application/json")

    def stages(self, items):
        return [ReservationItem.objects.get(pk=i.pk).stage for i in items]

    def mark(self, item, stage):
        extra = {}
        if stage == RETURNED:
            extra = {"picked_up_on": self.today - timedelta(days=20), "returned_on": self.today - timedelta(days=10)}
        elif stage == OUT:
            extra = {"picked_up_on": self.today - timedelta(days=20)}
        ReservationItem.objects.filter(pk=item.pk).update(stage=stage, **extra)

    def mixed(self, gowns=4):
        """A booking of `gowns` gowns, pick-up long past: the first came back, the second is still out, the rest never collected."""
        reservation, items = self.booking(gowns=gowns)
        self.mark(items[0], RETURNED)
        self.mark(items[1], OUT)
        return reservation, items

    def page(self):
        return self.client.get(reverse("arabela_admin:active_reservations")).content.decode()


class CancelGownsEndpointTests(GownBase):
    def test_one_gown_is_cancelled_and_the_rest_of_the_booking_carries_on(self):
        reservation, items = self.booking(gowns=3)
        response = self.cancel_gowns(reservation, [items[1]])
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body["count"], body["gowns"], body["booking_cancelled"], body["booking_complete"]),
                         (1, "Cancel Gown 2", False, False))
        self.assertEqual(self.stages(items), [PICKUP, CANCELLED, PICKUP])
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)                # the booking itself is untouched
        items[1].gown.refresh_from_db()
        items[0].gown.refresh_from_db()
        self.assertEqual((items[1].gown.status, items[0].gown.status), (Gown.Status.AVAILABLE, Gown.Status.RESERVED))
        self.assertFalse(GownUnavailability.objects.filter(auto_for_item=items[1]).exists())   # nothing to rest from
        self.assertTrue(GownUnavailability.objects.filter(auto_for_item=items[0]).exists())

    def test_the_timeline_says_which_gown_and_who_and_why_without_showing_the_customer_the_staff_note(self):
        reservation, items = self.booking(gowns=3)
        self.cancel_gowns(reservation, [items[1]], note="Called twice, no answer")
        shop = ReservationStatusEvent.objects.get(reservation=reservation, label=cancellation.GOWN_CANCEL_LABEL)
        self.assertEqual((shop.detail, shop.actor, shop.staff_only, shop.item_id),
                         ("Cancel Gown 2: The gown was not picked up.", "Staff", False, None))
        note = ReservationStatusEvent.objects.get(reservation=reservation, label=cancellation.GOWN_STAFF_NOTE_LABEL)
        self.assertEqual(note.detail, "Maria Cruz: Cancel Gown 2 - The gown was not picked up - Called twice, no answer")
        self.assertTrue(note.staff_only)
        # it is NOT written up as the whole booking being cancelled
        self.assertFalse(ReservationStatusEvent.objects.filter(
            reservation=reservation, label__in=[cancellation.SHOP_CANCEL_LABEL, cancellation.STAFF_NOTE_LABEL]).exists())

    def test_the_customer_is_told_by_message_and_email_that_only_that_gown_was_cancelled(self):
        reservation, items = self.booking(gowns=3)
        with override_settings(**EMAILS_ON), self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.cancel_gowns(reservation, [items[1]]).status_code, 200)
        message = CustomerMessage.objects.get(recipient=self.customer)
        self.assertEqual(message.category, CustomerMessage.Category.GENERAL)
        for text in ("Cancel Gown 2", reservation.reference_code, "The gown was not picked up.", "The rest of your reservation is unchanged"):
            self.assertIn(text, message.body)
        self.assertNotIn("security deposit is not refunded", message.body)        # that is for a whole booking, not one gown
        [email] = mail.outbox
        self.assertEqual(email.to, ["denmark@gmail.com"])
        self.assertEqual(email.subject, f"A gown in your reservation {reservation.reference_code} was cancelled")
        self.assertIn("Cancel Gown 2", email.body)
        self.assertIn("The gown was not picked up.", email.body)
        self.assertIn("The rest of your reservation is unchanged", email.body)
        self.assertNotIn("Your reservation was cancelled", email.body)           # it was NOT -- only one gown was

    def test_several_gowns_at_once_are_one_message_one_email_and_one_timeline_line(self):
        reservation, items = self.booking(gowns=6)
        with override_settings(**EMAILS_ON), self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.cancel_gowns(reservation, items[:5]).status_code, 200)
        self.assertEqual(self.stages(items), [CANCELLED] * 5 + [PICKUP])
        self.assertEqual(CustomerMessage.objects.filter(recipient=self.customer).count(), 1)
        self.assertEqual(len(mail.outbox), 1)
        event = ReservationStatusEvent.objects.get(reservation=reservation, label=cancellation.GOWNS_CANCEL_LABEL)
        self.assertEqual(event.detail, "Cancel Gown 1, Cancel Gown 2 and 3 more: The gowns were not picked up.")   # fits one line
        message = CustomerMessage.objects.get(recipient=self.customer)
        for n in range(1, 6):
            self.assertIn(f"Cancel Gown {n}", message.body)                       # the message names every one of them
        self.assertNotIn("Cancel Gown 6", message.body)

    def test_two_different_gowns_cancelled_one_after_the_other_are_two_real_notices(self):
        reservation, items = self.booking(gowns=3)
        with override_settings(**EMAILS_ON), self.captureOnCommitCallbacks(execute=True):
            self.cancel_gowns(reservation, [items[0]])
            self.cancel_gowns(reservation, [items[1]])
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("Cancel Gown 1", mail.outbox[0].body)
        self.assertIn("Cancel Gown 2", mail.outbox[1].body)

    def test_the_reason_the_customer_is_told_follows_what_staff_chose(self):
        reservation, items = self.booking(gowns=3)
        self.cancel_gowns(reservation, [items[0]], reason="customer_asked")
        self.cancel_gowns(reservation, [items[1]], reason="other", note="The gown is no longer available")
        details = list(ReservationStatusEvent.objects.filter(
            reservation=reservation, label=cancellation.GOWN_CANCEL_LABEL).order_by("id").values_list("detail", flat=True))
        self.assertEqual(details, ["Cancel Gown 1: You asked us to cancel.", "Cancel Gown 2: The gown is no longer available"])

    def test_a_gown_that_already_went_out_or_came_back_or_was_cancelled_is_refused_and_nothing_changes(self):
        reservation, items = self.mixed(gowns=4)
        self.cancel_gowns(reservation, [items[3]])                                  # cancelled once...
        for item, status, words in ((items[0], 409, "already returned"),
                                    (items[1], 400, "already gone out"),
                                    (items[3], 409, "already cancelled")):
            response = self.cancel_gowns(reservation, [item])
            self.assertEqual(response.status_code, status, (item.gown_name, response.content))
            self.assertIn(words, response.json()["error"])
        self.assertEqual(self.stages(items), [RETURNED, OUT, PICKUP, CANCELLED])

    def test_one_bad_gown_in_the_list_means_none_are_cancelled(self):
        reservation, items = self.mixed(gowns=4)
        response = self.cancel_gowns(reservation, [items[2], items[1]])             # the second one is out with the customer
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stages(items), [RETURNED, OUT, PICKUP, PICKUP])
        self.assertFalse(CustomerMessage.objects.filter(recipient=self.customer).exists())

    def test_a_gown_from_another_booking_is_refused(self):
        reservation, items = self.booking(gowns=2)
        other, [stranger] = self.booking(gowns=1, number_from=30)
        response = self.cancel_gowns(reservation, [stranger])
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.stages([stranger]), [PICKUP])

    def test_a_gown_that_changed_while_the_window_was_open_is_refused_and_nothing_is_cancelled(self):
        reservation, items = self.booking(gowns=3)
        real = cancellation._gowns_to_cancel

        def stale(all_items, ids):
            chosen = real(all_items, ids)
            ReservationItem.objects.filter(pk=chosen[1].pk).update(stage=OUT)       # somebody marked it picked up a moment ago
            return chosen

        with patch.object(cancellation, "_gowns_to_cancel", stale):
            response = self.cancel_gowns(reservation, items[:2])
        self.assertEqual(response.status_code, 409)
        self.assertIn("changed while you were working", response.json()["error"])
        self.assertEqual(self.stages(items)[0], PICKUP)                              # the other one was not cancelled either
        self.assertFalse(CustomerMessage.objects.filter(recipient=self.customer).exists())

    def test_a_reason_is_required_and_another_reason_needs_words_the_customer_will_read(self):
        reservation, items = self.booking(gowns=3)
        self.assertEqual(self.cancel_gowns(reservation, [items[0]], reason="").status_code, 400)
        self.assertEqual(self.cancel_gowns(reservation, [items[0]], reason="made-up").status_code, 400)
        self.assertEqual(self.cancel_gowns(reservation, [items[0]], reason="other", note="   ").status_code, 400)
        self.assertEqual(self.stages(items), [PICKUP] * 3)

    def test_a_bad_request_is_a_clear_error_not_a_crash(self):
        reservation, items = self.booking(gowns=3)
        url = reverse("arabela_admin:reservation_cancel_gowns", args=[reservation.pk])
        for raw in ({"reason": "no_show"}, {"reason": "no_show", "items": []}, {"reason": "no_show", "items": ["x"]},
                    {"reason": "no_show", "items": [True]}, {"reason": "no_show", "items": "5"}, {"reason": "no_show", "items": [1.5]}):
            response = self.client.post(url, data=json.dumps(raw), content_type="application/json")
            self.assertEqual(response.status_code, 400, raw)
            self.assertIn("error", response.json())
        for body in ("not json", "[1, 2]"):
            self.assertEqual(self.client.post(url, data=body, content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post(reverse("arabela_admin:reservation_cancel_gowns", args=[999999]),
                                          data=json.dumps({"reason": "no_show", "items": [1]}),
                                          content_type="application/json").status_code, 404)
        self.assertEqual(self.stages(items), [PICKUP] * 3)

    def test_only_staff_can_do_it(self):
        reservation, items = self.booking(gowns=3)
        self.client.logout()
        self.assertEqual(self.cancel_gowns(reservation, [items[0]]).status_code, 401)
        self.client.force_login(self.customer)
        self.assertEqual(self.cancel_gowns(reservation, [items[0]]).status_code, 401)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("arabela_admin:reservation_cancel_gowns", args=[reservation.pk])).status_code, 405)
        self.assertEqual(self.stages(items), [PICKUP] * 3)

    def test_it_only_works_on_an_approved_booking(self):
        for number, (status, expected) in zip((70, 75, 80), (
                (Reservation.Status.PENDING, 400), (Reservation.Status.CANCELLED, 409), (Reservation.Status.REJECTED, 400))):
            reservation, items = self.booking(gowns=2, status=status, number_from=number)
            response = self.cancel_gowns(reservation, [items[0]])
            self.assertEqual(response.status_code, expected, status)
            self.assertEqual(self.stages(items), [PICKUP, PICKUP])

    def test_a_gown_another_booking_still_needs_stays_reserved(self):
        reservation, items = self.booking(gowns=2)
        later = Reservation.objects.create(customer=self.customer, customer_name="Denmark Concepcion", status=Reservation.Status.CONFIRMED)
        ReservationItem.objects.create(
            reservation=later, gown=items[0].gown, gown_name="Cancel Gown 1", rental_date=self.today + timedelta(days=30),
            return_date=self.today + timedelta(days=34))
        self.cancel_gowns(reservation, [items[0]])
        items[0].gown.refresh_from_db()
        self.assertEqual(items[0].gown.status, Gown.Status.RESERVED)                  # the later booking still needs it


class LastGownTests(GownBase):
    def test_cancelling_the_last_gown_left_cancels_the_whole_booking_like_a_whole_cancel(self):
        reservation, items = self.booking(gowns=2)
        before = count_cancellations(self.customer)
        first = self.cancel_gowns(reservation, [items[0]])
        self.assertFalse(first.json()["booking_cancelled"])
        self.assertEqual(count_cancellations(self.customer), before)                 # one gown is not a cancelled reservation
        second = self.cancel_gowns(reservation, [items[1]], note="Called twice")
        self.assertEqual(second.status_code, 200, second.content)
        self.assertTrue(second.json()["booking_cancelled"])
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CANCELLED)
        self.assertEqual(count_cancellations(self.customer), before + 1)             # the booking is, though
        self.assertEqual(self.stages(items), [CANCELLED, CANCELLED])
        shop = ReservationStatusEvent.objects.get(reservation=reservation, label=cancellation.SHOP_CANCEL_LABEL)
        self.assertEqual(shop.detail, "The gown was not picked up.")
        note = ReservationStatusEvent.objects.get(reservation=reservation, label=cancellation.STAFF_NOTE_LABEL)
        self.assertEqual(note.detail, "Maria Cruz: The gown was not picked up - Called twice")
        whole = CustomerMessage.objects.filter(recipient=self.customer).order_by("id").last()
        self.assertIn("was cancelled by Arabela", whole.body)
        self.assertIn("not refunded", whole.body)                                    # now it IS the whole booking

    def test_every_gown_at_once_is_the_same_as_cancelling_the_whole_booking(self):
        reservation, items = self.booking(gowns=3)
        with override_settings(**EMAILS_ON), self.captureOnCommitCallbacks(execute=True):
            response = self.cancel_gowns(reservation, items)
        self.assertTrue(response.json()["booking_cancelled"])
        self.assertEqual(CustomerMessage.objects.filter(recipient=self.customer).count(), 1)
        [email] = mail.outbox
        self.assertEqual(email.subject, f"Your reservation {reservation.reference_code} was cancelled")

    def test_when_the_other_gowns_are_all_back_the_booking_is_finished_and_the_deposit_settles_once(self):
        reservation, items = self.booking(gowns=3)
        self.mark(items[0], RETURNED)
        with override_settings(**EMAILS_ON), self.captureOnCommitCallbacks(execute=True):
            response = self.cancel_gowns(reservation, items[1:])
        body = response.json()
        self.assertEqual((body["booking_cancelled"], body["booking_complete"]), (False, True))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
        self.assertEqual(ReservationStatusEvent.objects.filter(reservation=reservation, label="Security deposit settled").count(), 1)
        self.assertIsNotNone(reservation.deposit_returned_on)
        self.assertEqual(sorted(m.subject for m in mail.outbox),
                         sorted([f"A gown in your reservation {reservation.reference_code} was cancelled",
                                 f"Your security deposit is settled - {reservation.reference_code}"]))

    def test_the_deposit_still_waits_for_a_gown_that_is_out(self):
        reservation, items = self.booking(gowns=3)
        self.mark(items[0], OUT)
        self.cancel_gowns(reservation, items[1:])
        reservation.refresh_from_db()
        self.assertIsNone(reservation.deposit_returned_on)
        self.assertFalse(ReservationStatusEvent.objects.filter(reservation=reservation, label="Security deposit settled").exists())

    def test_returning_the_last_gown_settles_the_deposit_even_when_a_sibling_was_cancelled(self):
        reservation, items = self.booking(gowns=2)
        self.mark(items[0], OUT)
        self.cancel_gowns(reservation, [items[1]])
        response = self.client.post(reverse("arabela_admin:reservation_item_mark_returned", args=[items[0].pk]),
                                    data=json.dumps({"condition": "Good"}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        reservation.refresh_from_db()
        self.assertIsNotNone(reservation.deposit_returned_on)
        self.assertEqual(ReservationStatusEvent.objects.filter(reservation=reservation, label="Security deposit settled").count(), 1)

    def test_a_whole_booking_cancel_still_works_after_one_gown_was_cancelled_and_leaves_that_gown_alone(self):
        reservation, items = self.booking(gowns=3)
        self.cancel_gowns(reservation, [items[0]])
        self.assertIn(reverse("arabela_admin:reservation_cancel", args=[reservation.pk]), self.page())   # the button is back
        self.assertEqual(self.cancel(reservation).status_code, 200)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CANCELLED)
        self.assertEqual(self.stages(items), [CANCELLED, PICKUP, PICKUP])
        whole = CustomerMessage.objects.filter(recipient=self.customer).order_by("id").last()
        self.assertIn("Cancel Gown 2", whole.body)                                   # only what was still part of it
        self.assertIn("Cancel Gown 3", whole.body)
        self.assertNotIn("Cancel Gown 1", whole.body)


class StalePageTests(GownBase):
    """A cancelled gown can never be "rented" afterwards by a button on a page that was opened before."""

    def setUp(self):
        super().setUp()
        self.reservation, self.items = self.booking(gowns=3)
        self.target = self.items[1]
        self.cancel_gowns(self.reservation, [self.target])

    def post(self, name, body=None):
        return self.client.post(reverse(f"arabela_admin:{name}", args=[self.target.pk]),
                                data=json.dumps(body or {}), content_type="application/json")

    def test_none_of_the_old_buttons_can_bring_a_cancelled_gown_back(self):
        future = (self.today + timedelta(days=3)).isoformat()
        for name, body in (("reservation_item_mark_returned", {"condition": "Good"}), ("reservation_item_mark_picked_up", {}),
                           ("reservation_item_change_pickup", {"date": future}), ("reservation_item_change_return", {"date": future}),
                           ("reservation_item_send_reminder", {"body": "Hello"}), ("reservation_item_undo_pickup", {})):
            response = self.post(name, body)
            self.assertIn(response.status_code, (400, 409), name)
            self.assertIn("error", response.json(), name)
            self.assertEqual(self.stages([self.target]), [CANCELLED], name)
        self.assertEqual(ReservationItem.objects.get(pk=self.target.pk).picked_up_on, None)
        self.assertEqual(ReservationItem.objects.get(pk=self.target.pk).returned_on, None)

    def test_marking_picked_up_cannot_overwrite_a_cancel_that_happened_a_moment_earlier(self):
        item = self.items[2]
        real = admin_views._load_open_item

        def stale(request, item_id):
            loaded, error = real(request, item_id)
            ReservationItem.objects.filter(pk=item_id).update(stage=CANCELLED)       # the cancel lands just after the page loaded it
            return loaded, error

        past = self.today - timedelta(days=1)
        ReservationItem.objects.filter(pk=item.pk).update(rental_date=past)
        with patch.object(admin_views, "_load_open_item", stale):
            response = self.client.post(reverse("arabela_admin:reservation_item_mark_picked_up", args=[item.pk]))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.stages([item]), [CANCELLED])                           # still cancelled, NOT quietly turned into picked up
        self.assertIsNone(ReservationItem.objects.get(pk=item.pk).picked_up_on)

    def test_marking_picked_up_still_works_normally(self):
        item = self.items[0]
        ReservationItem.objects.filter(pk=item.pk).update(rental_date=self.today - timedelta(days=1))
        response = self.client.post(reverse("arabela_admin:reservation_item_mark_picked_up", args=[item.pk]))
        self.assertEqual(response.status_code, 200, response.content)
        refreshed = ReservationItem.objects.get(pk=item.pk)
        self.assertEqual((refreshed.stage, refreshed.picked_up_on), (OUT, self.today))
        self.assertTrue(ReservationStatusEvent.objects.filter(reservation=self.reservation, item=item, label__endswith="picked up").exists())


class CancelledGownIsGoneEverywhereTests(GownBase):
    def test_its_dates_are_open_again_for_other_customers(self):
        reservation, items = self.booking(gowns=2, days_late=-5)
        gown_ids = [items[0].gown_id, items[1].gown_id]
        bookings, blocks = availability.load_busy_ranges(gown_ids, self.today, 120)
        self.assertEqual((len(bookings[gown_ids[0]]), len(bookings[gown_ids[1]])), (1, 1))
        self.cancel_gowns(reservation, [items[0]])
        bookings, blocks = availability.load_busy_ranges(gown_ids, self.today, 120)
        self.assertEqual(len(bookings.get(gown_ids[0], [])), 0)
        self.assertEqual(len(blocks.get(gown_ids[0], [])), 0)                        # and its rest block is gone too
        self.assertEqual(len(bookings[gown_ids[1]]), 1)                              # the other gown is still booked

    def test_checkout_can_hand_the_cancelled_gown_to_someone_else_for_those_dates(self):
        reservation, items = self.booking(gowns=2, days_late=-5)
        rental, back = items[0].rental_date, items[0].return_date
        gown = items[0].gown
        outcome = lambda: _find_available_unit(gown.name, rental, back, gown_slug=gown.slug, size="M")
        self.assertEqual(outcome()[0], _UNIT_UNAVAILABLE)                            # both units are booked
        self.cancel_gowns(reservation, [items[0]])
        result, unit = outcome()
        self.assertEqual((result, unit.pk), (_UNIT_ASSIGNED, items[0].gown_id))

    def test_the_rest_days_after_a_rental_no_longer_count_a_cancelled_booking(self):
        solo = make_gown(90, name="Solo Cancel Gown")
        other = make_gown(91, name="Other Cancel Gown")
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Denmark Concepcion",
                                                 status=Reservation.Status.CONFIRMED, reviewed_at=timezone.now())
        later = ReservationItem.objects.create(reservation=reservation, gown=solo, gown_name=solo.name,
                                               rental_date=self.today + timedelta(days=20), return_date=self.today + timedelta(days=24))
        ReservationItem.objects.create(reservation=reservation, gown=other, gown_name=other.name,
                                       rental_date=self.today + timedelta(days=20), return_date=self.today + timedelta(days=24))
        rental, back = self.today + timedelta(days=10), self.today + timedelta(days=14)
        outcome = lambda: _find_available_unit(solo.name, rental, back, gown_slug=solo.slug, size="M", rest_days=7)[0]
        self.assertEqual(outcome(), _UNIT_UNAVAILABLE)                               # its rest days would run into that booking
        self.cancel_gowns(reservation, [later])
        self.assertEqual(outcome(), _UNIT_ASSIGNED)

    def test_staff_conflict_checks_and_the_delete_guard_ignore_it(self):
        reservation, items = self.booking(gowns=2, days_late=-5)
        gown = items[0].gown
        self.assertIsNotNone(admin_views._gown_blocking_reservation_item(gown))
        self.assertEqual(admin_views._holding_items([gown.id]).count(), 1)
        self.cancel_gowns(reservation, [items[0]])
        self.assertIsNone(admin_views._gown_blocking_reservation_item(gown))         # the gown can be deleted from the catalog now
        self.assertEqual(admin_views._holding_items([gown.id]).count(), 0)

    def test_the_rental_schedule_and_rental_history_leave_it_out(self):
        reservation, items = self.booking(gowns=2, days_late=-5)
        self.cancel_gowns(reservation, [items[0]])
        events = self.client.get(reverse("arabela_admin:rental_schedule")).context["calendar_events"]
        ids = {e["extendedProps"].get("itemId") for e in events}
        self.assertIn(items[1].pk, ids)
        self.assertNotIn(items[0].pk, ids)
        history = self.client.get(reverse("arabela_admin:rental_history")).context["rental_items"]
        self.assertEqual([i.pk for i in history if i.reservation_id == reservation.pk], [items[1].pk])

    def test_it_is_never_reported_overdue_for_return_and_the_bell_follows_the_rest_of_the_booking(self):
        reservation, items = self.booking(gowns=2, days_late=21)                     # pick-up and return long past
        self.cancel_gowns(reservation, [items[0]])
        self.assertEqual([n for n in build_items(self.staff) if n["kind"] == "overdue"], [])
        [entry] = [n for n in build_items(self.staff) if n["kind"] == "late_pickup"]
        self.assertIn("never picked up the gown and the event date passed", entry["title"])     # ONE gown left: "the gown", not "the gowns"
        self.assertTrue(entry["key"].endswith(":decide"))                            # nothing went out, so Cancel booking is offered

    def test_a_booking_with_a_gown_out_stays_a_plain_warning_and_clears_once_the_rest_are_cancelled(self):
        reservation, items = self.mixed(gowns=3)
        late = lambda: [n for n in build_items(self.staff) if n["kind"] == "late_pickup"]
        self.assertEqual(late()[0]["level"], "warning")
        self.cancel_gowns(reservation, [items[2]])
        self.assertEqual(late(), [])

    def test_reservation_records_show_it_as_cancelled_and_keep_the_booking_window_to_the_rest(self):
        reservation, items = self.booking(gowns=3, days_late=10)
        ReservationItem.objects.filter(pk=items[2].pk).update(rental_date=self.today - timedelta(days=40), return_date=self.today - timedelta(days=36))
        self.mark(items[0], RETURNED)
        self.mark(items[1], RETURNED)
        self.cancel_gowns(reservation, [items[2]])
        html = self.client.get(reverse("arabela_admin:reservation_records")).content.decode()
        match = re.search(r'<script id="reservation-records-data" type="application/json">(.*?)</script>', html, re.S)
        record = {r["reference"]: r for r in json.loads(match.group(1))}[reservation.reference_code]
        self.assertEqual([i["stage"] for i in record["items"]], ["Cancelled", "Returned", "Returned"])
        self.assertEqual(record["progress"], "Completed")                            # every gown that was rented is back
        self.assertEqual(record["start"], (self.today - timedelta(days=10)).isoformat())   # not stretched back by the cancelled gown
        self.assertEqual(record["depositStatus"], "Returned")

    def test_security_deposits_lists_it_as_cancelled(self):
        reservation, items = self.booking(gowns=2)
        self.cancel_gowns(reservation, [items[0]])
        response = self.client.get(reverse("arabela_admin:security_deposits"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(">Cancelled</span>", response.content.decode())

    def test_the_deposit_never_waits_for_a_gown_that_was_cancelled(self):
        reservation, items = self.booking(gowns=2)
        self.mark(items[0], RETURNED)
        self.assertIsNone(Reservation.objects.get(pk=reservation.pk).deposit_returned_on)          # the other is still waiting
        ReservationItem.objects.filter(pk=items[1].pk).update(stage=CANCELLED)
        self.assertIsNotNone(Reservation.objects.get(pk=reservation.pk).deposit_returned_on)
        ReservationItem.objects.filter(pk=items[0].pk).update(stage=CANCELLED)
        self.assertIsNone(Reservation.objects.get(pk=reservation.pk).deposit_returned_on)          # nothing was rented at all


class ActiveReservationsButtonTests(GownBase):
    def gown_buttons(self, html):
        return re.findall(r'data-mode="gown" data-ids="(\d+)"', html)

    def test_a_mixed_booking_gets_a_cancel_button_on_each_waiting_gown_and_the_shortcut_but_not_cancel_booking(self):
        reservation, items = self.mixed(gowns=4)
        html = self.page()
        self.assertEqual(sorted(self.gown_buttons(html)), sorted([str(items[2].pk), str(items[3].pk)]))   # not the returned / out ones
        [shortcut] = re.findall(r'data-mode="gowns" data-ids="([\d,]+)"', html)
        self.assertEqual(sorted(shortcut.split(",")), sorted([str(items[2].pk), str(items[3].pk)]))
        self.assertIn("Cancel all not picked up (2)", html)
        self.assertNotIn(reverse("arabela_admin:reservation_cancel", args=[reservation.pk]), html)   # a gown went out: not as a whole
        self.assertIn(reverse("arabela_admin:reservation_cancel_gowns", args=[reservation.pk]), html)

    def test_a_booking_nobody_picked_up_has_both_ways_but_no_shortcut(self):
        reservation, items = self.booking(gowns=3)
        html = self.page()
        self.assertIn(reverse("arabela_admin:reservation_cancel", args=[reservation.pk]), html)           # Cancel booking
        self.assertEqual(sorted(self.gown_buttons(html)), sorted(str(i.pk) for i in items))               # and Cancel gown on each
        self.assertNotIn('data-mode="gowns"', html)                                                       # Cancel booking already does it

    def test_a_booking_with_one_gown_only_has_cancel_booking(self):
        reservation, items = self.booking(gowns=1)
        html = self.page()
        self.assertIn(reverse("arabela_admin:reservation_cancel", args=[reservation.pk]), html)
        self.assertEqual(self.gown_buttons(html), [])
        self.assertNotIn('data-mode="gowns"', html)

    def test_after_one_of_two_waiting_gowns_is_cancelled_the_shortcut_goes_but_the_other_keeps_its_button(self):
        reservation, items = self.mixed(gowns=4)
        self.cancel_gowns(reservation, [items[2]])
        html = self.page()
        self.assertEqual(self.gown_buttons(html), [str(items[3].pk)])
        self.assertNotIn('data-mode="gowns"', html)

    def test_a_cancelled_gown_row_says_so_and_offers_nothing(self):
        reservation, items = self.booking(gowns=3)
        self.cancel_gowns(reservation, [items[1]])
        html = self.page()
        self.assertIn("Cancelled · never picked up", html)
        self.assertNotIn(f'data-item-actions="{items[1].pk}"', html)                  # no Remind / Mark Picked Up / Cancel on it
        self.assertIn(f'data-item-actions="{items[0].pk}"', html)
        self.assertNotIn(f'data-kind="return" data-item-id="{items[1].pk}"', html)    # and no Change return date
        self.assertEqual(sorted(self.gown_buttons(html)), sorted([str(items[0].pk), str(items[2].pk)]))

    def test_the_button_is_urgent_once_the_event_date_has_passed_and_not_before(self):
        reservation, items = self.booking(gowns=2, days_late=21)
        self.assertIn('class="cb-open-btn cb-open-btn--small cb-open-btn--urgent"', self.page())
        later, later_items = self.booking(gowns=2, days_late=-5, number_from=20)
        ReservationItem.objects.filter(reservation=reservation).update(stage=CANCELLED)
        Reservation.objects.filter(pk=reservation.pk).update(status=Reservation.Status.CANCELLED)
        html = self.page()
        self.assertIn('class="cb-open-btn cb-open-btn--small"', html)
        self.assertNotIn("cb-open-btn--small cb-open-btn--urgent", html)

    def test_the_confirm_window_has_the_simple_modes(self):
        self.booking(gowns=2)
        html = self.page()
        for text in ("Cancel this gown?", "Keep gown", "no refund is given", "Cancel gown", "cb-code-wrap", "To confirm, type"):
            self.assertIn(text, html)


class CustomerSideTests(GownBase):
    def setUp(self):
        super().setUp()
        self.reservation, self.items = self.booking(gowns=3, days_late=-5)
        self.cancel_gowns(self.reservation, [self.items[1]])
        self.client.force_login(self.customer)

    def detail(self, item):
        return self.client.get(reverse("gowns:reservation_item_detail", args=[item.pk]))

    def test_the_reservations_list_marks_only_that_gown_as_cancelled(self):
        html = self.client.get(reverse("gowns:orders")).content.decode()
        cards = re.findall(r'<div class="res-card[^>]*?data-status-key="(\w+)"', html)      # real cards only (the page's CSS names the keys too)
        self.assertEqual(sorted(cards), ["cancelled", "pickup", "pickup"])           # it is under the Cancelled tab; the other two carry on
        self.assertEqual(html.count("Gown Cancelled"), 1)
        self.assertNotIn("Reservation Cancelled", html)

    def test_the_cancelled_gowns_page_says_only_this_gown_and_why_to_look_in_messages(self):
        response = self.detail(self.items[1])
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for text in ("Gown Cancelled", "Arabela cancelled this gown", "The rest of your reservation is unchanged", "Cancelled gown"):
            self.assertIn(text, html)
        self.assertNotIn("This reservation was cancelled and will not be fulfilled", html)
        self.assertNotIn("Reservation Cancelled", html)
        self.assertIn("Gown cancelled by Arabela", html)                             # the timeline entry
        self.assertIn("The gown was not picked up.", html)
        self.assertIn("&middot; Cancelled</span>", html)                             # the order summary still lists it, marked

    def test_a_sibling_gowns_page_is_not_cancelled(self):
        html = self.detail(self.items[0]).content.decode()
        self.assertIn("Ready for Pick-up", html)
        self.assertNotIn("Gown Cancelled", html)
        self.assertNotIn("Arabela cancelled this gown", html)

    def test_the_customer_can_still_cancel_what_is_left_and_the_cancelled_gown_stays_as_it_was(self):
        sibling = ReservationItem.objects.select_related("reservation").get(pk=self.items[0].pk)
        self.assertTrue(sibling.can_customer_cancel)                                 # a cancelled sibling must not block this
        self.assertFalse(ReservationItem.objects.select_related("reservation").get(pk=self.items[1].pk).can_customer_cancel)
        response = self.client.post(reverse("gowns:reservation_item_cancel", args=[sibling.pk]))
        self.assertEqual(response.status_code, 200, response.content)
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.CANCELLED)
        self.assertEqual(self.stages(self.items), [PICKUP, CANCELLED, PICKUP])

    def test_a_sibling_that_really_went_out_still_blocks_the_customers_cancel(self):
        self.mark(self.items[2], OUT)
        sibling = ReservationItem.objects.select_related("reservation").get(pk=self.items[0].pk)
        self.assertFalse(sibling.can_customer_cancel)

    def test_a_cancelled_gown_does_not_light_the_reservations_badge_but_the_message_does(self):
        from gowns.context_processors import reservations_with_news_count, unread_messages_total
        profile = self.customer.profile
        profile.reservations_last_seen_at = None
        profile.save(update_fields=["reservations_last_seen_at"])
        self.assertEqual(reservations_with_news_count(self.customer), 2)             # the two gowns still in play
        self.assertEqual(unread_messages_total(self.customer), 1)                    # and the notice about the cancelled one


class EmailPreviewTests(GownBase):
    def test_the_new_email_is_wired_to_both_labels_and_reads_right(self):
        from accounts import customer_emails as ce
        self.assertEqual(ce.EVENT_KINDS[cancellation.GOWN_CANCEL_LABEL], ce.GOWN_CANCELLED)
        self.assertEqual(ce.EVENT_KINDS[cancellation.GOWNS_CANCEL_LABEL], ce.GOWN_CANCELLED)
        self.assertEqual(ce.EVENT_KINDS[cancellation.DEPOSIT_SETTLED_LABEL], ce.DEPOSIT_SETTLED)
        built = ce.sample_email(ce.GOWN_CANCELLED)
        self.assertIn("A gown in your reservation", built.subject)
        self.assertIn("The rest of your reservation is unchanged", built.text)
        self.assertIn("The gowns were not picked up.", built.text)
        self.assertNotIn("security deposit is not refunded", built.text)

    def test_a_deposit_settled_email_does_not_list_a_cancelled_gown_as_if_it_was_rented(self):
        from accounts import customer_emails as ce
        reservation, items = self.booking(gowns=3)
        self.mark(items[0], RETURNED)
        self.cancel_gowns(reservation, items[1:])
        data = ce._reservation_data(Reservation.objects.get(pk=reservation.pk))
        self.assertEqual([gown for gown, _ in data["items"]], ["Cancel Gown 1"])
        # when everything was cancelled, the cancelled booking lists them all
        whole, whole_items = self.booking(gowns=2, number_from=40)
        self.cancel_gowns(whole, whole_items)
        data = ce._reservation_data(Reservation.objects.get(pk=whole.pk))
        self.assertEqual(len(data["items"]), 2)
