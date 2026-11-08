"""The Rental Schedule (calendar) design: a plain dim backdrop behind the pop-ups (no blur), ONE way to close Booking Details (the X), and a
week / day view that is not crowded.

Only the look changed. What the calendar shows and the rules behind it (which bookings, which dates, view-only) keep their own tests in
tests.py, test_cancel_gowns.py and test_new_booking.py. What is checked here: the page markup and the calendar options keep the promises the
redesign made."""
import json
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

User = get_user_model()
TEMPLATE = Path(settings.BASE_DIR) / "templates" / "arabela_admin" / "calendar.html"
BUNDLE = Path(settings.BASE_DIR) / "static" / "arabela_admin" / "bundle.js"
END_MARK = "//# sourceURL=webpack://tailadmin-free/./src/js/components/calendar-init.js?"


def calendar_module():
    """The calendar-init module inside the committed webpack bundle (one eval("...") line)."""
    text = BUNDLE.read_text(encoding="utf8")
    end = text.index(END_MARK)
    return text[text.rindex('eval("', 0, end):end]


def calendar_source():
    """The same module as the JavaScript the browser runs: the string literal decoded, white space flattened."""
    literal = calendar_module()[len('eval("'):]
    return re.sub(r"\s+", " ", json.loads('"' + literal + '"'))


class CalendarPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("cal_owner", "cal_owner@example.com", "Pass-12345!")

    def page(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("arabela_admin:calendar"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_page_has_no_blurred_backdrop(self):
        html = self.page()
        self.assertNotIn("backdrop-blur", TEMPLATE.read_text(encoding="utf8"))   # the 32px blur behind both pop-ups is gone from the page
        for modal_id in ("bookingDetailModal", "dayEventsModal"):
            start = html.index(f'id="{modal_id}"')
            self.assertNotIn("backdrop-blur", html[start:start + 600])            # (the shared notifications pop-up keeps its own look)
        self.assertEqual(html.count("calendar-backdrop fixed inset-0"), 2)        # Booking Details + the day list both use the plain dim backdrop
        self.assertIn(".calendar-backdrop { background: rgba(16, 24, 40, 0.5); }", html)

    def test_booking_details_closes_with_the_x_only(self):
        html = self.page()
        start = html.index('id="bookingDetailModal"')
        end = html.index('id="dayEventsModal"', start)
        modal = html[start:end]
        self.assertNotRegex(modal, r">\s*Close\s*<")                              # the second, wording "Close" button is removed
        buttons = re.findall(r"<button\b[^>]*\bmodal-close-btn\b[^>]*>", modal)
        self.assertEqual(len(buttons), 1)                                         # the X
        self.assertIn('class="modal-close-btn calendar-backdrop fixed inset-0', modal)     # clicking outside still closes it (same as before)
        self.assertIn("Open in Active Reservations", modal)                       # the one real action stays

    def test_the_day_list_pop_up_keeps_its_own_close_hooks(self):
        html = self.page()
        self.assertIn('class="day-events-modal-close calendar-backdrop', html)
        self.assertIn('class="day-events-modal-close transition-color', html)     # its X

    def test_the_hidden_leftover_buttons_are_still_there_for_the_script(self):
        html = self.page()
        for hook in ("bookingModalSaveChangesBtn", "bookingModalMarkReturnedBtn", "bookingModalEditable", "bookingModalReadOnly"):
            self.assertIn(f'id="{hook}"', html)

    def test_week_and_day_have_their_own_roomy_styles(self):
        html = self.page()
        self.assertIn(".fc-dayGridWeek-view .fc-daygrid-day-frame", html)
        self.assertIn(".fc-dayGridDay-view .fc-daygrid-day-frame", html)
        self.assertIn(".fc-dayGridWeek-view .fc-event-title", html)               # names wrap instead of being cut off


class CalendarOptionsTests(SimpleTestCase):
    """The calendar's own options (inside the bundle). The week / day views are now day-grid views: every booking is listed, nothing hides
    behind a '+N more' link, and there are no empty hourly rows (every booking is an all-day bar, so the old hourly grid was empty)."""

    def test_the_toolbar_offers_month_week_day_with_day_grid_views(self):
        module = calendar_source()
        self.assertIn("dayGridMonth,dayGridWeek,dayGridDay", module)
        self.assertNotIn("timeGridWeek", module)
        self.assertNotIn("timeGridDay", module)

    def test_week_and_day_are_not_capped(self):
        module = calendar_source()
        self.assertIn("dayGridWeek: { dayMaxEvents: false }", module)
        self.assertIn("dayGridDay: { dayMaxEvents: false }", module)
        self.assertIn("dayMaxEvents: true", module)                               # the month view keeps its '+N more' (the day list pop-up reads it)

    def test_the_booking_data_and_click_logic_are_untouched(self):
        module = calendar_source()
        for logic in ("events: readBookings()", "eventClassNames", "displayEventTime: false", "eventClick"):
            self.assertIn(logic, module)

    def test_the_bundle_module_is_still_one_line(self):
        self.assertEqual(calendar_module().count("\n"), 0)                        # still ONE eval("...") line, as webpack wrote it
