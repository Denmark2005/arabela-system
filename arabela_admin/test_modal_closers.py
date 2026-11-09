"""Every admin pop-up closes with ONE control -- the round X in its corner (or the dim area around it) -- and sits on a plain dim backdrop.

Before this, most View / Receipt / Customer windows also had a wording "Close" button at the bottom (the same job twice) and a 32px blur behind
them. Both are gone. What is checked here: nothing blurred is left in the admin, a wording "Close" button survives ONLY in a window that has no X
(so there is no other way to close it), and every window that lost its Close button still has its X wired to its own on/off flag. How the windows
open, fill and save is untouched and keeps its own tests."""
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

User = get_user_model()
ADMIN = Path(settings.BASE_DIR) / "templates" / "arabela_admin"
WORD_CLOSE = re.compile(r"<button\b[^>]*>\s*Close\s*</button>")

# Windows / panels that have a wording "Close" and NO X, so it is their only close button (not redundant): file -> how many.
ONLY_CLOSE = {
    "profile.html": 1,                       # GCash QR "View Full Image" window
    "gown-catalog.html": 4,                  # the Blocked Gowns / Tag Colors / Removal Log / Add Category panels (a header link, not a window)
    "partials/availability_check.html": 1,   # "Check a date" window
    "partials/attach_receipt.html": 1,       # "Attach the shop's receipt" window (its other button is Attach receipt)
}
# How the allowed ones look in the served page: the two own-window partials by id, the panel / QR links by their small grey style.
ALLOWED_MARKS = ('id="ca-close"', 'id="ar-close"', 'class="text-sm text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"')
# Windows that lost their Close button: file -> the flags their X is wired to.
LOST_CLOSE = {
    "dashboard.html": ["openReceiptModal"],
    "security-deposits.html": ["openReceiptModal"],
    "payment-verification.html": ["openReceiptModal"],
    "pending-approval.html": ["openCustomerModal"],
    "active-reservations.html": ["openCustomerModal"],
    "rental-history.html": ["openProofModal"],
    "clients.html": ["isViewCustomerModal"],
    "staff-management.html": ["isViewStaffModal"],
    "profile.html": ["isProfileInfoModal", "isProfileAddressModal"],
    "gown-catalog.html": ["isViewModal"],
    "partials/notifications.html": ["isAllNotificationsModal"],
}


def admin_templates():
    return sorted(ADMIN.rglob("*.html"))


def rel(path):
    return path.relative_to(ADMIN).as_posix()


class TemplateRulesTests(SimpleTestCase):
    def test_nothing_in_the_admin_is_blurred(self):
        for path in admin_templates():
            with self.subTest(template=rel(path)):
                text = path.read_text(encoding="utf8")
                self.assertNotIn("backdrop-blur", text)                    # the Tailwind class ...
                self.assertNotIn("backdrop-filter", text)                  # ... and the same thing written as plain CSS (Reservation Records, Categories, the confirm pop-up)
                self.assertNotRegex(text, r"(?<!-)filter:\s*blur")

    def test_a_wording_close_button_only_survives_in_a_window_that_has_no_x(self):
        found = {}
        for path in admin_templates():
            count = len(WORD_CLOSE.findall(path.read_text(encoding="utf8")))
            if count:
                found[rel(path)] = count
        self.assertEqual(found, ONLY_CLOSE)

    def test_every_window_that_lost_its_close_button_still_has_its_x(self):
        for name, flags in LOST_CLOSE.items():
            text = (ADMIN / name).read_text(encoding="utf8")
            for flag in flags:
                with self.subTest(template=name, flag=flag):
                    corner_x = re.search(r'@click="' + re.escape(flag) + r' = false"\s+class="transition-color absolute right-5 top-5', text)
                    self.assertIsNotNone(corner_x)
                    self.assertIn("M6.04289 16.5418", text[corner_x.start():corner_x.start() + 900])      # the cross drawn inside that button

    def test_the_categories_big_picture_window_closes_with_its_x_and_the_dim_area(self):
        text = (ADMIN / "categories.html").read_text(encoding="utf8")
        self.assertIn('<button type="button" class="cvr-view-x" data-view-close aria-label="Close">', text)
        self.assertIn('<div class="cvr-view-backdrop" data-view-close></div>', text)
        self.assertNotIn("cvr-view-foot", text)

    def test_the_plain_dim_class_is_defined_on_every_page_that_uses_it(self):
        """.admin-backdrop lives in the shared header partial, so a PAGE that uses it (directly, or through a window partial such as
        partials/payment_proof_window.html) must include that header partial."""
        for path in admin_templates():
            name = rel(path)
            if name.startswith("partials/"):
                continue
            text = path.read_text(encoding="utf8")
            if "admin-backdrop" in text or "partials/payment_proof_window.html" in text:
                with self.subTest(template=name):
                    self.assertIn("arabela_admin/partials/notifications.html", text)
        window = (ADMIN / "partials" / "payment_proof_window.html").read_text(encoding="utf8")
        self.assertIn("modal-close-btn admin-backdrop fixed inset-0 h-full w-full", window)       # the window's dim area is the plain one too
        self.assertNotIn("backdrop-blur", window)

    def test_every_dim_area_still_closes_its_window(self):
        """The dim area keeps the class the page script and the tests know it by (modal-close-btn)."""
        for path in admin_templates():
            text = path.read_text(encoding="utf8")
            for match in re.finditer(r'class="([^"]*\badmin-backdrop\b[^"]*)"', text):
                with self.subTest(template=rel(path)):
                    self.assertIn("modal-close-btn", match.group(1))
                    self.assertIn("fixed inset-0 h-full w-full", match.group(1))


