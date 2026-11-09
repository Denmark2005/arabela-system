"""Reservation Records, redesigned: the five counts are ONE strip of filter buttons (the Gown Catalog look, only the label and the number), the search and
the filters and the list are ONE card, every booking is one short line (a status circle before the name, the booking number with "N gowns", the rental
dates on one line) and everything else about it -- the status in words, the deposit, the receipts, who recorded it, each gown and the two icon buttons --
is in the line's dropdown. The eight statuses each have their own circle colour, with a key. Nothing else changed: the filters, the counts, the links
other pages send, the View window and the upload keep their own tests.

The rows are drawn by the page's script (Alpine) from the data the view sends, so what a server-side test can check is the page's markup, its script's
hooks, its styles, and that the data it draws from is unchanged. The real clicks are in the browser check."""
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from arabela_admin.test_active_dropdown import ActiveDropdownBase, P, R, RET
from reservations.models import Reservation

User = get_user_model()
URL = reverse("arabela_admin:reservation_records")
ADMIN = Path(settings.BASE_DIR) / "templates" / "arabela_admin"
PROGRESS = ["Pending approval", "Awaiting pickup", "With customer", "Partly returned", "Overdue", "Completed", "Rejected", "Cancelled"]


def source():
    return (ADMIN / "reservation-records.html").read_text(encoding="utf8").replace("\r\n", "\n")


def row_template(text):
    start = text.index('<template x-for="r in visibleRecords" :key="r.id">')
    return text[start:text.index('<div class="ar-empty" x-show="visibleRecords.length === 0"', start)]


