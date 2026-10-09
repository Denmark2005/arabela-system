import json
import re
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escapejs

from accounts.models import CustomerMessage, UserProfile
from gowns.models import (
    DEFAULT_CATEGORY_TAG_COLORS, TAG_COLOR_HEX, CustomCategory, Gown, GownRemoval, GownUnavailability,
    HiddenCategory, SiteSettings, all_category_names, resolve_tag_colors,
)
from arabela_admin import views as views_module
from reservations import reminders
from reservations.models import ReceiptRecord, Reservation, ReservationItem, ReservationStatusEvent

User = get_user_model()


def _make_gown(n=1, **overrides):
    defaults = dict(
        gown_id=f"BLOCKTEST-{n:04d}", name=f"Block Test Gown {n}",
        category=Gown.Category.GUEST_GOWN, color_name="Red", color_code="RD",
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
            name="New Test Gown", category=Gown.Category.GUEST_GOWN, color_name="Blue",
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

    def _delete(self, gown, **body):
        # Every removal now has to say why (see GownRemovalLogTests); these tests are about
        # the reservation guard, so they just supply a valid reason.
        payload = {"reason": "Damaged", **body}
        return self.client.post(
            reverse("arabela_admin:gown_delete", args=[gown.id]),
            data=json.dumps(payload), content_type="application/json",
        )

    def test_gown_with_no_reservations_can_be_deleted(self):
        gown = _make_gown()
        response = self._delete(gown)
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
        response = self._delete(gown)
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
        response = self._delete(gown)
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
        response = self._delete(gown)
        self.assertEqual(response.status_code, 200, response.content)


class CategoryRowsRealCountsTests(TestCase):
    """`gown_catalog_view`'s `category_rows` -- the browse-by-category chip strip,
    folded in from the old, now-removed standalone Categories page (bug #9 from the
    original audit: that page used to show 11 hardcoded fake inventory numbers with
    no connection to the database at all). One shared list now backs the chip strip
    AND both category `<select>` dropdowns on this same page, instead of each
    hardcoding its own copy of every category."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="category_rows_test_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def _row(self, response, key):
        return next(r for r in response.context["category_rows"] if r["key"] == key)

    def test_counts_reflect_real_gowns_and_exclude_out_of_stock(self):
        _make_gown(1, category=Gown.Category.GUEST_GOWN, status=Gown.Status.AVAILABLE)
        _make_gown(2, category=Gown.Category.GUEST_GOWN, status=Gown.Status.RESERVED)
        _make_gown(3, category=Gown.Category.GUEST_GOWN, status=Gown.Status.OUT_OF_STOCK)
        _make_gown(4, category=Gown.Category.SUIT, status=Gown.Status.AVAILABLE)

        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._row(response, Gown.Category.GUEST_GOWN)["count"], 2)  # excludes the Out-of-Stock one
        self.assertEqual(self._row(response, Gown.Category.SUIT)["count"], 1)

    def test_category_with_zero_gowns_shows_zero_not_a_fake_number(self):
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._row(response, Gown.Category.DRESSES)["count"], 0)

    def test_every_category_appears_exactly_once_in_declaration_order(self):
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        keys = [r["key"] for r in response.context["category_rows"]]
        self.assertEqual(keys, [key for key, _ in Gown.Category.choices])


class GownCatalogBlockedGownsListTests(TestCase):
    """The "Blocked Gowns" tile/list that replaced the old, unclickable "Needs
    Attention" tile -- every gown with an active block TODAY, surfaced right here
    in Inventory (previously only visible on the Rental Schedule calendar)."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="blocked_list_test_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)
        self.today = date.today()

    def test_gown_with_a_block_covering_today_is_listed(self):
        gown = _make_gown(1)
        GownUnavailability.objects.create(
            gown=gown, start_date=self.today - timedelta(days=1),
            end_date=self.today + timedelta(days=2), reason=GownUnavailability.Reason.CLEANING,
        )
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        rows = response.context["blocked_gowns_today"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["gown"].id, gown.id)
        self.assertEqual(rows[0]["block"]["reason"], "Cleaning")
        self.assertContains(response, "Blocked Gowns")
        self.assertContains(response, gown.gown_id)

    def test_gown_with_only_a_future_block_is_not_listed(self):
        gown = _make_gown(2)
        GownUnavailability.objects.create(
            gown=gown, start_date=self.today + timedelta(days=5),
            end_date=self.today + timedelta(days=8), reason=GownUnavailability.Reason.REPAIR,
        )
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertEqual(response.context["blocked_gowns_today"], [])

    def test_gown_with_only_an_expired_block_is_not_listed(self):
        gown = _make_gown(3)
        GownUnavailability.objects.create(
            gown=gown, start_date=self.today - timedelta(days=10),
            end_date=self.today - timedelta(days=1), reason=GownUnavailability.Reason.ALTERATION,
        )
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertEqual(response.context["blocked_gowns_today"], [])

    def test_a_block_ending_exactly_today_still_counts_as_active(self):
        gown = _make_gown(4)
        GownUnavailability.objects.create(
            gown=gown, start_date=self.today - timedelta(days=3),
            end_date=self.today, reason=GownUnavailability.Reason.CLEANING,
        )
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertEqual(len(response.context["blocked_gowns_today"]), 1)

    def test_sorted_soonest_to_free_up_first(self):
        gown_a = _make_gown(5)
        gown_b = _make_gown(6)
        GownUnavailability.objects.create(
            gown=gown_a, start_date=self.today, end_date=self.today + timedelta(days=10),
            reason=GownUnavailability.Reason.REPAIR,
        )
        GownUnavailability.objects.create(
            gown=gown_b, start_date=self.today, end_date=self.today + timedelta(days=2),
            reason=GownUnavailability.Reason.CLEANING,
        )
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        rows = response.context["blocked_gowns_today"]
        self.assertEqual([r["gown"].id for r in rows], [gown_b.id, gown_a.id])

    def test_zero_blocked_gowns_shows_the_empty_state_not_a_button(self):
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertEqual(response.context["blocked_gowns_today"], [])
        self.assertContains(response, "None blocked today")

    def test_old_needs_attention_tile_wording_is_gone(self):
        # "Needs Attention" itself still legitimately appears in the shared
        # notifications-bell partial included on every admin page -- only the old
        # stat tile's own subtitle text is unique to what was just replaced.
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertNotContains(response, "blocked by date today")


class GownCatalogTotalTileTests(TestCase):
    """The 4th "Total Gowns" stat tile -- unlike Available/Reserved/Needs
    Attention, this one must count EVERY status, including Out-of-Stock."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="total_tile_test_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def test_total_counts_every_status_including_out_of_stock(self):
        _make_gown(1, status=Gown.Status.AVAILABLE)
        _make_gown(2, status=Gown.Status.RESERVED)
        _make_gown(3, status=Gown.Status.OUT_OF_STOCK)

        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_gowns_count"], 3)
        self.assertContains(response, "Total Gowns")

    def test_total_matches_the_real_gown_table_count(self):
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertEqual(response.context["total_gowns_count"], Gown.objects.count())


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
    """`reservation_item_mark_returned_view` -- Mark Returned must cascade the gown's
    own condition/status (Needs Repair -> Out-of-Stock, everything else -> Available),
    without clobbering a still-active sibling booking of the same gown."""

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

    def test_mark_returned_does_not_clobber_a_still_active_sibling_booking(self):
        """Real bug: the same physical gown can be booked twice for non-overlapping
        dates (normal and fine). Returning the LATER booking used to unconditionally
        set Gown.status back to Available even while an EARLIER booking of that same
        gown was still genuinely out -- silently mislabeling it as free in Gown
        Catalog. Reproduces it with two items sharing one gown."""
        from reservations.models import Reservation, ReservationItem
        gown = _make_gown(status=Gown.Status.RESERVED)
        still_out = ReservationItem.objects.create(
            reservation=Reservation.objects.create(
                customer=self.customer, customer_name="Still Out Customer",
                status=Reservation.Status.CONFIRMED),
            gown=gown, gown_name=gown.name, stage=ReservationItem.Stage.RESERVED,
            rental_date=date.today() - timedelta(days=3), return_date=date.today() + timedelta(days=2),
        )
        later_booking = ReservationItem.objects.create(
            reservation=Reservation.objects.create(customer=self.customer, customer_name="Later Booking Customer"),
            gown=gown, gown_name=gown.name,
            rental_date=date.today() + timedelta(days=10), return_date=date.today() + timedelta(days=13),
        )
        response = self.client.post(
            reverse("arabela_admin:reservation_item_mark_returned", args=[later_booking.id]),
            data=json.dumps({"condition": "Good"}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        gown.refresh_from_db()
        self.assertEqual(gown.status, Gown.Status.RESERVED)
        still_out.refresh_from_db()
        self.assertEqual(still_out.stage, ReservationItem.Stage.RESERVED)

    def test_a_stale_pending_booking_does_not_keep_a_returned_gown_reserved(self):
        """Real bug (Wedding Gown 14): a Pending booking whose dates had long passed -- never
        approved, never rejected -- counted as 'still holding' the gown, so returning the
        real booking left it on Reserved in the catalog forever. Only an APPROVED booking
        ever made the gown Reserved, so only an approved one may keep it there."""
        from reservations.models import Reservation, ReservationItem
        gown = _make_gown(status=Gown.Status.RESERVED)
        ReservationItem.objects.create(
            reservation=Reservation.objects.create(customer=self.customer, customer_name="Stale Pending Customer"),
            gown=gown, gown_name=gown.name,
            rental_date=date.today() - timedelta(days=5), return_date=date.today() - timedelta(days=1),
        )
        real = ReservationItem.objects.create(
            reservation=Reservation.objects.create(
                customer=self.customer, customer_name="Real Customer", status=Reservation.Status.CONFIRMED),
            gown=gown, gown_name=gown.name, stage=ReservationItem.Stage.RESERVED,
            rental_date=date.today(), return_date=date.today() + timedelta(days=3),
        )
        response = self.client.post(
            reverse("arabela_admin:reservation_item_mark_returned", args=[real.id]),
            data=json.dumps({"condition": "Good"}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        gown.refresh_from_db()
        self.assertEqual(gown.status, Gown.Status.AVAILABLE)

    def test_an_approved_booking_still_keeps_the_gown_reserved(self):
        from reservations.models import Reservation, ReservationItem
        gown = _make_gown(status=Gown.Status.RESERVED)
        ReservationItem.objects.create(
            reservation=Reservation.objects.create(
                customer=self.customer, customer_name="Approved Later", status=Reservation.Status.CONFIRMED),
            gown=gown, gown_name=gown.name,
            rental_date=date.today() + timedelta(days=20), return_date=date.today() + timedelta(days=24),
        )
        real = ReservationItem.objects.create(
            reservation=Reservation.objects.create(
                customer=self.customer, customer_name="Real Customer", status=Reservation.Status.CONFIRMED),
            gown=gown, gown_name=gown.name, stage=ReservationItem.Stage.RESERVED,
            rental_date=date.today(), return_date=date.today() + timedelta(days=3),
        )
        self.client.post(
            reverse("arabela_admin:reservation_item_mark_returned", args=[real.id]),
            data=json.dumps({"condition": "Good"}), content_type="application/json",
        )
        gown.refresh_from_db()
        self.assertEqual(gown.status, Gown.Status.RESERVED)

    def test_mark_returned_missing_condition_is_rejected(self):
        item, _ = self._make_item()
        response = self.client.post(
            reverse("arabela_admin:reservation_item_mark_returned", args=[item.id]),
            data=json.dumps({}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


class SecurityDepositTests(TestCase):
    """The security deposit follows the gowns: it counts as returned the moment EVERY gown on the
    booking is Returned -- nothing for staff to press, no total value shown, and the customer's
    timeline says so once."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="deposit_test_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="deposit_test_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)

    def _reservation(self, *stages, name="Deposit Customer"):
        from reservations.models import Reservation, ReservationItem
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name=name, status=Reservation.Status.CONFIRMED,
            security_deposit=Decimal("2000"),
        )
        items = [
            ReservationItem.objects.create(
                reservation=reservation, gown_name=f"Deposit Gown {n}", stage=stage,
                rental_date=date.today() - timedelta(days=4), return_date=date.today() - timedelta(days=1),
            ) for n, stage in enumerate(stages, start=1)
        ]
        return reservation, items

    def _return(self, item, condition="Good"):
        return self.client.post(
            reverse("arabela_admin:reservation_item_mark_returned", args=[item.id]),
            data=json.dumps({"condition": condition}), content_type="application/json",
        )

    def test_there_is_no_return_deposit_endpoint_any_more(self):
        from django.urls import NoReverseMatch
        with self.assertRaises(NoReverseMatch):
            reverse("arabela_admin:reservation_return_deposit", args=[1])

    def test_the_deposit_is_held_while_any_gown_is_still_out(self):
        reservation, _ = self._reservation("Returned", "Reserved")
        self.assertIsNone(reservation.deposit_returned_on)
        self.assertFalse(reservation.deposit_is_returned)

    def test_the_deposit_is_returned_once_every_gown_is_back_with_the_last_return_date(self):
        reservation, (first, second) = self._reservation("Returned", "Reserved")
        self.assertEqual(self._return(second).status_code, 200)
        reservation.refresh_from_db()
        self.assertTrue(reservation.deposit_is_returned)
        self.assertEqual(reservation.deposit_returned_on, date.today())

    def test_a_deposit_already_marked_returned_by_hand_keeps_its_recorded_date(self):
        reservation, _ = self._reservation("Returned")
        reservation.deposit_returned_at = timezone.now() - timedelta(days=9)
        reservation.save(update_fields=["deposit_returned_at"])
        self.assertEqual(reservation.deposit_returned_on, (timezone.localtime(reservation.deposit_returned_at)).date())

    def test_a_booking_with_no_gowns_never_counts_as_returned(self):
        reservation, _ = self._reservation()
        self.assertIsNone(reservation.deposit_returned_on)

    def test_the_customer_timeline_is_told_once_when_the_last_gown_comes_back(self):
        reservation, (first, second) = self._reservation("Reserved", "Reserved")
        self._return(first)
        self.assertFalse(reservation.status_events.filter(label="Security deposit settled").exists())  # one still out
        self._return(second)
        event = reservation.status_events.get(label="Security deposit settled")
        self.assertEqual(event.detail, "Your security deposit has been settled. This reservation is complete.")
        self.assertFalse(event.staff_only)
        self.assertEqual(reservation.status_events.filter(label="Security deposit settled").count(), 1)

    def test_a_needs_repair_return_still_settles_the_deposit(self):
        reservation, (item,) = self._reservation("Reserved")
        self._return(item, "Needs Repair")
        self.assertTrue(reservation.status_events.filter(label="Security deposit settled").exists())
        reservation.refresh_from_db()
        self.assertTrue(reservation.deposit_is_returned)

    def test_the_page_has_no_button_no_total_and_no_waiting_text(self):
        self._reservation("Returned", name="Back Customer")
        self._reservation("Reserved", name="Out Customer")
        html = self.client.get(reverse("arabela_admin:security_deposits")).content.decode()
        for gone in ("Return Deposit", "Total Held Value", "Awaiting gown return", "returnDeposit", "return-deposit"):
            self.assertNotIn(gone, html)
        self.assertIn("Deposits Held", html)
        self.assertIn("Returned This Month", html)
        self.assertIn("View Payment", html)

    def test_the_page_counts_held_and_returned_this_month(self):
        self._reservation("Returned", name="A")
        self._reservation("Returned", name="B")
        self._reservation("Reserved", name="C")
        response = self.client.get(reverse("arabela_admin:security_deposits"))
        self.assertEqual(response.context["held_count"], 1)
        self.assertEqual(response.context["returned_this_month"], 2)
        self.assertNotIn("total_held_value", response.context)
        html = response.content.decode()
        self.assertEqual(html.count(">Returned</p>"), 2)
        self.assertEqual(html.count(">Held</p>"), 1)
        self.assertIn(f"Returned on {date.today():%b} {date.today().day}, {date.today().year}", html)

    def test_reservation_records_reports_the_automatic_status(self):
        reservation, _ = self._reservation("Returned", name="Records Back")
        html = self.client.get(reverse("arabela_admin:reservation_records")).content.decode()
        match = re.search(r'<script id="reservation-records-data" type="application/json">(.*?)</script>', html, re.S)
        row = {r["reference"]: r for r in json.loads(match.group(1))}[reservation.reference_code]
        self.assertEqual(row["depositStatus"], "Returned")
        self.assertTrue(row["depositReturnedOn"])

    def test_the_old_still_held_notification_no_longer_exists(self):
        self._reservation("Returned", name="Notify Customer")
        html = self.client.get(reverse("arabela_admin:dashboard")).content.decode()
        self.assertNotIn("deposit still held", html)


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
            name="Color Test Gown", category=Gown.Category.GUEST_GOWN, color_name="Blue",
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
            name="Updated Name", category=Gown.Category.GUEST_GOWN, color_name="Green",
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
            name="X", category=Gown.Category.GUEST_GOWN, color_name="X", color_code="X",
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
            "action": "delete", "ids": [deletable.id, blocked.id], "reason": "Retired",
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

    def test_missed_pickup_appears_as_late_pickup_notification_not_a_customer_message(self):
        from reservations.models import Reservation, ReservationItem
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Late Pickup Notif Customer",
            status=Reservation.Status.CONFIRMED,
        )
        ReservationItem.objects.create(
            reservation=reservation, gown_name="Late Pickup Notif Gown",
            rental_date=date.today() - timedelta(days=1),
            return_date=date.today() + timedelta(days=2),
        )
        messages_before = CustomerMessage.objects.count()
        notifications = self._notifications_as(self.staff)
        late_pickups = [n for n in notifications if n["kind"] == "late_pickup"]
        self.assertEqual(len(late_pickups), 1)
        self.assertEqual(late_pickups[0]["level"], "warning")
        self.assertIn(reverse("arabela_admin:active_reservations"), late_pickups[0]["url"])
        self.assertIn(reservation.reference_code, late_pickups[0]["url"])
        # No automatic customer-facing reminder -- this is staff-only, on purpose.
        self.assertEqual(CustomerMessage.objects.count(), messages_before)

    def test_pending_reservations_missed_pickup_not_reported(self):
        from reservations.models import Reservation, ReservationItem
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Pending Missed Pickup Customer",
        )
        ReservationItem.objects.create(
            reservation=reservation, gown_name="Pending Missed Pickup Gown",
            rental_date=date.today() - timedelta(days=1),
            return_date=date.today() + timedelta(days=2),
        )
        kinds = [n["kind"] for n in self._notifications_as(self.staff)]
        self.assertNotIn("late_pickup", kinds)

    def test_already_picked_up_item_not_reported_as_late_pickup(self):
        from reservations.models import Reservation, ReservationItem
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Already Picked Up Customer",
            status=Reservation.Status.CONFIRMED,
        )
        ReservationItem.objects.create(
            reservation=reservation, gown_name="Already Picked Up Gown",
            stage=ReservationItem.Stage.RESERVED,
            rental_date=date.today() - timedelta(days=1),
            return_date=date.today() + timedelta(days=2),
        )
        kinds = [n["kind"] for n in self._notifications_as(self.staff)]
        self.assertNotIn("late_pickup", kinds)

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


