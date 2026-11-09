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

from accounts.models import UserProfile
from arabela_admin.tests import _make_gown
from gowns.models import Gown
from reservations.models import Reservation

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


class OverviewStripTests(DashboardBase):
    def test_the_three_figures_are_one_slim_strip_and_each_still_opens_its_own_page(self):
        _make_gown(1)
        response, html = self.page()
        strip = html[html.index('class="ov-strip ov-strip--top"'):html.index('class="ov-strip ov-strip--gowns"')]
        for label, url, number in (
            ("Registered Customers", reverse("arabela_admin:clients"), response.context["registered_customers"]),
            ("Active Rentals", reverse("arabela_admin:active_reservations"), response.context["active_rentals"]),
            ("Reservations This Month", response.context["reservations_this_month_url"], response.context["reservations_this_month"]),
        ):
            with self.subTest(label=label):
                self.assertIn(label, strip)
                self.assertIn('href="%s"' % url.replace("&", "&amp;"), strip)
                self.assertIn('<span class="ov-num">%s</span>' % number, strip)
        self.assertEqual(strip.count('class="ov-cell"'), 3)
        self.assertIn('<span class="ov-tag">%s</span>' % response.context["reservations_this_month_label"], strip)    # "November 2026" is a small tag now

    def test_the_old_tall_tiles_are_gone(self):
        _make_gown(1)
        _, html = self.page()
        for gone in ("metric-tile-wide", "admin-stat-grid-4", ">Picked up in", "h-12 w-12 items-center justify-center rounded-xl"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, html)

    def test_the_gown_figures_are_one_strip_with_the_catalogs_words_and_circles(self):
        gowns = [_make_gown(i, status=Gown.Status.AVAILABLE) for i in (1, 2)] + [_make_gown(3, status=Gown.Status.RESERVED)]
        response, html = self.page()
        self.assertEqual(html.count('class="ov-strip ov-strip--gowns"'), 1)
        tile = html[html.index("Gown Inventory"):]
        tile = tile[:tile.index("</a>")]                                            # the whole strip is ONE link, to the Gown Catalog
        for word in ("Total Gowns", "Available", "Reserved", "Blocked Gowns"):
            self.assertIn(word, tile)
        for dot in ("ov-dot--available", "ov-dot--reserved", "ov-dot--blocked"):
            self.assertIn(dot, tile)
        self.assertEqual([response.context[k] for k in ("gown_total", "gown_available", "gown_reserved", "gown_blocked")], [3, 2, 1, 0])
        self.assertEqual(len(re.findall(r'<span class="ov-num[^"]*">\d+</span>', tile)), 4)
        self.assertIn('href="%s"' % reverse("arabela_admin:gown_catalog"), html[html.index('class="ov-strip ov-strip--gowns"') - 150:html.index('class="ov-strip ov-strip--gowns"')])

    def test_a_zero_blocked_is_grey_and_says_none_blocked_today_on_hover(self):
        _make_gown(1)
        _, html = self.page()
        self.assertIn('title="None blocked today"', html)
        self.assertRegex(html, r'<span class="ov-num ov-num--zero">0</span>')

    def test_with_an_empty_catalog_the_alert_replaces_the_gown_strip(self):
        response, html = self.page()
        self.assertTrue(response.context["catalog_is_empty"])
        self.assertIn("Add Your First Gown", html)
        self.assertNotIn("ov-strip--gowns", html[html.index("<main>"):])
        self.assertIn('class="ov-strip ov-strip--top"', html)                      # the three figures are still there

    def test_the_overview_comes_before_quick_verify(self):
        _make_gown(1)
        _, html = self.page()
        self.assertLess(html.index('class="ov-strip ov-strip--top"'), html.index('class="ov-strip ov-strip--gowns"'))
        self.assertLess(html.index('class="ov-strip ov-strip--gowns"'), html.index('class="qv-card"'))

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

        self.pending("Q1")
        few = queries()
        for i in range(2, 14):
            self.pending("Q%d" % i, hours_ago=i)
        self.assertLessEqual(queries(), few)


class StaticTemplateTests(SimpleTestCase):
    def setUp(self):
        self.source = TEMPLATE.read_text(encoding="utf8")

    def test_the_shared_list_look_is_included_once_and_the_page_has_its_own_rules(self):
        self.assertEqual(self.source.count('{% include "arabela_admin/partials/ar_list_styles.html" %}'), 1)
        for rule in (".ov-strip {", ".qv-card {", ".qv-line {", ".ar-ib.qv-ok {", ".ar-ib.qv-no {"):
            self.assertEqual(self.source.count("\n    " + rule), 1, rule)
        self.assertNotIn("metric-tile-wide", self.source)

    def test_every_color_rule_has_a_dark_twin(self):
        for light in (".ov-strip {", ".ov-ic {", ".ov-num {", ".qv-card {", ".qv-line {", ".qv-title {", ".qv-pill {"):
            self.assertIn("\n    .dark " + light, self.source, light)

    def test_the_small_screens_have_their_own_layout(self):
        for rule in ("@media (max-width: 1180px)", "@media (max-width: 760px)", "grid-template-areas: \"who who acts\" \"ref amt date\""):
            self.assertIn(rule, self.source)

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