class RecordsBase(ActiveDropdownBase):
    def page(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()


class StripTests(RecordsBase):
    def test_the_five_counts_are_one_strip_of_buttons_that_still_filter(self):
        self.book("Strip", "09170000001", [(3, 6, P, {})])
        _, html = self.page()
        strip = html[html.index('<div class="rr-strip">'):html.index('<div class="rr-card">')]
        self.assertEqual(strip.count("<button"), 5)
        for mode, label in (("all", "All Reservations"), ("week", "This Week"), ("month", "This Month"), ("cancelled", "Cancelled"), ("needs", "Needs a Receipt")):
            with self.subTest(mode=mode):
                self.assertIn(f'@click="applyCard(\'{mode}\')"', strip)                       # the same filter as before
                self.assertIn(f":class=\"cardActive('{mode}') && 'is-on'\"", strip)
                self.assertIn(f'<span class="rr-tile-l">{label}</span>', strip)
                self.assertIn(f"x-text=\"countFor('{mode}')\"", strip)
        self.assertNotIn("rounded-2xl", strip)

    def test_the_words_that_only_repeated_the_label_are_gone(self):
        self.book("Words", "09170000002", [(3, 6, P, {})])
        _, html = self.page()
        for gone in ("Every booking on record", "Cancelled bookings and what was paid", "Rentals out any day this week", "Rentals out any day this month",
                     "Confirmed, no manual receipt yet", "rr-stat-hint", "rr-stat ", ".rr-stats"):
            self.assertNotIn(gone, html)

    def test_the_two_rules_that_matter_are_a_hover_tooltip(self):
        self.book("Tips", "09170000003", [(3, 6, P, {})])
        _, html = self.page()
        for tip in ('title="Rentals out on any day this week"', 'title="Rentals out on any day this month"', 'title="Confirmed bookings that have no receipt from the shop yet"'):
            self.assertIn(tip, html)

    def test_the_strip_has_its_own_styles_and_a_phone_layout(self):
        text = source()
        for rule in (".rr-strip { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr));", ".rr-tile.is-on { background: #f5f7ff;", ".dark .rr-tile.is-on {",
                     ".rr-tile:last-child:nth-child(odd) { grid-column: 1 / -1; }"):
            self.assertIn(rule, text)


class CardTests(RecordsBase):
    def test_the_search_the_filters_and_the_list_are_one_card(self):
        self.book("Card", "09170000011", [(3, 6, P, {})])
        _, html = self.page()
        card = html[html.index('<div class="rr-card">'):html.index('x-show="openDetail"')]
        self.assertEqual(html.count('class="rr-card"'), 1)
        self.assertLess(card.index('x-model="search"'), card.index('data-arpg='))
        self.assertIn('placeholder="Search customer, email, reference, or gown..."', card)
        self.assertNotIn("Showing <span", card)                                      # the second "Showing X of Y" is gone: the count is said once
        self.assertEqual(card.count('id="rr-count"'), 1)
        self.assertNotIn('<table class="min-w-full">', html)
        self.assertNotRegex(html, r'<h3[^>]*>\s*Reservations\s*</h3>')                # no card title row either

    def test_every_filter_is_still_bound_to_the_same_state(self):
        self.book("Bound", "09170000012", [(3, 6, P, {})])
        _, html = self.page()
        for needle in ('x-model="search"', 'x-model="statusFilter"', 'x-model="receiptFilter"', 'x-model="channelFilter"', 'x-model="pickupYearInput"',
                       'x-model="pickupMonthNum"', '@change="pickupYearChanged()"', '@change="pickupPeriodChanged()"', 'x-model="customFrom"', 'x-model="customTo"',
                       "setDateFilter('all')", "setDateFilter('today')", "setDateFilter('week')", "setDateFilter('month')", "setDateFilter('custom')",
                       'x-for="option in progressOptions"', 'x-for="m in pickupMonthOptions"', 'clearCustomerFilter()', '@click="resetFilters()"'):
            with self.subTest(needle=needle):
                self.assertIn(needle, html)
        for words in ("All statuses", "All receipts", "Has a receipt", "Needs a receipt", "All channels", ">Online</option>", ">Walk-in</option>", "All months",
                      "Rental dates:", "All time", "Pick dates", 'placeholder="Any year"', "No reservations match these filters."):
            self.assertIn(words, html)

    def test_the_fake_spinner_is_gone_and_the_pager_still_reads_the_list(self):
        self.book("Spin", "09170000013", [(3, 6, P, {})])
        _, html = self.page()
        for gone in ("this.filtering", "filterTimeout", "flash(", 'x-show="filtering"', 'style="height: 240px;"'):
            self.assertNotIn(gone, html)
        self.assertIn('data-arpg=".ar-booking" data-arpg-noun="reservations" data-arpg-top="off" data-arpg-count="#rr-count"', html)
        self.assertRegex(html, r'<div class="custom-scrollbar overflow-y-auto" style="max-height: 760px;" data-arpg=')

    def test_the_heading_is_gone_but_the_breadcrumb_stays(self):
        self.book("Head", "09170000014", [(3, 6, P, {})])
        _, html = self.page()
        self.assertNotRegex(html, r'<h2\s[^>]*x-text="pageName"')
        self.assertIn("mb-4 flex flex-wrap items-center justify-end gap-3", html)


class RowTests(RecordsBase):
    def test_a_line_is_the_circle_the_name_the_number_the_gown_count_and_the_dates(self):
        self.book("Row", "09170000021", [(3, 6, P, {})])
        _, html = self.page()
        row = row_template(html)
        closed = row[:row.index('<div class="ar-panel"')]
        for needle in ('class="ar-booking"', ':data-booking-id="r.id"', '@click="toggleRow(r.id)"', 'x-text="r.customer"', 'x-text="r.reference"', 'x-text="datesLine(r)"',
                       "'rr-dot--' + statusKind(r.progress)", ':title="statusTip(r.progress)"', 'role="img"', 'x-show="liveGownCount(r) > 1"', "' gowns'"):
            with self.subTest(needle=needle):
                self.assertIn(needle, closed)
        self.assertLess(closed.index("rr-dot--"), closed.index('x-text="r.customer"'))     # the circle comes first
        self.assertEqual(closed.count('class="ar-chev"'), 1)
        self.assertIn('@click.stop="toggleRow(r.id)"', closed)

    def test_the_closed_line_leaves_the_rest_for_the_dropdown(self):
        self.book("Closed", "09170000022", [(3, 6, P, {})])
        _, html = self.page()
        row = row_template(html)
        closed = row[:row.index('<div class="ar-panel"')]
        for gone in ("r.depositStatus", "r.deposit", "r.receipts", "r.booked", "r.cancel", "r.gownSummary", "startUpload", "openRecord", "rr-badge", "rr-channel"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, closed)

    def test_the_dropdown_has_the_facts_the_gowns_and_two_icon_buttons(self):
        self.book("Panel", "09170000023", [(3, 6, P, {})])
        _, html = self.page()
        row = row_template(html)
        panel = row[row.index('<div class="ar-panel"'):]
        for needle in ("<dt>Status</dt>", "<dt>Deposit</dt>", "<dt>Receipts</dt>", "<dt>Booked</dt>", 'progressClass(r.progress)', 'depositClass(r.depositStatus)', 'x-text="receiptsText(r)"',
                       'x-text="r.channelLabel"', "Not paid yet", "Payment incomplete", 'x-for="(item, n) in r.items"', "shortRange(item.pickup, item.ret)", "stageLabel(item.stage)",
                       "stageTone(item.stage)", "No gowns on this booking."):
            with self.subTest(needle=needle):
                self.assertIn(needle, panel)
        eye = re.search(r'<button[^>]*title="View details"[^>]*>(.*?)</button>', panel, re.S)
        upload = re.search(r'<button[^>]*title="Upload receipt"[^>]*>(.*?)</button>', panel, re.S)
        self.assertIn('@click.stop="openRecord(r)"', eye.group(0))
        self.assertIn('@click.stop="startUpload(r)"', upload.group(0))
        for button in (eye, upload):
            self.assertEqual(re.sub(r"<[^>]+>", "", button.group(1)).strip(), "")             # an icon, no words
            self.assertIn('class="ar-ib"', button.group(0))
            self.assertIn(":aria-label=", button.group(0))

    def test_the_old_wording_buttons_are_gone_from_the_list(self):
        self.book("Words", "09170000024", [(3, 6, P, {})])
        _, html = self.page()
        card = html[html.index('<div class="rr-card">'):html.index('x-show="openDetail"')]
        self.assertNotRegex(card, r">\s*View\s*</button>")
        self.assertNotRegex(card, r">\s*Upload Receipt\s*</button>")

    def test_the_line_opens_from_one_place_and_a_single_match_opens_itself(self):
        text = source()
        self.assertEqual(text.count("toggleRow(id) {"), 1)
        self.assertIn("get autoOpenId() {", text)
        self.assertIn("return rows.length === 1 ? rows[0].id : null;", text)
        self.assertIn("this.$watch('search', () => { this.syncUrl(); this.autoDismissed = []; });", text)
        self.assertIn("isRowOpen(id) {", text)


class StatusCircleTests(RecordsBase):
    def test_each_of_the_eight_statuses_has_its_own_colour_and_its_own_words(self):
        text = source()
        kinds = re.search(r"statusKind\(progress\) \{\s*return \(\{(.*?)\}\)\[progress\]", text, re.S).group(1)
        mapped = dict(re.findall(r"'([^']+)': '([a-z]+)'", kinds))
        self.assertEqual(sorted(mapped), sorted(PROGRESS))
        self.assertEqual(len(set(mapped.values())), 8)                                  # no two statuses share a circle
        tips = re.search(r"statusTip\(progress\) \{\s*return \(\{(.*?)\}\)\[progress\]", text, re.S).group(1)
        for status in PROGRESS:
            self.assertIn(f"'{status}': '{status}:", tips)                              # each hover starts with the status's own name
        self.assertIn("progressOptions: ['Pending approval', 'Awaiting pickup', 'With customer', 'Partly returned', 'Overdue', 'Completed', 'Rejected', 'Cancelled'],", text)

    def test_the_eight_circles_are_eight_different_looks(self):
        text = source()
        looks = {}
        for kind in ("pending", "pickup", "out", "partly", "overdue", "done", "rejected", "cancelled"):
            rule = re.search(r"\.rr-dot--%s \{([^}]*)\}" % kind, text).group(1)
            looks[kind] = re.sub(r"\s+", " ", rule.strip())
        self.assertEqual(len(set(looks.values())), 8, looks)
        self.assertIn("border: 2px solid #98a2b3", looks["cancelled"])                   # Cancelled is the empty ring
        self.assertIn("background: transparent", looks["cancelled"])
        for kind in ("pending", "pickup", "out", "partly", "overdue", "done", "rejected"):
            self.assertRegex(looks[kind], r"^background: #[0-9a-f]{6};$")

    def test_the_key_lists_all_eight_with_the_same_circles(self):
        self.book("Key", "09170000031", [(3, 6, P, {})])
        _, html = self.page()
        key = html[html.index('<details class="ar-tip rr-key"'):html.index("</details>")]
        items = re.findall(r'<li><span class="ar-dot (rr-dot--[a-z]+)"></span><span><b>([^<]+):</b>', key)
        self.assertEqual([words for _, words in items], PROGRESS)
        self.assertEqual(len({dot for dot, _ in items}), 8)
        self.assertIn('@click.outside="$el.removeAttribute(\'open\')"', key)
        self.assertIn('@keydown.escape.stop="$el.removeAttribute(\'open\')"', key)
        self.assertIn("Colour key", key)

    def test_the_same_statuses_still_come_from_the_view(self):
        """The circle only draws what _reservation_progress already says; nothing about when a booking is Overdue / Completed / ... changed."""
        self.book("Pending", "09170000032", [(3, 6, P, {})])
        self.book("Back", "09170000033", [(-9, -2, RET, {"picked_up_on": self.today - timedelta(days=9), "returned_on": self.today - timedelta(days=2)})])
        response, _ = self.page()
        progress = {r["customer"]: r["progress"] for r in response.context["records"]}
        self.assertEqual(progress["Back"], "Completed")
        self.assertEqual(progress["Pending"], "Awaiting pickup")


class UnchangedLogicTests(RecordsBase):
    def test_the_script_keeps_every_rule_and_link_it_had(self):
        text = source()
        for needle in ("applyCard(mode) {", "cardActive(mode) {", "countFor(mode) {", "setDateFilter(mode) {", "pickupYearChanged() {", "pickupPeriodChanged() {",
                       "clearCustomerFilter() {", "resetFilters() {", "needsReceipt(r) {", "matches(r) {", "get visibleRecords()", "get scopedRecords()", "get pickupYearTotal()",
                       "get pickupMonthOptions()", "params.get('search')", "params.get('customer')", "params.get('month')", "params.get('year')", "syncUrl() {",
                       "startUpload(record) {", "submitUpload() {", "openRecord(r) {", "isHighlighted(r) {", "progressClass(progress) {", "depositClass(status) {"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)
        self.assertIn("this.statusFilter = 'Cancelled';", text)
        self.assertIn("this.receiptFilter = 'needs';", text)

    def test_the_view_window_and_the_upload_window_are_still_there(self):
        text = source()
        for needle in ('role="dialog" aria-modal="true" aria-label="Booking details"', "Upload Receipt</h4>", 'x-ref="rrFileInput"', 'x-ref="rrCameraInput"',
                       "closeTopModal()", "timelineHtml(viewing)"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_the_data_the_rows_are_drawn_from_is_unchanged(self):
        self.book("Data", "09170000041", [(3, 6, P, {})])
        response, _ = self.page()
        [record] = [r for r in response.context["records"] if r["customer"] == "Data"]
        for field in ("id", "reference", "customer", "customerId", "email", "phone", "bookedAs", "booked", "progress", "start", "end", "startDisplay", "endDisplay", "pickupMonth",
                      "countsAsRental", "gownSummary", "items", "subtotal", "deposit", "total", "depositStatus", "depositLink", "cancel", "receipts", "channel", "channelLabel",
                      "staffRecorded", "awaitingPayment", "paymentIncomplete", "q"):
            with self.subTest(field=field):
                self.assertIn(field, record)
        self.assertEqual(record["items"][0]["stage"], P)


class FewQuestionsTests(RecordsBase):
    def queries(self):
        self.client.get(URL)
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(URL).status_code, 200)
        return len(captured.captured_queries)

    def test_a_longer_list_does_not_ask_the_database_more_questions(self):
        for number in range(3):
            self.book(f"Few {number}", f"0917100000{number}", [(3, 6, P, {}), (4, 7, R, {})])
        short = self.queries()
        for number in range(3, 12):
            self.book(f"Many {number}", f"091710000{number:02d}", [(3, 6, P, {}), (4, 7, R, {}), (5, 8, P, {})])
        self.assertEqual(self.queries(), short)


class AccessTests(TestCase):
    """The decorator that closes the page to everyone who is not staff sits on the page's own function."""

    def test_an_anonymous_visitor_is_sent_to_sign_in(self):
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_a_customer_is_turned_away(self):
        self.client.force_login(User.objects.create_user("rr_customer", password="x"))
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_staff_get_the_page(self):
        self.client.force_login(User.objects.create_user("rr_staff", password="x", is_staff=True))
        self.assertEqual(self.client.get(URL).status_code, 200)

    def test_an_empty_list_still_renders(self):
        self.client.force_login(User.objects.create_user("rr_staff2", password="x", is_staff=True))
        response = self.client.get(URL)
        self.assertEqual(response.context["records"], [])
        self.assertIn("No reservations match these filters.", response.content.decode())


class SharedStylesTests(SimpleTestCase):
    text = source()

    def test_the_page_uses_the_shared_list_styles_and_its_own_box_rule(self):
        self.assertEqual(self.text.count('{% include "arabela_admin/partials/ar_list_styles.html" %}'), 1)
        self.assertIn(".rr-card { margin-bottom: 1.25rem;", self.text)
        self.assertNotIn(".ar-card {", self.text)                                        # partials/attach_receipt.html owns that name
        self.assertNotRegex(self.text, r"(?m)^\s*\.ar-line \{")                          # the shared line rule is not copied here

    def test_no_column_is_sized_per_row(self):
        rule = re.search(r"\.ar-head\.rr-grid, \.ar-line\.rr-grid \{ grid-template-columns: ([^;]+); \}", self.text).group(1)
        self.assertNotIn("auto", rule)

    def test_the_styles_the_old_table_used_are_gone(self):
        for gone in (".rr-row {", ".rr-scroll-x", ".rr-clip", ".rr-grow", ".rr-fixed", "@media (min-width: 1024px) and (max-width: 1599px)"):
            self.assertNotIn(gone, self.text)

    def test_the_search_and_the_three_boxes_share_one_row_on_a_small_laptop(self):
        block = self.text[self.text.index("@media (max-width: 1180px) {"):]
        block = block[:block.index("\n      }\n")]
        self.assertIn(".ar-search-input.rr-select { flex-basis: 8rem; width: 8rem; }", block)
        self.assertIn(".rr-card .ar-search { flex-basis: 12rem; }", block)

    def test_no_blur_on_the_page(self):
        for blur in ("backdrop-blur", "backdrop-filter", "filter: blur"):
            self.assertNotIn(blur, self.text)

    def test_the_file_keeps_its_windows_line_endings(self):
        raw = (ADMIN / "reservation-records.html").read_bytes()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))


