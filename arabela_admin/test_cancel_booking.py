"""Cancel booking: staff cancel an approved booking nobody has picked up (a no-show, or the customer phoned). The deposit is not
refunded; the booking stays on record as Cancelled, found in Reservation Records, and counts toward the customer's cancellation limits.

Also covers the bell (one entry per booking, red once the event date has passed), the Cancel booking button, the Cancelled card and
details in Reservation Records, and the customer's own cancellation showing up there too."""
import json
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import customer_emails as ce
from accounts.models import CustomerMessage, UserProfile
from accounts.services import count_cancellations
from arabela_admin.notifications import build_items
from gowns.models import Gown, GownUnavailability
from reservations import cancellation
from reservations.models import Reservation, ReservationItem, ReservationStatusEvent

User = get_user_model()
EMAILS_ON = dict(CUSTOMER_EMAILS_ENABLED=True, CUSTOMER_EMAILS_BACKGROUND=False)


def make_gown(number=1, status=Gown.Status.RESERVED, name="Cancel Gown"):
    return Gown.objects.create(
        gown_id=f"CANCEL-{number:04d}", name=name, category=Gown.Category.WEDDING_GOWN, color_name="White", color_code="WH",
        size=Gown.Size.MEDIUM, rental_price=Decimal("5000"), status=status)


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("cancel_staff", password="x", is_staff=True, first_name="Maria", last_name="Cruz")
        # cancelling a confirmed booking is for a Manager or the owner (a Staff account is refused: arabela_admin/test_role_permissions.py)
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.MANAGER})
        cls.owner = User.objects.create_superuser("cancel_owner", "owner@gmail.com", "x")
        cls.customer = User.objects.create_user("cancel_customer", email="denmark@gmail.com", password="x")
        UserProfile.objects.update_or_create(user=cls.customer, defaults={"display_name": "Denmark Concepcion"})

    def setUp(self):
        self.today = timezone.localdate()
        self.client.force_login(self.staff)

    def booking(self, *, days_late=21, gowns=1, status=Reservation.Status.CONFIRMED, verified=True, stage=ReservationItem.Stage.PICKUP,
                customer=None, number_from=1):
        """An approved booking whose pick-up date was `days_late` days ago (event 2 days after pick-up, return 4 after)."""
        reservation = Reservation.objects.create(
            customer=customer or self.customer, customer_name="Denmark Concepcion", status=status,
            security_deposit=Decimal(2000 * gowns), reviewed_at=timezone.now() if verified else None)
        items = []
        for n in range(gowns):
            gown = make_gown(number_from + n)
            item = ReservationItem.objects.create(
                reservation=reservation, gown=gown, gown_name=gown.name + f" {n + 1}", stage=stage,
                rental_date=self.today - timedelta(days=days_late), return_date=self.today - timedelta(days=days_late - 4))
            GownUnavailability.objects.create(
                gown=gown, start_date=item.return_date + timedelta(days=1), end_date=item.return_date + timedelta(days=5),
                reason=GownUnavailability.Reason.COOLDOWN, auto_for_item=item)
            items.append(item)
        return reservation, items

    def cancel(self, reservation, *, reason="no_show", note="", confirm=None, user=None):
        if user:
            self.client.force_login(user)
        body = {"reason": reason, "note": note, "confirm": reservation.reference_code if confirm is None else confirm}
        return self.client.post(reverse("arabela_admin:reservation_cancel", args=[reservation.pk]),
                                data=json.dumps(body), content_type="application/json")


