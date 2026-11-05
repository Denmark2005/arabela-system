"""Reservation Records -> View: the booking-details window. These checks read the template only (no database), so they run in a second.
They guard the things a stray character in a long template can silently break: the markup stays balanced, the page still compiles, the
window keeps calling the same actions as before (upload, view photo, History), and every new style is namespaced rrd- so nothing else
on the page (or on the other pages that share the History list) can change."""
import re
from html.parser import HTMLParser
from pathlib import Path

from django.conf import settings
from django.template.loader import get_template
from django.test import SimpleTestCase

PAGE = Path(settings.BASE_DIR) / "templates" / "arabela_admin" / "reservation-records.html"


def page_text():
    return PAGE.read_text(encoding="utf8")


def window_markup():
    html = page_text()
    start = html.index('<div class="no-scrollbar rr-panel rrd-win')
    return html[start:html.index("<!-- ===== Receipt photo viewer ===== -->")]


class _Balance(HTMLParser):
    """Every opened tag must be closed in order (void tags and self-closed SVG shapes aside)."""
    VOID = {"img", "br", "hr", "input", "meta", "link"}

    def __init__(self):
        super().__init__()
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"</{tag}> closes {self.stack[-1] if self.stack else 'nothing'}")
        else:
            self.stack.pop()


class ViewWindowStructureTests(SimpleTestCase):
    def test_the_page_still_compiles(self):
        get_template("arabela_admin/reservation-records.html")

    def test_the_window_markup_is_balanced(self):
        parser = _Balance()
        parser.feed("<div>" + window_markup())          # the block ends with the closing tag of the overlay that opens just before it
        self.assertEqual(parser.errors, [])
        self.assertEqual(parser.stack, [])

    def test_the_style_block_is_balanced(self):
        css = page_text().split("<style>")[1].split("</style>")[0]
        self.assertEqual(css.count("{"), css.count("}"))

    def test_the_window_still_calls_the_same_actions(self):
        markup = window_markup()
        for needle in ("startUpload(viewing)", "viewPhoto(receipt, viewing)", "timelineHtml(viewing)", "nextReceiptCovers(viewing)",
                       "progressClass(viewing.progress)", "depositClass(viewing.depositStatus)", "openDetail = false", "Upload Receipt",
                       "viewing.paymentProofUrl", "viewing.depositLink", "viewing.cancel", "viewing.awaitingPayment", "viewing.paymentIncomplete"):
            with self.subTest(needle=needle):
                self.assertIn(needle, markup)

    def test_the_new_helpers_exist_and_history_starts_collapsed(self):
        html = page_text()
        for needle in ("historyAll: false,", "this.historyAll = false;", "historyCount(record) {", "periodParts(r) {", "stageLabel(stage) {",
                       "stageTone(stage) {", "copyText(text, event) {"):
            with self.subTest(needle=needle):
                self.assertEqual(html.count(needle), 1)

    def test_every_style_added_for_the_window_is_namespaced(self):
        """The old rr- rules stay exactly as they were; the new ones are all rrd- (or the shared History list, only inside .rrd-history)."""
        css = page_text().split("/* ===== Booking details window (View)")[1].split("</style>")[0]
        selectors = re.findall(r"(?m)^\s*((?:\.dark )?[.][^{]+?)\s*\{", css)
        self.assertTrue(selectors)
        for selector in selectors:
            for part in selector.split(","):
                part = part.strip()
                with self.subTest(selector=part):
                    self.assertRegex(part, r"^(\.dark )?\.rrd-")

    def test_the_history_changes_stay_inside_the_window(self):
        """timeline_styles.html is shared by 4 pages and is not touched; this page only overrides it under .rrd-history."""
        css = page_text().split("/* History: the server-built list")[1].split("@media (max-width: 620px)")[0]
        for line in css.splitlines():
            if "arb-tl" in line and "{" in line:
                with self.subTest(line=line.strip()[:60]):
                    self.assertIn(".rrd-history", line)
        shared = (PAGE.parent / "partials" / "timeline_styles.html").read_text(encoding="utf8")
        self.assertIn("max-height: 22rem;", shared)       # the other pages keep their own scroll box
