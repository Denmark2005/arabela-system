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


def function_body(name):
    """The text of one method of the page script (from its name to the end of that method)."""
    script = page_text()
    start = script.index("          %s {" % name)
    end = script.index("\n          },", start)
    return script[start:end]


class DropdownLayoutTests(SimpleTestCase):
    """The View window is short: Contact is one line, each gown is one short line that opens its own dates (first 5, then "Show all N gowns"),
    Payment and History are closed rows with a one-line summary, Manual Receipts is one header line with an upload icon."""

    def css(self):
        return page_text().split("/* ===== Booking details window (View)")[1].split("</style>")[0]

    def test_the_sections_come_in_the_planned_order(self):
        markup = window_markup()
        spots = [markup.index(piece) for piece in ('class="rrd-summary"', 'class="rrd-contactline"', "Gowns <small", '<span class="rrd-acct">Payment</span>',
                                                   "Manual Receipts <small", '<span class="rrd-acct">History')]
        self.assertEqual(spots, sorted(spots))

    def test_each_gown_is_one_line_that_opens_its_own_dates(self):
        markup = window_markup()
        for needle in ('class="rrd-gline"', "toggleGown(itemIndex)", ':aria-expanded="gownOpen === itemIndex', 'x-show="gownOpen === itemIndex"',
                       "gownDates(item)", "stageDot(item.stage)", "stageLabel(item.stage)", "gownSummary(viewing)", 'class="rrd-steps"', "Actually picked up", "Actually returned"):
            with self.subTest(needle=needle):
                self.assertIn(needle, markup)
        self.assertEqual(markup.count('class="rrd-gline"'), 1)               # one template row, drawn once per gown by x-for
        self.assertNotIn('class="rrd-gown"', markup)                          # the big card per gown is gone

    def test_only_the_first_five_gowns_show_until_show_all(self):
        markup = window_markup()
        self.assertIn('x-show="gownsAll || 5 > itemIndex"', markup)
        self.assertIn('x-show="viewing.items.length > 5"', markup)
        self.assertIn("'Show fewer gowns' : 'Show all ' + viewing.items.length + ' gowns'", markup)
        self.assertIn("No gowns on this reservation.", markup)

    def test_the_new_state_and_helpers_exist_once_and_open_resets_them(self):
        html = page_text()
        for needle in ("gownOpen: null,", "gownsAll: false,", "payOpen: false,", "historyOpen: false,", "stageDot(stage) {", "gownSummary(record) {",
                       "toggleGown(index) {", "gownDates(item) {", "historyLatest(record) {"):
            with self.subTest(needle=needle):
                self.assertEqual(html.count(needle), 1)
        opening = function_body("openRecord(r)")
        for reset in ("this.gownOpen = null;", "this.gownsAll = false;", "this.payOpen = false;", "this.historyOpen = false;", "this.historyAll = false;"):
            with self.subTest(reset=reset):
                self.assertIn(reset, opening)

    def test_the_five_gown_states_each_have_their_own_circle(self):
        dots = function_body("stageDot(stage)")
        for stage in ("'Reserved'", "'Return'", "'Overdue'", "'Returned'", "'Cancelled'"):
            self.assertIn(stage, dots)
        css = self.css()
        looks = {kind: re.search(r"\.rrd-dot\.is-%s \{ ([^}]*)\}" % kind, css).group(1) for kind in ("wait", "out", "late", "done", "void")}
        self.assertEqual(len(set(looks.values())), 5)                         # five different looks

    def test_payment_and_history_are_closed_rows_with_a_summary_and_the_right_hooks(self):
        markup = window_markup()
        self.assertEqual(markup.count('class="rrd-acch"'), 2)
        for needle in ('@click="payOpen = !payOpen"', '@click="historyOpen = !historyOpen"', ":aria-expanded=\"payOpen ? 'true' : 'false'\"",
                       ":aria-expanded=\"historyOpen ? 'true' : 'false'\"", 'x-show="payOpen" x-cloak', 'x-show="historyOpen" x-cloak', "historyLatest(viewing)",
                       "historyCount(viewing)", "viewing.subtotal", "viewing.paymentState", "timelineHtml(viewing)"):
            with self.subTest(needle=needle):
                self.assertIn(needle, markup)
        self.assertNotIn("rrd-statusrow", markup)                             # the status words live in the top band only

    def test_contact_is_one_line_with_icon_only_copy_buttons_that_never_write_text_into_themselves(self):
        markup = window_markup()
        self.assertEqual(markup.count('class="rrd-cchip"'), 2)
        for needle in ('title="Copy email"', 'aria-label="Copy email"', 'title="Copy phone number"', 'aria-label="Copy phone number"',
                       "copyText(viewing.email, $event)", "copyText(viewing.phone, $event)", "Not provided"):
            with self.subTest(needle=needle):
                self.assertIn(needle, markup)
        self.assertNotRegex(markup, r">\s*Copy\s*</button>")                  # no word buttons
        body = function_body("copyText(text, event)")
        self.assertNotIn("textContent", body)                                  # it must keep its drawing (the Remind button had this bug once)
        self.assertIn("classList.add('is-copied')", body)

    def test_the_buttons_that_were_words_are_icons_with_hover_and_screen_reader_words(self):
        markup = window_markup()
        for words in ("View customer's payment proof", "Upload Receipt", "View receipt photo"):
            with self.subTest(words=words):
                self.assertIn('title="%s"' % words, markup)
                self.assertIn('aria-label="%s"' % words, markup)
        self.assertNotRegex(markup, r">\s*Upload Receipt\s*</button>")
        self.assertNotRegex(markup, r">\s*View\s*</button>")
        self.assertIn(':title="viewing.depositLinkLabel"', markup)
        self.assertIn("No receipt attached yet.", markup)                      # kept as a small grey line (test_help_tips_and_dates needs the words)

    def test_the_new_buttons_rows_and_circles_have_a_dark_twin_and_the_phone_has_its_own_layout(self):
        css = self.css()
        lines = [line.strip() for line in css.splitlines()]
        for light in (".rrd-ib {", ".rrd-cchip {", ".rrd-gl-dates {", ".rrd-acct {", ".rrd-accs {", ".rrd-accb {", ".rrd-gsum {"):
            with self.subTest(light=light):
                self.assertIn(True, [line.startswith(".dark " + light) for line in lines])
        self.assertIn(True, [line.startswith(".dark .rrd-glist, .dark .rrd-acc {") for line in lines])
        phone = css.split("@media (max-width: 620px)")[1]
        for rule in ('grid-template-areas: "dot main chev" ". dates dates" ". stage stage"', 'grid-template-areas: "title chev" "sum sum"'):
            with self.subTest(rule=rule):
                self.assertIn(rule, phone)

    def test_the_file_keeps_its_windows_line_endings(self):
        raw = PAGE.read_bytes()
        self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"))                  # this template is CRLF all the way, like four other admin pages
