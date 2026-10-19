"""The Size Guide on the gown page (Suit first): the link shows only where a guide exists, and the panel is just the table with the shop's
exact numbers in the shop's own look -- every size looks the same, no extra text (no "not available", no how-to-measure, no footer), and
nothing that scrolls or slides."""
import re
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from gowns.models import Gown
from gowns.size_guides import (
    BARONG_GUIDE, DRESSES_GUIDE, FILIPINIANA_GUIDE, GUEST_GUIDE, MOTHER_GUIDE, SIZE_GUIDES, SUIT_GUIDE, WEDDING_GUIDE, size_guide_for,
)

GUIDE_JS = Path(settings.BASE_DIR) / "static" / "js" / "size-guide.js"

# The owner's chart, typed here a second time on purpose: if the data file is ever edited wrongly, this catches it.
SUIT_CHART = {
    "S": ["92–96 cm", '(36–38")', "44–45 cm", '(17.3–17.7")', "60–61 cm", '(23.5–24")', "76–80 cm", '(30–31.5")'],
    "M": ["97–101 cm", '(38–40")', "46–47 cm", '(18.1–18.5")', "62–63 cm", '(24.5–25")', "81–85 cm", '(32–33.5")'],
    "L": ["102–106 cm", '(40–42")', "48–49 cm", '(18.9–19.3")', "64–65 cm", '(25.5–26")', "86–90 cm", '(34–35.5")'],
    "XL": ["107–111 cm", '(42–44")', "50–51 cm", '(19.7–20.1")', "66–67 cm", '(26.5–27")', "91–95 cm", '(36–37.5")'],
    "XXL": ["112–116 cm", '(44–46")', "52–53 cm", '(20.5–20.9")', "68–69 cm", '(27.5–28")', "96–100 cm", '(38–39.5")'],
}


WEDDING_CHART = {
    "S": ["85–88 cm", '(33.5–34.5")', "66–69 cm", '(26–27")', "90–93 cm", '(35.5–36.5")'],
    "M": ["89–93 cm", '(35–36.5")', "70–74 cm", '(27.5–29")', "94–98 cm", '(37–38.5")'],
    "L": ["94–99 cm", '(37–39")', "75–80 cm", '(29.5–31.5")', "99–104 cm", '(39–41")'],
    "XL": ["100–105 cm", '(39.5–41.5")', "81–86 cm", '(32–34")', "105–110 cm", '(41.5–43.5")'],
    "XXL": ["106–112 cm", '(42–44")', "87–93 cm", '(34.5–36.5")', "111–116 cm", '(44–45.5")'],
}


BARONG_CHART = {
    "S": ["96–100 cm", '(38–39.5")', "43–44 cm", '(17–17.5")', "58–59 cm", '(23–23.5")', "71–73 cm", '(28–28.5")'],
    "M": ["101–105 cm", '(40–41.5")', "45–46 cm", '(17.5–18")', "60–61 cm", '(23.5–24")', "74–76 cm", '(29–29.5")'],
    "L": ["106–110 cm", '(42–43.5")', "47–48 cm", '(18.5–19")', "62–63 cm", '(24.5–25")', "77–79 cm", '(30–31")'],
    "XL": ["111–115 cm", '(44–45.5")', "49–50 cm", '(19.5–20")', "64–65 cm", '(25.5–26")', "80–82 cm", '(31.5–32")'],
    "XXL": ["116–120 cm", '(46–47.5")', "51–52 cm", '(20.5–21")', "66–67 cm", '(26.5–27")', "83–85 cm", '(32.5–33.5")'],
}

