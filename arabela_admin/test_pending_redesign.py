"""Pending Approval, redesigned: ONE card with one short line per booking (the Active Reservations look), a booking with several gowns shown as
"3 gowns" that opens to one short line per gown, the small actions as icon buttons (Approve / Reject stay as words), and "View payment"
opening the payment proof RIGHT THERE (with Approve / Reject inside it) instead of sending staff to Payment Verification and back.

How a booking is approved or rejected, the confirm / reason windows, the timeline and "Attach receipt" are untouched and keep their own
tests (tests.py, test_new_booking, test_double_actions). What is checked here: the order, the numbers the line shows, that every hook the
buttons and scripts need is still there, that the page asks the database the same number of questions however long the list is, and that
the page is still closed to anyone who is not staff."""
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escapejs

from arabela_admin.test_active_dropdown import ActiveDropdownBase, P, make_gown
from arabela_admin.views import _same_gcash_number_elsewhere, _waiting_text
from reservations.models import ReceiptRecord, Reservation, ReservationItem

User = get_user_model()
URL = reverse("arabela_admin:pending_approval")
ADMIN = Path(settings.BASE_DIR) / "templates" / "arabela_admin"


class PendingBase(ActiveDropdownBase):
    def pend(self, name, phone, gowns=1, *, proof="", gcash="", waited=0, channel=Reservation.Channel.ONLINE, pickup=3, ret=6, customer=None):
        """A booking waiting for approval, `waited` days ago, with `gowns` gowns."""
        reservation = Reservation.objects.create(
            customer=customer, customer_name=name, phone=phone, address="Antipolo", status=Reservation.Status.PENDING,
            payment_method=Reservation.PaymentMethod.GCASH, payment_proof_url=proof, gcash_reference=gcash, channel=channel,
            rental_subtotal=Decimal("3000") * gowns, security_deposit=Decimal("2000"), total_amount=Decimal("3000") * gowns + Decimal("2000"))
        Reservation.objects.filter(pk=reservation.pk).update(created_at=timezone.now() - timedelta(days=waited))
        reservation.refresh_from_db()
        items = []
        for number in range(gowns):
            self.counter += 1
            gown = make_gown(self.counter + 100 * Reservation.objects.count())
            items.append(ReservationItem.objects.create(
                reservation=reservation, gown=gown, gown_name=gown.name, gown_slug=gown.slug, size=gown.size, rental_price=gown.rental_price,
                rental_date=self.today + timedelta(days=pickup + number), event_date=self.today + timedelta(days=pickup + number + 2),
                return_date=self.today + timedelta(days=ret + number), overdue_date=self.today + timedelta(days=ret + number), stage=P))
        return reservation, items

    def page(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def booking_html(self, html, reservation):
        start = html.index('<div class="ar-booking" data-booking-id="%d"' % reservation.id)
        end = html.find('<div class="ar-booking" data-booking-id=', start + 10)
        return html[start:end if end != -1 else html.index('x-show="matchCount() === 0"', start)]


class OrderAndNumbersTests(PendingBase):
    def test_the_booking_that_has_waited_longest_is_first(self):
        newest, _ = self.pend("Newest Request", "09170000001", waited=0)
        oldest, _ = self.pend("Oldest Request", "09170000002", waited=9)
        middle, _ = self.pend("Middle Request", "09170000003", waited=4)
        response, html = self.page()
        self.assertEqual([r.id for r in response.context["reservations"]], [oldest.id, middle.id, newest.id])
        at = lambda r: html.index('<div class="ar-booking" data-booking-id="%d"' % r.id)        # (the names also appear in the header bell, so go by the booking's own box)
        self.assertLess(at(oldest), at(middle))
        self.assertLess(at(middle), at(newest))

    def test_how_long_a_booking_has_waited_is_said_in_plain_words(self):
        today = timezone.localdate()
        at = lambda days: timezone.now() - timedelta(days=days)
        self.assertEqual(_waiting_text(at(0), today), "Requested today")
        self.assertEqual(_waiting_text(at(1), today), "Waiting 1 day")
        self.assertEqual(_waiting_text(at(5), today), "Waiting 5 days")
        self.assertEqual(_waiting_text(timezone.now() + timedelta(hours=2), today), "Requested today")         # a clock a little ahead never says "-1 days"

    def test_the_line_shows_the_gown_count_the_first_pickup_the_last_return_and_the_total(self):
        reservation, items = self.pend("Three Gowns", "0917 000 0004", gowns=3, waited=2, pickup=3, ret=6)
        response, html = self.page()
        shown = next(r for r in response.context["reservations"] if r.id == reservation.id)
        self.assertEqual((shown.gown_count, shown.span_start, shown.span_end), (3, items[0].rental_date, items[2].return_date))
        self.assertEqual(shown.waiting_text, "Waiting 2 days")
        block = self.booking_html(html, reservation)
        self.assertIn("3 gowns", block)
        self.assertIn(f"&#8369;{shown.total_amount:,.0f}", block)
        self.assertIn("Waiting 2 days", block)
        start, end = items[0].rental_date, items[2].return_date
        self.assertIn(f"{start:%b} {start.day} &rarr; {end:%b} {end.day}, {end.year}", block)

    def test_a_one_gown_booking_names_its_gown_and_tag_on_the_line_and_has_no_count_pill(self):
        reservation, [item] = self.pend("One Gown", "09170000005", gowns=1)
        _, html = self.page()
        block = self.booking_html(html, reservation)
        self.assertNotIn("ar-count-pill", block)
        closed = block[:block.index('<div class="ar-panel"')]
        self.assertIn(item.gown_name, closed)
        self.assertIn("White tag", closed)

    def test_every_gown_of_a_booking_has_its_own_short_line_with_its_dates_and_price(self):
        reservation, items = self.pend("Lines", "09170000006", gowns=3)
        _, html = self.page()
        panel = self.booking_html(html, reservation)
        panel = panel[panel.index('<div class="ar-panel"'):]
        self.assertEqual(panel.count('class="ar-gl pa-gl"'), 3)
        for number, item in enumerate(items, start=1):
            self.assertIn(item.gown_name, panel)
            self.assertIn(f"{number} of 3", panel)
            self.assertIn(f"&#8369;{item.rental_price:,.0f}", panel)


class SameGcashNumberTests(PendingBase):
    def test_the_one_query_answer_is_the_models_own_answer(self):
        a, _ = self.pend("Dup A", "09170000011", gcash="GC-1001", waited=3)
        b, _ = self.pend("Dup B", "09170000012", gcash="GC-1001", waited=2)
        c, _ = self.pend("Dup C", "09170000013", gcash="GC-2002", waited=1)
        d, _ = self.pend("No Number", "09170000014", gcash="")
        elsewhere = Reservation.objects.create(customer_name="Other", phone="1", address="x", status=Reservation.Status.CONFIRMED, gcash_reference="GC-1001")
        pending = list(Reservation.objects.filter(status=Reservation.Status.PENDING))
        answer = _same_gcash_number_elsewhere(pending)
        for r in pending:
            with self.subTest(booking=r.customer_name):
                self.assertEqual(answer.get(r.id, ""), r.gcash_duplicate_codes)
        self.assertIn(b.reference_code, answer[a.id])
        self.assertIn(elsewhere.reference_code, answer[a.id])       # a booking that is NOT pending counts too
        self.assertEqual(answer[c.id], "")
        self.assertNotIn(d.id, answer)

    def test_the_line_and_the_window_warn_about_a_number_that_is_on_another_booking(self):
        a, _ = self.pend("Warn A", "09170000015", gcash="GC-3003", proof="https://files.example/a.jpg", waited=2)
        b, _ = self.pend("Warn B", "09170000016", gcash="GC-3003", proof="https://files.example/b.jpg", waited=1)
        _, html = self.page()
        self.assertIn(f"Same GCash no. as {b.reference_code}", self.booking_html(html, a))
        self.assertIn(f"gcashDup: '{escapejs(b.reference_code)}'", self.booking_html(html, a))


class FewQuestionsTests(PendingBase):
    def count_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.client.get(URL)                    # the first visit also creates the shop-settings row: not what is being counted
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get(URL).status_code, 200)
        return len(queries)

    def test_a_longer_list_does_not_ask_the_database_more_questions(self):
        for number in range(3):
            self.pend(f"Few {number}", f"0917100000{number}", gowns=2, gcash=f"GC-F{number}", proof="https://files.example/f.jpg", waited=number)
        short = self.count_queries()
        for number in range(3, 14):
            self.pend(f"Many {number}", f"091710000{number:02d}", gowns=3, gcash=f"GC-F{number}", proof="https://files.example/f.jpg", waited=number)
        self.assertEqual(self.count_queries(), short)

    def test_receipts_come_with_the_staff_member_who_attached_each_one_in_one_query(self):
        for number in range(2):
            reservation, _ = self.pend(f"Receipts {number}", f"0917200000{number}", proof="https://files.example/r.jpg", gcash=f"GC-R{number}")
            for photo in range(2):
                ReceiptRecord.objects.create(reservation=reservation, photo_url=f"https://files.example/receipt_photos/{number}{photo}.jpg", uploaded_by=self.staff)
        short = self.count_queries()
        for number in range(2, 9):
            reservation, _ = self.pend(f"Receipts {number}", f"0917200000{number}", proof="https://files.example/r.jpg", gcash=f"GC-R{number}")
            ReceiptRecord.objects.create(reservation=reservation, photo_url=f"https://files.example/receipt_photos/{number}.jpg", uploaded_by=self.staff)
        self.assertEqual(self.count_queries(), short)


