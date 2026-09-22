import json
import random
import re
import string
import threading
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections
from django.db.utils import IntegrityError, OperationalError
from django.test import TestCase
from django.urls import NoReverseMatch, reverse
from django.utils import formats, timezone
from django.utils.datastructures import MultiValueDict

from arabela_system.middleware import DatabaseRetryMiddleware
from gowns.models import Gown, GownSequence, GownSlugSequence, GownUnavailability
from gowns.views import (
    _UNIT_ASSIGNED,
    _UNIT_NO_INVENTORY,
    _UNIT_OUT_OF_STOCK,
    _UNIT_UNAVAILABLE,
    _find_available_unit,
)
from reservations.models import Reservation, ReservationItem, ReservationStatusEvent

User = get_user_model()

# A 1x1 pixel JPEG -- just needs to pass _validate_proof_file's extension/type/size
# checks, its actual image content is never inspected.
_TINY_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


def _make_proof():
    return SimpleUploadedFile("proof.jpg", _TINY_JPEG, content_type="image/jpeg")


class ReservationSubmitPriceTrustTests(TestCase):
    """`reservation_submit` (gowns/views.py) must compute every peso amount itself --
    a customer submitting a fabricated rental_price/rental_subtotal/security_deposit/
    total_amount must never have it saved. Covers both real inventory (price comes
    from the matched Gown row) and the placeholder catalog (price comes from the
    fixed 8-item price table, resolved by slug or, since the main cart-drawer
    checkout path never actually sends a slug, by the item's own name).

    _save_proof_file is mocked in every test: this environment's default storage is
    real Cloudinary (see arabela_system/settings.py), and these tests must never
    upload anything to that live external account. The mock is the only thing
    standing in for storage -- everything else in the view runs for real, against
    this test run's own throwaway database.
    """

    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="price_trust_customer", password="x")

    def setUp(self):
        self.client.force_login(self.customer)
        self.submit_url = reverse("gowns:reservation_submit")
        patcher = patch("gowns.views._save_proof_file", return_value="https://example.test/fake-proof.jpg")
        self.addCleanup(patcher.stop)
        patcher.start()

    def _submit(self, items, **overrides):
        payload = {
            "items": json.dumps(items),
            "first_name": "Test",
            "last_name": "Buyer",
            "phone": "09171234567",
            "address": "123 Test St",
            "city": "Test City",
            "postal_code": "1000",
            "payment_method": "GCash",
            # Deliberately fabricated on every call -- the point of this whole test
            # class is proving none of these three ever survive into the database.
            "rental_subtotal": "1",
            "security_deposit": "1",
            "total_amount": "1",
            "proof_of_payment": _make_proof(),
        }
        payload.update(overrides)
        return self.client.post(self.submit_url, data=payload)

    def _make_real_gown(self, **overrides):
        defaults = dict(
            gown_id=f"TESTGOWN-{Gown.objects.count() + 1:04d}",
            name="Price Trust Test Gown",
            category=Gown.Category.BELO,
            color_name="Red", color_code="RD", size=Gown.Size.MEDIUM,
            rental_price=Decimal("4500.00"), status=Gown.Status.AVAILABLE,
        )
        defaults.update(overrides)
        return Gown.objects.create(**defaults)

    # ---------------------------------------------------------------- real inventory

    def test_real_gown_price_cannot_be_overridden_by_client(self):
        gown = self._make_real_gown()
        response = self._submit([{
            "gown_name": gown.name, "gown_slug": gown.slug, "size": "Medium",
            "rental_price": "1",  # the exploit attempt
            "rental_date": "2027-01-10", "return_date": "2027-01-13",
        }])
        self.assertEqual(response.status_code, 200, response.content)
        reservation = Reservation.objects.get(reference_code=response.json()["reference_code"])
        item = reservation.items.get()
        self.assertEqual(item.rental_price, Decimal("4500.00"))
        self.assertEqual(reservation.rental_subtotal, Decimal("4500.00"))
        self.assertEqual(reservation.security_deposit, Decimal("2000.00"))
        self.assertEqual(reservation.total_amount, Decimal("6500.00"))
        self.assertEqual(item.gown_id, gown.id)

    def test_real_gown_uses_current_price_not_a_stale_cart_price(self):
        gown = self._make_real_gown(rental_price=Decimal("4500.00"))
        gown.rental_price = Decimal("5000.00")
        gown.save(update_fields=["rental_price", "updated_at"])
        response = self._submit([{
            "gown_name": gown.name, "gown_slug": gown.slug, "size": "Medium",
            "rental_price": "999",  # what a stale cart snapshot might still say
            "rental_date": "2027-01-20", "return_date": "2027-01-23",
        }])
        self.assertEqual(response.status_code, 200, response.content)
        item = ReservationItem.objects.get(reservation__reference_code=response.json()["reference_code"])
        self.assertEqual(item.rental_price, Decimal("5000.00"))

    def test_out_of_stock_real_gown_is_rejected(self):
        gown = self._make_real_gown(status=Gown.Status.OUT_OF_STOCK)
        response = self._submit([{
            "gown_name": gown.name, "gown_slug": gown.slug, "size": "Medium",
            "rental_price": "1", "rental_date": "2027-02-01", "return_date": "2027-02-04",
        }])
        self.assertEqual(response.status_code, 400)

    # ------------------------------------------------- retired placeholder catalog

    def test_stale_placeholder_item_in_the_bag_is_refused(self):
        """The 8-per-category placeholder catalog is retired, so nothing prices it any
        more. A bag saved in the browser BEFORE that change can still submit one of
        those items -- it must be refused, never booked as a gown the shop does not own."""
        before = Reservation.objects.count()
        response = self._submit([{
            "gown_name": "Suit Three", "gown_slug": "florence-organza", "size": "",
            "rental_price": "1", "rental_date": "2027-03-01", "return_date": "2027-03-04",
        }])
        self.assertEqual(response.status_code, 400)
        self.assertIn("could not be verified", response.json()["error"])
        self.assertEqual(Reservation.objects.count(), before)

    def test_stale_placeholder_item_is_refused_when_slug_is_blank_too(self):
        """Same, via the real cart-drawer path, which never sends a slug at all -- the
        old name-suffix fallback ("... Three") must not resurrect a price either."""
        before = Reservation.objects.count()
        response = self._submit([{
            "gown_name": "Wedding Gown Three", "gown_slug": "", "size": "",
            "rental_price": "1", "rental_date": "2027-03-10", "return_date": "2027-03-13",
        }])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Reservation.objects.count(), before)

    def test_completely_fabricated_gown_is_rejected(self):
        before = Reservation.objects.count()
        response = self._submit([{
            "gown_name": "Totally Made Up Gown Name Nine", "gown_slug": "not-a-real-slug",
            "size": "", "rental_price": "1", "rental_date": "2027-04-01", "return_date": "2027-04-04",
        }])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Reservation.objects.count(), before)

    def test_multi_item_cart_totals(self):
        """Two real gowns at different prices: every peso is recomputed server-side,
        so the fabricated "1"s below never reach the database."""
        cheap = self._make_real_gown(rental_price=Decimal("1600.00"), name="Multi Cart Cheap")
        dear = self._make_real_gown(rental_price=Decimal("3000.00"), name="Multi Cart Dear")
        response = self._submit([
            {"gown_name": cheap.name, "gown_slug": cheap.slug, "size": "Medium",
             "rental_price": "1", "rental_date": "2027-05-01", "return_date": "2027-05-04"},
            {"gown_name": dear.name, "gown_slug": dear.slug, "size": "Medium",
             "rental_price": "1", "rental_date": "2027-05-10", "return_date": "2027-05-13"},
        ])
        self.assertEqual(response.status_code, 200, response.content)
        reservation = Reservation.objects.get(reference_code=response.json()["reference_code"])
        self.assertEqual(reservation.rental_subtotal, Decimal("4600"))  # 1600 + 3000
        self.assertEqual(reservation.security_deposit, Decimal("4000"))  # 2000 x 2 items
        self.assertEqual(reservation.total_amount, Decimal("8600"))
        self.assertEqual(reservation.items.count(), 2)

    # ---------------------------------------------------------------- unrelated guards

    def test_empty_cart_rejected(self):
        response = self._submit([])
        self.assertEqual(response.status_code, 400)

    def test_missing_gown_name_rejected(self):
        response = self._submit([{
            "gown_name": "", "gown_slug": "", "size": "", "rental_price": "1",
            "rental_date": "2027-06-01", "return_date": "2027-06-05",
        }])
        self.assertEqual(response.status_code, 400)

    def test_missing_proof_of_payment_rejected(self):
        response = self.client.post(self.submit_url, data={
            "items": json.dumps([{
                "gown_name": "Belo Two", "gown_slug": "archive-satin", "size": "",
                "rental_price": "1", "rental_date": "2027-06-10", "return_date": "2027-06-13",
            }]),
            "first_name": "Test", "last_name": "Buyer", "payment_method": "GCash",
            "rental_subtotal": "1", "security_deposit": "1", "total_amount": "1",
            # no proof_of_payment file
        })
        self.assertEqual(response.status_code, 400)


