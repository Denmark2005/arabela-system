"""Security Deposits, redesigned: ONE card (the Active Reservations look) with the search, the Held / Returned counts as filter buttons and the year,
then one short line per booking; a booking with many gowns shows "10 gowns" and opens to one short line per gown; ONE status per booking (the
"Completed / Active" column said the same thing as "Returned / Held"); no page title row, no two number tiles, no fake half-second spinner.

When a deposit counts as returned (Reservation.deposit_returned_on), the receipts window and the counts keep their own tests (tests.py,
test_new_booking, test_cancel_gowns). What is checked here: the numbers each line shows, where each gown is, that every hook the page's scripts
need is still there, that the page asks the database the same number of questions however long the list is, and that the page is still closed
to anyone who is not staff."""
import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from arabela_admin.test_active_dropdown import ActiveDropdownBase, CAN, P, R, RET
from arabela_admin.views import _deposit_gown_state
from reservations.models import ReservationItem, Reservation

User = get_user_model()
URL = reverse("arabela_admin:security_deposits")
ADMIN = Path(settings.BASE_DIR) / "templates" / "arabela_admin"
OVERDUE = ReservationItem.Stage.OVERDUE


class DepositsBase(ActiveDropdownBase):
    def page(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def booking_html(self, html, reservation):
        start = html.index('<div class="ar-booking" data-booking-id="%d"' % reservation.id)
        end = html.find('<div class="ar-booking" data-booking-id=', start + 10)
        return html[start:end if end != -1 else html.index('x-show="matchCount() === 0"', start)]

    def returned_item(self, back_days_ago=1):
        return (-9, -2, RET, {"picked_up_on": self.today - timedelta(days=9), "returned_on": self.today - timedelta(days=back_days_ago)})


class GownStateTests(DepositsBase):
    """_deposit_gown_state: where one gown is, in words (display only)."""

    def state(self, stage, **extra):
        _, [item] = self.book("State", "09170000001", [(2, 5, stage, extra)])
        return _deposit_gown_state(item)

    def test_every_stage_has_its_words(self):
        self.assertEqual(self.state(P), ("waiting", "Waiting for pick-up"))
        self.assertEqual(self.state(R, picked_up_on=self.today), ("out", "Out with the customer"))
        self.assertEqual(self.state(OVERDUE, picked_up_on=self.today - timedelta(days=5)), ("late", "Out with the customer, late"))
        self.assertEqual(self.state(CAN), ("cancelled", ""))

    def test_a_returned_gown_says_the_day_it_came_back(self):
        back = self.today - timedelta(days=3)
        kind, text = self.state(RET, picked_up_on=self.today - timedelta(days=9), returned_on=back)
        self.assertEqual((kind, text), ("returned", f"Returned {back:%b} {back.day}"))


class ClosedLineTests(DepositsBase):
    def test_a_booking_with_many_gowns_shows_the_count_the_names_and_how_many_are_back(self):
        reservation, items = self.book("Many Gowns", "0917 000 0002", [self.returned_item(), self.returned_item(), (-3, 2, R, {"picked_up_on": self.today - timedelta(days=3)}), (3, 6, P, {})])
        response, html = self.page()
        shown = next(r for r in response.context["reservations"] if r.id == reservation.id)
        self.assertEqual((shown.gown_count, shown.cancelled_count, shown.returned_gowns, shown.item_total), (4, 0, 2, 4))
        block = self.booking_html(html, reservation)
        closed = block[:block.index('<div class="ar-panel"')]
        self.assertIn("4 gowns", closed)
        self.assertIn("2 of 4 gowns back", closed)
        self.assertIn('data-status="Held"', block)
        for item in items:
            self.assertIn(item.gown_name, closed)                                   # the names are on the line, cut with "..." when they are long

    def test_a_cancelled_gown_is_not_counted_and_is_still_listed_inside(self):
        reservation, items = self.book("With Cancelled", "09170000003", [self.returned_item(), (3, 6, CAN, {}), (3, 6, CAN, {})])
        response, html = self.page()
        shown = next(r for r in response.context["reservations"] if r.id == reservation.id)
        self.assertEqual((shown.gown_count, shown.cancelled_count, shown.item_total), (1, 2, 3))
        block = self.booking_html(html, reservation)
        closed = block[:block.index('<div class="ar-panel"')]
        self.assertNotIn("gowns</span>", closed)                                    # one live gown: its name, not a count pill
        self.assertIn(items[0].gown_name, closed)
        self.assertIn("2 cancelled", closed)
        panel = block[block.index('<div class="ar-panel"'):]
        self.assertEqual(panel.count('class="ar-gl sd-gl"'), 3)                     # all three are listed
        self.assertEqual(panel.count(">Cancelled</span>"), 2)
        self.assertEqual(panel.count("text-decoration: line-through"), 2)
        self.assertIn('data-status="Returned"', block)                              # the cancelled ones never held it up

    def test_a_one_gown_booking_names_its_gown_and_says_the_gown_is_not_back(self):
        reservation, [item] = self.book("One Gown", "09170000004", [(2, 5, R, {"picked_up_on": self.today})])
        _, html = self.page()
        closed = self.booking_html(html, reservation)
        closed = closed[:closed.index('<div class="ar-panel"')]
        self.assertIn(item.gown_name, closed)
        self.assertNotIn("ar-count-pill", closed)
        self.assertIn("Gown not back yet", closed)
        self.assertIn("<span>Held</span>", closed)

    def test_a_returned_deposit_says_returned_and_the_day(self):
        back = self.today - timedelta(days=2)
        reservation, _ = self.book("Back Home", "09170000005", [self.returned_item(back_days_ago=2)])
        _, html = self.page()
        closed = self.booking_html(html, reservation)
        closed = closed[:closed.index('<div class="ar-panel"')]
        self.assertIn("<span>Returned</span>", closed)
        self.assertIn(f"{back:%b} {back.day}, {back.year}", closed)
        self.assertNotIn("<span>Held</span>", closed)

    def test_the_line_shows_the_deposit_the_date_held_and_the_booking_number(self):
        reservation, _ = self.book("Numbers", "09170000006", [(3, 6, P, {})])
        _, html = self.page()
        closed = self.booking_html(html, reservation)
        closed = closed[:closed.index('<div class="ar-panel"')]
        self.assertIn(f"&#8369;{reservation.security_deposit:,.0f}", closed)
        self.assertIn(f"{reservation.created_at:%b} {reservation.created_at.day}, {reservation.created_at.year}", closed)
        self.assertIn(f"<b>{reservation.reference_code}</b>", closed)

    def test_the_panel_says_where_each_gown_is(self):
        reservation, items = self.book("Where", "09170000007", [self.returned_item(), (-2, 3, R, {"picked_up_on": self.today - timedelta(days=2)}), (3, 6, P, {})])
        _, html = self.page()
        panel = self.booking_html(html, reservation)
        panel = panel[panel.index('<div class="ar-panel"'):]
        self.assertEqual(panel.count('class="ar-gl sd-gl"'), 3)
        for number, item in enumerate(items, start=1):
            self.assertIn(item.gown_name, panel)
            self.assertIn(f"{number} of 3", panel)
        for kind, words in (("returned", "Returned "), ("out", "Out with the customer"), ("waiting", "Waiting for pick-up")):
            self.assertIn(f"sd-gstate sd-gstate--{kind}\">{words}", panel)

    def test_one_status_only(self):
        self.book("Status", "09170000008", [(3, 6, P, {})])
        _, html = self.page()
        for gone in ("Reservation Status", "Deposit Status", ">Completed<", ">Active<", "Returned on "):
            self.assertNotIn(gone, html)
        headers = re.findall(r"<span>([^<]*)</span>", html[html.index('class="ar-head sd-grid"'):html.index('class="ar-head sd-grid"') + 400])
        self.assertEqual([h for h in headers if h], ["Customer", "Gown", "Deposit", "Date held", "Status"])


class FiltersAndSearchTests(DepositsBase):
    def test_the_pills_carry_the_counts_and_the_note_says_how_many_came_back_this_month(self):
        self.book("A", "09170000011", [self.returned_item()])
        self.book("B", "09170000012", [self.returned_item()])
        self.book("C", "09170000013", [(3, 6, P, {})])
        response, html = self.page()
        self.assertEqual((response.context["all_count"], response.context["held_count"], response.context["returned_count"]), (3, 1, 2))
        bar = html[html.index('aria-label="Show deposits by status"'):html.index('class="sd-year-wrap"')]
        self.assertRegex(bar, r">All <span class=\"ar-n\">3</span>")
        self.assertRegex(bar, r"Held <span class=\"ar-n\">1</span>")
        self.assertRegex(bar, r"Returned <span class=\"ar-n\">2</span>")
        self.assertIn(f'{response.context["returned_this_month"]} returned this month</span>', html)

    def test_each_booking_carries_what_the_filters_read(self):
        reservation, [item] = self.book("Search Me", "0917 555 1234", [(3, 6, P, {})])
        _, html = self.page()
        tag = re.search(r'<div class="ar-booking" data-booking-id="%d"[^>]*>' % reservation.id, html).group(0)
        self.assertIn('data-status="Held"', tag)
        self.assertIn(f'data-year="{reservation.created_at:%Y}"', tag)
        haystack = re.search(r'data-search="([^"]*)"', tag).group(1)
        for word in ("search me", reservation.reference_code.lower(), item.gown_name.lower(), "09175551234"):
            self.assertIn(word, haystack)
        self.assertIn('x-show="matches($el)"', tag)

    def test_the_page_still_reads_the_links_the_dashboard_and_the_bell_send(self):
        self.book("Links", "09170000014", [(3, 6, P, {})])
        _, html = self.page()
        self.assertIn("presetSearchParams.get('search')", html)
        self.assertIn("presetSearchParams.get('status')", html)
        self.assertIn("$watch('search', syncFilterParamsToUrl);", html)
        self.assertIn("$watch('statusFilter', syncFilterParamsToUrl);", html)
        self.assertIn("sdSetupBookings($data, $watch);", html)
        self.assertIn("function sdSetupBookings(state, watch)", html)

    def test_the_year_box_and_its_memory_and_toast_are_unchanged(self):
        self.book("Year", "09170000015", [(3, 6, P, {})])
        _, html = self.page()
        self.assertIn("localStorage.getItem('financialReportYear')", html)
        self.assertIn('x-model="yearFilter"', html)
        self.assertIn("yearFilter.replace(/[^0-9]/g, '').slice(0, 4)", html)
        self.assertIn("Year applied across Payments &amp; Deposits", html)

    def test_the_filter_is_decided_in_one_place(self):
        self.book("One Rule", "09170000016", [(3, 6, P, {})])
        _, html = self.page()
        self.assertEqual(html.count("matches(el) {"), 1)
        self.assertIn("matchCount() { return Array.from(document.querySelectorAll('.ar-booking[data-booking-id]')).filter(el => this.matches(el)).length; }", html)


class PageChromeTests(DepositsBase):
    def test_the_title_row_the_two_tiles_and_the_fake_spinner_are_gone(self):
        self.book("Chrome", "09170000021", [(3, 6, P, {})])
        _, html = self.page()
        for gone in ("pageName", "Breadcrumb Start", "Deposits Held", "Returned This Month", "filtering", "filterTimeout", 'style="height: 300px;"'):
            self.assertNotIn(gone, html)
        self.assertNotRegex(html, r'<h2\s[^>]*x-text="pageName"')
        self.assertEqual(html.count('class="sd-card"'), 1)
        self.assertNotIn('<table', html[html.index('class="sd-card"'):html.index('x-show="openReceiptModal"')])

    def test_the_card_is_the_active_reservations_look_with_the_pagers_hooks(self):
        self.book("Look", "09170000022", [(3, 6, P, {})])
        _, html = self.page()
        self.assertIn('data-arpg=".ar-booking" data-arpg-noun="bookings" data-arpg-top="off" data-arpg-count="#ar-count"', html)
        self.assertRegex(html, r'<div class="custom-scrollbar overflow-y-auto" style="max-height: 760px;" data-arpg=')
        self.assertIn('placeholder="Search by customer, booking number or gown..."', html)
        self.assertNotIn(".ar-card { margin-bottom", html)                           # this page draws its own box (.sd-card)

    def test_the_payment_window_and_its_numbers_are_untouched(self):
        reservation, _ = self.book("Window", "09170000023", [(3, 6, P, {})])
        Reservation.objects.filter(pk=reservation.pk).update(gcash_reference="GC-D1", payment_proof_url="https://files.example/d.jpg")
        _, html = self.page()
        button = re.search(r'<button[^>]*title="View payment"[^>]*>', self.booking_html(html, reservation), re.S).group(0)
        for needle in ("openReceiptModal = true", "proofUrl: 'https://files.example/d.jpg'", "receipts: JSON.parse(", "breakdown: JSON.parse(", "gcashRef: 'GC\\u002DD1'"):
            self.assertIn(needle, button)
        self.assertIn('<div @click.outside="openReceiptModal = false" x-effect="if (openReceiptModal)', html)
        self.assertIn("viewingPayment.breakdown ? viewingPayment.breakdown.headline : viewingPayment.amount", html)
        self.assertNotRegex(html[html.index("GCash Payment Proof"):], r">\s*Close\s*</button>")

    def test_the_payment_button_does_not_stop_the_click(self):
        reservation, _ = self.book("Click", "09170000024", [(3, 6, P, {})])
        _, html = self.page()
        block = self.booking_html(html, reservation)
        expected = "@click=\"if (!$event.target.closest('.sd-eye')) { toggleBooking(%d) }\"" % reservation.id
        self.assertIn(expected, block)
        self.assertNotIn("@click.stop", block[:block.index('class="ar-chev"')])


class SameGcashNumberTests(DepositsBase):
    def test_the_deposit_cell_and_the_window_warn_about_a_number_on_another_booking(self):
        a, _ = self.book("Twin A", "09170000031", [(3, 6, P, {})])
        b, _ = self.book("Twin B", "09170000032", [(3, 6, P, {})])
        Reservation.objects.filter(pk__in=[a.pk, b.pk]).update(gcash_reference="GC-5005", payment_proof_url="https://files.example/t.jpg")
        _, html = self.page()
        block = self.booking_html(html, a)
        self.assertIn(f"Same GCash ref as {b.reference_code}", block)
        self.assertIn("gcashDup: '%s'" % b.reference_code.replace("-", "\\u002D"), block)


class FewQuestionsTests(DepositsBase):
    def queries(self):
        self.client.get(URL)                                                          # the first visit also creates the shop-settings row
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(URL).status_code, 200)
        return len(captured.captured_queries)

    def test_a_longer_list_does_not_ask_the_database_more_questions(self):
        for number in range(3):
            reservation, _ = self.book(f"Few {number}", f"0917100000{number}", [self.returned_item(), (3, 6, P, {})])
            Reservation.objects.filter(pk=reservation.pk).update(gcash_reference=f"GC-Q{number}")
        short = self.queries()
        for number in range(3, 15):
            reservation, _ = self.book(f"Many {number}", f"091710000{number:02d}", [self.returned_item(), (3, 6, P, {}), (4, 7, CAN, {}), (5, 8, P, {})])
            Reservation.objects.filter(pk=reservation.pk).update(gcash_reference=f"GC-Q{number}")
        self.assertEqual(self.queries(), short)


