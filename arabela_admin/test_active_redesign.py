"""The redesigned Active Reservations page: ONE box for the search, the colour key and the list; status circles with a "?" key; icon buttons
for the booking's actions; one short line per gown with its dots (everything you can do to a gown appears when they are pressed); and the
grey customer icon removed from every admin page.

The rules behind the buttons (who may cancel, when a gown can be picked up or returned, the day counts) are untouched and keep their own tests
(test_active_dropdown, test_active_overdue_filter, test_cancel_booking, test_cancel_gowns, test_role_permissions). What is checked here: the
number/colour each gown gets, and that the markup keeps the promises the redesign made."""
import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from django.urls import reverse

from accounts.models import UserProfile
from arabela_admin.test_active_dropdown import ActiveDropdownBase, CAN, P, R, RET
from arabela_admin.views import _gown_circle
from reservations.models import Reservation, ReservationItem

User = get_user_model()
ADMIN = Path(settings.BASE_DIR) / "templates" / "arabela_admin"
PERSON = "M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0z"


class GownCircleTests(ActiveDropdownBase):
    """_gown_circle: the colour and the number a gown's circle carries (the same three colours as the booking's own status)."""

    def circle(self, pickup, ret, stage, **extra):
        _, [item] = self.book("Circle Check", "09170001111", [(pickup, ret, stage, extra)])
        return _gown_circle(item, self.today)

    def test_waiting_for_pickup_is_green_with_the_days_until_pickup(self):
        self.assertEqual(self.circle(3, 7, P)["kind"], "confirmed")
        self.assertEqual(self.circle(3, 7, P)["number"], 3)
        self.assertEqual(self.circle(0, 4, P)["number"], 0)                       # today
        self.assertIn("Pick-up in 3 days", self.circle(3, 7, P)["title"])

    def test_a_pickup_nobody_collected_is_red_with_the_days_late(self):
        circle = self.circle(-2, 2, P)
        self.assertEqual((circle["kind"], circle["number"]), ("overdue", 2))
        self.assertIn("2 days late for pick-up", circle["title"])

    def test_out_with_the_customer_is_blue_with_the_days_until_it_is_due_back(self):
        circle = self.circle(-1, 3, R, picked_up_on=self.today - timedelta(days=1))
        self.assertEqual((circle["kind"], circle["number"]), ("active", 3))
        self.assertEqual(self.circle(-4, 0, R, picked_up_on=self.today - timedelta(days=4))["number"], 0)      # due today

    def test_a_late_gown_is_red_with_the_days_late_counted_from_the_original_return_date(self):
        circle = self.circle(-9, -5, R, picked_up_on=self.today - timedelta(days=9))
        self.assertEqual((circle["kind"], circle["number"]), ("overdue", 5))
        self.assertIn("5 days late", circle["title"])
        _, [moved] = self.book("Moved Return", "09170002222", [(-9, -3, R, {"picked_up_on": self.today - timedelta(days=9), "original_return_date": self.today - timedelta(days=5)})])
        self.assertEqual(_gown_circle(moved, self.today)["number"], 5)            # the customer asked for longer: still counted from the date they booked

    def test_returned_and_cancelled_gowns_are_grey_with_no_number(self):
        back = self.circle(-9, -5, RET, picked_up_on=self.today - timedelta(days=9), returned_on=self.today - timedelta(days=5))
        self.assertEqual((back["kind"], back["number"]), ("returned", ""))
        gone = self.circle(8, 14, CAN)
        self.assertEqual((gone["kind"], gone["number"]), ("cancelled", ""))

    def test_the_hover_sentence_is_what_the_old_chips_said(self):
        circle = self.circle(-9, -5, R, picked_up_on=self.today - timedelta(days=9))
        self.assertIn("Out with customer", circle["title"])
        self.assertIn("Overdue", circle["title"])