class TimelineTests(SimpleTestCase):
    """The history's circles: an event that already happened is a faded circle, only the latest is solid (with three soft pulses). The list is shared by
    Active Reservations, Pending Approval, Rental History and the Records window, so they all change together."""
    css = (ADMIN / "partials" / "timeline_styles.html").read_text(encoding="utf8")

    def test_an_event_that_happened_is_a_faded_circle(self):
        rule = re.search(r"\.arb-tl-dot \{(.*?)\n  \}", self.css, re.S).group(1)
        self.assertIn("border: 2px solid rgba(18, 183, 106, 0.3);", rule)
        self.assertIn("background: rgba(18, 183, 106, 0.16);", rule)
        self.assertNotIn("background: #ffffff", rule)                                    # no longer an empty white ring
        self.assertIn(".arb-tl-dot.is-negative { border-color: rgba(240, 68, 56, 0.32); background: rgba(240, 68, 56, 0.14); }", self.css)

    def test_only_the_latest_is_solid_and_it_pulses_three_times_then_rests(self):
        self.assertIn("background: var(--color-success-500, #12b76a); animation: arb-tl-pulse 2.2s ease-out 3; }", self.css)
        self.assertIn("animation-name: arb-tl-pulse-bad;", self.css)
        self.assertIn("@keyframes arb-tl-pulse {", self.css)
        self.assertIn("@keyframes arb-tl-pulse-bad {", self.css)
        self.assertIn("@media (prefers-reduced-motion: reduce) { .arb-tl-dot.is-latest { animation: none; } }", self.css)
        self.assertNotIn("infinite", self.css)                                           # nothing keeps moving

    def test_dark_mode_has_its_own_faded_circles(self):
        self.assertIn(".dark .arb-tl-dot { border-color: rgba(50, 213, 131, 0.32); background: rgba(18, 183, 106, 0.16); }", self.css)
        self.assertIn(".dark .arb-tl-dot.is-negative { border-color: rgba(240, 68, 56, 0.4); background: rgba(240, 68, 56, 0.16); }", self.css)

    def test_the_markup_and_the_scroll_box_are_unchanged(self):
        self.assertIn(".arb-tl-row { display: flex", self.css)
        self.assertIn("max-height: 22rem;", self.css)
        rows = (ADMIN / "partials" / "timeline_rows.html").read_text(encoding="utf8")
        self.assertIn('<span class="arb-tl-dot{% if event.is_latest %} is-latest{% endif %}{% if event.is_negative %} is-negative{% endif %}"></span>', rows)


