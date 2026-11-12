"""Cancellation requests: a staff account WITHOUT the "Cancel bookings" permission asks the owner to cancel a booking (or some of its gowns).

Checked here: the request leaves the booking exactly as it was; the rules for asking (right reason, something that can still be cancelled,
one waiting request per booking, only for accounts that may not cancel); approving runs the real cancellation (released gown, message to
the customer, the customer's cancellation count, who asked and who approved on the record) and is re-checked under a lock so a booking that
changed in the meantime closes the request instead of cancelling by surprise; declining needs a note staff can read in their bell; the
requester can withdraw; only the owner answers (even a staff account with every permission cannot); a double tap cancels once; the page shows the
right buttons to the right person; the bell; the one 12-hour reminder email; and the timeline and Audit Log lines."""
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditLog, CustomerMessage, UserProfile
from accounts.roles import PERMISSION_KEYS
from accounts.services import count_cancellations
from arabela_admin.notifications import build_items
from arabela_admin.test_cancel_booking import Base, make_gown
from arabela_admin.test_role_permissions import make_staff
from gowns.models import Gown
from reservations import cancel_requests
from reservations.models import CancelRequest, Reservation, ReservationItem, ReservationStatusEvent

User = get_user_model()
ON = dict(CUSTOMER_EMAILS_ENABLED=True, CUSTOMER_EMAILS_BACKGROUND=False, SITE_BASE_URL="https://arabela.example.org",
          DEFAULT_FROM_EMAIL="Arabela <shop@gmail.com>")