class FindAvailableUnitTests(TestCase):
    """`_find_available_unit` (gowns/views.py) is the entire double-booking defense --
    this was the original audit's bug #4, the most financially damaging of the lot if
    it ever silently regressed. These test it directly against real Gown/
    ReservationItem/GownUnavailability rows in this run's own Postgres test database,
    so the real `select_for_update()` row-locking code path actually executes (not
    just "doesn't error" the way it would under SQLite, which treats it as a no-op)."""

    def setUp(self):
        self.today = date.today()
        self.start = self.today + timedelta(days=30)
        self.end = self.start + timedelta(days=3)

    def _make_gown(self, **overrides):
        n = Gown.objects.count() + 1
        defaults = dict(
            gown_id=f"AVAILTEST-{n:04d}", name="Availability Test Gown",
            category=Gown.Category.BELO, color_name="Red", color_code="RD",
            size=Gown.Size.MEDIUM, rental_price=Decimal("3000.00"), status=Gown.Status.AVAILABLE,
        )
        defaults.update(overrides)
        return Gown.objects.create(**defaults)

    def test_single_unit_first_booking_succeeds(self):
        gown = self._make_gown()
        outcome, unit = _find_available_unit(gown.name, self.start, self.end, gown_slug=gown.slug)
        self.assertEqual(outcome, _UNIT_ASSIGNED)
        self.assertEqual(unit.id, gown.id)

    def test_second_overlapping_booking_of_the_only_unit_is_rejected(self):
        gown = self._make_gown()
        ReservationItem.objects.create(
            reservation=Reservation.objects.create(customer=User.objects.create_user(username="a1", password="x"), customer_name="A"),
            gown=gown, gown_name=gown.name, rental_date=self.start, return_date=self.end,
        )
        outcome, unit = _find_available_unit(gown.name, self.start, self.end, gown_slug=gown.slug)
        self.assertEqual(outcome, _UNIT_UNAVAILABLE)
        self.assertIsNone(unit)

    def test_two_units_same_name_second_booking_gets_the_other_unit_not_double_booked(self):
        gown_a = self._make_gown()
        gown_b = self._make_gown(name=gown_a.name, color_code="BL")
        ReservationItem.objects.create(
            reservation=Reservation.objects.create(customer=User.objects.create_user(username="b1", password="x"), customer_name="B"),
            gown=gown_a, gown_name=gown_a.name, rental_date=self.start, return_date=self.end,
        )
        outcome, unit = _find_available_unit(gown_a.name, self.start, self.end)
        self.assertEqual(outcome, _UNIT_ASSIGNED)
        self.assertEqual(unit.id, gown_b.id)
        self.assertNotEqual(unit.id, gown_a.id)

    def test_non_overlapping_dates_on_the_same_unit_both_succeed(self):
        gown = self._make_gown()
        earlier_start = self.start - timedelta(days=10)
        earlier_end = earlier_start + timedelta(days=2)
        ReservationItem.objects.create(
            reservation=Reservation.objects.create(customer=User.objects.create_user(username="c1", password="x"), customer_name="C"),
            gown=gown, gown_name=gown.name, rental_date=earlier_start, return_date=earlier_end,
        )
        outcome, unit = _find_available_unit(gown.name, self.start, self.end, gown_slug=gown.slug)
        self.assertEqual(outcome, _UNIT_ASSIGNED)
        self.assertEqual(unit.id, gown.id)

    def test_returned_item_frees_the_unit_back_up(self):
        gown = self._make_gown()
        item = ReservationItem.objects.create(
            reservation=Reservation.objects.create(customer=User.objects.create_user(username="d1", password="x"), customer_name="D"),
            gown=gown, gown_name=gown.name, rental_date=self.start, return_date=self.end,
            stage=ReservationItem.Stage.RETURNED,
        )
        outcome, unit = _find_available_unit(gown.name, self.start, self.end, gown_slug=gown.slug)
        self.assertEqual(outcome, _UNIT_ASSIGNED)
        self.assertEqual(unit.id, gown.id)

    def test_rejected_reservation_does_not_occupy_the_unit(self):
        gown = self._make_gown()
        rejected = Reservation.objects.create(
            customer=User.objects.create_user(username="e1", password="x"), customer_name="E",
            status=Reservation.Status.REJECTED,
        )
        ReservationItem.objects.create(
            reservation=rejected, gown=gown, gown_name=gown.name,
            rental_date=self.start, return_date=self.end,
        )
        outcome, unit = _find_available_unit(gown.name, self.start, self.end, gown_slug=gown.slug)
        self.assertEqual(outcome, _UNIT_ASSIGNED)
        self.assertEqual(unit.id, gown.id)

    def test_admin_blocked_dates_occupy_the_unit_same_as_a_real_booking(self):
        gown = self._make_gown()
        GownUnavailability.objects.create(
            gown=gown, start_date=self.start, end_date=self.end, reason=GownUnavailability.Reason.CLEANING,
        )
        outcome, unit = _find_available_unit(gown.name, self.start, self.end, gown_slug=gown.slug)
        self.assertEqual(outcome, _UNIT_UNAVAILABLE)

    def test_out_of_stock_unit_is_never_assignable(self):
        gown = self._make_gown(status=Gown.Status.OUT_OF_STOCK)
        outcome, unit = _find_available_unit(gown.name, self.start, self.end, gown_slug=gown.slug)
        self.assertEqual(outcome, _UNIT_OUT_OF_STOCK)
        self.assertIsNone(unit)

    def test_name_with_no_real_inventory_is_reported_as_placeholder_not_unavailable(self):
        outcome, unit = _find_available_unit("A Name Nobody Ever Used", self.start, self.end)
        self.assertEqual(outcome, _UNIT_NO_INVENTORY)
        self.assertIsNone(unit)

    def test_already_taken_prevents_the_same_cart_from_double_assigning_one_unit(self):
        # Two lines in ONE cart for the same gown name/dates -- must not both land on
        # the same physical unit even though nothing has been saved to the DB yet.
        gown_a = self._make_gown()
        gown_b = self._make_gown(name=gown_a.name, color_code="BL")
        outcome1, unit1 = _find_available_unit(gown_a.name, self.start, self.end, already_taken=set())
        outcome2, unit2 = _find_available_unit(gown_a.name, self.start, self.end, already_taken={unit1.id})
        self.assertEqual(outcome1, _UNIT_ASSIGNED)
        self.assertEqual(outcome2, _UNIT_ASSIGNED)
        self.assertNotEqual(unit1.id, unit2.id)
        self.assertEqual({unit1.id, unit2.id}, {gown_a.id, gown_b.id})