class PaymentVerificationCleanupTests(RecordsBase):
    """Payment Verification: the "Gown ... + Deposit ..." line is gone from the opened booking (the proof window's Amount breakdown says it)."""

    def test_the_split_line_is_gone_but_booked_as_stays(self):
        reservation, _ = self.book("Typed Name", "09170000051", [(3, 6, P, {})], customer=self.customer)
        Reservation.objects.filter(pk=reservation.pk).update(status=Reservation.Status.PENDING, payment_proof_url="https://files.example/p.jpg", payment_method="GCash",
                                                             total_amount=Decimal("5000"))
        html = self.client.get(reverse("arabela_admin:payment_verification")).content.decode()
        start = html.index('<div class="ar-booking" data-booking-id="%d"' % reservation.id)
        block = html[start:html.index('x-show="matchCount() === 0"', start)]
        panel = block[block.index('<div class="ar-panel"'):]
        self.assertIn('<div class="ar-strip"><span>Booked as: Typed Name</span></div>', panel)
        self.assertNotIn("Gown ₱3,000", panel)
        self.assertNotIn("Total ₱5,000", panel)
        self.assertIn("Amount breakdown", html)                                           # the window still has it
        self.assertIn('<div class="pv-amt">₱2,000</div>', block)                     # and the bold amount is still on the line

    def test_a_booking_with_no_booked_as_has_no_extra_line(self):
        reservation, _ = self.book("Same Name", "09170000052", [(3, 6, P, {})])
        Reservation.objects.filter(pk=reservation.pk).update(status=Reservation.Status.PENDING, payment_proof_url="https://files.example/p.jpg", payment_method="GCash")
        html = self.client.get(reverse("arabela_admin:payment_verification")).content.decode()
        start = html.index('<div class="ar-booking" data-booking-id="%d"' % reservation.id)
        panel = html[start:html.index('x-show="matchCount() === 0"', start)]
        panel = panel[panel.index('<div class="ar-panel"'):]
        self.assertNotIn('class="ar-strip"', panel)
        self.assertEqual(panel.count('class="ar-gl pv-gl"'), 1)
