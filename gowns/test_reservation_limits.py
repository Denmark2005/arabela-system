"""How much one account can hold at once: up to 3 bookings waiting for the shop (5 once the customer has completed a rental), up to 5 gowns
in one booking -- and a booking nobody verifies within 2 days frees its dates by itself.

The checkout is the real one (reservation_submit); the receipt reader is a stand-in that always sees a good receipt, so only the limits
decide what happens."""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomerMessage
from accounts.test_support import agreed
from gowns import receipt_check as rc, reservation_limits
from gowns.models import Gown, GownUnavailability
from gowns.sample_images import unique_jpeg
from reservations import pending_release
from reservations.models import Reservation, ReservationItem, ReservationStatusEvent

User = get_user_model()


def make_gown(number):
    return Gown.objects.create(
        gown_id=f"Wedding Gown-LM-{number:03d}", name=f"Limit Gown {number}", category=Gown.Category.WEDDING_GOWN, color_name="White",
        color_code="WH", size=Gown.Size.MEDIUM, rental_price=Decimal("3000"), status=Gown.Status.AVAILABLE)


class LimitsBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user("lm_buyer", "lm_buyer@example.com", "Pass-12345!")
        cls.other = User.objects.create_user("lm_other", "lm_other@example.com", "Pass-12345!")
        cls.gowns = [make_gown(n) for n in range(1, 21)]

    def setUp(self):
        saver = patch("gowns.views._save_proof_file", return_value="https://example.test/fake-proof.jpg")
        saver.start()
        self.addCleanup(saver.stop)
        self.numbers = iter(range(10 ** 12, 10 ** 12 + 1000))
        reader = patch("gowns.receipt_check.read_receipt", side_effect=self.a_good_reading)
        reader.start()
        self.addCleanup(reader.stop)
        self.event = timezone.localdate() + timedelta(days=15)

    def a_good_reading(self, image_bytes, mime):
        number = next(self.numbers)
        return rc.Reading(
            is_payment_screen=True, is_transaction_list=False, app="gcash", direction="sent", recipient_number="", recipient_name="",
            sender_number="", amount=Decimal("99999"), reference=str(number)[:13].ljust(13, "0"), paid_at=timezone.now() - timedelta(minutes=1) - timedelta(seconds=number % 997),
            status="completed", gcash_signs=[],
        ), ""

    def checkout(self, user, gown_indexes, event=None):
        event = event or self.event
        items = [{"gown_name": self.gowns[i].name, "gown_slug": self.gowns[i].slug, "size": "Medium",
                  "rental_date": (event - timedelta(days=2)).isoformat(), "return_date": (event + timedelta(days=2)).isoformat()}
                 for i in gown_indexes]
        client = Client()
        client.force_login(user)
        agreed(user)
        return client.post(reverse("gowns:reservation_submit"), data={
            "items": json.dumps(items), "first_name": "Lim", "last_name": "Buyer", "phone": "09171234567", "address": "1 Test St",
            "city": "Test City", "postal_code": "1000", "payment_method": "GCash",
            "proof_of_payment": SimpleUploadedFile("proof.jpg", unique_jpeg(), content_type="image/jpeg")})

    def pending_booking(self, user, gown, *, status=Reservation.Status.PENDING, age=timedelta(0), **fields):
        values = dict(
            customer=user, customer_name="Lim Buyer", status=status, channel=Reservation.Channel.ONLINE,
            payment_proof_url="https://example.test/p.jpg", security_deposit=Decimal("2000"), rental_subtotal=Decimal("3000"),
            total_amount=Decimal("5000"))
        values.update(fields)
        reservation = Reservation.objects.create(**values)
        item = ReservationItem.objects.create(
            reservation=reservation, gown=gown, gown_name=gown.name, size="Medium", rental_price=Decimal("3000"),
            rental_date=self.event - timedelta(days=2), return_date=self.event + timedelta(days=2))
        if age:
            Reservation.objects.filter(pk=reservation.pk).update(created_at=timezone.now() - age)
        return reservation, item

    def returned_rental(self, user):
        """A completed rental: one gown of this customer has come back."""
        reservation, item = self.pending_booking(user, self.gowns[19], status=Reservation.Status.RETURNED)
        ReservationItem.objects.filter(pk=item.pk).update(stage=ReservationItem.Stage.RETURNED)
        return reservation


