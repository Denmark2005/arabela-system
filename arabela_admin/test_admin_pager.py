"""The admin list pager (static/arabela_admin/pager.js): every long admin list shows 15 rows at a time. These checks read the template
and script files only -- no database -- so they run in a second."""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR)
TEMPLATES = ROOT / "templates" / "arabela_admin"
PAGER = ROOT / "static" / "arabela_admin" / "pager.js"
LIST_PAGES = (
    "active-reservations", "rental-history", "reservation-records", "gown-catalog",
    "clients", "pending-approval", "payment-verification", "security-deposits",
)


def read(name):
    return (TEMPLATES / f"{name}.html").read_text(encoding="utf8")


class AdminPagerWiringTests(SimpleTestCase):
    def test_every_long_list_opts_in_exactly_once_and_names_its_rows(self):
        for name in LIST_PAGES:
            with self.subTest(page=name):
                html = read(name)
                self.assertEqual(html.count('data-arpg="'), 1)
                self.assertEqual(html.count('data-arpg-noun="'), 1)

    def test_every_long_list_loads_the_pager_script_after_the_bundle(self):
        for name in LIST_PAGES:
            with self.subTest(page=name):
                html = read(name)
                tag = "<script defer src=\"{% static 'arabela_admin/pager.js' %}\"></script>"
                self.assertEqual(html.count(tag), 1)
                self.assertGreater(html.index(tag), html.index("arabela_admin/bundle.js"))

    def test_the_list_box_keeps_its_own_scroll_and_filter_attributes(self):
        """The pager only adds attributes to the box; the 760px scroll and the page's own x-show are untouched."""
        for name in LIST_PAGES:
            with self.subTest(page=name):
                box = re.search(r"<div[^>]*data-arpg=[^>]*>", read(name))
                self.assertIsNotNone(box)
                self.assertIn("max-height: 760px", box.group(0))

    def test_the_pager_script_exists_pages_by_15_and_turns_itself_off_if_it_fails(self):
        js = PAGER.read_text(encoding="utf8")
        self.assertIn("var PAGE_SIZE = 15;", js)
        self.assertIn("function failOpen", js)           # any error -> every hidden row is shown again, like before paging existed
        self.assertIn('var OFF = "data-arpg-off";', js)  # its own attribute: it never touches the style Alpine's x-show sets

    def test_clicking_through_the_pages_keeps_the_screen_still(self):
        """A last page with fewer rows used to make the card shorter: the Back / Next bar jumped up from under the pointer, the page
        scrolled and, once it needed no scrollbar, the layout slid sideways. The list keeps the height of a full page, is pinned BEFORE any
        row is hidden (hiding first shortens the page for a moment and the browser scrolls it), and the page is never scrolled by a click."""
        js = PAGER.read_text(encoding="utf8")
        self.assertNotRegex(js, r"scrollIntoView|scrollTo\(|scrollBy\(")      # nothing may scroll the page (only the list's own box.scrollTop = 0)
        self.assertIn("function pinHeight", js)
        self.assertIn("function holdStill", js)
        self.assertLess(js.index("pinHeight();"), js.index("u.forEach(function (row)"))      # pinned before the rows are hidden
        self.assertNotIn("scrollbarGutter", js)                                               # no reserved scrollbar space: layout stays as it was

    def test_the_number_row_is_always_seven_slots_so_back_and_next_never_slide(self):
        js = PAGER.read_text(encoding="utf8")
        for pattern in ("[1, 2, 3, 4, 5, 0, last]", "[1, 0, last - 4, last - 3, last - 2, last - 1, last]", "[1, 0, current - 1, current, current + 1, 0, last]"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, js)
        self.assertIn('.arpg-btn[data-key^=\\"n\\"]{width:2.5rem', js)      # 1, 10 and 100 take the same room
        self.assertIn(".arpg-gap{width:2.5rem", js)                         # and so does a "..."


class GownCatalogSelectAllTests(SimpleTestCase):
    """The Gown Catalog's select-all works on the page being shown. Its code lives inside ONE double-quoted x-data attribute, so a stray
    double quote in a comment there cuts the attribute short and breaks the whole page -- these tests pin both the logic and that."""

    def x_data(self):
        html = read("gown-catalog").replace("\r\n", "\n")
        start = html.index('x-data="{ page: \'gown\'') + len('x-data="')
        return html[start:html.index('"', start)]

    def test_the_x_data_attribute_holds_the_whole_page_aware_selection_code(self):
        code = self.x_data()
        for needle in ("pageGownIds()", "canSelectAllMatching()", "selectAllMatching()", "toggleSelectAllVisible(checked)", "pageTick: 0"):
            with self.subTest(needle=needle):
                self.assertIn(needle, code)

    def test_select_all_falls_back_to_every_match_if_the_pager_is_not_running(self):
        self.assertIn("if (!rows) { return this.visibleGownIds(); }", self.x_data())

    def test_the_header_box_and_its_half_ticked_state_use_the_page(self):
        code = self.x_data()
        for fn in ("allVisibleSelected", "someVisibleSelected", "toggleSelectAllVisible"):
            with self.subTest(fn=fn):
                definition = re.search(re.escape(fn) + r"\([a-z]*\) \{", code)      # the method itself, not a call to it
                self.assertIsNotNone(definition)
                self.assertIn("this.pageGownIds()", code[definition.end():definition.end() + 200])

    def test_the_page_redraws_the_header_box_when_the_pager_changes_page(self):
        html = read("gown-catalog")
        self.assertIn('@arabela-pager.window="pageTick++"', html)
        self.assertIn('x-show="canSelectAllMatching()"', html)