class AccessTests(TestCase):
    """The decorator that closes the page to everyone who is not staff sits on the page's own function (a helper placed above it once took
    the decorator by accident, on another page), so this is checked on its own."""

    def test_an_anonymous_visitor_is_sent_to_sign_in(self):
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_a_customer_is_turned_away(self):
        self.client.force_login(User.objects.create_user("sd_customer", password="x"))
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_staff_get_the_page(self):
        self.client.force_login(User.objects.create_user("sd_staff", password="x", is_staff=True))
        self.assertEqual(self.client.get(URL).status_code, 200)

    def test_the_helper_is_a_plain_function_not_a_page(self):
        self.assertFalse(hasattr(_deposit_gown_state, "__wrapped__"))

    def test_an_empty_list_says_so(self):
        self.client.force_login(User.objects.create_user("sd_staff2", password="x", is_staff=True))
        self.assertIn("No security deposits on file yet.", self.client.get(URL).content.decode())


class SharedStylesTests(SimpleTestCase):
    def test_the_page_uses_the_shared_list_styles_and_its_own_box_rule(self):
        text = (ADMIN / "security-deposits.html").read_text(encoding="utf8")
        self.assertEqual(text.count('{% include "arabela_admin/partials/ar_list_styles.html" %}'), 1)
        self.assertIn(".sd-card { margin-bottom: 1.25rem;", text)
        self.assertNotIn(".ar-line {", text)

    def test_no_column_is_sized_per_row(self):
        text = (ADMIN / "security-deposits.html").read_text(encoding="utf8")
        rule = re.search(r"\.ar-head\.sd-grid, \.ar-line\.sd-grid \{ grid-template-columns: ([^;]+); \}", text).group(1)
        self.assertNotIn("auto", rule)                                               # an auto column would stop the titles lining up with the rows