class GownsPerBookingTests(LimitsBase):
    def test_five_gowns_in_one_booking_are_fine(self):
        response = self.checkout(self.customer, range(5))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Reservation.objects.get().items.count(), 5)

    def test_six_gowns_are_refused_with_how_many_to_remove_and_nothing_is_booked(self):
        response = self.checkout(self.customer, range(6))
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertTrue(data["limit_reached"])
        self.assertIn("up to 5 gowns", data["error"])
        self.assertIn("remove 1 gown from", data["error"])
        self.assertFalse(Reservation.objects.exists())
        self.assertEqual(self.checkout(self.customer, range(8)).json()["error"].count("remove 3 gowns"), 1)

    def test_the_message_helpers(self):
        self.assertEqual(reservation_limits.gown_count_message(5), "")
        self.assertIn("remove 2 gowns", reservation_limits.gown_count_message(7))


class OpenBookingsTests(LimitsBase):
    def test_three_pending_bookings_are_allowed_the_fourth_is_refused(self):
        for n in range(3):
            response = self.checkout(self.customer, [n], event=self.event + timedelta(days=n * 12))
            self.assertEqual(response.status_code, 200, (n, response.content))
        response = self.checkout(self.customer, [3], event=self.event + timedelta(days=40))
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertTrue(data["limit_reached"])
        self.assertIn("3 bookings waiting for the shop to verify", data["error"])
        self.assertEqual(Reservation.objects.filter(customer=self.customer).count(), 3)

    def test_one_customers_limit_never_blocks_another(self):
        for n in range(3):
            self.pending_booking(self.customer, self.gowns[n])
        self.assertEqual(self.checkout(self.other, [5]).status_code, 200)

    def test_an_approved_rejected_or_cancelled_booking_no_longer_counts(self):
        statuses = (Reservation.Status.CONFIRMED, Reservation.Status.REJECTED, Reservation.Status.CANCELLED)
        for n, status in enumerate(statuses):
            self.pending_booking(self.customer, self.gowns[n], status=status)
        self.assertEqual(reservation_limits.open_bookings(self.customer), 0)
        self.assertEqual(self.checkout(self.customer, [9]).status_code, 200)

    def test_a_returning_customer_may_have_five(self):
        self.returned_rental(self.customer)
        self.assertTrue(reservation_limits.is_returning_customer(self.customer))
        self.assertEqual(reservation_limits.open_booking_cap(self.customer), 5)
        for n in range(5):
            self.pending_booking(self.customer, self.gowns[n])
        data = self.checkout(self.customer, [10]).json()
        self.assertIn("5 bookings waiting", data["error"])
        Reservation.objects.filter(customer=self.customer, status=Reservation.Status.PENDING).first().delete()
        self.assertEqual(self.checkout(self.customer, [10]).status_code, 200)

    def test_a_new_customer_is_capped_at_three(self):
        self.assertFalse(reservation_limits.is_returning_customer(self.customer))
        self.assertEqual(reservation_limits.open_booking_cap(self.customer), 3)


class StartingAReservationTests(LimitsBase):
    def hold(self, user):
        client = Client()
        client.force_login(user)
        agreed(user)
        return client.post(reverse("gowns:reservation_hold_start"))

    def test_proceed_to_reservation_tells_a_customer_at_the_limit_before_the_form(self):
        for n in range(3):
            self.pending_booking(self.customer, self.gowns[n])
        response = self.hold(self.customer)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.json()["limit_reached"])
        self.assertEqual(self.hold(self.other).status_code, 200)

    def test_proceed_works_when_there_is_room(self):
        self.pending_booking(self.customer, self.gowns[0])
        self.assertEqual(self.hold(self.customer).status_code, 200)


class RaceTests(LimitsBase):
    def test_the_limit_is_checked_again_inside_the_locked_section(self):
        """Two quick taps both pass the first check; the second one is stopped by the re-check under the account's row lock."""
        for n in range(2):
            self.pending_booking(self.customer, self.gowns[n])
        original = reservation_limits.open_bookings_message
        calls = []

        def message(user):
            calls.append(1)
            if len(calls) == 1:
                return ""                                                    # the first (early) check: still room, as it was a moment ago
            return original(user)

        self.pending_booking(self.customer, self.gowns[2])                    # ... then the other tab's booking lands: now 3
        with patch("gowns.views.reservation_limits.open_bookings_message", side_effect=message):
            response = self.checkout(self.customer, [7])
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()["limit_reached"])
        self.assertEqual(Reservation.objects.filter(customer=self.customer).count(), 3)
        self.assertGreaterEqual(len(calls), 2)


