"""static/js/proof-details.js (the typed reference and amount boxes): its pure helpers run under Node with a bare stand-in for the page, and every
answer is compared with the SERVER's rule (gowns/typed_payment.py), so the page and the server can never disagree about what is acceptable."""
import json
import shutil
import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from gowns import typed_payment as tp

JS = Path(settings.BASE_DIR) / "static" / "js" / "proof-details.js"

REFERENCES = ["5046933479702", "5046 933 479702", "  5046 9334 79702  ", "5046-933-479702", "Ref No. 5046 933 479702", "#5046933479702", "",
              "abc", "586757758", "1" * 12, "1" * 14, "1" * 25, "12 34", "５０４６９３３４７９７０２"]
AMOUNTS = ["2000", "2,000", "2,000.00", "₱2,000.00", "PHP 2000", "php2000.5", " 2 000 ", "1", "0.5", "999999", "", "   ", "abc", "-5", "0", "0.00",
           "2000.123", "2.000,00", "1e3", "two thousand", "₱", "1000001", "1999.99", "2500", "100"]

NODE_SCRIPT = r"""
const vm = require('vm'), fs = require('fs');
const ctx = { window: {} };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const d = ctx.arabelaProofDetails;
const data = JSON.parse(process.argv[3]);
const out = { api: Object.keys(d).sort() };
out.clean = data.references.map((t) => d.cleanReference(t));
out.format = data.references.map((t) => d.formatReference(t));
out.refProblem = data.references.map((t) => d.referenceProblem(t));
out.amount = data.amounts.map((t) => d.parseAmount(t));
out.amountProblem = data.amounts.map((t) => d.amountProblem(t, 2000));
out.pesos = [d.peso(2000), d.peso(2000.5), d.peso(5), d.peso(1234567)];
// attach(): typing and pasting regroup the digits
const listeners = {};
const input = { value: '', attrs: {}, getAttribute(n) { return this.attrs[n] || null; }, setAttribute(n, v) { this.attrs[n] = v; },
                addEventListener(type, fn) { listeners[type] = fn; }, setSelectionRange() {} };
d.attach(input); d.attach(input);                    // a second attach must not stack a second listener
out.attachedOnce = Object.keys(listeners);
const typed = (value) => { input.value = value; listeners.input(); return input.value; };
out.typing = [typed('5'), typed('50469'), typed('5046933'), typed('5046933479702'), typed('50469334797029999'), typed('5046 933 479702'), typed('abc'), typed('')];
d.attach(null);                                      // no input on the page: nothing to do, nothing thrown
console.log(JSON.stringify(out));
"""


class ProofDetailsScriptTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.result = None
        node = shutil.which("node")
        if not node:
            return
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "run.js"
            script.write_text(NODE_SCRIPT, encoding="utf-8")
            done = subprocess.run([node, str(script), str(JS), json.dumps({"references": REFERENCES, "amounts": AMOUNTS})],
                                  capture_output=True, text=True, encoding="utf-8", timeout=60)
        if done.returncode != 0:
            raise AssertionError(done.stderr or done.stdout)
        cls.result = json.loads(done.stdout)

    def setUp(self):
        if self.result is None:
            self.skipTest("Node is not installed")

    def test_the_helpers_the_pages_use_are_all_there(self):
        self.assertEqual(self.result["api"], ["REFERENCE_DIGITS", "amountProblem", "attach", "cleanReference", "formatReference", "parseAmount", "peso",
                                              "referenceProblem"])

    def test_the_page_cleans_a_reference_exactly_like_the_server(self):
        self.assertEqual(self.result["clean"], [tp.clean_reference(t) for t in REFERENCES])

    def test_the_page_says_the_same_about_a_reference_as_the_server(self):
        self.assertEqual(self.result["refProblem"], [tp.reference_problem(t) for t in REFERENCES])

    def test_the_page_groups_a_reference_the_way_gcash_prints_it(self):
        expected = []
        for t in REFERENCES:
            digits = tp.clean_reference(t)[:13]
            expected.append(" ".join(part for part in (digits[:4], digits[4:7], digits[7:]) if part))
        self.assertEqual(self.result["format"], expected)

    def test_the_page_understands_an_amount_exactly_like_the_server(self):
        expected = []
        for t in AMOUNTS:
            parsed = tp.parse_amount(t)
            expected.append(None if parsed is None else float(parsed))
        self.assertEqual(self.result["amount"], expected)

    def test_the_page_says_the_same_about_an_amount_as_the_server(self):
        self.assertEqual(self.result["amountProblem"], [tp.amount_problem(t, Decimal("2000")) for t in AMOUNTS])

    def test_pesos_are_written_plainly(self):
        self.assertEqual(self.result["pesos"], ["₱2,000", "₱2,000.50", "₱5", "₱1,234,567"])

    def test_typing_and_pasting_regroup_the_digits_and_keep_at_most_thirteen(self):
        self.assertEqual(self.result["typing"], ["5", "5046 9", "5046 933", "5046 933 479702", "5046 933 479702", "5046 933 479702", "", ""])

    def test_attaching_twice_adds_one_listener_only(self):
        self.assertEqual(self.result["attachedOnce"], ["input"])