class RedesignedPageTests(ActiveDropdownBase):
    def setUp(self):
        super().setUp()
        self.walkin, self.w_items = self.book("Walkin Redesign", "09170003333", [(15, 19, P, {}), (16, 20, P, {})], channel=Reservation.Channel.WALK_IN)
        self.many, self.m_items = self.book("Account Redesign", "09170004444", [
            (6, 10, P, {}), (-1, 3, R, {"picked_up_on": self.today - timedelta(days=1)}),
            (-9, -5, R, {"picked_up_on": self.today - timedelta(days=9)}),
            (-12, -8, RET, {"picked_up_on": self.today - timedelta(days=12), "returned_on": self.today - timedelta(days=8)})], customer=self.customer)
        _, self.html = self.page()
        self.card = self.html[self.html.index('class="ar-card"'):self.html.index('x-show="openCustomerModal"')]

    # ---- one box
    def test_the_search_the_filters_and_the_list_are_one_card_and_the_repeated_title_is_gone(self):
        self.assertEqual(self.html.count('class="ar-card"'), 1)
        toolbar = self.card[self.card.index('class="ar-toolbar"'):self.card.index('id="ar-count"')]
        self.assertIn('class="ar-search-input"', toolbar)
        self.assertIn('x-model="search"', toolbar)
        self.assertIn("Search by customer, reference, cellphone or gown ID...", toolbar)
        self.assertEqual(toolbar.count('<button type="button" class="ar-filter'), 4)                    # All / Confirmed / Active / Overdue
        self.assertNotIn("Confirmed &amp; Active Rentals", self.html)

    def test_paging_still_works_and_the_count_moves_into_the_box(self):
        self.assertIn('data-arpg=".ar-booking" data-arpg-noun="bookings" data-arpg-top="off" data-arpg-count="#ar-count"', self.card)
        self.assertIn('id="ar-count"', self.card)

    # ---- the colour key and the circles
    def test_the_filter_pills_are_the_colour_key_and_the_question_mark_explains_every_circle(self):
        for dot in ("ar-dot--confirmed", "ar-dot--active", "ar-dot--overdue"):
            self.assertIn(f'<span class="ar-dot {dot}"></span>', self.card)
        key = self.card[self.card.index('aria-label="What the circles mean"'):]
        key = key[:key.index("</details>")]
        for word in ("Green: waiting for pick-up", "Blue: out with the customer", "Red: late", "Grey: already returned, or cancelled", "How many gowns are in the booking"):
            self.assertIn(word, key)
        self.assertEqual(key.count("<li>"), 5)

    def test_every_booking_line_starts_with_its_status_circle_and_no_customer_icon(self):
        line = self.card[self.card.index('<div class="ar-who">'):self.card.index('<div class="ar-ref">')]
        self.assertRegex(line, r'<span class="ar-dot ar-dot--(confirmed|active|overdue)" role="img"')
        self.assertNotIn("<svg", line)
        self.assertNotIn("rounded-full", line)
        self.assertEqual(self.card.count('role="img" aria-label="Overdue"'), 1)       # the account holder's booking has a late gown
        self.assertEqual(self.card.count('role="img" aria-label="Confirmed"'), 1)

    def test_the_gown_circles_carry_the_right_colour_and_number(self):
        expect = {self.m_items[0].id: ("confirmed", "6"), self.m_items[1].id: ("active", "3"), self.m_items[2].id: ("overdue", "5")}
        for item_id, (kind, number) in expect.items():
            more = self.card.index(f'id="ar-more-{item_id}"')
            line = self.card[self.card.rindex('<div class="ar-gl">', 0, more):more]
            with self.subTest(kind=kind):
                self.assertRegex(line, r'class="ar-num ar-num--%s" role="img" title="[^"]+" aria-label="[^"]+">%s</span>' % (kind, number))
        back = self.card.index(f'id="ar-more-{self.m_items[3].id}"')
        self.assertIn('ar-num ar-num--returned', self.card[self.card.rindex('<div class="ar-gl">', 0, back):back])

    # ---- the opened booking: icons, then one short line per gown
    def test_the_booking_actions_are_three_icon_buttons_with_labels_and_no_words(self):
        acts = self.card[self.card.index('class="ar-acts"'):self.card.index('class="ar-gowns"')]
        tags = re.findall(r'<(?:a|button)\b[^>]*class="ar-ib[^"]*"[^>]*>.*?</(?:a|button)>', acts, flags=re.S)
        self.assertEqual(len(tags), 3)
        for tag in tags:
            with self.subTest(tag=tag[:90]):
                self.assertRegex(tag, r"title=")
                self.assertRegex(tag, r"aria-label=")
                self.assertEqual(re.sub(r"<svg.*?</svg>", "", re.sub(r"<[^>]+>", "", tag, count=0), flags=re.S).strip(), "")     # nothing but the drawing inside

    def test_every_small_gown_action_is_an_icon_with_a_tooltip_and_a_label(self):
        icons = re.findall(r'<button\b[^>]*class="(?:resv-[a-z-]+-btn )?ar-ib[^"]*"[^>]*>.*?</button>', self.card, flags=re.S)
        self.assertGreaterEqual(len(icons), 10)
        for tag in icons:
            with self.subTest(tag=tag[:90]):
                self.assertRegex(tag, r"title=")
                self.assertRegex(tag, r"aria-label=")
                self.assertEqual(re.sub(r"<[^>]+>", "", tag).strip(), "")

    def test_the_cancel_buttons_keep_their_words(self):
        self.assertIn(">Cancel booking</button>", self.card)                                       # the walk-in nobody picked up
        self.assertIn(">Cancel gown</button>", self.card)
        _, html = self.page()
        self.book("Partial Redesign", "09170005555", [(12, 16, P, {}), (13, 17, P, {}), (-1, 3, R, {"picked_up_on": self.today - timedelta(days=1)})])
        _, html = self.page()
        self.assertIn(">Cancel all not picked up (2)</button>", html)

    def test_a_gown_line_has_no_buttons_but_its_dots_and_the_actions_are_inside_them(self):
        for item in self.m_items:
            more = self.card.index(f'id="ar-more-{item.id}"')
            line = self.card[self.card.rindex('<div class="ar-gl">', 0, more):more]
            self.assertEqual(line.count("<button"), 1)
            self.assertIn('class="ar-ib ar-more"', line)
            self.assertIn(f'aria-controls="ar-more-{item.id}"', line)
        self.assertEqual(self.card.count('x-data="{ more: false }"'), 6)                          # 2 walk-in gowns + 4 account gowns
        for item in self.m_items[:3]:
            more = self.card[self.card.index(f'id="ar-more-{item.id}"'):]
            self.assertIn(f'data-item-actions="{item.id}"', more[:6000])
        back = self.card[self.card.index(f'id="ar-more-{self.m_items[3].id}"'):]
        self.assertNotIn(f'data-item-actions="{self.m_items[3].id}"', back[:3000])               # a returned gown has nothing to press

    def test_the_status_words_the_old_chips_said_are_written_out_when_a_gown_is_opened(self):
        more = self.card[self.card.index(f'id="ar-more-{self.m_items[2].id}"'):][:2500]
        self.assertIn("Out with customer", more)
        self.assertIn("Overdue · 5 days late", more)

    def test_the_changed_dates_show_as_a_small_icon_with_the_old_date_as_its_hover(self):
        self.client.logout()
        self.client.force_login(self.staff)
        _, [moved] = self.book("Moved Redesign", "09170006666", [(4, 8, P, {"original_rental_date": self.today + timedelta(days=6), "original_return_date": self.today + timedelta(days=8)})])
        _, html = self.page()
        self.assertIn('title="Dates changed &middot; pick-up was ', html)
        self.assertIn("Pick-up originally ", html)

    def test_the_timeline_is_a_toggle_icon_over_one_block_per_booking(self):
        self.assertEqual(self.card.count('aria-label="Timeline"'), 2)
        self.assertEqual(self.card.count('<div class="ar-tl"'), 2)
        self.assertIn(":aria-pressed=\"openTimeline ===", self.card)
        # the block no longer re-checks the search text (it hid the timeline when a booking was found by its phone number)
        self.assertNotIn("search.toLowerCase()))\" x-cloak", self.card[self.card.index('<div class="ar-tl"'):][:400])

    # ---- the Remind icon can no longer rely on text
    def test_the_remind_script_swaps_the_icon_and_never_touches_the_buttons_text(self):
        script = self.html[self.html.index("function sendReminder(btn)"):]
        script = script[:script.index("\n    }\n")]
        self.assertNotIn("btn.textContent", script)
        for needle in ("btn.innerHTML", 'class="ar-spin"', "btn.title = 'Sending...'", "btn.title = 'Sent'", "btn.title = originalTitle"):
            self.assertIn(needle, script)

    def test_every_hook_the_pages_script_looks_for_is_still_there(self):
        for hook in ("resv-remind-btn", "resv-mark-pickedup-btn", "resv-mark-returned-btn", "resv-change-date-btn", "resv-undo-pickup-btn", "cb-open-btn"):
            self.assertIn(hook, self.card)
        for attr in ('data-kind="pickup"', 'data-kind="return"', "data-current=", "data-original=", "data-rental=", "data-return=", "data-suggested=", 'onclick="sendReminder(this)"'):
            self.assertIn(attr, self.card)


