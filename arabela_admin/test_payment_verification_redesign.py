"""Payment Verification, redesigned: the three counts are ONE strip of filter buttons (the Gown Catalog look), Verified Today is a calm slim card with one
balanced line per booking, and the list is ONE card (the Pending Approval / Security Deposits look): search + month / status / year, the count with the key to the
status circles, and one short line per booking -- the status is a circle before the name, the payment cell shows the amount with a small grey method line (the
"Reference No." column said "GCash", which is not a reference), the proof is an eye icon, and Approve / Reject use the same words as Pending Approval. A booking
with many gowns shows "5 gowns" and opens to one short line per gown.

What is checked here: what each part shows, that every hook the page's scripts and the other pages need is still there (the same proof window, the same
Approve / Reject calls, the same filters, links and Year memory), that the page asks the database the same number of questions however long the list is, and
that the page is still closed to anyone who is not staff. The Approve / Reject server actions keep their own tests."""
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

from arabela_admin.test_active_dropdown import ActiveDropdownBase, CAN, P, R
from reservations.models import Reservation, ReceiptRecord

User = get_user_model()
URL = reverse("arabela_admin:payment_verification")
ADMIN = Path(settings.BASE_DIR) / "templates" / "arabela_admin"
Status = Reservation.Status


class PvBase(ActiveDropdownBase):
    def page(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def booking_html(self, html, reservation):
        start = html.index('<div class="ar-booking" data-booking-id="%d"' % reservation.id)
        end = html.find('<div class="ar-booking" data-booking-id=', start + 10)
        return html[start:end if end != -1 else html.index('x-show="matchCount() === 0"', start)]

    def closed(self, html, reservation):
        block = self.booking_html(html, reservation)
        return block[:block.index('<div class="ar-panel"')]

    def panel(self, html, reservation):
        block = self.booking_html(html, reservation)
        return block[block.index('<div class="ar-panel"'):]

    def make(self, name, phone, items=None, *, status=Status.PENDING, proof="", gcash="", method="GCash", total=None, **extra):
        """A booking in the given status. By default it has ONE gown picked up in three days."""
        reservation, made = self.book(name, phone, items if items is not None else [(3, 6, P, {})])
        fields = {"status": status, "payment_method": method, "payment_proof_url": proof, "gcash_reference": gcash}
        if status == Status.PENDING:
            fields["reviewed_at"] = None
        if total is not None:
            fields["total_amount"] = Decimal(total)
        fields.update(extra)
        Reservation.objects.filter(pk=reservation.pk).update(**fields)
        reservation.refresh_from_db()
        return reservation, made

    def pay_cell(self, html, reservation):
        block = self.closed(html, reservation)
        return block[block.index('class="pv-pay"'):block.index('class="pv-sub-at"')]


class ViewFieldsTests(PvBase):
    def test_each_booking_knows_its_gowns_and_one_status_word(self):
        pending, _ = self.make("Pending One", "09170000001", [(3, 6, P, {}), (3, 6, P, {})])
        verified, _ = self.make("Verified One", "09170000002", status=Status.CONFIRMED)
        rejected, _ = self.make("Rejected One", "09170000003", status=Status.REJECTED)
        cancelled, _ = self.make("Cancelled One", "09170000004", status=Status.CANCELLED)
        out, _ = self.make("Out One", "09170000005", status=Status.ACTIVE)
        response, _ = self.page()
        shown = {r.id: r for r in response.context["reservations"]}
        self.assertEqual(
            [shown[r.id].pv_status for r in (pending, verified, rejected, cancelled, out)],
            ["Pending", "Verified", "Rejected", "Cancelled", "Verified"])
        self.assertEqual((shown[pending.id].gown_count, shown[pending.id].item_total), (2, 2))

    def test_a_cancelled_gown_is_not_counted_but_is_still_listed(self):
        reservation, _ = self.make("Some Cancelled", "09170000006", [(3, 6, P, {}), (3, 6, CAN, {}), (3, 6, CAN, {})])
        response, html = self.page()
        shown = next(r for r in response.context["reservations"] if r.id == reservation.id)
        self.assertEqual((shown.gown_count, shown.cancelled_count, shown.item_total), (1, 2, 3))
        panel = self.panel(html, reservation)
        self.assertEqual(panel.count('class="ar-gl pv-gl"'), 3)
        self.assertEqual(panel.count(">Cancelled</span>"), 2)
        self.assertEqual(panel.count("text-decoration: line-through"), 2)

    def test_the_other_stat_counts_are_unchanged(self):
        self.make("Count A", "09170000007")
        self.make("Count B", "09170000008", status=Status.CONFIRMED)
        self.make("Count C", "09170000009", status=Status.REJECTED)
        response, _ = self.page()
        self.assertEqual(
            (response.context["pending_review_count"], response.context["verified_this_month_count"], response.context["rejected_count"]), (1, 1, 1))


class StripTests(PvBase):
    def test_the_three_counts_are_one_strip_of_buttons_that_still_filter(self):
        self.make("Strip A", "09170000011")
        self.make("Strip B", "09170000012", status=Status.CONFIRMED)
        self.make("Strip C", "09170000013", status=Status.REJECTED)
        response, html = self.page()
        strip = html[html.index('<div class="pv-strip">'):html.index('class="pv-vt"') if 'class="pv-vt"' in html else html.index('class="pv-card"')]
        self.assertEqual(strip.count("<button"), 3)
        for word, label, count in (("Pending", "Pending Review", 1), ("Verified", "Verified This Month", 1), ("Rejected", "Rejected", 1)):
            self.assertIn("@click=\"statusFilter = '%s'\"" % word, strip)
            self.assertIn(f"</i>{label}</span>", strip)
            self.assertRegex(strip, r'class="pv-stat-n">%d</span>' % count)
            self.assertIn(":class=\"statusFilter === '%s' ? 'is-on' : ''\"" % word, strip)
        self.assertNotIn("rounded-2xl", strip)                                        # the big separate tiles are gone

    def test_the_old_tiles_and_the_fake_spinner_are_gone(self):
        self.make("Gone", "09170000014")
        _, html = self.page()
        for gone in ("filtering", "filterTimeout", 'style="height: 300px;"', "grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3"):
            self.assertNotIn(gone, html)


class ListLineTests(PvBase):
    def test_the_columns_are_customer_payment_submitted_actions_only(self):
        self.make("Columns", "09170000021")
        _, html = self.page()
        start = html.index('class="ar-head pv-grid"')
        headers = re.findall(r"<span>([^<]*)</span>", html[start:start + 300])
        self.assertEqual([h for h in headers if h], ["Customer", "Payment", "Submitted", "Actions"])
        for gone in ("Reference No.</p>", ">Status</p>", "<th ", "<table", ">Date Submitted<", ">Amount<", ">Reservation<"):
            self.assertNotIn(gone, html[html.index('class="pv-card"'):html.index('x-show="openReceiptModal"')])

    def test_the_status_is_a_circle_before_the_name_with_its_words_on_hover(self):
        pending, _ = self.make("Dot Pending", "09170000022")
        verified, _ = self.make("Dot Verified", "09170000023", status=Status.CONFIRMED)
        rejected, _ = self.make("Dot Rejected", "09170000024", status=Status.REJECTED)
        cancelled, _ = self.make("Dot Cancelled", "09170000025", status=Status.CANCELLED)
        _, html = self.page()
        for reservation, word in ((pending, "Pending"), (verified, "Verified"), (rejected, "Rejected"), (cancelled, "Cancelled")):
            block = self.closed(html, reservation)
            self.assertIn('data-status="%s"' % word, self.booking_html(html, reservation)[:700])
            self.assertIn('class="ar-dot pv-dot--%s" role="img" aria-label="%s"' % (word.lower(), word), block)
            self.assertIn('title="%s:' % word, block)
            self.assertLess(block.index("ar-dot pv-dot"), block.index("ar-name"))     # the circle comes first
            self.assertNotIn("rounded-full px-2 py-0.5", block)                       # no status pill any more

    def test_the_key_names_all_four_circles_once(self):
        self.make("Key", "09170000026")
        _, html = self.page()
        legend = html[html.index('class="pv-legend"'):html.index('class="custom-scrollbar overflow-y-auto"')]
        self.assertEqual(re.findall(r'</i>(\w+)</span>', legend), ["Pending", "Verified", "Rejected", "Cancelled"])
        self.assertIn('id="pv-count"', html)

    def test_the_payment_cell_is_the_amount_with_the_method_under_it(self):
        reservation, _ = self.make("Amount", "09170000027", proof="https://files.example/a.jpg", total="4321")
        _, html = self.page()
        cell = self.pay_cell(html, reservation)
        self.assertIn('<div class="pv-amt">&#8369;4,321</div>', cell)
        self.assertIn('<div class="pv-note">GCash</div>', cell)
        self.assertNotIn("GCash &middot;", cell)
        self.assertNotIn("No proof uploaded", cell)
        self.assertNotIn("Shop receipt attached", cell)

    def test_an_older_booking_with_a_stored_gcash_number_shows_it_after_the_method(self):
        reservation, _ = self.make("Old Number", "09170000028", proof="https://files.example/o.jpg", gcash="1234567890123")
        _, html = self.page()
        self.assertIn('<div class="pv-note">GCash &middot; 1234567890123</div>', self.pay_cell(html, reservation))

    def test_no_proof_and_a_shop_receipt_say_so_under_the_same_conditions_as_before(self):
        none, _ = self.make("No Proof", "09170000029", proof="")
        shop, _ = self.make("Shop Receipt", "09170000030", proof="", method="Cash")
        ReceiptRecord.objects.create(reservation=shop, photo_url="https://files.example/receipt_photos/s.jpg", uploaded_by=self.staff)
        paid, _ = self.make("Has Proof", "09170000031", proof="https://files.example/p.jpg")
        _, html = self.page()
        self.assertIn('<div class="pv-note pv-note--bad">No proof uploaded</div>', self.pay_cell(html, none))
        self.assertIn('<div class="pv-note">Shop receipt attached</div>', self.pay_cell(html, shop))
        self.assertNotIn("No proof uploaded", self.pay_cell(html, shop))
        for words in ("No proof uploaded", "Shop receipt attached"):
            self.assertNotIn(words, self.pay_cell(html, paid))

    def test_the_amount_leads_with_what_was_paid_and_the_split_is_not_repeated(self):
        reservation, _ = self.make("Split", "09170000032", proof="https://files.example/s.jpg", method="GCash")
        _, html = self.page()
        self.assertIn('<div class="pv-amt">₱2,000</div>', self.pay_cell(html, reservation))
        self.assertNotIn("Gown ₱3,000", self.closed(html, reservation))
        self.assertNotIn("Gown ₱3,000", self.panel(html, reservation))      # the proof window's Amount breakdown already says it
        self.assertEqual(html.count("Amount breakdown"), 1)

    def test_the_same_gcash_number_on_two_bookings_is_flagged_on_both(self):
        a, _ = self.make("Twin A", "09170000033", proof="https://files.example/t.jpg", gcash="5005005005005")
        b, _ = self.make("Twin B", "09170000034", proof="https://files.example/t.jpg", gcash="5005005005005")
        _, html = self.page()
        self.assertIn(f"Same GCash ref as {b.reference_code}", self.pay_cell(html, a))
        self.assertIn(f"Same GCash ref as {a.reference_code}", self.pay_cell(html, b))
        alone, _ = self.make("Alone", "09170000035", proof="https://files.example/t.jpg", gcash="7007007007007")
        _, html = self.page()
        self.assertNotIn("Same GCash ref as", self.pay_cell(html, alone))

    def test_the_submitted_date_and_time_and_the_name_link_are_there(self):
        reservation, _ = self.make("Dated", "09170000036")
        _, html = self.page()
        block = self.closed(html, reservation)
        when = timezone.localtime(reservation.created_at)
        self.assertIn(f'<div class="pv-day">{when:%b} {when.day}, {when.year}</div>', block)
        self.assertIn(f'<div class="pv-note">{when.strftime("%I:%M %p").lstrip("0")}</div>', block)
        self.assertIn(f'href="{reverse("arabela_admin:reservation_records")}?search={reservation.reference_code}"', block)
        self.assertIn(f"<b>{reservation.reference_code}</b>", block)

    def test_a_booked_as_name_and_the_gowns_are_in_the_opened_booking(self):
        reservation, items = self.make("Book Panel", "09170000037", [(3, 6, P, {}), (4, 7, R, {})], customer=self.customer)
        _, html = self.page()
        panel = self.panel(html, reservation)
        self.assertIn("Booked as: Book Panel", panel)
        for number, item in enumerate(items, start=1):
            self.assertIn(item.gown_name, panel)
            self.assertIn(f"{number} of 2", panel)
            self.assertIn(f"&#8369;{item.rental_price:,.0f}", panel)
        self.assertNotIn("Booked as", self.closed(html, reservation))


class ManyGownsTests(PvBase):
    def test_five_gowns_show_a_count_not_five_lines(self):
        reservation, items = self.make("Five Gowns", "09170000041", [(3, 6, P, {}) for _ in range(5)])
        _, html = self.page()
        closed = self.closed(html, reservation)
        self.assertIn("5 gowns</span>", closed)
        self.assertEqual(closed.count("ar-count-pill"), 1)
        for item in items:
            self.assertNotIn(item.gown_name, closed)                                   # the names are inside, one line each
        panel = self.panel(html, reservation)
        self.assertEqual(panel.count('class="ar-gl pv-gl"'), 5)
        for number in range(1, 6):
            self.assertIn(f"{number} of 5", panel)

    def test_one_gown_has_no_count(self):
        reservation, _ = self.make("One Gown", "09170000042")
        _, html = self.page()
        self.assertNotIn("ar-count-pill", self.closed(html, reservation))
        self.assertNotIn(" of 1<", self.panel(html, reservation))

    def test_a_booking_with_no_gowns_does_not_break_the_page(self):
        reservation = Reservation.objects.create(customer=None, customer_name="Empty Booking", phone="09170000043", status=Status.PENDING)
        response, html = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Empty Booking", self.closed(html, reservation))


class ActionsTests(PvBase):
    def test_pending_rows_have_approve_and_reject_with_the_same_hooks_as_before(self):
        reservation, _ = self.make("Act Pending", "09170000051", proof="https://files.example/x.jpg")
        _, html = self.page()
        block = self.closed(html, reservation)
        approve = re.search(r"<button[^>]*data-action=\"verify\"[^>]*>(.*?)</button>", block, re.S)
        reject = re.search(r"<button[^>]*data-action=\"reject\"[^>]*>(.*?)</button>", block, re.S)
        self.assertEqual((approve.group(1), reject.group(1)), ("Approve", "Reject"))
        for button, view in ((approve.group(0), "reservation_approve"), (reject.group(0), "reservation_reject")):
            self.assertIn(f'data-url="{reverse("arabela_admin:" + view, args=[reservation.id])}"', button)
            self.assertIn(f'data-customer="{reservation.display_customer_name}"', button)
            self.assertIn(f'data-reservation="{reservation.reference_code}"', button)
            self.assertIn('onclick="reviewPayment(this)"', button)
        self.assertIn("resv-btn resv-btn-approve", approve.group(0))
        self.assertIn("resv-btn resv-btn-reject", reject.group(0))

    def test_every_other_status_has_no_approve_or_reject(self):
        reservations = [self.make(f"Act {status}", f"091700000{60 + number}", status=status)[0]
                        for number, status in enumerate((Status.CONFIRMED, Status.REJECTED, Status.CANCELLED, Status.ACTIVE, Status.RETURNED, Status.OVERDUE))]
        _, html = self.page()
        for reservation in reservations:
            block = self.closed(html, reservation)
            self.assertNotIn("data-action=", block, reservation.status)
            self.assertNotIn("reviewPayment(", block, reservation.status)

    def test_the_words_are_approve_and_reject_everywhere(self):
        self.make("Words", "09170000052")
        _, html = self.page()
        self.assertNotIn("Verify payment", html)
        self.assertNotRegex(html, r">\s*Verify\s*</button>")
        self.assertIn("title: isVerify ? 'Approve payment' : 'Reject payment'", html)
        self.assertIn("confirmText: isVerify ? 'Approve payment' : 'Reject payment'", html)

    def test_the_dashboard_quick_verify_says_approve_too_and_keeps_its_hooks(self):
        reservation, _ = self.make("Dash Approve", "09170000053", proof="https://files.example/d.jpg")
        html = self.client.get(reverse("arabela_admin:dashboard")).content.decode()
        self.assertNotIn("Verify payment", html)
        self.assertNotRegex(html, r">\s*Verify\s*</button>")
        self.assertRegex(html, r'data-action="verify"[^>]*onclick="reviewQuickVerify\(this\)"[^>]*title="Approve payment"')           # an icon button now: the words are its hover and its label
        self.assertRegex(html, r'data-action="reject"[^>]*onclick="reviewQuickVerify\(this\)"[^>]*title="Reject payment"')
        self.assertIn("title: isVerify ? 'Approve payment' : 'Reject payment'", html)
        self.assertIn("confirmText: isVerify ? 'Approve payment' : 'Reject payment'", html)
        self.assertIn('data-action="reject"', html)
        self.assertIn(reverse("arabela_admin:reservation_approve", args=[reservation.id]), html)

    def test_the_dashboard_popup_gets_the_plain_booking_number_and_name(self):
        # The pop-up prints these two attributes as they are. They were once written with the JavaScript escape (escapejs), so it said
        # "RSV-2026-0018" and "O'Neil" instead of the booking number and the name.
        reservation, _ = self.make("Dash O'Neil", "09170000058", proof="https://files.example/d.jpg")
        html = self.client.get(reverse("arabela_admin:dashboard")).content.decode()
        self.assertIn(f'data-reservation="{reservation.reference_code}"', html)
        self.assertIn('data-customer="Dash O&#x27;Neil"', html)

    def test_the_script_finds_the_booking_the_buttons_live_in(self):
        self.make("Script", "09170000054")
        _, html = self.page()
        self.assertIn("var row = btn.closest('tr, .ar-booking');", html)
        self.assertIn("row.querySelectorAll('button[data-url]')", html)
        self.assertIn("window.location.reload();", html)

    def test_the_eye_opens_the_same_window_with_the_same_payload(self):
        reservation, _ = self.make("Eye", "09170000055", proof="https://files.example/e.jpg", gcash="1112223334445")
        _, html = self.page()
        eye = re.search(r'<button[^>]*title="View payment proof"[^>]*>', self.closed(html, reservation), re.S).group(0)
        for needle in ("openReceiptModal = true", "proofUrl: 'https://files.example/e.jpg'", "receipts: JSON.parse(", "breakdown: JSON.parse(",
                       "gcashRef: '1112223334445'", "status: 'Pending'", "reservation: '%s'" % reservation.reference_code.replace("-", "\\u002D")):
            self.assertIn(needle, eye)
        self.assertIn('aria-label="View the payment proof of Eye,', eye)
        self.assertNotIn("View Proof", html[html.index('class="pv-card"'):html.index('x-show="openReceiptModal"')])

    def test_the_proof_window_itself_is_untouched(self):
        self.make("Window", "09170000056", proof="https://files.example/w.jpg")
        _, html = self.page()
        window = html[html.index('x-show="openReceiptModal"'):]
        self.assertIn('@click.outside="openReceiptModal = false"', window)
        for words in ("GCash Payment Proof", "GCash Reference No.", "Date Submitted", "Amount breakdown", "Check the receipt before approving"):
            self.assertIn(words, html)
        self.assertEqual(html.count("Amount breakdown"), 1)

    def test_the_eye_and_the_name_do_not_open_the_booking_but_the_rest_of_the_line_does(self):
        reservation, _ = self.make("Click", "09170000057")
        _, html = self.page()
        block = self.booking_html(html, reservation)
        self.assertIn("@click=\"if (!$event.target.closest('.pv-acts, a')) { toggleBooking(%d) }\"" % reservation.id, block)
        self.assertNotIn("@click.stop", block[:block.index('class="ar-chev"')])


class VerifiedTodayTests(PvBase):
    def block(self, html):
        return html[html.index('class="pv-vt"'):html.index('class="pv-card"')]

    def test_no_card_when_nothing_was_verified_today(self):
        self.make("Not Today", "09170000071", status=Status.CONFIRMED, reviewed_at=timezone.now() - timedelta(days=2))
        _, html = self.page()
        self.assertNotIn('class="pv-vt"', html)
        self.assertNotIn('id="verified-today-list"', html)

    def test_one_balanced_line_per_booking(self):
        reservation, [item] = self.make("Today One", "09170000072", status=Status.CONFIRMED)
        _, html = self.page()
        card = self.block(html)
        self.assertIn("<h3>Verified Today</h3>", card)
        self.assertIn("Double-check these are correctly scheduled before you move on.", card)
        self.assertRegex(card, r'<span class="pv-vt-n">1</span>')
        self.assertIn("Today One", card)
        self.assertIn(f"<b>{reservation.reference_code}</b>", card)
        self.assertIn(item.gown_name, card)
        self.assertIn(f"Pick-up {item.rental_date:%b} {item.rental_date.day}", card)
        when = timezone.localtime(reservation.reviewed_at)
        self.assertIn(f"Verified {when.strftime('%I:%M %p').lstrip('0')}", card)
        self.assertIn("View schedule", card)
        self.assertIn(f'href="{reverse("arabela_admin:active_reservations")}?search={reservation.reference_code}"', card)
        self.assertNotIn("border-success-500 bg-success-50", html)                     # the big green box is gone

    def test_a_booking_with_many_gowns_shows_a_count_and_opens_to_one_line_per_gown(self):
        reservation, items = self.make("Today Many", "09170000073", [(3, 6, P, {}) for _ in range(4)], status=Status.CONFIRMED)
        single, [one] = self.make("Today Single", "09170000074", status=Status.CONFIRMED)
        _, html = self.page()
        card = self.block(html)
        lines = {name: next(part for part in card.split('class="verified-today-card pv-vt-item"')[1:] if name in part) for name in ("Today Many", "Today Single")}
        many, one_line = lines["Today Many"], lines["Today Single"]
        self.assertIn("4 gowns</span>", many)
        self.assertEqual(many.count('class="pv-vt-line"'), 4)
        self.assertIn('x-show="open"', many)
        self.assertIn("@click=\"open = !open\"", many)
        for item in items:
            self.assertIn(item.gown_name, many)
        self.assertIn(one.gown_name, one_line)
        self.assertNotIn("ar-count-pill", one_line)
        self.assertNotIn('class="pv-vt-more"', one_line)
        self.assertNotIn("@click=\"open = !open\"", one_line)

    def test_the_list_keeps_its_capped_height_scroll_and_clears_itself_tomorrow(self):
        self.make("Scroll", "09170000075", status=Status.CONFIRMED)
        _, html = self.page()
        self.assertIn('id="verified-today-list" class="custom-scrollbar"', html)
        self.assertIn("#verified-today-list { max-height: 420px; overflow-y: auto;", html)
        view = (Path(settings.BASE_DIR) / "arabela_admin" / "views.py").read_text(encoding="utf8")
        self.assertIn("reviewed_at__date=timezone.localdate()", view)


class FiltersAndHooksTests(PvBase):
    def test_each_booking_carries_what_the_filters_read(self):
        reservation, [item] = self.make("Search Me", "09170000081", status=Status.CONFIRMED)
        _, html = self.page()
        tag = re.search(r'<div class="ar-booking" data-booking-id="%d"[^>]*>' % reservation.id, html).group(0)
        self.assertIn('data-status="Verified"', tag)
        made = timezone.localtime(reservation.created_at)
        self.assertIn(f'data-year="{made:%Y}"', tag)
        self.assertIn(f'data-month="{made:%b}"', tag)
        haystack = re.search(r'data-search="([^"]*)"', tag).group(1)
        for word in ("search me", reservation.reference_code.lower(), item.gown_name.lower()):
            self.assertIn(word, haystack)
        self.assertIn('x-show="matches($el)"', tag)

    def test_the_toolbar_has_the_same_filters(self):
        self.make("Toolbar", "09170000082")
        _, html = self.page()
        self.assertIn('placeholder="Search by customer, booking number or gown..."', html)
        self.assertIn('x-model="search"', html)
        self.assertIn('x-model="monthFilter"', html)
        self.assertIn('x-model="statusFilter"', html)
        self.assertIn('x-model="yearFilter"', html)
        for words in ("All Months", "All Status", ">Pending</option>", ">Verified</option>", ">Rejected</option>", ">Cancelled</option>", ">January</option>", ">December</option>"):
            self.assertIn(words, html)
        self.assertIn('placeholder="Year"', html)

    def test_the_page_still_reads_the_links_the_dashboard_and_the_bell_send(self):
        self.make("Links", "09170000083")
        _, html = self.page()
        for needle in ("presetSearchParams.get('search')", "presetSearchParams.get('status')", "presetSearchParams.get('month')",
                       "$watch('search', syncFilterParamsToUrl);", "$watch('statusFilter', syncFilterParamsToUrl);", "$watch('monthFilter', syncFilterParamsToUrl);",
                       "pvSetupBookings($data, $watch);", "function pvSetupBookings(state, watch)"):
            self.assertIn(needle, html)

    def test_the_year_box_and_its_memory_and_toast_are_unchanged(self):
        self.make("Year", "09170000084")
        _, html = self.page()
        self.assertIn("localStorage.getItem('financialReportYear')", html)
        self.assertIn("localStorage.setItem('financialReportYear', value)", html)
        self.assertIn("yearFilter.replace(/[^0-9]/g, '').slice(0, 4)", html)
        self.assertIn("Year applied across Payments &amp; Deposits", html)

    def test_the_filter_is_decided_in_one_place(self):
        self.make("One Rule", "09170000085")
        _, html = self.page()
        self.assertEqual(html.count("matches(el) {"), 1)
        for field in ("data-status", "data-year", "data-month", "data-search"):
            self.assertIn("el.getAttribute('%s')" % field, html)
        self.assertIn("matchCount() { return Array.from(document.querySelectorAll('.ar-booking[data-booking-id]')).filter(el => this.matches(el)).length; }", html)

    def test_the_card_is_the_shared_look_with_the_pagers_hooks(self):
        self.make("Look", "09170000086")
        _, html = self.page()
        self.assertIn('data-arpg=".ar-booking" data-arpg-noun="bookings" data-arpg-top="off" data-arpg-count="#pv-count"', html)
        self.assertRegex(html, r'<div class="custom-scrollbar overflow-y-auto" style="max-height: 760px;" data-arpg=')
        self.assertEqual(html.count('class="pv-card"'), 1)
        self.assertNotIn(".ar-card { margin-bottom", html)                              # this page draws its own box (.pv-card)

    def test_the_heading_is_gone_but_the_breadcrumb_stays(self):
        self.make("Chrome", "09170000087")
        _, html = self.page()
        self.assertNotRegex(html, r'<h2\s[^>]*x-text="pageName"')
        self.assertIn("pageName: `Payment Verification`", html)
        self.assertIn("mb-4 flex flex-wrap items-center justify-end gap-3", html)

    def test_an_empty_list_says_so_and_a_search_with_no_match_says_so(self):
        _, html = self.page()
        self.assertIn("No reservations have been submitted yet.", html)
        self.assertNotIn('x-show="matchCount() === 0"', html)
        self.make("Some", "09170000088")
        _, html = self.page()
        self.assertIn("No payment matches your search.", html)

    def test_no_unrendered_template_syntax_leaks(self):
        self.make("Leak", "09170000089", [(3, 6, P, {}), (4, 7, R, {})], status=Status.CONFIRMED)
        _, html = self.page()
        for leak in ("{%", "{{", "{#"):
            self.assertNotIn(leak, html)


class FewQuestionsTests(PvBase):
    def queries(self):
        self.client.get(URL)                                                            # the first visit also creates the shop-settings row
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(URL).status_code, 200)
        return len(captured.captured_queries)

    def test_a_longer_list_does_not_ask_the_database_more_questions(self):
        for number in range(3):
            reservation, _ = self.make(f"Few {number}", f"0917100000{number}", [(3, 6, P, {}), (4, 7, CAN, {})], status=Status.CONFIRMED, gcash=f"GC-Q{number}")
            ReceiptRecord.objects.create(reservation=reservation, photo_url=f"https://files.example/receipt_photos/{number}.jpg", uploaded_by=self.staff)
        short = self.queries()
        for number in range(3, 15):
            reservation, _ = self.make(f"Many {number}", f"091710000{number:02d}", [(3, 6, P, {}), (4, 7, CAN, {}), (5, 8, P, {}), (6, 9, R, {})],
                                       status=Status.CONFIRMED if number % 2 else Status.PENDING, gcash=f"GC-Q{number}")
            ReceiptRecord.objects.create(reservation=reservation, photo_url=f"https://files.example/receipt_photos/{number}.jpg", uploaded_by=self.staff)
        self.assertEqual(self.queries(), short)


