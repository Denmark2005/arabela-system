"""The dashboard, redesigned: the overview is two slim strips (the four tall tiles took about 480px) and Quick Verify has the Pending Approval look
(one card, one short line per payment, icon buttons, "N waiting", "Showing the latest 5 of N").

What is checked here: every number is still the number the page it links to shows, the strips and the list say what they should (and not what they
used to), the three icon buttons keep every hook the confirm script and the proof window need, the list stays short and the page asks the database the
same number of questions however many payments are waiting, and the page is still closed to anyone who is not staff. The proof window itself keeps its
own tests (tests.py QuickVerifyProofPopupTests, test_new_booking, test_role_permissions)."""
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
from django.utils.html import escapejs

from accounts.models import UserProfile
from arabela_admin.tests import _make_gown
from gowns.models import Gown
from reservations.models import Reservation, ReservationItem

User = get_user_model()
URL = reverse("arabela_admin:dashboard")
TEMPLATE = Path(settings.BASE_DIR) / "templates" / "arabela_admin" / "dashboard.html"
PROOF = "https://res.cloudinary.com/demo/image/upload/payment_proofs/receipt.jpg"


class DashboardBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("db_staff", password="x", is_staff=True)
        cls.customer = User.objects.create_user("db_customer", email="db.customer@gmail.com", password="x")
        UserProfile.objects.update_or_create(user=cls.customer, defaults={"display_name": "Account Holder"})

    def setUp(self):
        self.client.force_login(self.staff)
        self.n = 0

    def pending(self, name="Waiting Customer", *, hours_ago=0, status=Reservation.Status.PENDING, customer=None, total="2500"):
        self.n += 1
        reservation = Reservation.objects.create(
            customer=customer, customer_name=name, phone="0917%07d" % self.n, status=status, payment_method="GCash", payment_proof_url=PROOF,
            gcash_reference="%013d" % self.n, total_amount=Decimal(total))
        Reservation.objects.filter(pk=reservation.pk).update(created_at=timezone.now() - timedelta(hours=hours_ago))
        return Reservation.objects.get(pk=reservation.pk)

    def page(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def card(self, html):
        return html[html.index('class="qv-card"'):html.index("Quick Verify End")]


def region(html, name):
    """The inside of one live part of the page (data-live-region="name")."""
    start = html.index('data-live-region="%s"' % name)
    start = html.index(">", start) + 1
    depth, i = 1, start
    for token in re.finditer(r"<(/?)div\b[^>]*>", html[start:]):
        depth += -1 if token.group(1) else 1
        if depth == 0:
            return html[start:start + token.start()]
    return html[start:]


class OverviewCardTests(DashboardBase):
    def test_the_booking_figures_are_separate_boxes_and_each_still_opens_its_own_page(self):
        _make_gown(1)
        response, html = self.page()
        card = region(html, "overview-bookings")
        for label, url, number in (
            ("Registered Customers", reverse("arabela_admin:clients"), response.context["registered_customers"]),
            ("Active Rentals", reverse("arabela_admin:active_reservations"), response.context["active_rentals"]),
            ("Reservations This Month", response.context["reservations_this_month_url"], response.context["reservations_this_month"]),
        ):
            with self.subTest(label=label):
                self.assertIn(label, card)
                self.assertIn('href="%s"' % url.replace("&", "&amp;"), card)
                self.assertIn('<span class="db-fig-num">%s</span>' % number, card)
        self.assertEqual(card.count('class="db-fig"'), 3)
        self.assertIn('<span class="ov-tag">%s</span>' % response.context["reservations_this_month_label"], card)    # "November 2026" is a small tag

    def test_the_old_tiles_and_strips_are_gone(self):
        _make_gown(1)
        _, html = self.page()
        for gone in ("metric-tile-wide", "admin-stat-grid-4", ">Picked up in", "ov-strip", "ov-cell", "Staff activity today", "New bookings today"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, html)

    def test_the_gown_figures_are_five_boxes_with_the_catalogs_words_and_circles(self):
        [_make_gown(i, status=Gown.Status.AVAILABLE) for i in (1, 2)] + [_make_gown(3, status=Gown.Status.RESERVED)]
        response, html = self.page()
        card = region(html, "overview-gowns")
        for word in ("Gown Inventory", "Total Gowns", "Available", "Reserved", "Blocked Gowns", "Out-of-Stock"):
            self.assertIn(word, card)
        for dot in ("ov-dot--available", "ov-dot--reserved", "ov-dot--blocked", "ov-dot--oos"):
            self.assertIn(dot, card)
        self.assertEqual([response.context[k] for k in ("gown_total", "gown_available", "gown_reserved", "gown_blocked", "gown_out_of_stock")], [3, 2, 1, 0, 0])
        self.assertEqual(len(re.findall(r'<span class="db-fig-num[^"]*">\d+</span>', card)), 5)
        self.assertEqual(card.count('class="db-fig"'), 5)                                   # each figure is its own box, each one a link
        catalog = reverse("arabela_admin:gown_catalog")
        self.assertEqual(card.count('href="%s"' % catalog), 1)                              # Total: the whole catalog
        self.assertIn('href="%s?status=Available"' % catalog, card)                         # the others open it on exactly those gowns
        self.assertIn('href="%s?status=Blocked"' % catalog, card)
        self.assertIn('href="%s?status=Reserved"' % catalog, card)                          # the catalog's own filter counts exactly these
        self.assertIn('href="%s?status=Out-of-Stock"' % catalog, card)

    def test_a_zero_blocked_is_grey_and_says_none_blocked_today_on_hover(self):
        _make_gown(1)
        _, html = self.page()
        self.assertIn('title="None blocked today"', html)
        self.assertRegex(html, r'<span class="db-fig-num is-zero">0</span>')

    def test_with_an_empty_catalog_the_alert_replaces_the_gown_card(self):
        response, html = self.page()
        self.assertTrue(response.context["catalog_is_empty"])
        self.assertIn("Add Your First Gown", html)
        self.assertNotIn('data-live-region="overview-gowns"', html)
        self.assertIn('data-live-region="overview-bookings"', html)                         # the booking figures are still there

    def test_the_greeting_comes_first_then_the_money_then_the_figures_then_quick_verify(self):
        _make_gown(1)
        _, html = self.page()
        order = [html.index('id="db-greet"'), html.index('data-live-region="overview-top"'), html.index('data-live-region="overview-gowns"'),
                 html.index('data-live-region="overview-bookings"'), html.index('class="qv-card"')]
        self.assertEqual(order, sorted(order))

    def test_the_new_booking_button_is_still_there(self):
        _make_gown(1)
        _, html = self.page()
        self.assertIn('href="%s"' % reverse("arabela_admin:new_booking"), html[html.index("<main>"):])
        self.assertIn("+ New Booking", html)


class QuickVerifyListTests(DashboardBase):
    def test_a_short_list_shows_every_payment_the_count_and_no_footer(self):
        for i in range(3):
            self.pending("Short %d" % i, hours_ago=i)
        response, html = self.page()
        card = self.card(html)
        self.assertEqual(card.count("data-qv-row"), 3)
        self.assertIn("3 waiting", card)
        self.assertNotIn("Showing the latest", card)
        self.assertEqual((response.context["pending_total"], response.context["quick_verify_count"]), (3, 3))

    def test_a_long_list_still_shows_five_and_says_how_many_are_waiting(self):
        for i in range(12):
            self.pending("Long %d" % i, hours_ago=i)
        response, html = self.page()
        card = self.card(html)
        self.assertEqual(card.count("data-qv-row"), 5)                             # a fixed short list, however many are waiting
        self.assertIn("12 waiting", card)
        self.assertIn("Showing the latest 5 of 12", card)
        self.assertEqual((response.context["pending_total"], response.context["quick_verify_count"]), (12, 5))

    def test_only_pending_payments_are_counted_and_listed(self):
        pending = self.pending("Still Pending")
        self.pending("Already Confirmed", status=Reservation.Status.CONFIRMED)
        self.pending("Already Rejected", status=Reservation.Status.REJECTED)
        response, html = self.page()
        card = self.card(html)
        self.assertEqual(response.context["pending_total"], 1)
        self.assertIn(pending.reference_code, card)
        self.assertNotIn("Already Confirmed", card)
        self.assertNotIn("Already Rejected", card)

    def test_the_newest_payment_is_first_like_before(self):
        old = self.pending("Oldest", hours_ago=30)
        new = self.pending("Newest", hours_ago=1)
        mid = self.pending("Middle", hours_ago=10)
        response, html = self.page()
        card = self.card(html)
        self.assertEqual([r.id for r in response.context["quick_verify_reservations"]], [new.id, mid.id, old.id])
        self.assertLess(card.index(new.reference_code), card.index(mid.reference_code))
        self.assertLess(card.index(mid.reference_code), card.index(old.reference_code))

    def test_with_nothing_waiting_it_says_so_and_shows_no_count_or_footer(self):
        response, html = self.page()
        card = self.card(html)
        self.assertIn("No payments awaiting review right now.", card)
        self.assertNotIn("waiting</span>", card)
        self.assertNotIn("Showing the latest", card)
        self.assertEqual(card.count("data-qv-row"), 0)
        self.assertEqual(response.context["pending_total"], 0)

    def test_see_all_opens_payment_verification_in_the_header_and_the_footer(self):
        for i in range(7):
            self.pending("See %d" % i, hours_ago=i)
        _, html = self.page()
        card = self.card(html)
        self.assertEqual(card.count('href="%s"' % reverse("arabela_admin:payment_verification")), 2)


class QuickVerifyRowTests(DashboardBase):
    def row(self, html, reservation):
        start = html.index(reservation.reference_code, html.index('class="qv-card"'))
        start = html.rindex("data-qv-row", 0, start)
        limit = html.index("Quick Verify End")
        end = html.find("data-qv-row", start + 10, limit)
        return html[start:end if end != -1 else limit]

    def test_one_short_line_with_the_name_the_booking_number_the_amount_and_the_day(self):
        reservation = self.pending("Lorna Reyes", hours_ago=3, total="2500")
        _, html = self.page()
        row = self.row(html, reservation)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row.split('<div class="qv-acts">')[0]))
        for piece in ("Lorna Reyes", reservation.reference_code, "2,500", f"{timezone.localtime(reservation.created_at):%b} {timezone.localtime(reservation.created_at).day}"):
            self.assertIn(piece, text)
        self.assertNotIn("Gown", text)                                              # no "Gown + Total" sub-line any more
        self.assertIn('class="ar-dot qv-dot"', row)                                 # the amber "waiting" circle before the name
        self.assertIn('title="Sent ', row)                                          # the full date and time on hover

    def test_booked_as_shows_under_the_name_only_when_the_account_booked_under_another_name(self):
        shown = self.pending("Typed By Staff", customer=self.customer)
        plain = self.pending("Plain Walk In")
        _, html = self.page()
        self.assertIn("Booked as: Typed By Staff", self.row(html, shown))
        self.assertNotIn("Booked as", self.row(html, plain))

    def test_three_icon_buttons_view_proof_approve_and_reject(self):
        reservation = self.pending("Icon Person")
        _, html = self.page()
        row = self.row(html, reservation)
        acts = row[row.index('<div class="qv-acts">'):]
        self.assertEqual(acts.count("<button"), 3)
        for word in (">Approve<", ">Reject<", ">View Proof<", ">Verify<"):
            self.assertNotIn(word, row)                                              # no word buttons left
        for title in ("View proof", "Approve payment", "Reject payment"):
            self.assertIn('title="%s"' % title, acts)
        self.assertEqual(acts.count('aria-label="'), 3)
        self.assertEqual(acts.count("<svg"), 3)

    def test_the_buttons_keep_every_hook_the_confirm_script_and_the_window_need(self):
        reservation = self.pending("Hook Person")
        _, html = self.page()
        row = self.row(html, reservation)
        self.assertIn('data-url="%s"' % reverse("arabela_admin:reservation_approve", args=[reservation.id]), row)
        self.assertIn('data-url="%s"' % reverse("arabela_admin:reservation_reject", args=[reservation.id]), row)
        self.assertEqual(row.count('onclick="reviewQuickVerify(this)"'), 2)
        for piece in ('data-action="verify"', 'data-action="reject"', 'data-customer="Hook Person"', 'data-reservation="%s"' % reservation.reference_code,
                      "openReceiptModal = true", "proofUrl: '%s'" % PROOF, "gcashRef: '%s'" % reservation.gcash_reference, "receipts: JSON.parse(", "breakdown: JSON.parse("):
            self.assertIn(piece, row)
        self.assertIn("x-data=\"{ status: 'Pending' }\"", row)
        self.assertEqual(row.count("x-show=\"status === 'Pending'\""), 2)           # approve and reject (view proof is always there)
        # the confirm script now looks for ITS OWN row, not the nearest rounded box (which is the whole card)
        self.assertIn("btn.closest('[data-qv-row]')", html)
        self.assertNotIn("btn.closest('.rounded-2xl')", html)

    def test_the_top_bar_search_still_hides_rows_and_the_fake_spinner_is_gone(self):
        reservation = self.pending("Search Person")
        _, html = self.page()
        row = self.row(html, reservation)
        self.assertIn("x-show=\"headerSearch === '' ||", row)
        self.assertIn("includes(headerSearch.toLowerCase())", row)
        self.assertIn("No matching customer found.", self.card(html))
        self.assertNotIn('x-show="filtering"', html)
        self.assertNotIn('x-show="!filtering"', html)

    def test_names_with_quotes_and_tags_are_escaped_and_the_page_still_renders(self):
        reservation = self.pending("O'Neil \"Q\" <b>x</b>")
        _, html = self.page()
        card = self.card(html)
        self.assertNotIn("<b>x</b>", card)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", card)
        self.assertIn(reservation.reference_code, card)
        for leak in ("{%", "{{", "{#"):
            self.assertNotIn(leak, html)

    def test_the_page_asks_the_database_the_same_number_of_questions_however_many_are_waiting(self):
        def queries():
            with CaptureQueriesContext(connection) as captured:
                self.assertEqual(self.client.get(URL).status_code, 200)
            return len(captured.captured_queries)

        self.client.get(URL)                       # the once-a-day reminder sweep runs on the first load of the day: keep its queries out of the count
        self.pending("Q1")
        few = queries()
        for i in range(2, 14):
            self.pending("Q%d" % i, hours_ago=i)
        self.assertEqual(queries(), few)           # 1 row or the 5 the list shows: the same number of questions (no query per row)

    # ---- the title row, the labels and the pick-up / return columns
    def item(self, reservation, pickup, ret, name="Test Gown", **extra):
        return ReservationItem.objects.create(
            reservation=reservation, gown_name=name, size="Medium", rental_price=Decimal("3000"), rental_date=pickup, return_date=ret, **extra)

    def day(self, offset):
        d = timezone.localdate() + timedelta(days=offset)
        return f"{d:%b} {d.day}, {d.year}"

    def test_the_title_row_names_every_column_in_the_order_the_values_come(self):
        self.pending("Title Person")
        _, html = self.page()
        start = html.index('class="qv-cols"')
        titles = re.findall(r"<span>([^<]*)</span>", html[start:html.index("</div>", start)])
        self.assertEqual(titles, ["Customer", "Booking no.", "Deposit paid", "Pick-up", "Return", "Submitted", "Actions"])

    def test_with_nothing_waiting_there_is_no_title_row(self):
        _, html = self.page()
        self.assertNotIn('class="qv-cols"', html)
        self.assertIn("No payments awaiting review right now.", html)

    def test_each_value_carries_its_own_label_for_laptops_and_phones(self):
        reservation = self.pending("Label Person")
        _, html = self.page()
        row = self.row(html, reservation)
        for label in ("Booking no.", "Deposit paid", "Pick-up", "Return", "Submitted"):
            with self.subTest(label=label):
                self.assertIn('data-label="%s"' % label, row)

    def test_pickup_and_return_are_the_first_pickup_and_the_last_return_of_the_booking(self):
        reservation = self.pending("Two Gowns")
        self.item(reservation, timezone.localdate() + timedelta(days=10), timezone.localdate() + timedelta(days=14), "First")
        self.item(reservation, timezone.localdate() + timedelta(days=8), timezone.localdate() + timedelta(days=12), "Second")
        _, html = self.page()
        row = self.row(html, reservation)
        self.assertIn('data-label="Pick-up">%s<' % self.day(8), row)
        self.assertIn('data-label="Return">%s<' % self.day(14), row)

    def test_a_single_gown_shows_its_own_dates_with_the_year(self):
        reservation = self.pending("One Gown")
        self.item(reservation, timezone.localdate() + timedelta(days=40), timezone.localdate() + timedelta(days=44))
        _, html = self.page()
        row = self.row(html, reservation)
        self.assertIn('data-label="Pick-up">%s<' % self.day(40), row)
        self.assertIn('data-label="Return">%s<' % self.day(44), row)

    def test_a_gown_the_shop_cancelled_does_not_stretch_the_window(self):
        reservation = self.pending("Cancelled One")
        today = timezone.localdate()
        self.item(reservation, today + timedelta(days=10), today + timedelta(days=14), "Kept")
        self.item(reservation, today + timedelta(days=2), today + timedelta(days=30), "Dropped", stage=ReservationItem.Stage.CANCELLED)
        _, html = self.page()
        row = self.row(html, reservation)
        self.assertIn('data-label="Pick-up">%s<' % self.day(10), row)
        self.assertIn('data-label="Return">%s<' % self.day(14), row)

    def test_a_booking_with_no_gowns_shows_dashes_not_an_error(self):
        reservation = self.pending("No Gowns")
        _, html = self.page()
        row = self.row(html, reservation)
        self.assertIn('data-label="Pick-up">&mdash;<', row)
        self.assertIn('data-label="Return">&mdash;<', row)

    def test_deposit_paid_is_the_deposit_and_only_a_booking_that_cannot_be_split_says_total(self):
        split = self.pending("Split Person")
        Reservation.objects.filter(pk=split.pk).update(security_deposit=Decimal("2000"), rental_subtotal=Decimal("3000"), total_amount=Decimal("5000"))
        plain = self.pending("Plain Person", total="2500")
        _, html = self.page()
        split_row, plain_row = self.row(html, split), self.row(html, plain)
        self.assertIn('data-label="Deposit paid"', split_row)
        self.assertIn("₱2,000", split_row.split('data-label="Pick-up"')[0])
        self.assertNotIn("qv-tot", split_row)                                       # the deposit alone: nothing to explain
        self.assertIn("2,500", plain_row)
        self.assertIn('<span class="qv-tot">total</span>', plain_row)                # a total that could not be split says so

    def test_the_duplicate_gcash_warning_is_the_same_as_before_for_every_row(self):
        a = self.pending("Dup A")
        b = self.pending("Dup B")
        c = self.pending("Dup C")
        Reservation.objects.filter(pk=b.pk).update(gcash_reference=a.gcash_reference)                  # B has A's number
        Reservation.objects.filter(pk=c.pk).update(proof_typed_reference=a.gcash_reference)            # C TYPED A's number
        _, html = self.page()
        for reservation in (a, b, c):
            reservation.refresh_from_db()
            with self.subTest(reservation=reservation.reference_code):
                self.assertTrue(reservation.gcash_duplicate_codes)                                      # each of the three really has a duplicate
                self.assertIn("gcashDup: '%s'" % escapejs(reservation.gcash_duplicate_codes), self.row(html, reservation))
        self.assertIn(b.reference_code, a.gcash_duplicate_codes)


