"""The post-rental cooldown: event on day E, return on E+2, then COOLDOWN_DAYS (5) days of checking/washing/repair, so the
gown is off the market for 7 days after the event (E+1 .. E+7). Checked end to end through checkout and the customer's
calendar rules."""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from gowns.models import COOLDOWN_DAYS, Gown, GownUnavailability
from gowns.views import _UNIT_ASSIGNED, _UNIT_UNAVAILABLE, _blocked_dates_for_category, _find_available_unit
from reservations.models import ReservationItem

User = get_user_model()
_TINY_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


class CooldownTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="cooldown_customer", password="x")
        cls.gown = Gown.objects.create(
            gown_id="COOL-0001", name="Cooldown Gown", category=Gown.Category.WEDDING_GOWN, color_name="White", color_code="WH",
            size=Gown.Size.MEDIUM, rental_price=Decimal("5000"), status=Gown.Status.AVAILABLE)

    def setUp(self):
        self.client.force_login(self.customer)
        patcher = patch("gowns.views._save_proof_file", return_value="https://example.test/fake-proof.jpg")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.event = timezone.localdate() + timedelta(days=30)

    def book(self):
        items = [{"gown_name": self.gown.name, "gown_slug": self.gown.slug, "size": "M",
                  "rental_date": (self.event - timedelta(days=2)).isoformat(), "return_date": (self.event + timedelta(days=2)).isoformat()}]
        return self.client.post(reverse("gowns:reservation_submit"), data={
            "items": json.dumps(items), "first_name": "Cool", "last_name": "Down", "phone": "09171234567", "address": "1 Test St",
            "city": "Test City", "postal_code": "1000", "payment_method": "GCash",
            "proof_of_payment": SimpleUploadedFile("proof.jpg", _TINY_JPEG, content_type="image/jpeg")})

    def test_the_cooldown_is_five_days_so_the_gown_rests_seven_days_after_the_event(self):
        self.assertEqual(COOLDOWN_DAYS, 5)
        response = self.book()
        self.assertEqual(response.status_code, 200, response.content)
        block = GownUnavailability.objects.get(gown=self.gown, reason=GownUnavailability.Reason.COOLDOWN)
        self.assertEqual(block.start_date, self.event + timedelta(days=3))            # the day after the return (E+2)
        self.assertEqual(block.end_date, self.event + timedelta(days=7))              # 2 return days + 5 cleaning days = 7
        self.assertEqual((block.end_date - self.event).days, 7)
        self.assertEqual(block.auto_for_item, ReservationItem.objects.get())

    def test_another_customer_can_pick_up_the_day_after_the_rest_ends_and_not_before(self):
        self.book()
        last_busy_day = self.event + timedelta(days=7)
        def outcome(pickup):
            return _find_available_unit(self.gown.name, pickup, pickup + timedelta(days=4), gown_slug=self.gown.slug, size="M")[0]
        self.assertEqual(outcome(last_busy_day), _UNIT_UNAVAILABLE)                   # still resting
        self.assertEqual(outcome(last_busy_day + timedelta(days=1)), _UNIT_ASSIGNED)  # first free pick-up day

    def test_the_customer_calendar_greys_the_event_dates_until_the_first_one_that_really_fits(self):
        self.book()
        busy = set(_blocked_dates_for_category(Gown.Category.WEDDING_GOWN, self.gown.name))
        first, last = self.event - timedelta(days=2), self.event + timedelta(days=7)
        self.assertTrue(all((first + timedelta(days=n)).isoformat() in busy for n in range((last - first).days + 1)))
        self.assertNotIn((first - timedelta(days=1)).isoformat(), busy)
        self.assertNotIn((last + timedelta(days=1)).isoformat(), busy)
        # The gown page's own plan: the first event day after the rest is the first free day, picked up the same day.
        response = self.client.get(reverse("gowns:product_detail", kwargs={"collection": "wedding", "slug": self.gown.slug}))
        plans = response.context["calendar_data"]["plans"]["M"]
        self.assertEqual(plans[(self.event + timedelta(days=7)).isoformat()], "booked")           # last rest day
        first_free = (self.event + timedelta(days=8)).isoformat()                                   # event Oct 20 -> Oct 28
        self.assertEqual(plans[first_free], [first_free, 1])                                        # same-day pick-up
