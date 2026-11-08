"""Gown Catalog layout (inventory): slim stat strip, category dropdown, Manage menu, ONE list card, status circles + legend, the physical-check text
behind an (i), the Gown Photo window without its second Close button, and a thousands comma on every price in the admin.

Mostly instant file checks; one test renders the real page for a staff user. The page's data (category_rows, check_label, ...) is unchanged, so the
older catalog tests keep covering the logic."""
import re
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from gowns.models import Gown

User = get_user_model()
ROOT = Path(settings.BASE_DIR)
ADMIN = ROOT / "templates" / "arabela_admin"


def catalog_html():
    return (ADMIN / "gown-catalog.html").read_text(encoding="utf8").replace("\r\n", "\n")


class LayoutFilesTests(SimpleTestCase):
    def test_the_four_big_tiles_are_one_slim_strip_with_the_same_dots_as_the_list(self):
        html = catalog_html()
        self.assertIn('class="gc-stats"', html)
        self.assertNotIn("admin-stat-grid-4", html)
        strip = html[html.index('class="gc-stats"'):html.index("Blocked Gowns list")]
        for label in ("Total Gowns", "Available", "Reserved", "Blocked Gowns"):
            self.assertIn(label, strip)
        self.assertIn("showBlockedGownsList = !showBlockedGownsList", strip)        # View & release still works

    def test_the_chips_and_the_old_category_select_are_gone_and_one_dropdown_drives_the_same_filter(self):
        html = catalog_html()
        self.assertNotIn("more categor", html)
        self.assertNotIn('<option value="All">All Categories</option>', html)
        menu = html[html.index('aria-label="Filter by category"'):html.index('aria-label="Filter by status"')]
        self.assertIn("All categories", menu)
        self.assertIn("{% for row in category_rows_populated %}", menu)
        self.assertIn("{% for row in category_rows_empty %}", menu)
        self.assertEqual(menu.count("category = '"), 3)                       # All / a stocked category / a not-yet-stocked one: each sets the page's own `category`
        self.assertIn("onGownFilterChanged()", menu)

    def test_manage_opens_the_same_three_panels_and_add_category_is_still_owner_only(self):
        html = catalog_html()
        manage = html[html.index('class="gc-menu gc-btn"'):html.index('<div class="gc-legend">')]
        for needle in ("showTagColors = !showTagColors", "showRemovalLog = !showRemovalLog", "{% if can_edit_tag_colors %}", "showAddCategory = !showAddCategory"):
            with self.subTest(needle=needle):
                self.assertIn(needle, manage)
        self.assertLess(manage.index("{% if can_edit_tag_colors %}"), manage.index("showAddCategory = !showAddCategory"))
        for panel in ("x-show=\"showTagColors\"", "x-show=\"showRemovalLog\"", "x-show=\"showAddCategory\""):
            self.assertIn(panel, html)                     # the panels themselves are still on the page

    def test_search_and_list_are_one_card_with_a_legend_and_one_count(self):
        html = catalog_html()
        self.assertEqual(html.count('class="gc-card '), 1)
        card = html[html.index('class="gc-card '):html.index("<!-- Add Gown Modal")]
        self.assertIn('class="gc-toolbar"', card)
        self.assertIn('class="gc-legend"', card)
        self.assertIn('data-arpg="tbody" data-arpg-noun="gowns" data-arpg-top="off" data-arpg-count="#gc-count"', card)
        self.assertEqual(card.count('id="gc-count"'), 1)
        self.assertLess(card.index('class="gc-toolbar"'), card.index('class="gc-legend"'))
        self.assertLess(card.index('class="gc-legend"'), card.index('data-arpg="tbody"'))
        for name in ("Available", "Reserved", "Out-of-Stock"):
            self.assertIn(f"</span>{name}</li>", card)

    def test_the_long_physical_check_sentence_is_behind_an_info_button(self):
        html = catalog_html()
        self.assertNotIn('<p class="mt-3 text-xs text-gray-500 dark:text-gray-400">\n                <span class="font-medium text-gray-700 dark:text-gray-300">Physical check:</span>', html)
        info = html[html.index('aria-label="About the physical check"'):html.index('class="gc-btn"')]
        self.assertIn("{{ checked_recent_count }} of {{ total_gowns_count }} gowns confirmed present", info)
        self.assertIn("Mark Checked", info)

    def test_the_status_is_a_circle_with_a_name_and_the_check_line_is_no_longer_orange(self):
        html = catalog_html()
        self.assertIn('gc-dot gc-dot--row', html)
        self.assertIn('role="img" aria-label="{{ gown.status }}" title="{{ gown.status }}"', html)
        self.assertNotIn("{{ gown.status }}</p>", html)                                  # the old green / amber / red word badge
        self.assertNotIn("text-warning-700 dark:text-warning-400{% else %}text-gray-400", html)

    def test_the_gown_photo_window_has_only_the_x_to_close_it(self):
        html = catalog_html()
        window = html[html.index("Replace Photo"):html.index("<!-- Gown View Modal")]
        self.assertNotIn(">Close</button>", window)
        before = html[html.index("isGownImageModal = true"):html.index("Replace Photo")]
        self.assertIn('@click="isGownImageModal = false"', html[html.index('x-show="isGownImageModal"'):html.index("Replace Photo")])
        self.assertTrue(before)

    def test_the_big_alpine_attribute_still_has_no_stray_double_quote(self):
        """The page's whole JavaScript lives in ONE double-quoted x-data attribute: a double quote inside it cuts it short and breaks the page."""
        html = catalog_html()
        start = html.index('x-data="{ page: \'gown\'') + len('x-data="')
        attribute = html[start:html.index('"', start)]
        for needle in ("matchingGownCount()", "onGownFilterChanged()", "sortGowns(key)", "pageGownIds()", "toggleSelectAllVisible(checked)"):
            self.assertIn(needle, attribute)


