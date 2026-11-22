"""The gown photo window: click a gown's NAME on Active Reservations, Reservation Records or Pending Approval and see its picture.

The behaviour (the right picture and name open, it closes three ways, a click on a name never also toggles the booking row, it opens above
Reservation Records' View window and closing it leaves that window open, phone width, dark mode) is checked in a real browser. These tests hold what
a browser run cannot keep honest for ever: which picture and line a booking line gets, that the three pages carry the data and the window and the
other pages do not, that the names are escaped, that the window stays view-only, and that the Gown Catalog's own photo window is untouched."""
import json
import pathlib
import re
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from arabela_admin import gown_photo
from gowns.models import Gown
from reservations.models import Reservation, ReservationItem

User = get_user_model()
BASE = pathlib.Path(settings.BASE_DIR)


def read(relative):
    return (BASE / relative).read_text(encoding="utf-8")


def line(gown=None, snapshot="", size="", gown_id=7):
    """A booking line, without the database: `gown` is (current photo, gown code, gown size) or None for a line whose gown was deleted."""
    catalog = SimpleNamespace(photo_url=gown[0], gown_id=gown[1], size=gown[2]) if gown else None
    return SimpleNamespace(gown_id=gown_id if gown else None, gown=catalog, gown_photo_url=snapshot, size=size)


class WhichPictureAndWhichLineTests(SimpleTestCase):
    def test_the_gowns_current_catalog_photo_comes_first(self):
        self.assertEqual(gown_photo.photo_url(line(("https://cdn.test/new.jpg", "WH-017", "Large"), snapshot="https://cdn.test/old.jpg")), "https://cdn.test/new.jpg")

    def test_without_a_catalog_photo_the_picture_saved_at_booking_time_is_used(self):
        self.assertEqual(gown_photo.photo_url(line(("", "WH-017", "Large"), snapshot="https://cdn.test/old.jpg")), "https://cdn.test/old.jpg")

    def test_a_deleted_gown_keeps_the_picture_saved_on_its_booking_line(self):
        self.assertEqual(gown_photo.photo_url(line(None, snapshot="https://cdn.test/old.jpg")), "https://cdn.test/old.jpg")

    def test_no_photo_anywhere_is_an_empty_string(self):
        self.assertEqual(gown_photo.photo_url(line(("", "WH-017", "Large"))), "")
        self.assertEqual(gown_photo.photo_url(line(None)), "")

    def test_spaces_around_an_address_are_dropped(self):
        self.assertEqual(gown_photo.photo_url(line(("  https://cdn.test/a.jpg  ", "WH-017", "Large"))), "https://cdn.test/a.jpg")

    def test_the_line_under_the_name_is_tag_code_and_size(self):
        self.assertEqual(gown_photo.subtitle(line(("", "WH-017", "Large"), size="Large"), "White"), "White tag · WH-017 · Large")

    def test_the_booked_size_wins_over_the_gowns_own(self):
        self.assertEqual(gown_photo.subtitle(line(("", "WH-017", "Large"), size="Medium"), "White"), "White tag · WH-017 · Medium")

    def test_an_unknown_booked_size_falls_back_to_the_gowns_own(self):
        for unknown in ("", "-", "TBD"):
            self.assertEqual(gown_photo.subtitle(line(("", "WH-017", "Large"), size=unknown), "White"), "White tag · WH-017 · Large", unknown)

    def test_a_deleted_gown_has_no_code_and_no_tag_just_the_booked_size(self):
        self.assertEqual(gown_photo.subtitle(line(None, size="Large"), ""), "Large")

    def test_nothing_known_is_an_empty_line_not_a_stray_separator(self):
        self.assertEqual(gown_photo.subtitle(line(None, size="-"), ""), "")
        self.assertEqual(gown_photo.subtitle(line(("", "", ""), size=""), ""), "")

    def test_it_never_touches_the_database(self):
        """The pages already load each line's gown, so reading the photo and the line must cost no query: the module has no way to ask one."""
        source = read("arabela_admin/gown_photo.py")
        for query in ("import", ".objects", ".filter(", ".get(", "select_related", "prefetch_related", "connection"):
            self.assertNotIn(query, source, query)


class TheWindowStaysViewOnlyAndSafeTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partial = read("templates/arabela_admin/partials/gown_photo_window.html")
        cls.script = re.search(r"<script>(.*?)</script>", cls.partial, re.S).group(1)
        cls.catalog = read("templates/arabela_admin/gown-catalog.html")

    def test_it_has_no_replace_or_view_full_image_and_no_gown_photo_heading(self):
        markup = self.partial.split("{% endcomment %}", 1)[1]            # what the page gets, without the explanation above it
        for gone in ("View Full Image", "Replace Photo", "updateGownPhoto", ">Gown Photo<", "type=\"file\""):
            self.assertNotIn(gone, markup, gone)

    def test_the_gown_catalogs_own_photo_window_is_untouched(self):
        for kept in ("View Full Image", "Replace Photo", "updateGownPhoto", "Gown Photo</h4>"):
            self.assertIn(kept, self.catalog, kept)
        self.assertNotIn("gown_photo_window.html", self.catalog)
        self.assertNotIn("data-gpw-", self.catalog)

    def test_the_picture_address_is_only_used_when_it_is_a_web_address_or_a_path_on_this_site(self):
        self.assertIn(r"/^https?:\/\//i.test(url)", self.script)
        self.assertIn("url.charAt(0) === '/' && url.charAt(1) !== '/'", self.script)     # a path on this site, never a //host address
        body = self.script.split("function safeUrl", 1)[1].split("function isOpen", 1)[0]
        self.assertRegex(body.rstrip(), r"return '';\s*\}$")             # anything else (javascript:, data:, //host) comes out as "no photo"

    def test_the_name_and_the_line_under_it_are_plain_text_never_html(self):
        self.assertIn("titleEl.textContent = name", self.script)
        self.assertIn("subEl.textContent = sub", self.script)
        self.assertNotIn("innerHTML", self.script)

    def test_a_click_on_a_name_never_also_reaches_the_row_or_a_window_under_it(self):
        self.assertIn("}, true);", self.script)                                          # capture phase
        self.assertGreaterEqual(self.script.count("stopImmediatePropagation"), 4)
        self.assertIn("event.key === 'Escape'", self.script)

    def test_it_sits_above_the_other_admin_windows_and_says_it_is_open_for_the_live_updates(self):
        self.assertIn("z-index: 100000", self.partial)                                   # the other admin windows are 99999
        self.assertIn("data-overlay-root", self.partial)
        self.assertIn('role="dialog"', self.partial)
        self.assertIn("[data-overlay-root][aria-hidden=\"false\"]", read("static/arabela_admin/live-regions.js"))

    def test_a_missing_or_broken_picture_says_so(self):
        self.assertIn("No photo available for this gown", self.partial)
        self.assertIn("Photo could not load", self.partial)


class ThePagesCarryTheNamesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="gpw_owner", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.owner, role=UserProfile.Role.OWNER)
        cls.customer = User.objects.create_user(username="gpw_customer", password="x", first_name="Gpw", last_name="Customer")
        today = timezone.localdate()

        def gown(n, name, photo):
            return Gown.objects.create(gown_id=f"GPWT-{n:04d}", name=name, slug=f"gpwt-{n}", category=Gown.Category.GUEST_GOWN, color_name="White", color_code="WH",
                                       size=Gown.Size.LARGE, rental_price=Decimal("1500.00"), photo_url=photo)

        cls.with_photo = gown(1, "Gpwt Photo Gown", "https://cdn.test/photo.jpg")
        cls.no_catalog_photo = gown(2, "Gpwt Snapshot Gown", "")
        cls.evil = 'Evil "name" <b>x</b>'

        def booking(name, status, lines):
            r = Reservation.objects.create(customer=cls.customer, customer_name=name, phone="09171234567", rental_subtotal=1500, total_amount=3500, status=status,
                                           payment_proof_url="https://example.test/p.jpg", reviewed_at=timezone.now() if status != "Pending" else None)
            for i, (g, item_name, snapshot) in enumerate(lines):
                ReservationItem.objects.create(reservation=r, gown=g, gown_name=item_name, gown_photo_url=snapshot, size="Large", rental_price=1500,
                                               rental_date=today + timedelta(days=3 + i), event_date=today + timedelta(days=5 + i), return_date=today + timedelta(days=7 + i))
            return r

        cls.active = booking("Gpwt Active", Reservation.Status.CONFIRMED, [
            (cls.with_photo, cls.with_photo.name, ""),
            (cls.no_catalog_photo, cls.no_catalog_photo.name, "https://cdn.test/saved.jpg"),
            (None, "Gpwt Deleted Gown", "https://cdn.test/deleted.jpg"),
            (None, cls.evil, ""),
        ])
        cls.pending_one = booking("Gpwt Pending One", Reservation.Status.PENDING, [(cls.with_photo, cls.with_photo.name, "")])
        cls.pending_many = booking("Gpwt Pending Many", Reservation.Status.PENDING, [(cls.with_photo, cls.with_photo.name, ""), (cls.no_catalog_photo, cls.no_catalog_photo.name, "")])

    def setUp(self):
        self.client.force_login(self.owner)

    def page(self, name):
        response = self.client.get(reverse(f"arabela_admin:{name}"))
        self.assertEqual(response.status_code, 200, name)
        return response.content.decode()

    def names(self, html):
        """{gown name: (sub, photo)} of every element that opens the window."""
        found = {}
        for tag in re.findall(r"<[^>]*data-gpw-name=[^>]*>", html):
            def attr(name):
                match = re.search(r'%s="([^"]*)"' % name, tag)
                return match.group(1) if match else None
            found[attr("data-gpw-name")] = (attr("data-gpw-sub"), attr("data-gpw-photo"))
        return found

    def test_active_reservations_names_open_the_right_picture_and_line(self):
        html = self.page("active_reservations")
        names = self.names(html)
        self.assertEqual(names["Gpwt Photo Gown"][1], "https://cdn.test/photo.jpg")                       # the catalog photo
        self.assertEqual(names["Gpwt Snapshot Gown"][1], "https://cdn.test/saved.jpg")                    # the picture saved at booking time
        self.assertEqual(names["Gpwt Deleted Gown"][1], "https://cdn.test/deleted.jpg")                   # the gown is gone: the saved picture
        self.assertIn("GPWT-0001", names["Gpwt Photo Gown"][0])
        self.assertTrue(names["Gpwt Photo Gown"][0].endswith("Large"))
        self.assertNotIn("GPWT", names["Gpwt Deleted Gown"][0])                                           # no code for a gown that is no longer in the catalog
        self.assertIn('role="button" tabindex="0" data-gpw-name="Gpwt Photo Gown"', html)
        self.assertIn('title="Click to see the photo"', html)
        self.assertIn('id="gpw"', html)                                                                   # the window itself is on the page, once
        self.assertEqual(html.count('id="gpw"'), 1)

    def test_a_gown_name_with_quotes_and_tags_cannot_break_out_of_the_attribute(self):
        html = self.page("active_reservations")
        self.assertIn('data-gpw-name="Evil &quot;name&quot; &lt;b&gt;x&lt;/b&gt;"', html)
        self.assertNotIn('data-gpw-name="Evil "name"', html)
        self.assertNotIn("<b>x</b>", html)

    def test_pending_approval_names_are_clickable_where_there_is_one_gown_and_inside_an_opened_booking(self):
        html = self.page("pending_approval")
        self.assertIn('id="gpw"', html)
        closed = re.findall(r'<div class="pa-g1"><span role="button" tabindex="0" data-gpw-name="([^"]*)"', html)
        self.assertEqual(closed, ["Gpwt Photo Gown"])                                                     # only the one-gown booking has a name on its closed line
        opened = re.findall(r'<span class="ar-gtitle" role="button" tabindex="0" data-gpw-name="([^"]*)"', html)
        self.assertEqual(sorted(opened), ["Gpwt Photo Gown", "Gpwt Photo Gown", "Gpwt Snapshot Gown"])    # every gown line of every booking, once opened

    def test_reservation_records_hands_the_photo_and_the_line_to_its_rows_and_its_view_window(self):
        html = self.page("reservation_records")
        data = json.loads(re.search(r'<script id="reservation-records-data"[^>]*>(.*?)</script>', html, re.S).group(1))
        record = next(r for r in data if r["customer"] == "Gpwt Active")
        by_name = {i["gown"]: i for i in record["items"]}
        self.assertEqual(by_name["Gpwt Photo Gown"]["photo"], "https://cdn.test/photo.jpg")
        self.assertEqual(by_name["Gpwt Snapshot Gown"]["photo"], "https://cdn.test/saved.jpg")
        self.assertEqual(by_name["Gpwt Deleted Gown"]["photo"], "https://cdn.test/deleted.jpg")
        self.assertIn("GPWT-0001", by_name["Gpwt Photo Gown"]["photoSub"])
        self.assertEqual(html.count(':data-gpw-name="item.gown"'), 2)                                    # the list row and the View window
        self.assertIn('id="gpw"', html)

    def test_the_other_admin_pages_do_not_carry_the_window(self):
        for name in ("dashboard", "payment_verification", "security_deposits", "clients"):
            self.assertNotIn('id="gpw"', self.page(name), name)

    def test_the_gown_catalog_page_is_unchanged(self):
        html = self.page("gown_catalog")
        self.assertNotIn('id="gpw"', html)
        self.assertNotIn("data-gpw-", html)
        self.assertIn("View Full Image", html)
