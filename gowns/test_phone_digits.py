"""The mobile-number boxes (customer checkout, customer Profile, admin New Booking) accept digits only, at most 11.

static/js/phone-digits.js is run here under Node with a tiny stand-in for the page, so the real file is what is tested. The
page checks and the server's rules are not part of this: they did not change.
"""
import json
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

BASE = Path(settings.BASE_DIR)
PHONE_JS = BASE / "static" / "js" / "phone-digits.js"

NODE_SCRIPT = r"""
const vm = require('vm'), fs = require('fs');
const handlers = {};
const document = {
  activeElement: null,
  addEventListener(type, fn, capture) { (handlers[type] = handlers[type] || []).push({ fn, capture }); },
};
const ctx = { document };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);

function box(value, marked = true, caret = null) {
  const b = {
    value, selectionStart: caret === null ? value.length : caret, picked: null,
    hasAttribute(name) { return marked && name === 'data-phone-digits'; },
    setSelectionRange(a, z) { this.picked = [a, z]; },
  };
  return b;
}
const fire = (type, b) => (handlers[type] || []).forEach((h) => h.fn({ target: b }));
const typed = (value, caret) => { const b = box(value, true, caret); document.activeElement = b; fire('input', b); return b; };

const out = {};
out.listens = { input: (handlers.input || []).map((h) => h.capture), change: (handlers.change || []).map((h) => h.capture) };
out.plain = typed('09171234567').value;
out.letters = typed('09ab17x').value;
out.onlyLetters = typed('hello').value;
out.spacesAndDashes = typed('0917 123-4567').value;
out.twelfthDigit = typed('091712345678').value;
out.fifteenDigits = typed('091712345678901').value;
out.plusSixtyThree = typed('+63 917 123 4567').value;
out.sixtyThreeNoPlus = typed('639171234567').value;
out.tenDigitsFromNine = typed('9171234567').value;
out.empty = typed('').value;
out.notMarked = (() => { const b = box('abc123', false); fire('input', b); return b.value; })();
out.changeEventToo = (() => { const b = box('+639171234567'); fire('change', b); return b.value; })();
// the cursor stays where the customer was typing: a letter typed after "0917" (caret 5) leaves it at 4 digits in
const mid = typed('0917x1234567', 5); out.caretMiddle = mid.picked;
const end = typed('09171234567z', 12); out.caretEnd = end.picked;
const fixed = typed('+639171234567', 4); out.caretCountryCode = fixed.picked;
const untouched = typed('0917', 4); out.caretUntouched = untouched.picked;
console.log(JSON.stringify(out));
"""


class PhoneDigitsBehaviourTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.result = None
        cls.node_error = ""
        node = shutil.which("node")
        if node:
            run = subprocess.run([node, "-e", NODE_SCRIPT, str(PHONE_JS)], capture_output=True, text=True, timeout=60)
            if run.returncode == 0:
                cls.result = json.loads(run.stdout.strip().splitlines()[-1])
            else:
                cls.node_error = run.stderr

    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("Node is not installed, so the phone box rule cannot be run here")
        self.assertIsNotNone(self.result, self.node_error)
        self.r = self.result

    def test_it_listens_on_the_document_before_the_page_own_handlers(self):
        self.assertEqual(self.r["listens"], {"input": [True], "change": [True]})        # capture phase

    def test_letters_spaces_and_dashes_never_stay(self):
        self.assertEqual(self.r["letters"], "0917")
        self.assertEqual(self.r["onlyLetters"], "")
        self.assertEqual(self.r["spacesAndDashes"], "09171234567")

    def test_a_valid_number_is_left_alone(self):
        self.assertEqual(self.r["plain"], "09171234567")
        self.assertEqual(self.r["empty"], "")

    def test_a_twelfth_digit_cannot_be_typed(self):
        self.assertEqual(self.r["twelfthDigit"], "09171234567")
        self.assertEqual(self.r["fifteenDigits"], "09171234567")

    def test_a_number_with_the_country_code_becomes_the_local_form(self):
        self.assertEqual(self.r["plusSixtyThree"], "09171234567")
        self.assertEqual(self.r["sixtyThreeNoPlus"], "09171234567")
        self.assertEqual(self.r["changeEventToo"], "09171234567")                       # an autofill that only fires "change"

    def test_ten_digits_are_not_guessed_at(self):
        self.assertEqual(self.r["tenDigitsFromNine"], "9171234567")                    # the page's own check still says Invalid number

    def test_only_marked_boxes_are_touched(self):
        self.assertEqual(self.r["notMarked"], "abc123")

    def test_the_cursor_stays_where_the_customer_was_typing(self):
        self.assertEqual(self.r["caretMiddle"], [4, 4])
        self.assertEqual(self.r["caretEnd"], [11, 11])
        self.assertEqual(self.r["caretCountryCode"], [11, 11])
        self.assertIsNone(self.r["caretUntouched"])                                    # nothing to clean, so nothing moved


class PhoneBoxMarkupTests(SimpleTestCase):
    """Each of the three boxes is marked for the script, and each page loads it."""

    def read(self, rel):
        return (BASE / rel).read_text(encoding="utf-8")

    def test_the_script_file_exists(self):
        self.assertTrue(PHONE_JS.is_file())

    def test_the_customer_checkout_box(self):
        source = self.read("templates/reservation.html")
        self.assertIn('id="phone" placeholder=" " type="tel" inputmode="numeric" autocomplete="tel" data-phone-digits>', source)
        self.assertIn("{% static 'js/phone-digits.js' %}", source)

    def test_the_customer_profile_box(self):
        source = self.read("templates/profile.htmls")
        self.assertIn('id="phone-input" type="tel" inputmode="numeric"', source)
        self.assertIn("data-phone-digits", source)
        self.assertIn("{% static 'js/phone-digits.js' %}", source)
        self.assertLess(source.index("{% load static %}"), source.index("{% static 'js/phone-digits.js' %}"))

    def test_the_new_booking_box(self):
        source = self.read("templates/arabela_admin/new-booking.html")
        self.assertIn('id="nb-phone" type="tel" inputmode="numeric" maxlength="20" autocomplete="off" data-phone-digits x-model="phone"', source)
        self.assertIn("{% static 'js/phone-digits.js' %}", source)

    def test_the_checks_that_were_already_there_are_still_there(self):
        self.assertIn("phoneVal.length !== 11", self.read("templates/reservation.html"))
        self.assertIn("!/^\\d{11}$/.test(entry.phone)", self.read("templates/profile.htmls"))
