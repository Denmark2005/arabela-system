"""Live updates of the pages (static/arabela_admin/live-regions.js): a customer's new reservation shows up in an open admin page, and a new message or
status shows up on an open customer page, WITHOUT a reload.

The behaviour itself (the page really updates, waits while someone is busy, keeps the page number) is checked in a real browser; these tests hold the
parts a browser run cannot keep honest for ever:
  * the scripts keep the safety rules written down (never swap a part that holds a <script>, wait for pop-ups / typing, stop quietly when signed out ...),
  * every page that promises live parts really marks them, loads the script, and draws each part THE SAME WAY every time -- if a part ever held
    something that differs from one render to the next (a form token, a random id), the page would swap it every minute for nothing."""
import pathlib
import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomerMessage, UserProfile
from reservations.models import Reservation, ReservationItem

User = get_user_model()
BASE = pathlib.Path(settings.BASE_DIR)


def read(relative):
    return (BASE / relative).read_text(encoding="utf-8")


def live_regions(html):
    """{name: inner html} of every element marked data-live-region (the same finding the script does, on the server's page)."""
    found = {}
    for start in re.finditer(r'<(\w+)\b[^>]*\bdata-live-region="([^"]+)"[^>]*>', html):
        tag, name = start.group(1), start.group(2)
        depth = 1
        for token in re.finditer(r"<(/?)%s\b[^>]*?(/?)>" % tag, html[start.end():]):
            depth += -1 if token.group(1) else (0 if token.group(2) else 1)
            if depth == 0:
                found[name] = html[start.end():start.end() + token.start()]
                break
    return found


def squeeze(text):
    return re.sub(r"\s+", " ", text).strip()


class TheScriptsKeepTheirSafetyRulesTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.live = read("static/arabela_admin/live-regions.js")
        cls.bell = read("static/arabela_admin/notifications-live.js")
        cls.badges = read("static/js/customer-badges-live.js")
        cls.pager = read("static/arabela_admin/pager.js")

    def test_a_part_that_holds_a_script_is_never_swapped(self):
        self.assertIn("/<script[\\s>]/i.test(content)", self.live)

    def test_it_waits_while_a_popup_is_open_someone_is_typing_or_working_in_the_part(self):
        for needle in ("adm-dialog", ".fixed.inset-0", "KEY_ACTIVE_MS", "POINTER_ACTIVE_MS", "CLICK_ACTIVE_MS", "NAV_ACTIVE_MS", "data-live-hold", "getSelection"):
            self.assertIn(needle, self.live, needle)

    def test_it_never_asks_the_server_more_often_than_every_ten_seconds_and_never_in_the_background(self):
        self.assertIn("MIN_GAP_MS = 10000", self.live)
        self.assertIn("if (document.hidden) { return; }", self.live)

    def test_a_signed_out_session_stops_it_quietly(self):
        self.assertIn("res.status === 401 || res.status === 403", self.live)
        self.assertIn("res.redirected", self.live)

    def test_a_failing_server_is_counted_and_never_logged_as_an_error(self):
        self.assertIn("FAILURES_FOR_BAR = 3", self.live)
        self.assertNotIn("report('the background check failed'", self.live)

    def test_what_changed_is_compared_with_what_the_server_sent(self):
        self.assertIn("ArabelaLiveBaseline", self.live)
        self.assertIn("name=\"csrfmiddlewaretoken\"", self.live)

    def test_the_pager_keeps_the_page_the_person_is_on(self):
        self.assertIn("keepPage", self.pager)
        self.assertIn("&& !keepPage", self.pager)
        self.assertIn("window.arabelaPager.keepPage(run)", self.live)

    def test_the_admin_bell_and_the_customer_badges_tell_the_page_when_something_changed(self):
        self.assertIn("'arabela-feed-changed'", self.bell)
        self.assertIn("'arabela-feed-changed'", self.badges)
        self.assertIn("items.length !== known.size", self.bell)               # a changed set of notifications, not "5 min ago" ticking over

    def test_the_bell_keeps_the_rules_the_back_button_tests_hold(self):
        self.assertIn("res.status === 401 || res.status === 403", self.bell)
        self.assertIn("if (cameBackFromHistory()) { verifySignIn(); }", self.bell)

    def test_the_asset_version_stamp_covers_the_new_script(self):
        self.assertIn('"live-regions.js"', read("arabela_admin/context_processors.py"))

    def test_the_script_is_loaded_by_the_bell_partial_before_alpine_and_by_the_customer_pages(self):
        self.assertIn("arabela_admin/live-regions.js", read("templates/arabela_admin/partials/notifications.html"))
        for page in ("reservations", "messages", "reservation_item_detail"):
            self.assertIn("arabela_admin/live-regions.js", read(f"templates/{page}.html"), page)


