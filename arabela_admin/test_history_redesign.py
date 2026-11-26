"""Rental History's list: ONE card (the Security Deposits / Active Reservations look) with the search and the list filters, a button per status
(they are the legend too, and the number on each is the count), the "Showing 1-15 of 50 rentals" line, then one line per gown: the booking number,
the dates, the status in words with a short line under it, the price, an eye only when there is something to show, and an opened row with the status
sentence, three steps (Booked, Pick-up, Return), the facts, the buttons and the history. The report above the list has its own tests
(test_rental_report).

What is checked here: the status each gown gets (words, colour, steps) in the same wording Active Reservations uses, the money and event facts,
where the buttons lead, the eye, that the page updates itself, that every hook the page's scripts need is still there (the photo window, the
records JSON, the pager) and that the page is still closed to anyone who is not staff. The photo window itself and the timeline markup keep
their own tests (test_new_booking, test_modal_closers, tests.py)."""
import json
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from arabela_admin import rental_history
from arabela_admin.test_active_dropdown import ActiveDropdownBase, P, R, RET
from reservations import payments as reservation_payments
from reservations.models import Payment, ReceiptRecord, Reservation, ReservationItem

User = get_user_model()
URL = reverse("arabela_admin:rental_history")
TEMPLATE = Path(settings.BASE_DIR) / "templates" / "arabela_admin" / "rental-history.html"

PENDING, WAITING, OUT, OVERDUE, RETURNED, MISSED = (
    "Pending approval", "Waiting for pick-up", "Out with customer", "Overdue", "Returned", "Not picked up")


