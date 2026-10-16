"""The checkout form remembers what was typed, for this tab only -- and the profile's saved default wins over anything remembered.

Why it remembers: Chrome's "Desktop site" switch re-runs the Google sign-in, so the checkout page can reload under a customer who has already
started typing (the second Google answer lands them back on the checkout -- see accounts/login_next.py). A reload used to leave the form empty.
Each box's value is kept in sessionStorage as they type and put back when the page loads. (The receipt photo can't be kept: a browser never
lets a page fill a file input.)

Why the profile wins: the first version put the remembered values back BEFORE the profile default, so a customer who later saved a default
still saw what they had typed in an earlier checkout. Each remembered value now carries the default it was typed against, and the rule that
decides is in static/js/checkout-fields.js (tested for real in gowns/test_checkout_fields.py).

The behaviour was also checked in a real browser (type, replay Google's answer, read the boxes back); these guard the wiring."""
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class CheckoutRemembersTypingTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = (Path(settings.BASE_DIR) / "templates" / "reservation.html").read_text(encoding="utf-8")

    def test_every_field_a_customer_types_is_remembered(self):
        self.assertIn(
            "var DRAFT_FIELD_IDS = ['first_name', 'last_name', 'address', 'city', 'postal', 'phone', 'gcash_reference'];", self.source)
        for field_id in ("first_name", "last_name", "address", "city", "postal", "phone", "gcash_reference"):
            self.assertIn(f'id="{field_id}"', self.source)                        # and each of them really is a field on the page

    def test_it_is_kept_per_account_and_only_in_this_tab(self):
        self.assertIn("var DRAFT_KEY = 'arabela_checkout_draft_' + USER_STORAGE_KEY;", self.source)
        self.assertIn("sessionStorage.setItem(DRAFT_KEY", self.source)
        self.assertNotIn("localStorage.setItem(DRAFT_KEY", self.source)           # never outlives the tab

    def test_each_remembered_value_carries_the_default_it_was_typed_against(self):
        self.assertIn("draft[id] = { value: el.value, base: defaults[id] || '' };", self.source)

    def test_one_function_decides_every_boxes_starting_value_using_the_shared_rule(self):
        self.assertIn("function fillCheckoutFields() {", self.source)
        self.assertIn("rule.chooseValue(base, draft ? draft[id] : null)", self.source)
        self.assertIn("if (!el || el.value.trim()) return;", self.source)         # the browser's own autofill is never overwritten
        self.assertNotIn("prefillReservationFromProfile", self.source)            # no second, competing way of filling the boxes
        self.assertNotIn("restoreCheckoutDraft", self.source)
        fill = self.source.index("fillCheckoutFields();")
        self.assertLess(fill, self.source.index("watchCheckoutDraft();", fill))

    def test_the_profile_is_where_the_defaults_come_from(self):
        self.assertIn("function profileDefaults() {", self.source)
        for key in ("first_name:", "last_name:", "address:", "city:", "postal:", "phone:"):
            self.assertIn(key, self.source[self.source.index("function profileDefaults() {"):])

    def test_the_rule_script_loads_before_the_page_script(self):
        loader = "js/checkout-fields.js"
        self.assertIn(loader, self.source)
        self.assertLess(self.source.index(loader), self.source.index("var RESERVATION_FIELD_IDS"))

    def test_a_booking_that_went_through_leaves_no_draft_behind(self):
        clear = self.source.index("function clearReservationSelections() {")
        self.assertIn("sessionStorage.removeItem(DRAFT_KEY)", self.source[clear:clear + 200])