class RenderedPagesTests(TestCase):
    """The same promises on the pages as they are really served."""

    PAGES = {
        "dashboard": (reverse("arabela_admin:dashboard"), 0),
        "pending_approval": (reverse("arabela_admin:pending_approval"), 1),
        "active_reservations": (reverse("arabela_admin:active_reservations"), 0),
        "rental_history": (reverse("arabela_admin:rental_history"), 0),
        "payment_verification": (reverse("arabela_admin:payment_verification"), 0),
        "security_deposits": (reverse("arabela_admin:security_deposits"), 0),
        "clients": (reverse("arabela_admin:clients"), 0),
        "staff_management": (reverse("arabela_admin:staff_management"), 0),
        "profile": (reverse("arabela_admin:page", kwargs={"page": "profile"}), 1),
        "gown_catalog": (reverse("arabela_admin:gown_catalog"), 4),
        "categories": (reverse("arabela_admin:categories"), 0),
        "calendar": (reverse("arabela_admin:calendar"), 0),
    }

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("closer_owner", "closer_owner@example.com", "Pass-12345!")

    def test_every_page_has_the_plain_dim_rule_no_blur_and_no_extra_close_words(self):
        self.client.force_login(self.owner)
        for name, (url, expected_close_words) in self.PAGES.items():
            with self.subTest(page=name):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertNotIn("backdrop-blur", html)
                self.assertEqual(html.count(".admin-backdrop { background: rgba(16, 24, 40, 0.5); }"), 1)
                redundant = [m for m in WORD_CLOSE.findall(html) if not any(mark in m for mark in ALLOWED_MARKS)]
                self.assertEqual(redundant, [])                      # a wording Close is only ever one of the no-X windows / panels below
                self.assertLessEqual(len(WORD_CLOSE.findall(html)), expected_close_words)        # (some of those panels only show when there is something to show)

    def test_every_dim_area_on_a_page_is_the_plain_one(self):
        self.client.force_login(self.owner)
        for name in ("dashboard", "gown_catalog", "staff_management", "profile"):
            with self.subTest(page=name):
                html = self.client.get(self.PAGES[name][0]).content.decode()
                self.assertNotIn("bg-gray-400/50", html)
                self.assertGreaterEqual(html.count('modal-close-btn admin-backdrop fixed inset-0 h-full w-full'), 2)

    def test_the_gown_window_footer_still_offers_remove_to_the_owner(self):
        self.client.force_login(self.owner)
        html = self.client.get(self.PAGES["gown_catalog"][0]).content.decode()
        footer = '<div class="mt-6 flex items-center border-t border-gray-100 px-2 pt-6 dark:border-gray-800">'
        at = html.index(footer)
        self.assertIn('@click="deleteGown()"', html[at:at + 1200])
