"""Two staff on the same booking: once one of them approves or rejects it, the other's click (from an old page) is refused
with "Please refresh" instead of overturning the decision; a gown can't be checked in twice."""
import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from gowns.models import Gown
from reservations.models import Reservation, ReservationItem

User = get_user_model()


class SecondStaffClickTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff_a = User.objects.create_user(username="double_a", password="x" * 12, is_staff=True)
        cls.staff_b = User.objects.create_user(username="double_b", password="x" * 12, is_staff=True)
        for user in (cls.staff_a, cls.staff_b):
            UserProfile.objects.create(user=user, role=UserProfile.Role.STAFF)
        cls.customer = User.objects.create_user(username="double_customer", password="x" * 12)

    def setUp(self):
        self.client.force_login(self.staff_a)
        self.other = self.client_class()
        self.other.force_login(self.staff_b)
        today = timezone.localdate()
        self.gown = Gown.objects.create(gown_id="DBL-0001", name="Double Gown", category="Wedding Gown", color_name="White",
                                        color_code="WH", size=Gown.Size.MEDIUM, rental_price=5000)
        self.reservation = Reservation.objects.create(customer=self.customer, customer_name="Double Cust",
                                                      status=Reservation.Status.PENDING)
        self.item = ReservationItem.objects.create(
            reservation=self.reservation, gown=self.gown, gown_name="Double Gown", stage=ReservationItem.Stage.RETURN,
            rental_date=today - timedelta(days=3), event_date=today - timedelta(days=1),
            return_date=today + timedelta(days=1), overdue_date=today + timedelta(days=2))

    def post(self, client, name, arg, payload=None):
        return client.post(reverse(f"arabela_admin:{name}", args=[arg]), data=json.dumps(payload or {}),
                           content_type="application/json")

    def labels(self):
        return list(self.reservation.status_events.values_list("label", flat=True))

    def test_reject_after_someone_approved_is_refused_and_the_booking_stays_approved(self):
        self.assertEqual(self.post(self.client, "reservation_approve", self.reservation.id).status_code, 200)
        late = self.post(self.other, "reservation_reject", self.reservation.id, {"reason": "Payment not found"})
        self.assertEqual(late.status_code, 409)
        self.assertEqual(late.json()["error"], "This reservation was already approved. Please refresh the page.")
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.CONFIRMED)
        self.assertEqual(self.reservation.notes, "")
        self.assertNotIn("Reservation rejected", self.labels())          # the customer is never told it was rejected

    def test_approve_after_someone_rejected_is_refused_and_the_booking_stays_rejected(self):
        self.assertEqual(self.post(self.client, "reservation_reject", self.reservation.id, {"reason": "No payment"}).status_code, 200)
        late = self.post(self.other, "reservation_approve", self.reservation.id)
        self.assertEqual(late.status_code, 409)
        self.assertEqual(late.json()["error"], "This reservation was already rejected. Please refresh the page.")
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.REJECTED)
        self.assertNotIn("Reservation approved", self.labels())
        self.gown.refresh_from_db()
        self.assertEqual(self.gown.status, Gown.Status.AVAILABLE)        # the gown was not marked Reserved

    def test_approving_twice_records_and_tells_the_customer_once(self):
        self.post(self.client, "reservation_approve", self.reservation.id)
        self.assertEqual(self.post(self.other, "reservation_approve", self.reservation.id).status_code, 409)
        self.assertEqual(self.labels().count("Reservation approved"), 1)

    def test_a_booking_the_customer_cancelled_cannot_be_approved(self):
        Reservation.objects.filter(id=self.reservation.id).update(status=Reservation.Status.CANCELLED)
        response = self.post(self.client, "reservation_approve", self.reservation.id)
        self.assertEqual(response.status_code, 409)
        self.assertIn("cancelled by the customer", response.json()["error"])

    def test_a_missing_reservation_is_still_a_404(self):
        self.assertEqual(self.post(self.client, "reservation_approve", 99999999).status_code, 404)
        self.assertEqual(self.post(self.client, "reservation_reject", 99999999).status_code, 404)

    def test_a_gown_cannot_be_checked_in_twice(self):
        good = {"condition": ReservationItem.ReturnCondition.GOOD}
        self.assertEqual(self.post(self.client, "reservation_item_mark_returned", self.item.id, good).status_code, 200)
        self.item.refresh_from_db()
        first_day = self.item.returned_on
        late = self.post(self.other, "reservation_item_mark_returned", self.item.id,
                         {"condition": ReservationItem.ReturnCondition.NEEDS_REPAIR})
        self.assertEqual(late.status_code, 409)
        self.assertEqual(late.json()["error"], "This gown is already checked in. Please refresh the page.")
        self.item.refresh_from_db()
        self.assertEqual(self.item.returned_on, first_day)
        self.assertEqual(self.item.return_condition, ReservationItem.ReturnCondition.GOOD)   # the first check-in stands
        self.assertEqual(self.labels().count("Double Gown returned"), 1)
        self.assertNotIn("Double Gown needs repair", self.labels())
