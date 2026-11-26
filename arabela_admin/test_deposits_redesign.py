"""Security Deposits, the slim page: ONE card (the Active Reservations look) with the search, the All / Held / Return due / Returned counts as filter buttons,
the year and the shared "On this date" bar (it opens on the RETURN date), then one short line per booking; a booking with many gowns shows "10 gowns" and
opens to one short line per gown; ONE status per booking, read from the payments ledger (the same one the Payments page uses):
  Held         some gown is not back yet
  Return due   every gown is back and there is deposit left to hand back (it is handed back in Payments)
  Returned     handed back, or used up by the late fee and deductions
There is no proof-of-payment window and no duplicate-GCash note on this page any more (Payment Verification and Reservation Records keep those).

What is checked here: the numbers each line shows, where each gown is, the three states and how they are worked out, the two buttons (Receipt, Open in Payments),
that every hook the page's scripts need is still there, that the page asks the database the same number of questions however long the list is, and that
the page is still closed to anyone who is not staff."""
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
from django.utils import timezone

from arabela_admin.test_active_dropdown import ActiveDropdownBase, CAN, P, R, RET
from arabela_admin.views import _deposit_gown_state, _deposit_state
from reservations import payments as reservation_payments
from reservations.models import Payment, ReservationItem, Reservation
from reservations.test_ledger import make_booking, pay

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
        self.assertEqual(self.state(OVERDUE, picked_up_on=self.today - timedelta(days=5)), ("late", "Out with the customer, overdue"))
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
        self.assertEqual((shown.gown_count, shown.cancelled_count, shown.item_total), (4, 0, 4))
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
        self.assertIn('data-status="Return due"', block)                            # the cancelled ones never held it up: the one gown is back, the deposit is due

    def test_a_one_gown_booking_names_its_gown_and_says_the_gown_is_not_back(self):
        reservation, [item] = self.book("One Gown", "09170000004", [(2, 5, R, {"picked_up_on": self.today})])
        _, html = self.page()
        closed = self.booking_html(html, reservation)
        closed = closed[:closed.index('<div class="ar-panel"')]
        self.assertIn(item.gown_name, closed)
        self.assertNotIn("ar-count-pill", closed)
        self.assertIn("Gown not back yet", closed)
        self.assertIn("<span>Held</span>", closed)

    def test_a_booking_with_no_gowns_at_all_does_not_claim_they_were_cancelled(self):
        reservation = Reservation.objects.create(customer=None, customer_name="Empty Booking", phone="09170000099", status=Reservation.Status.CONFIRMED)
        _, html = self.page()
        closed = self.booking_html(html, reservation)
        closed = closed[:closed.index('<div class="ar-panel"')]
        self.assertIn("No gowns on this booking", closed)
        self.assertNotIn("Every gown was cancelled", closed)
        every, _ = self.book("All Cancelled", "09170000098", [(3, 6, CAN, {}), (3, 6, CAN, {})])
        _, html = self.page()
        shown = self.booking_html(html, every)
        self.assertIn("Every gown was cancelled", shown[:shown.index('<div class="ar-panel"')])

    def test_every_gown_back_means_the_deposit_is_due_to_be_handed_back(self):
        back = self.today - timedelta(days=2)
        reservation, _ = self.book("Back Home", "09170000005", [self.returned_item(back_days_ago=2)])
        _, html = self.page()
        closed = self.booking_html(html, reservation)
        closed = closed[:closed.index('<div class="ar-panel"')]
        self.assertIn('data-status="Return due"', self.booking_html(html, reservation))
        self.assertIn("<span>Return due</span>", closed)
        self.assertIn("to hand back", closed)
        self.assertIn(f'href="{reverse("arabela_admin:payments")}?open={reservation.id}"', closed)     # "Hand back in Payments" and the eye both open the Payments window
        self.assertIn("Hand back in Payments", closed)
        self.assertIn(f"Back {back:%b} {back.day}", closed)                                          # the day the last gown came back
        self.assertNotIn("<span>Held</span>", closed)
        self.assertNotIn("<span>Returned</span>", closed)

    def test_a_handed_back_deposit_says_returned_the_day_and_how(self):
        reservation, _ = self.book("Paid Back", "09170000009", [self.returned_item(back_days_ago=2)])
        reservation_payments.return_deposit(reservation_id=reservation.pk, amount=Decimal("2000"), method=Payment.Method.CASH, user=self.staff)
        _, html = self.page()
        closed = self.booking_html(html, reservation)
        closed = closed[:closed.index('<div class="ar-panel"')]
        self.assertIn('data-status="Returned"', self.booking_html(html, reservation))
        self.assertIn("<span>Returned</span>", closed)
        self.assertIn("handed back (Cash)", closed)
        self.assertIn(f"{self.today:%b} {self.today.day}, {self.today.year}", closed)
        self.assertNotIn("Hand back in Payments", closed)
        self.assertNotIn("<span>Return due</span>", closed)

    def test_the_line_shows_the_deposit_the_return_date_and_the_booking_number(self):
        reservation, _ = self.book("Numbers", "09170000006", [(3, 6, P, {})])
        _, html = self.page()
        closed = self.booking_html(html, reservation)
        closed = closed[:closed.index('<div class="ar-panel"')]
        due = self.today + timedelta(days=6)
        self.assertIn(f"&#8369;{reservation.security_deposit:,.0f}", closed)
        self.assertIn(f"{due:%b} {due.day}, {due.year}", closed)
        self.assertIn("Due back", closed)
        self.assertIn(f"<b>{reservation.reference_code}</b>", closed)

    def test_a_gown_past_its_return_date_and_still_out_says_past_due(self):
        reservation, _ = self.book("Late", "09170000010", [(-9, -2, R, {"picked_up_on": self.today - timedelta(days=9)})])
        _, html = self.page()
        closed = self.booking_html(html, reservation)
        closed = closed[:closed.index('<div class="ar-panel"')]
        self.assertIn("Past due", closed)
        self.assertIn("<span>Held</span>", closed)

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
        self.assertEqual([h for h in headers if h], ["Customer", "Gown", "Deposit", "Return date", "Status"])