# The Mother Gown and Guest Gown charts stop at XL: four rows, no XXL.
MOTHER_CHART = {
    "S": ["84–88 cm", '(33–34.5")', "66–70 cm", '(26–27.5")', "90–94 cm", '(35.5–37")'],
    "M": ["89–93 cm", '(35–36.5")', "71–75 cm", '(28–29.5")', "95–99 cm", '(37.5–39")'],
    "L": ["94–100 cm", '(37–39.5")', "76–82 cm", '(30–32")', "100–106 cm", '(39.5–41.5")'],
    "XL": ["101–108 cm", '(40–42.5")', "83–90 cm", '(32.5–35.5")', "107–114 cm", '(42–45")'],
}
GUEST_CHART = {
    "S": ["82–86 cm", '(32–34")', "64–68 cm", '(25–27")', "88–92 cm", '(34.5–36")'],
    "M": ["87–91 cm", '(34.5–36")', "69–73 cm", '(27–29")', "93–97 cm", '(36.5–38")'],
    "L": ["92–98 cm", '(36.5–38.5")', "74–80 cm", '(29–31.5")', "98–104 cm", '(38.5–41")'],
    "XL": ["99–106 cm", '(39–42")', "81–88 cm", '(32–34.5")', "105–112 cm", '(41.5–44")'],
}

# category label -> (collection address, guide, chart, headings, units per row of the chart)
CATEGORY_CASES = {
    "Filipiniana": ("filipiniana", FILIPINIANA_GUIDE, WEDDING_CHART, ["Size", "Bust (cm / in)", "Waist (cm / in)", "Hips (cm / in)"]),
    "Dresses": ("dresses", DRESSES_GUIDE, WEDDING_CHART, ["Size", "Bust (cm / in)", "Waist (cm / in)", "Hips (cm / in)"]),
    "Barong": ("barong", BARONG_GUIDE, BARONG_CHART, ["Size", "Chest / Bust (cm / in)", "Shoulder Width (cm / in)", "Sleeve Length (cm / in)", "Length (cm / in)"]),
    "Mother Gown": ("mother-gown", MOTHER_GUIDE, MOTHER_CHART, ["Size", "Bust (cm / in)", "Waist (cm / in)", "Hips (cm / in)"]),
    "Guest Gown": ("guest-gown", GUEST_GUIDE, GUEST_CHART, ["Size", "Bust (cm / in)", "Waist (cm / in)", "Hips (cm / in)"]),
}


def make_unit(n, size, category=Gown.Category.SUIT, name="Armani Suit Test"):
    return Gown.objects.create(
        gown_id=f"SG-{n:04d}", name=name, category=category, color_name="Black", color_code="BK", size=size,
        rental_price=Decimal("1500"), status=Gown.Status.AVAILABLE)