class CustomerStatusEventTests(TestCase):
    """The customer's own actions belong in the timeline too -- submitting, uploading
    proof, and cancelling -- attributed to them rather than to staff."""

    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="cust_evt", password="x")
        # A real gown: the placeholder catalog is retired, so only real inventory
        # can be checked out at all.
        cls.gown = Gown.objects.create(
            gown_id="CUSTEVT-0001", name="Cust Event Test Gown",
            category=Gown.Category.BELO, color_name="Purple", color_code="PG",
            size=Gown.Size.MEDIUM, rental_price=Decimal("2000.00"),
            status=Gown.Status.AVAILABLE,
        )

    def setUp(self):
        self.client.force_login(self.customer)
        patcher = patch("gowns.views._save_proof_file", return_value="https://example.test/proof.jpg")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.today = date.today()

    def _submit_one(self):
        rental = self.today + timedelta(days=10)
        return self.client.post(reverse("gowns:reservation_submit"), data={
            "items": json.dumps([{
                "name": self.gown.name, "slug": self.gown.slug, "size": "M",
                "rental_date": rental.isoformat(),
                "return_date": (rental + timedelta(days=4)).isoformat(),
            }]),
            "first_name": "Evt", "last_name": "Customer", "phone": "09171234567",
            "address": "1 St", "city": "City", "postal_code": "1000",
            "payment_method": "GCash", "proof_of_payment": _make_proof(),
        })

    def test_submitting_records_a_customer_event(self):
        response = self._submit_one()
        self.assertEqual(response.status_code, 200, response.content)
        reservation = Reservation.objects.get(
            reference_code=response.json()["reference_code"])
        event = reservation.status_events.get(label="Reservation submitted")
        self.assertEqual(event.actor, "Customer")
        self.assertIsNone(event.item_id)

    def test_a_rejected_submission_records_nothing(self):
        """The event is written inside the same atomic block as the reservation, so a
        submission that never becomes a row must never leave history behind."""
        response = self.client.post(reverse("gowns:reservation_submit"), data={
            "items": json.dumps([]), "first_name": "Evt", "last_name": "Customer",
            "phone": "09171234567", "address": "1 St", "city": "City",
            "postal_code": "1000", "payment_method": "GCash",
            "proof_of_payment": _make_proof(),
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ReservationStatusEvent.objects.count(), 0)

    def test_cancelling_records_one_reservation_wide_event(self):
        """Cancelling is a whole-booking action, so the event must NOT be filed under
        the single gown whose Cancel button the customer happened to press."""
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Evt Customer",
            status=Reservation.Status.PENDING)
        items = [ReservationItem.objects.create(
            reservation=reservation, gown_name="Gown %d" % n,
            rental_date=self.today, return_date=self.today + timedelta(days=3),
        ) for n in (1, 2)]

        response = self.client.post(
            reverse("gowns:reservation_item_cancel", args=[items[0].id]))
        self.assertEqual(response.status_code, 200, response.content)

        event = reservation.status_events.get(label="Reservation cancelled")
        self.assertEqual(event.actor, "Customer")
        self.assertIsNone(event.item_id)
        self.assertEqual(reservation.status_events.filter(label="Reservation cancelled").count(), 1)

    def test_a_refused_cancellation_records_nothing(self):
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Evt Customer",
            status=Reservation.Status.RETURNED)
        item = ReservationItem.objects.create(
            reservation=reservation, gown_name="Gown", stage=ReservationItem.Stage.RETURNED,
            rental_date=self.today, return_date=self.today + timedelta(days=3))
        response = self.client.post(reverse("gowns:reservation_item_cancel", args=[item.id]))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(reservation.status_events.count(), 0)

    def test_uploading_proof_later_records_a_customer_event(self):
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Evt Customer",
            status=Reservation.Status.PENDING, payment_proof_url="")
        item = ReservationItem.objects.create(
            reservation=reservation, gown_name="Gown",
            rental_date=self.today, return_date=self.today + timedelta(days=3))
        response = self.client.post(
            reverse("gowns:reservation_upload_proof", args=[item.id]),
            data={"proof_of_payment": _make_proof()})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            reservation.status_events.get(label="Proof of payment uploaded").actor, "Customer")


class CustomerTimelineRenderTests(TestCase):
    """The customer-facing timeline on the item detail page."""

    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="cust_tl", password="x")
        cls.other = User.objects.create_user(username="cust_tl_other", password="x")

    def setUp(self):
        self.client.force_login(self.customer)
        today = date.today()
        self.reservation = Reservation.objects.create(
            customer=self.customer, customer_name="TL Customer",
            status=Reservation.Status.CONFIRMED)
        self.item = ReservationItem.objects.create(
            reservation=self.reservation, gown_name="Mine",
            rental_date=today, return_date=today + timedelta(days=3))
        self.sibling = ReservationItem.objects.create(
            reservation=self.reservation, gown_name="Sibling",
            rental_date=today, return_date=today + timedelta(days=3))
        ReservationStatusEvent.record(
            self.reservation, "Reservation submitted",
            actor=ReservationStatusEvent.Actor.CUSTOMER)
        ReservationStatusEvent.record(
            self.reservation, "Mine picked up", item=self.item,
            actor=ReservationStatusEvent.Actor.STAFF)
        ReservationStatusEvent.record(
            self.reservation, "Sibling picked up", item=self.sibling,
            actor=ReservationStatusEvent.Actor.STAFF)
        self.url = reverse("gowns:reservation_item_detail", args=[self.item.id])

    def test_the_page_renders_this_gowns_timeline(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("Reservation Timeline", html)
        self.assertIn("Mine picked up", html)
        self.assertIn("Reservation submitted", html)

    def test_a_sibling_gowns_history_is_not_shown(self):
        html = self.client.get(self.url).content.decode()
        self.assertNotIn("Sibling picked up", html)

    def test_exactly_one_row_is_badged_latest(self):
        html = self.client.get(self.url).content.decode()
        self.assertEqual(html.count(">Latest</span>"), 1)

    def test_backfilled_rows_say_the_time_was_not_recorded(self):
        """A timeline people rely on must never invent a time it never had."""
        ReservationStatusEvent.objects.filter(label="Mine picked up").update(time_known=False)
        html = self.client.get(self.url).content.decode()
        self.assertIn("time not recorded", html)

    def test_rows_with_a_real_time_show_the_clock_time(self):
        """The whole point of the feature: the exact clock time, in shop-local time,
        not the UTC instant the row happens to be stored as."""
        event = self.reservation.status_events.get(label="Mine picked up")
        expected = formats.date_format(timezone.localtime(event.occurred_at), "g:i A")
        html = self.client.get(self.url).content.decode()
        self.assertNotIn("time not recorded", html)
        self.assertIn(expected, html)

    def test_no_unrendered_template_syntax_leaks_into_the_page(self):
        html = self.client.get(self.url).content.decode()
        for leak in ("{%", "{{", "{#"):
            self.assertNotIn(leak, html)

    def test_another_customer_cannot_open_this_timeline(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url).status_code, 404)


