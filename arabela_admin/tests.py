import json
import re
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import CustomerMessage, UserProfile
from gowns.models import Gown, GownUnavailability
from reservations import reminders
from reservations.models import ReceiptRecord, Reservation, ReservationItem, ReservationStatusEvent

User = get_user_model()


def _make_gown(n=1, **overrides):
    defaults = dict(
        gown_id=f"BLOCKTEST-{n:04d}", name=f"Block Test Gown {n}",
        category=Gown.Category.BELO, color_name="Red", color_code="RD",
        size=Gown.Size.MEDIUM, rental_price=Decimal("3000.00"), status=Gown.Status.AVAILABLE,
    )
    defaults.update(overrides)
    return Gown.objects.create(**defaults)


class GownBlockCreateOverlapTests(TestCase):
    """`gown_block_create_view` (arabela_admin/views.py) -- bug #8 from the original
    audit: staff used to be able to block the same gown for overlapping date ranges
    with no server-side check at all. Covers the overlap rejection, the inclusive
    boundary rule, and that expired blocks don't count as live conflicts."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="block_test_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)
        self.gown = _make_gown()
        self.url = reverse("arabela_admin:gown_block_create", args=[self.gown.id])
        self.today = date.today()

    def _block(self, start, end, reason="Cleaning"):
        return self.client.post(
            self.url,
            data=json.dumps({"start_date": start.isoformat(), "end_date": end.isoformat(), "reason": reason}),
            content_type="application/json",
        )

    def test_first_block_on_a_clean_gown_succeeds(self):
        start = self.today + timedelta(days=10)
        end = start + timedelta(days=10)
        response = self._block(start, end)
        self.assertEqual(response.status_code, 200, response.content)

    def test_overlapping_block_is_rejected(self):
        start = self.today + timedelta(days=10)
        end = start + timedelta(days=10)
        self._block(start, end)
        overlap_start = start + timedelta(days=5)
        overlap_end = end + timedelta(days=5)
        response = self._block(overlap_start, overlap_end)
        self.assertEqual(response.status_code, 400)
        self.assertIn("overlaps", response.json().get("error", ""))

    def test_boundary_touching_block_is_rejected_inclusive_overlap(self):
        start = self.today + timedelta(days=10)
        end = start + timedelta(days=10)
        self._block(start, end)
        # New range starts exactly on the existing range's end date -- inclusive rule
        # means this still counts as an overlap.
        response = self._block(end, end + timedelta(days=5))
        self.assertEqual(response.status_code, 400)

    def test_non_overlapping_block_right_after_succeeds(self):
        start = self.today + timedelta(days=10)
        end = start + timedelta(days=10)
        self._block(start, end)
        response = self._block(end + timedelta(days=1), end + timedelta(days=5))
        self.assertEqual(response.status_code, 200, response.content)

    def test_expired_block_does_not_block_a_new_valid_range(self):
        GownUnavailability.objects.create(
            gown=self.gown,
            start_date=self.today - timedelta(days=60),
            end_date=self.today - timedelta(days=50),
            reason=GownUnavailability.Reason.REPAIR,
        )
        start = self.today + timedelta(days=10)
        end = start + timedelta(days=10)
        response = self._block(start, end)
        self.assertEqual(response.status_code, 200, response.content)

    def test_end_before_start_is_rejected(self):
        start = self.today + timedelta(days=10)
        end = start - timedelta(days=5)
        response = self._block(start, end)
        self.assertEqual(response.status_code, 400)

    def test_end_date_in_the_past_is_rejected(self):
        start = self.today - timedelta(days=10)
        end = self.today - timedelta(days=5)
        response = self._block(start, end)
        self.assertEqual(response.status_code, 400)

    def test_anonymous_request_is_rejected(self):
        self.client.logout()
        start = self.today + timedelta(days=10)
        response = self._block(start, start + timedelta(days=5))
        self.assertEqual(response.status_code, 401)


class AdminAccessControlTests(TestCase):
    """RBAC gates across the whole admin panel: staff-only pages/APIs must reject
    anonymous and non-staff users; owner-only pages/APIs must additionally reject
    staff who aren't the owner. A regression here would expose sensitive admin
    actions to whoever happens to be signed in."""

    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="rbac_customer", password="x")
        cls.staff = User.objects.create_user(username="rbac_staff", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.staff, role=UserProfile.Role.STAFF)
        cls.owner = User.objects.create_user(username="rbac_owner", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.owner, role=UserProfile.Role.OWNER)

    def test_anonymous_redirected_from_staff_only_page(self):
        response = self.client.get(reverse("arabela_admin:dashboard"))
        self.assertRedirects(response, reverse("arabela_admin:admin_login"))

    def test_logged_in_customer_redirected_from_staff_only_page(self):
        self.client.force_login(self.customer)
        response = self.client.get(reverse("arabela_admin:dashboard"))
        self.assertRedirects(response, reverse("arabela_admin:admin_login"))

    def test_staff_can_access_staff_only_page(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("arabela_admin:dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_staff_can_access_gown_catalog(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertEqual(response.status_code, 200)

    def test_non_owner_staff_redirected_from_owner_only_page(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("arabela_admin:staff_management"))
        self.assertRedirects(response, reverse("arabela_admin:dashboard"))

    def test_owner_can_access_owner_only_page(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("arabela_admin:staff_management"))
        self.assertEqual(response.status_code, 200)

    def test_non_owner_staff_gets_403_from_owner_only_api(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("arabela_admin:staff_create"),
            data=json.dumps({"name": "New Person", "role": "Staff", "password": "somepassword123"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_anonymous_gets_401_from_staff_only_api(self):
        gown = _make_gown()
        response = self.client.post(
            reverse("arabela_admin:gown_block_create", args=[gown.id]),
            data=json.dumps({"start_date": "2027-01-01", "end_date": "2027-01-05"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)


class GownCreateValidationTests(TestCase):
    """`gown_create_view` -- bugs #1/#6/#7 from the original audit: field validation
    that mirrors the model's own constraints (so a bad value gets a clear staff-facing
    message instead of a raw database error), rental price bounds, and the retry-on-
    IntegrityError race guard for two staff adding a gown in the same category+color
    at the same instant."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="create_test_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)
        self.url = reverse("arabela_admin:gown_create")

    def _create(self, **overrides):
        data = dict(
            name="New Test Gown", category=Gown.Category.BELO, color_name="Blue",
            color_code="BU", size=Gown.Size.MEDIUM, rental_price="3500",
        )
        data.update(overrides)
        return self.client.post(self.url, data=data)

    def test_valid_gown_is_created(self):
        response = self._create()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["success"])
        gown = Gown.objects.get(id=response.json()["gown"]["id"])
        self.assertEqual(gown.rental_price, Decimal("3500.00"))

    def test_missing_name_is_rejected(self):
        response = self._create(name="")
        self.assertEqual(response.status_code, 400)

    def test_invalid_category_is_rejected(self):
        response = self._create(category="Not A Real Category")
        self.assertEqual(response.status_code, 400)

    def test_color_code_over_two_chars_is_rejected(self):
        response = self._create(color_code="TOOLONG")
        self.assertEqual(response.status_code, 400)

    def test_zero_price_is_rejected(self):
        response = self._create(rental_price="0")
        self.assertEqual(response.status_code, 400)

    def test_negative_price_is_rejected(self):
        response = self._create(rental_price="-100")
        self.assertEqual(response.status_code, 400)

    def test_price_over_the_max_is_rejected(self):
        response = self._create(rental_price="9999999")
        self.assertEqual(response.status_code, 400)

    def test_non_numeric_price_is_rejected(self):
        response = self._create(rental_price="not-a-number")
        self.assertEqual(response.status_code, 400)

    def test_two_gowns_same_category_and_color_get_different_sequential_ids(self):
        r1 = self._create(name="First", color_code="ZZ")
        r2 = self._create(name="Second", color_code="ZZ")
        self.assertEqual(r1.status_code, 200, r1.content)
        self.assertEqual(r2.status_code, 200, r2.content)
        self.assertNotEqual(r1.json()["gown"]["gown_id"], r2.json()["gown"]["gown_id"])

    def test_anonymous_cannot_create_a_gown(self):
        self.client.logout()
        response = self._create()
        self.assertEqual(response.status_code, 401)