class SizeGuideDataTests(SimpleTestCase):
    def test_the_suit_chart_is_exactly_the_shops_numbers(self):
        flat = {row["code"]: [part for pair in row["values"] for part in pair] for row in SUIT_GUIDE["rows"]}
        self.assertEqual(flat, SUIT_CHART)

    def test_the_columns_and_rows_line_up(self):
        self.assertEqual([row["code"] for row in SUIT_GUIDE["rows"]], ["S", "M", "L", "XL", "XXL"])
        for row in SUIT_GUIDE["rows"]:
            self.assertEqual(len(row["values"]), len(SUIT_GUIDE["columns"]))
        self.assertEqual(SUIT_GUIDE["columns"], ["Chest / Bust", "Shoulder Width", "Sleeve Length", "Waist"])

    def test_the_wedding_gown_chart_is_exactly_the_shops_numbers(self):
        flat = {row["code"]: [part for pair in row["values"] for part in pair] for row in WEDDING_GUIDE["rows"]}
        self.assertEqual(flat, WEDDING_CHART)
        self.assertEqual([row["code"] for row in WEDDING_GUIDE["rows"]], ["S", "M", "L", "XL", "XXL"])
        self.assertEqual(WEDDING_GUIDE["columns"], ["Bust", "Waist", "Hips"])
        for row in WEDDING_GUIDE["rows"]:
            self.assertEqual(len(row["values"]), 3)

    def test_the_barong_chart_is_exactly_the_shops_numbers(self):
        flat = {row["code"]: [part for pair in row["values"] for part in pair] for row in BARONG_GUIDE["rows"]}
        self.assertEqual(flat, BARONG_CHART)
        self.assertEqual(BARONG_GUIDE["columns"], ["Chest / Bust", "Shoulder Width", "Sleeve Length", "Length"])

    def test_filipiniana_and_dresses_carry_the_owners_bust_waist_hips_chart(self):
        for guide in (FILIPINIANA_GUIDE, DRESSES_GUIDE):
            flat = {row["code"]: [part for pair in row["values"] for part in pair] for row in guide["rows"]}
            self.assertEqual(flat, WEDDING_CHART)
            self.assertEqual(guide["columns"], ["Bust", "Waist", "Hips"])

    def test_each_category_owns_its_own_copy_of_a_shared_chart(self):
        # changing one category's numbers later must never change another's
        self.assertIsNot(FILIPINIANA_GUIDE["rows"], DRESSES_GUIDE["rows"])
        self.assertIsNot(FILIPINIANA_GUIDE["rows"], WEDDING_GUIDE["rows"])
        self.assertIsNot(FILIPINIANA_GUIDE["rows"][0], DRESSES_GUIDE["rows"][0])

    def test_the_mother_gown_and_guest_gown_charts_are_exactly_the_shops_numbers_and_stop_at_xl(self):
        for guide, chart in ((MOTHER_GUIDE, MOTHER_CHART), (GUEST_GUIDE, GUEST_CHART)):
            flat = {row["code"]: [part for pair in row["values"] for part in pair] for row in guide["rows"]}
            self.assertEqual(flat, chart)
            self.assertEqual([row["code"] for row in guide["rows"]], ["S", "M", "L", "XL"])
            self.assertEqual(guide["columns"], ["Bust", "Waist", "Hips"])

    def test_these_categories_have_a_guide_for_now(self):
        self.assertEqual(list(SIZE_GUIDES), ["Suit", "Wedding Gown", "Filipiniana", "Dresses", "Barong", "Mother Gown", "Guest Gown"])

    def test_other_categories_and_free_size_get_no_guide(self):
        every_size = [{"code": c, "offered": True} for c in ("S", "M", "L", "XL", "XXL")]
        self.assertIsNone(size_guide_for("Evening Gown", every_size))
        self.assertIsNone(size_guide_for("", every_size))
        self.assertIsNone(size_guide_for("Wedding Gown", [{"code": "FREE", "offered": True}]))
        self.assertIsNone(size_guide_for("Suit", [{"code": "FREE", "offered": True}, {"code": "S", "offered": False}]))
        self.assertIsNone(size_guide_for("Suit", []))

    def test_the_table_is_the_same_whichever_sizes_the_suit_has(self):
        only_m = [{"code": c, "offered": c == "M"} for c in ("S", "M", "L", "XL", "XXL")]
        every = [{"code": c, "offered": True} for c in ("S", "M", "L", "XL", "XXL")]
        self.assertEqual(size_guide_for("Suit", only_m), size_guide_for("Suit", every))
        self.assertEqual(size_guide_for("Suit", only_m)["rows"][1]["values"][0], {"cm": "97–101 cm", "inch": '(38–40")'})


