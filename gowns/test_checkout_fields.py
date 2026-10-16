"""Which value a checkout box starts with: the profile's saved default, or what the customer typed -- the rule in static/js/checkout-fields.js,
run for real in Node (skipped where Node is not installed), plus the Edit profile name boxes that must stay inside their window.

The report (Oct 2026): a customer saved a default name and address in their profile, rented a gown, and the checkout still showed what they had
typed in an earlier checkout, because the remembered values were put back before the default."""
import json
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import UserProfile

User = get_user_model()
RULE_JS = Path(settings.BASE_DIR) / "static" / "js" / "checkout-fields.js"

NODE_SCRIPT = r"""
const vm = require('vm'), fs = require('fs');
const ctx = {};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const choose = ctx.arabelaCheckoutFields.chooseValue;
const out = {};
// the customer's own case: they typed 'Rainer' in an earlier checkout (no default then), and later saved the default 'heheh'
out.newDefaultBeatsOldTyping = choose('heheh', { value: 'Rainer', base: '' });
// a default exists and they did not touch it
out.untouchedDefault = choose('heheh', { value: 'heheh', base: 'heheh' });
// no default at all: what they typed before comes back
out.noDefaultKeepsTyping = choose('', { value: 'Rainer', base: '' });
// they typed over a default in THIS checkout and the page reloaded: their typing is kept
out.typedOverDefaultIsKept = choose('heheh', { value: 'Rainer', base: 'heheh' });
// the default changed after they typed over the old one: the new default wins
out.changedDefaultBeatsTypedOverOne = choose('newname', { value: 'Rainer', base: 'heheh' });
// the default was removed after they typed (they had typed over it): nothing stale comes back as if it were theirs
out.removedDefault = choose('', { value: 'Rainer', base: 'heheh' });
// nothing remembered
out.nothingRemembered = [choose('heheh', null), choose('heheh', undefined), choose('', null), choose('', { value: '', base: '' })];
// the shape the first version of the draft saved (a bare string): trusted only when the profile has nothing for that box
out.oldShapeWithDefault = choose('heheh', 'Rainer');
out.oldShapeWithoutDefault = choose('', 'Rainer');
// spaces and odd values never break it
out.spacesIgnored = [choose('  heheh ', { value: 'Rainer', base: 'heheh' }), choose('heheh', { value: '   ', base: 'heheh' }), choose(null, { value: 'A', base: null }), choose(undefined, { value: 'A' })];
console.log(JSON.stringify(out));
"""


class CheckoutFieldRuleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.result = None
        node = shutil.which("node")
        if node:
            run = subprocess.run([node, "-e", NODE_SCRIPT, str(RULE_JS)], capture_output=True, text=True, timeout=60)
            if run.returncode == 0:
                cls.result = json.loads(run.stdout.strip().splitlines()[-1])
            else:
                cls.node_error = run.stderr

    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("Node is not installed, so the shared checkout rule cannot be run here")
        self.assertIsNotNone(self.result, getattr(self, "node_error", ""))
        self.r = self.result

    def test_a_default_saved_later_beats_what_was_typed_earlier(self):
        self.assertEqual(self.r["newDefaultBeatsOldTyping"], "heheh")                 # the reported bug
        self.assertEqual(self.r["changedDefaultBeatsTypedOverOne"], "newname")

    def test_an_untouched_default_is_shown(self):
        self.assertEqual(self.r["untouchedDefault"], "heheh")

    def test_with_no_default_what_was_typed_before_comes_back(self):
        self.assertEqual(self.r["noDefaultKeepsTyping"], "Rainer")

    def test_typing_over_a_default_survives_a_reload_of_the_same_checkout(self):
        self.assertEqual(self.r["typedOverDefaultIsKept"], "Rainer")

    def test_a_removed_default_does_not_bring_back_stale_typing(self):
        self.assertEqual(self.r["removedDefault"], "")

    def test_nothing_remembered_means_the_default_or_blank(self):
        self.assertEqual(self.r["nothingRemembered"], ["heheh", "heheh", "", ""])

    def test_the_first_versions_bare_string_is_only_used_when_the_profile_has_nothing(self):
        self.assertEqual(self.r["oldShapeWithDefault"], "heheh")
        self.assertEqual(self.r["oldShapeWithoutDefault"], "Rainer")

    def test_odd_values_never_break_it(self):
        self.assertEqual(self.r["spacesIgnored"], ["Rainer", "heheh", "A", "A"])


class EditProfileNameBoxesTests(TestCase):
    """The two name boxes of the Edit profile window had no width of their own (the address boxes below did), so on a phone's wider
    font -- or a phone in Desktop mode -- the last-name box ran past the window."""

    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user("profile_boxes_customer", password="x")
        UserProfile.objects.update_or_create(user=cls.customer, defaults={"display_name": "Bag Demo"})

    def setUp(self):
        self.client.force_login(self.customer)

    def test_both_name_boxes_fill_their_column_and_may_shrink(self):
        html = self.client.get(reverse("gowns:profile")).content.decode()
        for box in ("profile-first-name-input", "profile-last-name-input"):
            tag = html[html.rindex("<input", 0, html.index(f'id="{box}"')):html.index(f'id="{box}"') + 80]
            self.assertIn("w-full", tag, box)
            self.assertIn("min-w-0", tag, box)

    def test_the_name_is_still_split_into_the_two_boxes(self):
        html = self.client.get(reverse("gowns:profile")).content.decode()
        self.assertIn('placeholder="First name" value="Bag"', html)
        self.assertIn('placeholder="Last name" value="Demo"', html)