class AccessTests(TestCase):
    """The decorator that closes the page to everyone who is not staff sits on the page's own function (a helper placed above it once took
    the decorator by accident), so this is checked on its own."""

    def test_an_anonymous_visitor_is_sent_to_sign_in(self):
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_a_customer_is_turned_away(self):
        customer = User.objects.create_user("pa_customer", password="x")
        self.client.force_login(customer)
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_staff_get_the_page(self):
        staff = User.objects.create_user("pa_staff", password="x", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(URL).status_code, 200)

    def test_the_helpers_are_plain_functions_not_pages(self):
        from arabela_admin import views
        for helper in (views._waiting_text, views._same_gcash_number_elsewhere):
            self.assertFalse(hasattr(helper, "__wrapped__"), helper.__name__)        # nothing decorated them


class MarkupPromisesTests(PendingBase):
    def test_the_card_is_the_active_reservations_look_with_the_pagers_hooks(self):
        self.pend("Look", "09170000021")
        _, html = self.page()
        self.assertEqual(html.count('class="pa-card"'), 1)
        self.assertIn('data-arpg=".ar-booking" data-arpg-noun="bookings" data-arpg-top="off" data-arpg-count="#ar-count"', html)
        self.assertRegex(html, r'<div class="custom-scrollbar overflow-y-auto" style="max-height: 760px;" data-arpg=')
        self.assertIn('placeholder="Search by customer, reference, cellphone or gown ID..."', html)
        self.assertNotIn("<table", html[html.index('class="pa-card"'):html.index('x-show="openCustomerModal"')])

    def test_the_words_that_only_repeated_the_page_title_are_gone(self):
        self.pend("Quiet", "09170000022")
        _, html = self.page()
        for gone in ("Awaiting confirmation", "Reservations Awaiting Confirmation", 'status-legend'):
            self.assertNotIn(gone, html)
        self.assertNotRegex(html, r'<p class="inline-flex rounded-full bg-warning-50[^>]*>Pending</p>')

    def test_approve_and_reject_keep_their_words_and_every_hook_the_script_needs(self):
        reservation, _ = self.pend("Hooks", "09170000023", proof="https://files.example/h.jpg")
        _, html = self.page()
        block = self.booking_html(html, reservation)
        approve = re.search(r'<button[^>]*class="resv-btn resv-btn-approve[^"]*"[^>]*>Approve</button>', block, re.S).group(0)
        reject = re.search(r'<button[^>]*class="resv-btn resv-btn-reject[^"]*"[^>]*>Reject</button>', block, re.S).group(0)
        for needle in (f'data-url="{reverse("arabela_admin:reservation_approve", args=[reservation.id])}"', 'data-label="approved"',
                       f'data-ref="{reservation.reference_code}"', 'data-walkin="0"', 'data-receipts="0"', 'onclick="reviewReservation(this)"'):
            self.assertIn(needle, approve)
        for needle in (f'data-url="{reverse("arabela_admin:reservation_reject", args=[reservation.id])}"', 'data-label="rejected"', 'onclick="reviewReservation(this)"'):
            self.assertIn(needle, reject)

    def test_a_click_on_an_action_does_not_open_the_booking_and_is_not_stopped(self):
        """The line ignores clicks that come from the actions; the actions themselves must NOT stop the click, because the Attach receipt
        window (partials/attach_receipt.html) listens for its button on the whole document, and a stopped click never gets there."""
        reservation, _ = self.pend("Clicks", "09170000024", proof="https://files.example/c.jpg")
        _, html = self.page()
        block = self.booking_html(html, reservation)
        expected = "@click=\"if (!$event.target.closest('.pa-acts')) { toggleBooking(%d) }\"" % reservation.id
        self.assertIn(expected, block)
        self.assertIn('<div class="pa-acts">', block)
        self.assertNotIn("@click.stop", block[block.index('<div class="pa-acts">'):block.index('class="ar-chev"')])

    def test_view_payment_opens_the_proof_on_this_page_and_no_longer_leaves_for_payment_verification(self):
        reservation, _ = self.pend("Stays Here", "09170000025", proof="https://files.example/s.jpg", gcash="GC-S1")
        _, html = self.page()
        block = self.booking_html(html, reservation)
        button = re.search(r'<button[^>]*title="View payment"[^>]*>', block, re.S)
        self.assertIsNotNone(button)
        self.assertIn("openReceiptModal = true", button.group(0))
        self.assertIn("proofUrl: 'https://files.example/s.jpg'", button.group(0))
        self.assertIn(f"gcashRef: '{escapejs('GC-S1')}'", button.group(0))
        self.assertIn(f"approveUrl: '{reverse('arabela_admin:reservation_approve', args=[reservation.id])}'", button.group(0))
        self.assertNotIn(reverse("arabela_admin:payment_verification"), html[html.index('class="pa-card"'):html.index('x-show="openCustomerModal"')])
        self.assertEqual(html.count("GCash Payment Proof"), 1)                    # the window is on the page once, whatever the number of bookings

    def test_the_payment_window_has_only_the_x_and_approve_reject_that_run_the_pages_own_script(self):
        self.pend("Window", "09170000026", proof="https://files.example/w.jpg")
        _, html = self.page()
        window = html[html.index("GCash Payment Proof") - 2500:html.index('x-show="openCustomerModal"')]
        self.assertNotRegex(window, r">\s*Close\s*<")
        self.assertIn('@click="openReceiptModal = false"', window)
        self.assertIn("$event.target.closest('#adm-dialog')", window)             # pressing Cancel in the confirm window must not also close the proof
        footer = window[window.index("data-resv-scope"):]
        self.assertEqual(footer.count("onclick=\"reviewReservation(this)\""), 2)
        for bound in (':data-url="viewingPayment.approveUrl"', ':data-url="viewingPayment.rejectUrl"', ':data-ref="viewingPayment.reservation"',
                      ':data-walkin="viewingPayment.walkin ? \'1\' : \'0\'"', ':data-receipts="viewingPayment.receiptCount"'):
            self.assertIn(bound, footer)

    def test_the_review_script_disables_the_buttons_of_the_booking_or_window_being_answered(self):
        self.pend("Script", "09170000027")
        _, html = self.page()
        self.assertIn("btn.closest('tr, .ar-booking, [data-resv-scope]')", html)
        self.assertIn("function paSetupBookings(state, watch)", html)
        self.assertIn("paSetupBookings($data, $watch);", html)

    def test_a_walk_in_that_has_not_paid_keeps_attach_receipt_and_its_receipt_count(self):
        reservation, _ = self.pend("Walk In", "09170000028", channel=Reservation.Channel.WALK_IN)
        ReceiptRecord.objects.create(reservation=reservation, photo_url="https://files.example/receipt_photos/walkin.jpg", uploaded_by=self.staff)
        _, html = self.page()
        block = self.booking_html(html, reservation)
        self.assertIn('class="ar-open-btn"', block)
        self.assertIn(">Attach receipt</button>", block)
        self.assertNotIn('title="View payment"', block)                          # there is no online proof to view
        self.assertIn("Not paid yet", block)
        self.assertIn(f'data-receipt-count-for="{reservation.reference_code}" data-count="1">1 receipt attached', block)
        self.assertIn('data-walkin="1"', block)
        self.assertIn('data-receipts="1"', block)

    def test_an_online_booking_with_no_proof_says_so(self):
        reservation, _ = self.pend("No Proof", "09170000029", proof="")
        _, html = self.page()
        self.assertIn("No proof uploaded", self.booking_html(html, reservation))

    def test_what_staff_type_in_the_search_box_finds_the_name_the_number_the_gown_and_the_phone(self):
        reservation, [item] = self.pend("Search Me", "0917 555 1234")
        _, html = self.page()
        haystack = re.search(r'data-booking-id="%d" data-search="([^"]*)"' % reservation.id, html).group(1)
        for word in ("search me", reservation.reference_code.lower(), item.gown.gown_id.lower(), "09175551234"):
            self.assertIn(word, haystack)

    def test_the_customer_window_and_the_timeline_are_still_wired(self):
        reservation, _ = self.pend("Wired", "09170000030", proof="https://files.example/x.jpg")
        _, html = self.page()
        block = self.booking_html(html, reservation)
        self.assertIn("openCustomerModal = true", block)
        self.assertIn("viewingCustomer = {", block)
        self.assertIn(f"openTimeline = (openTimeline === {reservation.id} ? null : {reservation.id})", block)
        self.assertIn('<div class="arb-tl-panel">', html)                       # the timeline partial is still drawn

    def test_an_empty_list_says_so(self):
        _, html = self.page()
        self.assertIn("No reservations are waiting for approval right now.", html)