class SizeGuidePageTests(TestCase):
    def page(self, unit, collection="suit"):
        return self.client.get(reverse("gowns:product_detail", kwargs={"collection": collection, "slug": unit.slug}))

    def panel(self, html):
        """The Size Guide panel only: from its root to a little past the end of its table."""
        start = html.index('id="size-guide-root"')
        return html[start:start + html[start:].index("</table>") + 300]

    def test_a_suit_page_has_the_link_and_the_panel(self):
        html = self.page(make_unit(1, Gown.Size.MEDIUM)).content.decode()
        self.assertIn('id="size-guide-open"', html)
        self.assertIn('id="size-guide-root"', html)
        self.assertIn('role="dialog"', html)
        self.assertIn('aria-modal="true"', html)
        self.assertIn("js/size-guide.js?v=", html)

    def test_the_table_carries_every_number_of_the_chart(self):
        panel = self.panel(self.page(make_unit(2, Gown.Size.LARGE)).content.decode())
        for code, parts in SUIT_CHART.items():
            row = re.search(r'<tr data-size-code="%s".*?</tr>' % code, panel, re.S).group(0)
            for part in parts:
                self.assertIn(part, row.replace("&quot;", '"'), (code, part))

    def test_the_headings_say_cm_and_in_like_the_shops_chart(self):
        panel = self.panel(self.page(make_unit(3, Gown.Size.LARGE)).content.decode())
        for heading in ("Size", "Chest / Bust (cm / in)", "Shoulder Width (cm / in)", "Sleeve Length (cm / in)", "Waist (cm / in)"):
            self.assertIn(">%s</th>" % heading, panel)

    def test_every_size_looks_the_same_whether_or_not_the_suit_has_it(self):
        panel = self.panel(self.page(make_unit(4, Gown.Size.MEDIUM)).content.decode())
        self.assertEqual(re.findall(r'<tr data-size-code="([A-Z]+)"', panel), ["S", "M", "L", "XL", "XXL"])
        for gone in ("not available", "Not available", "data-offered", "sg-chip", "sg-tag", "sg-legend", "Faded"):
            self.assertNotIn(gone, panel)

    def test_the_panel_is_only_the_table(self):
        panel = self.panel(self.page(make_unit(5, Gown.Size.EXTRA_LARGE)).content.decode())
        for gone in ("How to measure", "How To Measure", "Not sure which size", "Message us on Facebook", "Done</button>", "Swipe sideways",
                     "sg-tips", "sg-foot", "sg-note", "sg-hint"):
            self.assertNotIn(gone, panel)

    def test_an_xxl_suit_gets_the_guide_too(self):
        html = self.page(make_unit(6, Gown.Size.EXTRA_EXTRA_LARGE)).content.decode()
        self.assertIn('<tr data-size-code="XXL"', html)

    def test_a_suit_in_free_size_only_has_no_guide_and_no_dead_link(self):
        html = self.page(make_unit(7, Gown.Size.FREE_SIZE)).content.decode()
        self.assertNotIn("size-guide-open", html)
        self.assertNotIn("size-guide-root", html)
        self.assertNotIn("js/size-guide.js?v=", html)
        self.assertNotIn(">Size Guide<", html)

    def test_a_gown_of_another_category_has_no_size_guide_link(self):
        unit = make_unit(8, Gown.Size.MEDIUM, category=Gown.Category.EVENING_GOWN, name="Plain Gown")
        response = self.page(unit, collection="evening-gown")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn("size-guide-open", html)
        self.assertNotIn("size-guide-root", html)
        self.assertNotIn(">Size Guide<", html)

    def test_the_panel_wears_the_shops_own_look_and_never_shows_a_scrollbar(self):
        html = self.page(make_unit(9, Gown.Size.MEDIUM)).content.decode()
        css = html[html.index("/* ---- Size Guide"):html.index(".no-scrollbar::-webkit-scrollbar")]
        self.assertIn("background: #ffffff", css)               # white like the page, not a dark box
        self.assertIn('"Poppins"', css)                         # the site's font
        self.assertNotIn("#0f0f0f", css)
        self.assertIn("scrollbar-width: none", css)
        self.assertIn(".sg-panel::-webkit-scrollbar { display: none; }", css)
        self.assertNotIn("overflow-x", css)                     # an overflow-x on a wrapper is what made a scrollbar flash in
        self.assertNotIn("sg-bar", html)                        # no sliding highlight bar
        self.assertNotIn("transition-delay", css)               # no row-by-row sliding


