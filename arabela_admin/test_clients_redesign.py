"""Client List, redesigned: ONE card (search + filter buttons that carry the three tiles' numbers + a key to the circles), the account's status as a
CIRCLE before the name (no Status column), "N/A" for a missing phone, no fake spinners, no card title.

Who is flagged / inactive, how a flag or a lock works, the View window and the role rules keep their own tests (tests.py, test_role_permissions,
test_modal_closers). What is checked here: what each row says, that every hook the page's scripts need is still there, and that the page is still
closed to anyone who is not staff."""
import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from reservations.models import Reservation

User = get_user_model()
URL = reverse("arabela_admin:clients")
ADMIN = Path(settings.BASE_DIR) / "templates" / "arabela_admin"


class ClientsBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("cl_owner", "cl_owner@example.com", "Pass-12345!")

    def setUp(self):
        self.client.force_login(self.owner)
        self.now = timezone.now()

    def customer(self, username, first="", last="", *, phone=None, flagged=False, active=True, locked=False, old=False, typed=None):
        user = User.objects.create_user(username, f"{username}@example.test", "x", first_name=first, last_name=last, is_active=active)
        defaults = {"is_flagged": flagged}
        if locked:
            defaults["cancel_lockout_until"] = self.now + timedelta(days=1)
        UserProfile.objects.update_or_create(user=user, defaults=defaults)
        if old:
            User.objects.filter(pk=user.pk).update(date_joined=self.now - timedelta(days=90))
        if phone is not None or typed:
            Reservation.objects.create(customer=user, customer_name=typed or f"{first} {last}".strip(), phone=phone or "", status=Reservation.Status.CONFIRMED)
        return user

    def page(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def row(self, html, username):
        """The table row of one customer (every row carries the account's e-mail, which is built from the username)."""
        start = html.index(f"{username}@example.test")
        start = html.rindex("<tr ", 0, start)
        return html[start:html.index("</tr>", start)]


class StatusCircleTests(ClientsBase):
    def test_each_status_is_a_circle_before_the_name_with_its_words_on_hover(self):
        self.customer("cl_active", "Anna", "Active")
        self.customer("cl_inactive", "Ivan", "Inactive", active=False)
        self.customer("cl_flagged", "Flora", "Flagged", flagged=True)
        _, html = self.page()
        for username, kind, words in (("cl_active", "active", "Active account"), ("cl_inactive", "inactive", "Inactive account"), ("cl_flagged", "flagged", "Flagged account")):
            with self.subTest(customer=username):
                row = self.row(html, username)
                self.assertIn(f'<span class="ar-dot cl-dot--{kind}" role="img"', row)
                self.assertIn(f'title="{words}"', row)
                self.assertIn(f'aria-label="{words}"', row)
                self.assertLess(row.index("cl-dot--"), row.index("<span class=\"block font-medium"))      # the circle comes BEFORE the name

    def test_a_flagged_account_that_is_also_inactive_is_flagged_like_the_rows_own_status(self):
        self.customer("cl_both", "Both", "States", flagged=True, active=False)
        _, html = self.page()
        row = self.row(html, "cl_both")
        self.assertIn("cl-dot--flagged", row)
        self.assertIn("status: 'Flagged'", row)                                        # the same answer the filter and the View window use

    def test_there_is_no_status_column_and_no_status_chip_any_more(self):
        self.customer("cl_cols", "Col", "Umns")
        _, html = self.page()
        self.assertEqual(html.count("<th "), 7)
        headers = [re.sub(r"\s+", " ", h).strip() for h in re.findall(r"<th[^>]*>\s*<p[^>]*>([^<]*)</p>", html)]
        self.assertEqual(headers, ["Customer", "Email", "Phone", "Registered", "Reservations", "Cancel Attempts", "Action"])
        self.assertNotRegex(html, r'x-text="status"')                                  # the old Active / Inactive / Flagged chip
        self.assertNotIn("bg-success-50 text-success-700 dark:bg-success-500/15 dark:text-success-500': status === 'Active'", html)

    def test_the_key_to_the_circles_is_always_visible(self):
        self.customer("cl_key", "Key", "Reader")
        _, html = self.page()
        key = html[html.index('aria-label="What the circles mean"'):]
        key = key[:key.index("</span>\n              </div>")]
        for kind, word in (("active", "Active"), ("inactive", "Inactive"), ("flagged", "Flagged")):
            self.assertIn(f'<i class="ar-dot cl-dot--{kind}"></i>{word}', key)

    def test_the_lock_note_and_the_booked_as_name_are_still_there(self):
        self.customer("cl_lock", "Lena", "Locked", locked=True, phone="0917 000 0001", typed="Lena Typed")
        _, html = self.page()
        row = self.row(html, "cl_lock")
        self.assertIn("Locked until", row)
        self.assertIn("Booked as", row)
        self.assertIn("Lena Typed", row)


class PhoneAndSpinnerTests(ClientsBase):
    def test_a_missing_phone_says_n_a_in_the_table_and_the_window(self):
        self.customer("cl_nophone", "No", "Phone")
        self.customer("cl_phone", "Has", "Phone", phone="0917 123 4567")
        _, html = self.page()
        no = self.row(html, "cl_nophone")
        self.assertIn('>N/A</p>', no)
        self.assertIn("phone: 'N/A'", no)
        self.assertNotIn("Not provided", html)
        yes = self.row(html, "cl_phone")
        self.assertIn("0917 123 4567", yes)
        self.assertNotIn(">N/A</p>", yes)
        self.assertIn("|| 'N/A'", html)                                                 # the window's address line

    def test_the_fake_spinners_the_tiles_and_the_card_title_are_gone(self):
        self.customer("cl_clean", "Clean", "Page")
        _, html = self.page()
        for gone in ("filtering", "filterTimeout", "searching", "searchTimeout", "Total Customers", "Registered Customer Accounts", 'x-model="customerFilter"',
                     'style="height: 300px;"'):
            self.assertNotIn(gone, html)
        self.assertNotRegex(html, r'<h2\s[^>]*x-text="pageName"')

    def test_the_rows_are_a_little_tighter(self):
        self.customer("cl_tight", "Tight", "Rows")
        _, html = self.page()
        self.assertIn(".cl-table td { padding-top: .7rem; padding-bottom: .7rem; }", html)
        self.assertIn(".cl-table td:nth-child(3), .cl-table td:nth-child(4) { white-space: nowrap; }", html)       # phone and registered date never split in two
        self.assertIn(".cl-table th, .cl-table td { padding-left: .875rem; padding-right: .875rem; }", html)      # the laptop rule that keeps View on screen


class FilterAndHookTests(ClientsBase):
    def test_the_filter_buttons_carry_the_numbers_the_tiles_had(self):
        self.customer("cl_n1", "New", "One")
        self.customer("cl_n2", "New", "Two", flagged=True)
        self.customer("cl_o1", "Old", "One", old=True)
        response, html = self.page()
        self.assertEqual((response.context["total_customers"], response.context["new_this_month"], response.context["flagged_count"]), (3, 2, 1))
        bar = html[html.index('aria-label="Show customers"'):html.index('<div class="ar-count cl-countrow">')]
        self.assertRegex(bar, r">All <span class=\"ar-n\">3</span>")
        self.assertRegex(bar, r"New this month <span class=\"ar-n\">2</span>")
        self.assertRegex(bar, r"Flagged <span class=\"ar-n\">1</span>")
        for value in ("All", "New", "Flagged"):
            self.assertIn(f"@click=\"customerFilter = '{value}'\"", bar)

    def test_a_flagged_filter_row_rule_is_unchanged(self):
        self.customer("cl_rule", "Rule", "Row")
        _, html = self.page()
        row = self.row(html, "cl_rule")
        self.assertIn("customerFilter === 'Flagged' && status === 'Flagged'", row)
        self.assertIn("customerFilter === 'New' && ", row)

    def test_the_card_has_the_pagers_hooks_and_the_search_the_links_use(self):
        self.customer("cl_hooks", "Hooks", "Here")
        _, html = self.page()
        self.assertEqual(html.count('class="cl-card"'), 1)
        self.assertIn('data-arpg="tbody" data-arpg-noun="customers" data-arpg-top="off" data-arpg-count="#cl-count"', html)
        self.assertRegex(html, r'<div class="cl-table custom-scrollbar overflow-y-auto" style="max-height: 760px;" data-arpg=')
        self.assertIn('placeholder="Search by account name, reservation name or email..."', html)
        self.assertIn("presetSearchParams.get('search')", html)                       # a notification's ?search=<email> still lands on the customer
        self.assertIn("presetSearchParams.get('filter')", html)
        self.assertIn("$watch('customerFilter', syncFilterParamsToUrl);", html)

    def test_what_the_search_matches_is_unchanged(self):
        self.customer("cl_search", "Sage", "Astro", phone="0917 000 0009", typed="Metro Astro")
        _, html = self.page()
        row = self.row(html, "cl_search")
        for word in ("sage astro", "cl_search@example.test", "metro astro"):
            self.assertIn(word, row)

    def test_the_view_button_still_carries_everything_the_window_needs(self):
        self.customer("cl_view", "View", "Me", phone="0917 000 0010")
        _, html = self.page()
        row = self.row(html, "cl_view")
        for key in ("userId:", "name:", "email:", "phone:", "registered:", "reservations:", "cancellations:", "cancelLockedNow:", "names: JSON.parse(", "status: status"):
            self.assertIn(key, row)
        self.assertIn("isViewCustomerModal = true", row)

    def test_the_view_windows_hooks_are_still_wired(self):
        self.customer("cl_win", "Win", "Dow")
        _, html = self.page()
        for hook in ("unlockCustomer(viewingCustomer.userId, viewingCustomer.name)", "pendingFlagAction = 'flag'", "pendingFlagAction = 'unflag'", "View Reservation History"):
            self.assertIn(hook, html)

    def test_an_empty_list_says_so_in_seven_columns(self):
        response, html = self.page()
        self.assertEqual(response.context["total_customers"], 0)
        self.assertIn("No registered customers yet.", html)
        self.assertIn('colspan="7"', html)


class AccessTests(TestCase):
    def test_an_anonymous_visitor_is_sent_to_sign_in(self):
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_a_customer_is_turned_away(self):
        self.client.force_login(User.objects.create_user("cl_cust", password="x"))
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_staff_get_the_page(self):
        self.client.force_login(User.objects.create_user("cl_staff", password="x", is_staff=True))
        self.assertEqual(self.client.get(URL).status_code, 200)


class SharedStylesTests(SimpleTestCase):
    def test_the_page_uses_the_shared_list_styles_and_its_own_box_rule(self):
        text = (ADMIN / "clients.html").read_text(encoding="utf8")
        self.assertEqual(text.count('{% include "arabela_admin/partials/ar_list_styles.html" %}'), 1)
        self.assertIn(".cl-card { margin-bottom: 1.25rem;", text)
        self.assertNotIn(".ar-card {", text)
        self.assertNotIn(".ar-line {", text)