class RequestBase(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.maria = make_staff("cr_maria", first="Maria", last="Lopez")                       # may not cancel
        cls.pedro = make_staff("cr_pedro", first="Pedro", last="Reyes")
        cls.senior = make_staff("cr_senior", PERMISSION_KEYS, first="Ada", last="Senior")      # may cancel, but may not answer a request
        cls.boss = User.objects.create_user("cr_boss", "boss@gmail.com", "x", is_staff=True, first_name="Olga", last_name="Owner")
        UserProfile.objects.update_or_create(user=cls.boss, defaults={"role": UserProfile.Role.OWNER})

    def setUp(self):
        super().setUp()
        self.n = 1

    def fresh_booking(self, gowns=1, **kw):
        self.n += 10
        return self.booking(number_from=self.n, gowns=gowns, **kw)

    def post(self, name, *args, body=None, user=None):
        if user is not None:
            self.client.force_login(user)
        return self.client.post(reverse(f"arabela_admin:{name}", args=args), data=json.dumps(body or {}), content_type="application/json")

    def ask(self, reservation, user=None, **body):
        body = {"reason": "no_show", "note": "Called twice, no answer", **body}
        return self.post("cancel_request_create", reservation.pk, body=body, user=user or self.maria)

    def waiting(self, reservation):
        return CancelRequest.objects.get(reservation=reservation, status=CancelRequest.Status.PENDING)


class AskingTests(RequestBase):
    def test_a_request_leaves_the_booking_exactly_as_it_was(self):
        reservation, [item] = self.fresh_booking()
        response = self.ask(reservation)
        self.assertEqual(response.status_code, 200, response.content)
        reservation.refresh_from_db()
        item.refresh_from_db()
        item.gown.refresh_from_db()
        self.assertEqual((reservation.status, item.stage, item.gown.status), (Reservation.Status.CONFIRMED, ReservationItem.Stage.PICKUP, Gown.Status.RESERVED))
        self.assertEqual(CustomerMessage.objects.count(), 0)                                  # the customer is told nothing yet
        waiting = self.waiting(reservation)
        self.assertEqual((waiting.requested_by, waiting.requested_by_name, waiting.reason_key, waiting.note, waiting.item_ids),
                         (self.maria, "Maria Lopez", "no_show", "Called twice, no answer", ""))
        event = reservation.status_events.get(label=cancel_requests.REQUESTED_LABEL)
        self.assertTrue(event.staff_only)
        self.assertIn("Maria Lopez asked to cancel the whole booking", event.detail)

    def test_the_request_is_written_to_the_audit_log_as_important(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        row = AuditLog.objects.get(action="cancel_request.create")
        self.assertEqual((row.actor_name, row.sensitive, row.target_label, row.category), ("Maria Lopez", True, reservation.reference_code, "Bookings"))
        self.assertIn("asked the owner to cancel booking " + reservation.reference_code, row.summary)

    def test_an_account_that_may_cancel_has_nothing_to_ask(self):
        reservation, _ = self.fresh_booking()
        for who in (self.senior, self.owner, self.boss):
            response = self.ask(reservation, user=who)
            self.assertEqual(response.status_code, 400)
            self.assertIn("allowed to cancel this yourself", response.json()["error"])
        self.assertEqual(CancelRequest.objects.count(), 0)

    def test_nobody_signed_in_cannot_ask(self):
        reservation, _ = self.fresh_booking()
        self.client.logout()
        self.assertEqual(self.post("cancel_request_create", reservation.pk, body={"reason": "no_show"}).status_code, 401)

    def test_the_reason_rules_are_the_cancel_windows(self):
        reservation, _ = self.fresh_booking()
        self.assertEqual(self.ask(reservation, reason="nonsense").status_code, 400)
        response = self.ask(reservation, reason="other", note="")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Please write the reason in the note.")
        self.assertEqual(self.ask(reservation, reason="other", note="Customer moved abroad").status_code, 200)
        self.assertEqual(CancelRequest.objects.get().note, "Customer moved abroad")

    def test_the_note_is_cut_to_the_same_length_as_a_cancel_note(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation, note="x" * 400)
        self.assertEqual(len(CancelRequest.objects.get().note), 150)

    def test_only_one_request_waits_per_booking(self):
        reservation, _ = self.fresh_booking()
        self.assertEqual(self.ask(reservation).status_code, 200)
        for who in (self.maria, self.pedro):
            response = self.ask(reservation, user=who)
            self.assertEqual((response.status_code, response.json()["error"]), (409, "A cancellation request is already waiting for this booking."))
        self.assertEqual(CancelRequest.objects.count(), 1)

    def test_two_requests_at_the_same_instant_leave_one_the_database_enforces_it(self):
        reservation, _ = self.fresh_booking()
        self.assertEqual(self.ask(reservation).status_code, 200)
        with patch("reservations.cancel_requests.pending_for", return_value=None):             # the second request does not see the first ...
            response = self.ask(reservation, user=self.pedro)
        self.assertEqual(response.status_code, 409)                                           # ... and the unique constraint refuses it
        self.assertEqual(CancelRequest.objects.filter(status="Pending").count(), 1)

    def test_a_booking_that_cannot_be_cancelled_whole_is_refused_with_the_real_reason(self):
        reservation, [item] = self.fresh_booking(stage=ReservationItem.Stage.RESERVED)         # already picked up
        response = self.ask(reservation)
        self.assertEqual(response.status_code, 400)
        self.assertIn("already gone out", response.json()["error"])
        cancelled, _ = self.fresh_booking(status=Reservation.Status.CANCELLED)
        self.assertEqual(self.ask(cancelled).status_code, 409)
        pending, _ = self.fresh_booking(status=Reservation.Status.PENDING, verified=False, days_late=-5)
        self.assertIn("Reject", self.ask(pending).json()["error"])
        self.assertEqual(self.post("cancel_request_create", 99999, body={"reason": "no_show"}, user=self.maria).status_code, 404)
        self.assertEqual(CancelRequest.objects.count(), 0)

    def test_a_request_for_some_gowns_names_them_and_checks_each_one(self):
        reservation, items = self.fresh_booking(gowns=3)
        other, [stranger] = self.fresh_booking()
        self.assertEqual(self.ask(reservation, items=[stranger.pk]).status_code, 404)           # not part of this booking
        self.assertEqual(self.ask(reservation, items=[]).status_code, 400)
        self.assertEqual(self.ask(reservation, items=["a"]).status_code, 400)
        self.assertEqual(self.ask(reservation, items=[items[0].pk, items[1].pk]).status_code, 200)
        waiting = self.waiting(reservation)
        self.assertEqual(waiting.item_id_list, [items[0].pk, items[1].pk])
        self.assertFalse(waiting.is_whole_booking)
        self.assertEqual(cancel_requests.what_text(waiting), f"{items[0].gown_name}, {items[1].gown_name}")

    def test_an_item_that_already_went_out_cannot_be_requested(self):
        reservation, items = self.fresh_booking(gowns=2)
        ReservationItem.objects.filter(pk=items[0].pk).update(stage=ReservationItem.Stage.RESERVED)
        response = self.ask(reservation, items=[items[0].pk])
        self.assertEqual(response.status_code, 400)
        self.assertIn("already gone out", response.json()["error"])


class ApprovingTests(RequestBase):
    def test_approving_cancels_the_booking_for_real(self):
        reservation, [item] = self.fresh_booking()
        self.ask(reservation)
        waiting = self.waiting(reservation)
        before = count_cancellations(self.customer)
        with override_settings(**ON), self.captureOnCommitCallbacks(execute=True):
            response = self.post("cancel_request_approve", waiting.pk, user=self.boss)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["outcome"], "approved")
        reservation.refresh_from_db()
        item.gown.refresh_from_db()
        self.assertEqual((reservation.status, item.gown.status), (Reservation.Status.CANCELLED, Gown.Status.AVAILABLE))
        self.assertEqual(count_cancellations(self.customer), before + 1)
        message = CustomerMessage.objects.get(recipient=self.customer)
        self.assertIn("not refunded", message.body)
        self.assertTrue(any("was cancelled" in m.subject for m in mail.outbox))
        waiting.refresh_from_db()
        self.assertEqual((waiting.status, waiting.decided_by, waiting.decided_by_name), ("Approved", self.boss, "Olga Owner"))
        note = reservation.status_events.get(label="Booking cancelled by staff")
        self.assertEqual(note.detail, "Maria Lopez, approved by Olga Owner: The gown was not picked up - Called twice, no answer")
        self.assertTrue(reservation.status_events.filter(label=cancel_requests.APPROVED_LABEL, staff_only=True).exists())
        row = AuditLog.objects.get(action="cancel_request.approve")
        self.assertEqual((row.actor_name, row.sensitive, row.target_label), ("Olga Owner", True, reservation.reference_code))
        self.assertIn("approved the request of Maria Lopez to cancel booking " + reservation.reference_code, row.summary)

    def test_the_superuser_owner_can_answer_too(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        self.assertEqual(self.post("cancel_request_approve", self.waiting(reservation).pk, user=self.owner).status_code, 200)

    def test_approving_a_request_for_some_gowns_cancels_only_those(self):
        reservation, items = self.fresh_booking(gowns=3)
        self.ask(reservation, items=[items[1].pk])
        response = self.post("cancel_request_approve", self.waiting(reservation).pk, user=self.boss)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()["count"], response.json()["booking_cancelled"]), (1, False))
        stages = {i.pk: ReservationItem.objects.get(pk=i.pk).stage for i in items}
        self.assertEqual(stages, {items[0].pk: "Pick-up", items[1].pk: "Cancelled", items[2].pk: "Pick-up"})
        self.assertEqual(Reservation.objects.get(pk=reservation.pk).status, Reservation.Status.CONFIRMED)

    def test_a_booking_that_changed_before_the_owner_answered_closes_the_request_and_cancels_nothing(self):
        reservation, [item] = self.fresh_booking()
        self.ask(reservation)
        waiting = self.waiting(reservation)
        ReservationItem.objects.filter(pk=item.pk).update(stage=ReservationItem.Stage.RESERVED)          # picked up in the meantime
        response = self.post("cancel_request_approve", waiting.pk, user=self.boss)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["outcome"], "closed")
        self.assertIn("already gone out", response.json()["message"])
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, "Closed")
        self.assertIn("already gone out", waiting.decision_note)
        self.assertEqual(Reservation.objects.get(pk=reservation.pk).status, Reservation.Status.CONFIRMED)
        self.assertEqual(CustomerMessage.objects.count(), 0)
        self.assertTrue(reservation.status_events.filter(label=cancel_requests.CLOSED_LABEL).exists())
        self.assertIn("no longer possible", AuditLog.objects.get(action="cancel_request.approve").summary)

    def test_a_booking_cancelled_directly_in_the_meantime_closes_the_request(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        waiting = self.waiting(reservation)
        self.assertEqual(self.post("reservation_cancel", reservation.pk, user=self.senior, body={
            "reason": "no_show", "note": "", "confirm": reservation.reference_code}).status_code, 200)
        self.assertEqual(cancel_requests.pending().count(), 0)                                 # no longer shown as waiting
        response = self.post("cancel_request_approve", waiting.pk, user=self.boss)
        self.assertEqual(response.json()["outcome"], "closed")
        self.assertEqual(CustomerMessage.objects.count(), 1)                                   # told once, by the direct cancel

    def test_a_double_tap_cancels_once(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        waiting = self.waiting(reservation)
        first = self.post("cancel_request_approve", waiting.pk, user=self.boss)
        second = self.post("cancel_request_approve", waiting.pk, user=self.boss)
        self.assertEqual((first.status_code, second.status_code), (200, 409))
        self.assertIn("already answered", second.json()["error"])
        self.assertEqual(CustomerMessage.objects.count(), 1)
        self.assertEqual(reservation.status_events.filter(label="Reservation cancelled by Arabela").count(), 1)

    def test_only_the_owner_answers_even_a_staff_account_with_every_permission(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        waiting = self.waiting(reservation)
        for who in (self.senior, self.maria, self.pedro, self.staff):                          # self.staff is an old Manager row: all four permissions
            self.assertEqual(self.post("cancel_request_approve", waiting.pk, user=who).status_code, 403)
            self.assertEqual(self.post("cancel_request_decline", waiting.pk, user=who, body={"note": "no"}).status_code, 403)
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, "Pending")
        self.assertEqual(Reservation.objects.get(pk=reservation.pk).status, Reservation.Status.CONFIRMED)
        self.assertEqual(AuditLog.objects.filter(action="cancel_request.approve", outcome="refused").count(), 4)

    def test_service_level_guard_even_if_a_view_forgot_the_owner_check(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        with self.assertRaises(cancel_requests.CannotCancel):
            cancel_requests.approve(self.waiting(reservation).pk, self.senior)
        with self.assertRaises(cancel_requests.CannotCancel):
            cancel_requests.decline(self.waiting(reservation).pk, self.senior, "no")

    def test_an_unknown_request_is_a_404(self):
        self.assertEqual(self.post("cancel_request_approve", 987654, user=self.boss).status_code, 404)


class DecliningTests(RequestBase):
    def test_declining_needs_a_note_and_leaves_the_booking_alone(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        waiting = self.waiting(reservation)
        self.assertEqual(self.post("cancel_request_decline", waiting.pk, user=self.boss, body={"note": "   "}).status_code, 400)
        self.assertEqual(self.post("cancel_request_decline", waiting.pk, body={"note": "x" * 300}).status_code, 400)
        response = self.post("cancel_request_decline", waiting.pk, body={"note": "She is coming on Saturday"})
        self.assertEqual(response.status_code, 200, response.content)
        waiting.refresh_from_db()
        self.assertEqual((waiting.status, waiting.decision_note, waiting.decided_by_name), ("Declined", "She is coming on Saturday", "Olga Owner"))
        self.assertEqual(Reservation.objects.get(pk=reservation.pk).status, Reservation.Status.CONFIRMED)
        self.assertEqual(CustomerMessage.objects.count(), 0)
        self.assertTrue(reservation.status_events.filter(label=cancel_requests.DECLINED_LABEL, staff_only=True).exists())
        row = AuditLog.objects.get(action="cancel_request.decline")
        self.assertEqual(row.details, {"note": "She is coming on Saturday"})

    def test_after_a_decline_staff_can_ask_again(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        self.post("cancel_request_decline", self.waiting(reservation).pk, user=self.boss, body={"note": "Not yet"})
        self.assertEqual(self.ask(reservation, user=self.maria).status_code, 200)
        self.assertEqual(CancelRequest.objects.filter(reservation=reservation).count(), 2)

    def test_the_requester_reads_the_note_in_their_bell_for_three_days(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        waiting = self.waiting(reservation)
        self.post("cancel_request_decline", waiting.pk, user=self.boss, body={"note": "She is coming on Saturday"})
        mine = [n for n in build_items(self.maria) if n["key"].startswith("cancelreq-declined:")]
        self.assertEqual(len(mine), 1)
        self.assertIn("She is coming on Saturday", mine[0]["title"])
        self.assertEqual([n for n in build_items(self.pedro) if n["key"].startswith("cancelreq-declined:")], [])
        CancelRequest.objects.filter(pk=waiting.pk).update(decided_at=timezone.now() - timedelta(days=4))
        self.assertEqual([n for n in build_items(self.maria) if n["key"].startswith("cancelreq-declined:")], [])


class WithdrawingTests(RequestBase):
    def test_the_requester_can_take_it_back_and_nobody_else_but_the_owner(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        waiting = self.waiting(reservation)
        self.assertEqual(self.post("cancel_request_withdraw", waiting.pk, user=self.pedro).status_code, 403)
        self.assertEqual(self.post("cancel_request_withdraw", waiting.pk, user=self.senior).status_code, 403)
        self.assertEqual(self.post("cancel_request_withdraw", waiting.pk, user=self.maria).status_code, 200)
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, "Withdrawn")
        self.assertEqual(Reservation.objects.get(pk=reservation.pk).status, Reservation.Status.CONFIRMED)
        self.assertTrue(reservation.status_events.filter(label=cancel_requests.WITHDRAWN_LABEL).exists())
        self.assertEqual(self.post("cancel_request_withdraw", waiting.pk, user=self.maria).status_code, 409)       # already answered
        self.assertTrue(AuditLog.objects.filter(action="cancel_request.withdraw", actor=self.maria, outcome="ok").exists())

    def test_the_owner_can_also_clear_a_request(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        self.assertEqual(self.post("cancel_request_withdraw", self.waiting(reservation).pk, user=self.boss).status_code, 200)

    def test_a_withdrawn_request_frees_the_booking_for_a_new_one(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        self.post("cancel_request_withdraw", self.waiting(reservation).pk, user=self.maria)
        self.assertEqual(self.ask(reservation, user=self.pedro).status_code, 200)

    def test_a_requester_whose_account_was_removed_cannot_be_impersonated(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        waiting = self.waiting(reservation)
        self.maria.delete()
        waiting.refresh_from_db()
        self.assertEqual((waiting.requested_by, waiting.requested_by_name), (None, "Maria Lopez"))
        self.assertEqual(self.post("cancel_request_withdraw", waiting.pk, user=self.pedro).status_code, 403)


class PageTests(RequestBase):
    def html(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("arabela_admin:active_reservations")).content.decode()

    def test_a_staff_account_that_may_not_cancel_is_offered_to_ask_instead(self):
        reservation, _ = self.fresh_booking()
        html = self.html(self.maria)
        cancel_url = reverse("arabela_admin:reservation_cancel", args=[reservation.id])
        request_url = reverse("arabela_admin:cancel_request_create", args=[reservation.id])
        self.assertNotIn(cancel_url, html)
        self.assertIn(f'data-request="1" data-url="{request_url}"', html)
        self.assertIn("Request cancellation", html)

    def test_the_owner_and_an_allowed_staff_account_still_cancel_directly(self):
        reservation, _ = self.fresh_booking()
        for who in (self.boss, self.senior):
            html = self.html(who)
            self.assertIn(f'data-url="{reverse("arabela_admin:reservation_cancel", args=[reservation.id])}"', html)
            self.assertNotIn("data-request=\"1\"", html)

    def test_a_per_gown_request_button_is_offered_too(self):
        reservation, items = self.fresh_booking(gowns=2)
        html = self.html(self.maria)
        self.assertIn(f'data-mode="gown" data-ids="{items[0].id}"', html)
        self.assertIn("Request cancellation", html)

    def test_with_a_request_waiting_the_booking_shows_it_and_no_second_request_button(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        html = self.html(self.pedro)
        self.assertIn("Cancel requested", html)
        self.assertIn("Maria Lopez", html)
        self.assertIn("Called twice, no answer", html)
        self.assertNotIn("data-request=\"1\"", html)
        self.assertNotIn('data-cr-action="approve"', html)                                    # not his to answer
        self.assertNotIn('data-cr-action="withdraw"', html)                                   # not his request
        self.assertIn('data-cr-action="withdraw"', self.html(self.maria))                     # hers: she can take it back
        owner_html = self.html(self.boss)
        self.assertIn('data-cr-action="approve"', owner_html)
        self.assertIn('data-cr-action="decline"', owner_html)
        self.assertNotIn('data-cr-action="withdraw"', owner_html)

    def test_the_owner_still_has_the_direct_cancel_button_while_a_request_waits(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        self.assertIn(f'data-url="{reverse("arabela_admin:reservation_cancel", args=[reservation.id])}"', self.html(self.boss))

    def test_a_request_older_than_two_days_is_marked_late(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        CancelRequest.objects.update(created_at=timezone.now() - timedelta(hours=49))
        self.assertIn('class="cr-note is-late"', self.html(self.boss))

    def test_no_request_marks_nothing(self):
        self.fresh_booking()
        html = self.html(self.boss)
        self.assertNotIn('class="cr-note', html)                      # the style rule exists, the element does not
        self.assertNotIn('<span class="cr-chip', html)


class BellTests(RequestBase):
    def test_the_owner_sees_each_waiting_request_and_staff_do_not(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        waiting = self.waiting(reservation)
        items = [n for n in build_items(self.boss) if n["key"] == f"cancelreq:{waiting.pk}"]
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["level"], items[0]["actor"]), ("warning", "Maria Lopez"))
        self.assertIn(reservation.reference_code, items[0]["title"])
        for who in (self.maria, self.pedro, self.senior):
            self.assertEqual([n for n in build_items(who) if n["key"].startswith("cancelreq:")], [])

    def test_it_turns_critical_after_forty_eight_hours(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        CancelRequest.objects.update(created_at=timezone.now() - timedelta(hours=50))
        [item] = [n for n in build_items(self.boss) if n["key"].startswith("cancelreq:")]
        self.assertEqual(item["level"], "critical")

    def test_an_answered_request_leaves_the_bell(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        self.post("cancel_request_decline", self.waiting(reservation).pk, user=self.boss, body={"note": "No"})
        self.assertEqual([n for n in build_items(self.boss) if n["key"].startswith("cancelreq:")], [])


@override_settings(**ON)
class ReminderTests(RequestBase):
    def setUp(self):
        super().setUp()
        User.objects.filter(pk=self.boss.pk).update(email="boss@gmail.com")
        User.objects.filter(pk=self.owner.pk).update(email="owner2@gmail.com")

    def old_request(self, hours):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        CancelRequest.objects.filter(reservation=reservation).update(created_at=timezone.now() - timedelta(hours=hours))
        return reservation

    def test_one_email_lists_every_request_waiting_more_than_twelve_hours(self):
        a, b, fresh = self.old_request(13), self.old_request(30), self.old_request(2)
        sent = cancel_requests.send_due_reminders()
        self.assertEqual(sent, 2)
        mails = [m for m in mail.outbox if "cancellation" in m.subject]
        self.assertTrue(mails)
        body = mails[0].body
        self.assertIn(a.reference_code, body)
        self.assertIn(b.reference_code, body)
        self.assertNotIn(fresh.reference_code, body)
        self.assertIn("2 cancellation requests", mails[0].subject)
        self.assertIn("Maria Lopez", body)
        self.assertEqual({address for m in mails for address in m.to}, {"boss@gmail.com", "owner2@gmail.com"})      # owners only, never staff

    def test_each_request_gets_only_one_reminder(self):
        self.old_request(13)
        self.assertEqual(cancel_requests.send_due_reminders(), 1)
        count = len(mail.outbox)
        self.assertEqual(cancel_requests.send_due_reminders(), 0)
        self.assertEqual(len(mail.outbox), count)

    def test_an_answered_request_gets_no_reminder(self):
        reservation = self.old_request(20)
        self.post("cancel_request_decline", self.waiting(reservation).pk, user=self.boss, body={"note": "No"})
        self.assertEqual(cancel_requests.send_due_reminders(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_with_no_owner_address_nothing_is_marked_sent(self):
        User.objects.filter(pk__in=[self.boss.pk, self.owner.pk]).update(email="")
        reservation = self.old_request(20)
        self.assertEqual(cancel_requests.send_due_reminders(), 0)
        self.assertIsNone(CancelRequest.objects.get(reservation=reservation).reminder_sent_at)

    def test_the_throttled_call_never_raises_and_runs_once_in_five_minutes(self):
        from django.core.cache import cache
        cache.clear()
        self.old_request(13)
        self.assertEqual(cancel_requests.maybe_send_reminders(), 1)
        self.assertEqual(cancel_requests.maybe_send_reminders(), 0)
        with patch("reservations.cancel_requests.send_due_reminders", side_effect=RuntimeError("mail down")):
            cache.clear()
            self.assertEqual(cancel_requests.maybe_send_reminders(), 0)


class ModelTests(RequestBase):
    def test_the_constraint_allows_many_answered_requests_but_one_waiting(self):
        reservation, _ = self.fresh_booking()
        for status in ("Declined", "Declined", "Withdrawn", "Closed"):
            CancelRequest.objects.create(reservation=reservation, requested_by_name="x", reason_key="no_show", status=status)
        CancelRequest.objects.create(reservation=reservation, requested_by_name="x", reason_key="no_show")
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            CancelRequest.objects.create(reservation=reservation, requested_by_name="x", reason_key="no_show")

    def test_deleting_a_booking_takes_its_requests_with_it(self):
        reservation, _ = self.fresh_booking()
        self.ask(reservation)
        reservation.delete()
        self.assertEqual(CancelRequest.objects.count(), 0)