class WeddingGownSizeGuideTests(TestCase):
    """Wedding gowns get the same panel with their own chart: Bust, Waist and Hips instead of a suit's four measurements."""

    def page(self, unit):
        response = self.client.get(reverse("gowns:product_detail", kwargs={"collection": "wedding", "slug": unit.slug}))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def panel(self, html):
        start = html.index('id="size-guide-root"')
        return html[start:start + html[start:].index("</table>") + 300]

    def wedding(self, n, size, name="Bridal Test Gown"):
        return make_unit(n, size, category=Gown.Category.WEDDING_GOWN, name=name)

    def test_a_wedding_gown_page_has_the_link_and_the_panel(self):
        html = self.page(self.wedding(1, Gown.Size.MEDIUM))
        self.assertIn('id="size-guide-open"', html)
        self.assertIn('id="size-guide-root"', html)
        self.assertIn('aria-label="Wedding gown size guide"', html)
        self.assertIn("js/size-guide.js?v=", html)

    def test_the_table_carries_every_number_of_the_wedding_chart(self):
        panel = self.panel(self.page(self.wedding(2, Gown.Size.MEDIUM)))
        for code, parts in WEDDING_CHART.items():
            row = re.search(r'<tr data-size-code="%s".*?</tr>' % code, panel, re.S).group(0)
            self.assertEqual(row.count("<td>"), 3, code)
            for part in parts:
                self.assertIn(part, row.replace("&quot;", '"'), (code, part))

    def test_the_headings_are_size_bust_waist_hips_with_cm_and_in(self):
        panel = self.panel(self.page(self.wedding(3, Gown.Size.MEDIUM)))
        headings = re.findall(r'<th scope="col">(.*?)</th>', panel)
        self.assertEqual(headings, ["Size", "Bust (cm / in)", "Waist (cm / in)", "Hips (cm / in)"])
        self.assertNotIn("Shoulder", panel)

    def test_every_size_looks_the_same_and_there_is_no_extra_text(self):
        panel = self.panel(self.page(self.wedding(4, Gown.Size.MEDIUM)))
        self.assertEqual(re.findall(r'<tr data-size-code="([A-Z]+)"', panel), ["S", "M", "L", "XL", "XXL"])
        for gone in ("not available", "Not available", "data-offered", "How to measure", "Not sure which size", "Done</button>", "sg-bar"):
            self.assertNotIn(gone, panel)

    def test_a_wedding_gown_in_free_size_only_has_no_guide(self):
        html = self.page(self.wedding(5, Gown.Size.FREE_SIZE))
        self.assertNotIn("size-guide-open", html)
        self.assertNotIn("size-guide-root", html)
        self.assertNotIn(">Size Guide<", html)

    def test_the_suit_chart_never_shows_on_a_wedding_gown_and_the_wedding_chart_never_on_a_suit(self):
        wedding = self.panel(self.page(self.wedding(6, Gown.Size.LARGE)))
        self.assertNotIn("Chest / Bust", wedding)
        self.assertNotIn("102–106 cm", wedding)
        suit = self.client.get(reverse("gowns:product_detail", kwargs={"collection": "suit", "slug": make_unit(7, Gown.Size.LARGE).slug})).content.decode()
        suit_panel = suit[suit.index('id="size-guide-root"'):]
        self.assertNotIn("Hips", suit_panel[:suit_panel.index("</table>")])
        self.assertNotIn("94–99 cm", suit_panel[:suit_panel.index("</table>")])