class StaticTemplateTests(SimpleTestCase):
    def setUp(self):
        self.source = TEMPLATE.read_text(encoding="utf8")

    def test_the_shared_list_look_is_included_once_and_the_page_has_its_own_rules(self):
        self.assertEqual(self.source.count('{% include "arabela_admin/partials/ar_list_styles.html" %}'), 1)
        for rule in (".db-card {", ".db-fig {", ".qv-card {", ".qv-line {", ".ar-ib.qv-ok {", ".ar-ib.qv-no {"):
            self.assertEqual(self.source.count("\n    " + rule), 1, rule)
        self.assertNotIn("metric-tile-wide", self.source)

    def test_every_color_rule_has_a_dark_twin(self):
        for light in (".db-card {", ".db-fig {", ".db-greet {", ".db-money-num {", ".db-fig-num {", ".db-alert {", ".ov-ic {", ".qv-card {", ".qv-line {",
                      ".qv-title {", ".qv-pill {"):
            self.assertIn("\n    .dark " + light, self.source, light)

    def test_the_small_screens_have_their_own_layout(self):
        for rule in ("@media (max-width: 1279px)", "@media (max-width: 760px)",
                     "@container db (max-width: 76rem)", "@container db (max-width: 42rem)", "@container db (max-width: 30rem)",
                     "grid-template-areas: \"who who who who acts\" \"ref amt pick ret sent\"",          # a laptop / tablet: two lines per booking
                     "grid-template-areas: \"who who acts\" \"ref amt date\" \"pick pick ret\""):        # a phone
            self.assertIn(rule, self.source)

    def test_the_title_row_has_a_dark_twin_and_goes_away_where_the_labels_take_over(self):
        self.assertIn("\n    .dark .qv-cols {", self.source)
        self.assertRegex(self.source, r"@media \(max-width: 1279px\) \{\s+\.qv-cols \{ display: none; \}")
        self.assertIn(".qv-line > [data-label]::before { content: attr(data-label);", self.source)

    def test_no_comment_spans_lines(self):
        """Django's {# #} comment is ONE line: a longer one prints its words on the page (a {% comment %} block is used instead)."""
        self.assertEqual([c for c in re.findall(r"\{#(.*?)#\}", self.source, re.S) if "\n" in c], [])

    def test_the_file_keeps_its_line_endings(self):
        self.assertEqual(TEMPLATE.read_bytes().count(b"\r\n"), 0)                    # LF, like the rest of the admin pages except four


class AccessTests(TestCase):
    def test_an_anonymous_visitor_is_sent_to_sign_in(self):
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_a_customer_is_turned_away(self):
        self.client.force_login(User.objects.create_user("db_plain", password="x"))
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_staff_get_the_page(self):
        self.client.force_login(User.objects.create_user("db_staff2", password="x", is_staff=True))
        self.assertEqual(self.client.get(URL).status_code, 200)