class GownDeleteTests(TestCase):
    """`gown_delete_view` -- must refuse to delete a gown that's still on a live
    reservation (not Returned, not Rejected/Cancelled), so deleting inventory can
    never silently orphan an active booking."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="delete_test_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="delete_test_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)

    def test_gown_with_no_reservations_can_be_deleted(self):
        gown = _make_gown()
        response = self.client.post(reverse("arabela_admin:gown_delete", args=[gown.id]))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(Gown.objects.filter(id=gown.id).exists())

    def test_gown_on_an_active_reservation_cannot_be_deleted(self):
        from reservations.models import Reservation, ReservationItem
        gown = _make_gown()
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Test Customer")
        ReservationItem.objects.create(
            reservation=reservation, gown=gown, gown_name=gown.name,
            rental_date=date.today(), return_date=date.today() + timedelta(days=3),
        )
        response = self.client.post(reverse("arabela_admin:gown_delete", args=[gown.id]))
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Gown.objects.filter(id=gown.id).exists())

    def test_gown_on_a_returned_item_CAN_be_deleted(self):
        from reservations.models import Reservation, ReservationItem
        gown = _make_gown()
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Test Customer")
        ReservationItem.objects.create(
            reservation=reservation, gown=gown, gown_name=gown.name,
            rental_date=date.today(), return_date=date.today() + timedelta(days=3),
            stage=ReservationItem.Stage.RETURNED,
        )
        response = self.client.post(reverse("arabela_admin:gown_delete", args=[gown.id]))
        self.assertEqual(response.status_code, 200, response.content)

    def test_gown_on_a_rejected_reservation_CAN_be_deleted(self):
        from reservations.models import Reservation, ReservationItem
        gown = _make_gown()
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Test Customer", status=Reservation.Status.REJECTED,
        )
        ReservationItem.objects.create(
            reservation=reservation, gown=gown, gown_name=gown.name,
            rental_date=date.today(), return_date=date.today() + timedelta(days=3),
        )
        response = self.client.post(reverse("arabela_admin:gown_delete", args=[gown.id]))
        self.assertEqual(response.status_code, 200, response.content)


class CategoriesRealCountsTests(TestCase):
    """`categories_view` -- bug #9 from the original audit: this page used to show 11
    hardcoded fake inventory numbers with no connection to the database at all."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="categories_test_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def test_counts_reflect_real_gowns_and_exclude_out_of_stock(self):
        _make_gown(1, category=Gown.Category.BELO, status=Gown.Status.AVAILABLE)
        _make_gown(2, category=Gown.Category.BELO, status=Gown.Status.RESERVED)
        _make_gown(3, category=Gown.Category.BELO, status=Gown.Status.OUT_OF_STOCK)
        _make_gown(4, category=Gown.Category.SUIT, status=Gown.Status.AVAILABLE)

        response = self.client.get(reverse("arabela_admin:categories"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["belo_count"], 2)  # excludes the Out-of-Stock one
        self.assertEqual(response.context["suit_count"], 1)

    def test_category_with_zero_gowns_shows_zero_not_a_fake_number(self):
        response = self.client.get(reverse("arabela_admin:categories"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["dresses_count"], 0)


class GlobalSearchGownCoverageTests(TestCase):
    """`admin_search_view` used to only search Reservations -- typing a gown's name/ID
    returned nothing. Covers that it now also finds gowns, correctly tagged, linking
    to the pre-filtered, highlighted row on Gown Catalog."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="search_test_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)
        self.url = reverse("arabela_admin:admin_search")

    def _search(self, q):
        return self.client.get(self.url, {"q": q})

    def test_gown_found_by_gown_id(self):
        gown = _make_gown(1, name="Unique Search Probe")
        response = self._search(gown.gown_id)
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        gown_hits = [r for r in results if r.get("type") == "gown"]
        self.assertEqual(len(gown_hits), 1)
        self.assertEqual(gown_hits[0]["gown_id"], gown.gown_id)
        self.assertIn("gown-catalog", gown_hits[0]["target_url"])
        self.assertIn(gown.gown_id, gown_hits[0]["target_url"])

    def test_gown_found_by_name(self):
        gown = _make_gown(2, name="Zzyx Unique Name Probe")
        response = self._search("Zzyx Unique Name")
        results = response.json()["results"]
        self.assertTrue(any(r.get("gown_id") == gown.gown_id for r in results if r.get("type") == "gown"))

    def test_out_of_stock_gown_is_still_findable(self):
        gown = _make_gown(3, name="Withdrawn Search Probe", status=Gown.Status.OUT_OF_STOCK)
        response = self._search(gown.gown_id)
        results = response.json()["results"]
        self.assertTrue(any(r.get("gown_id") == gown.gown_id for r in results if r.get("type") == "gown"))

    def test_short_query_returns_no_results(self):
        response = self._search("a")
        self.assertEqual(response.json(), {"results": []})

    def test_anonymous_gets_401(self):
        self.client.logout()
        response = self._search("test")
        self.assertEqual(response.status_code, 401)


class ReservationApprovalTests(TestCase):
    """`reservation_approve_view` / `reservation_reject_view` -- the Pending Approval
    queue's core actions. Approving must also flip any linked Available gown to
    Reserved so the catalog reflects the booking; rejecting must store an optional
    reason without requiring one."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="approval_test_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="approval_test_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)

    def _make_pending_reservation_with_gown(self):
        from reservations.models import Reservation, ReservationItem
        gown = _make_gown(status=Gown.Status.AVAILABLE)
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Approval Customer")
        ReservationItem.objects.create(
            reservation=reservation, gown=gown, gown_name=gown.name,
            rental_date=date.today(), return_date=date.today() + timedelta(days=3),
        )
        return reservation, gown

    def test_approving_confirms_the_reservation_and_reserves_the_gown(self):
        reservation, gown = self._make_pending_reservation_with_gown()
        response = self.client.post(reverse("arabela_admin:reservation_approve", args=[reservation.id]))
        self.assertEqual(response.status_code, 200, response.content)
        reservation.refresh_from_db()
        gown.refresh_from_db()
        self.assertEqual(reservation.status, "Confirmed")
        self.assertIsNotNone(reservation.reviewed_at)
        self.assertEqual(gown.status, Gown.Status.RESERVED)

    def test_rejecting_stores_the_reason(self):
        reservation, _ = self._make_pending_reservation_with_gown()
        response = self.client.post(
            reverse("arabela_admin:reservation_reject", args=[reservation.id]),
            data=json.dumps({"reason": "Incomplete proof of payment"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, "Rejected")
        self.assertEqual(reservation.notes, "Incomplete proof of payment")

    def test_rejecting_without_a_reason_still_succeeds(self):
        reservation, _ = self._make_pending_reservation_with_gown()
        response = self.client.post(
            reverse("arabela_admin:reservation_reject", args=[reservation.id]),
            data=json.dumps({}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_anonymous_cannot_approve(self):
        reservation, _ = self._make_pending_reservation_with_gown()
        self.client.logout()
        response = self.client.post(reverse("arabela_admin:reservation_approve", args=[reservation.id]))
        self.assertEqual(response.status_code, 401)

    def test_nonexistent_reservation_returns_404(self):
        response = self.client.post(reverse("arabela_admin:reservation_approve", args=[999999]))
        self.assertEqual(response.status_code, 404)


class ReservationItemLifecycleTests(TestCase):
    """`reservation_item_mark_returned_view` / `reservation_item_reschedule_view` --
    the Booking Details modal's two actions. Mark Returned must cascade the gown's
    own condition/status (Needs Repair -> Out-of-Stock, everything else -> Available);
    Reschedule must enforce Pick-up <= Event <= Return <= Overdue and only accept the
    3 settable stages."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="lifecycle_test_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="lifecycle_test_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)

    def _make_item(self, gown_status=Gown.Status.RESERVED):
        from reservations.models import Reservation, ReservationItem
        gown = _make_gown(status=gown_status)
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Lifecycle Customer")
        item = ReservationItem.objects.create(
            reservation=reservation, gown=gown, gown_name=gown.name,
            rental_date=date.today(), return_date=date.today() + timedelta(days=3),
        )
        return item, gown

    def test_mark_returned_good_condition_frees_the_gown(self):
        item, gown = self._make_item()
        response = self.client.post(
            reverse("arabela_admin:reservation_item_mark_returned", args=[item.id]),
            data=json.dumps({"condition": "Good"}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        item.refresh_from_db()
        gown.refresh_from_db()
        self.assertEqual(item.stage, "Returned")
        self.assertEqual(item.return_condition, "Good")
        self.assertEqual(gown.status, Gown.Status.AVAILABLE)
        self.assertEqual(gown.condition, "Good")

    def test_mark_returned_needs_repair_withdraws_the_gown(self):
        item, gown = self._make_item()
        response = self.client.post(
            reverse("arabela_admin:reservation_item_mark_returned", args=[item.id]),
            data=json.dumps({"condition": "Needs Repair"}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        gown.refresh_from_db()
        self.assertEqual(gown.status, Gown.Status.OUT_OF_STOCK)

    def test_mark_returned_missing_condition_is_rejected(self):
        item, _ = self._make_item()
        response = self.client.post(
            reverse("arabela_admin:reservation_item_mark_returned", args=[item.id]),
            data=json.dumps({}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_reschedule_valid_dates_succeeds(self):
        item, gown = self._make_item(gown_status=Gown.Status.AVAILABLE)
        today = date.today()
        # _make_item leaves event_date null, so the modal would show the computed
        # event day (return - 2 = today + 1, see ReservationItem.effective_event_date)
        # -- sent back here exactly as a real Save Changes would, since this test
        # isn't about the Event date at all.
        response = self.client.post(
            reverse("arabela_admin:reservation_item_reschedule", args=[item.id]),
            data=json.dumps({
                "rental_date": today.isoformat(),
                "event_date": (today + timedelta(days=1)).isoformat(),
                "return_date": (today + timedelta(days=4)).isoformat(),
                "overdue_date": (today + timedelta(days=6)).isoformat(),
                "stage": "Reserved",
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        item.refresh_from_db()
        gown.refresh_from_db()
        self.assertEqual(item.stage, "Reserved")
        self.assertEqual(gown.status, Gown.Status.RESERVED)  # leaving Pick-up reserves the gown

    def test_reschedule_out_of_order_dates_is_rejected(self):
        item, _ = self._make_item()
        today = date.today()
        response = self.client.post(
            reverse("arabela_admin:reservation_item_reschedule", args=[item.id]),
            data=json.dumps({
                "rental_date": today.isoformat(),
                "event_date": (today - timedelta(days=1)).isoformat(),  # before rental_date
                "return_date": (today + timedelta(days=4)).isoformat(),
                "overdue_date": (today + timedelta(days=6)).isoformat(),
                "stage": "Reserved",
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_reschedule_to_returned_stage_directly_is_rejected(self):
        # "Returned" is only reachable via Mark Returned, never via Reschedule.
        item, _ = self._make_item()
        today = date.today()
        # Every date is sent exactly as it already stands (event_date as the computed
        # return - 2 = today + 1), so the stage really is the only thing wrong here --
        # otherwise this could trip one of the four dates' own back-dating checks instead
        # and stop proving anything about the stage.
        response = self.client.post(
            reverse("arabela_admin:reservation_item_reschedule", args=[item.id]),
            data=json.dumps({
                "rental_date": today.isoformat(),
                "event_date": (today + timedelta(days=1)).isoformat(),
                "return_date": (today + timedelta(days=3)).isoformat(),
                "overdue_date": (today + timedelta(days=3)).isoformat(),
                "stage": "Returned",
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


class SecurityDepositTests(TestCase):
    """`reservation_return_deposit_view` -- must refuse to release a deposit until
    every item on the reservation is actually marked Returned, and must refuse a
    second release on the same reservation."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="deposit_test_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="deposit_test_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)

    def _make_reservation(self, stage):
        from reservations.models import Reservation, ReservationItem
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Deposit Customer")
        ReservationItem.objects.create(
            reservation=reservation, gown_name="Deposit Test Gown",
            rental_date=date.today(), return_date=date.today() + timedelta(days=3), stage=stage,
        )
        return reservation

    def test_deposit_cannot_be_returned_before_the_item_is_returned(self):
        reservation = self._make_reservation(stage="Reserved")
        response = self.client.post(reverse("arabela_admin:reservation_return_deposit", args=[reservation.id]))
        self.assertEqual(response.status_code, 400)

    def test_deposit_can_be_returned_once_the_item_is_returned(self):
        reservation = self._make_reservation(stage="Returned")
        response = self.client.post(reverse("arabela_admin:reservation_return_deposit", args=[reservation.id]))
        self.assertEqual(response.status_code, 200, response.content)
        reservation.refresh_from_db()
        self.assertIsNotNone(reservation.deposit_returned_at)

    def test_deposit_cannot_be_returned_twice(self):
        reservation = self._make_reservation(stage="Returned")
        self.client.post(reverse("arabela_admin:reservation_return_deposit", args=[reservation.id]))
        response = self.client.post(reverse("arabela_admin:reservation_return_deposit", args=[reservation.id]))
        self.assertEqual(response.status_code, 400)


class CustomerFlagTests(TestCase):
    """`customer_flag_view` -- flagging a customer account requires a reason;
    unflagging doesn't."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="flag_test_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="flag_test_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)
        self.url = reverse("arabela_admin:customer_flag", args=[self.customer.id])

    def test_flagging_with_a_reason_succeeds(self):
        response = self.client.post(
            self.url, data=json.dumps({"flagged": True, "reason": "Suspicious activity"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(UserProfile.objects.get(user=self.customer).is_flagged)

    def test_flagging_without_a_reason_is_rejected(self):
        response = self.client.post(
            self.url, data=json.dumps({"flagged": True}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_unflagging_does_not_require_a_reason(self):
        UserProfile.objects.create(user=self.customer, is_flagged=True)
        response = self.client.post(
            self.url, data=json.dumps({"flagged": False}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(UserProfile.objects.get(user=self.customer).is_flagged)

    def test_cannot_flag_a_staff_account(self):
        staff_target = User.objects.create_user(username="not_a_customer", password="x", is_staff=True)
        response = self.client.post(
            reverse("arabela_admin:customer_flag", args=[staff_target.id]),
            data=json.dumps({"flagged": True, "reason": "test"}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)


class StaffManagementCRUDTests(TestCase):
    """Full owner-only staff account lifecycle, plus the guardrails stopping an owner
    from disabling/deleting their own account or another owner's through this UI."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="crud_test_owner", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.owner, role=UserProfile.Role.OWNER)
        cls.other_owner = User.objects.create_user(username="crud_other_owner", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.other_owner, role=UserProfile.Role.OWNER)

    def setUp(self):
        self.client.force_login(self.owner)

    def test_owner_can_create_a_staff_account(self):
        response = self.client.post(
            reverse("arabela_admin:staff_create"),
            data=json.dumps({"name": "New Staffer", "username": "new_staffer_1", "password": "password123", "role": "Staff"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(User.objects.filter(username="new_staffer_1").exists())

    def test_duplicate_username_is_rejected(self):
        User.objects.create_user(username="taken_name", password="x", is_staff=True)
        response = self.client.post(
            reverse("arabela_admin:staff_create"),
            data=json.dumps({"name": "Dup", "username": "taken_name", "password": "password123", "role": "Staff"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_short_password_is_rejected(self):
        response = self.client.post(
            reverse("arabela_admin:staff_create"),
            data=json.dumps({"name": "Short PW", "username": "short_pw_1", "password": "short", "role": "Staff"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_owner_can_update_a_staff_account(self):
        target = User.objects.create_user(username="update_target", password="x", is_staff=True)
        UserProfile.objects.create(user=target, role=UserProfile.Role.STAFF)
        response = self.client.post(
            reverse("arabela_admin:staff_update", args=[target.id]),
            data=json.dumps({"name": "Renamed Person", "role": "Manager"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(UserProfile.objects.get(user=target).role, "Manager")

    def test_owner_can_toggle_staff_active_status(self):
        target = User.objects.create_user(username="toggle_target", password="x", is_staff=True, is_active=True)
        response = self.client.post(reverse("arabela_admin:staff_toggle_status", args=[target.id]))
        self.assertEqual(response.status_code, 200, response.content)
        target.refresh_from_db()
        self.assertFalse(target.is_active)

    def test_owner_can_delete_a_staff_account(self):
        target = User.objects.create_user(username="delete_target", password="x", is_staff=True)
        response = self.client.post(reverse("arabela_admin:staff_delete", args=[target.id]))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(User.objects.filter(id=target.id).exists())

    def test_owner_cannot_deactivate_their_own_account(self):
        response = self.client.post(reverse("arabela_admin:staff_toggle_status", args=[self.owner.id]))
        self.assertEqual(response.status_code, 400)

    def test_owner_cannot_delete_their_own_account(self):
        response = self.client.post(reverse("arabela_admin:staff_delete", args=[self.owner.id]))
        self.assertEqual(response.status_code, 400)

    def test_owner_cannot_act_on_another_owner_account(self):
        response = self.client.post(reverse("arabela_admin:staff_delete", args=[self.other_owner.id]))
        self.assertEqual(response.status_code, 400)


class AdminLoginLockoutTests(TestCase):
    """Brute-force protection on the admin login: 5 failed attempts locks that
    username out, independent of any other username."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="lockout_test_user", password="correctpassword123", is_staff=True)

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def _attempt(self, username, password):
        return self.client.post(reverse("arabela_admin:admin_login"), data={"username": username, "password": password})

    def test_correct_password_logs_in(self):
        response = self._attempt("lockout_test_user", "correctpassword123")
        self.assertEqual(response.status_code, 302)

    def test_five_failed_attempts_locks_the_account(self):
        for _ in range(5):
            self._attempt("lockout_test_user", "wrongpassword")
        response = self._attempt("lockout_test_user", "correctpassword123")  # even the RIGHT password now
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Too many failed login attempts")

    def test_lockout_does_not_affect_a_different_username(self):
        User.objects.create_user(username="other_lockout_user", password="anotherpassword123", is_staff=True)
        for _ in range(5):
            self._attempt("lockout_test_user", "wrongpassword")
        response = self._attempt("other_lockout_user", "anotherpassword123")
        self.assertEqual(response.status_code, 302)



class GownColorCodeConsistencyTests(TestCase):
    """`_check_color_code_consistency` (arabela_admin/views.py), reached through both
    gown_create_view and gown_update_view via _validate_gown_fields -- a 2-letter code
    must always mean the same color everywhere in the catalog. This closes the exact
    bug found in this project: a gown saved as Blue with Blush's own code (BL), which
    then showed as an unrecognizable 'Other' color when reopened for editing."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="color_consistency_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)
        self.create_url = reverse("arabela_admin:gown_create")

    def _create(self, **overrides):
        data = dict(
            name="Color Test Gown", category=Gown.Category.BELO, color_name="Blue",
            color_code="BU", size=Gown.Size.MEDIUM, rental_price="3500",
        )
        data.update(overrides)
        return self.client.post(self.create_url, data=data)

    # ---------------------------------------------------------------- preset codes
    def test_a_preset_code_with_its_correct_name_is_accepted(self):
        response = self._create(color_name="Blue", color_code="BU")
        self.assertEqual(response.status_code, 200, response.content)

    def test_the_exact_bug_this_closes_is_now_rejected(self):
        """The real mistake this feature exists to prevent: BL is Blush's code, not
        Blue's -- reproduces it exactly."""
        response = self._create(color_name="Blue", color_code="BL")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Blush", response.json()["error"])
        self.assertEqual(Gown.objects.filter(color_name="Blue", color_code="BL").count(), 0)

    def test_the_error_names_both_the_code_and_what_it_already_means(self):
        error = self._create(color_name="Black", color_code="BL").json()["error"]
        self.assertIn("BL", error)
        self.assertIn("Blush", error)

    def test_preset_matching_is_case_insensitive(self):
        response = self._create(color_name="blue", color_code="bu")
        self.assertEqual(response.status_code, 200, response.content)

    def test_a_different_preset_code_with_its_correct_name_is_also_fine(self):
        response = self._create(color_name="Sage Green", color_code="SG")
        self.assertEqual(response.status_code, 200, response.content)

    # ------------------------------------------------------- custom "Other" colors
    def test_a_brand_new_custom_color_not_colliding_with_anything_is_accepted(self):
        response = self._create(color_name="Peacock Blue", color_code="PB")
        self.assertEqual(response.status_code, 200, response.content)

    def test_reusing_the_same_custom_color_on_a_second_gown_is_fine(self):
        """Two real gowns legitimately sharing one custom color must never conflict
        with each other -- only a DIFFERENT color trying to reuse the code should."""
        first = self._create(name="First", color_name="Peacock Blue", color_code="PB")
        second = self._create(name="Second", color_name="Peacock Blue", color_code="PB")
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)

    def test_a_custom_code_already_claimed_by_a_different_color_is_rejected(self):
        self._create(name="First", color_name="Peacock Blue", color_code="PB")
        response = self._create(name="Second", color_name="Pale Beige", color_code="PB")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Peacock Blue", response.json()["error"])

    def test_custom_color_matching_is_also_case_insensitive(self):
        self._create(name="First", color_name="Peacock Blue", color_code="PB")
        response = self._create(name="Second", color_name="peacock blue", color_code="PB")
        self.assertEqual(response.status_code, 200, response.content)


class GownColorCodeConsistencyOnEditTests(TestCase):
    """Same rule, on the edit path -- gown_update_view must exclude the gown being
    edited from the collision check, or every edit would collide with its own
    existing color."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="color_edit_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def _gown(self, **overrides):
        return _make_gown(n=Gown.objects.count() + 1, **overrides)

    def _update(self, gown, **overrides):
        data = dict(
            name=gown.name, category=gown.category, color_name=gown.color_name,
            color_code=gown.color_code, size=gown.size, rental_price="4000",
        )
        data.update(overrides)
        return self.client.post(reverse("arabela_admin:gown_update", args=[gown.id]), data=data)

    def test_re_saving_a_gowns_own_unchanged_color_is_not_a_false_conflict(self):
        gown = self._gown(color_name="Peacock Blue", color_code="PB")
        response = self._update(gown)
        self.assertEqual(response.status_code, 200, response.content)

    def test_correcting_a_gowns_own_mismatched_color_succeeds(self):
        """Simulates fixing exactly the mistake this whole feature was built for:
        a gown wrongly saved as Blue/BL, corrected to Blue/BU."""
        gown = self._gown(color_name="Blue", color_code="ZZ")  # ZZ: unclaimed placeholder
        response = self._update(gown, color_name="Blue", color_code="BU")
        self.assertEqual(response.status_code, 200, response.content)
        gown.refresh_from_db()
        self.assertEqual(gown.color_code, "BU")

    def test_editing_into_a_DIFFERENT_gowns_established_color_is_rejected(self):
        self._gown(color_name="Peacock Blue", color_code="PB")
        other = self._gown(color_name="Pale Beige", color_code="XX")
        response = self._update(other, color_name="Pale Beige", color_code="PB")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Peacock Blue", response.json()["error"])
        other.refresh_from_db()
        self.assertEqual(other.color_code, "XX")  # unchanged -- rejected before saving

    def test_editing_into_a_preset_codes_wrong_name_is_rejected(self):
        gown = self._gown(color_name="Something", color_code="ZZ")
        response = self._update(gown, color_name="Black", color_code="BL")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Blush", response.json()["error"])


class GownUpdateTests(TestCase):
    """`gown_update_view` -- editing an existing gown must never let gown_id or slug
    drift (bookmarked customer product links and the booking-matching logic in
    gowns.views._find_available_unit both depend on slug staying stable), and status/
    photo are deliberately untouched here (they have their own dedicated endpoints)."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="update_test_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)
        self.gown = _make_gown(status=Gown.Status.RESERVED)
        self.original_gown_id = self.gown.gown_id
        self.original_slug = self.gown.slug
        self.url = reverse("arabela_admin:gown_update", args=[self.gown.id])

    def _update(self, **overrides):
        data = dict(
            name="Updated Name", category=Gown.Category.BELO, color_name="Green",
            color_code="GR", size=Gown.Size.LARGE, rental_price="4000",
        )
        data.update(overrides)
        return self.client.post(self.url, data=data)

    def test_valid_edit_updates_the_editable_fields(self):
        response = self._update()
        self.assertEqual(response.status_code, 200, response.content)
        self.gown.refresh_from_db()
        self.assertEqual(self.gown.name, "Updated Name")
        self.assertEqual(self.gown.color_name, "Green")
        self.assertEqual(self.gown.rental_price, Decimal("4000.00"))

    def test_gown_id_and_slug_never_change(self):
        self._update(name="A Totally Different Name")
        self.gown.refresh_from_db()
        self.assertEqual(self.gown.gown_id, self.original_gown_id)
        self.assertEqual(self.gown.slug, self.original_slug)

    def test_status_is_untouched_by_this_endpoint(self):
        self._update()
        self.gown.refresh_from_db()
        self.assertEqual(self.gown.status, Gown.Status.RESERVED)

    def test_invalid_price_is_rejected_and_nothing_is_saved(self):
        response = self._update(rental_price="-5", name="Should Not Save")
        self.assertEqual(response.status_code, 400)
        self.gown.refresh_from_db()
        self.assertNotEqual(self.gown.name, "Should Not Save")

    def test_missing_name_is_rejected(self):
        response = self._update(name="")
        self.assertEqual(response.status_code, 400)

    def test_nonexistent_gown_returns_404(self):
        response = self.client.post(reverse("arabela_admin:gown_update", args=[999999]), data=dict(
            name="X", category=Gown.Category.BELO, color_name="X", color_code="X",
            size=Gown.Size.MEDIUM, rental_price="1000",
        ))
        self.assertEqual(response.status_code, 404)


class GownStatusUpdateTests(TestCase):
    """`gown_status_update_view` -- the single-gown status toggle."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="status_test_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def test_valid_status_change_succeeds(self):
        gown = _make_gown(status=Gown.Status.AVAILABLE)
        response = self.client.post(
            reverse("arabela_admin:gown_status_update", args=[gown.id]),
            data=json.dumps({"status": "Out-of-Stock"}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        gown.refresh_from_db()
        self.assertEqual(gown.status, Gown.Status.OUT_OF_STOCK)

    def test_invalid_status_is_rejected(self):
        gown = _make_gown()
        response = self.client.post(
            reverse("arabela_admin:gown_status_update", args=[gown.id]),
            data=json.dumps({"status": "Not A Real Status"}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


class GownBulkActionTests(TestCase):
    """`gown_bulk_action_view` -- the catalog's multi-select toolbar. Bulk delete must
    skip (not fail) gowns still on a live reservation, exactly like the single-delete
    endpoint, and report which ones were skipped."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="bulk_test_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="bulk_test_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)
        self.url = reverse("arabela_admin:gown_bulk_action")

    def test_bulk_status_update_changes_every_selected_gown(self):
        g1 = _make_gown(1, status=Gown.Status.AVAILABLE)
        g2 = _make_gown(2, status=Gown.Status.AVAILABLE)
        response = self.client.post(self.url, data=json.dumps({
            "action": "status", "status": "Out-of-Stock", "ids": [g1.id, g2.id],
        }), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["updated"], 2)
        g1.refresh_from_db()
        g2.refresh_from_db()
        self.assertEqual(g1.status, Gown.Status.OUT_OF_STOCK)
        self.assertEqual(g2.status, Gown.Status.OUT_OF_STOCK)

    def test_bulk_delete_skips_gowns_on_active_reservations(self):
        from reservations.models import Reservation, ReservationItem
        deletable = _make_gown(1)
        blocked = _make_gown(2)
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Bulk Customer")
        ReservationItem.objects.create(
            reservation=reservation, gown=blocked, gown_name=blocked.name,
            rental_date=date.today(), return_date=date.today() + timedelta(days=3),
        )
        response = self.client.post(self.url, data=json.dumps({
            "action": "delete", "ids": [deletable.id, blocked.id],
        }), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertFalse(Gown.objects.filter(id=deletable.id).exists())
        self.assertTrue(Gown.objects.filter(id=blocked.id).exists())

    def test_empty_selection_is_rejected(self):
        response = self.client.post(self.url, data=json.dumps({"action": "delete", "ids": []}), content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_unknown_action_is_rejected(self):
        gown = _make_gown()
        response = self.client.post(self.url, data=json.dumps({"action": "explode", "ids": [gown.id]}), content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_too_many_ids_rejected(self):
        from arabela_admin.views import _BULK_MAX_IDS
        response = self.client.post(self.url, data=json.dumps({
            "action": "status", "status": "Available", "ids": list(range(1, _BULK_MAX_IDS + 2)),
        }), content_type="application/json")
        self.assertEqual(response.status_code, 400)


class AdminNotificationsTests(TestCase):
    """`admin_notifications` (context_processors.py) -- the derived work-queue feed
    behind the header bell. Nothing here is stored; it's all live-computed from real
    data on every request, so this locks in that each source of "still needs doing"
    actually surfaces, that role-gating works (staff never see the owner-only staff
    roster items), and that critical items always sort first."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="notif_test_staff", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.staff, role=UserProfile.Role.STAFF)
        cls.owner = User.objects.create_user(username="notif_test_owner", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.owner, role=UserProfile.Role.OWNER)
        cls.customer = User.objects.create_user(username="notif_test_customer", password="x")

    def _notifications_as(self, user):
        self.client.force_login(user)
        response = self.client.get(reverse("arabela_admin:dashboard"))
        return response.context["admin_notifications"]

    def test_anonymous_sees_no_notifications(self):
        response = self.client.get(reverse("arabela_admin:dashboard"))
        self.assertRedirects(response, reverse("arabela_admin:admin_login"))

    def test_pending_reservation_appears_as_a_reservation_notification(self):
        from reservations.models import Reservation
        Reservation.objects.create(customer=self.customer, customer_name="Pending Notif Customer")
        kinds = [n["kind"] for n in self._notifications_as(self.staff)]
        self.assertIn("reservation", kinds)

    def test_pending_reservation_with_proof_also_gets_a_payment_notification(self):
        from reservations.models import Reservation
        Reservation.objects.create(
            customer=self.customer, customer_name="Proof Notif Customer",
            payment_proof_url="https://example.test/proof.jpg",
        )
        kinds = [n["kind"] for n in self._notifications_as(self.staff)]
        self.assertIn("payment", kinds)

    def test_overdue_item_appears_as_critical(self):
        from reservations.models import Reservation, ReservationItem
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Overdue Notif Customer")
        ReservationItem.objects.create(
            reservation=reservation, gown_name="Overdue Notif Gown",
            rental_date=date.today() - timedelta(days=10),
            return_date=date.today() - timedelta(days=3),
        )
        notifications = self._notifications_as(self.staff)
        overdue = [n for n in notifications if n["kind"] == "overdue"]
        self.assertEqual(len(overdue), 1)
        self.assertEqual(overdue[0]["level"], "critical")

    def test_rejected_reservations_item_is_not_reported_overdue(self):
        from reservations.models import Reservation, ReservationItem
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Rejected Notif Customer", status=Reservation.Status.REJECTED,
        )
        ReservationItem.objects.create(
            reservation=reservation, gown_name="Rejected Notif Gown",
            rental_date=date.today() - timedelta(days=10),
            return_date=date.today() - timedelta(days=3),
        )
        kinds = [n["kind"] for n in self._notifications_as(self.staff)]
        self.assertNotIn("overdue", kinds)

    def test_out_of_stock_gown_appears_as_inventory_notification(self):
        _make_gown(status=Gown.Status.OUT_OF_STOCK)
        kinds = [n["kind"] for n in self._notifications_as(self.staff)]
        self.assertIn("inventory", kinds)

    def test_flagged_customer_appears_as_customer_notification(self):
        UserProfile.objects.create(user=self.customer, is_flagged=True)
        kinds = [n["kind"] for n in self._notifications_as(self.staff)]
        self.assertIn("customer", kinds)

    def test_inactive_staff_notification_shown_to_owner_not_to_staff(self):
        inactive = User.objects.create_user(username="inactive_notif_staff", password="x", is_staff=True, is_active=False)
        UserProfile.objects.create(user=inactive, role=UserProfile.Role.STAFF)
        owner_kinds = [n["kind"] for n in self._notifications_as(self.owner)]
        staff_kinds = [n["kind"] for n in self._notifications_as(self.staff)]
        self.assertIn("staff", owner_kinds)
        self.assertNotIn("staff", staff_kinds)

    def test_critical_notifications_sort_before_everything_else(self):
        from reservations.models import Reservation, ReservationItem
        # An "info"-level reservation notification, freshly created (newest).
        Reservation.objects.create(customer=self.customer, customer_name="Info Notif Customer")
        # A "critical"-level overdue item, from days ago (older by timestamp).
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Critical Notif Customer")
        ReservationItem.objects.create(
            reservation=reservation, gown_name="Critical Notif Gown",
            rental_date=date.today() - timedelta(days=10),
            return_date=date.today() - timedelta(days=3),
        )
        notifications = self._notifications_as(self.staff)
        self.assertEqual(notifications[0]["level"], "critical")


class AdminListPageSmokeTests(TestCase):
    """Every remaining admin list page that had zero test coverage: confirms each one
    actually renders (200) for a signed-in staff member, both with no data at all
    (the empty-state path) and with real reservations/items in a mix of statuses
    (the populated path) -- catching any template/context crash either shape could
    trigger, the same class of bug the Google-sign-in SocialApp gap in accounts/tests.py
    turned out to be."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="listpage_test_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="listpage_test_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)

    def _assert_page_ok(self, url_name):
        response = self.client.get(reverse(f"arabela_admin:{url_name}"))
        self.assertEqual(response.status_code, 200, f"{url_name} did not render: {response.content[:300]}")

    def test_all_list_pages_render_with_no_data(self):
        for url_name in (
            "rental_schedule", "calendar", "payment_verification", "rental_history",
            "security_deposits", "receipt_records", "active_reservations",
            "pending_approval", "clients",
        ):
            with self.subTest(page=url_name):
                self._assert_page_ok(url_name)

    def test_all_list_pages_render_with_real_data_present(self):
        from reservations.models import Reservation, ReservationItem
        gown = _make_gown()

        pending = Reservation.objects.create(customer=self.customer, customer_name="Pending Customer")
        ReservationItem.objects.create(
            reservation=pending, gown=gown, gown_name=gown.name,
            rental_date=date.today(), return_date=date.today() + timedelta(days=3),
        )

        active = Reservation.objects.create(
            customer=self.customer, customer_name="Active Customer", status=Reservation.Status.ACTIVE,
        )
        ReservationItem.objects.create(
            reservation=active, gown_name="Active Item Gown", stage=ReservationItem.Stage.RESERVED,
            rental_date=date.today(), return_date=date.today() + timedelta(days=3),
        )

        returned = Reservation.objects.create(
            customer=self.customer, customer_name="Returned Customer", status=Reservation.Status.CONFIRMED,
        )
        ReservationItem.objects.create(
            reservation=returned, gown_name="Returned Item Gown", stage=ReservationItem.Stage.RETURNED,
            rental_date=date.today() - timedelta(days=10), return_date=date.today() - timedelta(days=5),
            returned_on=date.today() - timedelta(days=5),
        )

        for url_name in (
            "rental_schedule", "calendar", "payment_verification", "rental_history",
            "security_deposits", "receipt_records", "active_reservations",
            "pending_approval", "clients",
        ):
            with self.subTest(page=url_name):
                self._assert_page_ok(url_name)

    def test_anonymous_redirected_from_every_list_page(self):
        self.client.logout()
        for url_name in (
            "rental_schedule", "payment_verification", "rental_history",
            "security_deposits", "receipt_records", "active_reservations",
            "pending_approval", "clients",
        ):
            with self.subTest(page=url_name):
                response = self.client.get(reverse(f"arabela_admin:{url_name}"))
                self.assertRedirects(response, reverse("arabela_admin:admin_login"))


class StatusEventHookTests(TestCase):
    """Every admin action that changes what a customer sees must leave a timestamped
    row behind. Equally important, the actions that change NOTHING must not: staff
    press Save on the Booking Details modal constantly, and a timeline that logs every
    no-op save buries the real milestones in noise."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="evt_hook_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="evt_hook_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)
        self.today = date.today()
        self.reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Hook Customer",
            status=Reservation.Status.PENDING,
        )
        self.item = ReservationItem.objects.create(
            reservation=self.reservation, gown_name="Hook Gown",
            rental_date=self.today, event_date=self.today + timedelta(days=2),
            return_date=self.today + timedelta(days=4),
            overdue_date=self.today + timedelta(days=5),
        )

    def _labels(self):
        return [e.label for e in self.reservation.status_events.all()]

    def _reschedule(self, **overrides):
        payload = {
            "rental_date": str(self.today),
            "event_date": str(self.today + timedelta(days=2)),
            "return_date": str(self.today + timedelta(days=4)),
            "overdue_date": str(self.today + timedelta(days=5)),
            "stage": "Reserved",
        }
        payload.update(overrides)
        return self.client.post(
            reverse("arabela_admin:reservation_item_reschedule", args=[self.item.id]),
            data=json.dumps(payload), content_type="application/json",
        )

    def test_approving_records_a_staff_event(self):
        self.client.post(reverse("arabela_admin:reservation_approve", args=[self.reservation.id]))
        event = self.reservation.status_events.get(label="Reservation approved")
        self.assertEqual(event.actor, "Staff")
        self.assertIsNone(event.item_id)

    def test_rejecting_records_the_reason_staff_typed(self):
        self.client.post(
            reverse("arabela_admin:reservation_reject", args=[self.reservation.id]),
            data=json.dumps({"reason": "Receipt is unreadable"}), content_type="application/json",
        )
        event = self.reservation.status_events.get(label="Reservation rejected")
        self.assertEqual(event.detail, "Receipt is unreadable")

    def test_marking_returned_records_an_event_against_that_gown(self):
        self.client.post(
            reverse("arabela_admin:reservation_item_mark_returned", args=[self.item.id]),
            data=json.dumps({"condition": "Good"}), content_type="application/json",
        )
        event = self.reservation.status_events.get(label="Hook Gown returned")
        self.assertEqual(event.item_id, self.item.id)
        # The customer sees this entry, so it says nothing about the gown's condition.
        self.assertEqual(event.detail, "Checked in by staff.")
        self.assertFalse(event.staff_only)
        self.assertFalse(self.reservation.status_events.filter(label="Hook Gown needs repair").exists())

    def test_needs_repair_adds_a_staff_only_note_the_customer_never_sees(self):
        from reservations import timeline

        self.client.post(
            reverse("arabela_admin:reservation_item_mark_returned", args=[self.item.id]),
            data=json.dumps({"condition": "Needs Repair"}), content_type="application/json",
        )
        returned = self.reservation.status_events.get(label="Hook Gown returned")
        note = self.reservation.status_events.get(label="Hook Gown needs repair")
        self.assertFalse(returned.staff_only)
        self.assertTrue(note.staff_only)
        self.assertEqual(note.item_id, self.item.id)

        customer_labels = [e.label for e in timeline.for_item(self.item)]
        self.assertIn("Hook Gown returned", customer_labels)
        self.assertNotIn("Hook Gown needs repair", customer_labels)

        admin_labels = [e.label for e in timeline.attach_to_items([self.item])[0].timeline]
        self.assertIn("Hook Gown needs repair", admin_labels)

    def test_fair_is_no_longer_a_return_choice(self):
        response = self.client.post(
            reverse("arabela_admin:reservation_item_mark_returned", args=[self.item.id]),
            data=json.dumps({"condition": "Fair"}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.item.refresh_from_db()
        self.assertNotEqual(self.item.stage, "Returned")
        self.assertEqual(self.reservation.status_events.count(), 0)

    def test_releasing_the_deposit_records_an_event(self):
        self.client.post(
            reverse("arabela_admin:reservation_item_mark_returned", args=[self.item.id]),
            data=json.dumps({"condition": "Good"}), content_type="application/json",
        )
        self.client.post(
            reverse("arabela_admin:reservation_return_deposit", args=[self.reservation.id]))
        self.assertIn("Security deposit returned", self._labels())

    def test_first_stage_move_records_a_pick_up_not_a_generic_status_line(self):
        self._reschedule()
        self.assertIn("Hook Gown picked up", self._labels())
        self.assertFalse([l for l in self._labels() if "status changed to" in l])

    def test_a_save_that_changes_nothing_records_nothing(self):
        self._reschedule()
        before = self.reservation.status_events.count()
        self._reschedule()
        self.assertEqual(self.reservation.status_events.count(), before)

    def test_changing_only_the_dates_records_a_dates_updated_event(self):
        self._reschedule()
        before = self.reservation.status_events.count()
        self._reschedule(return_date=str(self.today + timedelta(days=9)),
                         overdue_date=str(self.today + timedelta(days=10)))
        self.assertEqual(self.reservation.status_events.count(), before + 1)
        self.assertIn("Hook Gown rental dates updated", self._labels())

    def test_a_later_stage_change_records_the_new_status(self):
        self._reschedule()
        # Overdue now requires the return date to have actually passed, and staff may
        # not back-date the schedule (both in reservation_item_reschedule_view) -- so
        # the past window is put on the item directly first, as if it had been booked
        # that way all along, then resubmitted UNCHANGED with only the stage flipped,
        # exactly like a normal Booking Details resave. That's a genuinely valid
        # Overdue transition, not staff rewriting history mid-request.
        self.item.rental_date = self.today - timedelta(days=10)
        self.item.event_date = self.today - timedelta(days=8)
        self.item.return_date = self.today - timedelta(days=4)
        self.item.overdue_date = self.today - timedelta(days=3)
        self.item.save(update_fields=["rental_date", "event_date", "return_date", "overdue_date"])
        self._reschedule(
            stage="Overdue",
            rental_date=str(self.item.rental_date),
            event_date=str(self.item.event_date),
            return_date=str(self.item.return_date),
            overdue_date=str(self.item.overdue_date),
        )
        self.assertIn("Hook Gown status changed to Overdue", self._labels())

    def test_a_rejected_request_records_nothing(self):
        """Validation failures must leave no trace -- a timeline of things that did not
        happen is worse than no timeline."""
        response = self._reschedule(stage="Returned")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.reservation.status_events.count(), 0)

    def test_an_unauthorised_request_records_nothing(self):
        self.client.logout()
        self.client.post(reverse("arabela_admin:reservation_approve", args=[self.reservation.id]))
        self.assertEqual(self.reservation.status_events.count(), 0)


class AdminTimelineRenderTests(TestCase):
    """The three admin pages that expose the timeline must render it from real data,
    with the component's stylesheet present exactly once per page."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="evt_render_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="evt_render_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)
        today = date.today()
        self.reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Render Customer",
            status=Reservation.Status.CONFIRMED,
        )
        self.item = ReservationItem.objects.create(
            reservation=self.reservation, gown_name="Render Gown",
            rental_date=today, event_date=today, return_date=today,
            overdue_date=today, stage=ReservationItem.Stage.RESERVED,
        )
        ReservationStatusEvent.record(
            self.reservation, "Reservation submitted",
            actor=ReservationStatusEvent.Actor.CUSTOMER)
        ReservationStatusEvent.record(
            self.reservation, "Render Gown picked up", item=self.item,
            actor=ReservationStatusEvent.Actor.STAFF)

    def test_active_reservations_renders_the_timeline(self):
        html = self.client.get(reverse("arabela_admin:active_reservations")).content.decode()
        self.assertIn('<div class="arb-tl-panel">', html)
        self.assertIn("Render Gown picked up", html)

    def test_rental_history_renders_the_timeline(self):
        html = self.client.get(reverse("arabela_admin:rental_history")).content.decode()
        self.assertIn('<div class="arb-tl-panel">', html)

    def test_pending_approval_renders_the_timeline(self):
        Reservation.objects.filter(id=self.reservation.id).update(status=Reservation.Status.PENDING)
        html = self.client.get(reverse("arabela_admin:pending_approval")).content.decode()
        self.assertIn('<div class="arb-tl-panel">', html)

    def test_the_component_stylesheet_is_included_exactly_once(self):
        """It is included per PAGE but the timeline markup is included per ROW -- if the
        two are ever confused the stylesheet gets duplicated dozens of times."""
        for name in ("active_reservations", "pending_approval", "rental_history"):
            with self.subTest(page=name):
                html = self.client.get(reverse("arabela_admin:%s" % name)).content.decode()
                self.assertEqual(html.count(".arb-tl-row { display: flex"), 1)

    def test_every_panel_marks_exactly_one_row_as_latest(self):
        html = self.client.get(reverse("arabela_admin:active_reservations")).content.decode()
        self.assertEqual(html.count('<div class="arb-tl-panel">'), html.count(">Latest</span>"))

    def test_no_unrendered_template_syntax_leaks_into_the_page(self):
        html = self.client.get(reverse("arabela_admin:active_reservations")).content.decode()
        for leak in ("{%", "{{", "{#"):
            self.assertNotIn(leak, html)


class DashboardReminderSweepTests(TestCase):
    """The dashboard is this project's stand-in for a cron job -- it is the page staff
    open every day, so the once-daily reminder sweep rides on it. It must fire, must not
    re-fire on every refresh, and must never be able to take the dashboard down with it."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="sweep_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="sweep_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)
        today = date.today()
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Sweep Customer",
            status=Reservation.Status.CONFIRMED)
        ReservationItem.objects.create(
            reservation=reservation, gown_name="Sweep Gown",
            stage=ReservationItem.Stage.RESERVED,
            rental_date=today - timedelta(days=3), event_date=today,
            return_date=today + timedelta(days=1), overdue_date=today + timedelta(days=2))
        self.url = reverse("arabela_admin:dashboard")

    def test_opening_the_dashboard_sends_the_days_reminders(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)
        message = CustomerMessage.objects.get(recipient=self.customer)
        self.assertIn("Sweep Gown", message.body)

    def test_refreshing_the_dashboard_does_not_re_send(self):
        for _ in range(4):
            self.client.get(self.url)
        self.assertEqual(CustomerMessage.objects.filter(recipient=self.customer).count(), 1)

    def test_the_dashboard_still_loads_if_the_sweep_blows_up(self):
        """A courtesy message failing must never cost staff their control panel."""
        with patch("reservations.reminders.run_daily_sweep_if_due",
                   side_effect=RuntimeError("reminder backend exploded")):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CustomerMessage.objects.count(), 0)

    def test_the_sweep_does_not_run_for_a_signed_out_visitor(self):
        self.client.logout()
        self.client.get(self.url)
        self.assertEqual(CustomerMessage.objects.count(), 0)


class StaffSendReminderTests(TestCase):
    """`reservation_item_send_reminder_view` -- the Remind button on Active Reservations.

    The automatic sweep is a safety net; this is the shop's judgement. Staff can see
    things the schedule cannot, so the endpoint stays permissive about WHEN it may be
    used and strict about WHO may use it and WHAT it may claim."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="manual_rem_staff", password="x", is_staff=True,
            first_name="Ana", last_name="Cruz")
        cls.customer = User.objects.create_user(username="manual_rem_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)
        self.today = date.today()
        self.reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Manual Customer",
            status=Reservation.Status.CONFIRMED)
        self.item = ReservationItem.objects.create(
            reservation=self.reservation, gown_name="Manual Gown",
            stage=ReservationItem.Stage.RESERVED,
            rental_date=self.today - timedelta(days=3), event_date=self.today,
            return_date=self.today + timedelta(days=1),
            overdue_date=self.today + timedelta(days=2))
        self.url = reverse("arabela_admin:reservation_item_send_reminder", args=[self.item.id])

    def _post(self, body="Please return the gown by 5pm today.", url=None):
        return self.client.post(url or self.url, data=json.dumps({"body": body}),
                                content_type="application/json")

    def test_staff_reminder_reaches_the_customers_inbox_verbatim(self):
        typed = "Hi po, please return by 5pm, we have another booking. Salamat!"
        self.assertEqual(self._post(typed).status_code, 200)
        message = CustomerMessage.objects.get(recipient=self.customer)
        self.assertEqual(message.body, typed)
        self.assertFalse(message.is_read)
        self.assertEqual(message.category, CustomerMessage.Category.RESERVATION_REMINDER)

    def test_it_is_logged_as_a_staff_action_not_an_automatic_one(self):
        """The timeline must never blur 'the system noticed' with 'a person decided' --
        that distinction is the whole value of the record in a deposit dispute."""
        self._post()
        event = self.reservation.status_events.get(label=reminders.STAFF_EVENT_LABEL)
        self.assertEqual(event.actor, "Staff")
        self.assertEqual(event.item_id, self.item.id)
        self.assertIn("Ana Cruz", event.detail)

    def test_staff_may_send_again_even_after_the_automatic_one_went_out(self):
        """A person clicking send has decided the customer needs to hear from the shop.
        Silently swallowing that because a robot messaged them today would be the system
        overruling the only party who can see the actual situation."""
        reminders.send_due_reminders(self.today)
        self.assertEqual(CustomerMessage.objects.filter(recipient=self.customer).count(), 1)
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(CustomerMessage.objects.filter(recipient=self.customer).count(), 2)

    def test_staff_may_send_twice_in_a_row(self):
        self._post("First nudge.")
        self._post("Second nudge.")
        self.assertEqual(CustomerMessage.objects.filter(recipient=self.customer).count(), 2)

    def test_a_booking_with_nothing_due_can_still_be_reminded(self):
        """Restricting this to near-due bookings would block the exact case staff reach
        for it: they know something the dates do not."""
        ReservationItem.objects.filter(id=self.item.id).update(
            return_date=self.today + timedelta(days=25),
            overdue_date=self.today + timedelta(days=26))
        self.assertEqual(self._post().status_code, 200)

    # ------------------------------------------------------------------ refusals
    def test_an_empty_message_is_refused(self):
        self.assertEqual(self._post("   ").status_code, 400)
        self.assertEqual(CustomerMessage.objects.count(), 0)

    def test_an_already_returned_gown_cannot_be_reminded(self):
        ReservationItem.objects.filter(id=self.item.id).update(
            stage=ReservationItem.Stage.RETURNED)
        self.assertEqual(self._post().status_code, 400)
        self.assertEqual(CustomerMessage.objects.count(), 0)

    def test_a_cancelled_booking_cannot_be_reminded(self):
        Reservation.objects.filter(id=self.reservation.id).update(
            status=Reservation.Status.CANCELLED)
        self.assertEqual(self._post().status_code, 400)
        self.assertEqual(CustomerMessage.objects.count(), 0)

    def test_a_pending_booking_cannot_be_reminded(self):
        Reservation.objects.filter(id=self.reservation.id).update(
            status=Reservation.Status.PENDING)
        self.assertEqual(self._post().status_code, 400)

    def test_a_signed_out_visitor_is_rejected(self):
        self.client.logout()
        self.assertEqual(self._post().status_code, 401)
        self.assertEqual(CustomerMessage.objects.count(), 0)

    def test_a_customer_cannot_send_reminders_to_themselves(self):
        self.client.force_login(self.customer)
        self.assertEqual(self._post().status_code, 401)
        self.assertEqual(CustomerMessage.objects.count(), 0)

    def test_a_missing_item_is_a_clean_404(self):
        missing = reverse("arabela_admin:reservation_item_send_reminder", args=[99999999])
        self.assertEqual(self._post(url=missing).status_code, 404)

    def test_malformed_json_is_refused_cleanly(self):
        response = self.client.post(self.url, data="not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_an_overlong_message_is_trimmed_rather_than_rejected(self):
        self._post("x" * (reminders.MANUAL_BODY_MAX_CHARS + 500))
        message = CustomerMessage.objects.get(recipient=self.customer)
        self.assertEqual(len(message.body), reminders.MANUAL_BODY_MAX_CHARS)


class StaffSendReminderPageTests(TestCase):
    """The Active Reservations page must actually wire the button to the real endpoint.
    Admin JS here has looked fully correct before while pointing at a URL that did not
    exist, so this asserts on the URL the RENDERED PAGE carries, not one reversed here."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="manual_page_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="manual_page_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)
        today = date.today()
        self.reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Page Customer",
            status=Reservation.Status.CONFIRMED)
        self.due_soon = ReservationItem.objects.create(
            reservation=self.reservation, gown_name="Due Soon Gown",
            stage=ReservationItem.Stage.RESERVED,
            rental_date=today - timedelta(days=3), event_date=today,
            return_date=today + timedelta(days=1), overdue_date=today + timedelta(days=2))
        self.returned = ReservationItem.objects.create(
            reservation=self.reservation, gown_name="Returned Gown",
            stage=ReservationItem.Stage.RETURNED,
            rental_date=today - timedelta(days=9), event_date=today - timedelta(days=7),
            return_date=today - timedelta(days=5), overdue_date=today - timedelta(days=4))
        self.html = self.client.get(reverse("arabela_admin:active_reservations")).content.decode()

    def _button_for(self, gown_name):
        for match in re.finditer(r'<button[^>]*resv-remind-btn.*?</button>', self.html, re.S):
            if 'data-gown="%s"' % gown_name in match.group(0):
                return match.group(0)
        return None

    def test_the_button_points_at_the_real_endpoint(self):
        button = self._button_for("Due Soon Gown")
        self.assertIsNotNone(button)
        expected = reverse("arabela_admin:reservation_item_send_reminder", args=[self.due_soon.id])
        self.assertIn('data-url="%s"' % expected, button)

    def test_posting_to_the_url_the_page_rendered_actually_works(self):
        button = self._button_for("Due Soon Gown")
        url = re.search(r'data-url="([^"]*)"', button).group(1)
        response = self.client.post(url, data=json.dumps({"body": "Please return today."}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(CustomerMessage.objects.filter(recipient=self.customer).count(), 1)

    def test_the_message_is_pre_filled_with_the_same_wording_the_sweep_would_send(self):
        button = self._button_for("Due Soon Gown")
        self.assertIn("due back tomorrow", button)

    def test_a_returned_gown_gets_no_button(self):
        self.assertIsNone(self._button_for("Returned Gown"))

    def test_the_page_ships_the_dialog_and_csrf_token_the_button_needs(self):
        self.assertIn("adm-dlg__card", self.html)
        self.assertIn('id="adm-dlg-textarea"', self.html)
        self.assertIn('name="csrfmiddlewaretoken"', self.html)


class AccountSettingsPageTests(TestCase):
    """`page_view`'s "account-settings" branch. Everyone who can sign in to the admin
    panel can see it; only the owner sees (and can use) the password form -- staff get
    a plain explanation instead. Also verifies the "Account settings" link across the
    panel actually points here now (it used to be a leftover `href="messages.html"`
    from the original template pack, 404ing on every single admin page)."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username="acctpage_owner", password="OldPass123", is_staff=True,
            first_name="Ana", last_name="Cruz")
        UserProfile.objects.update_or_create(user=cls.owner, defaults={"role": UserProfile.Role.OWNER})
        cls.staffer = User.objects.create_user(
            username="acctpage_staff", password="StaffPass123", is_staff=True,
            first_name="Bo", last_name="Reyes")
        UserProfile.objects.update_or_create(user=cls.staffer, defaults={"role": UserProfile.Role.STAFF})

    def setUp(self):
        self.url = reverse("arabela_admin:page", args=["account-settings"])

    def test_owner_sees_the_password_form(self):
        self.client.force_login(self.owner)
        html = self.client.get(self.url).content.decode()
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertIn('x-model="currentPassword"', html)
        self.assertIn("Ana Cruz", html)
        self.assertIn("acctpage_owner", html)

    def test_staff_does_not_see_the_password_form(self):
        self.client.force_login(self.staffer)
        html = self.client.get(self.url).content.decode()
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertNotIn('x-model="currentPassword"', html)
        self.assertIn("only the shop owner can change passwords", html.lower())
        self.assertIn("Bo Reyes", html)

    def test_a_signed_out_visitor_is_redirected_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("arabela_admin:admin_login"), response.url)

    def test_no_unrendered_template_syntax_leaks_into_either_view(self):
        for user in (self.owner, self.staffer):
            self.client.force_login(user)
            html = self.client.get(self.url).content.decode()
            for leak in ("{%", "{{", "{#"):
                self.assertNotIn(leak, html)

    def test_the_account_settings_link_on_a_real_page_points_here_now(self):
        """The link used to be a dead `href="messages.html"` leftover from the
        original template pack -- 404ing on every admin page it appeared on."""
        self.client.force_login(self.owner)
        dashboard_html = self.client.get(reverse("arabela_admin:dashboard")).content.decode()
        self.assertNotIn('href="messages.html"', dashboard_html)
        self.assertIn(self.url, dashboard_html)


class ChangeOwnPasswordTests(TestCase):
    """`change_own_password_view` -- by explicit design, ONLY the owner may change any
    password in this panel, including their own. Staff have no self-service password
    change at all; if they need a new one, the owner sets it via Staff Management."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username="chpw_owner", password="OldPass123", is_staff=True)
        UserProfile.objects.update_or_create(user=cls.owner, defaults={"role": UserProfile.Role.OWNER})
        cls.staffer = User.objects.create_user(
            username="chpw_staff", password="StaffPass123", is_staff=True)
        UserProfile.objects.update_or_create(user=cls.staffer, defaults={"role": UserProfile.Role.STAFF})

    def setUp(self):
        self.url = reverse("arabela_admin:change_own_password")

    def _post(self, current="OldPass123", new="NewSecurePass456", confirm=None):
        return self.client.post(self.url, data=json.dumps({
            "current_password": current, "new_password": new,
            "confirm_password": confirm if confirm is not None else new,
        }), content_type="application/json")

    def test_owner_can_change_their_own_password(self):
        self.client.force_login(self.owner)
        response = self._post()
        self.assertEqual(response.status_code, 200, response.content)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password("NewSecurePass456"))
        self.assertFalse(self.owner.check_password("OldPass123"))

    def test_owner_is_not_logged_out_by_changing_their_own_password(self):
        """update_session_auth_hash is the whole reason this matters -- without it the
        owner is silently signed out the instant their own change succeeds."""
        self.client.force_login(self.owner)
        self._post()
        # A follow-up request on the SAME client must still be authenticated.
        still_in = self.client.get(reverse("arabela_admin:dashboard"))
        self.assertEqual(still_in.status_code, 200)

    def test_new_password_actually_works_for_a_real_login_afterward(self):
        self.client.force_login(self.owner)
        self._post()
        fresh = Client()
        fresh.logout()
        self.assertTrue(fresh.login(username="chpw_owner", password="NewSecurePass456"))

    def test_old_password_stops_working_afterward(self):
        self.client.force_login(self.owner)
        self._post()
        fresh = Client()
        self.assertFalse(fresh.login(username="chpw_owner", password="OldPass123"))

    def test_wrong_current_password_is_refused(self):
        self.client.force_login(self.owner)
        response = self._post(current="TotallyWrong")
        self.assertEqual(response.status_code, 400)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password("OldPass123"))

    def test_a_too_short_new_password_is_refused(self):
        self.client.force_login(self.owner)
        response = self._post(new="short", confirm="short")
        self.assertEqual(response.status_code, 400)

    def test_mismatched_confirmation_is_refused(self):
        self.client.force_login(self.owner)
        response = self._post(new="FirstOption1", confirm="SecondOption2")
        self.assertEqual(response.status_code, 400)

    def test_reusing_the_current_password_is_refused(self):
        self.client.force_login(self.owner)
        response = self._post(new="OldPass123", confirm="OldPass123")
        self.assertEqual(response.status_code, 400)

    def test_staff_cannot_change_their_own_password_here(self):
        """The explicit business rule this feature was built around: staff get NO
        self-service password change, full stop -- not even for their own account."""
        self.client.force_login(self.staffer)
        response = self._post(current="StaffPass123", new="StaffWantsThis1")
        self.assertEqual(response.status_code, 403)
        self.staffer.refresh_from_db()
        self.assertTrue(self.staffer.check_password("StaffPass123"))

    def test_staff_cannot_change_the_owners_password_here_either(self):
        self.client.force_login(self.staffer)
        response = self._post(current="OldPass123", new="StaffWantsThis1")
        self.assertEqual(response.status_code, 403)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password("OldPass123"))

    def test_a_signed_out_visitor_cannot_reach_it(self):
        response = self._post()
        self.assertIn(response.status_code, (302, 401, 403))

    def test_malformed_json_is_refused_cleanly_not_a_500(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.url, data="not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)


class ImageViewerFixTests(TestCase):
    """Two real staff complaints, one shared fix: the Gown Photo modal was cropping
    tall gown photos down to a thin horizontal strip (a fixed-height box +
    object-cover), and the payment-proof modals had no way to see an upload any
    bigger than its own (sometimes small/low-res) natural size. Both now show the
    full image uncropped, and both now offer a "View Full Image" link that opens the
    real uploaded file directly, at its true resolution, in a new tab."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="imgview_staff", password="x", is_staff=True)
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.OWNER})
        cls.customer = User.objects.create_user(username="imgview_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)

    def test_gown_photo_modal_no_longer_crops_with_object_cover(self):
        _make_gown(photo_url="https://example.test/gowns/tall.jpg")
        html = self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()
        self.assertNotIn('alt="Current gown photo" class="h-full w-full object-cover"', html)
        self.assertIn("max-height: 60vh", html)

    def test_gown_photo_modal_has_a_view_full_image_link_bound_to_the_real_url(self):
        _make_gown(photo_url="https://example.test/gowns/tall.jpg")
        html = self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()
        self.assertIn(':href="viewingGownImage.photoUrl"', html)
        self.assertIn('target="_blank"', html)
        self.assertIn('rel="noopener"', html)

    def _make_reservation_with_proof(self, proof_url="https://example.test/proofs/small.jpg"):
        today = date.today()
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Image View Customer",
            status=Reservation.Status.PENDING, payment_proof_url=proof_url,
            payment_method="GCash", total_amount=Decimal("5000"))
        ReservationItem.objects.create(
            reservation=reservation, gown_name="Image View Item",
            rental_date=today, return_date=today + timedelta(days=3))
        return reservation

    def test_payment_verification_has_a_view_full_image_link(self):
        self._make_reservation_with_proof()
        html = self.client.get(reverse("arabela_admin:payment_verification")).content.decode()
        self.assertIn(':href="viewingPayment.proofUrl"', html)
        self.assertIn('target="_blank"', html)

    def test_security_deposits_has_a_view_full_image_link(self):
        self._make_reservation_with_proof()
        html = self.client.get(reverse("arabela_admin:security_deposits")).content.decode()
        self.assertIn(':href="viewingPayment.proofUrl"', html)
        self.assertIn('target="_blank"', html)

    def test_no_unrendered_template_syntax_on_any_of_the_three_pages(self):
        _make_gown(photo_url="https://example.test/gowns/tall.jpg")
        self._make_reservation_with_proof()
        for url_name in ("gown_catalog", "payment_verification", "security_deposits"):
            with self.subTest(page=url_name):
                html = self.client.get(reverse(f"arabela_admin:{url_name}")).content.decode()
                for leak in ("{%", "{{", "{#"):
                    self.assertNotIn(leak, html)


def _make_jpeg(name="receipt.jpg"):
    tiny_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    return SimpleUploadedFile(name, tiny_jpeg, content_type="image/jpeg")


class ReceiptRecordsTests(TestCase):
    """Receipt Records -- staff attach a photo of a manually-issued receipt (the shop's
    own paper receipt) to a real reservation. Deliberately the opposite of
    Reservation.payment_proof_url (the customer's own GCash screenshot, captured
    automatically at checkout): this is produced by the shop, attached by staff,
    afterward. _save_receipt_photo is mocked in every test -- this environment's
    default storage is real Cloudinary, and these tests must never upload anything to
    that live external account."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="receipt_test_staff", password="x", is_staff=True,
            first_name="Ana", last_name="Cruz")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.OWNER})
        cls.customer = User.objects.create_user(username="receipt_test_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)
        today = date.today()
        self.reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Receipt Test Customer",
            status=Reservation.Status.CONFIRMED, total_amount=Decimal("5000"))
        ReservationItem.objects.create(
            reservation=self.reservation, gown_name="Receipt Test Item",
            rental_date=today, return_date=today + timedelta(days=3))
        patcher = patch("arabela_admin.views._save_receipt_photo",
                        side_effect=lambda f: f"https://example.test/receipts/{f.name}")
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_page_no_longer_shows_leftover_demo_data(self):
        html = self.client.get(reverse("arabela_admin:receipt_records")).content.decode()
        self.assertNotIn("RSV-0142", html)
        self.assertNotIn("Maria Santos", html)
        self.assertNotIn("Customer Name</label>", html)
        # The booking is searched for and PICKED (never a typed customer name), and the
        # receipt is still filed under that reservation's reference code.
        self.assertIn(reverse("arabela_admin:receipt_reservation_search"), html)
        self.assertIn("pickReservation(", html)
        for leak in ("{%", "{{", "{#"):
            self.assertNotIn(leak, html)

    def test_uploading_by_reference_code_resolves_the_real_customer_name(self):
        """The whole point: staff never type a customer name -- it comes from the
        reservation, so it can never drift from the booking it's actually attached to."""
        response = self.client.post(reverse("arabela_admin:receipt_upload"), data={
            "reference_code": self.reservation.reference_code.lower(),
            "photo": _make_jpeg(),
        })
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["receipt"]
        self.assertEqual(data["customer"], "Receipt Test Customer")
        self.assertEqual(data["reservation"], self.reservation.reference_code)
        self.assertTrue(data["photoUrl"])

    def test_a_real_record_is_created_and_attributed_to_the_uploader(self):
        self.client.post(reverse("arabela_admin:receipt_upload"), data={
            "reference_code": self.reservation.reference_code, "photo": _make_jpeg(),
        })
        receipt = ReceiptRecord.objects.get(reservation=self.reservation)
        self.assertEqual(receipt.uploaded_by_id, self.staff.id)
        self.assertTrue(receipt.photo_url)

    def test_unknown_reference_code_is_a_clean_404(self):
        response = self.client.post(reverse("arabela_admin:receipt_upload"), data={
            "reference_code": "RSV-2026-DOES-NOT-EXIST", "photo": _make_jpeg(),
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ReceiptRecord.objects.count(), 0)

    def test_a_blank_reference_code_is_refused(self):
        response = self.client.post(reverse("arabela_admin:receipt_upload"),
                                    data={"reference_code": "   "})
        self.assertEqual(response.status_code, 400)

    def test_a_missing_file_is_refused(self):
        response = self.client.post(reverse("arabela_admin:receipt_upload"),
                                    data={"reference_code": self.reservation.reference_code})
        self.assertEqual(response.status_code, 400)

    def test_a_disallowed_file_type_is_refused(self):
        bad_file = SimpleUploadedFile("virus.exe", b"not an image",
                                      content_type="application/octet-stream")
        response = self.client.post(reverse("arabela_admin:receipt_upload"), data={
            "reference_code": self.reservation.reference_code, "photo": bad_file,
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ReceiptRecord.objects.count(), 0)

    def test_replacing_the_photo_updates_the_record_but_not_the_upload_time(self):
        self.client.post(reverse("arabela_admin:receipt_upload"), data={
            "reference_code": self.reservation.reference_code, "photo": _make_jpeg("first.jpg"),
        })
        receipt = ReceiptRecord.objects.get(reservation=self.reservation)
        original_uploaded_at = receipt.uploaded_at

        response = self.client.post(
            reverse("arabela_admin:receipt_replace", args=[receipt.id]),
            data={"photo": _make_jpeg("second.jpg")})
        self.assertEqual(response.status_code, 200, response.content)

        receipt.refresh_from_db()
        self.assertIn("second.jpg", receipt.photo_url)
        self.assertEqual(receipt.uploaded_at, original_uploaded_at)

    def test_replacing_a_nonexistent_receipt_is_a_clean_404(self):
        response = self.client.post(
            reverse("arabela_admin:receipt_replace", args=[999999]), data={"photo": _make_jpeg()})
        self.assertEqual(response.status_code, 404)

    def test_a_signed_out_visitor_cannot_upload(self):
        self.client.logout()
        response = self.client.post(reverse("arabela_admin:receipt_upload"), data={
            "reference_code": self.reservation.reference_code, "photo": _make_jpeg(),
        })
        self.assertIn(response.status_code, (302, 401))
        self.assertEqual(ReceiptRecord.objects.count(), 0)

    def test_stat_cards_reflect_real_counts(self):
        other = User.objects.create_user(username="receipt_test_other", password="x")
        other_reservation = Reservation.objects.create(
            customer=other, customer_name="Other Customer", status=Reservation.Status.CONFIRMED)
        for reservation, name in ((self.reservation, "a.jpg"), (other_reservation, "b.jpg")):
            self.client.post(reverse("arabela_admin:receipt_upload"), data={
                "reference_code": reservation.reference_code, "photo": _make_jpeg(name),
            })
        html = self.client.get(reverse("arabela_admin:receipt_records")).content.decode()
        self.assertIn(">2<", html)  # total receipts and reservations covered both == 2

    def test_a_second_receipt_on_the_same_reservation_is_allowed(self):
        """No one-per-booking constraint -- a redo or a second physical receipt for a
        partial payment must not be blocked."""
        for name in ("first.jpg", "second.jpg"):
            response = self.client.post(reverse("arabela_admin:receipt_upload"), data={
                "reference_code": self.reservation.reference_code, "photo": _make_jpeg(name),
            })
            self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            ReceiptRecord.objects.filter(reservation=self.reservation).count(), 2)

    def test_view_full_image_link_is_present_and_bound_to_the_real_url(self):
        self.client.post(reverse("arabela_admin:receipt_upload"), data={
            "reference_code": self.reservation.reference_code, "photo": _make_jpeg(),
        })
        html = self.client.get(reverse("arabela_admin:receipt_records")).content.decode()
        self.assertIn(':href="viewingReceipt.photoUrl"', html)
        self.assertIn('target="_blank"', html)

    def test_each_receipt_row_names_the_staff_member_who_uploaded_it(self):
        response = self.client.post(reverse("arabela_admin:receipt_upload"), data={
            "reference_code": self.reservation.reference_code, "photo": _make_jpeg(),
        })
        self.assertEqual(response.json()["receipt"]["uploadedBy"], "Ana Cruz")
        html = self.client.get(reverse("arabela_admin:receipt_records")).content.decode()
        self.assertIn(">Uploaded By<", html)


class ReservationRecordsTests(TestCase):
    """Reservation Records -- one row per reservation with its dates, where it actually
    is, the deposit, and every manual receipt (with who uploaded it), plus the live
    reservation search behind Receipt Records' picker. _save_receipt_photo is mocked:
    this environment's default storage is real Cloudinary."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="records_test_staff", password="x", is_staff=True,
            first_name="Ana", last_name="Cruz")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.STAFF})
        cls.customer = User.objects.create_user(
            username="records_test_customer", password="x", email="records.maria@gmail.com")
        UserProfile.objects.update_or_create(user=cls.customer, defaults={"display_name": "Maria Records"})

    def setUp(self):
        self.client.force_login(self.staff)
        patcher = patch("arabela_admin.views._save_receipt_photo",
                        side_effect=lambda f: f"https://example.test/receipts/{f.name}")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.today = date.today()
        self.two_gowns = Reservation.objects.create(
            customer=self.customer, customer_name="Maria Records",
            status=Reservation.Status.CONFIRMED, security_deposit=Decimal("4000"))
        ReservationItem.objects.create(
            reservation=self.two_gowns, gown_name="Records Gown A",
            rental_date=self.today - timedelta(days=1), return_date=self.today + timedelta(days=3),
            stage=ReservationItem.Stage.RESERVED)
        ReservationItem.objects.create(
            reservation=self.two_gowns, gown_name="Records Gown B",
            rental_date=self.today, return_date=self.today + timedelta(days=5))

    def _records(self):
        html = self.client.get(reverse("arabela_admin:reservation_records")).content.decode()
        match = re.search(
            r'<script id="reservation-records-data" type="application/json">(.*?)</script>', html, re.S)
        return {r["reference"]: r for r in json.loads(match.group(1))}

    def test_one_row_per_reservation_with_its_full_window(self):
        row = self._records()[self.two_gowns.reference_code]
        self.assertEqual(row["gownSummary"], "Records Gown A + 1 more")
        self.assertEqual(row["start"], (self.today - timedelta(days=1)).isoformat())
        self.assertEqual(row["end"], (self.today + timedelta(days=5)).isoformat())
        self.assertEqual(len(row["items"]), 2)

    def test_status_follows_the_gowns_not_the_frozen_reservation_status(self):
        # reservation.status stays "Confirmed" after the gown goes out and comes back;
        # the row must say where the booking actually is.
        self.assertEqual(self._records()[self.two_gowns.reference_code]["progress"], "With customer")
        self.two_gowns.items.update(stage=ReservationItem.Stage.RETURNED)
        self.assertEqual(self._records()[self.two_gowns.reference_code]["progress"], "Completed")

    def test_deposit_status_and_security_deposits_link(self):
        row = self._records()[self.two_gowns.reference_code]
        self.assertEqual(row["depositStatus"], "Held")
        self.assertEqual(row["deposit"], "₱4,000.00")
        self.assertTrue(row["depositLink"].endswith("?search=" + self.two_gowns.reference_code))
        cancelled = Reservation.objects.create(
            customer=self.customer, customer_name="Maria Records", status=Reservation.Status.CANCELLED)
        row = self._records()[cancelled.reference_code]
        self.assertEqual(row["depositStatus"], "Not held")
        self.assertEqual(row["depositLink"], "")

    def test_receipts_show_who_uploaded_them(self):
        self.client.post(reverse("arabela_admin:receipt_upload"), data={
            "reference_code": self.two_gowns.reference_code, "photo": _make_jpeg(),
        })
        receipts = self._records()[self.two_gowns.reference_code]["receipts"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["uploadedBy"], "Ana Cruz")

    def test_staff_only(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("arabela_admin:reservation_records")).status_code, 302)
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(reverse("arabela_admin:reservation_records")).status_code, 302)
        response = self.client.get(reverse("arabela_admin:receipt_reservation_search"), {"q": "maria"})
        self.assertEqual(response.status_code, 401)

    def test_picker_search_finds_by_name_email_reference_and_gown(self):
        url = reverse("arabela_admin:receipt_reservation_search")
        for query in ("Maria Records", "records.maria", self.two_gowns.reference_code, "Records Gown B"):
            results = self.client.get(url, {"q": query}).json()["results"]
            self.assertIn(self.two_gowns.reference_code, [r["reference"] for r in results], query)
        self.assertEqual(self.client.get(url, {"q": "m"}).json()["results"], [])
        self.assertEqual(self.client.get(url, {"q": "nobody-by-this-name"}).json()["results"], [])

    def test_client_list_links_to_the_customers_history(self):
        html = self.client.get(reverse("arabela_admin:clients")).content.decode()
        self.assertIn(
            reverse("arabela_admin:reservation_records") + "?customer=' + viewingCustomer.userId", html)