class OtherCategorySizeGuideTests(TestCase):
    """Filipiniana, Dresses and Barong: the same panel as the suit and the wedding gown, each with its own chart."""

    def open_page(self, label, n, size=Gown.Size.LARGE):
        collection = CATEGORY_CASES[label][0]
        unit = make_unit(n, size, category=label, name=f"{label} Test Gown")
        response = self.client.get(reverse("gowns:product_detail", kwargs={"collection": collection, "slug": unit.slug}))
        self.assertEqual(response.status_code, 200, label)
        return response.content.decode()

    def panel(self, html):
        start = html.index('id="size-guide-root"')
        return html[start:start + html[start:].index("</table>") + 300]

    def test_each_category_page_has_the_link_and_the_panel(self):
        for n, label in enumerate(CATEGORY_CASES, start=100):
            with self.subTest(label=label):
                html = self.open_page(label, n)
                self.assertIn('id="size-guide-open"', html)
                self.assertIn('id="size-guide-root"', html)
                self.assertIn('aria-label="%s size guide"' % label, html)
                self.assertIn("js/size-guide.js?v=", html)

    def test_each_table_carries_every_number_of_its_chart_and_its_headings(self):
        for n, (label, (_, guide, chart, headings)) in enumerate(CATEGORY_CASES.items(), start=200):
            with self.subTest(label=label):
                panel = self.panel(self.open_page(label, n))
                self.assertEqual(re.findall(r'<th scope="col">(.*?)</th>', panel), headings)
                for code, parts in chart.items():
                    row = re.search(r'<tr data-size-code="%s".*?</tr>' % code, panel, re.S).group(0)
                    self.assertEqual(row.count("<td>"), len(guide["columns"]), (label, code))
                    for part in parts:
                        self.assertIn(part, row.replace("&quot;", '"'), (label, code, part))

    def test_every_size_looks_the_same_and_there_is_no_extra_text(self):
        for n, label in enumerate(CATEGORY_CASES, start=300):
            with self.subTest(label=label):
                panel = self.panel(self.open_page(label, n))
                self.assertEqual(re.findall(r'<tr data-size-code="([A-Z]+)"', panel), list(CATEGORY_CASES[label][2]))
                for gone in ("not available", "Not available", "data-offered", "How to measure", "Not sure which size", "Done</button>",
                             "ARABELA GOWN RENTAL", "SIZE GUIDE"):
                    self.assertNotIn(gone, panel)

    def test_a_category_in_free_size_only_has_no_guide(self):
        for n, label in enumerate(CATEGORY_CASES, start=400):
            with self.subTest(label=label):
                html = self.open_page(label, n, size=Gown.Size.FREE_SIZE)
                self.assertNotIn("size-guide-open", html)
                self.assertNotIn("size-guide-root", html)

    def test_a_category_without_numbers_yet_still_has_no_link(self):
        for n, (label, collection) in enumerate((("Kids Gown", "kids-gown"), ("Bridesmaid Dresses", "bridesmaid-dresses"), ("Long Gown", "long-gown"), ("Luxury Gown", "luxury-gown")), start=500):
            with self.subTest(label=label):
                unit = make_unit(n, Gown.Size.LARGE, category=label, name=f"{label} Plain")
                response = self.client.get(reverse("gowns:product_detail", kwargs={"collection": collection, "slug": unit.slug}))
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("size-guide-open", response.content.decode())


class SizeGuideScriptTests(SimpleTestCase):
    """The script is plain browser code; these read it for the behaviours the panel must keep."""

    def setUp(self):
        self.js = GUIDE_JS.read_text(encoding="utf8")

    def test_it_closes_on_escape(self):
        self.assertIn("'Escape'", self.js)

    def test_it_only_reads_the_pages_size_buttons_and_never_clicks_them(self):
        self.assertIn("aria-pressed", self.js)
        self.assertNotIn(".click()", self.js)

    def test_it_returns_focus_and_unlocks_scrolling_on_close(self):
        self.assertIn("opener.focus", self.js)
        self.assertIn("sg-lock", self.js)

    def test_nothing_slides_or_scrolls(self):
        self.assertNotIn("translateY", self.js)
        self.assertNotIn("offsetTop", self.js)

    def test_it_respects_reduced_motion(self):
        self.assertIn("prefers-reduced-motion", self.js)
