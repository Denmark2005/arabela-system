"""The owner's own check of her GCash: every online GCash payment waits for HER to look in her account and press Received or Not found.

The staff cannot see the shop's GCash account, so they approve or reject bookings exactly as before; this is the one step only the owner can do
(enforced on the server, not just hidden on the page). These tests cover who may answer, what each answer does to the booking, the dates and
the customer, the owner's bell, the badges the staff see, and the 12-hour reminder email."""
import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import services
from accounts.models import CustomerMessage, UserProfile
from gowns.models import Gown, GownUnavailability
from reservations import owner_check
from reservations.models import ProofRejection, Reservation, ReservationItem, ReservationStatusEvent

User = get_user_model()
ON = dict(CUSTOMER_EMAILS_ENABLED=True, CUSTOMER_EMAILS_BACKGROUND=False, SITE_BASE_URL="https://arabela.example.org",
          DEFAULT_FROM_EMAIL="Arabela <shop@gmail.com>")


def make_admin(username, role=None, **flags):
    user = User.objects.create_user(username, f"{username}@gmail.com", "Pass-12345!", is_staff=True, **flags)
    if role is not None:
        UserProfile.objects.update_or_create(user=user, defaults={"role": role})
    return user


def make_gown(number):
    return Gown.objects.create(
        gown_id=f"Wedding Gown-OC-{number:03d}", name=f"Owner Check Gown {number}", category=Gown.Category.WEDDING_GOWN, color_name="White",
        color_code="WH", size=Gown.Size.MEDIUM, rental_price=Decimal("3000"), status=Gown.Status.AVAILABLE)


class OwnerCheckBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser("oc_super", "oc_super@gmail.com", "Pass-12345!")
        cls.owner = make_admin("oc_owner", UserProfile.Role.OWNER)
        cls.manager = make_admin("oc_manager", UserProfile.Role.MANAGER)
        cls.staff = make_admin("oc_staff", UserProfile.Role.STAFF)
        cls.no_profile = make_admin("oc_noprofile")                    # an admin account with no profile row: the pages' own "owner" flag says yes, the server says no
        cls.customer = User.objects.create_user("oc_customer", "oc_customer@gmail.com", "Pass-12345!")
        UserProfile.objects.update_or_create(user=cls.customer, defaults={"display_name": "Rita Reyes"})
        cls.gowns = [make_gown(n) for n in range(1, 8)]

    def setUp(self):
        self.event = timezone.localdate() + timedelta(days=15)

    def booking(self, gown_index=0, *, status=Reservation.Status.PENDING, owner=Reservation.OwnerCheck.PENDING, stage=ReservationItem.Stage.PICKUP,
                age=timedelta(0), **fields):
        values = dict(
            customer=self.customer, customer_name="Rita Reyes", status=status, channel=Reservation.Channel.ONLINE,
            payment_proof_url="https://example.test/p.jpg", security_deposit=Decimal("2000"), rental_subtotal=Decimal("3000"),
            total_amount=Decimal("5000"), owner_check=owner, proof_check=Reservation.ProofCheck.PASS)
        values.update(fields)
        reservation = Reservation.objects.create(**values)
        gown = self.gowns[gown_index]
        item = ReservationItem.objects.create(
            reservation=reservation, gown=gown, gown_name=gown.name, size="Medium", rental_price=Decimal("3000"),
            rental_date=self.event - timedelta(days=2), return_date=self.event + timedelta(days=2), stage=stage)
        GownUnavailability.objects.create(
            gown=gown, start_date=item.return_date + timedelta(days=1), end_date=item.return_date + timedelta(days=5),
            reason=GownUnavailability.Reason.COOLDOWN, auto_for_item=item)
        if age:
            Reservation.objects.filter(pk=reservation.pk).update(created_at=timezone.now() - age)
        return reservation, item

    def answer(self, user, reservation, result):
        self.client.force_login(user)
        return self.client.post(
            reverse("arabela_admin:reservation_owner_check", args=[reservation.pk]),
            data=json.dumps({"result": result}), content_type="application/json")


