"""Active Reservations: one closed line per booking (name, booking number, cellphone, first pick-up, last return) that opens the
booking's own rows. The buttons inside are the old table's cells, moved unchanged -- these tests pin down what the closed line says,
that nothing a button needs went missing, and the page-level wiring (the search text, the open / closed state)."""
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from gowns.models import Gown
from reservations.models import Reservation, ReservationItem

User = get_user_model()
P, R, RET, CAN = (ReservationItem.Stage.PICKUP, ReservationItem.Stage.RESERVED, ReservationItem.Stage.RETURNED, ReservationItem.Stage.CANCELLED)


def make_gown(number, name="Dropdown Gown", size=Gown.Size.MEDIUM):
    return Gown.objects.create(
        gown_id=f"Wedding Gown-WH-{number:03d}", name=f"{name} {number}", category=Gown.Category.WEDDING_GOWN, color_name="White",
        color_code="WH", size=size, rental_price=Decimal("3000"), status=Gown.Status.AVAILABLE)


class ActiveDropdownBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("ad_staff", password="x", is_staff=True, first_name="Maria", last_name="Cruz")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.STAFF})
        cls.customer = User.objects.create_user("ad_customer", email="ad.customer@gmail.com", password="x")
        UserProfile.objects.update_or_create(user=cls.customer, defaults={"display_name": "Account Holder"})

    def setUp(self):
        self.today = timezone.localdate()
        self.client.force_login(self.staff)
        self.counter = 0

    def book(self, name, phone, items, *, customer=None, channel=Reservation.Channel.ONLINE):
        """items: [(offset of the pick-up in days, offset of the return, stage, extra fields)]."""
        reservation = Reservation.objects.create(
            customer=customer, customer_name=name, phone=phone, address="Antipolo", status=Reservation.Status.CONFIRMED,
            payment_method="Cash", channel=channel, rental_subtotal=Decimal("3000"), security_deposit=Decimal("2000"),
            total_amount=Decimal("5000"), reviewed_at=timezone.now())
        made = []
        for pickup, ret, stage, extra in items:
            self.counter += 1
            gown = make_gown(self.counter + 100 * (Reservation.objects.count()))
            made.append(ReservationItem.objects.create(
                reservation=reservation, gown=gown, gown_name=gown.name, gown_slug=gown.slug, size=gown.size, rental_price=gown.rental_price,
                rental_date=self.today + timedelta(days=pickup), event_date=self.today + timedelta(days=pickup + 2),
                return_date=self.today + timedelta(days=ret), overdue_date=self.today + timedelta(days=ret), stage=stage, **extra))
        return reservation, made

    def page(self):
        response = self.client.get(reverse("arabela_admin:active_reservations"))
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def day(self, offset):
        d = self.today + timedelta(days=offset)
        return f"{d:%b} {d.day}, {d.year}"