class FiltersAndSearchTests(DepositsBase):
    def test_the_pills_carry_the_counts_and_the_note_says_how_many_were_handed_back_this_month(self):
        a, _ = self.book("A", "09170000011", [self.returned_item(back_days_ago=2)])                 # back on its return day: no late fee, the whole 2,000 is handed back
        self.book("B", "09170000012", [self.returned_item()])
        self.book("C", "09170000013", [(3, 6, P, {})])
        response, html = self.page()
        context = response.context
        self.assertEqual((context["all_count"], context["held_count"], context["due_count"], context["returned_count"], context["returned_this_month"]), (3, 1, 2, 0, 0))
        bar = html[html.index('aria-label="Show deposits by status"'):html.index('class="sd-year-wrap"')]
        self.assertRegex(bar, r">All <span class=\"ar-n\">3</span>")
        self.assertRegex(bar, r"Held <span class=\"ar-n\">1</span>")
        self.assertRegex(bar, r"Return due <span class=\"ar-n\">2</span>")
        self.assertRegex(bar, r"Returned <span class=\"ar-n\">0</span>")
        reservation_payments.return_deposit(reservation_id=a.pk, amount=Decimal("2000"), method=Payment.Method.CASH, user=self.staff)
        response, html = self.page()
        context = response.context
        self.assertEqual((context["held_count"], context["due_count"], context["returned_count"], context["returned_this_month"]), (1, 1, 1, 1))
        self.assertIn("1 returned this month</span>", html)

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
        self.assertIn("['Held', 'Return due', 'Returned'].includes(presetStatus)", html)
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
    def test_the_heading_the_two_tiles_and_the_fake_spinner_are_gone(self):
        self.book("Chrome", "09170000021", [(3, 6, P, {})])
        _, html = self.page()
        for gone in ("Deposits Held", "Returned This Month", "filtering", "filterTimeout", 'style="height: 300px;"'):
            self.assertNotIn(gone, html)
        self.assertNotRegex(html, r'<h2\s[^>]*x-text="pageName"')                    # no big heading ...
        self.assertIn("pageName: `Security Deposits`", html)                         # ... but the "Home > Security Deposits" breadcrumb is there
        self.assertIn("mb-4 flex flex-wrap items-center justify-end gap-3", html)
        self.assertEqual(html.count('class="sd-card"'), 1)
        self.assertNotIn('<table', html[html.index('class="sd-card"'):html.index("</main>")])

    def test_the_card_is_the_active_reservations_look_with_the_pagers_hooks(self):
        self.book("Look", "09170000022", [(3, 6, P, {})])
        _, html = self.page()
        self.assertIn('data-arpg=".ar-booking" data-arpg-noun="bookings" data-arpg-top="off" data-arpg-count="#ar-count"', html)
        self.assertRegex(html, r'<div class="custom-scrollbar overflow-y-auto" style="max-height: 760px;" data-arpg=')
        self.assertIn('placeholder="Search by customer, booking number or gown..."', html)
        self.assertNotIn(".ar-card { margin-bottom", html)                           # this page draws its own box (.sd-card)

    def test_the_proof_window_is_gone_and_the_buttons_open_payments_and_the_receipt(self):
        reservation, _ = self.book("Window", "09170000023", [(3, 6, P, {})])
        Reservation.objects.filter(pk=reservation.pk).update(gcash_reference="GC-D1", payment_proof_url="https://files.example/d.jpg")
        _, html = self.page()
        for gone in ("viewingPayment", "openReceiptModal", "GCash Payment Proof", "gcash_reference_block", "https://files.example/d.jpg", "gcashDup"):
            self.assertNotIn(gone, html)
        block = self.booking_html(html, reservation)
        link = re.search(r'<a href="[^"]*"[^>]*title="Open in Payments"[^>]*>', block, re.S).group(0)
        self.assertIn(f"{reverse('arabela_admin:payments')}?open={reservation.id}", link)
        self.assertNotIn("arabelaReceipt.open", block)                                  # nothing was paid through the Payments page yet: no receipt to print
        pay(Reservation.objects.get(pk=reservation.pk), Payment.Purpose.DEPOSIT, "2000")
        _, html = self.page()
        self.assertIn(f"arabelaReceipt.open({reservation.id})", self.booking_html(html, reservation))

    def test_the_buttons_do_not_open_or_close_the_booking(self):
        reservation, _ = self.book("Click", "09170000024", [(3, 6, P, {})])
        _, html = self.page()
        block = self.booking_html(html, reservation)
        expected = "@click=\"if (!$event.target.closest('.sd-acts')) { toggleBooking(%d) }\"" % reservation.id
        self.assertIn(expected, block)
        self.assertIn('class="sd-acts"', block)
        acts = block[block.index('class="sd-acts"'):block.index('class="ar-chev"')]
        self.assertIn('@click.stop', acts)                                              # a click on a button is the button's, not the line's