class OnlyTheOwnerCanAnswerTests(OwnerCheckBase):
    def test_the_owner_and_a_superuser_can_answer(self):
        for user in (self.owner, self.superuser):
            reservation, _ = self.booking()
            response = self.answer(user, reservation, "received")
            self.assertEqual(response.status_code, 200, (user, response.content))
            self.assertEqual(response.json()["owner_check"], "received")
            reservation.delete()

    def test_staff_and_manager_are_refused_on_the_server_and_nothing_changes(self):
        reservation, _ = self.booking()
        for user in (self.staff, self.manager):
            for result in ("received", "not_found"):
                response = self.answer(user, reservation, result)
                self.assertEqual(response.status_code, 403, (user, result))
                self.assertIn("Only the owner", response.json()["error"])
        reservation.refresh_from_db()
        self.assertEqual((reservation.status, reservation.owner_check), (Reservation.Status.PENDING, Reservation.OwnerCheck.PENDING))
        self.assertFalse(CustomerMessage.objects.exists())

    def test_a_customer_or_a_signed_out_visitor_gets_401(self):
        reservation, _ = self.booking()
        self.assertEqual(self.answer(self.customer, reservation, "received").status_code, 401)
        self.client.logout()
        response = self.client.post(reverse("arabela_admin:reservation_owner_check", args=[reservation.pk]),
                                    data=json.dumps({"result": "received"}), content_type="application/json")
        self.assertEqual(response.status_code, 401)

    def test_only_post_with_a_real_answer_is_accepted(self):
        reservation, _ = self.booking()
        url = reverse("arabela_admin:reservation_owner_check", args=[reservation.pk])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url, data="{oops", content_type="application/json").status_code, 400)
        for bad in ({}, {"result": "maybe"}, {"result": ""}, {"result": None}, [1], "received"):
            response = self.client.post(url, data=json.dumps(bad), content_type="application/json")
            self.assertEqual(response.status_code, 400, bad)
        reservation.refresh_from_db()
        self.assertEqual(reservation.owner_check, Reservation.OwnerCheck.PENDING)

    def test_an_unknown_booking_is_a_404(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("arabela_admin:reservation_owner_check", args=[999999]),
                                    data=json.dumps({"result": "received"}), content_type="application/json")
        self.assertEqual(response.status_code, 404)

    def test_a_booking_with_nothing_to_check_is_refused(self):
        walk_in, _ = self.booking(owner="", channel=Reservation.Channel.WALK_IN)
        response = self.answer(self.owner, walk_in, "received")
        self.assertEqual(response.status_code, 400)
        self.assertIn("no GCash payment to check", response.json()["error"])

    def test_a_payment_can_only_be_answered_once(self):
        reservation, _ = self.booking()
        self.assertEqual(self.answer(self.owner, reservation, "received").status_code, 200)
        for result in ("received", "not_found"):
            response = self.answer(self.owner, reservation, result)
            self.assertEqual(response.status_code, 409)
            self.assertIn("already checked", response.json()["error"])
        reservation.refresh_from_db()
        self.assertEqual(reservation.owner_check, Reservation.OwnerCheck.RECEIVED)
        self.assertEqual(reservation.status, Reservation.Status.PENDING)