class HistoryBase(ActiveDropdownBase):
    def page(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def views(self):
        """{item id: the item as the page shows it}."""
        response, _ = self.page()
        return {i.id: i for i in response.context["rental_items"]}

    def row_html(self, html, item):
        start = re.search(r'<div class="ar-booking(?: is-overdue)?" data-row-id="%d"' % item.id, html).start()
        end = re.search(r'<div class="ar-booking(?: is-overdue)?" data-row-id=', html[start + 10:])
        return html[start:start + 10 + end.start() if end else html.index('x-show="matchCount() === 0"', start)]

    def closed(self, html, item):
        block = self.row_html(html, item)
        return block[:block.index('<div class="ar-panel"')]

    def panel(self, html, item):
        block = self.row_html(html, item)
        return block[block.index('<div class="ar-panel"'):]

    def words(self, fragment):
        """The visible words of a piece of the page, one space between them."""
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()

    def one(self, name, pickup, ret, stage, channel=Reservation.Channel.ONLINE, **extra):
        """One booking with one gown -> (reservation, item)."""
        reservation, [item] = self.book(name, "09170000000", [(pickup, ret, stage, extra)], channel=channel)
        return reservation, item

    def back(self, name="Back Home", **kwargs):
        return self.one(name, -9, -4, RET, picked_up_on=self.today - timedelta(days=9), returned_on=self.today - timedelta(days=4), **kwargs)

    def weekday(self, day):
        """The way the page writes a day in the headline: 'Mon, Nov 30', with the year only when it is another year."""
        return f"{day:%a}, {day:%b} {day.day}" + ("" if day.year == self.today.year else f", {day.year}")

    def offset(self, days):
        return self.today + timedelta(days=days)

    def every_kind(self):
        """One booking of each status -> {status: item}."""
        pending_res, pending = self.one("Pending Person", 3, 7, P)
        Reservation.objects.filter(pk=pending_res.pk).update(status=Reservation.Status.PENDING, reviewed_at=None)
        _, waiting = self.one("Waiting Person", 2, 6, P)
        _, out = self.one("Out Person", -1, 3, R, picked_up_on=self.offset(-1))
        _, late = self.one("Late Person", -9, -2, R, picked_up_on=self.offset(-9))
        _, done = self.back("Done Person")
        _, missed = self.one("Missed Person", -9, -5, P)
        return {PENDING: pending, WAITING: waiting, OUT: out, OVERDUE: late, RETURNED: done, MISSED: missed}


class StatusTests(HistoryBase):
    def test_every_gown_gets_its_status_circle_and_sentence(self):
        items = self.every_kind()
        shown = self.views()
        for status, item in items.items():
            with self.subTest(status=status):
                view = shown[item.id].view
                self.assertEqual((view["status"], shown[item.id].history_status), (status, status))
                kind, tip = rental_history.LOOK[status]
                self.assertEqual((view["kind"], view["tip"], view["dot"]), (kind, tip, rental_history.DOTS[kind]))

    def test_the_six_statuses_each_have_their_own_colour_and_circle(self):
        self.assertEqual(list(rental_history.LOOK), [PENDING, WAITING, OUT, OVERDUE, RETURNED, MISSED])      # the order of the buttons
        kinds = [kind for kind, _ in rental_history.LOOK.values()]
        self.assertEqual(len(set(kinds)), 6)
        self.assertEqual(len({rental_history.DOTS[kind] for kind in kinds}), 6)
        self.assertTrue(all(tip for _, tip in rental_history.LOOK.values()))
        css = TEMPLATE.read_text(encoding="utf8")
        for kind in kinds:
            self.assertIn(f".rh-chip--{kind} {{", css, kind)

    def test_the_short_line_under_the_status_uses_the_words_of_active_reservations(self):
        items = self.every_kind()
        _, today_pickup = self.one("Today Pickup", 0, 4, P)
        _, late_pickup = self.one("Late Pickup", -1, 3, P)
        _, due_today = self.one("Due Today", -4, 0, R, picked_up_on=self.offset(-4))
        shown = self.views()
        expected = {
            items[PENDING]: ("Waiting for approval", f"Waiting for approval · pick-up {self.weekday(self.offset(3))}"),
            items[WAITING]: ("Pick-up in 2 days", f"Pick-up in 2 days · {self.weekday(self.offset(2))}"),
            today_pickup: ("Pick up today", "Pick up today, 1:00 PM to 5:00 PM"),
            late_pickup: ("1 day late for pick-up", f"1 day late for pick-up · was due {self.weekday(self.offset(-1))}"),
            items[OUT]: ("Return in 3 days", f"Due back in 3 days · {self.weekday(self.offset(3))}"),
            due_today: ("Return due today", "Due back today"),
            items[OVERDUE]: ("2 days overdue", f"2 days overdue · was due {self.weekday(self.offset(-2))}"),
            items[RETURNED]: ("On time", f"Returned {self.weekday(self.offset(-4))} · on time"),
            items[MISSED]: ("Never picked up", f"Never picked up · the return date was {self.weekday(self.offset(-5))}"),
        }
        for item, (note, headline) in expected.items():
            with self.subTest(item=item.reservation.customer_name):
                self.assertEqual((shown[item.id].view["note"], shown[item.id].view["headline"]), (note, headline))

    def test_the_old_return_stage_past_its_date_is_overdue_like_everywhere_else(self):
        _, legacy = self.one("Legacy Stage", -9, -2, ReservationItem.Stage.RETURN, picked_up_on=self.offset(-9))
        self.assertEqual(self.views()[legacy.id].history_status, OVERDUE)

    def test_a_rejected_or_cancelled_booking_never_appears(self):
        for status in (Reservation.Status.REJECTED, Reservation.Status.CANCELLED):
            reservation, _ = self.one(f"{status} Person", 2, 6, P)
            Reservation.objects.filter(pk=reservation.pk).update(status=status)
        response, _ = self.page()
        self.assertEqual(response.context["rental_items"], [])

    def test_the_records_the_buttons_count_carry_the_status_of_every_row_in_order(self):
        self.every_kind()
        response, html = self.page()
        records = json.loads(re.search(r'<script id="rental-records-data" type="application/json">(.*?)</script>', html, re.S).group(1))
        self.assertEqual([r["status"] for r in records], [i.history_status for i in response.context["rental_items"]])
        self.assertEqual(sorted(r["status"] for r in records), sorted(rental_history.LOOK))
        for record in records:
            self.assertEqual(set(record), {"status", "q", "category", "channel", "gown"})          # what the list filters read


class StepTests(HistoryBase):
    """Booked, Pick-up, Return: the state of each step and of the line leading to it."""

    def test_the_steps_of_every_status(self):
        items = self.every_kind()
        _, late_pickup = self.one("Late Pickup", -1, 3, P)
        shown = self.views()
        expected = {
            items[PENDING]: [("done", ""), ("todo", "todo"), ("todo", "todo")],
            items[WAITING]: [("done", ""), ("now", "done"), ("todo", "todo")],
            late_pickup: [("done", ""), ("bad", "bad"), ("todo", "todo")],
            items[OUT]: [("done", ""), ("done", "done"), ("now", "done")],
            items[OVERDUE]: [("done", ""), ("done", "done"), ("bad", "bad")],
            items[RETURNED]: [("done", ""), ("done", "done"), ("done", "done")],
            items[MISSED]: [("done", ""), ("bad", "bad"), ("todo", "todo")],
        }
        for item, states in expected.items():
            with self.subTest(item=item.reservation.customer_name):
                steps = shown[item.id].view["steps"]
                self.assertEqual([s["name"] for s in steps], ["Booked", "Pick-up", "Return"])
                self.assertEqual([(s["state"], s["line"]) for s in steps], states)

    def test_a_moved_date_says_the_day_it_was_first_booked_for(self):
        _, item = self.one("Moved Person", 4, 10, P, original_rental_date=self.today, original_return_date=self.offset(4))
        view = self.views()[item.id].view
        pickup, back = view["steps"][1], view["steps"][2]
        self.assertEqual((pickup["date"], pickup["was"]), (f"due {self.offset(4):%b} {self.offset(4).day}", f"was {self.today:%b} {self.today.day}"))
        self.assertEqual(back["was"], f"was {self.offset(4):%b} {self.offset(4).day}")

    def test_the_steps_are_drawn_with_a_check_a_warning_and_the_current_one_marked(self):
        _, late = self.one("Late Person", -9, -2, R, picked_up_on=self.offset(-9))
        _, html = self.page()
        panel = self.panel(html, late)
        self.assertEqual(panel.count('<li class="rh-step '), 3)
        self.assertIn('<li class="rh-step is-done">', panel)
        self.assertIn('<li class="rh-step is-done rh-line--done">', panel)
        self.assertIn('<li class="rh-step is-bad rh-line--bad" aria-current="step">', panel)
        self.assertIn('M20 6L9 17l-5-5', panel)          # the check of a finished step
        self.assertIn("2 days overdue", self.words(panel))


class FactTests(HistoryBase):
    def fact(self, html, item, name):
        """The words under one fact of the opened row, or None when that fact is not there."""
        match = re.search(r"<dt>%s</dt><dd[^>]*>(.*?)</dd>" % name, self.panel(html, item), re.S)
        return self.words(match.group(1)) if match else None

    def test_the_money_and_the_deposit_follow_the_booking(self):
        walk_res, walk = self.back("Walk Unpaid", channel=Reservation.Channel.WALK_IN)       # nothing paid at all
        online_res, online = self.back("Online Back")                                       # the deposit was paid online
        held_res, held = self.one("Still Out", -1, 3, R, picked_up_on=self.offset(-1))
        _, html = self.page()
        self.assertEqual((self.fact(html, walk, "Payment"), self.fact(html, walk, "Security deposit")), ("Balance ₱5,000.00 to pay", "Not paid yet"))
        self.assertEqual((self.fact(html, online, "Payment"), self.fact(html, online, "Security deposit")),
                         ("Balance ₱3,000.00 to pay", "Return due ₱2,000.00"))
        self.assertEqual(self.fact(html, held, "Security deposit"), "Held ₱2,000.00")
        reservation_payments.record_payment(reservation_id=online_res.pk, purpose=Payment.Purpose.BALANCE, amount=Decimal("3000"),
                                            method=Payment.Method.CASH, user=self.staff)
        reservation_payments.return_deposit(reservation_id=online_res.pk, amount=Decimal("2000"), method=Payment.Method.CASH, user=self.staff)
        _, html = self.page()
        self.assertEqual((self.fact(html, online, "Payment"), self.fact(html, online, "Security deposit")), ("Paid in full", "Returned"))

    def test_a_pending_booking_shows_the_payment_check_and_no_deposit(self):
        reservation, item = self.one("Pending Person", 3, 7, P)
        Reservation.objects.filter(pk=reservation.pk).update(status=Reservation.Status.PENDING, reviewed_at=None)
        _, html = self.page()
        self.assertEqual(self.fact(html, item, "Payment"), Reservation.objects.get(pk=reservation.pk).payment_state)
        self.assertIsNone(self.fact(html, item, "Security deposit"))
        self.assertIsNone(self.fact(html, item, "Fees"))

    def test_the_fees_are_named_and_a_gown_never_picked_up_has_none(self):
        late_res, late = self.one("Late Person", -9, -2, R, picked_up_on=self.offset(-9))
        moved_res, moved = self.one("Moved Person", -3, 6, R, picked_up_on=self.offset(-3), original_return_date=self.offset(2))
        missed_res, missed = self.one("Missed Person", -9, -5, P, original_return_date=self.offset(-7))
        Reservation.objects.filter(pk__in=[late_res.pk, moved_res.pk, missed_res.pk]).update(late_fee_per_day=200)
        _, html = self.page()
        self.assertEqual(self.fact(html, late, "Fees"), "Overdue fee ₱400.00")
        self.assertEqual(self.fact(html, moved, "Fees"), "Date change fee ₱800.00")
        self.assertIsNone(self.fact(html, missed, "Fees"))

    def test_the_event_day_shows_what_it_was_and_warns_when_it_is_outside_the_rental(self):
        _, item = self.one("Event Person", 4, 8, P, original_return_date=self.offset(5))
        ReservationItem.objects.filter(pk=item.pk).update(event_date=self.offset(1))           # the pick-up was moved past the event day
        _, html = self.page()
        shown = self.fact(html, item, "Event")
        first = self.offset(1)
        self.assertEqual(shown, f"{self.weekday(first)} was {self.offset(3):%b} {self.offset(3).day} Before pick-up: set the new date in Active Reservations")
        ReservationItem.objects.filter(pk=item.pk).update(event_date=self.offset(6))
        _, html = self.page()
        self.assertEqual(self.fact(html, item, "Event"), f"{self.weekday(self.offset(6))} was {self.offset(3):%b} {self.offset(3).day}")

    def test_the_rental_price_and_the_cellphone_are_there(self):
        _, item = self.back("Price Person")
        _, html = self.page()
        self.assertEqual(self.fact(html, item, "Rental price"), "&#8369;3,000")
        self.assertIn('<a href="tel:09170000000">09170000000</a>', self.panel(html, item))


class ButtonTests(HistoryBase):
    def link_of(self, html, item):
        match = re.search(r'<a class="rh-btn rh-btn--main" href="([^"]*)">(.*?)</a>', self.panel(html, item), re.S)
        return self.words(match.group(2)), match.group(1)

    def test_the_main_button_leads_to_the_page_where_staff_act_on_the_booking(self):
        items = self.every_kind()
        _, html = self.page()
        code = lambda item: item.reservation.reference_code
        where = {
            PENDING: ("Open in Pending Approval", reverse("arabela_admin:pending_approval")),
            WAITING: ("Open in Active Reservations", reverse("arabela_admin:active_reservations")),
            OUT: ("Open in Active Reservations", reverse("arabela_admin:active_reservations")),
            OVERDUE: ("Open in Active Reservations", reverse("arabela_admin:active_reservations")),
            MISSED: ("Open in Active Reservations", reverse("arabela_admin:active_reservations")),
            RETURNED: ("Open in All Bookings", reverse("arabela_admin:reservation_records")),
        }
        for status, (label, address) in where.items():
            with self.subTest(status=status):
                self.assertEqual(self.link_of(html, items[status]), (label, f"{address}?search={code(items[status])}"))

    def test_a_returned_gown_of_a_booking_still_running_leads_to_active_reservations(self):
        reservation, [back, still] = self.book("Two Gowns", "09170000000", [
            (-9, -4, RET, {"picked_up_on": self.offset(-9), "returned_on": self.offset(-4)}), (-1, 3, R, {"picked_up_on": self.offset(-1)})])
        _, html = self.page()
        self.assertEqual(self.link_of(html, back)[0], "Open in Active Reservations")

    def test_the_receipt_button_appears_only_when_there_is_a_receipt(self):
        reservation, item = self.back("Receipt Person")
        _, html = self.page()
        self.assertNotIn(f"arabelaReceipt.open({reservation.id})", self.panel(html, item))
        reservation_payments.record_payment(reservation_id=reservation.pk, purpose=Payment.Purpose.BALANCE, amount=Decimal("3000"),
                                            method=Payment.Method.CASH, user=self.staff)
        _, html = self.page()
        self.assertIn(f'<button type="button" class="rh-btn" @click="arabelaReceipt.open({reservation.id})">', self.panel(html, item))

    def test_the_history_button_opens_and_closes_the_whole_timeline(self):
        _, item = self.back("History Person")
        _, html = self.page()
        panel = self.panel(html, item)
        self.assertIn(f'@click="toggleHistory({item.id})"', panel)
        self.assertIn(f'aria-controls="rh-history-{item.id}"', panel)
        self.assertIn(f'<div class="rh-history" id="rh-history-{item.id}" x-show="isHistoryOpen({item.id})" x-cloak x-slide>', panel)
        self.assertIn('<div class="arb-tl-panel">', panel)


class EyeTests(HistoryBase):
    def test_no_eye_when_there_is_nothing_to_show(self):
        _, item = self.back("Nothing Person")
        _, html = self.page()
        line = self.closed(html, item)
        self.assertNotIn('class="ar-ib rh-eye"', line)
        self.assertIn('<span class="rh-noeye" aria-hidden="true"></span>', line)

    def test_the_eye_opens_the_receipt_when_the_shop_issued_one(self):
        reservation, item = self.back("Receipt Person")
        reservation_payments.record_payment(reservation_id=reservation.pk, purpose=Payment.Purpose.BALANCE, amount=Decimal("1000"),
                                            method=Payment.Method.CASH, user=self.staff)
        _, html = self.page()
        line = self.closed(html, item)
        self.assertIn(f'@click="arabelaReceipt.open({reservation.id})" class="ar-ib rh-eye" title="View receipt"', line)
        self.assertNotIn("openProofModal = true", line)

    def test_the_eye_opens_the_photo_window_for_a_payment_photo_with_everything_it_needs(self):
        reservation, item = self.back("Photo Person")
        Reservation.objects.filter(pk=reservation.pk).update(payment_proof_url="https://example.com/proof.png")
        _, html = self.page()
        line = self.closed(html, item)
        eye_at = line.index('class="ar-ib rh-eye"')
        eye = line[max(0, eye_at - 1500):eye_at]
        for piece in ("viewingRental = {", "proofUrl: 'https://example.com/proof.png'", "receipts: JSON.parse(", "reference:", "openProofModal = true",
                      "status: 'Returned'"):
            self.assertIn(piece, eye)
        self.assertIn('title="View payment photo"', line)
        self.assertIn('aria-label="View the payment photo of Photo Person', line)
        self.assertIn("openProofModal", html[html.index('x-show="openProofModal"'):])   # the window is still on the page

    def test_an_old_receipt_photo_counts_as_a_photo(self):
        reservation, item = self.back("Old Photo", channel=Reservation.Channel.WALK_IN)
        ReceiptRecord.objects.create(reservation=reservation, photo_url="https://example.com/old.jpg", covers=ReceiptRecord.Covers.BOTH)
        _, html = self.page()
        self.assertIn('title="View payment photo"', self.closed(html, item))

    def test_a_click_on_the_line_opens_it_but_not_a_click_on_the_eye(self):
        _, item = self.back()
        _, html = self.page()
        self.assertIn("if (!$event.target.closest('.rh-eye')) { toggleRow(%d) }" % item.id, self.closed(html, item))


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
        self.assertNotIn(">Ongoing<", html)
        self.assertNotIn(">Completed<", html)

    def test_it_is_one_card_with_the_search_the_list_filters_the_buttons_and_the_count(self):
        self.back()
        _, html = self.page()
        main = html[html.index("<main>"):]
        self.assertEqual(main.count('class="rh-card"'), 1)
        self.assertEqual(main.count('class="ar-toolbar"'), 1)
        for piece in ('placeholder="Search by customer, gown or booking no..."', 'x-model="categoryFilter"', 'x-model="channelFilter"', 'id="ar-count"',
                      'data-arpg=".ar-booking"', 'data-arpg-noun="rentals"', 'data-arpg-count="#ar-count"'):
            self.assertEqual(main.count(piece), 1, piece)
        self.assertEqual(html.count('data-arpg='), 1)                               # the pager opts in exactly once

    def test_the_status_buttons_are_the_legend_and_each_shows_its_own_count(self):
        self.back()
        _, html = self.page()
        group = html[html.index('role="group" aria-label="Show rentals by status"'):html.index('id="ar-count"')]
        self.assertIn("x-text=\"countFor('All')\"", group)
        for status, (kind, tip) in rental_history.LOOK.items():
            with self.subTest(status=status):
                self.assertIn("statusFilter = '%s'" % status, group)
                self.assertIn("x-text=\"countFor('%s')\"" % status, group)
                self.assertIn('title="%s"' % tip, group)
                self.assertIn(rental_history.DOTS[kind], group)
        self.assertEqual(group.count('class="ar-filter'), 7)
        self.assertEqual(group.count("ar-filter--hot"), 1)                          # only Overdue shouts

    def test_the_list_and_the_counts_update_by_themselves(self):
        self.back()
        _, html = self.page()
        self.assertIn('data-arpg-count="#ar-count" data-live-region="history-list"', html)
        self.assertIn('<span hidden data-live-data="history-records" data-live-source="#rental-records-data"', html)
        self.assertIn("window.ArabelaLiveData['history-records']", html)
        listed = html[html.index('data-live-region="history-list"'):html.index('id="rental-records-data"')]
        self.assertNotRegex(listed[:listed.index('x-show="openProofModal"')] if 'x-show="openProofModal"' in listed else listed, r"<script[\s>]")

    def test_with_no_rentals_it_says_so_and_still_draws_the_card(self):
        _, html = self.page()
        self.assertIn("No rentals recorded yet", html)
        self.assertNotIn('data-row-id="', html)
        self.assertNotIn("No rentals match these filters", html)                     # that one only exists when rows exist


class ClosedLineTests(HistoryBase):
    def test_the_line_shows_the_name_the_booking_number_the_gown_the_dates_and_the_price(self):
        reservation, item = self.one("Lorna Reyes", 2, 6, P)
        _, html = self.page()
        line = self.words(self.closed(html, item))
        start, end = self.offset(2), self.offset(6)
        for piece in ("Lorna Reyes", reservation.reference_code, item.gown_name, "&#8369;3,000", "Waiting for pick-up", "Pick-up in 2 days",
                      f"{start:%b} {start.day}, {start.year}", f"{end:%b} {end.day}, {end.year}"):
            self.assertIn(piece, line)

    def test_the_dates_are_the_days_the_gown_really_went_out_and_came_back(self):
        _, item = self.one("Real Days", -9, -4, RET, picked_up_on=self.offset(-8), returned_on=self.offset(-3))
        _, html = self.page()
        block = self.closed(html, item)
        out, back = self.offset(-8), self.offset(-3)
        self.assertIn(f'data-label="Pick-up">{out:%b} {out.day}, {out.year}</div>', block)
        self.assertIn(f'data-label="Return">{back:%b} {back.day}, {back.year}</div>', block)

    def test_a_rental_that_crosses_new_year_writes_both_years(self):
        _, cross = self.one("Cross Year", 2, 6, P)
        ReservationItem.objects.filter(id=cross.id).update(rental_date=date(2025, 12, 29), return_date=date(2026, 1, 2))
        _, html = self.page()
        words = self.words(self.closed(html, cross))
        self.assertIn("Dec 29, 2025", words)
        self.assertIn("Jan 2, 2026", words)

    def test_an_overdue_line_is_marked_and_the_others_are_not(self):
        items = self.every_kind()
        _, html = self.page()
        self.assertIn('<div class="ar-booking is-overdue" data-row-id="%d"' % items[OVERDUE].id, html)
        self.assertEqual(html.count('class="ar-booking is-overdue"'), 1)

    def test_the_circle_has_the_status_in_words_for_hover_and_screen_readers(self):
        _, done = self.back()
        _, missed = self.one("Missed", -9, -5, P)
        _, html = self.page()
        self.assertIn('class="ar-dot ar-dot--done" role="img" aria-label="Returned"', self.closed(html, done))
        self.assertIn('class="ar-dot rh-dot--missed" role="img" aria-label="Not picked up"', self.closed(html, missed))
        self.assertIn("The gown is back", self.closed(html, done))


class OpenedRowTests(HistoryBase):
    def test_the_opened_row_has_the_status_the_sentence_the_booking_number_and_the_history(self):
        reservation, item = self.back("Opened Person")
        _, html = self.page()
        panel = self.panel(html, item)
        self.assertIn('id="rh-panel-%d"' % item.id, panel)
        self.assertIn('<span class="rh-chip rh-chip--returned">Returned</span>', panel)
        self.assertIn(f"Returned {self.weekday(self.offset(-4))} · on time", panel)
        self.assertIn(f'<span class="rh-code" title="Booking number">{reservation.reference_code}</span>', panel)
        self.assertIn('<div class="arb-tl-panel">', panel)

    def test_booked_as_shows_inside_only_when_the_account_booked_under_another_name(self):
        reservation, item = self.one("Typed Name", 2, 6, P)
        type(reservation).objects.filter(id=reservation.id).update(customer=self.customer)
        _, html = self.page()
        self.assertIn("Booked as", self.panel(html, item))
        _, plain = self.one("Walk In Name", 2, 6, P)
        _, html = self.page()
        self.assertNotIn("Booked as", self.panel(html, plain))

    def test_every_row_carries_what_the_filters_read(self):
        reservation, item = self.one("Filter Me", 2, 6, P)
        _, html = self.page()
        block = self.row_html(html, item)
        self.assertIn('data-status="Waiting for pick-up"', block)
        self.assertIn('data-search="filter me %s %s"' % (item.gown_name.lower(), reservation.reference_code.lower()), block)
        self.assertIn('data-category="Wedding Gown"', block)
        self.assertIn('data-channel="Online"', block)
        self.assertIn('data-gown="Wedding Gown|%s"' % item.gown_name.casefold(), block)
        self.assertIn('x-show="matches($el)"', block)


class QueryTests(HistoryBase):
    def count(self):
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(URL).status_code, 200)
        return len(captured.captured_queries)

    def test_more_rentals_ask_the_database_nothing_more(self):
        reservation, _ = self.back("First Person")
        reservation_payments.record_payment(reservation_id=reservation.pk, purpose=Payment.Purpose.BALANCE, amount=Decimal("1000"),
                                            method=Payment.Method.CASH, user=self.staff)
        self.one("Second Person", 2, 6, P)
        first = self.count()
        for number in range(3):
            self.back(f"More {number}")
            self.one(f"Waiting {number}", 3, 7, P)
        self.assertEqual(self.count(), first)