class LiveNotificationFeedTests(TestCase):
    """The bell's live feed (api/notifications/): the same work-queue the page draws, as JSON plus
    ready-made HTML, so a customer's reservation appears in an open admin page without a refresh.
    The page and the feed come from ONE builder (arabela_admin.notifications), so they must agree."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="feed_staff", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.staff, role=UserProfile.Role.STAFF)
        cls.owner = User.objects.create_user(username="feed_owner", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.owner, role=UserProfile.Role.OWNER)
        cls.customer = User.objects.create_user(username="feed_customer", password="x")

    def setUp(self):
        self.url = reverse("arabela_admin:admin_notifications_feed")
        self.client.force_login(self.staff)

    def _feed(self, user=None, **params):
        if user is not None:
            self.client.force_login(user)
        return self.client.get(self.url, params)

    def _reserve(self, name="Feed Customer", proof=""):
        return Reservation.objects.create(customer=self.customer, customer_name=name, payment_proof_url=proof)

    def _overdue(self, name="Overdue Feed Customer"):
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name=name, status=Reservation.Status.ACTIVE,
        )
        return ReservationItem.objects.create(
            reservation=reservation, gown_name="Overdue Feed Gown", stage=ReservationItem.Stage.RESERVED,
            rental_date=date.today() - timedelta(days=10), return_date=date.today() - timedelta(days=3),
        )

    # ---- who may ask --------------------------------------------------------------------------
    def test_a_signed_out_visitor_gets_a_json_401_not_a_login_page(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["Content-Type"], "application/json")

    def test_a_customer_account_is_refused(self):
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_the_feed_only_answers_get(self):
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_the_answer_is_never_cached(self):
        self.assertIn("no-store", self._feed()["Cache-Control"])

    # ---- same data as the page ------------------------------------------------------------------
    def test_the_feed_lists_exactly_what_the_page_bell_lists(self):
        self._reserve("Page Match One", proof="https://example.test/p.jpg")
        self._overdue()
        _make_gown(status=Gown.Status.OUT_OF_STOCK)
        page = self.client.get(reverse("arabela_admin:dashboard"))
        data = self._feed().json()
        self.assertEqual([i["key"] for i in data["items"]], page.context["admin_notification_keys"])
        self.assertEqual(data["count"], page.context["admin_notification_count"])
        self.assertEqual(data["urgent"], page.context["admin_notification_urgent"])
        self.assertEqual(data["version"], page.context["admin_notification_version"])

    def test_every_notification_has_a_unique_key_that_stays_the_same(self):
        reservation = self._reserve("Key Customer", proof="https://example.test/p.jpg")
        self._overdue()
        first = [i["key"] for i in self._feed().json()["items"]]
        second = [i["key"] for i in self._feed().json()["items"]]
        self.assertEqual(first, second)
        self.assertEqual(len(first), len(set(first)))
        self.assertIn(f"reservation:{reservation.reference_code}", first)
        self.assertIn(f"payment:{reservation.reference_code}", first)
        self.assertTrue(any(k.startswith("overdue:") for k in first))

    def test_only_things_other_people_cause_alert(self):
        self._reserve("Alert Customer", proof="https://example.test/p.jpg")
        self._overdue()
        _make_gown(status=Gown.Status.OUT_OF_STOCK)
        inactive = User.objects.create_user(username="feed_inactive", password="x", is_staff=True, is_active=False)
        UserProfile.objects.create(user=inactive, role=UserProfile.Role.STAFF)
        UserProfile.objects.create(user=User.objects.create_user(username="feed_flagged", password="x"), is_flagged=True)
        alerts = {i["kind"]: i["alert"] for i in self._feed(self.owner).json()["items"]}
        for kind in ("reservation", "payment", "overdue", "customer"):
            self.assertTrue(alerts[kind], kind)
        for kind in ("inventory", "staff"):  # staff-caused: shown in the bell, never announced
            self.assertFalse(alerts[kind], kind)

    def test_staff_do_not_get_the_owner_only_roster_items(self):
        inactive = User.objects.create_user(username="feed_inactive2", password="x", is_staff=True, is_active=False)
        UserProfile.objects.create(user=inactive, role=UserProfile.Role.STAFF)
        self.assertIn("staff", [i["kind"] for i in self._feed(self.owner).json()["items"]])
        self.assertNotIn("staff", [i["kind"] for i in self._feed(self.staff).json()["items"]])

    # ---- live behaviour --------------------------------------------------------------------------
    def test_an_unchanged_bell_gets_a_tiny_answer(self):
        self._reserve()
        version = self._feed().json()["version"]
        again = self._feed(v=version).json()
        self.assertEqual(again, {"version": version, "unchanged": True})
        stale = self._feed(v="not-the-current-version").json()
        self.assertNotIn("unchanged", stale)
        self.assertIn("html", stale)

    def test_a_new_reservation_shows_up_in_the_next_check(self):
        version = self._feed().json()["version"]
        reservation = self._reserve("Brand New Customer")
        data = self._feed(v=version).json()
        self.assertNotEqual(data["version"], version)
        self.assertIn(f"reservation:{reservation.reference_code}", [i["key"] for i in data["items"]])
        self.assertIn("Brand New Customer", data["items"][0]["title"])
        self.assertEqual(data["count"], 1)

    def test_a_handled_notification_disappears(self):
        reservation = self._reserve()
        key = f"reservation:{reservation.reference_code}"
        self.assertIn(key, [i["key"] for i in self._feed().json()["items"]])
        reservation.status = Reservation.Status.CONFIRMED
        reservation.save(update_fields=["status"])
        data = self._feed().json()
        self.assertNotIn(key, [i["key"] for i in data["items"]])
        self.assertEqual(data["count"], 0)
        self.assertIn("All caught up", data["html"]["list"])

    def test_the_ages_tick_over_by_themselves(self):
        from unittest.mock import patch
        from django.utils import timezone as dj_timezone
        self._reserve()
        version = self._feed().json()["version"]
        with patch.object(dj_timezone, "now", return_value=dj_timezone.now() + timedelta(minutes=7)):
            data = self._feed(v=version).json()
        self.assertNotEqual(data["version"], version)  # "just now" became "7 min ago"
        self.assertIn("7 min ago", data["html"]["list"])

    # ---- the HTML the feed hands over -------------------------------------------------------------
    def test_the_feed_html_is_drawn_from_the_same_templates_as_the_page(self):
        reservation = self._reserve("Same Markup Customer")
        page = self.client.get(reverse("arabela_admin:dashboard")).content.decode()
        html = self._feed().json()["html"]
        row = f'<li data-key="reservation:{reservation.reference_code}"'
        self.assertIn(row, page)
        self.assertIn(row, html["list"])
        self.assertIn(row, html["modal"])
        self.assertIn("Same Markup Customer", html["modal"])
        self.assertIn(">1<", html["badge"])
        self.assertIn(">1<", html["chip"])
        self.assertIn("View all notifications", html["footer"])

    def test_only_six_rows_show_in_the_dropdown_but_all_are_there_to_be_pinned(self):
        for n in range(8):
            self._reserve(f"Many Customer {n}")
        data = self._feed().json()
        self.assertEqual(data["count"], 8)
        rows = re.findall(r'<li data-key="[^"]*"[^>]*>', data["html"]["list"])
        self.assertEqual(len(rows), 8)
        self.assertEqual(sum(1 for r in rows if " hidden" in r), 2)
        modal_rows = re.findall(r'<li data-key="[^"]*"[^>]*>', data["html"]["modal"])
        self.assertEqual(len(modal_rows), 8)
        self.assertEqual(sum(1 for r in modal_rows if " hidden" in r), 0)
        self.assertIn("View all 8 notifications", data["html"]["footer"])

    def test_the_view_all_window_offers_a_tab_per_kind_with_counts(self):
        self._reserve("Tab Customer", proof="https://example.test/p.jpg")
        self._overdue()
        modal = self._feed().json()["html"]["modal"]
        for label in ("Reservations", "Payments", "Returns &amp; pick-ups"):
            self.assertIn(label, modal)
        self.assertIn('data-notif-filter="all"', modal)
        self.assertIn('data-notif-filter="returns"', modal)
        self.assertNotIn('data-notif-filter="inventory"', modal)  # nothing of that kind -> no tab

    def test_one_kind_of_notification_needs_no_tabs(self):
        self._reserve("Single Kind Customer")
        self.assertNotIn("data-notif-filter", self._feed().json()["html"]["modal"])

    def test_nothing_to_do_shows_the_all_caught_up_state(self):
        html = self._feed().json()["html"]
        self.assertEqual(html["badge"].strip(), "")
        self.assertEqual(html["chip"].strip(), "")
        self.assertEqual(html["footer"].strip(), "")
        self.assertIn("All caught up", html["list"])
        self.assertIn("Nothing is waiting on you right now", html["modal"])

    def test_a_customer_name_can_never_become_markup(self):
        self._reserve("<img src=x onerror=alert(1)>")
        data = self._feed().json()
        for region in ("list", "modal"):
            self.assertNotIn("<img src=x", data["html"][region])
            self.assertIn("&lt;img src=x onerror=alert(1)&gt;", data["html"][region])
        self.assertIn("<img src=x onerror=alert(1)>", data["items"][0]["title"])  # plain text: the script uses textContent

    def test_the_badge_stops_at_99_plus_and_goes_red_when_urgent(self):
        from django.template.loader import render_to_string
        big = render_to_string("arabela_admin/partials/notif_badge.html", {"admin_notification_count": 150, "admin_notification_urgent": 0})
        self.assertIn("99+", big)
        self.assertIn("bg-brand-500", big)
        urgent = render_to_string("arabela_admin/partials/notif_badge.html", {"admin_notification_count": 3, "admin_notification_urgent": 1})
        self.assertIn("bg-error-500", urgent)
        self.assertIn("animate-ping", urgent)

    def test_the_shared_feed_costs_a_small_fixed_number_of_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        for n in range(5):
            self._reserve(f"Query Customer {n}", proof="https://example.test/p.jpg")
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self._feed().status_code, 200)
        self.assertLessEqual(len(queries), 12, [q["sql"][:80] for q in queries])

    # ---- the page ---------------------------------------------------------------------------------
    def test_every_admin_page_carries_what_the_bell_script_needs(self):
        reservation = self._reserve("Page Carries Customer")
        for name in ("dashboard", "gown_catalog", "pending_approval", "payment_verification", "active_reservations", "clients"):
            html = self.client.get(reverse(f"arabela_admin:{name}")).content.decode()
            self.assertIn('id="arabela-notifier"', html, name)
            self.assertIn(f'data-notif-feed="{self.url}"', html, name)
            self.assertIn("data-notif-version=", html, name)
            self.assertIn("notifications-live.js", html, name)
            self.assertIn('id="arabela-notif-keys"', html, name)
            for region in ("badge", "chip", "list", "footer", "modal"):
                self.assertIn(f'data-notif-region="{region}"', html, f"{name}: {region}")
            self.assertIn(f"reservation:{reservation.reference_code}", html, name)

    def test_the_page_and_the_feed_agree_on_the_version(self):
        self._reserve("Version Customer")
        page = self.client.get(reverse("arabela_admin:dashboard"))
        version = page.context["admin_notification_version"]
        self.assertIn(f'data-notif-version="{version}"', page.content.decode())
        self.assertEqual(self._feed(v=version).json(), {"version": version, "unchanged": True})

    def test_customers_and_visitors_get_a_blank_bell(self):
        from arabela_admin import notifications
        blank = notifications.empty_context()
        self.assertEqual(blank["admin_notifications"], [])
        self.assertEqual(blank["admin_notification_count"], 0)
        self.assertEqual(blank["admin_notification_keys"], [])

    def test_the_chime_and_the_script_are_real_files(self):
        import wave
        from django.contrib.staticfiles import finders
        sound = finders.find("arabela_admin/sounds/new-notification.wav")
        self.assertTrue(sound)
        with wave.open(sound, "rb") as w:
            seconds = w.getnframes() / w.getframerate()
        self.assertTrue(0.5 <= seconds <= 3.0, seconds)  # a short chime, not a song
        script = finders.find("arabela_admin/notifications-live.js")
        self.assertTrue(script)
        self.assertIn("api/notifications", self.url)


class ClientListReservationNameTests(TestCase):
    """Client List: under each account's own name, "Booked as <name>" -- the name that customer typed on
    their reservations, the one on their receipt -- so staff can tell whose account a receipt name belongs
    to without opening anyone's history. Derived from the reservations the page already loads (no new
    column, no extra queries, no extra table width); no reservations -> nothing extra, like before."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="cn_staff", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.staff, role=UserProfile.Role.STAFF)

    def setUp(self):
        self.client.force_login(self.staff)

    def _customer(self, username, first="", last="", email=None):
        return User.objects.create_user(
            username=username, password="x", first_name=first, last_name=last,
            email=email or f"{username}@example.test",
        )

    def _book(self, customer, name, status=Reservation.Status.PENDING):
        return Reservation.objects.create(customer=customer, customer_name=name, status=status)

    def _page(self):
        response = self.client.get(reverse("arabela_admin:clients"))
        self.assertEqual(response.status_code, 200)
        return response, {c.username: c for c in response.context["customers"]}

    @staticmethod
    def _table(response):
        """Just the customer table's rows -- a customer's name also shows up in the notification bell."""
        return response.content.decode().split("<tbody", 1)[1].split("</tbody>", 1)[0]

    # ---- which name is shown --------------------------------------------------------------------
    def test_a_customer_with_no_reservations_shows_nothing_extra(self):
        self._customer("cn_none", "Nora", "None")
        response, rows = self._page()
        row = rows["cn_none"]
        self.assertEqual((row.reservation_name, row.reservation_names, row.reservation_name_extra), ("", [], 0))
        self.assertFalse(row.reservation_name_differs)
        table = self._table(response)
        self.assertNotIn("Booked as", table)
        self.assertNotIn("data-reservation-name", table)

    def test_the_name_typed_on_a_reservation_is_shown_and_highlighted_when_it_differs(self):
        customer = self._customer("cn_differs", "France", "Concepcion")
        self._book(customer, "Rainer Pepito")
        response, rows = self._page()
        row = rows["cn_differs"]
        self.assertEqual(row.display_name, "France Concepcion")
        self.assertEqual(row.reservation_name, "Rainer Pepito")
        self.assertTrue(row.reservation_name_differs)
        table = self._table(response)
        self.assertIn("Booked as", table)
        self.assertIn('data-reservation-name data-differs="1"', table)  # the highlighted one
        self.assertIn('>Rainer Pepito</span>', table)

    def test_a_name_that_matches_the_account_is_still_shown_but_not_highlighted(self):
        customer = self._customer("cn_same", "Sam", "Same")
        self._book(customer, "Sam Same")
        response, rows = self._page()
        row = rows["cn_same"]
        self.assertEqual(row.reservation_name, "Sam Same")
        self.assertFalse(row.reservation_name_differs)
        table = self._table(response)
        self.assertEqual(table.count('>Sam Same</span>'), 2)  # the account name AND the booked-as name
        self.assertIn('data-reservation-name data-differs="0"', table)  # shown, but plain -- nothing to flag

    def test_capitals_and_extra_spaces_do_not_make_a_different_name(self):
        customer = self._customer("cn_case", "Gina", "Gmail")
        self._book(customer, "  gina    GMAIL ")
        _, rows = self._page()
        row = rows["cn_case"]
        self.assertFalse(row.reservation_name_differs)
        self.assertEqual(row.reservation_name, "gina GMAIL")  # spacing tidied, the typed capitals kept
        self.assertEqual(len(row.reservation_names), 1)

    def test_a_cancelled_attempt_does_not_hide_the_name_on_the_booking_that_went_through(self):
        customer = self._customer("cn_cancel", "Cara", "Cancelled")
        self._book(customer, "Real Name", status=Reservation.Status.CONFIRMED)
        self._book(customer, "Typo Nmae", status=Reservation.Status.CANCELLED)  # newer
        _, rows = self._page()
        row = rows["cn_cancel"]
        self.assertEqual(row.reservation_name, "Real Name")
        self.assertEqual([n["name"] for n in row.reservation_names], ["Real Name", "Typo Nmae"])

    def test_when_every_reservation_was_cancelled_the_latest_name_is_still_shown(self):
        customer = self._customer("cn_allcancel", "Al", "Cancelled")
        self._book(customer, "Older Name", status=Reservation.Status.CANCELLED)
        self._book(customer, "Newer Name", status=Reservation.Status.REJECTED)
        _, rows = self._page()
        self.assertEqual(rows["cn_allcancel"].reservation_name, "Newer Name")

    def test_several_names_show_the_newest_and_how_many_more(self):
        customer = self._customer("cn_many", "Many", "Names")
        for name in ("First Name", "Second Name", "Third Name"):
            self._book(customer, name)
        response, rows = self._page()
        row = rows["cn_many"]
        self.assertEqual([n["name"] for n in row.reservation_names], ["Third Name", "Second Name", "First Name"])
        self.assertEqual(row.reservation_name_extra, 2)
        html = response.content.decode()
        self.assertIn("+2 more", html)
        self.assertIn('title="Also used: Second Name, First Name"', html)

    def test_one_name_used_twice_is_counted_once_with_its_count(self):
        customer = self._customer("cn_twice", "Pia", "Twice")
        self._book(customer, "pia  pending")
        self._book(customer, "Pia Pending")
        _, rows = self._page()
        names = rows["cn_twice"].reservation_names
        self.assertEqual(len(names), 1)
        self.assertEqual((names[0]["name"], names[0]["count"]), ("Pia Pending", 2))  # the newest spelling

    def test_two_accounts_can_share_a_reservation_name(self):
        for username in ("cn_share_a", "cn_share_b"):
            self._book(self._customer(username, "Denmark", "Concepcion"), "Rainer Pepito")
        response, rows = self._page()
        self.assertEqual(rows["cn_share_a"].reservation_name, rows["cn_share_b"].reservation_name)
        self.assertEqual(self._table(response).count("data-reservation-name"), 2)

    # ---- searching -------------------------------------------------------------------------------
    def test_search_matches_every_name_they_used_and_says_so(self):
        customer = self._customer("cn_search", "Sara", "Search")
        self._book(customer, "Alpha One")
        self._book(customer, "Beta Two")
        response, rows = self._page()
        self.assertEqual(rows["cn_search"].reservation_names_search, "beta two alpha one")
        html = response.content.decode()
        self.assertIn("beta two alpha one", html)  # inside the row's search text
        self.assertIn("Search by account name, reservation name or email", html)

    # ---- safety ------------------------------------------------------------------------------------
    def test_a_name_with_quotes_and_markup_can_never_break_the_page_or_run(self):
        customer = self._customer("cn_evil", "Eve", "Evil")
        evil = "O'Brien \"Ace\" </script><b>x</b>"
        self._book(customer, evil)
        response, rows = self._page()
        html = response.content.decode()
        self.assertNotIn("</script><b>x</b>", html)
        self.assertIn("&lt;/script&gt;&lt;b&gt;x&lt;/b&gt;", html)         # shown as text
        self.assertNotIn("'O'Brien", html)                                  # never an unescaped quote in a script string
        self.assertEqual(json.loads(rows["cn_evil"].reservation_names_json)[0]["name"], evil)  # the window's data round-trips

    # ---- the View window and the rest of the page ----------------------------------------------------
    def test_the_view_window_lists_the_names_and_keeps_its_history_link(self):
        customer = self._customer("cn_window", "Win", "Dow")
        self._book(customer, "Window Name")
        response, _ = self._page()
        html = response.content.decode()
        self.assertIn("Name on reservations", html)
        self.assertIn("names: JSON.parse(", html)
        self.assertIn("No reservations yet.", html)
        self.assertIn(reverse("arabela_admin:reservation_records") + "?customer=' + viewingCustomer.userId", html)

    def test_the_table_keeps_exactly_the_columns_it_had(self):
        # An extra column pushed the View button off-screen on 1366-1500px laptops, so the name sits
        # under the account name instead. Same 8 columns, same empty-state width.
        html = self.client.get(reverse("arabela_admin:clients")).content.decode()
        self.assertEqual(html.count("<th "), 8)
        self.assertIn(">Customer</p>", html)
        self.assertNotIn(">Reservation Name</p>", html)
        self.assertIn('colspan="8"', html)

    def test_the_names_cost_no_extra_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        first = self._customer("cn_q1", "Q", "One")
        self._book(first, "Q Name")
        self._page()  # warm-up: the first request of a run does one-off work (settings row, caches)
        with CaptureQueriesContext(connection) as small:
            self._page()
        for n in range(2, 7):
            other = self._customer(f"cn_q{n}", "Q", str(n))
            for k in range(3):
                self._book(other, f"Q Name {n} {k}")
        with CaptureQueriesContext(connection) as big:
            self._page()
        self.assertEqual(len(small), len(big), (len(small), len(big)))

    # ---- the name picker on its own -------------------------------------------------------------------
    def test_the_picker_ignores_blank_names_and_handles_no_reservations(self):
        from types import SimpleNamespace
        now = timezone.now()
        blank = SimpleNamespace(customer_name="   ", status=Reservation.Status.PENDING, created_at=now)
        self.assertEqual(views_module._reservation_names([]), [])
        self.assertEqual(views_module._reservation_names([blank]), [])
        self.assertEqual(views_module._name_key("  Mañana   SOL "), "mañana sol")


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
            "security_deposits", "active_reservations",
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
            "security_deposits", "active_reservations",
            "pending_approval", "clients",
        ):
            with self.subTest(page=url_name):
                self._assert_page_ok(url_name)

    def test_anonymous_redirected_from_every_list_page(self):
        self.client.logout()
        for url_name in (
            "rental_schedule", "payment_verification", "rental_history",
            "security_deposits", "active_reservations",
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

    def test_returning_the_last_gown_records_a_deposit_settled_event(self):
        self.client.post(
            reverse("arabela_admin:reservation_item_mark_returned", args=[self.item.id]),
            data=json.dumps({"condition": "Good"}), content_type="application/json",
        )
        self.assertIn("Security deposit settled", self._labels())

    def _confirm(self):
        self.reservation.status = Reservation.Status.CONFIRMED
        self.reservation.save(update_fields=["status"])

    def test_marking_picked_up_records_a_pick_up_event(self):
        self._confirm()
        response = self.client.post(reverse("arabela_admin:reservation_item_mark_picked_up", args=[self.item.id]))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("Hook Gown picked up", self._labels())

    def test_changing_the_pickup_records_a_pick_up_date_changed_event(self):
        self._confirm()
        ReservationItem.objects.filter(id=self.item.id).update(rental_date=self.today + timedelta(days=3))
        response = self.client.post(
            reverse("arabela_admin:reservation_item_change_pickup", args=[self.item.id]),
            data=json.dumps({"date": str(self.today + timedelta(days=1))}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        event = self.reservation.status_events.get(label="Hook Gown pick-up date changed")
        self.assertIn("2 days earlier than originally booked", event.detail)

    def test_a_refused_request_records_nothing(self):
        """Validation failures must leave no trace -- a timeline of things that did not
        happen is worse than no timeline."""
        self._confirm()
        ReservationItem.objects.filter(id=self.item.id).update(rental_date=self.today + timedelta(days=3))
        response = self.client.post(reverse("arabela_admin:reservation_item_mark_picked_up", args=[self.item.id]))
        self.assertEqual(response.status_code, 400)  # pick-up is in 3 days
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


class DashboardGownInventoryTileTests(TestCase):
    """The dashboard's Gown Inventory tile shows the same four figures, in the same words, as the stat row on
    Gown Catalog (Total / Available / Reserved / Blocked) -- no separate "Needs Attention" count."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="inv_tile_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)
        self.today = timezone.localdate()
        self.dashboard = reverse("arabela_admin:dashboard")

    def block(self, gown, start_offset, end_offset):
        return GownUnavailability.objects.create(
            gown=gown, start_date=self.today + timedelta(days=start_offset),
            end_date=self.today + timedelta(days=end_offset), reason=GownUnavailability.Reason.CLEANING)

    def seed(self):
        _make_gown(1, status=Gown.Status.AVAILABLE)
        _make_gown(2, status=Gown.Status.AVAILABLE)
        _make_gown(3, status=Gown.Status.RESERVED)
        _make_gown(4, status=Gown.Status.OUT_OF_STOCK)
        blocked = _make_gown(5, status=Gown.Status.AVAILABLE)
        self.block(blocked, -1, 1)                                  # covers today
        self.block(blocked, 2, 3)                                   # same gown again, later: still ONE blocked gown
        self.block(_make_gown(6, status=Gown.Status.AVAILABLE), 5, 8)    # future only: not blocked today
        self.block(_make_gown(7, status=Gown.Status.AVAILABLE), -9, -2)  # expired: not blocked today

    def test_the_four_figures_are_the_catalogs_own_numbers(self):
        self.seed()
        response = self.client.get(self.dashboard)
        catalog = self.client.get(reverse("arabela_admin:gown_catalog")).context
        self.assertEqual(response.context["gown_total"], catalog["total_gowns_count"])
        self.assertEqual(response.context["gown_available"], catalog["available_count"])
        self.assertEqual(response.context["gown_reserved"], catalog["reserved_count"])
        self.assertEqual(response.context["gown_blocked"], len(catalog["blocked_gowns_today"]))
        self.assertEqual(
            (response.context["gown_total"], response.context["gown_available"],
             response.context["gown_reserved"], response.context["gown_blocked"]), (7, 5, 1, 1))

    def test_the_tile_uses_the_catalogs_wording_and_drops_needs_attention(self):
        self.seed()
        html = self.client.get(self.dashboard).content.decode()
        tile = html[html.index("Gown Inventory"):]
        tile = tile[:tile.index("</a>")]
        for word in ("Total Gowns", "Available", "Reserved", "Blocked Gowns"):
            with self.subTest(word=word):
                self.assertIn(word, tile)
        self.assertNotIn("Needs Attention", tile)
        self.assertNotIn("None blocked today", tile)                # something IS blocked

    def test_with_nothing_blocked_the_tile_says_so_like_the_catalog(self):
        _make_gown(1)
        html = self.client.get(self.dashboard).content.decode()
        self.assertEqual(self.client.get(self.dashboard).context["gown_blocked"], 0)
        self.assertIn("None blocked today", html)


class QuickVerifyProofPopupTests(TestCase):
    """The dashboard's Quick Verify "View Proof" popup is the same viewer as Payment Verification's
    "View Payment": a full-size, uncropped receipt with a View Full Image link, the date it was submitted,
    the GCash reference number and the duplicate warning."""

    PROOF = "https://res.cloudinary.com/demo/image/upload/payment_proofs/receipt.jpg"

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="qv_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="qv_customer", password="x")
        cls.first = Reservation.objects.create(
            customer=cls.customer, customer_name="Quick Verify Customer", status=Reservation.Status.PENDING,
            payment_method="GCash", payment_proof_url=cls.PROOF, gcash_reference="1234567890123")
        cls.second = Reservation.objects.create(
            customer=cls.customer, customer_name="Quick Verify Customer", status=Reservation.Status.PENDING,
            payment_method="GCash", payment_proof_url=cls.PROOF, gcash_reference="1234567890123")

    def setUp(self):
        self.client.force_login(self.staff)
        self.html = self.client.get(reverse("arabela_admin:dashboard")).content.decode()

    def test_the_popup_is_the_payment_verification_viewer(self):
        for piece in ("GCash Payment Proof", "View Full Image", "admin-photo-frame", "max-height: 75vh",
                      "Date Submitted", "GCash Reference No.", "Check the receipt before approving"):
            with self.subTest(piece=piece):
                self.assertIn(piece, self.html)
        self.assertNotIn("GCash Receipt Preview", self.html)
        self.assertNotIn("max-height: 212px", self.html)          # the old thumbnail-sized image

    def test_the_photo_frame_styles_are_on_the_page(self):
        self.assertIn(".admin-photo-frame {", self.html)

    def test_each_row_hands_the_popup_its_real_details(self):
        self.assertIn("reservation: '%s'" % escapejs(self.first.reference_code), self.html)
        self.assertIn("gcashRef: '1234567890123'", self.html)
        self.assertIn("proofUrl: '%s'" % escapejs(self.PROOF), self.html)

    def test_a_reused_gcash_number_names_the_other_booking_on_each_row(self):
        self.assertIn("gcashDup: '%s'" % escapejs(self.second.reference_code), self.html)
        self.assertIn("gcashDup: '%s'" % escapejs(self.first.reference_code), self.html)

    def test_no_template_syntax_leaks_into_the_page(self):
        for leak in ("{%", "{{", "{#"):
            self.assertNotIn(leak, self.html)


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


class ReceiptUploadAndReplaceTests(TestCase):
    """receipt_upload_view / receipt_replace_view -- staff attach a photo of a
    manually-issued receipt (the shop's own paper receipt) to a real reservation.
    Deliberately the opposite of Reservation.payment_proof_url (the customer's own
    GCash screenshot, captured automatically at checkout): this is produced by the
    shop, attached by staff, afterward. Both endpoints are page-agnostic -- Reservation
    Records is the only caller now that the standalone Receipt Records page (which
    used to own this coverage) is gone, but these tests hit the endpoints directly so
    they never depend on which page is calling them. _save_receipt_photo is mocked in
    every test -- this environment's default storage is real Cloudinary, and these
    tests must never upload anything to that live external account."""

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
        self.assertEqual(data["uploadedBy"], "Ana Cruz")

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
        self.assertTrue(row["depositLink"].startswith(reverse("arabela_admin:security_deposits")))
        self.assertTrue(row["depositLink"].endswith("?search=" + self.two_gowns.reference_code))
        self.assertEqual(row["depositLinkLabel"], "Open in Security Deposits")
        cancelled = Reservation.objects.create(
            customer=self.customer, customer_name="Maria Records", status=Reservation.Status.CANCELLED)
        row = self._records()[cancelled.reference_code]
        self.assertEqual(row["depositStatus"], "Not held")
        self.assertEqual(row["depositLink"], "")
        self.assertEqual(row["depositLinkLabel"], "")

    def test_awaiting_verification_links_to_payment_verification_not_security_deposits(self):
        """Security Deposits only lists Confirmed-and-later bookings, so a Pending one
        awaiting its payment check isn't on that page at all yet -- send staff to where
        this booking's payment actually gets reviewed instead."""
        pending = Reservation.objects.create(
            customer=self.customer, customer_name="Maria Records",
            status=Reservation.Status.PENDING, payment_proof_url="https://example.test/proof.jpg",
        )
        row = self._records()[pending.reference_code]
        self.assertEqual(row["depositStatus"], "Awaiting verification")
        self.assertTrue(row["depositLink"].startswith(reverse("arabela_admin:payment_verification")))
        self.assertTrue(row["depositLink"].endswith("?search=" + pending.reference_code))
        self.assertEqual(row["depositLinkLabel"], "Open in Payment Verification")

        # Pending with no proof uploaded yet -- "Not paid", no link to anywhere.
        no_proof = Reservation.objects.create(
            customer=self.customer, customer_name="Maria Records", status=Reservation.Status.PENDING,
        )
        row = self._records()[no_proof.reference_code]
        self.assertEqual(row["depositStatus"], "Not paid")
        self.assertEqual(row["depositLink"], "")
        self.assertEqual(row["depositLinkLabel"], "")

    def test_receipts_show_who_uploaded_them(self):
        self.client.post(reverse("arabela_admin:receipt_upload"), data={
            "reference_code": self.two_gowns.reference_code, "photo": _make_jpeg(),
        })
        receipts = self._records()[self.two_gowns.reference_code]["receipts"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["uploadedBy"], "Ana Cruz")

    def test_photo_viewer_can_replace_an_uploaded_receipt(self):
        """The photo viewer (not just the Upload Receipt modal) can fix a wrong upload
        in place -- the gap this page used to have next to the old, now-removed
        Receipt Records page, folded in here instead of keeping a second page for it."""
        html = self.client.get(reverse("arabela_admin:reservation_records")).content.decode()
        self.assertIn("replacePhoto(", html)
        self.assertIn("rrReplaceFileInput", html)
        self.assertIn('x-if="photo.id"', html)

    def test_pending_upload_preview_can_be_viewed_full_size(self):
        html = self.client.get(reverse("arabela_admin:reservation_records")).content.decode()
        self.assertIn(':href="uploadPreviewUrl"', html)
        self.assertIn("View full size", html)

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


class MonthlyReservationCountTests(TestCase):
    """The dashboard's Reservations This Month tile and Reservation Records' monthly
    breakdown / ?month= filter (which replaced the old Monthly Rentals bar chart) must
    count the same bookings: one per reservation, in the month its FIRST gown is picked
    up, leaving out cancelled/rejected ones. If they ever disagree, clicking the tile
    lands on a different number of rows than it showed."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="monthly_count_owner", password="x", is_staff=True)
        UserProfile.objects.update_or_create(user=cls.owner, defaults={"role": UserProfile.Role.OWNER})
        cls.customer = User.objects.create_user(
            username="monthly_count_customer", password="x", email="monthly.count@gmail.com")

    def setUp(self):
        self.client.force_login(self.owner)
        # The dashboard runs the daily reminder sweep on load -- unrelated to these counts.
        patcher = patch("arabela_admin.views.reservation_reminders.run_daily_sweep_if_due")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.month_start = timezone.localdate().replace(day=1)
        self.last_month_day = self.month_start - timedelta(days=1)
        self.next_month_day = (self.month_start + timedelta(days=32)).replace(day=1)

    def _booking(self, status, *pickups):
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Monthly Count", status=status)
        for n, pickup in enumerate(pickups):
            ReservationItem.objects.create(
                reservation=reservation, gown_name=f"Count Gown {n}",
                rental_date=pickup, return_date=pickup + timedelta(days=4))
        return reservation

    def _add_every_kind_of_booking(self):
        this_month = self.month_start
        self._booking(Reservation.Status.CONFIRMED, this_month, this_month)  # two gowns: one booking
        self._booking(Reservation.Status.PENDING, this_month)  # awaiting approval still counts
        self._booking(Reservation.Status.CANCELLED, this_month)  # never became a rental
        self._booking(Reservation.Status.REJECTED, this_month)  # never became a rental
        self._booking(Reservation.Status.CONFIRMED, self.last_month_day, this_month)  # first pick-up last month
        self._booking(Reservation.Status.CONFIRMED, self.next_month_day)  # next month

    def _dashboard_count(self):
        return self.client.get(reverse("arabela_admin:dashboard")).context["reservations_this_month"]

    def _records(self):
        html = self.client.get(reverse("arabela_admin:reservation_records")).content.decode()
        match = re.search(
            r'<script id="reservation-records-data" type="application/json">(.*?)</script>', html, re.S)
        return {r["reference"]: r for r in json.loads(match.group(1))}

    def test_counts_bookings_once_by_first_pickup_and_skips_cancelled_and_rejected(self):
        self._add_every_kind_of_booking()
        self.assertEqual(self._dashboard_count(), 2)

    def test_tile_number_equals_the_rows_reservation_records_counts_for_that_month(self):
        self._add_every_kind_of_booking()
        key = self.month_start.strftime("%Y-%m")
        rows = [r for r in self._records().values() if r["pickupMonth"] == key and r["countsAsRental"]]
        self.assertEqual(len(rows), self._dashboard_count())

    def test_each_record_carries_its_pickup_month_and_whether_it_counts(self):
        spans_two_months = self._booking(Reservation.Status.CONFIRMED, self.last_month_day, self.month_start)
        cancelled = self._booking(Reservation.Status.CANCELLED, self.month_start)
        records = self._records()
        self.assertEqual(
            records[spans_two_months.reference_code]["pickupMonth"], self.last_month_day.strftime("%Y-%m"))
        self.assertTrue(records[spans_two_months.reference_code]["countsAsRental"])
        self.assertFalse(records[cancelled.reference_code]["countsAsRental"])

    def test_tile_links_to_this_months_records_and_the_bar_chart_is_gone(self):
        html = self.client.get(reverse("arabela_admin:dashboard")).content.decode()
        self.assertIn("Reservations This Month", html)
        self.assertIn(
            reverse("arabela_admin:reservation_records") + "?month=" + self.month_start.strftime("%Y-%m"), html)
        self.assertNotIn('id="chartOne"', html)


class FormsAndUiElementsRemovedTests(TestCase):
    """The Forms and UI Elements sidebar modules were leftover TailAdmin demo-template
    pages (Form Elements / Alerts / Badges / Buttons), not real business features.
    Covers that every real admin page still renders with no trace of that sidebar
    block left behind, and that the demo pages themselves are gone for good."""

    REAL_PAGES = [
        "dashboard",
        "calendar",
        "payment_verification",
        "rental_history",
        "security_deposits",
        "reservation_records",
        "active_reservations",
        "pending_approval",
        "gown_catalog",
        "clients",
        "staff_management",
    ]
    REMOVED_PAGE_SLUGS = ["form-elements", "alerts", "badge", "buttons"]

    @classmethod
    def setUpTestData(cls):
        # Owner so every gated page (e.g. Staff Management) is reachable in one pass --
        # RBAC itself is covered elsewhere and isn't what this test is checking.
        cls.owner = User.objects.create_user(username="forms_ui_removed_owner", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.owner, role=UserProfile.Role.OWNER)

    def setUp(self):
        self.client.force_login(self.owner)

    def _assert_sidebar_clean(self, html):
        self.assertNotIn("data-admin-template-nav", html)
        self.assertNotIn("UI Elements", html)
        for slug in self.REMOVED_PAGE_SLUGS:
            self.assertNotIn(f"/admin-panel/{slug}.html", html)

    def test_real_pages_render_with_no_leftover_sidebar_markup(self):
        for name in self.REAL_PAGES:
            with self.subTest(page=name):
                response = self.client.get(reverse(f"arabela_admin:{name}"))
                self.assertEqual(response.status_code, 200)
                self._assert_sidebar_clean(response.content.decode())

    def test_profile_and_account_settings_render_with_no_leftover_sidebar_markup(self):
        for slug in ["profile", "account-settings"]:
            with self.subTest(page=slug):
                response = self.client.get(reverse("arabela_admin:page", args=[slug]))
                self.assertEqual(response.status_code, 200)
                self._assert_sidebar_clean(response.content.decode())

    def test_removed_demo_pages_404(self):
        for slug in self.REMOVED_PAGE_SLUGS:
            with self.subTest(page=slug):
                response = self.client.get(reverse("arabela_admin:page", args=[slug]))
                self.assertEqual(response.status_code, 404)


def _owner_and_staff(prefix):
    """(owner, plain staff) accounts -- the owner by role, the staff with a STAFF profile."""
    owner = User.objects.create_user(username=f"{prefix}_owner", password="x", is_staff=True)
    UserProfile.objects.create(user=owner, role=UserProfile.Role.OWNER)
    staff = User.objects.create_user(username=f"{prefix}_staff", password="x", is_staff=True)
    UserProfile.objects.create(user=staff, role=UserProfile.Role.STAFF)
    return owner, staff


class TagColorsTests(TestCase):
    """Tag Colors: one physical-tag color per gown category, changeable by the OWNER only.

    The colors are only ever a category property -- never stored per gown -- so changing
    one recolors every gown in that category at once, past and future."""

    @classmethod
    def setUpTestData(cls):
        cls.owner, cls.staff = _owner_and_staff("tagcolors")

    def _save(self, colors):
        return self.client.post(
            reverse("arabela_admin:gown_tag_colors_update"),
            data=json.dumps({"colors": colors}), content_type="application/json",
        )

    # ---- the data rules -------------------------------------------------------------
    def test_every_category_has_a_default_tag_color_from_the_palette(self):
        # Pinned to Gown.Category so a new category can't ship without a tag color.
        self.assertEqual(set(DEFAULT_CATEGORY_TAG_COLORS), set(Gown.Category.values))
        for color in DEFAULT_CATEGORY_TAG_COLORS.values():
            self.assertIn(color, TAG_COLOR_HEX)

    def test_wedding_gown_defaults_to_a_white_tag(self):
        self.assertEqual(resolve_tag_colors({})["Wedding Gown"], "White")

    def test_saved_colors_win_and_untouched_categories_keep_their_default(self):
        resolved = resolve_tag_colors({"Wedding Gown": "Red"})
        self.assertEqual(resolved["Wedding Gown"], "Red")
        self.assertEqual(resolved["Suit"], DEFAULT_CATEGORY_TAG_COLORS["Suit"])

    def test_a_saved_color_that_left_the_palette_falls_back_to_the_default(self):
        self.assertEqual(
            resolve_tag_colors({"Wedding Gown": "Chartreuse"})["Wedding Gown"],
            DEFAULT_CATEGORY_TAG_COLORS["Wedding Gown"],
        )

    def test_garbage_saved_settings_never_break_resolution(self):
        for junk in (None, "nope", ["Wedding Gown"], 5):
            with self.subTest(junk=junk):
                self.assertEqual(resolve_tag_colors(junk), dict(DEFAULT_CATEGORY_TAG_COLORS))

    # ---- who may change them ---------------------------------------------------------
    def test_owner_can_change_a_tag_color(self):
        self.client.force_login(self.owner)
        response = self._save({"Wedding Gown": "Blue"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(SiteSettings.load().tag_colors()["Wedding Gown"], "Blue")
        row = next(c for c in response.json()["colors"] if c["category"] == "Wedding Gown")
        self.assertEqual(row["color"], "Blue")
        self.assertEqual(row["hex"], TAG_COLOR_HEX["Blue"])

    def test_a_superuser_counts_as_the_owner(self):
        boss = User.objects.create_superuser(username="tagcolors_super", password="x")
        self.client.force_login(boss)
        self.assertEqual(self._save({"Suit": "Red"}).status_code, 200)

    def test_staff_cannot_change_tag_colors_even_by_sending_the_request_directly(self):
        """Hiding the controls protects nothing on its own -- the endpoint itself refuses."""
        self.client.force_login(self.staff)
        response = self._save({"Wedding Gown": "Blue"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(SiteSettings.load().category_tag_colors, {})
        self.assertEqual(SiteSettings.load().tag_colors()["Wedding Gown"], "White")

    def test_a_staff_account_with_no_profile_is_not_the_owner(self):
        bare = User.objects.create_user(username="tagcolors_bare", password="x", is_staff=True)
        self.client.force_login(bare)
        self.assertEqual(self._save({"Wedding Gown": "Blue"}).status_code, 403)

    def test_signed_out_request_gets_json_401_not_a_login_redirect(self):
        response = self._save({"Wedding Gown": "Blue"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "Unauthorized")

    # ---- validation: all-or-nothing --------------------------------------------------
    def test_an_unknown_category_rejects_the_whole_save(self):
        self.client.force_login(self.owner)
        response = self._save({"Wedding Gown": "Blue", "Not A Category": "Red"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SiteSettings.load().tag_colors()["Wedding Gown"], "White")

    def test_an_unknown_color_rejects_the_whole_save(self):
        self.client.force_login(self.owner)
        response = self._save({"Suit": "Red", "Wedding Gown": "Chartreuse"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SiteSettings.load().category_tag_colors, {})

    def test_empty_or_malformed_requests_are_rejected(self):
        self.client.force_login(self.owner)
        url = reverse("arabela_admin:gown_tag_colors_update")
        for body in (b"not json", b"[]", b'{"colors": {}}', b'{"colors": []}', b"{}"):
            with self.subTest(body=body):
                response = self.client.post(url, data=body, content_type="application/json")
                self.assertEqual(response.status_code, 400)

    def test_separate_saves_merge_instead_of_replacing_each_other(self):
        self.client.force_login(self.owner)
        self._save({"Wedding Gown": "Blue"})
        self._save({"Suit": "Red"})
        saved = SiteSettings.load().tag_colors()
        self.assertEqual((saved["Wedding Gown"], saved["Suit"]), ("Blue", "Red"))

    def test_two_categories_can_share_a_color_but_it_is_reported(self):
        self.client.force_login(self.owner)
        response = self._save({"Suit": "White"})  # Wedding Gown is White by default
        self.assertEqual(response.status_code, 200)
        shared = {s["color"]: s["categories"] for s in response.json()["shared"]}
        self.assertEqual(sorted(shared["White"]), ["Suit", "Wedding Gown"])

    # ---- what the catalog page shows ---------------------------------------------------
    def test_owner_gets_the_editing_controls(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertTrue(response.context["can_edit_tag_colors"])
        self.assertContains(response, "Save tag colors")
        self.assertNotContains(response, "Only the owner can change tag colors.")

    def test_staff_sees_the_list_but_no_editing_controls(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertFalse(response.context["can_edit_tag_colors"])
        self.assertNotContains(response, "Save tag colors")
        self.assertContains(response, "Only the owner can change tag colors.")
        # ...but the list itself is right there.
        self.assertContains(response, "Wedding Gown")
        self.assertEqual(
            {row["key"]: row["tag_color"] for row in response.context["category_rows"]}["Wedding Gown"],
            "White",
        )

    def test_a_gowns_row_shows_its_categorys_tag_color_and_the_number_to_write_on_it(self):
        self.client.force_login(self.staff)
        gown = _make_gown(1, category=Gown.Category.WEDDING_GOWN, gown_id="Wedding Gown-WH-012")
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        row = next(g for g in response.context["gowns"] if g.id == gown.id)
        self.assertEqual((row.tag_color, row.tag_number), ("White", "012"))
        self.assertContains(response, "White tag")

    def test_changing_a_category_color_recolors_every_gown_in_it(self):
        gowns = [_make_gown(n, category=Gown.Category.WEDDING_GOWN) for n in (1, 2, 3)]
        self.client.force_login(self.owner)
        self._save({"Wedding Gown": "Red"})
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        for gown in gowns:
            row = next(g for g in response.context["gowns"] if g.id == gown.id)
            self.assertEqual(row.tag_color, "Red")


class CatalogSearchAndOrderTests(TestCase):
    """The tag number has to be easy to find and read: searchable in every form staff
    might type it, and listed in the order the numbers actually run."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="catsearch_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def test_search_text_covers_id_name_colors_and_every_form_of_the_number(self):
        gown = _make_gown(
            1, category=Gown.Category.WEDDING_GOWN, gown_id="Wedding Gown-WH-012",
            name="White", color_name="White", color_code="WH",
        )
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        hay = next(g for g in response.context["gowns"] if g.id == gown.id).search_hay
        for needle in ("wedding gown-wh-012", "white", "012", "12", "#12", "medium"):
            with self.subTest(needle=needle):
                self.assertIn(needle, hay)

    def test_the_client_side_filter_data_uses_the_same_search_text_as_the_row(self):
        gown = _make_gown(1, category=Gown.Category.WEDDING_GOWN, gown_id="Wedding Gown-WH-012")
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        row = next(g for g in response.context["gowns"] if g.id == gown.id)
        mini = next(m for m in response.context["gowns_min"] if m["id"] == gown.id)
        self.assertEqual(mini["hay"], row.search_hay)

    def test_a_tag_color_word_finds_the_gowns_in_that_category(self):
        wedding = _make_gown(1, category=Gown.Category.WEDDING_GOWN, gown_id="Wedding Gown-BU-001", color_name="Blue", color_code="BU")
        suit = _make_gown(2, category=Gown.Category.SUIT, gown_id="Suit-BU-001", color_name="Blue", color_code="BU")
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        hays = {m["id"]: m["hay"] for m in response.context["gowns_min"]}
        # Wedding Gowns are tagged White, Suits Black -- so "white tag" finds only the former,
        # even though both gowns are really Blue.
        self.assertIn("white", hays[wedding.id])
        self.assertIn("black", hays[suit.id])
        self.assertNotIn("black", hays[wedding.id])

    def test_gowns_list_by_category_then_number_not_grouped_by_color(self):
        ids = ["Wedding Gown-WH-001", "Wedding Gown-BU-002", "Wedding Gown-WH-003", "Wedding Gown-BU-004"]
        for n, gown_id in enumerate(ids, start=1):
            _make_gown(n, category=Gown.Category.WEDDING_GOWN, gown_id=gown_id,
                       color_code=gown_id.split("-")[1], color_name="X")
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        listed = [g.gown_id for g in response.context["gowns"]]
        self.assertEqual(listed, ids)

    def test_a_gown_whose_id_has_no_number_sinks_to_the_end_of_its_category(self):
        _make_gown(1, category=Gown.Category.WEDDING_GOWN, gown_id="Wedding Gown-WH-005")
        odd = _make_gown(2, category=Gown.Category.WEDDING_GOWN, gown_id="Wedding Gown-LEGACY")
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        listed = [g.gown_id for g in response.context["gowns"]]
        self.assertEqual(listed, ["Wedding Gown-WH-005", odd.gown_id])
        self.assertEqual(next(g for g in response.context["gowns"] if g.id == odd.id).tag_number, "—")


class GownRemovalLogTests(TestCase):
    """Removing a gown always records WHY, in the same atomic step as the delete -- so a
    number missing from the catalog is either explained by the Removal Log or is exactly
    the kind of gap worth asking about."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="removal_staff", password="x", is_staff=True,
            first_name="Maria", last_name="Cruz",
        )
        cls.customer = User.objects.create_user(username="removal_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)

    def _remove(self, gown, **body):
        return self.client.post(
            reverse("arabela_admin:gown_delete", args=[gown.id]),
            data=json.dumps(body), content_type="application/json",
        )

    # ---- a reason is mandatory -----------------------------------------------------------
    def test_no_reason_means_nothing_is_deleted_and_nothing_is_logged(self):
        gown = _make_gown()
        for body in ({}, {"reason": ""}, {"reason": "Because"}, {"note": "just a note"}):
            with self.subTest(body=body):
                response = self._remove(gown, **body)
                self.assertEqual(response.status_code, 400)
                self.assertIn("why", response.json()["error"].lower())
        self.assertTrue(Gown.objects.filter(id=gown.id).exists())
        self.assertEqual(GownRemoval.objects.count(), 0)

    def test_an_empty_body_is_refused_too(self):
        gown = _make_gown()
        response = self.client.post(reverse("arabela_admin:gown_delete", args=[gown.id]))
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Gown.objects.filter(id=gown.id).exists())

    def test_other_needs_a_note(self):
        gown = _make_gown()
        self.assertEqual(self._remove(gown, reason="Other").status_code, 400)
        self.assertEqual(self._remove(gown, reason="Other", note="   ").status_code, 400)
        self.assertTrue(Gown.objects.filter(id=gown.id).exists())
        self.assertEqual(self._remove(gown, reason="Other", note="Donated to the church").status_code, 200)

    def test_an_over_long_note_is_refused(self):
        gown = _make_gown()
        self.assertEqual(self._remove(gown, reason="Damaged", note="x" * 301).status_code, 400)
        self.assertTrue(Gown.objects.filter(id=gown.id).exists())

    # ---- what gets recorded --------------------------------------------------------------
    def test_a_removal_is_logged_with_everything_needed_to_recognise_the_gown_later(self):
        gown = _make_gown(
            3, category=Gown.Category.WEDDING_GOWN, gown_id="Wedding Gown-WH-003",
            name="White", color_name="White", color_code="WH", size=Gown.Size.LARGE,
            photo_url="https://example.com/white.jpg",
        )
        response = self._remove(gown, reason="Lost or stolen", note="Not on the rack after closing")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(Gown.objects.filter(id=gown.id).exists())

        entry = GownRemoval.objects.get()
        self.assertEqual(
            (entry.gown_id, entry.tracking_number, entry.name, entry.category, entry.color_name, entry.size),
            ("Wedding Gown-WH-003", 3, "White", "Wedding Gown", "White", "Large"),
        )
        self.assertEqual(entry.photo_url, "https://example.com/white.jpg")
        self.assertEqual((entry.reason, entry.note), ("Lost or stolen", "Not on the rack after closing"))
        self.assertEqual(entry.removed_by, self.staff)
        self.assertEqual(entry.removed_by_name, "Maria Cruz")
        self.assertIsNotNone(entry.removed_at)
        self.assertIn("retired", response.json()["message"])

    def test_the_log_keeps_saying_who_did_it_after_that_account_is_deleted(self):
        gown = _make_gown()
        self._remove(gown, reason="Retired")
        self.staff.delete()
        entry = GownRemoval.objects.get()
        self.assertIsNone(entry.removed_by)
        self.assertEqual(entry.removed_by_name, "Maria Cruz")
        self.staff = User.objects.create_user(username="removal_staff2", password="x", is_staff=True)

    # ---- the reservation guard still holds ------------------------------------------------
    def test_a_gown_on_an_active_reservation_is_refused_and_logs_nothing(self):
        gown = _make_gown()
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Booked Customer")
        ReservationItem.objects.create(
            reservation=reservation, gown=gown, gown_name=gown.name,
            rental_date=date.today(), return_date=date.today() + timedelta(days=3),
        )
        response = self._remove(gown, reason="Damaged")
        self.assertEqual(response.status_code, 400)
        self.assertIn(reservation.reference_code, response.json()["error"])
        self.assertTrue(Gown.objects.filter(id=gown.id).exists())
        self.assertEqual(GownRemoval.objects.count(), 0)

    def test_the_log_entry_and_the_delete_happen_together_or_not_at_all(self):
        gown = _make_gown()
        self.client.raise_request_exception = False
        with patch.object(Gown, "delete", side_effect=RuntimeError("database went away")):
            response = self._remove(gown, reason="Damaged")
        self.assertEqual(response.status_code, 500)
        self.assertTrue(Gown.objects.filter(id=gown.id).exists())
        self.assertEqual(GownRemoval.objects.count(), 0, "a failed delete must not leave a log entry claiming it happened")

    # ---- numbers are never reused -----------------------------------------------------------
    def test_a_removed_gowns_number_is_never_given_to_a_new_gown(self):
        def add(color_code, color_name="White"):
            r = self.client.post(reverse("arabela_admin:gown_create"), data={
                "name": "White", "category": "Wedding Gown", "color_name": color_name, "color_code": color_code,
                "size": "Medium", "rental_price": "20000", "name_choice": "same",
            })
            self.assertEqual(r.status_code, 200, r.content)
            return r.json()["gown"]

        first, second, third = add("WH"), add("WH"), add("BU", "Blue")
        self.assertEqual(
            [g["gown_id"] for g in (first, second, third)],
            ["Wedding Gown-WH-001", "Wedding Gown-WH-002", "Wedding Gown-BU-003"],
        )
        self.assertEqual(self._remove(Gown.objects.get(id=second["id"]), reason="Damaged").status_code, 200)
        fourth = add("WH")
        self.assertEqual(fourth["gown_id"], "Wedding Gown-WH-004")
        # The retired number is still accounted for in the log.
        self.assertEqual(GownRemoval.objects.get().gown_id, "Wedding Gown-WH-002")

    # ---- bulk ---------------------------------------------------------------------------------
    def _bulk(self, **body):
        return self.client.post(
            reverse("arabela_admin:gown_bulk_action"),
            data=json.dumps(body), content_type="application/json",
        )

    def test_bulk_remove_without_a_reason_deletes_nothing(self):
        gowns = [_make_gown(n) for n in (1, 2)]
        response = self._bulk(action="delete", ids=[g.id for g in gowns])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Gown.objects.count(), 2)
        self.assertEqual(GownRemoval.objects.count(), 0)

    def test_bulk_remove_logs_each_gown_and_skips_the_ones_on_a_reservation(self):
        free_a, free_b, booked = _make_gown(1), _make_gown(2), _make_gown(3)
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Bulk Customer")
        ReservationItem.objects.create(
            reservation=reservation, gown=booked, gown_name=booked.name,
            rental_date=date.today(), return_date=date.today() + timedelta(days=3),
        )
        response = self._bulk(action="delete", ids=[free_a.id, free_b.id, booked.id], reason="Retired", note="Sold")
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()
        self.assertEqual((result["deleted"], len(result["skipped"])), (2, 1))
        self.assertEqual(
            sorted(GownRemoval.objects.values_list("gown_id", flat=True)),
            sorted([free_a.gown_id, free_b.gown_id]),
        )
        self.assertEqual({r.reason for r in GownRemoval.objects.all()}, {"Retired"})
        self.assertTrue(Gown.objects.filter(id=booked.id).exists())

    # ---- what staff see ------------------------------------------------------------------------
    def test_the_catalog_lists_removals_newest_first_with_undated_history_last(self):
        old = GownRemoval.objects.create(
            gown_id="Wedding Gown-WH-018", tracking_number=18, category="Wedding Gown",
            reason="Other", note="Reason not recorded", removed_at=None,
        )
        recent = GownRemoval.objects.create(
            gown_id="Wedding Gown-WH-030", tracking_number=30, name="White", category="Wedding Gown",
            reason="Damaged", removed_by_name="Maria Cruz", removed_at=timezone.now(),
        )
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertEqual([r.id for r in response.context["gown_removals"]], [recent.id, old.id])
        self.assertContains(response, "Wedding Gown-WH-030")
        self.assertContains(response, "Damaged beyond repair")
        self.assertContains(response, "Removed before the Removal Log existed")
        self.assertEqual(response.context["gown_removals"][1].number_label, "018")

    def test_the_removal_search_text_matches_the_number_in_every_form(self):
        GownRemoval.objects.create(
            gown_id="Wedding Gown-WH-003", tracking_number=3, name="White", category="Wedding Gown",
            reason="Damaged", removed_at=timezone.now(),
        )
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        hay = response.context["removal_hays"][0]
        for needle in ("wedding gown-wh-003", "003", "#3", "damaged beyond repair", "white"):
            with self.subTest(needle=needle):
                self.assertIn(needle, hay)

    def test_the_remove_modal_offers_every_reason_and_explains_what_will_happen(self):
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        for label in ("Damaged beyond repair", "Lost or stolen", "Retired (sold or no longer offered)", "Other"):
            self.assertContains(response, label)
        self.assertContains(response, "retired for good")
        self.assertContains(response, "Removal Log")

    def test_an_empty_log_says_so(self):
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertContains(response, "No gowns have been removed yet.")


class GownNameConflictTests(TestCase):
    """Adding a gown whose name is already used in its category. The customer site shows
    every gown sharing a name as ONE product with a quantity -- right for another size of
    the same dress, wrong for a different dress that just shares the name -- so the person
    adding it is asked which it is, and a 'different' one is numbered automatically."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="nameconf_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def _add(self, name="White", category="Wedding Gown", **extra):
        data = {
            "name": name, "category": category, "color_name": "White", "color_code": "WH",
            "size": "Medium", "rental_price": "20000",
        }
        data.update(extra)
        return self.client.post(reverse("arabela_admin:gown_create"), data=data)

    def test_the_first_gown_with_a_name_is_added_without_any_question(self):
        response = self._add()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["gown"]["name"], "White")

    def test_a_repeated_name_is_asked_about_and_nothing_is_saved_yet(self):
        self._add()
        response = self._add()
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["code"], "name_conflict")
        self.assertEqual(body["name_conflict"]["name"], "White")
        self.assertEqual(body["name_conflict"]["count"], 1)
        self.assertEqual(body["name_conflict"]["existing"][0]["gown_id"], "Wedding Gown-WH-001")
        self.assertEqual(Gown.objects.count(), 1)

    def test_the_question_comes_before_the_photo_is_uploaded(self):
        """Answering must not upload the photo twice."""
        self._add()
        photo = SimpleUploadedFile("dress.jpg", b"\xff\xd8\xff\xe0fakejpeg", content_type="image/jpeg")
        with patch("arabela_admin.views._save_gown_photo") as save_photo:
            response = self._add(photo=photo)
        self.assertEqual(response.status_code, 409)
        save_photo.assert_not_called()

    def test_same_gown_keeps_the_identical_name_so_it_groups_into_one_listing(self):
        first = self._add().json()["gown"]
        second = self._add(name_choice="same", size="Large")
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(second.json()["gown"]["name"], first["name"])
        # Two gowns, one name -> the customer site's grouping shows them as one product.
        self.assertEqual(Gown.objects.filter(name="White").count(), 2)

    def test_a_different_gown_is_numbered_automatically(self):
        self._add()
        second = self._add(name_choice="different")
        third = self._add(name_choice="different")
        self.assertEqual(second.json()["gown"]["name"], "White (2)")
        self.assertEqual(third.json()["gown"]["name"], "White (3)")

    def test_a_different_gown_still_saves_when_the_plain_names_counter_already_reached_its_number(self):
        """The real catalog has many gowns named 'White' (slugs white, white-2, white-3...).
        'White (2)' slugifies to 'white-2' -- taken -- and this used to answer 'Couldn't save
        that gown just now'."""
        self._add()
        self._add(name_choice="same")
        self._add(name_choice="same")
        response = self._add(name_choice="different")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["gown"]["name"], "White (2)")
        slugs = list(Gown.objects.values_list("slug", flat=True))
        self.assertEqual(len(slugs), len(set(slugs)), slugs)

    def test_the_comparison_ignores_case_and_surrounding_spaces(self):
        self._add()
        self.assertEqual(self._add(name="  white ").status_code, 409)
        response = self._add(name="WHITE", name_choice="different")
        self.assertEqual(response.json()["gown"]["name"], "WHITE (2)")

    def test_retyping_a_numbered_name_gives_the_next_number_not_a_double_suffix(self):
        self._add()
        self._add(name_choice="different")  # White (2)
        response = self._add(name="White (2)", name_choice="different")
        self.assertEqual(response.json()["gown"]["name"], "White (3)")

    def test_the_same_name_in_another_category_is_not_a_conflict(self):
        self._add()
        response = self._add(category="Long Gown")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["gown"]["name"], "White")

    def test_saying_different_when_the_name_is_free_leaves_it_alone(self):
        response = self._add(name="Ivory", name_choice="different")
        self.assertEqual(response.json()["gown"]["name"], "Ivory")

    def test_a_bogus_choice_is_treated_as_no_answer(self):
        self._add()
        self.assertEqual(self._add(name_choice="whatever").status_code, 409)

    def test_the_numbered_name_always_fits_the_name_column(self):
        long_name = "W" * 150
        self._add(name=long_name)
        response = self._add(name=long_name, name_choice="different")
        self.assertEqual(response.status_code, 200, response.content)
        name = response.json()["gown"]["name"]
        self.assertEqual(len(name), 150)
        self.assertTrue(name.endswith(" (2)"))

    def test_the_form_fills_the_name_from_the_color_and_offers_the_choice(self):
        page = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertContains(page, "syncNameFromColor()")
        self.assertContains(page, "addGown('same')")
        self.assertContains(page, "addGown('different')")