class GownSequenceTests(TestCase):
    """`GownSequence.next_value_for` / `Gown.next_tracking_number` -- the row-locked
    counter that replaced "read the highest gown_id in this category+color group, add
    one". Identical fix to reservations.models.ReservationSequence, applied to
    Gown.gown_id; see that model's docstring for why the old shape (check, then write,
    no lock) is a real race and not just a theoretical one."""

    def test_first_call_for_a_fresh_group_returns_one(self):
        self.assertEqual(GownSequence.next_value_for("Belo", "T1"), 1)

    def test_consecutive_calls_increment_by_one(self):
        first = GownSequence.next_value_for("Belo", "T2")
        second = GownSequence.next_value_for("Belo", "T2")
        third = GownSequence.next_value_for("Belo", "T2")
        self.assertEqual([first, second, third], [1, 2, 3])

    def test_different_color_codes_have_independent_counters(self):
        self.assertEqual(GownSequence.next_value_for("Belo", "T3"), 1)
        self.assertEqual(GownSequence.next_value_for("Belo", "T4"), 1)
        self.assertEqual(GownSequence.next_value_for("Belo", "T3"), 2)

    def test_different_categories_have_independent_counters_even_with_the_same_color(self):
        self.assertEqual(GownSequence.next_value_for("Belo", "T5"), 1)
        self.assertEqual(GownSequence.next_value_for("Wedding Gown", "T5"), 1)

    def test_next_tracking_number_delegates_to_the_counter(self):
        self.assertEqual(Gown.next_tracking_number("Belo", "T6"), 1)
        self.assertEqual(Gown.next_tracking_number("Belo", "T6"), 2)

    def test_ten_real_concurrent_callers_never_receive_the_same_number(self):
        """The property this fix exists for, proven with real threads and real,
        separately-committed transactions -- not simulated. 10 threads stays under
        this dev database's Supabase pooler cap of 15 simultaneous session
        connections; going higher fails the CONNECTION itself before any application
        code runs, which would test the pool rather than the fix.

        Uses a randomized category+color group so this test stays safe to re-run
        against this project's persisted --keepdb database: each real thread commits
        on its own connection, bypassing this TestCase's normal per-test rollback, so
        a fixed key would already be past 1 on the test's second run. color_code is
        capped at 2 characters by the model itself, so the category half of the key
        (max_length=20, much more headroom) carries most of the randomization."""
        category = "RaceCat%d" % random.randint(0, 999999)
        color_code = "".join(random.choices(string.ascii_uppercase, k=2))
        n_threads = 10
        results = [None] * n_threads
        barrier = threading.Barrier(n_threads)

        def worker(index):
            connections.close_all()
            try:
                barrier.wait(timeout=5)
                results[index] = GownSequence.next_value_for(category, color_code)
            except Exception as exc:  # noqa: BLE001 -- surfaced via the assertion below
                results[index] = exc
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            # select_for_update() forces these 10 real transactions to queue up and
            # commit one at a time rather than run concurrently, and each one is a
            # real network round trip to Supabase -- generous so a slow moment for
            # the shared dev database can't fail this correctness test on timing.
            t.join(timeout=60)
        # Deliberately NOT closing the main thread's own connection here: that connection
        # is the one Django's TestCase has wrapped in an open, uncommitted atomic block/
        # savepoint stack for the whole test, and force-closing it leaves it unable to
        # reconnect cleanly -- every query for the REST OF THIS TEST CLASS then fails
        # with "the connection is closed". Each worker above already closes only its OWN
        # thread-local connection; the main thread's is never this test's to close.

        errors = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(errors, [], "a concurrent caller raised instead of queueing")
        self.assertEqual(sorted(results), list(range(1, n_threads + 1)),
                         "every thread must get a distinct, contiguous number")


class GownCreateConcurrencyTests(TestCase):
    """`gown_create_view` end-to-end -- proves the retry loop's simplification (no
    more `+ _attempt` offset, since next_tracking_number() now always returns a fresh
    number on every call) didn't reintroduce gaps or break the ordinary, uncontested
    path staff use every day."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="gown_create_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def _create(self, color_code, **overrides):
        payload = {
            "name": "Concurrency Test Gown", "category": Gown.Category.BELO,
            "color_name": "Test", "color_code": color_code,
            "size": Gown.Size.MEDIUM, "rental_price": "1500",
            "status": Gown.Status.AVAILABLE,
        }
        payload.update(overrides)
        return self.client.post(reverse("arabela_admin:gown_create"), data=payload)

    def test_the_ordinary_uncontested_path_still_gets_sequential_ids(self):
        r1 = self._create("U1").json()
        r2 = self._create("U1").json()
        self.assertEqual(r1["gown"]["gown_id"], "Belo-U1-001")
        self.assertEqual(r2["gown"]["gown_id"], "Belo-U1-002")

    def test_no_number_is_skipped_on_the_normal_path(self):
        """The old `+ _attempt` offset is gone specifically because, under the new
        counter, it would have wasted a number on every loop iteration -- this proves
        the common case (attempt 0 always succeeds) produces no such gap."""
        ids = [self._create("U2").json()["gown"]["gown_id"] for _ in range(4)]
        self.assertEqual(ids, ["Belo-U2-001", "Belo-U2-002", "Belo-U2-003", "Belo-U2-004"])


class GownSlugSequenceTests(TestCase):
    """`GownSlugSequence.reserve` / `Gown._generate_unique_slug` -- the row-locked
    reservation that replaced "check if anything already has this slug, then save".
    Same fix as GownSequence/ReservationSequence, applied to text-derived slugs
    instead of a plain numeric sequence."""

    def test_the_first_reservation_for_a_fresh_base_gets_the_bare_text(self):
        self.assertEqual(GownSlugSequence.reserve("slug-test-a"), "slug-test-a")

    def test_the_second_reservation_gets_dash_two(self):
        GownSlugSequence.reserve("slug-test-b")
        self.assertEqual(GownSlugSequence.reserve("slug-test-b"), "slug-test-b-2")

    def test_reservations_keep_incrementing(self):
        base = "slug-test-c"
        results = [GownSlugSequence.reserve(base) for _ in range(4)]
        self.assertEqual(results, [base, f"{base}-2", f"{base}-3", f"{base}-4"])

    def test_different_bases_are_independent(self):
        self.assertEqual(GownSlugSequence.reserve("slug-test-d"), "slug-test-d")
        self.assertEqual(GownSlugSequence.reserve("slug-test-e"), "slug-test-e")

    def test_skip_bare_forces_a_suffix_even_on_the_very_first_call(self):
        """What protects the placeholder catalog's 8 fixed demo slugs -- a brand new
        gown whose name happens to slugify to one of them must never be handed the
        bare form, which would silently shadow that placeholder's product page."""
        self.assertEqual(
            GownSlugSequence.reserve("slug-test-f", skip_bare=True), "slug-test-f-2")

    def test_the_group_continues_normally_after_a_skipped_bare(self):
        GownSlugSequence.reserve("slug-test-g", skip_bare=True)
        self.assertEqual(GownSlugSequence.reserve("slug-test-g"), "slug-test-g-3")

    def _make_gown(self, name, **overrides):
        defaults = dict(
            gown_id=f"SLUGTEST-{Gown.objects.count() + 1:04d}",
            name=name, category=Gown.Category.BELO,
            color_name="Test", color_code="ST", size=Gown.Size.MEDIUM,
            rental_price=Decimal("1000.00"),
        )
        defaults.update(overrides)
        return Gown.objects.create(**defaults)

    def test_a_gown_actually_gets_the_bare_slug_the_first_time_its_name_is_used(self):
        gown = self._make_gown("Slug Integration Test Gown")
        self.assertEqual(gown.slug, "slug-integration-test-gown")

    def test_a_second_gown_with_the_identical_name_gets_a_different_slug(self):
        first = self._make_gown("Duplicate Named Gown")
        second = self._make_gown("Duplicate Named Gown")
        self.assertNotEqual(first.slug, second.slug)
        self.assertEqual(first.slug, "duplicate-named-gown")
        self.assertEqual(second.slug, "duplicate-named-gown-2")

    def test_a_name_matching_a_placeholder_slug_never_gets_the_bare_form(self):
        gown = self._make_gown("Valencia Lace")  # slugifies to the exact placeholder slug
        self.assertNotEqual(gown.slug, "valencia-lace")
        self.assertTrue(gown.slug.startswith("valencia-lace-"))

    def test_ten_real_concurrent_callers_with_the_same_base_never_collide(self):
        """The property this fix exists for, proven with real threads and real,
        separately-committed transactions -- not simulated. 10 threads stays under
        this dev database's Supabase pooler cap of 15 simultaneous session
        connections.

        Uses a randomized base so this test stays safe to re-run against this
        project's persisted --keepdb database: each real thread commits on its own
        connection, bypassing this TestCase's normal per-test rollback."""
        base = "slug-race-%d" % random.randint(100000, 999999)
        n_threads = 10
        results = [None] * n_threads
        barrier = threading.Barrier(n_threads)

        def worker(index):
            connections.close_all()
            try:
                barrier.wait(timeout=5)
                results[index] = GownSlugSequence.reserve(base)
            except Exception as exc:  # noqa: BLE001 -- surfaced via the assertion below
                results[index] = exc
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            # select_for_update() forces these 10 real transactions to queue up and
            # commit one at a time rather than run concurrently, and each one is a
            # real network round trip to Supabase -- generous so a slow moment for
            # the shared dev database can't fail this correctness test on timing.
            t.join(timeout=60)
        # Deliberately NOT closing the main thread's own connection here: that connection
        # is the one Django's TestCase has wrapped in an open, uncommitted atomic block/
        # savepoint stack for the whole test, and force-closing it leaves it unable to
        # reconnect cleanly -- every query for the REST OF THIS TEST CLASS then fails
        # with "the connection is closed". Each worker above already closes only its OWN
        # thread-local connection; the main thread's is never this test's to close.

        errors = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(errors, [], "a concurrent caller raised instead of queueing")
        self.assertEqual(len(set(results)), n_threads,
                         "every thread must get a distinct slug")
        self.assertEqual(sum(1 for r in results if r == base), 1,
                         "exactly one caller must be the true 'first'")
        expected = {base} | {f"{base}-{n}" for n in range(2, n_threads + 1)}
        self.assertEqual(set(results), expected,
                         "the suffixes handed out must be exactly 2..N, no gaps or repeats")


