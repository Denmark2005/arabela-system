"""Gown Catalog, redesigned: the list is one short line per gown (tick box, photo, a status circle + the ID, the name, "Category · Size", the price, an eye and a
pencil) with a dropdown for the rest (tag, details, status in words, who holds it, the physical check, notes); the gown window keeps every action but its
tutorial text moved behind "?" and its Blocked Dates form is folded away until "+ Block dates"; the long hints on the page and in Add / Edit Gown moved behind "?".

What is checked: what each line and its dropdown say, that every hook the page's script, the pager, the bulk bar and the sort need is still there, where each
sentence went, that the page asks the database the same number of questions however many gowns there are, and that the page is still closed to anyone who is
not staff. The real clicks are in the browser check."""
import re
from datetime import date, timedelta
from html.parser import HTMLParser
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from arabela_admin.test_active_dropdown import ActiveDropdownBase, P
from gowns.models import Gown, GownUnavailability
from reservations.models import Reservation

User = get_user_model()
URL = reverse("arabela_admin:gown_catalog")
ADMIN = Path(settings.BASE_DIR) / "templates" / "arabela_admin"


def source():
    return (ADMIN / "gown-catalog.html").read_text(encoding="utf8").replace("\r\n", "\n")


def without_tips(html):
    """The page's text with every "?" bubble taken out: what a person reads WITHOUT clicking a "?"."""
    return re.sub(r'<div class="ht-tip-pop">.*?</div>', "", html, flags=re.S)


def tips_text(html):
    return " ".join(re.findall(r'<div class="ht-tip-pop">(.*?)</div>', html, flags=re.S))


class _TipPlacement(HTMLParser):
    """A "?" must never sit inside a <button> (a click on it would press the button) or inside a <label> (it would be taken as a click on the label's control)."""

    def __init__(self):
        super().__init__()
        self.stack, self.errors, self.tips = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("button", "label"):
            self.stack.append(tag)
        if tag == "details" and "ht-tip" in (dict(attrs).get("class") or "").split():
            self.tips += 1
            if self.stack:
                self.errors.append(f"a ? inside <{self.stack[-1]}> near line {self.getpos()[0]}")

    def handle_endtag(self, tag):
        if tag in self.stack:
            while self.stack and self.stack[-1] != tag:
                self.stack.pop()
            self.stack.pop()


def make_gown(number, **extra):
    fields = dict(gown_id=f"GC-TEST-{number:03d}", name=f"Catalog Gown {number}", category=Gown.Category.GUEST_GOWN, color_name="Red", color_code="RD",
                  size=Gown.Size.MEDIUM, rental_price=Decimal("1500"), status=Gown.Status.AVAILABLE)
    fields.update(extra)
    return Gown.objects.create(**fields)