class GownPhysicalCheckTests(TestCase):
    """The walkthrough check: someone confirms a gown is physically present, and the gown
    remembers when and by whom -- what narrows a missing gown to a time window."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="check_staff", password="x", is_staff=True, first_name="Ana", last_name="Reyes",
        )

    def setUp(self):
        self.client.force_login(self.staff)

    def _bulk(self, **body):
        return self.client.post(
            reverse("arabela_admin:gown_bulk_action"),
            data=json.dumps(body), content_type="application/json",
        )

    def test_marking_gowns_checked_stamps_when_and_by_whom(self):
        a, b, untouched = _make_gown(1), _make_gown(2), _make_gown(3)
        before = timezone.now()
        response = self._bulk(action="checked", ids=[a.id, b.id])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["updated"], 2)
        for gown in (a, b):
            gown.refresh_from_db()
            self.assertGreaterEqual(gown.last_checked_at, before)
            self.assertEqual(gown.last_checked_by, self.staff)
        untouched.refresh_from_db()
        self.assertIsNone(untouched.last_checked_at)

    def test_checking_a_gown_bumps_its_updated_at_too(self):
        gown = _make_gown()
        original = gown.updated_at
        self._bulk(action="checked", ids=[gown.id])
        gown.refresh_from_db()
        self.assertGreater(gown.updated_at, original)

    def test_the_catalog_reports_never_checked_recent_and_stale(self):
        never, recent, stale = _make_gown(1), _make_gown(2), _make_gown(3)
        now = timezone.now()
        Gown.objects.filter(id=recent.id).update(last_checked_at=now - timedelta(days=2), last_checked_by=self.staff)
        Gown.objects.filter(id=stale.id).update(last_checked_at=now - timedelta(days=20), last_checked_by=self.staff)
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        rows = {g.id: g for g in response.context["gowns"]}
        self.assertIsNone(rows[never.id].days_since_check)
        self.assertEqual(rows[never.id].check_label, "Never checked")
        self.assertEqual(rows[recent.id].days_since_check, 2)
        self.assertEqual(rows[stale.id].days_since_check, 20)
        self.assertIn("by Ana Reyes", rows[recent.id].check_label)
        self.assertEqual(response.context["checked_recent_count"], 1)

    def test_the_filter_data_marks_never_checked_as_none_so_it_counts_as_due(self):
        gown = _make_gown()
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        mini = next(m for m in response.context["gowns_min"] if m["id"] == gown.id)
        self.assertIsNone(mini["check_days"])
        self.assertEqual(mini["check_label"], "Never checked")

    def test_signed_out_and_unknown_ids_are_handled(self):
        self.client.logout()
        self.assertEqual(self._bulk(action="checked", ids=[1]).status_code, 401)
        self.client.force_login(self.staff)
        response = self._bulk(action="checked", ids=[999999999])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["updated"], 0)


class BookingScreensShowTheMatchedGownTests(TestCase):
    """At pick-up several gowns can share a name, so the booking screens name the exact
    physical gown -- its ID, and the color of its category's tag -- for the staffer to
    match against the tags on the rack."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="booking_tag_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="booking_tag_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)

    def _booking(self, status=Reservation.Status.CONFIRMED, gown="make"):
        if gown == "make":
            gown = _make_gown(
                12, category=Gown.Category.WEDDING_GOWN, gown_id="Wedding Gown-WH-012", name="White",
            )
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Tag Customer", status=status,
        )
        item = ReservationItem.objects.create(
            reservation=reservation, gown=gown, gown_name="White",
            rental_date=date.today(), return_date=date.today() + timedelta(days=3),
        )
        return reservation, item

    def test_active_reservations_names_the_exact_gown_and_its_tag(self):
        self._booking()
        response = self.client.get(reverse("arabela_admin:active_reservations"))
        self.assertContains(response, "Wedding Gown-WH-012")
        self.assertContains(response, "White tag")

    def test_active_reservations_can_be_searched_by_gown_id(self):
        """The reservation's own row filter includes every matched gown's ID (escaped the
        way this page already escapes customer names, so its hyphens appear as \\u002D)."""
        self._booking()
        response = self.client.get(reverse("arabela_admin:active_reservations"))
        self.assertContains(response, "wedding gown\\u002Dwh\\u002D012")

    def test_an_item_with_no_matched_gown_shows_no_tag_line(self):
        self._booking(gown=None)
        response = self.client.get(reverse("arabela_admin:active_reservations"))
        self.assertNotContains(response, "Match this to the tag on the dress")

    def test_the_tag_follows_the_owners_color_choice(self):
        self._booking()
        SiteSettings.load()
        settings_obj = SiteSettings.load()
        settings_obj.category_tag_colors = {"Wedding Gown": "Red"}
        settings_obj.save()
        response = self.client.get(reverse("arabela_admin:active_reservations"))
        self.assertContains(response, "Red tag")
        self.assertNotContains(response, "White tag")

    def test_pending_approval_names_the_exact_gown_and_its_tag(self):
        self._booking(status=Reservation.Status.PENDING)
        response = self.client.get(reverse("arabela_admin:pending_approval"))
        self.assertContains(response, "Wedding Gown-WH-012")
        self.assertContains(response, "White tag")

    def test_the_calendars_booking_details_get_the_gown_id_and_tag_color(self):
        self._booking()
        response = self.client.get(reverse("arabela_admin:rental_schedule"))
        booking_events = [
            e for e in response.context["calendar_events"]
            if e.get("extendedProps", {}).get("gownCode")
        ]
        self.assertTrue(booking_events)
        props = booking_events[0]["extendedProps"]
        self.assertEqual(props["gownCode"], "Wedding Gown-WH-012")
        self.assertEqual(props["tagColor"], "White")
        self.assertEqual(props["tagHex"], TAG_COLOR_HEX["White"])
        self.assertContains(response, 'id="bookingModalGownTag"')

    def test_a_calendar_booking_with_no_matched_gown_has_empty_tag_fields(self):
        self._booking(gown=None)
        response = self.client.get(reverse("arabela_admin:rental_schedule"))
        props = next(
            e["extendedProps"] for e in response.context["calendar_events"]
            if e.get("extendedProps", {}).get("itemId")
        )
        self.assertEqual((props["gownCode"], props["tagColor"], props["tagHex"]), ("", "", ""))

    def test_reservation_records_items_carry_the_tag_color_beside_the_gown_id(self):
        self._booking()
        response = self.client.get(reverse("arabela_admin:reservation_records"))
        item = response.context["records"][0]["items"][0]
        self.assertEqual(item["gownCode"], "Wedding Gown-WH-012")
        self.assertEqual((item["tagColor"], item["tagHex"]), ("White", TAG_COLOR_HEX["White"]))