class ReceivedTests(OwnerCheckBase):
    def test_received_is_recorded_and_the_booking_stays_for_the_staff_to_approve(self):
        reservation, _ = self.booking()
        response = self.answer(self.owner, reservation, "received")
        self.assertEqual(response.json(), {"success": True, "result": "received", "outcome": "confirmed", "status": "Pending", "owner_check": "received"})
        reservation.refresh_from_db()
        self.assertEqual(reservation.owner_checked_by, self.owner)
        self.assertIsNotNone(reservation.owner_checked_at)
        event = reservation.status_events.get(label="Payment confirmed by the owner")
        self.assertTrue(event.staff_only)
        self.assertFalse(CustomerMessage.objects.exists())                       # the customer is not bothered
        self.assertEqual(ProofRejection.objects.count(), 0)
        self.assertEqual(owner_check.awaiting().count(), 0)
        self.assertIn("Payment confirmed by owner ✓", [text for kind, text in reservation.proof_badges])

    def test_staff_can_still_approve_a_booking_the_owner_has_not_checked_yet(self):
        reservation, _ = self.booking()
        self.client.force_login(self.staff)
        response = self.client.post(reverse("arabela_admin:reservation_approve", args=[reservation.pk]),
                                    data=json.dumps({}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
        self.assertEqual(reservation.owner_check, Reservation.OwnerCheck.PENDING)       # still on the owner's list
        self.assertEqual(owner_check.awaiting().count(), 1)


class NotFoundTests(OwnerCheckBase):
    def test_not_found_with_nothing_picked_up_cancels_the_booking_and_frees_the_gowns(self):
        reservation, item = self.booking()
        response = self.answer(self.owner, reservation, "not_found")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["outcome"], "cancelled")
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.REJECTED)
        self.assertEqual(reservation.owner_check, Reservation.OwnerCheck.NOT_FOUND)
        self.assertIn("could not find your payment", reservation.notes)
        self.assertFalse(GownUnavailability.objects.filter(auto_for_item=item).exists())        # the rest days are gone
        self.assertEqual(reservation.status_events.filter(label="Owner: payment not found", staff_only=True).count(), 1)
        self.assertEqual(reservation.status_events.filter(label="Reservation cancelled").count(), 1)
        message = CustomerMessage.objects.get(recipient=self.customer)
        self.assertIn(reservation.reference_code, message.body)
        self.assertIn("could not find your payment", message.body)
        row = ProofRejection.objects.get(user=self.customer)
        self.assertEqual((row.source, row.reservation), (ProofRejection.Source.OWNER, reservation))

    def test_the_freed_gown_is_off_the_owners_list_and_the_customers_open_count(self):
        reservation, _ = self.booking()
        self.answer(self.owner, reservation, "not_found")
        self.assertEqual(owner_check.awaiting().count(), 0)
        from gowns import reservation_limits
        self.assertEqual(reservation_limits.open_bookings(self.customer), 0)

    def test_not_found_after_the_gown_went_out_only_records_the_answer(self):
        reservation, item = self.booking(status=Reservation.Status.ACTIVE, stage=ReservationItem.Stage.RESERVED)
        response = self.answer(self.owner, reservation, "not_found")
        self.assertEqual(response.json()["outcome"], "left_as_is")
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.ACTIVE)                   # the shop settles it in person
        self.assertEqual(reservation.owner_check, Reservation.OwnerCheck.NOT_FOUND)
        self.assertTrue(GownUnavailability.objects.filter(auto_for_item=item).exists())
        self.assertFalse(CustomerMessage.objects.exists())
        self.assertIn("Owner: payment not found ✗", [text for kind, text in reservation.proof_badges])

    def test_not_found_on_an_approved_booking_that_has_not_been_picked_up_cancels_it(self):
        reservation, item = self.booking(status=Reservation.Status.CONFIRMED)
        self.assertEqual(self.answer(self.owner, reservation, "not_found").json()["outcome"], "cancelled")
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.REJECTED)

    def test_four_refusals_of_any_kind_pause_the_account(self):
        for n in range(4):
            reservation, _ = self.booking(n)
            self.answer(self.owner, reservation, "not_found")
        profile = UserProfile.objects.get(user=self.customer)
        self.assertTrue(profile.proof_blocked)
        self.assertTrue(profile.is_flagged)
        self.assertEqual(services.count_recent_rejected_proofs(self.customer), 4)

    def test_a_booking_staff_already_rejected_is_not_on_the_owners_list(self):
        reservation, _ = self.booking()
        self.client.force_login(self.staff)
        self.client.post(reverse("arabela_admin:reservation_reject", args=[reservation.pk]),
                         data=json.dumps({"reason": "Looks edited."}), content_type="application/json")
        self.assertEqual(owner_check.awaiting().count(), 0)


class TheOwnersBellTests(OwnerCheckBase):
    def feed(self, user):
        self.client.force_login(user)
        response = self.client.get(reverse("arabela_admin:admin_notifications_feed"))
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_the_owner_is_told_to_check_her_gcash(self):
        reservation, _ = self.booking()
        items = [i for i in self.feed(self.owner)["items"] if i["key"].startswith("gcashcheck:")]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["key"], f"gcashcheck:{reservation.reference_code}")
        self.assertIn("has a payment for you to check in your GCash", items[0]["title"])
        self.assertIn(reservation.reference_code, items[0]["title"])
        self.assertIn("check=owner", items[0]["url"])
        self.assertIn(reservation.reference_code, items[0]["url"])

    def test_staff_and_managers_never_see_it(self):
        self.booking()
        for user in (self.staff, self.manager, self.no_profile):
            keys = [i["key"] for i in self.feed(user)["items"]]
            self.assertFalse([k for k in keys if k.startswith("gcashcheck:")], user)

    def test_it_goes_away_once_answered(self):
        reservation, _ = self.booking()
        self.answer(self.owner, reservation, "received")
        self.assertFalse([i for i in self.feed(self.owner)["items"] if i["key"].startswith("gcashcheck:")])

    def test_a_walk_in_booking_is_never_listed(self):
        self.booking(owner="", channel=Reservation.Channel.WALK_IN)
        self.assertFalse([i for i in self.feed(self.owner)["items"] if i["key"].startswith("gcashcheck:")])