class CatalogBase(ActiveDropdownBase):
    def page(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode().replace("\r\n", "\n")

    def row(self, html, gown):
        start = html.index('aria-label="Select %s"' % gown.gown_id)
        start = html.rindex('<div class="ar-booking gc-row"', 0, start)
        end = html.find('<div class="ar-booking gc-row"', start + 10)
        return html[start:end if end != -1 else html.index("<!-- Add Gown Modal", start)]

    def closed(self, row):
        return row[:row.index('<div class="ar-panel"')]

    def panel(self, row):
        return row[row.index('<div class="ar-panel"'):]


class ListLineTests(CatalogBase):
    def test_the_list_is_rows_not_a_table_and_the_pager_reads_them(self):
        make_gown(1)
        make_gown(2)
        _, html = self.page()
        card = html[html.index('class="gc-card '):html.index("<!-- Add Gown Modal")]
        self.assertNotIn("<table", card)
        self.assertEqual(card.count('class="ar-booking gc-row" data-gown-row'), 2)
        self.assertIn('data-arpg=".gc-row" data-arpg-noun="gowns" data-arpg-top="off" data-arpg-count="#gc-count"', card)
        self.assertIn('x-ref="gownTableBody"', card)                                      # the sort and select-all find the list through this
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", card[card.index('class="ar-head gc-grid"'):card.index('class="ar-empty"')]))
        self.assertIn("Gown", text)
        self.assertIn("Category · Size", text.replace("&middot;", "·"))
        self.assertIn("Price", text)
        self.assertIn("Actions", text)
        self.assertNotIn("Status", text)                                                  # the status column is gone: it is the circle beside the ID
        for key in ("gown", "category", "price"):
            self.assertIn(f"sortGowns('{key}')", card)
        self.assertNotIn("sortGowns('status')", card)

    def test_a_line_is_the_circle_the_id_the_name_the_category_the_price_and_two_icons(self):
        gown = make_gown(3, name="Crimson Dream", rental_price=Decimal("12500.50"))
        _, html = self.page()
        closed = self.closed(self.row(html, gown))
        for needle in ('class="ar-line gc-grid"', "@click=\"if (!$event.target.closest('.gc-pick, .gc-photo, .gc-acts, a')) { toggleGown(%d) }\"" % gown.id,
                       'class="gc-dot gc-dot--row gc-dot--available"', 'aria-label="Available" title="Available"', '<span class="ar-name">GC-TEST-003</span>',
                       '<span class="gc-sub">Crimson Dream</span>', '<div class="gc-cat">Guest Gown &middot; Medium</div>', '<div class="gc-price">&#8369;12,500.50</div>',
                       'class="ar-ib" title="View gown"', 'class="ar-ib" title="Edit gown"', '@click.stop="toggleGown(%d)"' % gown.id):
            with self.subTest(needle=needle[:60]):
                self.assertIn(needle, closed)
        self.assertLess(closed.index("gc-dot--row"), closed.index('<span class="ar-name">'))     # the circle comes first
        for button in re.findall(r'<button type="button"\s+@click="(?:viewingGown|openEditGownModal)[^>]*>(.*?)</button>', closed, re.S):
            self.assertEqual(re.sub(r"<[^>]+>", "", button).strip(), "")                       # icons, no words

    def test_each_status_has_its_own_circle(self):
        a = make_gown(4)
        r = make_gown(5, status=Gown.Status.RESERVED)
        o = make_gown(6, status=Gown.Status.OUT_OF_STOCK)
        _, html = self.page()
        for gown, cls, word in ((a, "available", "Available"), (r, "reserved", "Reserved"), (o, "oos", "Out-of-Stock")):
            with self.subTest(status=word):
                self.assertIn(f'class="gc-dot gc-dot--row gc-dot--{cls}" role="img" aria-label="{word}"', self.closed(self.row(html, gown)))

    def test_the_closed_line_leaves_the_rest_for_the_dropdown(self):
        gown = make_gown(7, notes="A note", design_variant="Lace")
        _, html = self.page()
        closed = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", self.closed(self.row(html, gown))))
        for gone in ("Never checked", "Checked", " tag ", "No. ", "Lace", "A note", "Condition", "Physical check", "View ", "Edit "):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, closed)

    def test_the_hooks_the_script_the_bulk_bar_and_the_sort_read_are_on_every_row(self):
        gown = make_gown(8, rental_price=Decimal("999"))
        _, html = self.page()
        row = self.row(html, gown)
        self.assertIn('data-sort-gown="guest gown-', row)
        self.assertIn('data-sort-category="guest gown"', row)
        self.assertIn('data-sort-price="999.00"', row)
        self.assertNotIn("data-sort-status", row)
        self.assertIn("x-show=\"gownMatchesFilters('Guest Gown', 'Available', ", row)
        self.assertIn('x-model.number="selectedGownIds"', row)
        self.assertIn(f'value="{gown.id}"', row)
        self.assertIn(f'aria-label="Select {gown.gown_id}"', row)
        self.assertIn("isGownImageModal = true", row)
        self.assertIn('title="View or replace gown photo"', row)
        self.assertIn("openGownBlockForm(); isViewModal = true", row)                      # the eye opens the same window with the same data
        self.assertIn("openEditGownModal({ id: %d," % gown.id, row)
        for field in ("gownId:", "name:", "category:", "colorName:", "colorCode:", "size:", "designVariant:", "price:", "condition:", "status:", "notes:", "slug:"):
            self.assertIn(field, row)

    def test_the_script_keeps_select_all_sort_and_gets_the_line_state(self):
        text = source()
        self.assertIn("const rows = Array.from(tbody.querySelectorAll('[data-gown-row]'));", text)
        self.assertNotIn("tr[data-gown-row]", text)
        for needle in ("openGowns: [],", "isGownOpen(id) {", "toggleGown(id) {", "showBlockForm: false,", "pageGownIds() {", "window.arabelaPager.shownUnits(tbody)",
                       "toggleSelectAllVisible(checked) {", "matchingGownCount() {", "this.showBlockForm = false;"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_an_empty_catalog_and_a_filter_with_no_match_say_so(self):
        _, html = self.page()
        self.assertIn("No gowns in the catalog yet. Click &quot;Add Gown&quot; to create the first one.", html)
        make_gown(9)
        _, html = self.page()
        self.assertIn("No gowns match your filters.", html)
        self.assertLess(html.index("No gowns match your filters."), html.index('class="ar-booking gc-row"'))      # before the rows, so a re-sort keeps the order

    def test_the_legend_the_strip_and_the_filters_are_still_there(self):
        make_gown(10)
        _, html = self.page()
        for needle in ('class="gc-legend-list"', "</span>Available</li>", "</span>Reserved</li>", "</span>Out-of-Stock</li>", 'class="gc-stats"', 'aria-label="Filter by status"',
                       'aria-label="Filter by category"', 'aria-label="Filter by physical check"', 'class="gc-toolbar-search"', 'id="gc-count"'):
            with self.subTest(needle=needle):
                self.assertIn(needle, html)


class DropdownTests(CatalogBase):
    def test_the_dropdown_has_the_tag_the_details_the_status_and_the_check(self):
        gown = make_gown(11, notes="Hem taken in", design_variant="Lace", condition="Fair", color_name="Wine")
        _, html = self.page()
        panel = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", self.panel(self.row(html, gown))))
        for needle in ("Tag", "tag · No.", "Category Guest Gown", "Color Wine", "Size Medium", "Variant Lace", "Condition Fair", "Status Available", "Physical check Never checked", "Notes: Hem taken in"):
            with self.subTest(needle=needle):
                self.assertIn(needle, panel.replace("&middot;", "·"))

    def test_the_variant_and_the_notes_show_only_when_there_are_some(self):
        gown = make_gown(12)
        _, html = self.page()
        panel = self.panel(self.row(html, gown))
        self.assertNotIn("<dt>Variant</dt>", panel)
        self.assertNotIn("gc-notes", panel)

    def test_a_reserved_gown_names_who_holds_it_with_a_link_and_a_count(self):
        reservation, [item] = self.book("Holder One", "09170000001", [(3, 6, P, {})])
        Reservation.objects.filter(pk=reservation.pk).update(status=Reservation.Status.PENDING, reviewed_at=None)
        Gown.objects.filter(pk=item.gown_id).update(status=Gown.Status.RESERVED)
        _, html = self.page()
        panel = self.panel(self.row(html, Gown.objects.get(pk=item.gown_id)))
        self.assertIn("Holder One", panel)
        self.assertIn(reverse("arabela_admin:pending_approval"), panel)
        self.assertIn("awaiting payment approval", panel)
        self.assertEqual(panel.count("<a "), 1)

    def test_a_reserved_gown_nobody_holds_says_so(self):
        gown = make_gown(13, status=Gown.Status.RESERVED)
        _, html = self.page()
        self.assertIn("Set manually &middot; no booking", self.panel(self.row(html, gown)))

    def test_a_checked_gown_says_when_and_by_whom(self):
        gown = make_gown(14)
        Gown.objects.filter(pk=gown.pk).update(last_checked_at=timezone.now() - timedelta(days=2), last_checked_by=self.staff)
        _, html = self.page()
        panel = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", self.panel(self.row(html, gown))))
        self.assertRegex(panel, r"Physical check Checked [A-Z][a-z]{2} \d{1,2} by ")


class GownWindowTests(CatalogBase):
    """The View window keeps every section and every button; its explanations moved behind "?"."""

    def window(self):
        make_gown(20)
        _, html = self.page()
        start = html.index("<!-- Gown View Modal")
        return html[start:html.index("<!-- Remove Gown modal", start)]

    def test_the_tutorial_sentences_are_only_inside_a_question_mark(self):
        window = self.window()
        shown = without_tips(window)
        for sentence in ("Damaged or otherwise unavailable?", "Days this gown can't go out even though nobody booked it", "when you've just seen this gown on the rack",
                         "Can this gown be booked for a customer's event date?", "Who the gown is reserved for."):
            with self.subTest(sentence=sentence):
                self.assertNotIn(sentence, shown)
                self.assertIn(sentence, tips_text(window))
        self.assertIn("Reserved is set automatically when a booking is approved", tips_text(window))

    def test_each_section_has_its_question_mark_and_keeps_its_title(self):
        window = self.window()
        for label, title in (("the status", "Current Status"), ("who holds this gown", "Booked by"), ("Check availability", "Check availability"),
                             ("blocked dates", "Blocked Dates"), ("the physical check", "Physical Check")):
            with self.subTest(section=title):
                self.assertIn(f'aria-label="About {label}"', window)
                self.assertIn(f">{title}</p>", window)
        self.assertEqual(window.count('<details class="ht-tip">'), 5)

    def test_every_button_of_the_window_is_still_there_with_its_call(self):
        window = self.window()
        for needle in ("changeGownStatus('Available')", "changeGownStatus('Out-of-Stock')", "Mark Available", "Mark Out-of-Stock", "markChecked([viewingGown.id])", "Mark as checked now",
                       "addGownBlock()", "removeGownBlock(block)", "window.arabelaAvailability.inline($refs.caInlineDate, viewingGown.id, $refs.caInlineOut)",
                       "currentHolders()", "currentBlocks()", "gownCheckLabel(viewingGown.id)", "Block These Dates"):
            with self.subTest(needle=needle):
                self.assertIn(needle, window)

    def test_the_blocked_dates_form_is_folded_until_asked_for(self):
        window = self.window()
        self.assertIn('<div x-show="!showBlockForm">', window)
        self.assertIn('@click="showBlockForm = true"', window)
        self.assertIn("+ Block dates", window)
        form = window[window.index('<div x-show="showBlockForm" x-cloak>'):window.index("Block These Dates")]
        for needle in ('x-model="newBlockStart"', 'x-model="newBlockEnd"', 'x-model="newBlockReason"', 'x-model="newBlockNote"', 'x-show="blockError"', "showBlockForm = false; blockError = ''"):
            with self.subTest(needle=needle):
                self.assertIn(needle, form)
        self.assertIn("No blocked dates.", window)

    def test_the_six_facts_sit_in_a_compact_block(self):
        window = self.window()
        self.assertIn('<div class="gc-win-facts">', window)
        for label in ("Category", "Color", "Size", "Design Variant", "Rental Price", "Condition"):
            self.assertIn(f">{label}</p>", window)
        self.assertIn(".gc-win-facts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));", source())

    def test_the_check_availability_box_has_its_limits_and_no_per_box_script(self):
        window = self.window()
        box = window[window.index('x-ref="caInlineDate"'):]
        box = box[:box.index(">")]
        self.assertIn('x-init="window.arabelaAvailability && window.arabelaAvailability.limit($el)"', box)
        self.assertNotIn("onclick", window)
        self.assertNotIn("showPicker", source())

    def test_blocked_dates_a_block_is_still_listed_with_its_release(self):
        gown = make_gown(21)
        GownUnavailability.objects.create(gown=gown, start_date=date.today() + timedelta(days=3), end_date=date.today() + timedelta(days=5), reason="Cleaning", note="Stain")
        _, html = self.page()
        self.assertIn("Stain", html)                                                        # the block's note is in the data the window lists from
        self.assertIn("removeGownBlock(block)", html)


class HintTests(CatalogBase):
    def test_the_panels_above_the_list_keep_their_sentences_behind_a_question_mark(self):
        owner = User.objects.create_superuser("gc_owner", password="x", email="gc@example.com")
        self.client.force_login(owner)
        gown = make_gown(30)
        GownUnavailability.objects.create(gown=gown, start_date=date.today() - timedelta(days=1), end_date=date.today() + timedelta(days=3), reason="Cleaning")
        _, html = self.page()
        shown = without_tips(html)
        for sentence, label in (("Sorted by soonest to free up.", "blocked gowns"), ("Every category has one tag color.", "tag colors"),
                                ("A new kind of gown or outfit", "adding a category"), ("The original ones can be brought back by adding a category with the same name.", "removing a category")):
            with self.subTest(sentence=sentence):
                self.assertNotIn(sentence, shown)
                self.assertIn(sentence, tips_text(html))
                self.assertIn(f'aria-label="About {label}"', html)
        self.assertIn("Up to 20 characters. Letters, numbers, spaces and hyphens.", shown)     # a short limit stays visible

    def test_add_and_edit_gown_hints(self):
        _, html = self.page()
        shown = without_tips(html)
        for sentence in ("Name a gown by its color. It fills in from the Color you pick below", "Gown ID and product link are permanent", "A portrait photo (3:4) works best"):
            with self.subTest(sentence=sentence):
                self.assertNotIn(sentence, shown)
                self.assertIn(sentence, tips_text(html))
        self.assertIn('<details class="ht-tip" x-show="!isEditMode" x-cloak>', html)               # the naming "?" only while adding
        self.assertIn("To change the photo, click the gown's thumbnail in the list.", html)
        self.assertIn("Keeps the name. One listing with a quantity", html)                          # the choice descriptions stay, shorter (they sit inside buttons)
        self.assertNotIn("Portrait photo works best (3:4) --", html)

    def test_no_question_mark_is_inside_a_button_or_a_label_that_wraps_a_control(self):
        parser = _TipPlacement()
        parser.feed(source())
        self.assertEqual(parser.errors, [])
        self.assertGreaterEqual(parser.tips, 12)


class FewQuestionsTests(CatalogBase):
    def queries(self):
        self.client.get(URL)
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(URL).status_code, 200)
        return len(captured.captured_queries)

    def test_a_longer_catalog_does_not_ask_the_database_more_questions(self):
        for number in range(3):
            make_gown(100 + number, status=Gown.Status.RESERVED, notes="n")
        short = self.queries()
        for number in range(3, 20):
            gown = make_gown(100 + number, status=Gown.Status.RESERVED, notes="n")
            Gown.objects.filter(pk=gown.pk).update(last_checked_at=timezone.now(), last_checked_by=self.staff)
        self.assertEqual(self.queries(), short)


