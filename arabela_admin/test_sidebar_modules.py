"""The admin sidebar: "Payments & Deposits" (Payment Verification + Security Deposits) is its own module, and Financial Report holds
only Reservation Records. The sidebar is copied into every admin page, so this renders all of them and checks the same structure on each
-- a page that drifts from the others (or a link that went missing) fails here."""
import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import UserProfile

User = get_user_model()

PAGES = {
    "dashboard": reverse("arabela_admin:dashboard"),
    "new-booking": reverse("arabela_admin:new_booking"),
    "rental-schedule": reverse("arabela_admin:rental_schedule"),
    "calendar": reverse("arabela_admin:calendar"),
    "active-reservations": reverse("arabela_admin:active_reservations"),
    "pending-approval": reverse("arabela_admin:pending_approval"),
    "rental-history": reverse("arabela_admin:rental_history"),
    "gown-catalog": reverse("arabela_admin:gown_catalog"),
    "categories": reverse("arabela_admin:categories"),
    "clients": reverse("arabela_admin:clients"),
    "staff-management": reverse("arabela_admin:staff_management"),
    "payment-verification": reverse("arabela_admin:payment_verification"),
    "security-deposits": reverse("arabela_admin:security_deposits"),
    "reservation-records": reverse("arabela_admin:reservation_records"),
    "profile": reverse("arabela_admin:page", kwargs={"page": "profile"}),
    "account-settings": reverse("arabela_admin:page", kwargs={"page": "account-settings"}),
}
OWNER_ONLY = {"categories", "staff-management"}
MARK = re.compile(r"<!-- Menu Item (.+?) -->")
PV, SD, RR = (reverse("arabela_admin:payment_verification"), reverse("arabela_admin:security_deposits"),
              reverse("arabela_admin:reservation_records"))


def nav_of(html):
    match = re.search(r"<nav x-data=\"\{selected: \$persist\('Dashboard'\)\}\".*?</nav>", html, re.S)
    assert match, "the page has no sidebar"
    return match.group(0)


def items(nav):
    out, pos = [], 0
    while True:
        m = MARK.search(nav, pos)
        if not m:
            return out
        n = MARK.search(nav, m.end())
        if not n or n.group(1) != m.group(1):
            pos = m.end()
            continue
        out.append((m.group(1), nav[m.end():n.start()]))
        pos = n.end()


def hrefs(item):
    return re.findall(r'<a\s+href="([^"]+)"\s+class="menu-dropdown-item', item)


class SidebarModulesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("sb_owner", "sb.owner@gmail.com", "x")
        cls.staff = User.objects.create_user("sb_staff", password="x", is_staff=True)
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.STAFF})

    def sidebars(self, user):
        self.client.force_login(user)
        found = {}
        for name, url in PAGES.items():
            response = self.client.get(url)
            if user == self.staff and name in OWNER_ONLY:
                self.assertEqual(response.status_code, 302, name)         # as before: staff are sent away from these
                continue
            self.assertEqual(response.status_code, 200, name)
            found[name] = (response.content.decode(), items(nav_of(response.content.decode())))
        return found

    def check_structure(self, found, owner):
        self.assertGreaterEqual(len(found), 13)
        for name, (html, modules) in found.items():
            with self.subTest(page=name):
                names = [n for n, _ in modules]
                self.assertEqual(names.count("Payments & Deposits"), 1)
                self.assertEqual(names.count("Financial Report"), 1)
                self.assertEqual(names.index("Payments & Deposits") + 1, names.index("Financial Report"))      # right before it
                self.assertEqual(names.index("Financial Report") + 1, names.index("Profile"))                  # and Profile stays last
                if owner:
                    self.assertIn("Staff Management", names[:names.index("Payments & Deposits")])
                else:
                    self.assertNotIn("Staff Management", names)
                by_name = dict(modules)
                self.assertEqual(hrefs(by_name["Payments & Deposits"]), [PV, SD])
                self.assertEqual(hrefs(by_name["Financial Report"]), [RR])
                self.assertNotIn("Financial", by_name["Payments & Deposits"])
                self.assertIn("Payments & Deposits", by_name["Payments & Deposits"])
                self.assertEqual(by_name["Payments & Deposits"].count("PaymentsDeposits"), 6)       # its own open/close key
                self.assertNotIn("PaymentsDeposits", by_name["Financial Report"])
                # which module opens for which page
                rule = re.search(r'x-init="([^"]*)"', nav_of(html)).group(1)
                self.assertIn("else if (page === 'payment-verification' || page === 'security-deposits') { selected = 'PaymentsDeposits' }", rule)
                self.assertIn("else if (page === 'reservation-records') { selected = 'FinancialReport' }", rule)
                self.assertNotIn("page === 'payment-verification' || page === 'security-deposits' || page === 'reservation-records'", rule)

    def test_every_page_has_the_same_sidebar_structure_for_the_owner(self):
        self.check_structure(self.sidebars(self.owner), owner=True)

    def test_and_for_a_staff_member(self):
        self.check_structure(self.sidebars(self.staff), owner=False)

    def test_the_other_modules_are_untouched(self):
        modules = dict(self.sidebars(self.owner)["dashboard"][1])
        for name in ("Dashboard", "Bookings & Schedule", "Inventory Management", "Customer Management", "Staff Management", "Profile"):
            self.assertIn(name, modules)
        self.assertEqual(hrefs(modules["Bookings & Schedule"]).count(reverse("arabela_admin:new_booking")), 1)
        self.assertEqual(len(hrefs(modules["Bookings & Schedule"])), 4)

    def test_the_two_pages_name_the_new_module_in_their_year_toast(self):
        found = self.sidebars(self.owner)
        for name in ("payment-verification", "security-deposits"):
            html = found[name][0]
            self.assertIn("Year applied across Payments &amp; Deposits", html)
            self.assertNotIn("Year applied across Financial Report", html)
            self.assertIn("financialReportYear", html)                  # the shared year is still remembered under the same key
        for name, (html, _) in found.items():
            self.assertNotIn("Year applied across Financial Report", html)

    def test_the_moved_pages_still_open_and_show_their_link_as_the_current_one(self):
        found = self.sidebars(self.owner)
        pv = dict(found["payment-verification"][1])["Payments & Deposits"]
        self.assertIn(":class=\"page === 'payment-verification' ? 'menu-dropdown-item-active' : 'menu-dropdown-item-inactive'\"", pv)
        sd = dict(found["security-deposits"][1])["Payments & Deposits"]
        self.assertIn(":class=\"page === 'security-deposits' ? 'menu-dropdown-item-active' : 'menu-dropdown-item-inactive'\"", sd)
        rr = dict(found["reservation-records"][1])["Financial Report"]
        self.assertIn(":class=\"page === 'reservation-records' ? 'menu-dropdown-item-active' : 'menu-dropdown-item-inactive'\"", rr)
        self.assertIn("x-data=\"{ page: 'payment-verification'", found["payment-verification"][0])
        self.assertIn("x-data=\"{ page: 'security-deposits'", found["security-deposits"][0])

    def test_no_template_syntax_leaks_into_any_page(self):
        for name, (html, _) in self.sidebars(self.owner).items():
            with self.subTest(page=name):
                for leak in ("{%", "{{", "{#"):
                    self.assertNotIn(leak, html)