class CategorySwapTests(TestCase):
    """Sexy Gown / Ninang Gown were removed and replaced with Long Gown / Luxury Gown /
    Mother Gown, everywhere: the customer-facing placeholder catalog, the real-inventory
    Gown model, and the admin panel. This is the one registry
    (gowns.context_processors._CATEGORIES) that every one of these surfaces reads from,
    so these tests cover that removing/adding a category there actually propagates
    everywhere it needs to -- not just that the registry itself looks right."""

    NEW_CATEGORIES = [
        ("collection_long_gown", "Long Gown"),
        ("collection_luxury_gown", "Luxury Gown"),
        ("collection_mother_gown", "Mother Gown"),
    ]

    def test_each_new_category_page_renders_with_its_own_label(self):
        for url_name, label in self.NEW_CATEGORIES:
            with self.subTest(category=label):
                html = self.client.get(reverse(f"gowns:{url_name}")).content.decode()
                self.assertIn(f">{label}<", html)
                self.assertIn(f"{label} Collection", html)  # <title>
                for leak in ("{%", "{{", "{#"):
                    self.assertNotIn(leak, html)

    def test_each_new_category_with_no_real_stock_shows_the_empty_state(self):
        """The placeholder catalog is retired, so a category with no real Gown rows now
        shows its {% empty %} state instead of 8 invented products nobody could rent."""
        for url_name, _ in self.NEW_CATEGORIES:
            with self.subTest(category=url_name):
                html = self.client.get(reverse(f"gowns:{url_name}")).content.decode()
                self.assertIn("No gowns available in this collection", html)
                # the old shared price table must not appear anywhere
                self.assertNotIn("1,600", html)
                self.assertNotIn("3,000", html)

    def test_the_old_category_url_names_no_longer_resolve(self):
        for old_name in ("collection_sexy_gown", "collection_ninang_gown"):
            with self.assertRaises(NoReverseMatch):
                reverse(f"gowns:{old_name}")

    def test_the_old_category_paths_404_cleanly_not_a_500(self):
        for path in ("/collections/sexy-gown/", "/collections/ninang-gown/",
                    "/sexy-gown/", "/ninang-gown/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_the_browse_all_grid_lists_the_new_categories_not_the_old(self):
        html = self.client.get(reverse("gowns:collections")).content.decode()
        for _, label in self.NEW_CATEGORIES:
            self.assertIn(label, html)
        self.assertNotIn("Sexy Gown", html)
        self.assertNotIn("Ninang Gown", html)

    def test_the_rent_all_cycle_through_view_includes_the_new_categories(self):
        html = self.client.get(reverse("gowns:collection_all")).content.decode()
        for _, label in self.NEW_CATEGORIES:
            self.assertIn(label, html)
        self.assertNotIn("Sexy Gown", html)
        self.assertNotIn("Ninang Gown", html)

    def test_the_homepage_showcase_no_longer_references_the_removed_categories(self):
        html = self.client.get(reverse("gowns:homepage")).content.decode()
        self.assertNotIn("Ninang", html)
        self.assertNotIn("Sexy", html)
        self.assertIn(reverse("gowns:collection_long_gown"), html)

    def test_gown_model_accepts_the_new_categories(self):
        for value in ("Long Gown", "Luxury Gown", "Mother Gown"):
            self.assertIn(value, Gown.Category.values)
        self.assertNotIn("Sexy Gown", Gown.Category.values)
        self.assertNotIn("Ninang Gown", Gown.Category.values)

    def test_a_real_gown_can_be_added_in_a_new_category_and_appears_live(self):
        """The actual end-to-end proof: create through the real admin endpoint, then
        confirm it is visible on the real customer-facing page -- not just that the
        category string is accepted somewhere in isolation."""
        staff = User.objects.create_user(username="catswap_admin", password="x", is_staff=True)
        self.client.force_login(staff)
        response = self.client.post(reverse("arabela_admin:gown_create"), data={
            "name": "Mother Gown Test Piece", "category": "Mother Gown",
            "color_name": "Ivory", "color_code": "IV",
            "size": Gown.Size.MEDIUM, "rental_price": "3000",
            "status": Gown.Status.AVAILABLE,
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["gown"]["gown_id"], "Mother Gown-IV-001")

        self.client.logout()
        html = self.client.get(reverse("gowns:collection_mother_gown")).content.decode()
        self.assertIn("Mother Gown Test Piece", html)

    def test_placeholder_checkout_is_refused_for_the_new_categories(self):
        """Nothing prices the retired placeholder catalog any more, so a placeholder
        item is refused rather than booked. Category-independent by design, so proving
        one category proves them all."""
        customer = User.objects.create_user(username="catswap_checkout", password="x")
        self.client.force_login(customer)
        rental = date.today() + timedelta(days=10)
        before = Reservation.objects.count()
        with patch("gowns.views._save_proof_file", return_value="https://example.test/fake-proof.jpg"):
            response = self.client.post(reverse("gowns:reservation_submit"), data={
                "items": json.dumps([{
                    "gown_name": "Mother Gown Three", "gown_slug": "", "size": "M",
                    "rental_price": "1",
                    "rental_date": rental.isoformat(),
                    "return_date": (rental + timedelta(days=4)).isoformat(),
                }]),
                "first_name": "Cat", "last_name": "Swap", "phone": "09171234567",
                "address": "1 St", "city": "City", "postal_code": "1000",
                "payment_method": "GCash", "proof_of_payment": _make_proof(),
            })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Reservation.objects.count(), before)

    def test_admin_categories_page_shows_new_categories_not_old(self):
        staff = User.objects.create_user(username="catswap_cats", password="x", is_staff=True)
        self.client.force_login(staff)
        html = self.client.get(reverse("arabela_admin:categories")).content.decode()
        for _, label in self.NEW_CATEGORIES:
            self.assertIn(label, html)
        self.assertNotIn("Sexy Gown", html)
        self.assertNotIn("Ninang Gown", html)
        for leak in ("{%", "{{", "{#"):
            self.assertNotIn(leak, html)

    def test_gown_catalog_dropdowns_offer_new_categories_not_old(self):
        staff = User.objects.create_user(username="catswap_catalog", password="x", is_staff=True)
        self.client.force_login(staff)
        html = self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()
        for _, label in self.NEW_CATEGORIES:
            self.assertIn(f'<option value="{label}">{label}</option>', html)
        self.assertNotIn('value="Sexy Gown"', html)
        self.assertNotIn('value="Ninang Gown"', html)


class CollectionPaginationTests(TestCase):
    """Every collection grid (`_render_collection` in gowns/views.py) now paginates at
    _COLLECTION_PAGE_SIZE (8) instead of dumping every real gown onto one page. The old
    "Next" button in every collection template was pure decoration -- no href, no
    onclick -- because a category's placeholder catalog is always exactly 8 items, so
    it never had anything to page through until real inventory could exceed 8 (see
    [[project_real_gowns_replace_placeholder_catalog]]). These tests use Belo (not
    Wedding Gown) for the >8 cases so they never collide with real production data."""

    def _make_gown(self, n, category=Gown.Category.BELO, color_code="PG"):
        return Gown.objects.create(
            gown_id=f"PAGETEST-{category}-{color_code}-{n:04d}",
            name=f"Page Test Gown {n}", category=category,
            color_name="Purple", color_code=color_code, size=Gown.Size.MEDIUM,
            rental_price=Decimal("3000.00"), status=Gown.Status.AVAILABLE,
        )

    def test_a_category_with_no_real_stock_shows_the_empty_state_and_no_pagination(self):
        html = self.client.get(reverse("gowns:collection_wedding")).content.decode()
        self.assertIn("No gowns available in this collection", html)
        self.assertNotIn("Page 1 of", html)

    def test_33_real_gowns_split_into_8_8_8_8_1_across_5_pages(self):
        for n in range(1, 34):
            self._make_gown(n)
        url = reverse("gowns:collection_belo")
        expected_counts = {1: 8, 2: 8, 3: 8, 4: 8, 5: 1}
        for page, expected in expected_counts.items():
            with self.subTest(page=page):
                html = self.client.get(url, {"page": page}).content.decode()
                # Each product card repeats its own name in more than one place (the
                # visible <h3>, the image alt text, the "add to cart" JS payload) --
                # scope the match to just the <h3> so each gown is counted once.
                titles = re.findall(r"<h3[^>]*>(Page Test Gown \d+)</h3>", html)
                self.assertEqual(len(titles), expected)
                self.assertIn(f"Page {page} of 5", html)

    def test_first_page_has_no_previous_link_but_has_next(self):
        for n in range(1, 10):
            self._make_gown(n)
        html = self.client.get(reverse("gowns:collection_belo")).content.decode()
        self.assertNotIn("Previous", html)
        self.assertIn(">Next<", html)

    def test_last_page_has_previous_link_but_no_next(self):
        for n in range(1, 10):
            self._make_gown(n)
        html = self.client.get(reverse("gowns:collection_belo"), {"page": 2}).content.decode()
        self.assertIn("Previous", html)
        self.assertNotIn(">Next<", html)

    def test_middle_page_has_both_previous_and_next(self):
        for n in range(1, 25):
            self._make_gown(n)
        html = self.client.get(reverse("gowns:collection_belo"), {"page": 2}).content.decode()
        self.assertIn("Previous", html)
        self.assertIn(">Next<", html)

    def test_out_of_range_or_garbage_page_number_never_errors(self):
        for n in range(1, 10):
            self._make_gown(n)
        url = reverse("gowns:collection_belo")
        for bad_page in ("999", "0", "-1", "not-a-number", ""):
            with self.subTest(page=bad_page):
                response = self.client.get(url, {"page": bad_page})
                self.assertEqual(response.status_code, 200)
                for leak in ("{%", "{{", "{#"):
                    self.assertNotIn(leak, response.content.decode())

    def test_next_and_previous_links_point_at_the_right_page_number(self):
        for n in range(1, 25):
            self._make_gown(n)
        html = self.client.get(reverse("gowns:collection_belo"), {"page": 2}).content.decode()
        self.assertIn('href="?page=1"', html)
        self.assertIn('href="?page=3"', html)


class DatabaseRetryMiddlewareTests(TestCase):
    """`arabela_system.middleware.DatabaseRetryMiddleware` -- retries a request when
    the database's connection pool (Supabase's free-tier plan caps this) briefly
    rejects it during a traffic spike, instead of showing the visitor a raw error
    screen. Lives here (not a standalone arabela_system test module) because
    arabela_system is the project's settings package, not a registered Django app,
    so `manage.py test` would never discover tests placed there."""

    def _request(self, files=None):
        request = MagicMock()
        request.FILES = files if files is not None else MultiValueDict()
        request.path = "/test/"
        return request

    def test_successful_request_passes_through_unaffected(self):
        get_response = MagicMock(return_value="ok-response")
        middleware = DatabaseRetryMiddleware(get_response)
        response = middleware(self._request())
        self.assertEqual(response, "ok-response")
        get_response.assert_called_once()

    @patch("arabela_system.middleware.time.sleep")
    @patch("arabela_system.middleware.close_old_connections")
    def test_retries_once_after_an_operational_error_then_succeeds(self, mock_close, mock_sleep):
        get_response = MagicMock(side_effect=[OperationalError("pool exhausted"), "ok-response"])
        middleware = DatabaseRetryMiddleware(get_response)
        response = middleware(self._request())
        self.assertEqual(response, "ok-response")
        self.assertEqual(get_response.call_count, 2)
        mock_close.assert_called_once()
        mock_sleep.assert_called_once()

    @patch("arabela_system.middleware.time.sleep")
    @patch("arabela_system.middleware.close_old_connections")
    def test_gives_up_after_max_attempts_with_a_friendly_503(self, mock_close, mock_sleep):
        from arabela_system.middleware import _MAX_ATTEMPTS
        get_response = MagicMock(side_effect=OperationalError("pool exhausted"))
        middleware = DatabaseRetryMiddleware(get_response)
        response = middleware(self._request())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(get_response.call_count, _MAX_ATTEMPTS)
        self.assertEqual(mock_close.call_count, _MAX_ATTEMPTS)

    def test_non_operational_database_errors_are_never_retried(self):
        """An IntegrityError means the code/data is wrong -- retrying would just
        fail again identically, so it must propagate immediately, never be
        swallowed as if it were a transient connection problem."""
        get_response = MagicMock(side_effect=IntegrityError("duplicate key"))
        middleware = DatabaseRetryMiddleware(get_response)
        with self.assertRaises(IntegrityError):
            middleware(self._request())
        get_response.assert_called_once()

    @patch("arabela_system.middleware.time.sleep")
    @patch("arabela_system.middleware.close_old_connections")
    def test_uploaded_files_are_rewound_before_a_retry(self, mock_close, mock_sleep):
        proof = _make_proof()
        proof.read()  # simulate the first (failed) attempt having already consumed it
        files = MultiValueDict({"proof": [proof]})
        get_response = MagicMock(side_effect=[OperationalError("pool exhausted"), "ok-response"])
        middleware = DatabaseRetryMiddleware(get_response)
        response = middleware(self._request(files=files))
        self.assertEqual(response, "ok-response")
        self.assertEqual(proof.tell(), 0)


class RetiredPlaceholderCatalogTests(TestCase):
    """The 8-per-category placeholder catalog is gone: customers only ever see, search
    and book real Gown rows. These lock in the three surfaces it used to leak through --
    the collection grid, the product page, and the search overlay."""

    def _gown(self, n, category=Gown.Category.BELO, name=None):
        return Gown.objects.create(
            gown_id=f"RETIRED-{category}-{n:04d}",
            name=name or f"Retired Test Gown {n}",
            category=category, color_name="Purple", color_code="PG",
            size=Gown.Size.MEDIUM, rental_price=Decimal("3000.00"),
            status=Gown.Status.AVAILABLE,
        )

    def test_old_placeholder_product_urls_are_404_not_a_bookable_page(self):
        """Stale links and bookmarks to the 8 fixed demo slugs must 404 rather than
        render a page for something the shop does not own."""
        for slug in ("valencia-lace", "archive-satin", "florence-organza"):
            for collection in ("wedding", "belo"):
                with self.subTest(slug=slug, collection=collection):
                    response = self.client.get(
                        reverse("gowns:product_detail",
                                kwargs={"collection": collection, "slug": slug})
                    )
                    self.assertEqual(response.status_code, 404)

    def test_a_category_with_no_real_gowns_renders_no_products(self):
        from gowns.views import _products_for_category
        self.assertEqual(_products_for_category("belo"), [])

    def test_search_catalog_holds_only_real_bookable_gowns(self):
        from gowns.context_processors import _build_search_catalog
        available = self._gown(1)
        withdrawn = self._gown(2)
        withdrawn.status = Gown.Status.OUT_OF_STOCK
        withdrawn.save(update_fields=["status", "updated_at"])

        slugs = [row["slug"] for row in _build_search_catalog()]
        self.assertIn(available.slug, slugs)
        self.assertNotIn(withdrawn.slug, slugs, "Out-of-Stock gowns are not bookable")
        for dead in ("valencia-lace", "archive-satin", "florence-organza"):
            self.assertNotIn(dead, slugs)


class RelatedGownsTests(TestCase):
    """"You may also like" -- real gowns, same collection only, never the one open."""

    def _gown(self, n, category=Gown.Category.BELO, status=Gown.Status.AVAILABLE):
        return Gown.objects.create(
            gown_id=f"RELATED-{category}-{n:04d}",
            name=f"Related Test Gown {n}", category=category,
            color_name="Purple", color_code="PG", size=Gown.Size.MEDIUM,
            rental_price=Decimal("3000.00"), status=status,
        )

    def test_suggestions_never_include_the_gown_being_viewed(self):
        from gowns.views import _related_gowns
        gowns = [self._gown(n) for n in range(1, 6)]
        for g in gowns:
            with self.subTest(viewing=g.slug):
                slugs = [r["slug"] for r in _related_gowns("Belo", g.slug)]
                self.assertNotIn(g.slug, slugs)

    def test_suggestions_stay_inside_the_same_collection(self):
        from gowns.views import _related_gowns
        belo = [self._gown(n, category=Gown.Category.BELO) for n in range(1, 4)]
        suit = self._gown(9, category=Gown.Category.SUIT)
        slugs = [r["slug"] for r in _related_gowns("Belo", belo[0].slug)]
        self.assertNotIn(suit.slug, slugs, "a Belo page must never suggest a Suit")

    def test_out_of_stock_gowns_are_never_suggested(self):
        from gowns.views import _related_gowns
        keep = self._gown(1)
        gone = self._gown(2, status=Gown.Status.OUT_OF_STOCK)
        slugs = [r["slug"] for r in _related_gowns("Belo", keep.slug)]
        self.assertNotIn(gone.slug, slugs)

    def test_returns_nothing_when_the_collection_has_no_other_gowns(self):
        from gowns.views import _related_gowns
        only = self._gown(1)
        self.assertEqual(_related_gowns("Belo", only.slug), [])


class MultiUnitProductGroupingTests(TestCase):
    """Several physical Gown rows sharing one name must behave as ONE product on
    every customer-facing surface -- the collection grid, search, "You may also
    like", and the product detail page -- never as duplicate cards of the same
    dress. Covers gowns/models.py's group_gowns_by_name/pick_representative_gown
    and every view/context-processor built on top of them."""

    def _gown(self, name, price="3000.00", status=Gown.Status.AVAILABLE,
              category=Gown.Category.BELO, gown_id=None):
        n = Gown.objects.count() + 1
        return Gown.objects.create(
            gown_id=gown_id or f"MUNIT-{n:04d}", name=name, category=category,
            color_name="Purple", color_code="PG", size=Gown.Size.MEDIUM,
            rental_price=Decimal(price), status=status,
        )

    # ---------------------------------------------------------- collection grid

    def test_three_units_of_the_same_name_become_one_card(self):
        for _ in range(3):
            self._gown("Grouped Gown")
        from gowns.views import _products_for_category
        cards = [c for c in _products_for_category("belo") if c["title"] == "Grouped Gown"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["available_count"], 3)

    def test_the_card_shows_the_lowest_price_among_its_units(self):
        self._gown("Mismatched Price Gown", price="5000.00")
        self._gown("Mismatched Price Gown", price="3500.00")
        self._gown("Mismatched Price Gown", price="4200.00")
        from gowns.views import _products_for_category
        card = next(c for c in _products_for_category("belo") if c["title"] == "Mismatched Price Gown")
        self.assertEqual(card["price"], 3500)

    def test_card_is_reserved_only_when_every_unit_is_reserved(self):
        self._gown("Half Reserved Gown", status=Gown.Status.RESERVED)
        self._gown("Half Reserved Gown", status=Gown.Status.AVAILABLE)
        from gowns.views import _products_for_category
        card = next(c for c in _products_for_category("belo") if c["title"] == "Half Reserved Gown")
        self.assertFalse(card["reserved"], "one free sibling means quick-add must still work")

    def test_card_is_reserved_when_every_single_unit_is_reserved(self):
        self._gown("Fully Reserved Gown", status=Gown.Status.RESERVED)
        self._gown("Fully Reserved Gown", status=Gown.Status.RESERVED)
        from gowns.views import _products_for_category
        card = next(c for c in _products_for_category("belo") if c["title"] == "Fully Reserved Gown")
        self.assertTrue(card["reserved"])

    def test_a_product_with_some_but_not_all_units_out_of_stock_shows_the_smaller_count(self):
        self._gown("Partially Withdrawn Gown", status=Gown.Status.OUT_OF_STOCK)
        self._gown("Partially Withdrawn Gown", status=Gown.Status.AVAILABLE)
        from gowns.views import _products_for_category
        card = next(c for c in _products_for_category("belo") if c["title"] == "Partially Withdrawn Gown")
        self.assertEqual(card["available_count"], 1)

    def test_a_product_with_every_unit_out_of_stock_has_no_card_at_all(self):
        self._gown("Fully Withdrawn Gown", status=Gown.Status.OUT_OF_STOCK)
        self._gown("Fully Withdrawn Gown", status=Gown.Status.OUT_OF_STOCK)
        from gowns.views import _products_for_category
        titles = [c["title"] for c in _products_for_category("belo")]
        self.assertNotIn("Fully Withdrawn Gown", titles)

    # ---------------------------------------------------------- product detail page

    def test_any_siblings_own_slug_renders_the_same_product(self):
        units = [self._gown("Same Product Gown") for _ in range(3)]
        client = self.client
        client.force_login(User.objects.create_user(username="munit_pdp", password="x"))
        for unit in units:
            with self.subTest(slug=unit.slug):
                response = client.get(
                    reverse("gowns:product_detail", args=["belo", unit.slug])
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Same Product Gown")
                self.assertContains(response, "3 available")

    def test_product_page_is_out_of_stock_only_once_every_sibling_is(self):
        withdrawn = self._gown("Product OOS Test Gown", status=Gown.Status.OUT_OF_STOCK)
        self._gown("Product OOS Test Gown", status=Gown.Status.AVAILABLE)
        response = self.client.get(
            reverse("gowns:product_detail", args=["belo", withdrawn.slug])
        )
        self.assertNotContains(response, "Currently Unavailable")

    def test_product_page_is_out_of_stock_when_every_sibling_is(self):
        withdrawn = self._gown("All Gone Test Gown", status=Gown.Status.OUT_OF_STOCK)
        self._gown("All Gone Test Gown", status=Gown.Status.OUT_OF_STOCK)
        response = self.client.get(
            reverse("gowns:product_detail", args=["belo", withdrawn.slug])
        )
        self.assertContains(response, "Currently Unavailable")

    def test_single_unit_product_never_shows_the_available_count_line(self):
        # "1 available" would be noise on the overwhelming majority of products
        # that only ever have one unit -- the line is reserved for when it's useful.
        unit = self._gown("Lonely Gown")
        response = self.client.get(
            reverse("gowns:product_detail", args=["belo", unit.slug])
        )
        self.assertNotContains(response, "available</p>")

    # ---------------------------------------------------------- search catalog

    def test_search_catalog_lists_a_multi_unit_product_exactly_once(self):
        from gowns.context_processors import _build_search_catalog
        for _ in range(3):
            self._gown("Searched Grouped Gown")
        matches = [row for row in _build_search_catalog() if row["title"] == "Searched Grouped Gown"]
        self.assertEqual(len(matches), 1)

    # ---------------------------------------------------------- you may also like

    def test_related_gowns_never_suggests_a_sibling_of_the_product_being_viewed(self):
        from gowns.views import _related_gowns
        units = [self._gown("Sibling Suggest Gown") for _ in range(3)]
        other = self._gown("Other Product Gown")
        for unit in units:
            with self.subTest(viewing=unit.slug):
                slugs = [r["slug"] for r in _related_gowns("Belo", unit.slug)]
                for sibling in units:
                    self.assertNotIn(sibling.slug, slugs)
                self.assertIn(other.slug, slugs)

    def test_related_gowns_suggests_a_multi_unit_product_only_once(self):
        from gowns.views import _related_gowns
        for _ in range(3):
            self._gown("Suggested Grouped Gown")
        viewer = self._gown("Viewer Gown")
        titles = [r["title"] for r in _related_gowns("Belo", viewer.slug)]
        self.assertEqual(titles.count("Suggested Grouped Gown"), 1)


class MultiUnitCalendarScopeTests(TestCase):
    """The customer calendar must grey out a date only once every unit of THIS
    product is taken -- never based on the whole category's combined stock. This is
    the exact live bug found and fixed 2026-09-22: a category with many different
    products almost never looks "full" as a whole, so a specific product's own
    calendar kept showing a date as open even after its one unit was booked for it."""

    def _gown(self, name, category=Gown.Category.WEDDING_GOWN, gown_id=None):
        n = Gown.objects.count() + 1
        return Gown.objects.create(
            gown_id=gown_id or f"CALSCOPE-{n:04d}", name=name, category=category,
            color_name="Purple", color_code="PG", size=Gown.Size.MEDIUM,
            rental_price=Decimal("3000.00"), status=Gown.Status.AVAILABLE,
        )

    def _book(self, gown, rental_date, return_date):
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        customer = User.objects.create_user(username=f"calscope_cust_{suffix}", password="x")
        reservation = Reservation.objects.create(
            customer=customer, customer_name="T", address="a", city="c", postal_code="1",
            phone="0900", payment_method="GCash", payment_proof_url="https://x.test/p.jpg",
            rental_subtotal=0, security_deposit=0, total_amount=0,
            status=Reservation.Status.CONFIRMED,
        )
        ReservationItem.objects.create(
            reservation=reservation, gown=gown, gown_name=gown.name, size=gown.size,
            rental_price=gown.rental_price, rental_date=rental_date, return_date=return_date,
            stage=ReservationItem.Stage.RESERVED,
        )

    def test_booking_one_product_never_blocks_a_different_products_calendar(self):
        from gowns.views import _blocked_dates_for_category
        booked = self._gown("Booked Product")
        other = self._gown("Untouched Product")
        rental = date.today() + timedelta(days=50)
        self._book(booked, rental, rental + timedelta(days=2))

        self.assertIn(
            rental.isoformat(),
            _blocked_dates_for_category(booked.category, booked.name),
            "the booked product's OWN calendar must show this date as taken",
        )
        self.assertNotIn(
            rental.isoformat(),
            _blocked_dates_for_category(other.category, other.name),
            "a different product in the same category must be unaffected",
        )

    def test_a_single_free_sibling_keeps_the_date_open_for_that_product(self):
        from gowns.views import _blocked_dates_for_category
        one = self._gown("Two Unit Product")
        two = self._gown("Two Unit Product", gown_id="CALSCOPE-PAIR-2")
        rental = date.today() + timedelta(days=55)
        self._book(one, rental, rental + timedelta(days=2))
        blocked = _blocked_dates_for_category(one.category, one.name)
        self.assertNotIn(rental.isoformat(), blocked, "one of two units is still free")

    def test_the_date_greys_out_once_every_unit_of_that_product_is_booked(self):
        from gowns.views import _blocked_dates_for_category
        one = self._gown("Fully Booked Product")
        two = self._gown("Fully Booked Product", gown_id="CALSCOPE-FULL-2")
        rental = date.today() + timedelta(days=60)
        return_date = rental + timedelta(days=2)
        self._book(one, rental, return_date)
        self._book(two, rental, return_date)
        blocked = _blocked_dates_for_category(one.category, one.name)
        self.assertIn(rental.isoformat(), blocked)