class CancelEndpointTests(Base):
    def test_cancelling_releases_the_gown_removes_its_rest_and_records_who_and_why(self):
        reservation, [item] = self.booking()
        response = self.cancel(reservation, note="Called twice, no answer")
        self.assertEqual(response.status_code, 200, response.content)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CANCELLED)
        item.gown.refresh_from_db()
        self.assertEqual(item.gown.status, Gown.Status.AVAILABLE)                       # free again
        self.assertFalse(GownUnavailability.objects.filter(auto_for_item=item).exists())  # nothing to rest from
        shop = ReservationStatusEvent.objects.get(reservation=reservation, label=cancellation.SHOP_CANCEL_LABEL)
        self.assertEqual((shop.detail, shop.actor, shop.staff_only), ("The gown was not picked up.", "Staff", False))
        note = ReservationStatusEvent.objects.get(reservation=reservation, label=cancellation.STAFF_NOTE_LABEL)
        self.assertEqual(note.detail, "Maria Cruz: The gown was not picked up - Called twice, no answer")
        self.assertTrue(note.staff_only)                                               # the customer never sees who or the note

    def test_the_customer_is_told_by_message_and_email_and_the_deposit_is_not_refunded(self):
        reservation, _ = self.booking()
        with override_settings(**EMAILS_ON), self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.cancel(reservation).status_code, 200)
        message = CustomerMessage.objects.get(recipient=self.customer)
        self.assertEqual(message.category, CustomerMessage.Category.GENERAL)
        self.assertIn(reservation.reference_code, message.body)
        self.assertIn("not refunded", message.body)
        self.assertIn("The gown was not picked up.", message.body)
        [email] = mail.outbox
        self.assertEqual(email.to, ["denmark@gmail.com"])
        self.assertEqual(email.subject, f"Your reservation {reservation.reference_code} was cancelled")
        self.assertIn("not refunded", email.body)
        self.assertIn("The gown was not picked up.", email.body)

    def test_it_counts_toward_the_customers_cancellations(self):
        reservation, _ = self.booking()
        before = count_cancellations(self.customer)
        self.cancel(reservation)
        self.assertEqual(count_cancellations(self.customer), before + 1)

    def test_a_whole_booking_with_several_gowns_is_cancelled_together(self):
        reservation, items = self.booking(gowns=3)
        self.assertEqual(self.cancel(reservation).status_code, 200)
        self.assertEqual(set(Gown.objects.filter(gown_id__startswith="CANCEL-").values_list("status", flat=True)), {Gown.Status.AVAILABLE})
        self.assertFalse(GownUnavailability.objects.filter(auto_for_item__in=items).exists())
        self.assertEqual(CustomerMessage.objects.filter(recipient=self.customer).count(), 1)     # one message, not one per gown

    def test_the_booking_code_must_be_typed_exactly_but_case_and_spaces_do_not_matter(self):
        reservation, _ = self.booking()
        for wrong in ("", "RSV-2026-9999", "yes"):
            response = self.cancel(reservation, confirm=wrong)
            self.assertEqual(response.status_code, 400, wrong)
            self.assertIn("doesn't match", response.json()["error"])
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)                # nothing changed
        self.assertEqual(self.cancel(reservation, confirm=f"  {reservation.reference_code.lower()} ").status_code, 200)

    def test_a_reason_is_required_and_another_reason_needs_words_the_customer_will_read(self):
        reservation, _ = self.booking()
        self.assertEqual(self.cancel(reservation, reason="").status_code, 400)
        self.assertEqual(self.cancel(reservation, reason="made-up").status_code, 400)
        self.assertEqual(self.cancel(reservation, reason="other", note="  ").status_code, 400)
        response = self.client.post(reverse("arabela_admin:reservation_cancel", args=[reservation.pk]),
                                    data=json.dumps({"reason": ["no_show"], "confirm": reservation.reference_code}), content_type="application/json")
        self.assertEqual(response.status_code, 400)                                       # a list, not a word: refused, not a crash
        self.assertEqual(self.cancel(reservation, reason="other", note="The gown was damaged in the shop").status_code, 200)
        shop = ReservationStatusEvent.objects.get(reservation=reservation, label=cancellation.SHOP_CANCEL_LABEL)
        self.assertEqual(shop.detail, "The gown was damaged in the shop")
        self.assertIn("The gown was damaged in the shop", CustomerMessage.objects.get().body)

    def test_the_customer_never_sees_the_staff_note_unless_the_reason_is_another_reason(self):
        reservation, _ = self.booking()
        self.cancel(reservation, reason="customer_asked", note="Private: she was rude on the phone")
        shop = ReservationStatusEvent.objects.get(reservation=reservation, label=cancellation.SHOP_CANCEL_LABEL)
        self.assertEqual(shop.detail, "You asked us to cancel.")
        self.assertNotIn("rude", CustomerMessage.objects.get().body)

    def test_a_booking_with_a_gown_already_out_cannot_be_cancelled(self):
        reservation, [first, second] = self.booking(gowns=2)
        ReservationItem.objects.filter(pk=second.pk).update(stage=ReservationItem.Stage.RESERVED)
        response = self.cancel(reservation)
        self.assertEqual(response.status_code, 400)
        self.assertIn("already gone out", response.json()["error"])
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)

    def test_only_an_approved_booking_can_be_cancelled(self):
        pending, _ = self.booking(status=Reservation.Status.PENDING, verified=False, number_from=10)
        response = self.cancel(pending)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Reject", response.json()["error"])
        rejected, _ = self.booking(status=Reservation.Status.REJECTED, number_from=20)
        response = self.cancel(rejected)
        self.assertEqual(response.status_code, 400)
        self.assertIn("status is Rejected", response.json()["error"])
        returned, _ = self.booking(status=Reservation.Status.RETURNED, number_from=30)
        self.assertEqual(self.cancel(returned).status_code, 400)

    def test_a_second_click_or_a_second_staff_member_is_told_it_is_already_cancelled(self):
        reservation, _ = self.booking()
        self.assertEqual(self.cancel(reservation).status_code, 200)
        again = self.cancel(reservation, user=self.owner)
        self.assertEqual(again.status_code, 409)
        self.assertIn("already cancelled", again.json()["error"])
        self.assertEqual(ReservationStatusEvent.objects.filter(label=cancellation.SHOP_CANCEL_LABEL).count(), 1)
        self.assertEqual(CustomerMessage.objects.filter(recipient=self.customer).count(), 1)
        self.assertEqual(count_cancellations(self.customer), 1)

    def test_a_gown_another_booking_still_needs_stays_reserved(self):
        reservation, [item] = self.booking()
        later = Reservation.objects.create(customer=self.customer, customer_name="Later", status=Reservation.Status.CONFIRMED)
        ReservationItem.objects.create(reservation=later, gown=item.gown, gown_name=item.gown_name,
                                       rental_date=self.today + timedelta(days=30), return_date=self.today + timedelta(days=34))
        self.cancel(reservation)
        item.gown.refresh_from_db()
        self.assertEqual(item.gown.status, Gown.Status.RESERVED)

    def test_only_staff_can_cancel_and_only_by_post(self):
        reservation, _ = self.booking()
        self.client.logout()
        self.assertEqual(self.cancel(reservation).status_code, 401)
        self.assertEqual(self.cancel(reservation, user=self.customer).status_code, 401)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("arabela_admin:reservation_cancel", args=[reservation.pk])).status_code, 405)
        self.assertEqual(self.cancel(reservation, user=self.staff).status_code, 200)

    def test_the_owner_can_cancel_too(self):
        reservation, _ = self.booking()
        self.assertEqual(self.cancel(reservation, user=self.owner).status_code, 200)

    def test_an_unknown_booking_and_a_broken_request_are_refused_cleanly(self):
        self.assertEqual(self.client.post(reverse("arabela_admin:reservation_cancel", args=[999999]),
                                          data=json.dumps({"reason": "no_show", "confirm": "x"}), content_type="application/json").status_code, 404)
        reservation, _ = self.booking()
        url = reverse("arabela_admin:reservation_cancel", args=[reservation.pk])
        self.assertEqual(self.client.post(url, data="{not json", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post(url, data="[1, 2]", content_type="application/json").status_code, 400)


class CancelButtonTests(Base):
    def page(self):
        return self.client.get(reverse("arabela_admin:active_reservations")).content.decode()

    def test_the_button_shows_for_a_booking_nothing_has_been_picked_up_in(self):
        reservation, _ = self.booking(days_late=21)
        html = self.page()
        self.assertIn("cb-open-btn", html)
        self.assertIn(reverse("arabela_admin:reservation_cancel", args=[reservation.pk]), html)
        self.assertIn(f'data-ref="{reservation.reference_code}"', html)
        self.assertIn('class="cb-open-btn cb-open-btn--urgent"', html)      # the event is long over
        self.assertIn('data-reason="no_show"', html)
        self.assertIn("days ago)", html)
        self.assertIn("&#8369;2,000.00", html)

    def test_a_booking_that_is_not_late_defaults_to_the_customer_asked(self):
        self.booking(days_late=-5)                                       # pick-up in 5 days
        html = self.page()
        self.assertIn('data-reason="customer_asked"', html)
        self.assertIn('class="cb-open-btn"', html)
        self.assertNotIn('class="cb-open-btn cb-open-btn--urgent"', html)
        self.assertIn("(in 7 days)", html)

    def test_no_button_once_a_gown_is_out(self):
        reservation, [item] = self.booking(stage=ReservationItem.Stage.RESERVED)
        html = self.page()
        self.assertNotIn('class="cb-open-btn', html)

    def test_the_confirm_window_is_on_the_page(self):
        self.booking()
        html = self.page()
        for text in ('id="cb-overlay"', "To confirm, type", "not refunded", "Keep booking"):
            self.assertIn(text, html)


class CancelBellTests(Base):
    def late(self, user=None):
        return [n for n in build_items(user or self.staff) if n["kind"] == "late_pickup"]

    def test_one_entry_per_booking_not_one_per_gown(self):
        self.booking(days_late=1, gowns=3)
        entries = self.late()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["level"], "warning")
        self.assertIn("Cancel Gown 1 and 2 more", entries[0]["title"])
        self.assertIn("are 1 day late for pick-up", entries[0]["title"])

    def test_after_the_event_date_it_turns_critical_and_asks_for_a_decision(self):
        reservation, _ = self.booking(days_late=21, gowns=2)
        [entry] = self.late()
        self.assertEqual(entry["level"], "critical")
        self.assertIn("never picked up the gowns and the event date passed 19 days ago. Cancel booking", entry["title"])
        self.assertIn(reservation.reference_code, entry["title"])
        self.assertIn(reservation.reference_code, entry["url"])
        self.assertTrue(entry["key"].endswith(":decide"))                 # a new key, so the escalation is announced again

    def test_just_after_the_pickup_date_it_is_still_only_a_warning(self):
        self.booking(days_late=1)                                        # event is the day after tomorrow
        [entry] = self.late()
        self.assertEqual(entry["level"], "warning")
        self.assertFalse(entry["key"].endswith(":decide"))

    def test_a_booking_with_a_gown_already_out_stays_a_warning_since_it_cannot_be_cancelled_whole(self):
        reservation, [first, second] = self.booking(days_late=21, gowns=2)
        ReservationItem.objects.filter(pk=second.pk).update(stage=ReservationItem.Stage.RESERVED)
        [entry] = self.late()
        self.assertEqual(entry["level"], "warning")

    def test_a_gown_that_never_left_the_shop_is_not_also_reported_overdue_for_return(self):
        self.booking(days_late=21)                                       # its return date is long past too
        self.assertEqual([n for n in build_items(self.staff) if n["kind"] == "overdue"], [])
        self.assertEqual(len(self.late()), 1)

    def test_a_gown_that_is_out_and_late_back_is_still_reported_overdue(self):
        self.booking(days_late=21, stage=ReservationItem.Stage.RESERVED)
        self.assertEqual(len([n for n in build_items(self.staff) if n["kind"] == "overdue"]), 1)

    def test_the_entry_clears_by_itself_once_the_booking_is_cancelled(self):
        reservation, _ = self.booking()
        self.assertEqual(len(self.late()), 1)
        self.cancel(reservation)
        self.assertEqual(self.late(), [])


