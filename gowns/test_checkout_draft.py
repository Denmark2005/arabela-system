"""The checkout form remembers what was typed, for this tab only.

Why: Chrome's "Desktop site" switch re-runs the Google sign-in, so the checkout page can reload under a customer who has already started
typing (the second Google answer lands them back on the checkout -- see accounts/login_next.py). A reload used to leave the form empty, so
the customer typed everything again. The form's values are now kept in sessionStorage as they type and put back when the page loads.
(The receipt photo can't be kept: a browser never lets a page fill a file input.)

The behaviour itself was checked in a real browser (type, replay Google's answer, read the fields back); these guard the wiring."""
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

    def test_what_they_typed_comes_back_before_the_profile_fills_the_blanks(self):
        restore = self.source.index("restoreCheckoutDraft();")
        prefill = self.source.index("prefillReservationFromProfile();", restore)
        self.assertLess(restore, prefill)
        self.assertIn("watchCheckoutDraft();", self.source)

    def test_a_field_that_already_has_something_is_never_overwritten(self):
        self.assertIn("if (el && !el.value.trim() && typeof draft[id] === 'string' && draft[id]) el.value = draft[id];", self.source)

    def test_a_booking_that_went_through_leaves_no_draft_behind(self):
        clear = self.source.index("function clearReservationSelections() {")
        self.assertIn("sessionStorage.removeItem(DRAFT_KEY)", self.source[clear:clear + 200])
