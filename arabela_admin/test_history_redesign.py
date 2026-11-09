"""Rental History, redesigned: ONE card (the Security Deposits / Active Reservations look) with the search, the month and the year, the four status
buttons (they are the legend too, and the number on each is the count), the "Showing 1-15 of 50 rentals" line instead of the old "Total Rentals"
tile, then one short line per gown with a status circle, an eye icon for the proof, and the timeline inside the opened row. No two-button column,
no status word chip column, no fake half-second spinner.

What is checked here: the status each gown gets (circle + sentence), what the closed line says and does NOT say, that every hook the page's
scripts need is still there (the proof window, the records JSON, the pager), the dates line, and that the page is still closed to anyone who is
not staff. The proof window itself and the timeline markup keep their own tests (test_new_booking, test_modal_closers, tests.py)."""
import json
import re
from datetime import date, timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from arabela_admin.test_active_dropdown import ActiveDropdownBase, P, R, RET
from arabela_admin.views import _HISTORY_LOOK
from reservations.models import ReservationItem

User = get_user_model()
URL = reverse("arabela_admin:rental_history")
TEMPLATE = Path(settings.BASE_DIR) / "templates" / "arabela_admin" / "rental-history.html"


class HistoryBase(ActiveDropdownBase):
    def page(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def row_html(self, html, item):
        start = html.index('<div class="ar-booking" data-row-id="%d"' % item.id)
        end = html.find('<div class="ar-booking" data-row-id=', start + 10)
        return html[start:end if end != -1 else html.index('x-show="matchCount() === 0"', start)]

    def closed(self, html, item):
        block = self.row_html(html, item)
        return block[:block.index('<div class="ar-panel"')]

    def one(self, name, pickup, ret, stage, **extra):
        """One booking with one gown -> (reservation, item)."""
        reservation, [item] = self.book(name, "09170000000", [(pickup, ret, stage, extra)])
        return reservation, item

    def back(self, name="Back Home"):
        return self.one(name, -9, -4, RET, picked_up_on=self.today - timedelta(days=9), returned_on=self.today - timedelta(days=4))


class StatusTests(HistoryBase):
    """The four words, their circle and the one sentence that explains each."""

    def test_every_status_gets_its_circle_and_its_sentence(self):
        _, done = self.back("Done")
        _, ongoing = self.one("Waiting", 2, 6, P)
        _, out_on_time = self.one("Out", -1, 3, R, picked_up_on=self.today - timedelta(days=1))
        _, late = self.one("Late", -9, -2, R, picked_up_on=self.today - timedelta(days=9))
        _, missed = self.one("Missed", -9, -5, P)
        response, _ = self.page()
        shown = {i.id: i for i in response.context["rental_items"]}
        expected = {done: "Completed", ongoing: "Ongoing", out_on_time: "Ongoing", late: "Overdue", missed: "Not picked up"}
        for item, status in expected.items():
            with self.subTest(item=item.reservation.customer_name):
                got = shown[item.id]
                self.assertEqual(got.history_status, status)
                self.assertEqual((got.history_kind, got.history_tip), _HISTORY_LOOK[status])

    def test_the_four_statuses_each_have_a_different_circle(self):
        self.assertEqual(sorted(kind for kind, _ in _HISTORY_LOOK.values()), ["active", "done", "missed", "overdue"])
        self.assertTrue(all(tip for _, tip in _HISTORY_LOOK.values()))

    def test_the_records_the_buttons_count_carry_the_status_of_every_row_in_order(self):
        self.back("One")
        self.one("Two", 2, 6, P)
        self.one("Three", -9, -2, R, picked_up_on=self.today - timedelta(days=9))
        response, html = self.page()
        records = json.loads(re.search(r'<script id="rental-records-data" type="application/json">(.*?)</script>', html, re.S).group(1))
        self.assertEqual([r["status"] for r in records], [i.history_status for i in response.context["rental_items"]])
        self.assertEqual(sorted(r["status"] for r in records), ["Completed", "Ongoing", "Overdue"])
        for record in records:
            self.assertEqual(set(record), {"month", "year", "status", "q"})


class OneCardTests(HistoryBase):
    def test_the_tile_the_table_and_the_two_buttons_are_gone(self):
        self.back()
        _, html = self.page()
        self.assertNotIn("Total Rentals", html)
        self.assertNotIn("<table", html[html.index("<main>"):])
        self.assertNotIn("View Proof", html)
        self.assertNotIn("Hide Timeline", html)
        self.assertNotIn("filtering", html)                                          # the fake half-second spinner
        self.assertNotIn("openTimeline", html)

    def test_it_is_one_card_with_the_search_the_month_the_year_the_buttons_and_the_count(self):
        self.back()
        _, html = self.page()
        main = html[html.index("<main>"):]
        self.assertEqual(main.count('class="rh-card"'), 1)
        self.assertEqual(main.count('class="ar-toolbar"'), 1)
        for piece in ('placeholder="Search by customer or gown..."', 'x-model="monthFilter"', 'x-model="yearFilter"', 'id="ar-count"',
                      'data-arpg=".ar-booking"', 'data-arpg-noun="rentals"', 'data-arpg-count="#ar-count"'):
            self.assertEqual(main.count(piece), 1, piece)
        self.assertEqual(html.count('data-arpg='), 1)                               # the pager opts in exactly once

    def test_the_status_buttons_are_the_legend_and_each_shows_its_own_count(self):
        self.back()
        _, html = self.page()
        group = html[html.index('role="group" aria-label="Show rentals by status"'):html.index('id="ar-count"')]
        for status, dot in (("All", None), ("Ongoing", "ar-dot--active"), ("Completed", "ar-dot--done"), ("Overdue", "ar-dot--overdue"), ("Not picked up", "rh-dot--missed")):
            with self.subTest(status=status):
                self.assertIn("statusFilter = '%s'" % status, group)
                self.assertIn("x-text=\"countFor('%s')\"" % status, group)
                if dot:
                    self.assertIn(dot, group)
        self.assertEqual(group.count('class="ar-filter'), 5)
        self.assertIn("title=", group)                                                # hover words

    def test_with_no_rentals_it_says_so_and_still_draws_the_card(self):
        _, html = self.page()
        self.assertIn("No rentals recorded yet", html)
        self.assertNotIn('data-row-id="', html)
        self.assertNotIn("No rental records found for this period", html)            # that one only exists when rows exist


class ClosedLineTests(HistoryBase):
    def test_the_line_shows_the_name_the_gown_the_dates_and_the_amount_and_not_the_booking_number(self):
        reservation, item = self.one("Lorna Reyes", 2, 6, P)
        _, html = self.page()
        line = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", self.closed(html, item)))
        for piece in ("Lorna Reyes", item.gown_name, "&#8369;3,000"):
            self.assertIn(piece, line)
        start, end = self.today + timedelta(days=2), self.today + timedelta(days=6)
        self.assertIn(f"{start:%b} {start.day}", line)
        self.assertIn("&rarr;", line)
        self.assertIn(f"{end:%b} {end.day}, {end.year}", line)
        self.assertNotIn(reservation.reference_code, line)                          # the booking number lives in the opened row

    def test_the_year_is_written_once_unless_the_rental_crosses_new_year(self):
        _, same = self.one("Same Year", 2, 6, P)
        _, cross = self.one("Cross Year", 2, 6, P)
        ReservationItem.objects.filter(id=cross.id).update(rental_date=date(2025, 12, 29), return_date=date(2026, 1, 2))
        ReservationItem.objects.filter(id=same.id).update(rental_date=date(2026, 3, 3), return_date=date(2026, 3, 7))
        _, html = self.page()
        self.assertIn("Mar 3 → Mar 7, 2026", re.sub(r"\s+", " ", self.closed(html, same)).replace("&rarr;", "→"))
        self.assertIn("Dec 29, 2025 → Jan 2, 2026", re.sub(r"\s+", " ", self.closed(html, cross)).replace("&rarr;", "→"))

    def test_late_says_on_time_n_days_late_or_a_dash(self):
        _, on_time = self.back("On Time")
        _, late = self.one("Late", -9, -2, R, picked_up_on=self.today - timedelta(days=9))
        _, waiting = self.one("Waiting", 2, 6, P)
        _, html = self.page()
        self.assertIn("On time", self.closed(html, on_time))
        self.assertIn("2 days late", self.closed(html, late))
        self.assertIn("Still not back", self.closed(html, late))                    # the hover: this number is still growing
        waiting_line = self.closed(html, waiting)
        self.assertNotIn("rh-chip", waiting_line.split('class="rh-late"')[1].split("rh-amt")[0])
        self.assertIn("&mdash;", waiting_line)

    def test_the_circle_has_the_status_in_words_for_hover_and_screen_readers(self):
        _, done = self.back()
        _, missed = self.one("Missed", -9, -5, P)
        _, html = self.page()
        self.assertIn('class="ar-dot ar-dot--done" role="img" aria-label="Completed"', self.closed(html, done))
        self.assertIn('class="ar-dot rh-dot--missed" role="img" aria-label="Not picked up"', self.closed(html, missed))
        self.assertIn("The gown is back", self.closed(html, done))

    def test_the_eye_still_opens_the_proof_window_with_everything_it_needs(self):
        _, item = self.back("Proof Person")
        _, html = self.page()
        line = self.closed(html, item)
        eye_at = line.index('class="ar-ib rh-eye"')
        eye = line[max(0, eye_at - 1500):eye_at]
        for piece in ("viewingRental = {", "proofUrl:", "receipts: JSON.parse(", "reference:", "openProofModal = true"):
            self.assertIn(piece, eye)
        self.assertIn('title="View proof"', line)
        self.assertIn('aria-label="View proof of Proof Person', line)
        self.assertIn("openProofModal", html[html.index('x-show="openProofModal"'):])   # the window is still on the page

    def test_a_click_on_the_line_opens_it_but_not_a_click_on_the_eye(self):
        _, item = self.back()
        _, html = self.page()
        self.assertIn("if (!$event.target.closest('.rh-eye')) { toggleRow(%d) }" % item.id, self.closed(html, item))


class OpenedRowTests(HistoryBase):
    def test_the_opened_row_has_the_status_in_words_the_returned_day_the_booking_and_the_timeline(self):
        reservation, item = self.back("Opened Person")
        _, html = self.page()
        block = self.row_html(html, item)
        panel = block[block.index('<div class="ar-panel"'):]
        self.assertIn('id="rh-panel-%d"' % item.id, panel)
        self.assertIn("Completed", panel)
        self.assertIn("The gown is back", panel)
        back = self.today - timedelta(days=4)
        self.assertIn(f"{back:%b} {back.day}, {back.year}", panel)
        self.assertIn(reservation.reference_code, panel)
        self.assertIn('<div class="arb-tl-panel">', panel)

    def test_booked_as_shows_inside_only_when_the_account_booked_under_another_name(self):
        reservation, item = self.one("Typed Name", 2, 6, P)
        type(reservation).objects.filter(id=reservation.id).update(customer=self.customer)
        _, html = self.page()
        panel = self.row_html(html, item)
        self.assertIn("Booked as", panel[panel.index('<div class="ar-panel"'):])
        _, plain = self.one("Walk In Name", 2, 6, P)
        _, html = self.page()
        self.assertNotIn("Booked as", self.row_html(html, plain)[self.row_html(html, plain).index('<div class="ar-panel"'):])

    def test_every_row_carries_what_the_filters_read(self):
        reservation, item = self.one("Filter Me", 2, 6, P)
        _, html = self.page()
        block = self.row_html(html, item)
        start = self.today + timedelta(days=2)
        self.assertIn('data-status="Ongoing"', block)
        self.assertIn('data-month="%s"' % start.strftime("%b"), block)
        self.assertIn('data-year="%d"' % start.year, block)
        self.assertIn('data-search="filter me %s"' % item.gown_name.lower(), block)
        self.assertIn('x-show="matches($el)"', block)


class StaticTemplateTests(SimpleTestCase):
    def setUp(self):
        self.source = TEMPLATE.read_text(encoding="utf8")

    def test_the_page_state_has_the_filters_and_no_leftovers(self):
        for piece in ("statusFilter: 'All'", "monthFilter: 'All'", "yearFilter: ''", "openRows: []", "matches(el)", "matchCount()", "countFor(status)",
                      "isRowOpen(id)", "toggleRow(id)", "presetMonth", "presetYear", "rental-records-data"):
            self.assertIn(piece, self.source, piece)
        for gone in ("filtering", "filterTimeout", "openTimeline", "totalRentals", "Total Rentals"):
            self.assertNotIn(gone, self.source, gone)

    def test_the_shared_list_look_is_included_once_and_the_page_keeps_its_own_container(self):
        self.assertEqual(self.source.count('{% include "arabela_admin/partials/ar_list_styles.html" %}'), 1)
        self.assertEqual(self.source.count("    .rh-card {"), 1)

    def test_the_file_keeps_its_line_endings(self):
        raw = TEMPLATE.read_bytes()
        self.assertEqual(raw.count(b"\r\n"), 0)                                      # this template is LF, like the rest of the admin pages except four


class AccessTests(TestCase):
    """The decorator that closes the page to everyone who is not staff sits on the page's own function."""

    def test_an_anonymous_visitor_is_sent_to_sign_in(self):
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_a_customer_is_turned_away(self):
        self.client.force_login(User.objects.create_user("rh_customer", password="x"))
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_staff_get_the_page(self):
        self.client.force_login(User.objects.create_user("rh_staff", password="x", is_staff=True))
        self.assertEqual(self.client.get(URL).status_code, 200)