class SameGcashNumberTests(DepositsBase):
    def test_the_duplicate_gcash_note_is_gone_from_this_page(self):
        a, _ = self.book("Twin A", "09170000031", [(3, 6, P, {})])
        b, _ = self.book("Twin B", "09170000032", [(3, 6, P, {})])
        Reservation.objects.filter(pk__in=[a.pk, b.pk]).update(gcash_reference="GC-5005", payment_proof_url="https://files.example/t.jpg")
        _, html = self.page()
        self.assertNotIn("Same GCash ref", html)                                       # Payment Verification and Reservation Records still warn about it
        self.assertNotIn("gcashDup", html)


class DateBarTests(DepositsBase):
    def test_the_bar_is_on_the_page_and_opens_on_the_return_date(self):
        reservation, _ = self.book("Dates", "09170000041", [(3, 6, P, {})])
        _, html = self.page()
        self.assertIn('class="df-bar"', html)
        self.assertIn("dateFocus: '', dateKind: 'return'", html)
        self.assertIn("arabelaDF.has(el, this.dateFocus, this.dateKind)", html)
        self.assertIn("presetSearchParams.get('kind') ? arabelaDF.kind(presetSearchParams.get('kind')) : 'return'", html)
        self.assertIn("$watch('dateFocus', syncFilterParamsToUrl);", html)
        tag = re.search(r'<div class="ar-booking" data-booking-id="%d"[^>]*>' % reservation.id, html).group(0)
        due, pick = self.today + timedelta(days=6), self.today + timedelta(days=3)
        self.assertIn(f'data-d-return="{due:%Y-%m-%d}"', tag)
        self.assertIn(f'data-d-pickup="{pick:%Y-%m-%d}"', tag)
        self.assertIn("data-d-event=", tag)

    def test_a_gown_the_shop_cancelled_has_no_date_on_the_bar(self):
        reservation, _ = self.book("Cancelled Dates", "09170000042", [(3, 6, P, {}), (20, 25, CAN, {})])
        _, html = self.page()
        tag = re.search(r'<div class="ar-booking" data-booking-id="%d"[^>]*>' % reservation.id, html).group(0)
        far = self.today + timedelta(days=25)
        self.assertNotIn(f"{far:%Y-%m-%d}", tag)