ADMIN_PAGES = {
    "dashboard": ["overview-alerts", "overview-top", "overview-bookings", "quick-verify"],
    "pending_approval": ["pending-list"],
    "gown_monitoring": ["monitoring-tabs", "monitoring-list"],
    "active_reservations": ["active-list", "active-filters"],
    "payment_verification": ["pv-list", "pv-verified-today"],
    "security_deposits": ["sd-list", "sd-filters", "sd-month"],
    "clients": ["cl-rows", "cl-filters"],
    "rental_history": ["history-summary", "history-list"],
}


class TheAdminPagesDrawTheirLivePartsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="live_owner", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.owner, role=UserProfile.Role.OWNER)
        cls.customer = User.objects.create_user(username="live_customer", password="x", first_name="Live", last_name="Customer")
        long_ago = timezone.now() - timedelta(hours=3)        # so "waited N" text cannot change between two renders made seconds apart
        today = timezone.localdate()
        for name, status, reviewed in (("Live Pending", Reservation.Status.PENDING, None), ("Live Confirmed", Reservation.Status.CONFIRMED, timezone.now())):
            reservation = Reservation.objects.create(
                customer=cls.customer, customer_name=name, phone="09171234567", payment_proof_url="https://example.test/p.jpg",
                rental_subtotal=1500, total_amount=3500, status=status, reviewed_at=reviewed,
            )
            Reservation.objects.filter(pk=reservation.pk).update(created_at=long_ago)
            ReservationItem.objects.create(
                reservation=reservation, gown_name="Live Gown", rental_date=today + timedelta(days=3),
                event_date=today + timedelta(days=5), return_date=today + timedelta(days=7), rental_price=1500,
            )

    def setUp(self):
        self.client.force_login(self.owner)

    def page(self, name):
        response = self.client.get(reverse(f"arabela_admin:{name}"))
        self.assertEqual(response.status_code, 200, name)
        return response.content.decode()

    def test_every_page_marks_its_live_parts_and_loads_the_script(self):
        for name, wanted in ADMIN_PAGES.items():
            html = self.page(name)
            found = live_regions(html)
            for region in wanted:
                self.assertIn(region, found, f"{name}: {region}")
            self.assertIn("arabela_admin/live-regions.js", html, name)

    def test_no_live_part_holds_a_script_or_a_form_token(self):
        for name in ADMIN_PAGES:
            for region, inner in live_regions(self.page(name)).items():
                self.assertNotRegex(inner, r"<script[\s>]", f"{name}: {region}")
                self.assertNotIn("csrfmiddlewaretoken", inner, f"{name}: {region}")

    def test_a_live_part_is_drawn_the_same_way_every_time(self):
        for name in ADMIN_PAGES:
            first, second = live_regions(self.page(name)), live_regions(self.page(name))
            self.assertEqual(set(first), set(second), name)
            for region in first:
                self.assertEqual(squeeze(first[region]), squeeze(second[region]), f"{name}: {region} differs between two renders")

    def test_the_live_script_loads_before_the_pages_own_deferred_scripts(self):
        """It must see the page as the server drew it, so it has to come BEFORE bundle.js (Alpine) and pager.js in the document."""
        for name in ADMIN_PAGES:
            html = self.page(name)
            live = html.index("arabela_admin/live-regions.js")
            self.assertLess(live, html.index("arabela_admin/bundle.js"), name)
            if "arabela_admin/pager.js" in html:
                self.assertLess(live, html.index("arabela_admin/pager.js"), name)

    def test_reservation_records_hands_its_fresh_json_to_its_own_function(self):
        html = self.page("reservation_records")
        marker = re.search(r'<span hidden data-live-data="records"[^>]*>', html)
        self.assertIsNotNone(marker)
        self.assertIn('data-live-source="#reservation-records-data"', marker.group(0))
        self.assertIn('id="reservation-records-data"', html)
        self.assertIn("window.ArabelaLiveData.records = function", html)
        self.assertIn("state.records = records", html)
        first = re.search(r'<script id="reservation-records-data"[^>]*>(.*?)</script>', html, re.S).group(1)
        second = re.search(r'<script id="reservation-records-data"[^>]*>(.*?)</script>', self.page("reservation_records"), re.S).group(1)
        self.assertEqual(first, second)                    # the same JSON every time, so a quiet page is never "changed"

    def test_rental_history_hands_its_fresh_counts_to_its_own_function(self):
        html = self.page("rental_history")
        marker = re.search(r'<span hidden data-live-data="history-records"[^>]*>', html)
        self.assertIsNotNone(marker)
        self.assertIn('data-live-source="#rental-records-data"', marker.group(0))
        self.assertIn("window.ArabelaLiveData['history-records'] = function", html)
        self.assertIn("state.rentalRecords = JSON.parse(text);", html)
        first = re.search(r'<script id="rental-records-data"[^>]*>(.*?)</script>', html, re.S).group(1)
        second = re.search(r'<script id="rental-records-data"[^>]*>(.*?)</script>', self.page("rental_history"), re.S).group(1)
        self.assertEqual(first, second)

    def test_the_markers_change_no_layout(self):
        """Only attributes were added (plus display: contents wrappers where a section can start empty): no new classes, no new boxes."""
        html = self.page("payment_verification")
        wrapper = re.search(r'<div data-live-region="pv-verified-today"[^>]*>', html).group(0)
        self.assertIn("display: contents", wrapper)


class TheCustomerPagesDrawTheirLivePartsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="live_cust_page", password="x", first_name="Live", last_name="Page")
        UserProfile.objects.create(user=cls.customer)
        today = timezone.localdate()
        cls.reservation = Reservation.objects.create(
            customer=cls.customer, customer_name="Live Page", phone="09171234567", rental_subtotal=1500, total_amount=3500,
            status=Reservation.Status.CONFIRMED,
        )
        cls.item = ReservationItem.objects.create(
            reservation=cls.reservation, gown_name="Live Page Gown", rental_date=today + timedelta(days=3),
            event_date=today + timedelta(days=5), return_date=today + timedelta(days=7), rental_price=1500,
        )
        # Read already: opening the Messages page marks unread ones read, so a second render would not show the "New" tag.
        CustomerMessage.objects.create(recipient=cls.customer, category=CustomerMessage.Category.GENERAL, body="Hello from the shop", is_read=True)

    def setUp(self):
        self.client.force_login(self.customer)

    def render(self, name, *args):
        response = self.client.get(reverse(f"gowns:{name}", args=args))
        self.assertEqual(response.status_code, 200, name)
        return response.content.decode()

    def pages(self):
        return {
            "orders": (("orders",), ["res-list"]),
            "messages": (("messages",), ["messages-list", "messages-pager"]),
            "reservation_item_detail": (("reservation_item_detail", self.item.id), ["item-status", "item-timeline"]),
        }

    def test_every_customer_page_marks_its_live_parts_and_loads_the_script(self):
        for name, (args, wanted) in self.pages().items():
            html = self.render(*args)
            found = live_regions(html)
            for region in wanted:
                self.assertIn(region, found, f"{name}: {region}")
            self.assertIn("arabela_admin/live-regions.js", html, name)

    def test_no_customer_part_holds_a_script_and_each_is_drawn_the_same_way_every_time(self):
        for name, (args, wanted) in self.pages().items():
            first, second = live_regions(self.render(*args)), live_regions(self.render(*args))
            for region in wanted:
                self.assertNotRegex(first[region], r"<script[\s>]", f"{name}: {region}")
                self.assertEqual(squeeze(first[region]), squeeze(second[region]), f"{name}: {region} differs between two renders")

    def test_the_reservations_page_says_what_the_server_drew_before_its_own_script_changes_it(self):
        html = self.render("orders")
        self.assertIn("window.ArabelaLiveBaseline = { 'res-list'", html)
        self.assertLess(html.index("window.ArabelaLiveBaseline"), html.index("initReservationFilters"))   # set BEFORE the filter script edits the cards
        self.assertIn("arabela-region-updated", html)                                                       # ...and the filters are redone after an update

    def test_the_messages_page_waits_while_a_delete_can_still_be_undone_and_redoes_its_empty_state(self):
        html = self.render("messages")
        list_tag = re.search(r'<div[^>]*id="messages-list"[^>]*>', html).group(0)
        self.assertIn('data-live-hold="#messages-undo-bar.is-visible"', list_tag)
        self.assertIn("refreshEmpty();", html)
        self.assertIn("arabela-region-updated", html)

    def test_the_checkout_and_cart_pages_are_not_touched(self):
        """The cart and the checkout hold a 20-minute hold and unsent form entries: they must never be updated in the background."""
        for page in ("reservation", "selection", "confirmation"):
            source = read(f"templates/{page}.html")
            self.assertNotIn("data-live-region", source, page)
            self.assertNotIn("live-regions.js", source, page)
