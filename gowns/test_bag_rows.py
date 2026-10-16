"""The selection ("bag") rows: one row per gown + size + dates, and never more of a gown than exists.

The report (Oct 2026): on a gown's page the customer tapped Book Now, pressed Back from "Your Selection" and tapped it again -- and every
tap stacked another identical row (Wedding Gown 5 six times, for a gown that exists once). Checkout, which gives every gown its own
unit, then refused the extra rows at the very last step with "no longer available".

The rules live in static/js/cart-lines.js, shared by the product page, the drawer, the selection page and checkout. The helper is tested
here by running it for real in Node (skipped where Node is not installed); the server's side -- one submitted item per gown, each on its
own unit, and a plain message when the same gown is asked for more often than it exists -- is tested through the real view."""
import json
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from gowns.models import Gown
from reservations.models import Reservation, ReservationItem

User = get_user_model()
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
CART_LINES_JS = Path(settings.BASE_DIR) / "static" / "js" / "cart-lines.js"

NODE_SCRIPT = r"""
const vm = require('vm'), fs = require('fs');
const ctx = {};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const L = ctx.arabelaCartLines;
const row = (o) => Object.assign({ id: 'wedding-gown-5', name: 'Wedding Gown 5', collection: 'wedding', size: 'M',
  rental: '10/22/2026 - 10/26/2026', qty: 1, maxQty: 1, price: 20000 }, o || {});
const copy = (x) => JSON.parse(JSON.stringify(x));
const out = {};

// the reported bag: Gown 3 (no dates yet) and the same Gown 5 six times
const reported = [row({ id: 'wedding-gown-3', name: 'Wedding Gown 3', size: '-', rental: '' })].concat([1, 2, 3, 4, 5, 6].map(() => row()));
let t = L.tidy(copy(reported));
out.reported = { rows: t.cart.length, changed: t.changed, gown5: t.cart.filter(r => r.id === 'wedding-gown-5').map(r => r.qty) };
const again = L.tidy(copy(t.cart));
out.reportedAgain = { rows: again.cart.length, changed: again.changed };

// several units: identical rows add up as a quantity, never past what exists
out.three = L.tidy([row({ maxQty: 3 }), row({ maxQty: 3 }), row({ maxQty: 3 })]).cart.map(r => r.qty);
out.fourOfThree = L.tidy([1, 2, 3, 4].map(() => row({ maxQty: 3 }))).cart.map(r => r.qty);
out.noCap = L.tidy([row({ maxQty: undefined }), row({ maxQty: undefined }), row({ maxQty: undefined })]).cart.map(r => r.qty);

// different dates are different rows; the size's spelling and an empty rental do not make rows different
t = L.tidy([row(), row({ rental: '11/10/2026 - 11/14/2026' })]);
out.otherDates = { rows: t.cart.length, changed: t.changed };
out.sameShape = { sizePrefix: L.sameLine(row({ size: 'Size: M' }), row({ size: 'M' })), emptyRental: L.sameLine(row({ rental: '-' }), row({ rental: '' })),
  otherSize: L.sameLine(row({ size: 'L' }), row()), otherCollection: L.sameLine(row({ collection: 'evening' }), row()) };

// quantities are brought into range; broken rows go
t = L.tidy([row({ qty: 5, maxQty: 2 })]);
out.clamped = { qty: t.cart[0].qty, changed: t.changed };
t = L.tidy([row({ qty: 0, maxQty: 2 }), null, 'junk']);
out.repaired = { rows: t.cart.length, qty: t.cart[0].qty, changed: t.changed };
out.notAnArray = L.tidy(null);

// more of a gown than exists, whatever the dates
const twoDates = [row(), row({ rental: '11/10/2026 - 11/14/2026' })];
out.overOneUnit = L.overbooked(twoDates);
out.overTwoUnits = L.overbooked([row({ maxQty: 2 }), row({ maxQty: 2, rental: '11/10/2026 - 11/14/2026' })]);
out.otherSizes = L.overbooked([row(), row({ size: 'L', maxQty: 1 })]);
out.checkoutShape = L.overbooked([row({ size: 'Size: M' }), row({ size: 'Size: M', rental: '11/10/2026 - 11/14/2026' })]);
out.unknownStock = L.overbooked([row({ maxQty: undefined }), row({ maxQty: undefined, rental: '11/10/2026 - 11/14/2026' })]);
out.held = L.heldOf(twoDates, row());
out.message = L.overbookedMessage(L.overbooked(twoDates));
out.noMessage = L.overbookedMessage([]);
console.log(JSON.stringify(out));
"""


class CartLinesHelperTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        node = shutil.which("node")
        cls.result = None
        if node:
            run = subprocess.run([node, "-e", NODE_SCRIPT, str(CART_LINES_JS)], capture_output=True, text=True, timeout=60)
            if run.returncode == 0:
                cls.result = json.loads(run.stdout.strip().splitlines()[-1])
            else:
                cls.node_error = run.stderr

    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("Node is not installed, so the shared cart script cannot be run here")
        self.assertIsNotNone(self.result, getattr(self, "node_error", ""))
        self.r = self.result

    def test_the_reported_bag_collapses_to_one_row_of_each_gown(self):
        self.assertEqual(self.r["reported"], {"rows": 2, "changed": True, "gown5": [1]})     # Gown 3 + ONE Gown 5, not six
        self.assertEqual(self.r["reportedAgain"], {"rows": 2, "changed": False})            # tidy is stable: no endless re-saving

    def test_identical_rows_add_up_as_a_quantity_never_past_what_exists(self):
        self.assertEqual(self.r["three"], [3])
        self.assertEqual(self.r["fourOfThree"], [3])
        self.assertEqual(self.r["noCap"], [3])                                               # unknown stock: never blocked

    def test_different_dates_are_different_rows_and_spelling_does_not_matter(self):
        self.assertEqual(self.r["otherDates"], {"rows": 2, "changed": False})
        self.assertEqual(self.r["sameShape"], {"sizePrefix": True, "emptyRental": True, "otherSize": False, "otherCollection": False})

    def test_quantities_are_brought_into_range_and_broken_rows_go(self):
        self.assertEqual(self.r["clamped"], {"qty": 2, "changed": True})
        self.assertEqual(self.r["repaired"], {"rows": 1, "qty": 1, "changed": True})
        self.assertEqual(self.r["notAnArray"], {"cart": [], "changed": False})

    def test_a_bag_cannot_hold_more_of_a_gown_than_exists_whatever_the_dates(self):
        self.assertEqual(self.r["overOneUnit"], [{"name": "Wedding Gown 5", "size": "M", "wanted": 2, "cap": 1}])
        self.assertEqual(self.r["overTwoUnits"], [])                                         # two exist: two rows are fine
        self.assertEqual(self.r["otherSizes"], [])                                           # a different size is a different gown
        self.assertEqual(len(self.r["checkoutShape"]), 1)                                    # 'Size: M' (the checkout's shape) counts too
        self.assertEqual(self.r["unknownStock"], [])
        self.assertEqual(self.r["held"], 2)

    def test_the_warning_is_one_plain_sentence(self):
        self.assertEqual(
            self.r["message"],
            "Wedding Gown 5 is in your selection 2 times, but only 1 exists in size M. Please keep 1 and remove the rest.")
        self.assertEqual(self.r["noMessage"], "")


class EveryPageUsesTheSharedRulesTests(TestCase):
    """The pages that build or hand off the bag all call the shared script (a rename or a lost include would quietly bring the
    duplicate rows back)."""

    def read(self, rel):
        return (Path(settings.BASE_DIR) / rel).read_text(encoding="utf-8")

    def test_the_helper_loads_before_the_flow_script_on_every_page(self):
        html = self.client.get("/").content.decode()
        self.assertIn("js/cart-lines.js", html)
        self.assertLess(html.index("js/cart-lines.js"), html.index("js/reservation-flow.js"))

    def test_book_now_never_stacks_an_identical_row_or_adds_more_than_exists(self):
        source = self.read("templates/products.html")
        for text in ("cartLines.sameLine(row, item)", "cartLines.heldOf(cart, item)", "cartLines.tidy(cart)", "Already in your selection"):
            self.assertIn(text, source)

    def test_the_selection_page_and_drawer_tidy_the_bag_and_refuse_too_many_of_one_gown(self):
        selection = self.read("templates/selection.html")
        for text in ("arabelaCartLines.tidy(cart)", "arabelaCartLines.overbooked(cart)", "maxQty: Number(item.maxQty)"):
            self.assertIn(text, selection)
        self.assertIn("arabelaCartLines.tidy(cart)", self.read("templates/base.html"))
        flow = self.read("static/js/reservation-flow.js")
        self.assertIn("arabelaCartLines.overbooked(storedBag)", flow)

    def test_checkout_books_one_gown_for_every_unit_of_quantity(self):
        source = self.read("templates/reservation.html")
        self.assertIn("var copies = Math.max(1, Math.min(parseInt(it.qty, 10) || 1, 20));", source)

    def test_the_cache_stamp_covers_the_new_script(self):
        self.assertIn('"cart-lines.js"', self.read("arabela_admin/context_processors.py"))