class StaticTemplateTests(SimpleTestCase):
    def setUp(self):
        self.source = TEMPLATE.read_text(encoding="utf8")

    def test_the_page_state_has_the_filters_and_no_leftovers(self):
        for piece in ("statusFilter: 'All'", "categoryFilter: ''", "channelFilter: ''", "gownFilter: ''", "openRows: []", "openHistory: []", "matches(el)",
                      "matchCount()", "countFor(status)", "isRowOpen(id)", "toggleRow(id)", "isHistoryOpen(id)", "toggleHistory(id)", "rental-records-data",
                      "rh-view-data", 'x-data="arabelaRentalHistory()"'):
            self.assertIn(piece, self.source, piece)
        # the month, year and "On this date" boxes were replaced by the report's own dates
        for gone in ("filtering", "filterTimeout", "openTimeline", "totalRentals", "Total Rentals", "history_kind", "history_tip", "rh-late", "View proof",
                     "monthFilter", "yearFilter", "dateFocus", "date_focus.html", "presetMonth"):
            self.assertNotIn(gone, self.source, gone)

    def test_the_history_is_shown_at_full_length_in_readable_sizes(self):
        self.assertIn(".rh-history .arb-tl-panel { max-height: none; overflow: visible; }", self.source)
        for rule in (".rh-history .arb-tl-label { font-size: .9375rem; }", ".rh-history .arb-tl-meta, .rh-history .arb-tl-detail { font-size: .875rem; }"):
            self.assertIn(rule, self.source)

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