class SharedStylesTests(PendingBase):
    """Active Reservations and Pending Approval draw the same list look from ONE partial."""

    def test_both_pages_use_the_shared_list_styles_once(self):
        self.pend("Shared", "09170000031")
        _, pending = self.page()
        active = self.client.get(reverse("arabela_admin:active_reservations")).content.decode()
        for html in (pending, active):
            self.assertEqual(html.count(".ar-line:hover { background: rgba(152,162,179,.08); }"), 1)
            self.assertEqual(html.count(".ar-ib { display: inline-flex;"), 1)

    def test_the_list_box_rule_is_not_shared_because_the_attach_receipt_window_uses_the_same_class_name(self):
        _, pending = self.page()
        active = self.client.get(reverse("arabela_admin:active_reservations")).content.decode()
        self.assertIn(".ar-card { margin-bottom: 1.25rem;", active)               # Active Reservations draws its box with .ar-card
        self.assertNotIn(".ar-card { margin-bottom: 1.25rem;", pending)           # ...Pending Approval must NOT: that would re-style the attach window
        self.assertIn(".ar-card { width: 100%; max-width: 30rem;", pending)       # the attach-receipt window keeps its own .ar-card exactly
        self.assertIn(".pa-card { margin-bottom: 1.25rem;", pending)

    def test_the_shared_file_holds_every_rule_active_reservations_had(self):
        shared = (ADMIN / "partials" / "ar_list_styles.html").read_text(encoding="utf8")
        for rule in (".ar-toolbar {", ".ar-line {", ".ar-panel {", ".ar-ib {", ".ar-chev {", ".ar-filter {", ".ar-tip-pop {", ".ar-gl {", "@media (max-width: 760px) {"):
            self.assertIn(rule, shared)
        self.assertNotIn(".ar-card {", shared)
        page = (ADMIN / "active-reservations.html").read_text(encoding="utf8")
        self.assertIn('{% include "arabela_admin/partials/ar_list_styles.html" %}', page)
        self.assertNotIn(".ar-line {", page)                                      # no second copy left behind


class TemplateOnlyTests(SimpleTestCase):
    def test_the_payment_window_is_a_partial_with_the_parameters_it_says(self):
        text = (ADMIN / "partials" / "payment_proof_window.html").read_text(encoding="utf8")
        for word in ("{{ var }}", "{{ flag }}", "with_actions"):
            self.assertIn(word, text)
        self.assertNotIn("backdrop-blur", text)
        self.assertNotRegex(text, r"<button[^>]*>\s*Close\s*</button>")

    def test_pending_approval_includes_the_window_once_with_actions(self):
        text = (ADMIN / "pending-approval.html").read_text(encoding="utf8")
        include = '{% include "arabela_admin/partials/payment_proof_window.html" with var="viewingPayment" flag="openReceiptModal" with_actions=1 %}'
        self.assertEqual(text.count(include), 1)
