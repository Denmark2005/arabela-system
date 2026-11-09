"""Every admin page has the SAME top row: no big page heading, only the small "Home > Page" breadcrumb on the right (a page's own name still
shows in the browser tab, the sidebar and that breadcrumb). Pending Approval and Active Reservations keep their "+ New Booking" button on the
left of that row. This pins the pattern so a heading cannot creep back into one page and make the pages look different again."""
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

User = get_user_model()
ADMIN = Path(settings.BASE_DIR) / "templates" / "arabela_admin"

# template file -> (url, name shown in the breadcrumb, does the row also hold "+ New Booking")
PAGES = {
    "account-settings.html": (reverse("arabela_admin:page", kwargs={"page": "account-settings"}), "Account Settings", False),
    "active-reservations.html": (reverse("arabela_admin:active_reservations"), "Active Reservations", True),
    "calendar.html": (reverse("arabela_admin:calendar"), "Calendar", False),
    "categories.html": (reverse("arabela_admin:categories"), "Categories", False),
    "clients.html": (reverse("arabela_admin:clients"), "Client List", False),
    "gown-catalog.html": (reverse("arabela_admin:gown_catalog"), "Gown Catalog", False),
    "new-booking.html": (reverse("arabela_admin:new_booking"), "New Booking", False),
    "payment-verification.html": (reverse("arabela_admin:payment_verification"), "Payment Verification", False),
    "pending-approval.html": (reverse("arabela_admin:pending_approval"), "Pending Approval", True),
    "profile.html": (reverse("arabela_admin:page", kwargs={"page": "profile"}), "Profile", False),
    "rental-history.html": (reverse("arabela_admin:rental_history"), "Rental History", False),
    "reservation-records.html": (reverse("arabela_admin:reservation_records"), "Reservation Records", False),
    "security-deposits.html": (reverse("arabela_admin:security_deposits"), "Security Deposits", False),
    "staff-management.html": (reverse("arabela_admin:staff_management"), "Staff Management", False),
}


class TemplateTests(SimpleTestCase):
    def test_no_page_template_has_a_big_heading_with_the_page_name(self):
        for name in PAGES:
            with self.subTest(template=name):
                text = (ADMIN / name).read_text(encoding="utf8")
                self.assertNotRegex(text, r'<h2\b[^>]*x-text="pageName"')
                self.assertEqual(text.count('x-text="pageName"'), 1)               # only the breadcrumb's own last crumb

    def test_every_page_has_the_breadcrumb_row_in_the_same_shape(self):
        for name, (_url, crumb, has_button) in PAGES.items():
            with self.subTest(template=name):
                text = (ADMIN / name).read_text(encoding="utf8")
                self.assertEqual(text.count("pageName: `%s`" % crumb), 1)
                row = re.search(r'<div class="mb-4 flex flex-wrap items-center (justify-end|justify-between) gap-3">', text)
                self.assertIsNotNone(row, "the top row is mb-4 flex ... gap-3")
                self.assertEqual(row.group(1), "justify-between" if has_button else "justify-end")      # alone, the breadcrumb sits on the RIGHT
                self.assertIn("{% url 'arabela_admin:dashboard' %}", text[row.start():row.start() + 800])
                self.assertEqual("+ New Booking</a>" in text, has_button)

    def test_the_old_row_with_a_heading_is_gone_everywhere(self):
        for path in sorted(ADMIN.glob("*.html")):
            with self.subTest(template=path.name):
                self.assertNotIn('<div class="mb-6 flex flex-wrap items-center justify-between gap-3">', path.read_text(encoding="utf8"))


class RenderedPagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("hd_owner", "hd_owner@example.com", "Pass-12345!")

    def test_every_page_opens_without_its_big_heading_and_with_its_breadcrumb(self):
        self.client.force_login(self.owner)
        for name, (url, crumb, has_button) in PAGES.items():
            with self.subTest(page=name):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertNotRegex(html, r'<h2\b[^>]*x-text="pageName"')
                self.assertIn("pageName: `%s`" % crumb, html)
                self.assertIn('x-text="pageName"', html)
                self.assertEqual("+ New Booking</a>" in html, has_button)
                self.assertRegex(html, r"<title>[^<]*Arabela[^<]*</title>")                 # the browser tab keeps its title

    def test_the_dashboard_is_untouched(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("arabela_admin:dashboard")).content.decode()
        self.assertIn("+ New Booking</a>", html)
        self.assertNotIn("pageName", html)