class ClosedLineTests(ActiveDropdownBase):
    def line_of(self, html, reservation):
        match = re.search(r'<div class="ar-booking" data-booking-id="%d".*?<div class="ar-panel"' % reservation.id, html, re.S)
        self.assertIsNotNone(match, "the booking has no line")
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(0)))

    def test_the_line_shows_the_name_the_number_the_cellphone_and_the_two_dates(self):
        reservation, _ = self.book("Lorna Reyes", "09181112222", [(2, 6, P, {})], channel=Reservation.Channel.WALK_IN)
        _, html = self.page()
        line = self.line_of(html, reservation)
        for piece in ("Lorna Reyes", reservation.reference_code, "09181112222", self.day(2), self.day(6)):
            self.assertIn(piece, line)
        self.assertNotIn("gowns", line)                                     # one gown: no count

    def test_a_many_gown_booking_is_one_line_with_the_first_pickup_and_the_last_return(self):
        reservation, _ = self.book("Maria Many", "09171234567", [
            (6, 10, P, {}),
            (-1, 3, R, {"picked_up_on": self.today - timedelta(days=1)}),
            (-9, -5, RET, {"picked_up_on": self.today - timedelta(days=9), "returned_on": self.today - timedelta(days=5)}),
            (8, 14, CAN, {}),                                                # cancelled: its later return must not count
        ])
        response, html = self.page()
        line = self.line_of(html, reservation)
        self.assertIn("3 gowns", line)                                       # the cancelled one is not a gown of the booking any more
        self.assertIn(self.day(-9), line)
        self.assertIn(self.day(10), line)
        self.assertNotIn(self.day(14), line)
        booking = next(r for r in response.context["reservations"] if r.id == reservation.id)
        self.assertEqual((booking.span_start, booking.span_end, booking.gown_count),
                         (self.today - timedelta(days=9), self.today + timedelta(days=10), 3))
        self.assertEqual(html.count('data-booking-id="%d"' % reservation.id), 1)         # ONE line for the four gown rows

    def test_a_booking_with_no_cellphone_shows_a_dash_and_no_call_link(self):
        reservation, _ = self.book("Nophone Customer", "", [(3, 7, P, {})], customer=self.customer)
        _, html = self.page()
        self.assertIn("&mdash;", html[html.index('data-booking-id="%d"' % reservation.id):][:2500])
        self.assertNotIn('class="ar-phone"', html[html.index('data-booking-id="%d"' % reservation.id):html.index('class="ar-panel"', html.index('data-booking-id="%d"' % reservation.id))])

    def test_the_cellphone_is_still_a_call_link_and_does_not_open_the_dropdown(self):
        self.book("Call Me", "09175550000", [(3, 7, P, {})])
        _, html = self.page()
        self.assertIn('<a href="tel:09175550000" class="ar-phone" title="Call the customer" @click.stop>09175550000</a>', html)

    def test_the_closed_list_has_its_own_header_and_the_search_says_cellphone(self):
        self.book("Header Check", "09170000000", [(3, 7, P, {})])
        _, html = self.page()
        header = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.search(r'<div class="ar-head">.*?</div>', html, re.S).group(0))).strip()
        self.assertEqual(header, "Customer Booking no. Cellphone Rental Date Return Date")
        self.assertIn("Search by customer, reference, cellphone or gown ID...", html)

    def test_the_empty_state_is_still_shown(self):
        _, html = self.page()
        self.assertIn("No active reservations yet. Approved bookings will appear here.", html)
        self.assertNotIn('class="ar-booking"', html)


class DropdownContentTests(ActiveDropdownBase):
    """What is inside a booking's panel is what the old table had: the same buttons, once per gown or once per booking."""

    def test_every_button_the_old_page_had_is_inside_the_booking_panels(self):
        walkin, (w1, w2) = self.book("Pedro Santos", "09175550000", [(15, 19, P, {}), (16, 20, P, {})], channel=Reservation.Channel.WALK_IN)
        online, (o1, o2, o3) = self.book("Account Holder", "09171234567", [
            (6, 10, P, {}), (-1, 3, R, {"picked_up_on": self.today - timedelta(days=1)}),
            (-9, -5, RET, {"picked_up_on": self.today - timedelta(days=9), "returned_on": self.today - timedelta(days=5)})], customer=self.customer)
        _, html = self.page()
        # per gown
        self.assertIn(f'data-kind="pickup" data-item-id="{w1.id}"', html)
        self.assertIn(f'data-kind="return" data-item-id="{w1.id}"', html)
        self.assertIn(f'data-item-id="{w1.id}">Mark Picked Up', html)
        self.assertIn(f'data-item-id="{o1.id}">Mark Picked Up', html)
        self.assertIn(f'data-item-id="{o2.id}">Mark Returned', html)
        self.assertIn(f'data-item-id="{o2.id}">Undo', html)
        self.assertNotIn(f'data-kind="pickup" data-item-id="{o2.id}"', html)             # already out
        self.assertNotIn(f'data-item-id="{o3.id}">Mark', html)                           # already back
        self.assertEqual(html.count('class="resv-remind-btn'), 2)                        # the account holder's two gowns still to come back
        self.assertEqual(html.count('onclick="sendReminder(this)"'), 2)
        self.assertNotIn(">Call</a>", html)                                              # no Call button anywhere: the walk-in has no Remind either, staff dial the number on the line
        self.assertEqual(html.count(">Cancel gown</button>"), 3)                         # the walk-in's two waiting gowns + the account holder's one
        # per booking
        self.assertEqual(html.count(">View Customer</button>"), 2)
        self.assertEqual(html.count(">Timeline</span>"), 2)
        self.assertEqual(html.count(f'data-url="{reverse("arabela_admin:reservation_cancel", args=[walkin.id])}"'), 1)    # the walk-in can be cancelled whole
        self.assertEqual(html.count(f'data-url="{reverse("arabela_admin:reservation_cancel", args=[online.id])}"'), 0)    # the other has a gown out
        self.assertIn(reverse("arabela_admin:reservation_cancel_gowns", args=[walkin.id]), html)
        self.assertEqual(html.count("payment-verification/?search="), 2)                  # View

    def test_the_cells_inside_a_panel_are_the_old_columns(self):
        reservation, _ = self.book("Columns Check", "09170000001", [(3, 7, P, {})])
        _, html = self.page()
        panel = html[html.index('id="ar-panel-%d"' % reservation.id):]
        panel = panel[:panel.index("</table>")]
        for column in ("Gown Name", "Rental Date", "Return Date", "Amount", "Status", "Action"):
            self.assertIn(f">{column}</p>", panel)
        self.assertIn("&#8369; 3000", panel)
        self.assertIn(">Confirmed</p>", panel)

    def test_the_status_remarks_and_the_moved_dates_are_still_shown(self):
        self.book("Moved Pickup", "09170000002", [
            (4, 8, P, {"original_rental_date": self.today + timedelta(days=6), "original_return_date": self.today + timedelta(days=8)})])
        _, html = self.page()
        self.assertIn("(originally ", html)

    def test_the_booked_as_name_and_channel_moved_into_the_opened_part(self):
        reservation, _ = self.book("Walk Name", "09170000003", [(3, 7, P, {})], channel=Reservation.Channel.WALK_IN)
        _, html = self.page()
        strip = html[html.index('id="ar-panel-%d"' % reservation.id):][:900]
        self.assertIn("Walk-in", strip)
        self.assertNotIn("09170000003", strip)                                           # the number is on the line only

    def test_timelines_are_still_one_per_booking(self):
        self.book("Timeline One", "09170000004", [(3, 7, P, {})])
        self.book("Timeline Two", "09170000005", [(3, 7, P, {})])
        _, html = self.page()
        self.assertEqual(html.count('<div class="arb-tl-panel">'), 2)
        self.assertEqual(html.count('colspan="6"'), 2)
        self.assertNotIn('colspan="7"', html)