class CustomerIconsAreGoneTests(SimpleTestCase):
    """The grey round person bubble beside a customer's name was removed from every admin page. The admin's own icon (header) and the staff
    accounts on Staff Management are not customers and keep theirs."""

    BUBBLE = re.compile(r'<div class="flex h-(?:10|14) w-(?:10|14) items-center justify-center overflow-hidden rounded-full bg-gray-100 text-gray-400[^"]*"[^>]*>\s*<svg[^>]*>\s*<path[^>]*d="' + re.escape(PERSON))

    def text(self, name):
        return (ADMIN / name).read_text(encoding="utf8")

    def test_no_customer_page_draws_the_person_bubble(self):
        for name in ("active-reservations.html", "pending-approval.html", "rental-history.html", "payment-verification.html", "clients.html", "dashboard.html"):
            with self.subTest(page=name):
                self.assertEqual(self.BUBBLE.findall(self.text(name)), [])

    def test_the_client_list_and_its_customer_window_and_quick_verify_have_no_person_drawing_left(self):
        self.assertNotIn(PERSON, self.text("clients.html").replace(self.header_avatar(self.text("clients.html")), ""))
        self.assertNotIn(PERSON, self.text("dashboard.html").replace(self.header_avatar(self.text("dashboard.html")), ""))

    def header_avatar(self, html):
        """The admin's OWN icon in the header (a fallback for an admin with no photo): the one person drawing every page keeps."""
        start = html.index("{% else %}", html.index("admin_avatar_url"))
        return html[start:html.index("{% endif %}", start)]

    def test_the_dashboard_quick_verify_row_keeps_its_view_button(self):
        row = self.text("dashboard.html")
        row = row[row.index("x-data=\"{ status: 'Pending' }\""):][:3500]
        self.assertIn("openReceiptModal = true", row)                       # the View button still opens the receipt

    def test_the_admins_own_icon_and_the_staff_accounts_keep_theirs(self):
        self.assertIn(PERSON, self.text("staff-management.html"))
        self.assertIn(PERSON, self.text("profile.html"))
        self.assertIn("admin_avatar_url", self.text("dashboard.html"))
