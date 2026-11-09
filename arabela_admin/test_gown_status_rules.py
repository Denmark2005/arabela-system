"""A gown can no longer be set to Reserved by hand, and the Check availability date box is readable in dark mode.

Reserved means "a booking is holding this gown": it is set when a booking is approved / picked up and cleared when the booking is cancelled,
rejected or returned. Staff used to be able to set it with the View-window button, the bulk toolbar and the Add Gown form with no booking behind it,
which left gowns stuck as "Reserved - set manually - no booking" and inflated every Reserved count. All three now refuse it with one message.
Available and Out-of-Stock still work from the same places, and a gown that is already (wrongly) Reserved can still be freed."""
import json
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from arabela_admin.views import RESERVED_IS_AUTOMATIC_MESSAGE
from gowns.models import Gown

User = get_user_model()
ADMIN = Path(settings.BASE_DIR) / "templates" / "arabela_admin"


def make_gown(n=1, **overrides):
    defaults = dict(gown_id=f"RSV-RULE-{n:03d}", name=f"Rule Gown {n}", category=Gown.Category.GUEST_GOWN, color_name="Red", color_code="RD",
                    size=Gown.Size.MEDIUM, rental_price=Decimal("3000.00"), status=Gown.Status.AVAILABLE)
    defaults.update(overrides)
    return Gown.objects.create(**defaults)


class ReservedCannotBeSetByHandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="rule_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def post_status(self, gown, status):
        return self.client.post(reverse("arabela_admin:gown_status_update", args=[gown.id]), data=json.dumps({"status": status}), content_type="application/json")

    def post_bulk(self, ids, status):
        return self.client.post(reverse("arabela_admin:gown_bulk_action"), data=json.dumps({"action": "status", "ids": ids, "status": status}), content_type="application/json")

    # ---- the View-window button
    def test_the_single_status_endpoint_refuses_reserved_and_leaves_the_gown_alone(self):
        gown = make_gown()
        response = self.post_status(gown, "Reserved")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], RESERVED_IS_AUTOMATIC_MESSAGE)
        gown.refresh_from_db()
        self.assertEqual(gown.status, Gown.Status.AVAILABLE)

    def test_the_message_tells_staff_where_to_go_instead(self):
        self.assertIn("New Booking", RESERVED_IS_AUTOMATIC_MESSAGE)
        self.assertIn("automatically", RESERVED_IS_AUTOMATIC_MESSAGE)

    def test_available_and_out_of_stock_still_work(self):
        gown = make_gown()
        self.assertEqual(self.post_status(gown, "Out-of-Stock").status_code, 200)
        gown.refresh_from_db()
        self.assertEqual(gown.status, Gown.Status.OUT_OF_STOCK)
        self.assertEqual(self.post_status(gown, "Available").status_code, 200)
        gown.refresh_from_db()
        self.assertEqual(gown.status, Gown.Status.AVAILABLE)

    def test_a_gown_that_is_already_reserved_by_hand_can_still_be_freed(self):
        stuck = make_gown(2, status=Gown.Status.RESERVED)
        self.assertEqual(self.post_status(stuck, "Available").status_code, 200)
        stuck.refresh_from_db()
        self.assertEqual(stuck.status, Gown.Status.AVAILABLE)

    def test_an_unknown_status_is_still_just_invalid(self):
        self.assertEqual(self.post_status(make_gown(), "Sparkly").json()["error"], "Invalid status")

    # ---- the bulk toolbar (the page only offers Available / Out-of-Stock, but the server must not trust the page)
    def test_the_bulk_endpoint_refuses_reserved_and_changes_nothing(self):
        a, b = make_gown(3), make_gown(4)
        response = self.post_bulk([a.id, b.id], "Reserved")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], RESERVED_IS_AUTOMATIC_MESSAGE)
        self.assertEqual(set(Gown.objects.filter(id__in=[a.id, b.id]).values_list("status", flat=True)), {Gown.Status.AVAILABLE})

    def test_the_bulk_endpoint_still_does_available_and_out_of_stock(self):
        a, b = make_gown(5), make_gown(6)
        self.assertEqual(self.post_bulk([a.id, b.id], "Out-of-Stock").status_code, 200)
        self.assertEqual(set(Gown.objects.filter(id__in=[a.id, b.id]).values_list("status", flat=True)), {Gown.Status.OUT_OF_STOCK})
        self.assertEqual(self.post_bulk([a.id, b.id], "Available").status_code, 200)
        self.assertEqual(set(Gown.objects.filter(id__in=[a.id, b.id]).values_list("status", flat=True)), {Gown.Status.AVAILABLE})

    # ---- the Add Gown form
    def create(self, **overrides):
        data = dict(name="Rule New Gown", category=Gown.Category.GUEST_GOWN, color_name="Blue", color_code="BU", size=Gown.Size.MEDIUM, rental_price="3500")
        data.update(overrides)
        return self.client.post(reverse("arabela_admin:gown_create"), data=data)

    def test_add_gown_refuses_reserved_and_creates_nothing(self):
        before = Gown.objects.count()
        response = self.create(status="Reserved")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], RESERVED_IS_AUTOMATIC_MESSAGE)
        self.assertEqual(Gown.objects.count(), before)

    def test_add_gown_still_takes_available_and_out_of_stock_or_nothing(self):
        self.assertIn(self.create(status="Out-of-Stock").status_code, (200, 201))
        self.assertEqual(Gown.objects.get(name="Rule New Gown").status, Gown.Status.OUT_OF_STOCK)
        Gown.objects.all().delete()
        self.assertIn(self.create().status_code, (200, 201))                   # no status sent -> Available, as before
        self.assertEqual(Gown.objects.get(name="Rule New Gown").status, Gown.Status.AVAILABLE)


class CatalogPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="rule_page_staff", password="x", is_staff=True)
        make_gown(10, status=Gown.Status.RESERVED)                              # one gown that is (wrongly) Reserved by hand: the page must still cope

    def page(self):
        self.client.force_login(self.staff)
        return self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()

    def test_the_view_window_offers_only_available_and_out_of_stock(self):
        html = self.page()
        self.assertNotIn("changeGownStatus('Reserved')", html)
        self.assertNotIn(">Mark Reserved<", html)
        self.assertIn("changeGownStatus('Available')", html)
        self.assertIn("changeGownStatus('Out-of-Stock')", html)
        self.assertIn("Reserved is set automatically when a booking is approved", html)

    def test_the_hand_set_reserved_gown_still_shows_its_old_explanation_and_the_way_out(self):
        html = self.page()
        # The explanation is behind the "?" next to "Booked by"; the box itself only says "Not held by any booking."
        self.assertIn("its status was set to Reserved by hand: use &quot;Mark Available&quot; above if it should be free.", html)
        self.assertIn("Not held by any booking.", html)

    def test_the_add_gown_status_list_has_no_reserved_but_the_status_filter_keeps_it(self):
        html = self.page().replace("\r\n", "\n")
        add_form = html[html.index('<select x-model="newGownStatus"'):]
        add_form = add_form[:add_form.index("</select>")]
        self.assertNotIn("Reserved", add_form)
        self.assertIn('<option value="Available">Available</option>', add_form)
        self.assertIn('<option value="Out-of-Stock">Out-of-Stock</option>', add_form)
        status_filter = html[html.index('aria-label="Filter by status"'):]
        status_filter = status_filter[:status_filter.index("</select>")]
        self.assertIn('<option value="Reserved">Reserved</option>', status_filter)        # real bookings still show as Reserved, so you can filter on them

    def test_the_rendered_page_carries_the_dark_date_rule(self):
        self.assertIn(".dark .ca-field { background: #0c111d; border-color: #344054; color: #f2f4f7; color-scheme: dark; }", self.page())

    def test_the_bulk_toolbar_never_offered_reserved(self):
        html = self.page()
        self.assertNotIn("bulkStatus('Reserved')", html)


class DateBoxIsReadableInDarkModeTests(SimpleTestCase):
    def text(self, relative):
        return (ADMIN / relative).read_text(encoding="utf8").replace("\r\n", "\n")

    def test_the_dark_rule_gives_the_box_light_text(self):
        css = self.text("partials/availability_check.html")
        line = next(l for l in css.split("\n") if l.strip().startswith(".dark .ca-field {"))
        self.assertIn("color: #f2f4f7", line)                 # the text AND the calendar icon follow this colour; before it inherited black from the View window
        self.assertIn("color-scheme: dark", line)
        self.assertIn("background: #0c111d", line)

    def test_the_view_window_box_keeps_typing_and_gets_the_shared_calendar_icon(self):
        """A click handler that opened the calendar on every click was tried and dropped: the popup takes the keyboard, so a date could no longer be typed.
        The shared handler (partials/date_picker.html) opens the calendar only from the icon at the right end of the box, so typing keeps working."""
        html = self.text("gown-catalog.html")
        box = html[html.index('x-ref="caInlineDate"'):]
        box = box[:box.index(">")]
        self.assertNotIn("onclick", box)
        self.assertNotIn("@click", box)
        self.assertEqual(html.count("showPicker"), 0)         # no per-box handler any more: the one shared handler does it
        picker = self.text("partials/date_picker.html")
        self.assertIn("box.getBoundingClientRect().right - event.clientX > ICON_ZONE", picker)      # only the icon end opens the calendar
        self.assertIn("var ICON_ZONE = 46;", picker)