class MoveGownsBetweenCategoriesWorkflowTests(TestCase):
    """The procedure for moving gowns to another category -- e.g. four gowns catalogued as
    Wedding Gowns that are really Evening Gowns: check any open booking on them back in, remove
    them (a reason, logged), then add them under the new category with the same photo. Runs
    through the same views staff use, so it doubles as proof the whole path works."""

    MOVES = [
        ("Wedding Gown-BU-001", "Wedding Gown 18", "Blue", "BU", "Evening Gown 95"),
        ("Wedding Gown-GD-001", "Wedding Gown 21", "Gold", "GD", "Evening Gown 96"),
        ("Wedding Gown-GN-001", "Wedding Gown 22", "Green", "GN", "Evening Gown 97"),
        ("Wedding Gown-PK-001", "Wedding Gown 19", "Pink", "PK", "Evening Gown 98"),
    ]

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username="move_owner", password="x", is_staff=True, first_name="Olivia", last_name="Owner",
        )
        UserProfile.objects.create(user=cls.owner, role=UserProfile.Role.OWNER)
        cls.customer = User.objects.create_user(username="move_customer", password="x")

    def setUp(self):
        self.client.force_login(self.owner)
        self.gowns = []
        self.items = []
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Move Customer", status=Reservation.Status.CONFIRMED,
        )
        for n, (gown_id, name, color, code, _new) in enumerate(self.MOVES):
            gown = Gown.objects.create(
                gown_id=gown_id, name=name, category="Wedding Gown", color_name=color, color_code=code,
                size=Gown.Size.MEDIUM, rental_price=Decimal("20000.00"), status=Gown.Status.RESERVED,
                photo_url=f"https://res.example.com/{code}.jpg",
            )
            self.gowns.append(gown)
            self.items.append(ReservationItem.objects.create(
                reservation=reservation, gown=gown, gown_name=name,
                rental_date=date.today(), return_date=date.today() + timedelta(days=4),
            ))
        # A neighbouring wedding gown that is NOT moved, holding the highest number.
        self.stays = Gown.objects.create(
            gown_id="Wedding Gown-WH-033", name="Wedding Gown 33", category="Wedding Gown",
            color_name="White", color_code="WH", size=Gown.Size.MEDIUM, rental_price=Decimal("20000.00"),
        )

    def _remove(self, gown, **body):
        return self.client.post(
            reverse("arabela_admin:gown_delete", args=[gown.id]),
            data=json.dumps(body), content_type="application/json",
        )

    def _check_in(self, item):
        return self.client.post(
            reverse("arabela_admin:reservation_item_mark_returned", args=[item.id]),
            data=json.dumps({"condition": "Good"}), content_type="application/json",
        )

    def test_the_whole_move_works_in_order(self):
        # 1. While the bookings are open the gowns can't be removed -- and nothing is logged.
        for gown in self.gowns:
            response = self._remove(gown, reason="Other", note="Moving to Evening Gown")
            self.assertEqual(response.status_code, 400)
            self.assertIn("RSV-", response.json()["error"])
        self.assertEqual(GownRemoval.objects.count(), 0)

        # 2. Check the bookings in through the normal workflow.
        for item in self.items:
            self.assertEqual(self._check_in(item).status_code, 200)

        # 3. Remove each gown with a reason; every removal is logged with its number.
        for gown, (gown_id, name, *_rest) in zip(self.gowns, self.MOVES):
            response = self._remove(gown, reason="Other", note=f"Moved to Evening Gown (was {name}).")
            self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            sorted(GownRemoval.objects.values_list("gown_id", flat=True)),
            sorted(m[0] for m in self.MOVES),
        )
        self.assertFalse(Gown.objects.filter(category="Wedding Gown").exclude(id=self.stays.id).exists())

        # The bookings survive, keeping the gown's name, with no gown attached.
        for item, (_gid, name, *_rest) in zip(self.items, self.MOVES):
            item.refresh_from_db()
            self.assertIsNone(item.gown_id)
            self.assertEqual(item.gown_name, name)
            self.assertEqual(item.stage, ReservationItem.Stage.RETURNED)

        # 4. Add them as Evening Gowns 95-98 (fresh category -> numbers 001-004), same photos.
        created = []
        for _gid, _name, color, code, new_name in self.MOVES:
            response = self.client.post(reverse("arabela_admin:gown_create"), data={
                "name": new_name, "category": "Evening Gown", "color_name": color, "color_code": code,
                "size": "Medium", "rental_price": "20000", "condition": "Good", "status": "Available",
            })
            self.assertEqual(response.status_code, 200, response.content)
            made = Gown.objects.get(id=response.json()["gown"]["id"])
            made.photo_url = f"https://res.example.com/{code}.jpg"
            made.save(update_fields=["photo_url", "updated_at"])
            created.append(made)
        self.assertEqual(
            [g.gown_id for g in created],
            ["Evening Gown-BU-001", "Evening Gown-GD-002", "Evening Gown-GN-003", "Evening Gown-PK-004"],
        )
        self.assertEqual([g.name for g in created], [m[4] for m in self.MOVES])
        self.assertEqual([g.status for g in created], ["Available"] * 4)
        self.assertTrue(all(g.photo_url for g in created))
        self.assertEqual(len({g.slug for g in created}), 4)

        # 5. The wedding side keeps counting from its own highest number -- the moved gowns'
        # numbers are retired, not handed to the next wedding gown.
        response = self.client.post(reverse("arabela_admin:gown_create"), data={
            "name": "Ivory", "category": "Wedding Gown", "color_name": "Ivory", "color_code": "IV",
            "size": "Medium", "rental_price": "20000",
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["gown"]["gown_id"], "Wedding Gown-IV-034")


class ScheduleActionsTests(TestCase):
    """The scheduling actions now live ONLY in Active Reservations -- Mark Picked Up (on or
    after the current pick-up date), Change pick-up date (earlier only) and Change return
    date (later only) -- and the Rental Schedule is view-only. Everything the calendar and
    the early / late remarks read is the booking's own pick-up / return date, so a change
    shows in the Rental Schedule colours by itself."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="sched_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="sched_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)
        self.today = date.today()
        self.gown = _make_gown(status=Gown.Status.AVAILABLE)

    def _item(self, start, end, gown=None, status=Reservation.Status.CONFIRMED, name="Sched Customer",
              stage=ReservationItem.Stage.PICKUP, **extra):
        gown = gown or self.gown
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name=name, status=status, phone="09171234567",
        )
        return ReservationItem.objects.create(
            reservation=reservation, gown=gown, gown_name=gown.name, stage=stage,
            rental_date=self.today + timedelta(days=start), return_date=self.today + timedelta(days=end), **extra,
        )

    def _post(self, name, item, date_=None):
        kwargs = {"data": json.dumps({"date": date_.isoformat()}), "content_type": "application/json"} if date_ else {}
        return self.client.post(reverse(f"arabela_admin:{name}", args=[item.id]), **kwargs)

    def _remarks(self, item):
        item.refresh_from_db()
        return [r["text"] for r in views_module._item_remarks(item, self.today)]

    # ---- the old editing endpoints are gone ------------------------------------------------
    def test_the_old_free_editing_endpoints_no_longer_exist(self):
        from django.urls import NoReverseMatch
        for name in ("reservation_item_reschedule", "reservation_item_set_actual_date"):
            with self.subTest(name=name), self.assertRaises(NoReverseMatch):
                reverse(f"arabela_admin:{name}", args=[1])

    # ---- Mark Picked Up -----------------------------------------------------------------------
    def test_mark_picked_up_is_refused_before_the_pickup_date(self):
        item = self._item(1, 5)
        response = self._post("reservation_item_mark_picked_up", item)
        self.assertEqual(response.status_code, 400)
        self.assertIn("not yet", response.json()["error"])
        item.refresh_from_db()
        self.assertEqual((item.stage, item.picked_up_on), (ReservationItem.Stage.PICKUP, None))

    def test_mark_picked_up_works_on_the_pickup_date_and_records_today(self):
        item = self._item(0, 4)
        response = self._post("reservation_item_mark_picked_up", item)
        self.assertEqual(response.status_code, 200, response.content)
        item.refresh_from_db()
        self.gown.refresh_from_db()
        self.assertEqual((item.stage, item.picked_up_on), (ReservationItem.Stage.RESERVED, self.today))
        self.assertEqual(self.gown.status, Gown.Status.RESERVED)

    def test_a_late_customer_can_still_be_marked_picked_up(self):
        item = self._item(-3, 1)
        self.assertEqual(self._post("reservation_item_mark_picked_up", item).status_code, 200)

    def test_it_follows_the_changed_pickup_date_not_the_original(self):
        item = self._item(6, 10)
        self.assertEqual(self._post("reservation_item_mark_picked_up", item).status_code, 400)
        self.assertEqual(self._post("reservation_item_change_pickup", item, self.today + timedelta(days=2)).status_code, 200)
        self.assertEqual(self._post("reservation_item_mark_picked_up", item).status_code, 400)  # 2 days still to go
        self.assertEqual(self._post("reservation_item_change_pickup", item, self.today).status_code, 200)
        self.assertEqual(self._post("reservation_item_mark_picked_up", item).status_code, 200)

    def test_mark_picked_up_twice_and_unapproved_bookings_are_refused(self):
        item = self._item(0, 4)
        self._post("reservation_item_mark_picked_up", item)
        self.assertEqual(self._post("reservation_item_mark_picked_up", item).status_code, 400)
        pending = self._item(0, 4, status=Reservation.Status.PENDING, name="Pending One", gown=_make_gown(2))
        self.assertEqual(self._post("reservation_item_mark_picked_up", pending).status_code, 400)

    def test_scheduling_actions_need_a_signed_in_staff_member(self):
        item = self._item(0, 4)
        self.client.logout()
        for name in ("reservation_item_mark_picked_up", "reservation_item_change_pickup", "reservation_item_change_return"):
            with self.subTest(name=name):
                self.assertEqual(self._post(name, item, self.today + timedelta(days=9)).status_code, 401)

    # ---- Change pick-up date (earlier only) ------------------------------------------------------
    def test_pickup_can_move_earlier_and_keeps_what_was_originally_booked(self):
        item = self._item(6, 10)
        response = self._post("reservation_item_change_pickup", item, self.today)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["days_early"], 6)
        item.refresh_from_db()
        self.assertEqual(item.rental_date, self.today)
        self.assertEqual(item.original_rental_date, self.today + timedelta(days=6))
        self.assertIn("Changed pick-up date by customer · 6 days early", self._remarks(item))

    def test_a_second_pickup_change_still_counts_from_the_original(self):
        item = self._item(10, 14)
        self._post("reservation_item_change_pickup", item, self.today + timedelta(days=6))
        self._post("reservation_item_change_pickup", item, self.today + timedelta(days=3))
        item.refresh_from_db()
        self.assertEqual(item.original_rental_date, self.today + timedelta(days=10))
        self.assertIn("Changed pick-up date by customer · 7 days early", self._remarks(item))

    def test_the_early_remark_survives_the_actual_pickup_and_a_late_one(self):
        """Booked for the 6th, moved to today (6 days early): collecting it today -- or even
        3 days after the moved date -- never changes the "6 days early" remark."""
        item = self._item(6, 10)
        self._post("reservation_item_change_pickup", item, self.today)
        self.assertEqual(self._post("reservation_item_mark_picked_up", item).status_code, 200)
        self.assertIn("Changed pick-up date by customer · 6 days early", self._remarks(item))
        late = self._item(-3, 1, name="Late Collector", gown=_make_gown(2),
                          original_rental_date=self.today + timedelta(days=3))
        self.assertEqual(self._post("reservation_item_mark_picked_up", late).status_code, 200)
        self.assertIn("Changed pick-up date by customer · 6 days early", self._remarks(late))

    def test_extending_the_return_does_not_move_the_customers_event_date(self):
        item = self._item(-2, 2, stage=ReservationItem.Stage.RESERVED, picked_up_on=self.today - timedelta(days=2))
        event_before = item.effective_event_date
        self.assertEqual(self._post("reservation_item_change_return", item, self.today + timedelta(days=6)).status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.effective_event_date, event_before)

    def test_pickup_cannot_move_later_or_stay_the_same(self):
        item = self._item(6, 10)
        for days in (6, 8):
            with self.subTest(days=days):
                self.assertEqual(self._post("reservation_item_change_pickup", item, self.today + timedelta(days=days)).status_code, 400)

    def test_pickup_cannot_move_into_the_past(self):
        item = self._item(6, 10)
        response = self._post("reservation_item_change_pickup", item, self.today - timedelta(days=1))
        self.assertEqual(response.status_code, 400)
        item.refresh_from_db()
        self.assertEqual(item.rental_date, self.today + timedelta(days=6))

    def test_pickup_cannot_move_onto_days_another_customer_holds(self):
        item = self._item(10, 14, name="Mover")
        other = self._item(5, 7, name="Booked First")
        response = self._post("reservation_item_change_pickup", item, self.today + timedelta(days=6))
        self.assertEqual(response.status_code, 409)
        self.assertIn("Booked First", response.json()["error"])
        self.assertIn(other.reservation.reference_code, response.json()["error"])
        item.refresh_from_db()
        self.assertEqual(item.rental_date, self.today + timedelta(days=10))

    def test_pickup_cannot_move_into_another_bookings_cooldown(self):
        item = self._item(14, 18)
        before = self._item(0, 2, name="Before", status=Reservation.Status.CONFIRMED, stage=ReservationItem.Stage.RESERVED)
        GownUnavailability.objects.create(
            gown=self.gown, start_date=self.today + timedelta(days=3), end_date=self.today + timedelta(days=5),
            reason=GownUnavailability.Reason.COOLDOWN, auto_for_item=before,
        )
        refused = self._post("reservation_item_change_pickup", item, self.today + timedelta(days=4))
        self.assertEqual(refused.status_code, 409)
        self.assertIn("blocked", refused.json()["error"])
        self.assertEqual(self._post("reservation_item_change_pickup", item, self.today + timedelta(days=6)).status_code, 200)

    def test_pickup_cannot_change_once_the_gown_is_out_or_back(self):
        out = self._item(0, 4, stage=ReservationItem.Stage.RESERVED, picked_up_on=self.today)
        self.assertEqual(self._post("reservation_item_change_pickup", out, self.today - timedelta(days=0)).status_code, 400)
        back = self._item(3, 7, stage=ReservationItem.Stage.RETURNED, name="Back", gown=_make_gown(2))
        self.assertEqual(self._post("reservation_item_change_pickup", back, self.today).status_code, 400)

    # ---- Change return date (later only) ----------------------------------------------------------
    def test_return_can_move_later_and_the_cooldown_follows(self):
        item = self._item(0, 4, stage=ReservationItem.Stage.RESERVED, picked_up_on=self.today)
        block = GownUnavailability.objects.create(
            gown=self.gown, start_date=self.today + timedelta(days=5), end_date=self.today + timedelta(days=7),
            reason=GownUnavailability.Reason.COOLDOWN, auto_for_item=item,
        )
        response = self._post("reservation_item_change_return", item, self.today + timedelta(days=7))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["days_late"], 3)
        item.refresh_from_db()
        block.refresh_from_db()
        self.assertEqual(item.return_date, self.today + timedelta(days=7))
        self.assertEqual(item.original_return_date, self.today + timedelta(days=4))
        self.assertEqual((block.start_date, block.end_date), (self.today + timedelta(days=8), self.today + timedelta(days=10)))
        self.assertIn("Changed return date by customer · 3 days late", self._remarks(item))

    def test_return_cannot_move_earlier_or_stay_the_same(self):
        item = self._item(0, 6)
        for days in (6, 4):
            with self.subTest(days=days):
                self.assertEqual(self._post("reservation_item_change_return", item, self.today + timedelta(days=days)).status_code, 400)

    def test_return_cannot_run_into_the_next_booking(self):
        item = self._item(0, 4)
        self._item(8, 10, name="Next Customer")
        response = self._post("reservation_item_change_return", item, self.today + timedelta(days=9))
        self.assertEqual(response.status_code, 409)
        self.assertIn("Next Customer", response.json()["error"])
        self.assertEqual(self._post("reservation_item_change_return", item, self.today + timedelta(days=7)).status_code, 200)

    def test_a_returned_gown_cannot_have_its_return_changed(self):
        item = self._item(-5, -1, stage=ReservationItem.Stage.RETURNED)
        self.assertEqual(self._post("reservation_item_change_return", item, self.today + timedelta(days=3)).status_code, 400)

    # ---- the remarks (one definition for both pages) -----------------------------------------------
    def test_pickup_remarks_count_down_then_go_late(self):
        cases = {3: "Pick-up in 3 days", 1: "Pick-up in 1 day", 0: "Pick up today", -2: "2 days late for pick-up"}
        for offset, text in cases.items():
            with self.subTest(offset=offset):
                item = self._item(offset, offset + 4, gown=_make_gown(10 + offset + 5))
                self.assertIn(text, self._remarks(item))

    def test_a_gown_still_out_after_its_return_date_is_overdue_by_itself(self):
        item = self._item(-6, -2, stage=ReservationItem.Stage.RESERVED, picked_up_on=self.today - timedelta(days=6))
        remarks = self._remarks(item)
        self.assertIn("Out with customer", remarks)
        self.assertIn("Overdue · 2 days late", remarks)
        events = self.client.get(reverse("arabela_admin:rental_schedule")).context["calendar_events"]
        mine = [e for e in events if e["extendedProps"].get("itemId") == item.id]
        self.assertIn("Danger", [e["extendedProps"]["calendar"] for e in mine])  # the red Overdue marker
        self.assertEqual(mine[0]["extendedProps"]["stage"], "Overdue")

    def test_overdue_counts_from_the_original_return_date(self):
        item = self._item(-8, -1, stage=ReservationItem.Stage.RESERVED, picked_up_on=self.today - timedelta(days=8),
                          original_return_date=self.today - timedelta(days=4))
        self.assertIn("Overdue · 4 days late", self._remarks(item))

    def test_a_late_return_keeps_its_late_remark_after_it_comes_back(self):
        item = self._item(-8, -2, stage=ReservationItem.Stage.RETURNED, returned_on=self.today - timedelta(days=1))
        self.assertIn("1 day late", self._remarks(item))

    # ---- the pages --------------------------------------------------------------------------------
    def test_active_reservations_shows_remarks_phone_and_the_right_buttons(self):
        waiting = self._item(6, 10, name="Waiting Customer")
        self._post("reservation_item_change_pickup", waiting, self.today + timedelta(days=2))
        out = self._item(-1, 3, stage=ReservationItem.Stage.RESERVED, picked_up_on=self.today - timedelta(days=1),
                         name="Out Customer", gown=_make_gown(2))
        response = self.client.get(reverse("arabela_admin:active_reservations"))
        html = response.content.decode()
        self.assertContains(response, "Changed pick-up date by customer · 4 days early")
        self.assertContains(response, "tel:09171234567")
        self.assertContains(response, f'data-item-id="{waiting.id}"')
        self.assertEqual(response.context["today_iso"], self.today.isoformat())
        self.assertIn(f'data-kind="pickup" data-item-id="{waiting.id}"', html)
        self.assertIn(f'data-kind="return" data-item-id="{waiting.id}"', html)
        self.assertIn(f'data-item-id="{waiting.id}">Mark Picked Up', html)
        self.assertNotIn(f'data-kind="pickup" data-item-id="{out.id}"', html)   # already out
        self.assertIn(f'data-kind="return" data-item-id="{out.id}"', html)
        self.assertIn(f'data-item-id="{out.id}">Undo', html)
        self.assertNotIn("data-actual-date-field", html)
        self.assertNotIn("resv-actual-date-edit-btn", html)

    def test_the_page_hands_each_item_the_other_bookings_of_its_gown(self):
        mine = self._item(10, 14, name="Mine")
        other = self._item(20, 24, name="Someone Else")
        holds = self.client.get(reverse("arabela_admin:active_reservations")).context["item_holds"]
        self.assertEqual([h["itemId"] for h in holds[mine.id]], [other.id])
        self.assertNotIn(mine.id, [h["itemId"] for h in holds[mine.id]])

    def test_the_rental_schedule_shows_the_changed_dates_and_is_view_only(self):
        item = self._item(6, 10)
        self._post("reservation_item_change_pickup", item, self.today + timedelta(days=1))
        self._post("reservation_item_change_return", item, self.today + timedelta(days=12))
        response = self.client.get(reverse("arabela_admin:rental_schedule"))
        props = next(e["extendedProps"] for e in response.context["calendar_events"]
                     if e.get("extendedProps", {}).get("itemId") == item.id)
        self.assertEqual(props["rentalDate"], (self.today + timedelta(days=1)).isoformat())
        self.assertEqual(props["originalRentalDate"], (self.today + timedelta(days=6)).isoformat())
        self.assertEqual(props["returnDate"], (self.today + timedelta(days=12)).isoformat())
        self.assertEqual(props["originalReturnDate"], (self.today + timedelta(days=10)).isoformat())
        texts = [r["text"] for r in props["remarks"]]
        self.assertIn("Changed pick-up date by customer · 5 days early", texts)
        self.assertIn("Changed return date by customer · 2 days late", texts)
        self.assertIn(item.reservation.reference_code, props["activeUrl"])
        # The pick-up / return stretches are painted by the same Pick-up / Return markers --
        # the pick-up starts on the NEW day, the return runs to the NEW end.
        spans = {e["extendedProps"]["calendar"]: (e["start"], e["end"]) for e in response.context["calendar_events"]
                 if e.get("extendedProps", {}).get("itemId") == item.id}
        self.assertEqual(spans["Warning"][0], (self.today + timedelta(days=1)).isoformat())
        html = response.content.decode()
        self.assertIn('id="bookingModalReadOnly"', html)
        self.assertIn("Open in Active Reservations", html)
        self.assertIn('id="bookingModalSaveChangesBtn" style="display: none;"', html)
        self.assertIn('id="bookingModalMarkReturnedBtn" style="display: none;"', html)
        self.assertIn('id="bookingModalEditable" style="display: none;"', html)


class GcashSettingsTests(TestCase):
    """The owner sets the shop's GCash QR picture, account name and number in Edit Profile; the
    customer checkout shows them. Owner-only (staff get a plain 403), blank is allowed, and a bad
    number or file is refused before anything is saved."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="gcash_owner", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.owner, role=UserProfile.Role.OWNER)
        cls.staff = User.objects.create_user(username="gcash_staff", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.staff, role=UserProfile.Role.STAFF)

    def setUp(self):
        self.client.force_login(self.owner)

    def _details(self, **data):
        return self.client.post(
            reverse("arabela_admin:gcash_details_update"), data=json.dumps(data), content_type="application/json",
        )

    @staticmethod
    def _png(name="qr.png", size=(8, 8)):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", size, "black").save(buf, format="PNG")
        return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")

    def _upload(self, upload):
        return self.client.post(reverse("arabela_admin:gcash_qr_upload"), data={"qr": upload})

    # ---- name + number ---------------------------------------------------------------------------
    def test_the_owner_can_save_the_name_and_number(self):
        response = self._details(name="  Arabela Gown Rental ", number="0917 123 4567")
        self.assertEqual(response.status_code, 200, response.content)
        saved = SiteSettings.load()
        self.assertEqual((saved.gcash_account_name, saved.gcash_number), ("Arabela Gown Rental", "09171234567"))

    def test_common_ways_of_writing_a_number_all_normalise(self):
        for raw in ("09171234567", "0917-123-4567", "+63 917 123 4567", "639171234567", "(0917) 123 4567"):
            with self.subTest(raw=raw):
                self.assertEqual(self._details(name="A", number=raw).json()["number"], "09171234567")

    def test_a_bad_number_is_refused_and_nothing_changes(self):
        self._details(name="Keep Me", number="09171234567")
        for bad in ("12345", "0817 123 4567", "091712345678", "abcdefghijk", "+1 555 123 4567"):
            with self.subTest(bad=bad):
                response = self._details(name="Changed", number=bad)
                self.assertEqual(response.status_code, 400)
                self.assertIn("11 digits", response.json()["error"])
        self.assertEqual(SiteSettings.load().gcash_account_name, "Keep Me")

    def test_blank_clears_and_an_over_long_name_is_refused(self):
        self._details(name="Someone", number="09171234567")
        self.assertEqual(self._details(name="", number="").status_code, 200)
        saved = SiteSettings.load()
        self.assertEqual((saved.gcash_account_name, saved.gcash_number), ("", ""))
        self.assertEqual(self._details(name="x" * 101, number="").status_code, 400)

    def test_malformed_requests_are_rejected(self):
        url = reverse("arabela_admin:gcash_details_update")
        for body in (b"not json", b"[]", b"5"):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(url, data=body, content_type="application/json").status_code, 400)

    # ---- who may do it -----------------------------------------------------------------------------
    def test_staff_who_are_not_the_owner_get_a_plain_403_on_every_endpoint(self):
        self.client.force_login(self.staff)
        self.assertEqual(self._details(name="X", number="09171234567").status_code, 403)
        self.assertEqual(self._upload(self._png()).status_code, 403)
        self.assertEqual(self.client.post(reverse("arabela_admin:gcash_qr_remove")).status_code, 403)
        self.assertEqual(SiteSettings.load().gcash_account_name, "")

    def test_signed_out_gets_json_401(self):
        self.client.logout()
        self.assertEqual(self._details(name="X", number="").status_code, 401)
        self.assertEqual(self.client.post(reverse("arabela_admin:gcash_qr_remove")).status_code, 401)

    def test_get_requests_are_not_allowed(self):
        self.assertEqual(self.client.get(reverse("arabela_admin:gcash_details_update")).status_code, 405)

    # ---- the QR picture ------------------------------------------------------------------------------
    def test_a_qr_can_be_uploaded_replaced_and_removed(self):
        with patch("arabela_admin.views.default_storage") as storage:
            storage.save.return_value = "gcash_qr/abc.png"
            storage.url.return_value = "https://files.example/gcash_qr/abc.png"
            storage.exists.return_value = True
            first = self._upload(self._png())
            self.assertEqual(first.status_code, 200, first.content)
            self.assertEqual(first.json()["url"], "https://files.example/gcash_qr/abc.png")
            self.assertEqual(SiteSettings.load().gcash_qr_url, "https://files.example/gcash_qr/abc.png")

            storage.save.return_value = "gcash_qr/def.png"
            storage.url.return_value = "https://files.example/gcash_qr/def.png"
            self.assertEqual(self._upload(self._png("again.png")).status_code, 200)
            storage.delete.assert_called_with("gcash_qr/abc.png")  # the old file is not left behind
            self.assertEqual(SiteSettings.load().gcash_qr_url, "https://files.example/gcash_qr/def.png")

            removed = self.client.post(reverse("arabela_admin:gcash_qr_remove"))
            self.assertEqual(removed.json(), {"success": True, "url": ""})
            storage.delete.assert_called_with("gcash_qr/def.png")
        self.assertEqual(SiteSettings.load().gcash_qr_url, "")

    def test_a_bad_upload_is_refused_without_touching_storage(self):
        with patch("arabela_admin.views.default_storage") as storage:
            cases = {
                "no file": None,
                "wrong type": SimpleUploadedFile("qr.gif", b"GIF89a", content_type="image/gif"),
                "renamed text file": SimpleUploadedFile("qr.png", b"this is not an image", content_type="image/png"),
                "too big": SimpleUploadedFile("qr.png", b"x" * (5 * 1024 * 1024 + 1), content_type="image/png"),
            }
            for label, upload in cases.items():
                with self.subTest(case=label):
                    response = self.client.post(reverse("arabela_admin:gcash_qr_upload"), data={"qr": upload} if upload else {})
                    self.assertEqual(response.status_code, 400)
            storage.save.assert_not_called()
        self.assertEqual(SiteSettings.load().gcash_qr_url, "")

    def test_a_storage_failure_is_a_clean_502_and_keeps_the_old_qr(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"gcash_qr_url": "https://files.example/gcash_qr/old.png"})
        with patch("arabela_admin.views.default_storage") as storage:
            storage.save.side_effect = RuntimeError("storage down")
            self.assertEqual(self._upload(self._png()).status_code, 502)
        self.assertEqual(SiteSettings.load().gcash_qr_url, "https://files.example/gcash_qr/old.png")

    def test_an_outside_url_is_never_deleted_from_storage(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"gcash_qr_url": "https://elsewhere.example/qr.png"})
        with patch("arabela_admin.views.default_storage") as storage:
            self.client.post(reverse("arabela_admin:gcash_qr_remove"))
            storage.delete.assert_not_called()

    # ---- the pages -----------------------------------------------------------------------------------
    def test_the_owner_sees_the_card_with_the_saved_values_and_staff_do_not(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={
            "gcash_account_name": "Arabela Gown Rental", "gcash_number": "09171234567",
            "gcash_qr_url": "https://files.example/gcash_qr/x.png",
        })
        html = self.client.get(reverse("arabela_admin:page", args=["profile"])).content.decode()
        self.assertIn('x-data="gcashCard()"', html)
        self.assertIn('data-number="09171234567"', html)
        self.assertIn('data-qr="https://files.example/gcash_qr/x.png"', html)
        self.assertIn("View Full Image", html)
        self.client.force_login(self.staff)
        self.assertNotIn("gcashCard", self.client.get(reverse("arabela_admin:page", args=["profile"])).content.decode())


