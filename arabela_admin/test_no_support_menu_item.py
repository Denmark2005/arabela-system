"""The "Support" item is gone from the account menu of every admin page (2026-11-17).

It linked to /admin-panel/settings.html, which has never existed (a "Page not found"). The account menu now has Edit profile and Sign out. Checked
on every admin page, for the owner and for staff, that the item and its address are not there, that the rest of the menu still works, and that
the old address still answers "not found" instead of anything odd."""
import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import UserProfile

User = get_user_model()

OWNER_ONLY = ("categories", "staff_management", "audit_log")             # staff are sent away from these (the owner's pages), so only she is checked there
ADMIN_PAGES = ("dashboard", "calendar", "gown_catalog", "active_reservations", "pending_approval", "rental_history", "reservation_records",
               "new_booking", "payment_verification", "security_deposits", "clients", "categories", "staff_management", "audit_log")


class TheSupportItemIsGoneTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user("sup_owner", "sup_owner@gmail.com", "x", is_staff=True, is_superuser=True, first_name="Ana", last_name="Cruz")
        UserProfile.objects.update_or_create(user=cls.owner, defaults={"role": UserProfile.Role.OWNER})
        cls.staff = User.objects.create_user("sup_staff", "sup_staff@gmail.com", "x", is_staff=True, first_name="Bo", last_name="Reyes")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.STAFF})

    def pages(self, user):
        yield "profile", reverse("arabela_admin:page", args=["profile"])
        for name in ADMIN_PAGES:
            if name in OWNER_ONLY and user is not self.owner:
                continue
            yield name, reverse(f"arabela_admin:{name}")

    def test_no_admin_page_has_a_support_item_or_links_to_the_missing_page(self):
        for user in (self.owner, self.staff):
            self.client.force_login(user)
            for name, url in self.pages(user):
                with self.subTest(user=user.username, page=name):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    html = response.content.decode()
                    self.assertNotIn("settings.html", html)
                    self.assertIsNone(re.search(r">\s*Support\s*<", html))
                    self.assertIsNone(re.search(r"\n\s*Support\s*\n\s*</a>", html))

    def test_the_account_menu_still_has_edit_profile_and_sign_out_everywhere(self):
        for user in (self.owner, self.staff):
            self.client.force_login(user)
            for name, url in self.pages(user):
                with self.subTest(user=user.username, page=name):
                    html = self.client.get(url).content.decode()
                    self.assertIn(reverse("arabela_admin:page", args=["profile"]), html)
                    self.assertIn("Edit profile", html)
                    self.assertIn(reverse("arabela_admin:admin_logout"), html)
                    self.assertIn("Sign out", html)

    def test_the_menu_list_is_still_well_formed(self):
        """Taking an item out must not leave a broken list: as many <li> as </li> and as many <ul> as </ul> in the account menu."""
        self.client.force_login(self.owner)
        for name, url in self.pages(self.owner):
            with self.subTest(page=name):
                html = self.client.get(url).content.decode()
                start = html.index("Edit profile")
                menu = html[html.rindex("<ul", 0, start):html.index("</ul>", start) + 5]
                self.assertEqual(menu.count("<li>"), menu.count("</li>"))
                self.assertEqual(menu.count("<li>"), 1)                               # only Edit profile is left in the list (Sign out is its own form)

    def test_staff_are_still_sent_away_from_the_owners_pages(self):
        self.client.force_login(self.staff)
        for name in OWNER_ONLY:
            self.assertEqual(self.client.get(reverse(f"arabela_admin:{name}")).status_code, 302, name)

    def test_the_old_address_is_still_just_not_found(self):
        for user in (self.owner, self.staff):
            self.client.force_login(user)
            self.assertEqual(self.client.get("/admin-panel/settings.html").status_code, 404)
        self.client.logout()
        self.assertIn(self.client.get("/admin-panel/settings.html").status_code, (302, 404))