class SubmitOneItemPerGownTests(TestCase):
    """The server side of the same rule: every submitted item takes its OWN physical gown."""

    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user("bagrows_customer", password="x")
        cls.other = User.objects.create_user("bagrows_other", password="x")

    def setUp(self):
        self.client.force_login(self.customer)
        patcher = patch("gowns.views._save_proof_file", return_value="https://example.test/fake-proof.jpg")
        self.addCleanup(patcher.stop)
        patcher.start()

    def gown(self, number, name="Bag Rows Gown"):
        return Gown.objects.create(
            gown_id=f"BAGROW-{number:04d}", name=name, category=Gown.Category.GUEST_GOWN, color_name="Red", color_code="RD",
            size=Gown.Size.MEDIUM, rental_price=Decimal("4500"), status=Gown.Status.AVAILABLE)

    def submit(self, gown, copies, start="2027-03-10", end="2027-03-13"):
        item = {"gown_name": gown.name, "gown_slug": gown.slug, "size": "Medium", "rental_date": start, "return_date": end}
        return self.client.post(reverse("gowns:reservation_submit"), data={
            "items": json.dumps([dict(item) for _ in range(copies)]), "first_name": "Test", "last_name": "Buyer", "phone": "09171234567",
            "address": "1 St", "city": "City", "postal_code": "1000", "payment_method": "GCash",
            "proof_of_payment": SimpleUploadedFile("proof.jpg", _JPEG, content_type="image/jpeg")})

    def test_the_same_one_off_gown_twice_is_refused_in_plain_words_and_nothing_is_booked(self):
        gown = self.gown(1)
        response = self.submit(gown, 2)
        self.assertEqual(response.status_code, 400)
        error = response.json()["error"]
        self.assertIn("Bag Rows Gown is in your selection 2 times", error)
        self.assertIn("remove the extra ones", error)
        self.assertNotIn("no longer available", error)                 # it was never the date that was wrong
        self.assertEqual(Reservation.objects.count(), 0)
        self.assertEqual(ReservationItem.objects.count(), 0)

    def test_a_gown_that_exists_twice_can_be_booked_twice_each_on_its_own_unit(self):
        first, second = self.gown(2), self.gown(3)
        response = self.submit(first, 2)
        self.assertEqual(response.status_code, 200, response.content)
        reservation = Reservation.objects.get(reference_code=response.json()["reference_code"])
        units = sorted(reservation.items.values_list("gown_id", flat=True))
        self.assertEqual(units, sorted([first.pk, second.pk]))          # two items, two different physical gowns
        self.assertEqual(reservation.security_deposit, Decimal("4000"))  # and the deposit is for both

    def test_three_asked_for_when_two_exist_says_so(self):
        self.gown(4), self.gown(5)
        gown = Gown.objects.get(gown_id="BAGROW-0004")
        response = self.submit(gown, 3)
        self.assertEqual(response.status_code, 400)
        self.assertIn("is in your selection 3 times", response.json()["error"])
        self.assertEqual(Reservation.objects.count(), 0)

    def test_an_ordinary_unavailable_date_keeps_its_old_message(self):
        gown = self.gown(6)
        self.assertEqual(self.submit(gown, 1).status_code, 200)
        self.client.force_login(self.other)
        response = self.submit(gown, 1)                                  # someone else already holds those dates
        self.assertEqual(response.status_code, 400)
        self.assertIn("is no longer available for", response.json()["error"])
        self.assertNotIn("in your selection", response.json()["error"])