class DropdownWiringTests(ActiveDropdownBase):
    def test_the_page_state_and_the_open_close_wiring(self):
        reservation, _ = self.book("Wiring Check", "09170000006", [(3, 7, P, {})])
        _, html = self.page()
        self.assertIn("openBookings: [], autoOpen: []", html)
        self.assertIn("isBookingOpen(id)", html)
        self.assertIn("toggleBooking(id)", html)
        self.assertIn("arSetupBookings($data, $watch);", html)
        self.assertIn("function arSetupBookings(state, watch)", html)
        self.assertIn(f'@click="toggleBooking({reservation.id})"', html)
        self.assertIn(f'aria-controls="ar-panel-{reservation.id}"', html)
        self.assertIn(f'id="ar-panel-{reservation.id}"', html)
        self.assertIn(f'x-show="isBookingOpen({reservation.id})" x-cloak', html)          # hidden, never removed: every button always exists

    def test_the_booking_search_text_has_the_name_number_gown_and_phone(self):
        reservation, (item,) = self.book("Search Words", "0917 555 0000", [(3, 7, P, {})])
        _, html = self.page()
        data_search = re.search(r'data-booking-id="%d" data-search="([^"]*)"' % reservation.id, html).group(1)
        for word in ("search words", reservation.reference_code.lower(), item.gown.gown_id.lower(), "09175550000"):
            self.assertIn(word, data_search)
        # the page's own filter (x-show) matches the same words, so the one-match auto-open and the filter can never disagree
        xshow = re.search(r'data-booking-id="%d" data-search="[^"]*" x-show="([^"]*)"' % reservation.id, html).group(1)
        for word in ("search words", reservation.reference_code.lower().replace("-", "\\u002D"), "09175550000"):
            self.assertIn(word, xshow)

    def test_the_old_per_gown_search_text_is_still_there(self):
        _, (item,) = self.book("Old Search", "09170000007", [(3, 7, P, {})])
        _, html = self.page()
        self.assertIn(item.gown.gown_id.lower().replace("-", "\\u002D"), html)

    def test_no_unrendered_template_syntax_leaks_into_the_page(self):
        self.book("Leak Check", "09170000008", [(3, 7, P, {}), (5, 9, P, {})])
        _, html = self.page()
        for leak in ("{%", "{{", "{#"):
            self.assertNotIn(leak, html)

    def test_other_pages_are_unaffected(self):
        self.book("Other Pages", "09170000009", [(3, 7, P, {})])
        for name in ("dashboard", "pending_approval", "rental_history", "payment_verification", "security_deposits", "reservation_records"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(f"arabela_admin:{name}")).status_code, 200)