class ReleaseAfterTwoDaysTests(LimitsBase):
    def test_a_booking_unverified_for_two_days_is_released(self):
        reservation, item = self.pending_booking(self.customer, self.gowns[0], age=timedelta(days=2, minutes=1))
        GownUnavailability.objects.create(
            gown=self.gowns[0], start_date=item.return_date + timedelta(days=1), end_date=item.return_date + timedelta(days=5),
            reason=GownUnavailability.Reason.COOLDOWN, auto_for_item=item)
        self.assertEqual(pending_release.release_stale_pending(), 1)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.REJECTED)
        self.assertIn("did not verify the payment within 2 days", reservation.notes)
        self.assertFalse(GownUnavailability.objects.filter(auto_for_item=item).exists())            # the rest days go with it
        event = reservation.status_events.get(label="Reservation released")
        self.assertEqual(event.actor, ReservationStatusEvent.Actor.SYSTEM)
        message = CustomerMessage.objects.get(recipient=self.customer)
        self.assertIn(reservation.reference_code, message.body)
        self.assertIn("free again", message.body)

    def test_a_booking_younger_than_two_days_stays(self):
        reservation, _ = self.pending_booking(self.customer, self.gowns[0], age=timedelta(days=1, hours=23))
        self.assertEqual(pending_release.release_stale_pending(), 0)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)

    def test_the_released_gown_can_be_booked_again_by_someone_else(self):
        self.pending_booking(self.customer, self.gowns[0], age=timedelta(days=3))
        self.assertEqual(self.checkout(self.other, [0]).status_code, 400)                              # still held
        pending_release.release_stale_pending()
        self.assertEqual(self.checkout(self.other, [0]).status_code, 200)                              # free again

    def test_a_booking_the_owner_already_confirmed_in_her_gcash_is_never_released(self):
        reservation, _ = self.pending_booking(self.customer, self.gowns[0], age=timedelta(days=5), owner_check=Reservation.OwnerCheck.RECEIVED)
        self.assertEqual(pending_release.release_stale_pending(), 0)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)

    def test_approved_and_walk_in_bookings_are_left_alone(self):
        confirmed, _ = self.pending_booking(self.customer, self.gowns[0], status=Reservation.Status.CONFIRMED, age=timedelta(days=9))
        walk_in, _ = self.pending_booking(self.customer, self.gowns[1], age=timedelta(days=9), channel=Reservation.Channel.WALK_IN)
        self.assertEqual(pending_release.release_stale_pending(), 0)
        confirmed.refresh_from_db()
        walk_in.refresh_from_db()
        self.assertEqual((confirmed.status, walk_in.status), (Reservation.Status.CONFIRMED, Reservation.Status.PENDING))

    def test_a_released_booking_leaves_the_owners_list_and_the_customers_count(self):
        reservation, _ = self.pending_booking(self.customer, self.gowns[0], age=timedelta(days=3), owner_check=Reservation.OwnerCheck.PENDING)
        pending_release.release_stale_pending()
        reservation.refresh_from_db()
        self.assertEqual(reservation.owner_check, "")
        self.assertEqual(reservation_limits.open_bookings(self.customer), 0)

    def test_releasing_twice_does_nothing_the_second_time(self):
        reservation, _ = self.pending_booking(self.customer, self.gowns[0], age=timedelta(days=3))
        self.assertTrue(pending_release.release_one(reservation.pk))
        self.assertFalse(pending_release.release_one(reservation.pk))
        self.assertEqual(CustomerMessage.objects.filter(recipient=self.customer).count(), 1)

    def test_a_released_booking_is_not_counted_as_a_refused_proof(self):
        self.pending_booking(self.customer, self.gowns[0], age=timedelta(days=3))
        pending_release.release_stale_pending()
        from reservations.models import ProofRejection
        self.assertEqual(ProofRejection.objects.count(), 0)

    def test_the_sweep_is_throttled_and_never_raises(self):
        cache.clear()
        self.pending_booking(self.customer, self.gowns[0], age=timedelta(days=3))
        self.assertEqual(pending_release.maybe_release(), 1)
        self.assertEqual(pending_release.maybe_release(), 0)                                           # within the minute: skipped
        cache.clear()
        with patch.object(pending_release, "release_stale_pending", side_effect=RuntimeError("boom")):
            self.assertEqual(pending_release.maybe_release(), 0)

    @override_settings(PENDING_RELEASE_ON_REQUEST=True)
    def test_an_ordinary_page_visit_runs_the_sweep(self):
        cache.clear()
        reservation, _ = self.pending_booking(self.customer, self.gowns[0], age=timedelta(days=3))
        self.assertEqual(self.client.get(reverse("gowns:homepage")).status_code, 200)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.REJECTED)

    @override_settings(PENDING_RELEASE_ON_REQUEST=True)
    def test_posts_and_static_files_do_not_run_it(self):
        cache.clear()
        reservation, _ = self.pending_booking(self.customer, self.gowns[0], age=timedelta(days=3))
        self.client.post(reverse("gowns:cart_fetch"))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)

    def test_the_sweep_is_off_by_default_while_the_tests_run(self):
        from django.conf import settings
        self.assertFalse(settings.PENDING_RELEASE_ON_REQUEST)
        self.assertFalse(settings.RECEIPT_READER_ENABLED)
