"""The Rental Schedule (calendar) design: a plain dim backdrop behind the pop-ups (no blur), ONE way to close Booking Details (the X), a
week / day view that is not crowded, status bars that share one look (solid stripe on the left, the rest a see-through tint), and the
"All Notifications" window (shared header partial) with the same plain dim backdrop.

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
            self.assertNotIn("backdrop-blur", html[start:start + 600])
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


class StatusBarStyleTests(SimpleTestCase):
    """Pick-up / Reserved / Return / Overdue / Cooldown all share the look the grey Cooldown bar had: a solid stripe on the left edge and a
    soft see-through tint for the rest. Colours only: the classes the script puts on the bars (fc-bg-*) and the legend are untouched."""

    STATUSES = ("warning", "success", "primary", "danger", "blocked")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.css = TEMPLATE.read_text(encoding="utf8")

    def test_every_status_sets_its_own_colour_numbers(self):
        for status in self.STATUSES:
            with self.subTest(status=status):
                rule = re.search(r"\.event-fc-color\.fc-bg-%s \{([^}]*)\}" % status, self.css)
                self.assertIsNotNone(rule)
                for token in ("--ev:", "--ev-stripe:", "--ev-text:", "--ev-a:"):
                    self.assertIn(token, rule.group(1))
                self.assertRegex(self.css, r"\.dark \.event-fc-color\.fc-bg-%s \{[^}]*--ev-text:" % status)       # a readable name colour in dark mode too

    def test_the_bars_are_a_see_through_tint_with_a_stripe_not_a_solid_fill(self):
        self.assertIn("background-color: rgba(var(--ev), var(--ev-a))", self.css)
        self.assertIn(".event-fc-color.fc-event-start { box-shadow: inset 3px 0 0 var(--ev-stripe); }", self.css)
        for solid in ("background-color: #F59E0B; border-color: #F59E0B", "background-color: #16A34A; border-color: #16A34A",
                      "background-color: #2563EB; border-color: #2563EB", "background-color: #DC2626; border-color: #DC2626"):
            self.assertNotIn(solid, self.css)                                     # the old solid fills are gone
        self.assertNotRegex(self.css, r"fc-event-title \{ color: #FFFFFF")         # no white-on-solid names left

    def test_the_day_list_rows_wear_the_same_stripe_and_colours(self):
        self.assertIn("#dayEventsModal .event-fc-color.day-event-row { color: var(--ev-text); box-shadow: inset 3px 0 0 var(--ev-stripe); }", self.css)
        self.assertIn("#dayEventsModal button.day-event-row:hover { background-color: rgba(var(--ev), 0.3); }", self.css)

    def test_overdue_stays_the_loudest(self):
        overdue = re.search(r"\.event-fc-color\.fc-bg-danger \{([^}]*)\}", self.css).group(1)
        calm = re.search(r"\.event-fc-color\.fc-bg-primary \{([^}]*)\}", self.css).group(1)
        tint = lambda rule: float(re.search(r"--ev-a: ([0-9.]+)", rule).group(1))
        self.assertGreater(tint(overdue), tint(calm))
        self.assertIn(".event-fc-color.fc-bg-danger .fc-event-title { font-weight: 700; }", self.css)

    def test_the_legend_dots_stay_solid(self):
        html = TEMPLATE.read_text(encoding="utf8")
        legend = html[html.index('class="status-legend'):html.index("Blocked days are gowns held back")]
        for solid in ("background:#F59E0B", "background:#16A34A", "background:#2563EB", "background:#DC2626", "background:#64748B"):
            self.assertIn(solid, legend)


class NotificationsBackdropTests(TestCase):
    """The "All Notifications" window lives in a header partial shared by every admin page; it sits on the plain dim backdrop too."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("notif_owner", "notif_owner@example.com", "Pass-12345!")

    def test_the_partial_has_no_blur(self):
        partial = Path(settings.BASE_DIR) / "templates" / "arabela_admin" / "partials" / "notifications.html"
        text = partial.read_text(encoding="utf8")
        self.assertNotIn("backdrop-blur", text)
        self.assertIn(".admin-backdrop { background: rgba(16, 24, 40, 0.5); }", text)
        self.assertIn(".dark .admin-backdrop { background: rgba(0, 0, 0, 0.6); }", text)

    def test_every_admin_page_draws_the_window_on_the_plain_backdrop_and_still_closes_it(self):
        self.client.force_login(self.owner)
        for name in ("calendar", "dashboard", "active_reservations", "gown_catalog"):
            with self.subTest(page=name):
                html = self.client.get(reverse("arabela_admin:" + name)).content.decode()
                start = html.index("<!-- All Notifications Modal -->")
                window = html[start:html.index("<!-- All Notifications Modal End -->")]
                self.assertIn('class="modal-close-btn admin-backdrop fixed inset-0 h-full w-full"', window)
                self.assertNotIn("backdrop-blur", window)
                self.assertIn("@click.outside=\"isAllNotificationsModal = false\"", window)      # clicking outside still closes it
                self.assertIn('@click="isAllNotificationsModal = false"', window)                # the X still closes it
                self.assertNotRegex(window, r">\s*Close\s*<")                                      # ...and it is the only way: no second "Close" button