class PricesHaveACommaTests(SimpleTestCase):
    def test_no_admin_page_prints_an_amount_without_the_thousands_comma(self):
        offenders = []
        for path in sorted(ADMIN.glob("*.html")):
            text = path.read_text(encoding="utf8")
            for number, line in enumerate(text.split("\n"), 1):
                if re.search(r"floatformat:\s*[\"']?0[\"']?\s*\}\}", line):
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [])

    def test_the_catalog_view_window_formats_the_price_in_the_browser(self):
        html = catalog_html()
        self.assertIn("Number(viewingGown.price).toLocaleString('en-PH', { maximumFractionDigits: 2 })", html)
        self.assertNotIn("'₱ ' + viewingGown.price", html)


class PagerFilesTests(SimpleTestCase):
    def test_the_page_number_is_written_once_and_the_count_can_live_elsewhere(self):
        js = (ROOT / "static" / "arabela_admin" / "pager.js").read_text(encoding="utf8").replace("\r\n", "\n")
        self.assertNotIn("topPos", js)                                      # no second "Page 2 / 46" in the top strip
        self.assertEqual(js.count('"Page " + page + " / " + pages'), 1)
        self.assertIn('box.getAttribute("data-arpg-top") !== "off"', js)
        self.assertIn("function setCount(", js)
        self.assertIn('box.getAttribute("data-arpg-count")', js)


class CatalogPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="layout_staff", password="x", is_staff=True)
        cls.owner = User.objects.create_superuser(username="layout_owner", password="x", email="layout@example.com")
        for i, (status, price) in enumerate(((Gown.Status.AVAILABLE, "1500"), (Gown.Status.RESERVED, "12500.50"), (Gown.Status.OUT_OF_STOCK, "999"))):
            Gown.objects.create(gown_id=f"LAYOUT-{i + 1:03d}", name=f"Layout Gown {i + 1}", category=Gown.Category.GUEST_GOWN, color_name="Red", color_code="RD",
                                size=Gown.Size.MEDIUM, rental_price=Decimal(price), status=status)

    def page(self, who):
        self.client.force_login(who)
        return self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()

    def test_each_row_has_a_circle_in_its_status_colour_and_a_comma_price(self):
        html = self.page(self.staff)
        self.assertEqual(html.count("gc-dot gc-dot--row"), 3)
        for word, circle in (("Available", "gc-dot--available"), ("Reserved", "gc-dot--reserved"), ("Out-of-Stock", "gc-dot--oos")):
            with self.subTest(status=word):
                self.assertRegex(html, r'gc-dot gc-dot--row %s" role="img" aria-label="%s"' % (circle, re.escape(word)))
        rows = re.findall(r'<p class="text-sm font-semibold text-brand-500">&#8369;([^<]*)</p>', html)
        self.assertEqual(sorted(rows), sorted(["1,500", "12,500.50", "999"]))

    def test_the_page_shows_the_legend_the_strip_and_the_dropdown_with_counts(self):
        html = self.page(self.staff)
        self.assertIn('<ul class="gc-legend-list"', html)
        self.assertIn(">3 gowns</p>", html.replace("\n", " "))
        self.assertRegex(html, r'<button type="button" class="gc-opt" :class="category === \'Guest Gown\'[^>]*>')
        self.assertIn("All categories", html)

    def test_only_the_owner_gets_add_category_in_the_manage_menu(self):
        self.assertNotIn("+ Add Category</button>", self.page(self.staff))
        self.assertIn("+ Add Category</button>", self.page(self.owner))


class LaptopFitTests(SimpleTestCase):
    """Laptop screens (about 1024-1599px): found on a real 1280 / 1366 laptop that Reservation Records hid its Actions column off to the right,
    the Client List cut its View button off, and the booking number chip broke in two. Phones and big monitors are left alone."""

    def text(self, name):
        return (ADMIN / name).read_text(encoding="utf8").replace("\r\n", "\n")

    def test_reservation_records_fits_a_laptop(self):
        html = self.text("reservation-records.html")
        self.assertIn("@media (min-width: 1024px) and (max-width: 1599px) {", html)
        block = html[html.index("@media (min-width: 1024px) and (max-width: 1599px) {"):]
        block = block[:block.index("\n      }\n")]
        for rule in (".rr-stats { grid-template-columns: repeat(5, minmax(0, 1fr))", ".rr-filters--main > .rr-grow { flex: 1 1 100%; }",
                     ".rr-scroll-x th, .rr-scroll-x td { padding-left: .5rem", ".rr-scroll-x .rr-dates { white-space: normal;", ".rr-scroll-x .rr-actions { flex-wrap: wrap; }"):
            with self.subTest(rule=rule):
                self.assertIn(rule, block)
        self.assertEqual(html.count('class="rr-filters rr-filters--main"'), 1)             # only the main filter row, not the date rows
        self.assertEqual(html.count("rr-dates text-theme-sm"), 1)
        self.assertEqual(html.count('class="rr-actions flex items-center gap-2"'), 1)

    def test_client_list_fits_a_laptop(self):
        html = self.text("clients.html")
        self.assertIn(".cl-table th, .cl-table td { padding-left: .875rem; padding-right: .875rem; }", html)
        self.assertEqual(html.count('<div class="cl-table custom-scrollbar overflow-y-auto"'), 1)

    def test_the_booking_number_chip_never_breaks_across_two_lines(self):
        for name in ("pending-approval.html", "dashboard.html"):
            with self.subTest(page=name):
                self.assertRegex(self.text(name), r'text-gray-400" style="white-space: nowrap;">\{\{ r\.reference_code \}\}</span>')