class AccessTests(TestCase):
    def test_an_anonymous_visitor_is_sent_to_sign_in(self):
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_a_customer_is_turned_away(self):
        self.client.force_login(User.objects.create_user("gc_customer", password="x"))
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_staff_get_the_page(self):
        self.client.force_login(User.objects.create_user("gc_staff", password="x", is_staff=True))
        self.assertEqual(self.client.get(URL).status_code, 200)


class SharedStylesTests(SimpleTestCase):
    text = source()

    def test_the_page_uses_the_shared_list_styles_once(self):
        self.assertEqual(self.text.count('{% include "arabela_admin/partials/ar_list_styles.html" %}'), 1)
        self.assertNotRegex(self.text, r"(?m)^\s*\.ar-line \{")

    def test_no_column_is_sized_per_row(self):
        rule = re.search(r"\.ar-head\.gc-grid, \.ar-line\.gc-grid \{ grid-template-columns: ([^;]+);", self.text).group(1)
        self.assertNotIn("auto", rule)
        small = re.search(r"@media \(max-width: 1180px\) \{\s*\.ar-head\.gc-grid, \.ar-line\.gc-grid \{ grid-template-columns: ([^;]+);", self.text).group(1)
        self.assertNotIn("auto", small)

    def test_a_small_laptop_drops_the_category_column_and_a_phone_stacks_the_price(self):
        self.assertIn(".gc-cat, .gc-h-cat { display: none; }", self.text)
        self.assertIn('grid-template-areas: "pick gown gown chev" ". price acts acts";', self.text)

    def test_no_blur_on_the_page(self):
        for blur in ("backdrop-blur", "backdrop-filter", "filter: blur"):
            self.assertNotIn(blur, self.text)

    def test_the_file_keeps_its_windows_line_endings(self):
        raw = (ADMIN / "gown-catalog.html").read_bytes()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))