class GcashReferenceTests(TestCase):
    """The optional GCash reference number: stored as digits, refused when it can't be real, and
    flagged in Payment Verification / Security Deposits when the same number is on two bookings."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="gcash_ref_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="gcash_ref_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)

    def _reservation(self, reference, status=Reservation.Status.PENDING):
        return Reservation.objects.create(
            customer=self.customer, customer_name="Ref Customer", status=status,
            gcash_reference=reference, payment_proof_url="https://example.test/p.jpg",
        )

    def test_no_reference_means_no_duplicates(self):
        self.assertEqual(self._reservation("").gcash_duplicate_codes, "")

    def test_a_number_used_once_is_not_a_duplicate(self):
        self.assertEqual(self._reservation("1234567890123").gcash_duplicate_codes, "")

    def test_the_same_number_on_two_bookings_names_the_other_one(self):
        first = self._reservation("1234567890123")
        second = self._reservation("1234567890123")
        self.assertEqual(second.gcash_duplicate_codes, first.reference_code)
        self.assertEqual(first.gcash_duplicate_codes, second.reference_code)

    def test_a_different_number_is_not_a_duplicate(self):
        self._reservation("1234567890123")
        self.assertEqual(self._reservation("9999999999999").gcash_duplicate_codes, "")

    def test_payment_verification_flags_a_reused_number_and_shows_the_number(self):
        first = self._reservation("1234567890123")
        second = self._reservation("1234567890123")
        html = self.client.get(reverse("arabela_admin:payment_verification")).content.decode()
        self.assertIn("gcashRef: '1234567890123'", html)
        self.assertIn(f"Same GCash ref as {first.reference_code}", html)
        self.assertIn(f"Same GCash ref as {second.reference_code}", html)
        self.assertIn("GCash Reference No.", html)

    def test_payment_verification_shows_no_warning_for_a_unique_number(self):
        self._reservation("1234567890123")
        html = self.client.get(reverse("arabela_admin:payment_verification")).content.decode()
        self.assertNotIn("Same GCash ref as", html)

    def test_security_deposits_shows_the_number_and_the_warning_too(self):
        first = self._reservation("5555555555555", status=Reservation.Status.CONFIRMED)
        second = self._reservation("5555555555555", status=Reservation.Status.CONFIRMED)
        for reservation in (first, second):  # the page lists a booking once per gown on it
            ReservationItem.objects.create(
                reservation=reservation, gown_name="Deposit Row Gown",
                rental_date=date.today(), return_date=date.today() + timedelta(days=3),
            )
        html = self.client.get(reverse("arabela_admin:security_deposits")).content.decode()
        self.assertIn("gcashRef: '5555555555555'", html)
        self.assertIn(f"Same GCash ref as {first.reference_code}", html)


class CustomCategoryTests(TestCase):
    """The owner adds (and removes) rental categories from Gown Catalog -> Add Category. A new one is
    treated like a built-in everywhere: Add Gown, Tag Colors, the customer's collection page, the
    All page, search and the AI helper."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="cat_owner", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.owner, role=UserProfile.Role.OWNER)
        cls.staff = User.objects.create_user(username="cat_staff", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.staff, role=UserProfile.Role.STAFF)

    def setUp(self):
        self.client.force_login(self.owner)

    def _add(self, name):
        return self.client.post(
            reverse("arabela_admin:gown_category_create"), data=json.dumps({"name": name}),
            content_type="application/json",
        )

    # ---- adding ----------------------------------------------------------------------------------
    def test_the_owner_can_add_a_category(self):
        response = self._add("  Debut   Gown ")
        self.assertEqual(response.status_code, 200, response.content)
        category = CustomCategory.objects.get()
        self.assertEqual((category.name, category.slug), ("Debut Gown", "debut-gown"))
        self.assertIn("Debut Gown", all_category_names())

    def test_bad_names_are_refused_with_a_plain_reason(self):
        cases = {
            "": "at least 2",
            "x": "at least 2",
            "A" * 21: "20 characters",
            "Debut <b>": "letters, numbers",
            "Debut & Co": "letters, numbers",
            "wedding gown": "already a category",    # a built-in, ignoring capitals
            "Wedding": "too close",                   # same page address as the built-in Wedding Gown
            "All": "too close",                       # reserved for the All page
            "Ball-Gown": "too close",                 # the old address of Evening Gown stays reserved
            "Evening-Gown": "too close",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                response = self._add(name)
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected, response.json()["error"])
        self.assertEqual(CustomCategory.objects.count(), 0)

    def test_the_same_name_twice_is_refused_ignoring_capitals(self):
        self.assertEqual(self._add("Debut Gown").status_code, 200)
        self.assertEqual(self._add("DEBUT GOWN").status_code, 400)
        self.assertEqual(CustomCategory.objects.count(), 1)

    def test_only_the_owner_can_add_or_remove(self):
        category = CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        self.client.force_login(self.staff)
        self.assertEqual(self._add("Another One").status_code, 403)
        self.assertEqual(self.client.post(reverse("arabela_admin:gown_category_delete", args=[category.id])).status_code, 403)
        self.client.logout()
        self.assertEqual(self._add("Another One").status_code, 401)
        self.assertEqual(CustomCategory.objects.count(), 1)

    def test_malformed_requests_are_rejected(self):
        url = reverse("arabela_admin:gown_category_create")
        for body in (b"not json", b"[]", b"5"):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(url, data=body, content_type="application/json").status_code, 400)

    # ---- removing --------------------------------------------------------------------------------------
    def test_an_empty_category_can_be_removed(self):
        category = CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        response = self.client.post(reverse("arabela_admin:gown_category_delete", args=[category.id]))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(CustomCategory.objects.exists())

    def test_a_category_with_gowns_cannot_be_removed(self):
        category = CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        _make_gown(1, category="Debut Gown")
        response = self.client.post(reverse("arabela_admin:gown_category_delete", args=[category.id]))
        self.assertEqual(response.status_code, 400)
        self.assertIn("1 gown", response.json()["error"])
        self.assertTrue(CustomCategory.objects.exists())

    def test_removing_something_already_gone_is_a_clean_404(self):
        self.assertEqual(self.client.post(reverse("arabela_admin:gown_category_delete", args=[999999])).status_code, 404)

    # ---- the admin side -----------------------------------------------------------------------------------
    def test_a_new_category_shows_in_the_catalog_add_gown_and_tag_colors(self):
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        rows = {r["key"]: r for r in response.context["category_rows"]}
        self.assertTrue(rows["Debut Gown"]["custom"])
        self.assertFalse(rows["Wedding Gown"]["custom"])
        self.assertEqual(rows["Debut Gown"]["tag_color"], "Gray")
        self.assertIn("Debut Gown", response.context["tag_colors_data"])
        self.assertContains(response, "+ Add Category")
        self.assertContains(response, "All categories")

    def test_staff_do_not_see_the_add_category_button(self):
        self.client.force_login(self.staff)
        html = self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()
        self.assertNotIn("+ Add Category", html)

    def test_a_gown_can_be_added_in_a_new_category_and_gets_its_own_numbering(self):
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        response = self.client.post(reverse("arabela_admin:gown_create"), data={
            "name": "Rose", "category": "Debut Gown", "color_name": "Pink", "color_code": "PK",
            "size": "Medium", "rental_price": "5000",
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["gown"]["gown_id"], "Debut Gown-PK-001")

    def test_an_unknown_category_is_still_refused(self):
        response = self.client.post(reverse("arabela_admin:gown_create"), data={
            "name": "Rose", "category": "Made Up", "color_name": "Pink", "color_code": "PK",
            "size": "Medium", "rental_price": "5000",
        })
        self.assertEqual(response.status_code, 400)

    def test_the_owner_can_set_a_new_categorys_tag_colour_and_it_is_used(self):
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        response = self.client.post(
            reverse("arabela_admin:gown_tag_colors_update"),
            data=json.dumps({"colors": {"Debut Gown": "Purple"}}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(SiteSettings.load().tag_colors()["Debut Gown"], "Purple")

    # ---- the customer side ----------------------------------------------------------------------------------
    def test_the_customer_gets_a_collection_page_for_the_new_category(self):
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        _make_gown(1, category="Debut Gown", name="Rose Debut")
        anon = Client()
        response = anon.get("/collections/debut-gown/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Debut Gown")
        self.assertContains(response, "Rose Debut")

    def test_an_unknown_collection_is_a_404_and_built_in_pages_are_unchanged(self):
        anon = Client()
        self.assertEqual(anon.get("/collections/not-a-category/").status_code, 404)
        for built_in in ("wedding", "evening-gown", "bridesmaid-dresses", "all"):
            with self.subTest(page=built_in):
                self.assertEqual(anon.get(f"/collections/{built_in}/").status_code, 200)

    def test_a_removed_category_stops_being_a_page(self):
        category = CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        self.client.post(reverse("arabela_admin:gown_category_delete", args=[category.id]))
        self.assertEqual(Client().get("/collections/debut-gown/").status_code, 404)

    def test_the_all_page_search_and_ai_helper_know_the_new_category(self):
        from ai_recommendation.views import _system_prompt
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        _make_gown(1, category="Debut Gown", name="Rose Debut")
        anon = Client()
        self.assertContains(anon.get("/collections/all/"), "Debut Gown")
        home = anon.get("/")
        meta = home.context["search_overlay_collections_meta"]
        self.assertIn(("Debut Gown", "/collections/debut-gown/"), [(m["label"], m["url"]) for m in meta])
        catalog = home.context["search_overlay_catalog"]
        self.assertIn("Rose Debut", [i["title"] for i in catalog])
        self.assertIn("/collections/debut-gown/", _system_prompt())

    def test_a_product_page_works_for_a_gown_in_a_new_category(self):
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        gown = _make_gown(1, category="Debut Gown", name="Rose Debut")
        response = Client().get(f"/collections/debut-gown/products/{gown.slug}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rose Debut")


class GownCatalogReservedHolderTests(TestCase):
    """Gown Catalog says WHO a Reserved gown is reserved for, and links to that booking: Active
    Reservations for a confirmed / picked-up / overdue one, Pending Approval for one still awaiting
    payment approval. A Reserved gown with no live booking is labelled as set by hand."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="holder_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="holder_customer", password="x")
        today = timezone.localdate()
        cls.start = date(today.year, 12, 20)
        cls.end = date(today.year, 12, 23)

    def setUp(self):
        self.client.force_login(self.staff)

    def _book(self, gown, status=Reservation.Status.CONFIRMED, name="Maria Santos", start=None, end=None,
              stage=None):
        reservation = Reservation.objects.create(customer=self.customer, customer_name=name, status=status)
        extra = {"stage": stage} if stage else {}
        item = ReservationItem.objects.create(
            reservation=reservation, gown=gown, gown_name=gown.name,
            rental_date=start or self.start, return_date=end or self.end, **extra,
        )
        return reservation, item

    def _page(self):
        return self.client.get(reverse("arabela_admin:gown_catalog"))

    def test_a_confirmed_booking_is_named_and_links_to_active_reservations(self):
        gown = _make_gown(1, status=Gown.Status.RESERVED)
        reservation, _ = self._book(gown)
        response = self._page()
        html = response.content.decode()
        link = reverse("arabela_admin:active_reservations") + "?search=" + reservation.reference_code
        self.assertIn(f'href="{link}"', html)
        self.assertIn("Maria Santos &middot; Dec 20\u201323", html)
        holders = response.context["gown_holders"][gown.id]
        self.assertEqual(len(holders), 1)
        self.assertEqual(holders[0]["reference"], reservation.reference_code)
        self.assertEqual(holders[0]["url"], link)
        self.assertFalse(holders[0]["pending"])
        self.assertNotIn("Set manually", html)

    def test_a_booking_awaiting_payment_approval_links_to_pending_approval(self):
        gown = _make_gown(1, status=Gown.Status.RESERVED)
        reservation, _ = self._book(gown, status=Reservation.Status.PENDING)
        response = self._page()
        html = response.content.decode()
        link = reverse("arabela_admin:pending_approval") + "?search=" + reservation.reference_code
        self.assertIn(f'href="{link}"', html)
        self.assertIn("awaiting payment approval", html)
        self.assertTrue(response.context["gown_holders"][gown.id][0]["pending"])

    def test_picked_up_and_overdue_bookings_link_to_active_reservations(self):
        for n, status in enumerate((Reservation.Status.ACTIVE, Reservation.Status.OVERDUE), start=1):
            gown = _make_gown(n, status=Gown.Status.RESERVED)
            self._book(gown, status=status)
        for holders in self._page().context["gown_holders"].values():
            self.assertIn(reverse("arabela_admin:active_reservations"), holders[0]["url"])

    def test_a_reserved_gown_with_no_booking_is_labelled_as_set_by_hand(self):
        gown = _make_gown(1, status=Gown.Status.RESERVED)
        response = self._page()
        self.assertIn("Set manually", response.content.decode())
        self.assertNotIn(gown.id, response.context["gown_holders"])
        self.assertEqual(response.context["gown_holders"], {})

    def test_returned_cancelled_and_rejected_bookings_do_not_count(self):
        returned = _make_gown(1, status=Gown.Status.RESERVED)
        cancelled = _make_gown(2, status=Gown.Status.RESERVED)
        rejected = _make_gown(3, status=Gown.Status.RESERVED)
        self._book(returned, stage=ReservationItem.Stage.RETURNED)
        self._book(cancelled, status=Reservation.Status.CANCELLED)
        self._book(rejected, status=Reservation.Status.REJECTED)
        response = self._page()
        self.assertEqual(response.context["gown_holders"], {})
        self.assertEqual(response.content.decode().count("Set manually"), 3)

    def test_an_available_gown_shows_no_booking_line_even_with_a_pending_booking(self):
        gown = _make_gown(1, status=Gown.Status.AVAILABLE)
        self._book(gown, status=Reservation.Status.PENDING)
        response = self._page()
        self.assertEqual(response.context["gown_holders"], {})
        self.assertNotIn("Set manually", response.content.decode())
        self.assertNotIn("awaiting payment approval", response.content.decode().split("Booked by")[0])

    def test_several_bookings_show_the_nearest_first_with_a_count_and_all_in_the_window(self):
        gown = _make_gown(1, status=Gown.Status.RESERVED)
        later, _ = self._book(gown, name="Later Customer", start=date(self.start.year, 12, 28), end=date(self.start.year, 12, 30))
        sooner, _ = self._book(gown, name="Sooner Customer")
        response = self._page()
        holders = response.context["gown_holders"][gown.id]
        self.assertEqual([h["reference"] for h in holders], [sooner.reference_code, later.reference_code])
        html = response.content.decode()
        row = html.split("<tr data-gown-row")[1]
        self.assertIn("Sooner Customer", row)
        self.assertIn("+1 more", row)
        self.assertNotIn("Later Customer", row.split("Never checked")[0])
        self.assertIn("Later Customer", html)  # still listed in the gown's View window data

    def test_two_gowns_each_name_their_own_customer(self):
        a = _make_gown(1, status=Gown.Status.RESERVED)
        b = _make_gown(2, status=Gown.Status.RESERVED)
        ra, _ = self._book(a, name="Alice Customer")
        rb, _ = self._book(b, name="Bruno Customer")
        holders = self._page().context["gown_holders"]
        self.assertEqual(holders[a.id][0]["reference"], ra.reference_code)
        self.assertEqual(holders[b.id][0]["reference"], rb.reference_code)

    def test_a_customer_name_is_escaped_not_injected(self):
        gown = _make_gown(1, status=Gown.Status.RESERVED)
        self._book(gown, name="<b>Sneaky</b>")
        html = self._page().content.decode()
        self.assertNotIn("<b>Sneaky</b>", html)
        self.assertIn("&lt;b&gt;Sneaky&lt;/b&gt;", html)

    def test_the_view_window_has_a_booked_by_section(self):
        _make_gown(1, status=Gown.Status.RESERVED)
        html = self._page().content.decode()
        self.assertIn("Booked by", html)
        self.assertIn("currentHolders()", html)
        self.assertIn('id="gown-holders-data"', html)
        self.assertIn("Open in Active Reservations", html)

    def test_it_costs_one_query_however_many_gowns_are_reserved(self):
        gowns = [_make_gown(n, status=Gown.Status.RESERVED) for n in range(1, 6)]
        for gown in gowns:
            self._book(gown)
        today = timezone.localdate()
        every_gown = list(Gown.objects.all())
        with self.assertNumQueries(1):
            holders = views_module._reserved_gown_holders(every_gown, today)
        self.assertEqual(len(holders), 5)
        with self.assertNumQueries(0):  # nothing Reserved -> nothing to look up
            views_module._reserved_gown_holders([], today)

    def test_the_booking_link_actually_opens_the_filtered_page(self):
        gown = _make_gown(1, status=Gown.Status.RESERVED)
        reservation, _ = self._book(gown, name="Findable Customer")
        url = self._page().context["gown_holders"][gown.id][0]["url"]
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, reservation.reference_code)
        self.assertContains(page, "Findable Customer")

    def test_date_ranges_read_short(self):
        today = date(2026, 10, 6)
        rng = views_module._holder_range
        self.assertEqual(rng(date(2026, 10, 12), date(2026, 10, 12), today), "Oct 12")
        self.assertEqual(rng(date(2026, 10, 12), date(2026, 10, 15), today), "Oct 12\u201315")
        self.assertEqual(rng(date(2026, 10, 30), date(2026, 11, 2), today), "Oct 30 \u2013 Nov 2")
        self.assertEqual(rng(date(2026, 12, 30), date(2027, 1, 2), today), "Dec 30 \u2013 Jan 2, 2027")
        self.assertEqual(rng(date(2027, 3, 4), date(2027, 3, 6), today), "Mar 4\u20136, 2027")


class BuiltinCategoryRemovalTests(TestCase):
    """The 13 original categories can be removed too (hidden -- they live in code), only while empty
    and only by the owner, and come back when a category with the same name is added again."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="bi_owner", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.owner, role=UserProfile.Role.OWNER)
        cls.staff = User.objects.create_user(username="bi_staff", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.staff, role=UserProfile.Role.STAFF)

    def setUp(self):
        self.client.force_login(self.owner)

    def _remove(self, name):
        return self.client.post(
            reverse("arabela_admin:gown_builtin_category_remove"), data=json.dumps({"name": name}),
            content_type="application/json",
        )

    def _add(self, name):
        return self.client.post(
            reverse("arabela_admin:gown_category_create"), data=json.dumps({"name": name}),
            content_type="application/json",
        )

    @staticmethod
    def _grid(path):
        html = Client().get(path).content.decode()
        return re.findall(r'<img src="[^"]*" alt="([^"]*)" loading="lazy" decoding="async" class="absolute inset-0 h-full w-full object-cover', html)

    def test_an_empty_original_category_can_be_removed_and_leaves_everywhere(self):
        self.assertIn("Barong", self._grid("/featured/men/"))
        self.assertEqual(Client().get("/collections/barong/").status_code, 200)
        response = self._remove("Barong")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(HiddenCategory.objects.filter(name="Barong").exists())
        self.assertNotIn("Barong", all_category_names())
        # the customer site
        self.assertNotIn("Barong", self._grid("/featured/men/"))
        self.assertEqual(Client().get("/collections/barong/").status_code, 404)
        self.assertNotIn("/collections/barong/", Client().get("/").content.decode())
        self.assertEqual(Client().get("/collections/all/").status_code, 200)
        # the admin side
        page = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertNotIn("Barong", [r["key"] for r in page.context["category_rows"]])
        self.assertNotIn("Barong", page.context["tag_colors_data"])

    def test_the_home_page_hides_a_removed_tile_and_keeps_the_rest(self):
        self.assertIn("/collections/suit/", Client().get("/").content.decode())
        self._remove("Suit")
        html = Client().get("/").content.decode()
        self.assertNotIn("/collections/suit/", html)
        self.assertIn("/collections/long-gown/", html)

    def test_a_category_with_gowns_cannot_be_removed(self):
        _make_gown(1, category=Gown.Category.BARONG)
        response = self._remove("Barong")
        self.assertEqual(response.status_code, 400)
        self.assertIn("still has 1 gown", response.json()["error"])
        self.assertFalse(HiddenCategory.objects.exists())
        self.assertIn("Barong", all_category_names())
        self.assertEqual(Client().get("/collections/barong/").status_code, 200)

    def test_an_unknown_or_already_removed_category_is_refused(self):
        self.assertEqual(self._remove("Not A Category").status_code, 404)
        self.assertEqual(self._remove("").status_code, 404)
        self.assertEqual(self._remove("Barong").status_code, 200)
        self.assertEqual(self._remove("Barong").status_code, 400)
        self.assertEqual(HiddenCategory.objects.count(), 1)

    def test_only_the_owner_can_remove_one(self):
        self.client.force_login(self.staff)
        self.assertEqual(self._remove("Barong").status_code, 403)
        self.client.logout()
        self.assertEqual(self._remove("Barong").status_code, 401)
        self.assertFalse(HiddenCategory.objects.exists())

    def test_malformed_requests_are_rejected(self):
        url = reverse("arabela_admin:gown_builtin_category_remove")
        for body in (b"not json", b"[]", b"5"):
            with self.subTest(body=body):
                self.assertIn(self.client.post(url, data=body, content_type="application/json").status_code, (400, 404))
        self.assertFalse(HiddenCategory.objects.exists())

    def test_a_removed_category_cannot_be_chosen_for_a_new_gown(self):
        self._remove("Barong")
        response = self.client.post(
            reverse("arabela_admin:gown_create"),
            data={"name": "Ghost Barong", "category": "Barong", "color_name": "Red", "color_code": "RD",
                  "size": "M", "rental_price": "1500"},
        )
        self.assertNotEqual(response.status_code, 200, response.content)
        self.assertFalse(Gown.objects.filter(name="Ghost Barong").exists())

    def test_adding_the_same_name_brings_it_back_exactly_as_it_was(self):
        self._remove("Barong")
        response = self._add("barong")  # capitals don't matter
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["restored"])
        self.assertEqual(response.json()["name"], "Barong")
        self.assertFalse(HiddenCategory.objects.exists())
        self.assertFalse(CustomCategory.objects.exists())  # no duplicate was created
        self.assertIn("Barong", all_category_names())
        self.assertIn("Barong", self._grid("/featured/men/"))
        self.assertEqual(Client().get("/collections/barong/").status_code, 200)

    def test_adding_the_name_of_a_category_still_in_use_is_still_refused(self):
        self.assertEqual(self._add("Barong").status_code, 400)

    def test_removing_every_original_category_leaves_a_working_site(self):
        from gowns.models import Gown as G
        for name in G.Category.values:
            self.assertEqual(self._remove(name).status_code, 200, name)
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        for path in ("/", "/featured/women/", "/featured/men/", "/collections/all/", "/collections/debut-gown/"):
            self.assertEqual(Client().get(path).status_code, 200, path)
        page = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertEqual([r["key"] for r in page.context["category_rows"]], ["Debut Gown"])

    def test_a_product_link_into_a_removed_collection_still_opens(self):
        gown = _make_gown(2, category=Gown.Category.WEDDING_GOWN)
        self._remove("Barong")
        response = Client().get(f"/collections/barong/products/{gown.slug}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Client().get(f"/collections/wedding/products/{gown.slug}/").status_code, 200)

    def test_the_panel_lists_every_category_with_a_remove_button_for_empty_originals(self):
        _make_gown(3, category=Gown.Category.BARONG)
        page = self.client.get(reverse("arabela_admin:gown_catalog"))
        rows = {r["key"]: r for r in page.context["category_rows"]}
        self.assertEqual(rows["Barong"]["audience_label"], "Men's collection")
        self.assertEqual(rows["Wedding Gown"]["audience_label"], "Women's collection")
        html = page.content.decode()
        self.assertIn("removeBuiltinCategory('Wedding Gown')", html)
        self.assertNotIn("removeBuiltinCategory('Barong')", html)  # it has a gown


class CategoryAudienceTests(TestCase):
    """Where an owner-added category shows: the customer's Women's or Men's collection page. The
    built-in ones are unchanged -- Suit and Barong on the Men's page, the rest on the Women's."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="aud_owner", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.owner, role=UserProfile.Role.OWNER)
        cls.staff = User.objects.create_user(username="aud_staff", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.staff, role=UserProfile.Role.STAFF)

    def setUp(self):
        self.client.force_login(self.owner)

    @staticmethod
    def _grid(path):
        """Labels of the tiles in the page's category grid (the nav menu lists every category, so
        a plain text search of the whole page would prove nothing)."""
        html = Client().get(path).content.decode()
        return re.findall(r'<img src="[^"]*" alt="([^"]*)" loading="lazy" decoding="async" class="absolute inset-0 h-full w-full object-cover', html)

    def _add(self, name, **extra):
        return self.client.post(
            reverse("arabela_admin:gown_category_create"),
            data=json.dumps({"name": name, **extra}), content_type="application/json",
        )

    def test_the_built_in_categories_stay_where_they_were(self):
        women, men = self._grid("/featured/women/"), self._grid("/featured/men/")
        self.assertEqual(sorted(men), ["Barong", "Suit"])
        self.assertIn("Wedding Gown", women)
        self.assertIn("Bridesmaid Dresses", women)
        self.assertNotIn("Suit", women)
        self.assertNotIn("Barong", women)
        self.assertNotIn("All", women)

    def test_a_new_category_defaults_to_the_womens_collection(self):
        self.assertEqual(self._add("Debut Gown").status_code, 200)
        self.assertEqual(CustomCategory.objects.get().audience, "women")
        self.assertIn("Debut Gown", self._grid("/featured/women/"))
        self.assertNotIn("Debut Gown", self._grid("/featured/men/"))

    def test_a_category_added_to_the_mens_collection_shows_only_there(self):
        self.assertEqual(self._add("Tuxedo", audience="men").status_code, 200)
        self.assertEqual(CustomCategory.objects.get().audience, "men")
        self.assertIn("Tuxedo", self._grid("/featured/men/"))
        self.assertNotIn("Tuxedo", self._grid("/featured/women/"))
        self.assertIn("Suit", self._grid("/featured/men/"))  # the built-in ones are still there

    def test_an_invalid_audience_is_refused(self):
        for bad in ("kids", "MEN", "both"):
            with self.subTest(bad=bad):
                self.assertEqual(self._add("Tuxedo", audience=bad).status_code, 400)
        self.assertEqual(CustomCategory.objects.count(), 0)

    def test_the_owner_can_move_a_category_between_the_two_pages(self):
        category = CustomCategory.objects.create(name="Tuxedo", slug="tuxedo")
        url = reverse("arabela_admin:gown_category_audience", args=[category.id])
        response = self.client.post(url, data=json.dumps({"audience": "men"}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("Men's collection", response.json()["message"])
        self.assertIn("Tuxedo", self._grid("/featured/men/"))
        self.assertNotIn("Tuxedo", self._grid("/featured/women/"))
        self.client.post(url, data=json.dumps({"audience": "women"}), content_type="application/json")
        self.assertIn("Tuxedo", self._grid("/featured/women/"))
        self.assertNotIn("Tuxedo", self._grid("/featured/men/"))

    def test_moving_needs_a_valid_audience_an_existing_category_and_the_owner(self):
        category = CustomCategory.objects.create(name="Tuxedo", slug="tuxedo")
        url = reverse("arabela_admin:gown_category_audience", args=[category.id])
        self.assertEqual(self.client.post(url, data=json.dumps({"audience": "kids"}), content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post(url, data=b"nope", content_type="application/json").status_code, 400)
        gone = reverse("arabela_admin:gown_category_audience", args=[999999])
        self.assertEqual(self.client.post(gone, data=json.dumps({"audience": "men"}), content_type="application/json").status_code, 404)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(url, data=json.dumps({"audience": "men"}), content_type="application/json").status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(url, data=json.dumps({"audience": "men"}), content_type="application/json").status_code, 401)
        category.refresh_from_db()
        self.assertEqual(category.audience, "women")

    def test_the_catalog_panel_offers_the_choice_and_shows_each_categorys_audience(self):
        CustomCategory.objects.create(name="Tuxedo", slug="tuxedo", audience="men")
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        rows = {r["key"]: r for r in response.context["category_rows"]}
        self.assertEqual(rows["Tuxedo"]["audience"], "men")
        self.assertEqual(rows["Wedding Gown"]["audience"], "")
        html = response.content.decode()
        self.assertIn("Women's collection", html)
        self.assertIn("Men's collection", html)
        self.assertIn("setCategoryAudience(", html)
        self.assertIn("audience: this.newCategoryAudience", html)


class ReceiptDeleteTests(TestCase):
    """Staff can delete a manual receipt photo: the record and file go, the booking is untouched,
    and a staff-only line on its timeline records who did it."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="receipt_del_staff", password="x", is_staff=True, first_name="Rita", last_name="Reyes",
        )
        cls.customer = User.objects.create_user(username="receipt_del_customer", password="x")

    def setUp(self):
        self.client.force_login(self.staff)
        self.reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Receipt Customer", status=Reservation.Status.CONFIRMED,
        )
        self.receipt = ReceiptRecord.objects.create(
            reservation=self.reservation, photo_url="https://files.example/receipt_photos/abc_receipt.jpg",
            uploaded_by=self.staff,
        )

    def _delete(self, receipt_id=None):
        return self.client.post(reverse("arabela_admin:receipt_delete", args=[receipt_id or self.receipt.id]))

    def test_deleting_removes_the_record_and_the_stored_file(self):
        with patch("arabela_admin.views.default_storage") as storage:
            storage.exists.return_value = True
            response = self._delete()
            storage.delete.assert_called_once_with("receipt_photos/abc_receipt.jpg")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["reference"], self.reservation.reference_code)
        self.assertFalse(ReceiptRecord.objects.filter(id=self.receipt.id).exists())

    def test_the_booking_is_untouched_and_a_staff_only_line_records_who_did_it(self):
        with patch("arabela_admin.views.default_storage"):
            self._delete()
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.CONFIRMED)
        event = self.reservation.status_events.get(label="Receipt photo deleted")
        self.assertTrue(event.staff_only)
        self.assertIn("Rita Reyes", event.detail)

    def test_only_that_receipt_goes(self):
        other = ReceiptRecord.objects.create(reservation=self.reservation, photo_url="https://files.example/receipt_photos/other.jpg")
        with patch("arabela_admin.views.default_storage"):
            self._delete()
        self.assertTrue(ReceiptRecord.objects.filter(id=other.id).exists())

    def test_a_file_that_is_not_ours_is_never_deleted_from_storage(self):
        ReceiptRecord.objects.filter(id=self.receipt.id).update(photo_url="https://elsewhere.example/r.jpg")
        with patch("arabela_admin.views.default_storage") as storage:
            self._delete()
            storage.delete.assert_not_called()
        self.assertFalse(ReceiptRecord.objects.filter(id=self.receipt.id).exists())

    def test_a_storage_failure_never_undoes_the_delete(self):
        with patch("arabela_admin.views.default_storage") as storage:
            storage.exists.side_effect = RuntimeError("storage down")
            response = self._delete()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ReceiptRecord.objects.filter(id=self.receipt.id).exists())

    def test_deleting_twice_or_a_missing_receipt_is_a_clean_404(self):
        with patch("arabela_admin.views.default_storage"):
            self._delete()
        self.assertEqual(self._delete().status_code, 404)
        self.assertEqual(self._delete(999999).status_code, 404)

    def test_signed_out_is_refused_and_nothing_is_deleted(self):
        self.client.logout()
        self.assertEqual(self._delete().status_code, 401)
        self.assertTrue(ReceiptRecord.objects.filter(id=self.receipt.id).exists())

    def test_get_is_not_allowed(self):
        response = self.client.get(reverse("arabela_admin:receipt_delete", args=[self.receipt.id]))
        self.assertEqual(response.status_code, 405)

    def test_the_records_page_offers_a_two_step_delete(self):
        html = self.client.get(reverse("arabela_admin:reservation_records")).content.decode()
        self.assertIn("Delete Receipt", html)
        self.assertIn("Yes, delete", html)
        self.assertIn("deleteReceipt()", html)