class ThePagesTests(OwnerCheckBase):
    def test_the_payment_verification_page_shows_the_owner_her_extra_count_and_buttons(self):
        reservation, _ = self.booking()
        self.client.force_login(self.owner)
        html = self.client.get(reverse("arabela_admin:payment_verification")).content.decode()
        self.assertIn("To check in my GCash", html)
        self.assertIn("ownerCheckPayment", html)
        self.assertIn(reservation.reference_code, html)

    def test_staff_see_the_badges_but_not_the_owners_box(self):
        self.booking()
        for user in (self.staff, self.manager, self.no_profile):
            self.client.force_login(user)
            response = self.client.get(reverse("arabela_admin:payment_verification"))
            self.assertEqual(response.status_code, 200)
            html = response.content.decode()
            self.assertNotIn("To check in my GCash", html, user)
            self.assertNotIn("Received &#10003;</button>", html, user)               # the owner's two buttons are not on the page at all
            self.assertIn("Pre-checked", html, user)
            self.assertIn("Not checked by owner yet", html, user)
        self.assertEqual(self.answer(self.no_profile, self.booking()[0], "received").status_code, 403)    # and the server agrees with the page

    def test_pending_approval_shows_the_badges_to_staff(self):
        self.booking(proof_check=Reservation.ProofCheck.NEEDS_CHECK, proof_old_number=True)
        self.client.force_login(self.staff)
        html = self.client.get(reverse("arabela_admin:pending_approval")).content.decode()
        self.assertIn("Needs check", html)
        self.assertIn("Old GCash number", html)

    def test_the_proof_window_carries_the_receipt_facts_for_the_staff(self):
        reservation, _ = self.booking(
            proof_check=Reservation.ProofCheck.NEEDS_CHECK, proof_notes="The amount is less than the deposit.\nSecond line.",
            proof_amount=Decimal("100"), proof_recipient="09488745992", proof_paid_at=timezone.now())
        window = reservation.proof_window
        self.assertEqual(window["check"], "needs_check")
        self.assertEqual(window["notes"], ["The amount is less than the deposit.", "Second line."])
        self.assertEqual(window["amount"], "₱100")
        self.assertEqual(window["recipient"], "0948 874 5992")
        self.assertTrue(window["ownerOpen"])
        json.loads(reservation.proof_window_json)                                         # valid JSON for the page script

    def test_a_booking_with_no_check_has_no_window_and_no_badges(self):
        reservation, _ = self.booking(owner="", proof_check="")
        self.assertIsNone(reservation.proof_window)
        self.assertEqual(reservation.proof_badges, [])


@override_settings(**ON)
class TheTwelveHourReminderTests(OwnerCheckBase):
    def test_one_email_lists_every_payment_waiting_more_than_12_hours(self):
        old_a, _ = self.booking(0, age=timedelta(hours=13))
        old_b, _ = self.booking(1, age=timedelta(hours=30))
        fresh, _ = self.booking(2, age=timedelta(hours=2))
        sent = owner_check.send_due_reminders()
        self.assertEqual(sent, 2)
        owner_mails = [m for m in mail.outbox if "waiting for your GCash check" in m.subject]
        self.assertEqual(len(owner_mails), 2)                                               # one email for each owner account (the owner role and the superuser)
        body = owner_mails[0].body
        self.assertIn(old_a.reference_code, body)
        self.assertIn(old_b.reference_code, body)
        self.assertNotIn(fresh.reference_code, body)
        self.assertIn("2 payments", owner_mails[0].subject)
        old_a.refresh_from_db()
        fresh.refresh_from_db()
        self.assertIsNotNone(old_a.owner_reminder_sent_at)
        self.assertIsNone(fresh.owner_reminder_sent_at)

    def test_only_the_owner_accounts_are_emailed_never_staff_or_managers(self):
        self.booking(0, age=timedelta(hours=13))
        owner_check.send_due_reminders()
        recipients = {address for m in mail.outbox for address in m.to}
        self.assertEqual(recipients, {"oc_owner@gmail.com", "oc_super@gmail.com"})

    def test_each_payment_gets_only_one_reminder(self):
        self.booking(0, age=timedelta(hours=13))
        self.assertEqual(owner_check.send_due_reminders(), 1)
        count = len(mail.outbox)
        self.assertEqual(owner_check.send_due_reminders(), 0)
        self.assertEqual(len(mail.outbox), count)

    def test_an_answered_or_released_payment_gets_no_reminder(self):
        answered, _ = self.booking(0, age=timedelta(hours=13))
        rejected, _ = self.booking(1, age=timedelta(hours=13), status=Reservation.Status.REJECTED)
        self.answer(self.owner, answered, "received")
        self.assertEqual(owner_check.send_due_reminders(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_with_no_owner_email_address_nothing_is_marked_sent(self):
        User.objects.filter(pk__in=[self.owner.pk, self.superuser.pk]).update(email="")
        reservation, _ = self.booking(0, age=timedelta(hours=13))
        self.assertEqual(owner_check.send_due_reminders(), 0)
        reservation.refresh_from_db()
        self.assertIsNone(reservation.owner_reminder_sent_at)                               # tried again once an address exists

    def test_the_throttled_call_never_raises(self):
        from django.core.cache import cache
        cache.clear()
        self.booking(0, age=timedelta(hours=13))
        self.assertEqual(owner_check.maybe_send_reminders(), 1)
        self.assertEqual(owner_check.maybe_send_reminders(), 0)                             # within five minutes: skipped