class AccessTests(TestCase):
    """The decorator that closes the page to everyone who is not staff sits on the page's own function (a helper placed above it once took the decorator
    by accident, on another page), so this is checked on its own."""

    def test_an_anonymous_visitor_is_sent_to_sign_in(self):
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_a_customer_is_turned_away(self):
        self.client.force_login(User.objects.create_user("pv_customer", password="x"))
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_staff_get_the_page(self):
        self.client.force_login(User.objects.create_user("pv_staff", password="x", is_staff=True))
        self.assertEqual(self.client.get(URL).status_code, 200)

    def test_a_manager_and_the_owner_get_the_page_too(self):
        from accounts.models import UserProfile
        for name, role in (("pv_manager", UserProfile.Role.MANAGER), ("pv_owner", UserProfile.Role.OWNER)):
            user = User.objects.create_user(name, password="x", is_staff=True)
            UserProfile.objects.update_or_create(user=user, defaults={"role": role})
            self.client.force_login(user)
            self.assertEqual(self.client.get(URL).status_code, 200, name)

    def test_the_helper_is_a_plain_function_not_a_page(self):
        from arabela_admin.views import _same_gcash_number_elsewhere
        self.assertFalse(hasattr(_same_gcash_number_elsewhere, "__wrapped__"))


class SharedStylesTests(SimpleTestCase):
    text = (ADMIN / "payment-verification.html").read_text(encoding="utf8")

    def test_the_page_uses_the_shared_list_styles_and_its_own_box_rule(self):
        self.assertEqual(self.text.count('{% include "arabela_admin/partials/ar_list_styles.html" %}'), 1)
        self.assertIn(".pv-card { margin-bottom: 1.25rem;", self.text)
        self.assertNotIn(".ar-line {", self.text)

    def test_no_column_is_sized_per_row(self):
        rule = re.search(r"\.ar-head\.pv-grid, \.ar-line\.pv-grid \{ grid-template-columns: ([^;]+); \}", self.text).group(1)
        self.assertNotIn("auto", rule)                                                  # an auto column would stop the titles lining up with the rows

    def test_no_blur_on_the_page(self):
        for blur in ("backdrop-blur", "backdrop-filter", "filter: blur"):
            self.assertNotIn(blur, self.text)

    def test_the_file_keeps_its_windows_line_endings(self):
        raw = (ADMIN / "payment-verification.html").read_bytes()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))                          # every line break is CRLF, none is bare LF
