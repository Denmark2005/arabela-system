import json
import random
import string
import threading
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections
from django.test import TestCase
from django.urls import NoReverseMatch, reverse
from django.utils import formats, timezone

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

    # ---------------------------------------------------------------- placeholder catalog

    def test_placeholder_item_priced_via_slug(self):
        # index 2 -> slug "florence-organza", price 2000 (gowns/context_processors.py)
        response = self._submit([{
            "gown_name": "Suit Three", "gown_slug": "florence-organza", "size": "",
            "rental_price": "1", "rental_date": "2027-03-01", "return_date": "2027-03-04",
        }])
        self.assertEqual(response.status_code, 200, response.content)
        item = ReservationItem.objects.get(reservation__reference_code=response.json()["reference_code"])
        self.assertEqual(item.rental_price, Decimal("2000"))
        self.assertIsNone(item.gown_id)

    def test_placeholder_item_priced_via_name_when_slug_is_blank(self):
        # Reproduces the real cart-drawer checkout path, which never sends a slug at all.
        response = self._submit([{
            "gown_name": "Wedding Gown Three", "gown_slug": "", "size": "",
            "rental_price": "1", "rental_date": "2027-03-10", "return_date": "2027-03-13",
        }])
        self.assertEqual(response.status_code, 200, response.content)
        item = ReservationItem.objects.get(reservation__reference_code=response.json()["reference_code"])
        self.assertEqual(item.rental_price, Decimal("2000"))

    def test_completely_fabricated_gown_is_rejected(self):
        before = Reservation.objects.count()
        response = self._submit([{
            "gown_name": "Totally Made Up Gown Name Nine", "gown_slug": "not-a-real-slug",
            "size": "", "rental_price": "1", "rental_date": "2027-04-01", "return_date": "2027-04-04",
        }])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Reservation.objects.count(), before)

    def test_multi_item_cart_totals(self):
        response = self._submit([
            {"gown_name": "Ball Gown One", "gown_slug": "valencia-lace", "size": "",
             "rental_price": "1", "rental_date": "2027-05-01", "return_date": "2027-05-04"},
            {"gown_name": "Ball Gown Eight", "gown_slug": "lumiere-silk", "size": "",
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
                "name": "Wedding Gown Three", "slug": "", "size": "M",
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
            t.join(timeout=15)
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
            t.join(timeout=15)
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

    def test_each_new_category_still_gets_the_shared_8_item_placeholder_catalog(self):
        """Category is irrelevant to the placeholder catalog by design -- the same 8
        products/prices appear regardless of which of the 11 categories is showing."""
        wedding_html = self.client.get(reverse("gowns:collection_wedding")).content.decode()
        for url_name, _ in self.NEW_CATEGORIES:
            with self.subTest(category=url_name):
                html = self.client.get(reverse(f"gowns:{url_name}")).content.decode()
                # ₱1,600 / ₱3,000 are the fixed low/high ends of the shared price table.
                self.assertIn("1,600", html)
                self.assertIn("3,000", html)

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

    def test_placeholder_checkout_still_works_for_the_new_categories(self):
        """The server-side price-trust computation (gowns.views._authoritative_price)
        is category-independent by design -- proving one new category proves all of
        them, since nothing in that code path branches on category at all."""
        customer = User.objects.create_user(username="catswap_checkout", password="x")
        self.client.force_login(customer)
        rental = date.today() + timedelta(days=10)
        with patch("gowns.views._save_proof_file", return_value="https://example.test/fake-proof.jpg"):
            response = self.client.post(reverse("gowns:reservation_submit"), data={
                "items": json.dumps([{
                    "gown_name": "Mother Gown Three", "gown_slug": "", "size": "M",
                    "rental_price": "1",  # fabricated on purpose -- server must ignore it
                    "rental_date": rental.isoformat(),
                    "return_date": (rental + timedelta(days=4)).isoformat(),
                }]),
                "first_name": "Cat", "last_name": "Swap", "phone": "09171234567",
                "address": "1 St", "city": "City", "postal_code": "1000",
                "payment_method": "GCash", "proof_of_payment": _make_proof(),
            })
        self.assertEqual(response.status_code, 200, response.content)
        item = Reservation.objects.get(
            reference_code=response.json()["reference_code"]).items.get()
        self.assertEqual(item.rental_price, Decimal("2000"))

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