class CancelledRecordsTests(Base):
    def records(self):
        html = self.client.get(reverse("arabela_admin:reservation_records")).content.decode()
        match = re.search(r'<script id="reservation-records-data" type="application/json">(.*?)</script>', html, re.S)
        return html, {r["reference"]: r for r in json.loads(match.group(1))}

    def test_a_staff_cancelled_booking_shows_who_when_why_and_that_the_deposit_is_kept(self):
        reservation, _ = self.booking(gowns=2)
        self.cancel(reservation, note="Called twice")
        _, records = self.records()
        record = records[reservation.reference_code]
        self.assertEqual(record["progress"], "Cancelled")
        self.assertEqual(record["depositStatus"], "Paid, not refunded")
        cancel = record["cancel"]
        self.assertEqual(cancel["by"], "Staff (Maria Cruz)")
        self.assertEqual(cancel["why"], "The gown was not picked up - Called twice")
        self.assertTrue(cancel["when"])
        self.assertRegex(cancel["deposit"], r"^Deposit ₱4,000\.00 was paid and verified on [A-Z][a-z]{2} \d{1,2}, \d{4}\. It is not refunded\.$")
        self.assertIn("payment-verification", record["depositLink"])

    def test_a_customer_cancelled_booking_whose_deposit_was_verified_is_recorded_the_same_way(self):
        reservation, [item] = self.booking(days_late=-5)
        self.client.force_login(self.customer)
        response = self.client.post(reverse("gowns:reservation_item_cancel", args=[item.pk]))
        self.assertEqual(response.status_code, 200, response.content)
        self.client.force_login(self.staff)
        _, records = self.records()
        record = records[reservation.reference_code]
        self.assertEqual(record["progress"], "Cancelled")
        self.assertEqual(record["cancel"]["by"], "Customer")
        self.assertEqual(record["cancel"]["why"], "Cancelled by the customer on the website.")
        self.assertIn("was paid and verified on", record["cancel"]["deposit"])
        self.assertIn("It is not refunded.", record["cancel"]["deposit"])
        self.assertEqual(record["depositStatus"], "Paid, not refunded")
        self.assertEqual(count_cancellations(self.customer), 1)             # and it counts

    def test_a_customer_cancelling_before_the_payment_was_verified_gets_the_message(self):
        reservation, [item] = self.booking(days_late=-5, status=Reservation.Status.PENDING, verified=False, number_from=40)
        self.client.force_login(self.customer)
        self.assertEqual(self.client.post(reverse("gowns:reservation_item_cancel", args=[item.pk])).status_code, 200)
        self.client.force_login(self.staff)
        _, records = self.records()
        record = records[reservation.reference_code]
        self.assertEqual(record["cancel"]["deposit"], "Cancelled before the payment was verified.")
        self.assertEqual(record["depositStatus"], "Never verified")
        self.assertEqual(record["depositLink"], "")
        self.assertEqual(count_cancellations(self.customer), 1)

    def test_an_old_cancelled_booking_with_no_log_still_shows_something_sensible(self):
        reservation, _ = self.booking(status=Reservation.Status.CANCELLED, days_late=-5, number_from=50)
        _, records = self.records()
        cancel = records[reservation.reference_code]["cancel"]
        self.assertEqual((cancel["by"], cancel["when"]), ("Customer", ""))

    def test_only_cancelled_bookings_carry_cancellation_details(self):
        live, _ = self.booking(days_late=-5, number_from=60)
        _, records = self.records()
        self.assertIsNone(records[live.reference_code]["cancel"])

    def test_the_page_has_a_cancelled_card_and_the_details_panel(self):
        html, _ = self.records()
        for text in ("applyCard('cancelled')", "countFor('cancelled')", "Cancelled by", "viewing.cancel", "rr-cancelled"):
            self.assertIn(text, html)


class CancelEmailTests(Base):
    def test_the_email_kind_is_wired_to_the_label_staff_cancellations_write(self):
        self.assertEqual(ce.EVENT_KINDS[cancellation.SHOP_CANCEL_LABEL], ce.CANCELLED)
        self.assertNotIn(cancellation.CUSTOMER_CANCEL_LABEL, ce.EVENT_KINDS)          # a customer's own cancel is not emailed

    def test_the_preview_and_wording_are_right(self):
        built = ce.sample_email(ce.CANCELLED)
        self.assertIn("cancelled", built.subject.lower())
        self.assertIn("Your security deposit is not refunded.", built.text)
        self.assertIn(ce.KIND_LABELS[ce.CANCELLED], ce.KIND_LABELS.values())

    @override_settings(**EMAILS_ON)
    def test_a_customers_own_cancellation_sends_no_email(self):
        reservation, [item] = self.booking(days_late=-5)
        self.client.force_login(self.customer)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("gowns:reservation_item_cancel", args=[item.pk]))
        self.assertEqual(mail.outbox, [])