class CustomerUnlockTests(TestCase):
    """`customer_unlock_view` -- Client List's "Remove lock": lifts one customer's temporary cancellation lock
    early (e.g. when it came from a site problem, not real cancelling). Only that account; the count stays."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="unlock_test_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user(username="unlock_test_customer", password="x")
        cls.other = User.objects.create_user(username="unlock_test_other", password="x")

    def setUp(self):
        self.client.force_login(self.staff)
        self.url = reverse("arabela_admin:customer_unlock", args=[self.customer.id])
        later = timezone.now() + timedelta(minutes=30)
        for user in (self.customer, self.other):
            UserProfile.objects.update_or_create(user=user, defaults={
                "hold_abandon_count": 5, "cancel_tier1_lockout_sent": True, "cancel_lockout_until": later})

    def test_removing_the_lock_frees_only_that_customer_and_tells_them(self):
        response = self.client.post(self.url, data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        profile = UserProfile.objects.get(user=self.customer)
        self.assertIsNone(profile.cancel_lockout_until)
        self.assertEqual(profile.hold_abandon_count, 5)        # the attempt count stays
        self.assertTrue(profile.cancel_tier1_lockout_sent)     # so the 30-minute step doesn't fire again at 6
        self.assertIsNotNone(UserProfile.objects.get(user=self.other).cancel_lockout_until)
        message = CustomerMessage.objects.get(recipient=self.customer)
        self.assertIn("lifted the temporary lock", message.body)
        self.assertFalse(CustomerMessage.objects.filter(recipient=self.other).exists())

        self.client.force_login(self.customer)
        self.assertEqual(self.client.post(reverse("gowns:reservation_hold_start")).status_code, 200)

    def test_an_account_that_is_not_locked_is_refused(self):
        UserProfile.objects.filter(user=self.customer).update(cancel_lockout_until=timezone.now() - timedelta(minutes=1))
        response = self.client.post(self.url, data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(CustomerMessage.objects.filter(recipient=self.customer).exists())

    def test_only_staff_can_remove_a_lock_and_only_from_customers(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(self.url, data="{}", content_type="application/json").status_code, 401)
        self.assertIsNotNone(UserProfile.objects.get(user=self.customer).cancel_lockout_until)
        self.client.force_login(self.staff)
        other_staff = User.objects.create_user(username="unlock_not_a_customer", password="x", is_staff=True)
        response = self.client.post(reverse("arabela_admin:customer_unlock", args=[other_staff.id]),
                                    data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 404)

    def test_client_list_offers_remove_lock_for_a_locked_customer(self):
        html = self.client.get(reverse("arabela_admin:clients")).content.decode()
        self.assertIn("unlockCustomer(viewingCustomer.userId, viewingCustomer.name)", html)
        self.assertIn("Remove lock", html)
        self.assertIn("/admin-panel/api/customers/999999/unlock/", html)


class GownCatalogComponentIntactTests(TestCase):
    """The whole Gown Catalog page is one Alpine component written inside a double-quoted x-data attribute. A
    stray double quote in that code silently cuts the attribute short and every button on the page stops
    working (found by the browser check while adding the phone photo fix). Parsed as real HTML here, so the
    methods must still be INSIDE the attribute, not spilled out after it."""

    def test_the_photo_handlers_are_still_inside_the_component(self):
        from html.parser import HTMLParser

        class ComponentFinder(HTMLParser):
            component = ""

            def handle_starttag(self, tag, attrs):
                for name, value in attrs:
                    if name == "x-data" and value and "isAddGownModal" in value:
                        self.component = value

        staff = User.objects.create_user(username="gc_intact_staff", password="x", is_staff=True)
        self.client.force_login(staff)
        finder = ComponentFinder()
        finder.feed(self.client.get(reverse("arabela_admin:gown_catalog")).content.decode())
        for method in ("onGownPhotoChosen(file)", "updateGownPhoto(file)", "addGown(nameChoice)", "resetAddGownForm()"):
            self.assertIn(method, finder.component, method)
        self.assertIn("window.arabelaReadPickedFile(file)", finder.component)


class ChangeOwnUsernameTests(TestCase):
    """`change_own_username_view` -- the owner can rename their own sign-in (current password required, 4-30 letters
    / numbers / . _ -, unique ignoring capitals across EVERY account). Staff cannot: only the owner manages logins."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="rename_owner", password="OldPass123xyz", is_staff=True)
        UserProfile.objects.update_or_create(user=cls.owner, defaults={"role": UserProfile.Role.OWNER})
        cls.staffer = User.objects.create_user(username="rename_staff", password="StaffPass123", is_staff=True)
        UserProfile.objects.update_or_create(user=cls.staffer, defaults={"role": UserProfile.Role.STAFF})
        cls.customer = User.objects.create_user(username="Rename_Customer", password="CustPass123")

    def setUp(self):
        self.url = reverse("arabela_admin:change_own_username")

    def _post(self, new="quiet.owner_42", password="OldPass123xyz"):
        return self.client.post(self.url, data=json.dumps({"new_username": new, "current_password": password}),
                                content_type="application/json")

    def _username(self):
        self.owner.refresh_from_db()
        return self.owner.username

    def test_the_owner_can_change_their_username(self):
        self.client.force_login(self.owner)
        response = self._post()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"success": True, "username": "quiet.owner_42"})
        self.assertEqual(self._username(), "quiet.owner_42")

    def test_the_owner_stays_signed_in_and_the_password_is_untouched(self):
        self.client.force_login(self.owner)
        self._post()
        self.assertEqual(self.client.get(reverse("arabela_admin:dashboard")).status_code, 200)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password("OldPass123xyz"))

    def test_the_new_username_signs_in_through_the_real_sign_in_page_and_the_old_one_does_not(self):
        self.client.force_login(self.owner)
        self._post()
        login_url = reverse("arabela_admin:admin_login")
        ok = Client().post(login_url, {"username": "quiet.owner_42", "password": "OldPass123xyz"})
        self.assertRedirects(ok, reverse("arabela_admin:dashboard"), fetch_redirect_response=False)
        old = Client().post(login_url, {"username": "rename_owner", "password": "OldPass123xyz"})
        self.assertEqual(old.status_code, 200)
        self.assertContains(old, "Invalid username or password")

    def test_the_current_password_is_required(self):
        self.client.force_login(self.owner)
        for password in ("TotallyWrong1", ""):
            response = self._post(password=password)
            self.assertEqual(response.status_code, 400)
            self.assertIn("current password is incorrect", response.json()["error"])
        self.assertEqual(self._username(), "rename_owner")

    def test_names_that_are_not_allowed_are_refused_with_a_clear_reason(self):
        self.client.force_login(self.owner)
        for bad in ("abc", "a" * 31, "has space", "bad@name", "1234", "1234567", "a$bcd", "", "   ", "名前abc"):
            with self.subTest(name=bad):
                response = self._post(new=bad)
                self.assertEqual(response.status_code, 400)
                self.assertIn("4 to 30 letters, numbers", response.json()["error"])
        self.assertEqual(self._username(), "rename_owner")

    def test_good_names_at_the_edges_are_accepted(self):
        self.client.force_login(self.owner)
        for good in ("abcd", "A.b-c_9", "a" * 30, "9lives"):
            with self.subTest(name=good):
                self.assertEqual(self._post(new=good).status_code, 200)
                self.assertEqual(self._username(), good)

    def test_spaces_around_the_name_are_trimmed(self):
        self.client.force_login(self.owner)
        self.assertEqual(self._post(new="  fresh.name9  ").json()["username"], "fresh.name9")

    def test_a_name_someone_else_has_is_refused_ignoring_capitals_and_for_customers_too(self):
        self.client.force_login(self.owner)
        for taken in ("rename_staff", "RENAME_STAFF", "rename_customer", "RENAME_CUSTOMER"):
            with self.subTest(name=taken):
                response = self._post(new=taken)
                self.assertEqual(response.status_code, 400)
                self.assertIn("already taken", response.json()["error"])
        self.assertEqual(self._username(), "rename_owner")

    def test_the_same_name_is_refused_but_only_changing_the_capitals_is_fine(self):
        self.client.force_login(self.owner)
        same = self._post(new="rename_owner")
        self.assertEqual(same.status_code, 400)
        self.assertIn("already your username", same.json()["error"])
        self.assertEqual(self._post(new="Rename_Owner").status_code, 200)
        self.assertEqual(self._username(), "Rename_Owner")

    def test_staff_cannot_rename_themselves(self):
        """Owner-only, enforced on the server -- the page just doesn't show staff the form."""
        self.client.force_login(self.staffer)
        response = self._post(password="StaffPass123")
        self.assertEqual(response.status_code, 403)
        self.staffer.refresh_from_db()
        self.assertEqual(self.staffer.username, "rename_staff")

    def test_customers_and_signed_out_visitors_are_sent_to_the_admin_sign_in(self):
        for who in (self.customer, None):
            self.client.logout()
            if who is not None:
                self.client.force_login(who)
            response = self._post()
            self.assertEqual(response.status_code, 302)
            self.assertIn(reverse("arabela_admin:admin_login"), response.url)
        self.assertEqual(self._username(), "rename_owner")

    def test_bad_requests_are_refused(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(self.url, data="{not json", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post(self.url, data="[]", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.get(self.url).status_code, 405)


class OwnerPasswordRulesTests(TestCase):
    """`change_own_password_view` after it was tightened: 10+ characters, none of the very common passwords, not only
    numbers, not close to the username (Django's own validators), and a password change signs out every OTHER device."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="pwrules_owner", password="OldPass123xyz", is_staff=True)
        UserProfile.objects.update_or_create(user=cls.owner, defaults={"role": UserProfile.Role.OWNER})

    def setUp(self):
        self.url = reverse("arabela_admin:change_own_password")
        self.client.force_login(self.owner)

    def _post(self, new, confirm=None, current="OldPass123xyz"):
        return self.client.post(self.url, data=json.dumps({
            "current_password": current, "new_password": new,
            "confirm_password": new if confirm is None else confirm,
        }), content_type="application/json")

    def _still_old(self):
        self.owner.refresh_from_db()
        return self.owner.check_password("OldPass123xyz")

    def test_fewer_than_ten_characters_is_refused(self):
        response = self._post("Abcdef123")   # 9
        self.assertEqual(response.status_code, 400)
        self.assertIn("at least 10 characters", response.json()["error"])
        self.assertTrue(self._still_old())

    def test_a_very_common_password_is_refused(self):
        response = self._post("qwertyuiop")
        self.assertEqual(response.status_code, 400)
        self.assertIn("too common", response.json()["error"])
        self.assertTrue(self._still_old())

    def test_a_password_made_only_of_numbers_is_refused(self):
        response = self._post("4829175306")
        self.assertEqual(response.status_code, 400)
        self.assertIn("entirely numeric", response.json()["error"])
        self.assertTrue(self._still_old())

    def test_a_password_too_close_to_the_username_is_refused(self):
        response = self._post("pwrules_owner1")
        self.assertEqual(response.status_code, 400)
        self.assertIn("too similar to the username", response.json()["error"])
        self.assertTrue(self._still_old())

    def test_an_absurdly_long_password_is_refused(self):
        response = self._post("a1" * 65)   # 130
        self.assertEqual(response.status_code, 400)
        self.assertIn("128 characters or fewer", response.json()["error"])
        self.assertTrue(self._still_old())

    def test_mismatched_confirmation_and_the_current_password_are_still_refused(self):
        self.assertEqual(self._post("river-candle-orange-42", confirm="river-candle-orange-43").status_code, 400)
        self.assertEqual(self._post("OldPass123xyz").status_code, 400)
        self.assertEqual(self._post("river-candle-orange-42", current="WrongOne123").status_code, 400)
        self.assertTrue(self._still_old())

    def test_a_good_phrase_and_a_plain_strong_password_are_accepted(self):
        for index, good in enumerate(("river-candle-orange-42", "NewSecurePass456", "Sunflower-Meadow-2049")):
            with self.subTest(password=good):
                self.owner.refresh_from_db()
                current = "OldPass123xyz" if index == 0 else ("river-candle-orange-42", "NewSecurePass456")[index - 1]
                response = self._post(good, current=current)
                self.assertEqual(response.status_code, 200, response.content)
                self.owner.refresh_from_db()
                self.assertTrue(self.owner.check_password(good))

    def test_the_owner_stays_signed_in_but_every_other_device_is_signed_out(self):
        other_device = Client()
        other_device.force_login(self.owner)
        self.assertEqual(other_device.get(reverse("arabela_admin:dashboard")).status_code, 200)
        self.assertEqual(self._post("river-candle-orange-42").status_code, 200)
        self.assertEqual(self.client.get(reverse("arabela_admin:dashboard")).status_code, 200)   # this device
        signed_out = other_device.get(reverse("arabela_admin:dashboard"))
        self.assertEqual(signed_out.status_code, 302)                                              # the other one
        self.assertIn(reverse("arabela_admin:admin_login"), signed_out.url)

    def test_a_body_that_is_not_an_object_is_refused(self):
        self.assertEqual(self.client.post(self.url, data="[1, 2]", content_type="application/json").status_code, 400)


class PreviousSignInTests(TestCase):
    """The sign-in BEFORE the current one is read just ahead of login() (Django overwrites last_login) and shown to the
    owner on Account settings and Edit Profile, so a sign-in she doesn't recognise can be noticed."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="prev_owner", password="OldPass123xyz", is_staff=True)
        UserProfile.objects.update_or_create(user=cls.owner, defaults={"role": UserProfile.Role.OWNER})
        cls.staffer = User.objects.create_user(username="prev_staff", password="StaffPass123", is_staff=True)
        UserProfile.objects.update_or_create(user=cls.staffer, defaults={"role": UserProfile.Role.STAFF})
        cls.account_page = reverse("arabela_admin:page", args=["account-settings"])
        cls.profile_page = reverse("arabela_admin:page", args=["profile"])

    def _sign_in(self, username="prev_owner", password="OldPass123xyz"):
        client = Client()
        response = client.post(reverse("arabela_admin:admin_login"), {"username": username, "password": password})
        self.assertEqual(response.status_code, 302, "the sign-in itself should have worked")
        return client

    def _shown(self, moment):
        from django.template.defaultfilters import date as date_filter
        return date_filter(timezone.localtime(moment), "M j, Y, g:i A")

    def test_a_first_ever_sign_in_has_nothing_before_it(self):
        client = self._sign_in()
        self.assertEqual(client.session["admin_previous_login"], "")
        self.assertContains(client.get(self.account_page), "Not recorded for this session")

    def test_the_next_sign_in_shows_when_the_one_before_it_was(self):
        before = timezone.now() - timedelta(days=2, hours=3)
        User.objects.filter(pk=self.owner.pk).update(last_login=before)
        client = self._sign_in()
        for page in (self.account_page, self.profile_page):
            html = client.get(page).content.decode()
            self.assertIn(self._shown(before), html, page)
            self.assertNotIn("Not recorded for this session", html, page)
        self.owner.refresh_from_db()
        self.assertGreater(self.owner.last_login, before)   # Django did overwrite last_login with "now"

    def test_signing_in_again_moves_the_previous_time_forward(self):
        first = self._sign_in()
        self.owner.refresh_from_db()
        first_login = self.owner.last_login
        second = self._sign_in()
        self.assertContains(second.get(self.account_page), self._shown(first_login))
        first.logout()

    def test_a_session_from_before_this_was_recorded_still_works(self):
        self.client.force_login(self.owner)   # no sign-in through the page, so nothing was recorded
        for page in (self.account_page, self.profile_page):
            response = self.client.get(page)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Not recorded for this session")

    def test_staff_see_their_own_previous_sign_in_on_account_settings(self):
        before = timezone.now() - timedelta(days=1)
        User.objects.filter(pk=self.staffer.pk).update(last_login=before)
        client = self._sign_in("prev_staff", "StaffPass123")
        self.assertContains(client.get(self.account_page), self._shown(before))


class OwnerSecurityPagesTests(TestCase):
    """What the owner (and only the owner) is shown: the username and password forms on Account settings, and the
    Login & Security card on Edit Profile that points at them."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="secpage_owner", password="OldPass123xyz", is_staff=True,
                                             first_name="Ana", last_name="Cruz")
        UserProfile.objects.update_or_create(user=cls.owner, defaults={"role": UserProfile.Role.OWNER})
        cls.staffer = User.objects.create_user(username="secpage_staff", password="StaffPass123", is_staff=True)
        UserProfile.objects.update_or_create(user=cls.staffer, defaults={"role": UserProfile.Role.STAFF})
        cls.account_page = reverse("arabela_admin:page", args=["account-settings"])
        cls.profile_page = reverse("arabela_admin:page", args=["profile"])

    def test_the_owner_sees_both_forms_with_the_live_checklist(self):
        self.client.force_login(self.owner)
        html = self.client.get(self.account_page).content.decode()
        for needle in ('id="change-username"', 'id="change-password"', 'x-model="newUsername"', 'x-model="usernamePassword"',
                       'x-model="currentPassword"', 'x-model="newPassword"', 'x-model="confirmPassword"',
                       "Show passwords", "At least 10 characters", "Save New Username", "Save New Password",
                       "function accountSecurity()", reverse("arabela_admin:change_own_username"),
                       reverse("arabela_admin:change_own_password"), "secpage_owner"):
            self.assertIn(needle, html, needle)
        self.assertNotIn("At least 8 characters", html)

    def test_staff_see_neither_form(self):
        self.client.force_login(self.staffer)
        html = self.client.get(self.account_page).content.decode()
        for needle in ('id="change-username"', 'x-model="newUsername"', 'x-model="currentPassword"', "Show passwords"):
            self.assertNotIn(needle, html, needle)
        self.assertIn("only the shop owner can change passwords", html.lower())
        self.assertIn("secpage_staff", html)

    def test_edit_profile_has_the_login_and_security_card_for_the_owner_only(self):
        self.client.force_login(self.owner)
        html = self.client.get(self.profile_page).content.decode()
        self.assertIn('id="login-security"', html)
        self.assertIn("secpage_owner", html)
        self.assertIn(f"{self.account_page}#change-username", html)
        self.assertIn(f"{self.account_page}#change-password", html)
        self.client.force_login(self.staffer)
        self.assertNotIn('id="login-security"', self.client.get(self.profile_page).content.decode())

    def test_no_template_code_leaks_into_either_page(self):
        for user in (self.owner, self.staffer):
            self.client.force_login(user)
            for page in (self.account_page, self.profile_page):
                html = self.client.get(page).content.decode()
                for leak in ("{%", "{{", "{#"):
                    self.assertNotIn(leak, html, f"{user.username} {page} {leak}")