class DepositStateTests(TestCase):
    """_deposit_state: the three states and the words under them, worked out from the ledger."""

    def state(self, reservation):
        reservation = Reservation.objects.get(pk=reservation.pk)
        live = [i for i in reservation.items.all() if i.stage != ReservationItem.Stage.CANCELLED]
        return _deposit_state(reservation, live, timezone.localdate())

    def test_a_gown_still_out_holds_the_deposit(self):
        booking = make_booking(stage=R, pickup=-3, ret=2)
        pay(booking, Payment.Purpose.FULL, "5500")
        held = self.state(booking)
        self.assertEqual((held["state"], held["kind"]), ("Held", "held"))
        self.assertEqual(held["notes"], ["Gown not back yet"])

    def test_how_many_gowns_are_back_is_said_for_a_booking_with_several(self):
        booking = make_booking(gowns=3, stage=R, pickup=-3, ret=2)
        first = ReservationItem.objects.filter(reservation=booking).order_by("id").first()
        ReservationItem.objects.filter(pk=first.pk).update(stage=RET, returned_on=timezone.localdate())
        pay(booking, Payment.Purpose.FULL, "5500")
        self.assertEqual(self.state(booking)["notes"], ["1 of 3 gowns back"])

    def test_a_deposit_not_paid_yet_says_so(self):
        booking = make_booking(stage=R, pickup=-3, ret=2)
        pay(booking, Payment.Purpose.DEPOSIT, "500")
        self.assertIn("of the deposit is not paid yet", " ".join(self.state(booking)["notes"]))

    def test_every_gown_back_and_nothing_handed_back_yet_is_return_due(self):
        booking = make_booking(stage=RET, pickup=-6, ret=-2, returned_on=-2)
        pay(booking, Payment.Purpose.FULL, "5500")
        due = self.state(booking)
        self.assertEqual((due["state"], due["kind"]), ("Return due", "due"))
        self.assertEqual(due["notes"], ["₱2,000.00 to hand back"])
        self.assertEqual(due["to_hand_back"], Decimal("2000.00"))

    def test_a_late_fee_comes_off_what_is_to_hand_back_and_the_line_says_so(self):
        booking = make_booking(stage=RET, pickup=-9, ret=-5, returned_on=-2, original_return=-5)       # 3 days late at 200 a day
        pay(booking, Payment.Purpose.FULL, "5500")
        due = self.state(booking)
        self.assertEqual(due["state"], "Return due")
        self.assertEqual(due["notes"], ["₱1,400.00 to hand back", "₱600.00 kept for fees or deductions"])
        self.assertEqual(due["late_fee"], Decimal("600.00"))

    def test_a_handed_back_deposit_is_returned_with_its_day_and_how(self):
        booking = make_booking(stage=RET, pickup=-6, ret=-2, returned_on=-2)
        pay(booking, Payment.Purpose.FULL, "5500")
        reservation_payments.return_deposit(reservation_id=booking.pk, amount=Decimal("2000"), method=Payment.Method.CASH)
        done = self.state(booking)
        self.assertEqual((done["state"], done["kind"], done["how"], done["when"]), ("Returned", "returned", "Cash", timezone.localdate()))
        self.assertEqual(done["notes"], ["₱2,000.00 handed back (Cash)"])

    def test_a_deposit_used_up_by_the_late_fee_is_returned_with_nothing_to_hand_back(self):
        booking = make_booking(stage=RET, pickup=-20, ret=-18, returned_on=-4, original_return=-18, deposit="2000")   # 14 days late at 200 a day = 2,800 > 2,000
        pay(booking, Payment.Purpose.FULL, "5500")
        used = self.state(booking)
        self.assertEqual(used["state"], "Returned")
        self.assertEqual(used["notes"], ["Nothing to hand back: the fees and deductions used the deposit"])

    def test_a_deposit_marked_returned_by_hand_before_the_payments_page_keeps_its_date(self):
        booking = make_booking(stage=RET, pickup=-6, ret=-2, returned_on=-2)
        when = timezone.now() - timedelta(days=30)
        Reservation.objects.filter(pk=booking.pk).update(deposit_returned_at=when)
        old = self.state(booking)
        self.assertEqual((old["state"], old["when"]), ("Returned", timezone.localtime(when).date()))
        self.assertEqual(old["notes"], ["Marked as returned before the Payments page"])

    def test_a_cancelled_gown_does_not_hold_the_deposit_up(self):
        booking = make_booking(gowns=2, stage=RET, pickup=-6, ret=-2, returned_on=-2)
        last = ReservationItem.objects.filter(reservation=booking).order_by("-id").first()
        ReservationItem.objects.filter(pk=last.pk).update(stage=CAN, returned_on=None)
        pay(booking, Payment.Purpose.FULL, "5500")
        self.assertEqual(self.state(booking)["state"], "Return due")


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
