"""A gown that comes in several sizes can be added to the bag with no size, like one with no dates: its row says "Select size", and
Proceed / checkout stop with a plain dialog until a size is chosen.

The report (Oct 2026): the + button on a Barong (Small, Medium, Large) jumped to the gown's page, unlike the Wedding Gown cards where it
opens the bag. The rules live in static/js/reservation-flow.js (arabelaNeedsSize, arabelaPromptForGaps, the checkout summary). They are
tested here by running that real script in Node with a stand-in page (skipped where Node is not installed); the pages that call them are
checked by reading their source, and the whole round trip was run in a real browser."""
import json
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.test import TestCase

FLOW_JS = Path(settings.BASE_DIR) / "static" / "js" / "reservation-flow.js"

NODE_SCRIPT = r"""
const vm = require('vm'), fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');

function load(elements, sessionValue) {
  const ctx = { console };
  ctx.window = ctx;
  ctx.location = { href: '' };
  ctx.document = {
    body: { dataset: {} }, readyState: 'complete', addEventListener() {},
    getElementById: (id) => (elements[id] === undefined ? null : elements[id]),
    createElement: () => ({ set textContent(v) { this._t = String(v); }, get innerHTML() { return this._t.replace(/&/g, '&amp;').replace(/</g, '&lt;'); } }),
  };
  ctx.addEventListener = () => {};
  ctx.sessionStorage = { getItem: () => (sessionValue === undefined ? null : sessionValue), setItem() {}, removeItem() {} };
  ctx.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return ctx;
}

const catalog = [
  { slug: 'barong-1', collection_key: 'barong', sizes: ['S', 'M', 'L'] },
  { slug: 'barong-2', collection_key: 'barong', sizes: ['S', 'M', 'L'] },
  { slug: 'wedding-gown-1', collection_key: 'wedding', sizes: ['M'] },
  { slug: 'no-sizes', collection_key: 'wedding' },
];
const withCatalog = () => load({ 'search-catalog-data': { textContent: JSON.stringify(catalog) } });
const need = (ctx, item) => ctx.arabelaNeedsSize(item);
const row = (o) => Object.assign({ id: 'barong-1', collection: 'barong', name: 'Barong 1', size: '-' }, o || {});

(async () => {
  const out = {};

  // ---- the rule ----
  let ctx = withCatalog();
  out.unsizedShapes = ['-', 'Size: TBD', 'TBD', '', 'Size: -', undefined, null, 'Select size'].map((s) => need(ctx, row({ size: s })));
  out.sizedShapes = ['M', 'Size: M', 'Medium', 'Size: Large', 'S', 'XL', 'Free Size'].map((s) => need(ctx, row({ size: s })));
  out.singleSizeGown = need(ctx, row({ id: 'wedding-gown-1', collection: 'wedding', size: '-' }));
  out.unknownGown = need(ctx, row({ id: 'not-in-catalog', size: '-' }));
  out.catalogRowWithoutSizes = need(ctx, row({ id: 'no-sizes', collection: 'wedding', size: '-' }));
  out.nullItem = [need(ctx, null), need(ctx, undefined)];
  out.noCollectionStillFound = need(ctx, row({ collection: undefined }));
  out.wrongCollectionStillFoundBySlug = need(ctx, row({ collection: 'wedding' }));
  out.numericId = need(ctx, row({ id: 7, size: '-' }));

  // a page with no catalog never holds a gown back, and a catalog that appears later is picked up (nothing was remembered)
  const late = {};
  ctx = load(late);
  out.noCatalog = need(ctx, row());
  late['search-catalog-data'] = { textContent: JSON.stringify(catalog) };
  out.catalogAppearsLater = need(ctx, row());
  ctx = load({ 'search-catalog-data': { textContent: '{not json' } });
  out.brokenCatalog = need(ctx, row());
  ctx = load({ 'search-catalog-data': { textContent: '{"a":1}' } });
  out.catalogNotAnArray = need(ctx, row());

  // the checkout page hides the search overlay and carries only the sizes (#gown-sizes-data): same answers from it
  const lean = catalog.map((r) => ({ slug: r.slug, collection_key: r.collection_key, sizes: r.sizes }));
  ctx = load({ 'gown-sizes-data': { textContent: JSON.stringify(lean) } });
  out.leanListOnly = [need(ctx, row()), need(ctx, row({ id: 'wedding-gown-1', collection: 'wedding' })), need(ctx, row({ size: 'M' }))];

  // ---- the dialog ----
  async function prompt(entries, answer, legacy) {
    const c = withCatalog();
    const calls = [];
    c.arabelaConfirm = (o) => { calls.push(o); return Promise.resolve(answer); };
    (legacy ? c.arabelaPromptForDates : c.arabelaPromptForGaps)(entries);
    await new Promise((r) => setTimeout(r, 0));
    return { dialog: calls[0] || null, count: calls.length, went: c.location.href };
  }
  const g = (name, needsSize, needsDates, url) => ({ name, url: url === undefined ? '/p/' + name.replace(/ /g, '-') + '/' : url, needsSize, needsDates });
  out.sizeOnlyOne = await prompt([g('Barong 1', true, false)], true);
  out.sizeOnlyMany = await prompt([g('Barong 1', true, false), g('Barong 2', true, false), g('Barong 3', true, false)], true);
  out.datesOnlyOne = await prompt([g('Wedding Gown 5', false, true)], true);
  out.datesOnlyMany = await prompt([g('Wedding Gown 5', false, true), g('Wedding Gown 3', false, true)], true);
  out.bothOne = await prompt([g('Barong 1', true, true)], true);
  out.mixedMany = await prompt([g('Barong 1', true, false), g('Wedding Gown 5', false, true)], true);
  out.notNow = await prompt([g('Barong 1', true, false)], false);
  out.noEntries = [await prompt([], true), await prompt(null, true)];
  out.noUrl = await prompt([g('Barong 1', true, false, '')], true);
  out.legacyDatesOnly = await prompt([{ name: 'Wedding Gown 5', url: '/legacy/' }], true, true);

  // ---- the checkout page's Order Summary ----
  const snapshot = { items: [
    { name: 'Barong 1', id: 'barong-1', collection: 'barong', size: 'Size: TBD', rental: '', image: '/a.jpg', unitPrice: 1000, qty: 1, lineTotal: 1000 },
    { name: 'Wedding Gown One', id: 'wedding-gown-1', collection: 'wedding', size: 'Size: M', rental: '10/25/2026 - 10/29/2026', image: '/b.jpg', unitPrice: 21000, qty: 1, lineTotal: 21000 },
    { name: 'Barong 2', id: 'barong-2', collection: 'barong', size: 'Size: S', rental: '', image: '/c.jpg', unitPrice: 1000, qty: 1, lineTotal: 1000 },
    { name: 'Barong 1', id: 'barong-1', collection: 'barong', size: 'Size: L', rental: '10/25/2026 - 10/29/2026', image: '/d.jpg', unitPrice: 1000, qty: 1, lineTotal: 1000 },
  ], subtotal: 24000, deposit: 8000, total: 32000, itemCount: 4 };
  const root = { innerHTML: '' };
  ctx = load({ 'reservation-items-root': root, 'search-catalog-data': { textContent: JSON.stringify(catalog) } }, JSON.stringify(snapshot));
  let gapsSeen = null;
  ctx.onReservationDateGaps = (gaps) => { gapsSeen = gaps; };
  ctx.initReservationOrderSummary();
  const rows = root.innerHTML.split('<a href=').slice(1);
  out.summary = {
    rows: rows.length,
    selectSizeRows: rows.map((r) => r.includes('Select size')),
    sizeLines: rows.map((r) => (r.match(/Size: [A-Za-z]+/) || [''])[0]),
    needsAttention: rows.map((r) => r.includes('res-item-row--needs-date')),
    labels: rows.map((r) => (r.match(/aria-label="([^"]*)"/) || ['', ''])[1]),
    gaps: gapsSeen,
  };
  console.log(JSON.stringify(out));
})();
"""


class NeedsSizeScriptTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        node = shutil.which("node")
        cls.result = None
        if node:
            run = subprocess.run([node, "-e", NODE_SCRIPT, str(FLOW_JS)], capture_output=True, text=True, timeout=60)
            if run.returncode == 0:
                cls.result = json.loads(run.stdout.strip().splitlines()[-1])
            else:
                cls.node_error = run.stderr

    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("Node is not installed, so the checkout script cannot be run here")
        self.assertIsNotNone(self.result, getattr(self, "node_error", ""))
        self.r = self.result

    # ---- the rule ----
    def test_a_gown_with_several_sizes_and_none_chosen_needs_a_size_whatever_shape_that_arrives_in(self):
        # '-', 'Size: TBD', 'TBD', '', 'Size: -', undefined, null (every "not chosen" spelling) and the dialog's own link text
        self.assertEqual(self.r["unsizedShapes"], [True] * 8)

    def test_a_chosen_size_never_needs_asking_in_any_spelling(self):
        self.assertEqual(self.r["sizedShapes"], [False] * 7)

    def test_a_gown_with_one_size_is_never_asked(self):
        self.assertFalse(self.r["singleSizeGown"])

    def test_a_gown_the_page_knows_nothing_about_is_never_held_back(self):
        # the server still refuses it at checkout, so staying quiet here can only let the server say it
        self.assertFalse(self.r["unknownGown"])
        self.assertFalse(self.r["catalogRowWithoutSizes"])
        self.assertEqual(self.r["nullItem"], [False, False])

    def test_the_gown_is_found_by_slug_even_when_the_collection_is_missing_or_off(self):
        self.assertTrue(self.r["noCollectionStillFound"])
        self.assertTrue(self.r["wrongCollectionStillFoundBySlug"])
        self.assertTrue(self.r["numericId"] is False)           # id 7 is not a gown in the catalog

    def test_a_page_without_the_catalog_never_holds_a_gown_back_and_a_late_catalog_is_still_used(self):
        self.assertFalse(self.r["noCatalog"])
        self.assertTrue(self.r["catalogAppearsLater"])           # "nothing found" was not remembered
        self.assertFalse(self.r["brokenCatalog"])
        self.assertFalse(self.r["catalogNotAnArray"])

    def test_the_checkout_pages_own_size_list_gives_the_same_answers(self):
        self.assertEqual(self.r["leanListOnly"], [True, False, False])

    # ---- the dialog ----
    def test_a_missing_size_has_its_own_dialog(self):
        one = self.r["sizeOnlyOne"]
        self.assertEqual(one["dialog"]["title"], "This gown needs a size")
        self.assertEqual(one["dialog"]["message"],
                         "Barong 1 has no size yet. Choose the size you want and we will check it on your dates.")
        self.assertEqual((one["dialog"]["confirmText"], one["dialog"]["cancelText"]), ("Select size", "Not now"))
        self.assertEqual(one["went"], "/p/Barong-1/")                 # "Select size" opens that gown's page

    def test_several_gowns_missing_a_size_are_named_and_the_first_one_opens(self):
        many = self.r["sizeOnlyMany"]
        self.assertEqual(many["dialog"]["title"], "These gowns need sizes")
        self.assertEqual(many["dialog"]["message"],
                         "Barong 1, Barong 2 and Barong 3 have no size yet. Choose the size you want and we will check it on your dates. "
                         "We will start with Barong 1.")
        self.assertEqual(many["went"], "/p/Barong-1/")

    def test_the_date_only_dialog_keeps_its_old_words_exactly(self):
        one, many = self.r["datesOnlyOne"], self.r["datesOnlyMany"]
        self.assertEqual(one["dialog"]["title"], "This gown needs a rental date")
        self.assertEqual(one["dialog"]["message"],
                         "Wedding Gown 5 has no rental date yet. Pick the date of your event and we will set the pick-up and return days for you.")
        self.assertEqual((one["dialog"]["confirmText"], one["dialog"]["cancelText"]), ("Set dates", "Not now"))
        self.assertEqual(many["dialog"]["title"], "These gowns need rental dates")
        self.assertEqual(many["dialog"]["message"],
                         "Wedding Gown 5 and Wedding Gown 3 have no rental dates yet. Pick the date of your event and we will set the pick-up "
                         "and return days for you. We will start with Wedding Gown 5.")

    def test_a_gown_missing_both_says_so_once(self):
        both = self.r["bothOne"]
        self.assertEqual(both["dialog"]["title"], "This gown needs a size and a rental date")
        self.assertIn("Barong 1 has no size or rental date yet.", both["dialog"]["message"])
        self.assertEqual(both["dialog"]["confirmText"], "Select size")
        self.assertEqual(both["went"], "/p/Barong-1/")

    def test_a_mix_of_gowns_missing_a_size_and_gowns_missing_dates(self):
        mixed = self.r["mixedMany"]
        self.assertEqual(mixed["dialog"]["title"], "These gowns need a size or a rental date")
        self.assertIn("Barong 1 and Wedding Gown 5 still need a size or a rental date.", mixed["dialog"]["message"])
        self.assertIn("We will start with Barong 1.", mixed["dialog"]["message"])
        self.assertEqual(mixed["dialog"]["confirmText"], "Select size")

    def test_not_now_goes_nowhere_and_an_empty_list_asks_nothing(self):
        self.assertEqual((self.r["notNow"]["count"], self.r["notNow"]["went"]), (1, ""))
        self.assertEqual([x["count"] for x in self.r["noEntries"]], [0, 0])
        self.assertEqual(self.r["noUrl"]["went"], "")                # a gown with no page to open: the dialog still shows, nothing breaks
        self.assertEqual(self.r["noUrl"]["count"], 1)

    def test_a_page_opened_before_this_update_still_works(self):
        legacy = self.r["legacyDatesOnly"]
        self.assertEqual(legacy["dialog"]["title"], "This gown needs a rental date")
        self.assertEqual(legacy["went"], "/legacy/")

    # ---- the checkout page ----
    def test_the_order_summary_marks_exactly_the_gowns_that_need_a_size(self):
        s = self.r["summary"]
        self.assertEqual(s["rows"], 4)
        self.assertEqual(s["selectSizeRows"], [True, False, False, False])                  # only the unsized Barong
        self.assertEqual(s["sizeLines"], ["", "Size: M", "Size: S", "Size: L"])
        self.assertEqual(s["needsAttention"], [True, False, True, False])                  # Barong 1 (size+dates) and Barong 2 (dates)
        self.assertEqual(s["labels"], ["Choose a size for Barong 1", "Change rental dates for Wedding Gown One",
                                       "Set a rental date for Barong 2", "Change rental dates for Barong 1"])

    def test_the_order_summary_tells_the_page_which_gowns_block_checkout_and_why(self):
        gaps = self.r["summary"]["gaps"]
        self.assertEqual([(g["name"], g["needsSize"], g["needsDates"]) for g in gaps],
                         [("Barong 1", True, True), ("Barong 2", False, True)])
        self.assertTrue(all(g["url"].endswith("?edit_reservation_item=%d" % i) for g, i in zip(gaps, (0, 2))))


class CheckoutPageKnowsTheSizesTests(TestCase):
    """The checkout page hides the search overlay, so the sizes a gown comes in reach it as their own small list. Without it the page
    could never tell an unsized Barong from a one-size gown (found in the browser check: the warning never showed)."""

    @classmethod
    def setUpTestData(cls):
        from decimal import Decimal
        from django.contrib.auth import get_user_model
        from gowns.models import Gown
        cls.customer = get_user_model().objects.create_user("sizes_checkout_customer", password="x")
        for number, (name, size) in enumerate((("Many Sizes Gown", Gown.Size.SMALL), ("Many Sizes Gown", Gown.Size.LARGE),
                                                ("One Size Gown", Gown.Size.MEDIUM)), start=1):
            Gown.objects.create(gown_id=f"CKSIZE-{number:04d}", name=name, category=Gown.Category.WEDDING_GOWN, color_name="White",
                                color_code="WH", size=size, rental_price=Decimal("5000"), status=Gown.Status.AVAILABLE)

    def test_the_list_is_on_the_checkout_page_with_each_gowns_sizes(self):
        import re
        from django.urls import reverse
        self.client.force_login(self.customer)
        html = self.client.get(reverse("gowns:reservation")).content.decode()
        found = re.search(r'<script id="gown-sizes-data" type="application/json">(.*?)</script>', html, re.S)
        self.assertIsNotNone(found)
        rows = {row["slug"]: row for row in json.loads(found.group(1))}
        many = next(row for row in rows.values() if row["sizes"] == ["S", "L"])
        self.assertEqual(sorted(many), ["collection_key", "sizes", "slug"])         # lean: nothing but what the rule reads
        self.assertEqual(many["collection_key"], "wedding")
        self.assertIn(["M"], [row["sizes"] for row in rows.values()])
        self.assertNotIn('id="search-catalog-data"', html)                            # which is exactly why the page needs its own list

    def test_the_list_matches_the_search_catalog_gown_for_gown(self):
        from gowns.context_processors import _build_search_catalog, featured_search_items
        from django.test import RequestFactory
        context = featured_search_items(RequestFactory().get("/"))
        catalog = _build_search_catalog()
        self.assertEqual(context["gown_sizes"], [{"slug": r["slug"], "collection_key": r["collection_key"], "sizes": r["sizes"]} for r in catalog])


class EveryPageUsesTheSizeRuleTests(TestCase):
    """The pages that show, add or hand off the bag all call the shared rule (a rename or a lost call would quietly bring back the
    redirect or let an unsized gown through)."""

    def read(self, rel):
        return (Path(settings.BASE_DIR) / rel).read_text(encoding="utf-8")

    def test_the_plus_button_adds_a_multi_size_gown_instead_of_opening_its_page(self):
        base = self.read("templates/base.html")
        add = base[base.index("window.addCollectionItem = function"):base.index("function closeCartDrawer")]
        self.assertIn("window.arabelaSizesOf", add)
        self.assertIn("sizes.length === 1", add)                     # a single size is still filled in
        self.assertNotIn("window.location.href", add)                # no redirect to the gown page any more
        self.assertNotIn("catalogSizesFor", base)                    # the one lookup lives in reservation-flow.js

    def test_the_bag_row_and_the_selection_page_ask_for_the_size(self):
        base, selection = self.read("templates/base.html"), self.read("templates/selection.html")
        for source in (base, selection):
            self.assertIn("window.arabelaNeedsSize(item)", source)
            self.assertIn("Select size", source)
        self.assertIn("arabelaPromptForGaps", selection)
        self.assertIn("needsSize: window.arabelaNeedsSize(item)", selection)

    def test_the_drawer_row_keeps_its_size_line_so_the_hand_off_reads_the_size_and_nothing_else(self):
        base = self.read("templates/base.html")
        row = base[base.index("const sizeLine = window.arabelaNeedsSize(item)"):base.index("const rentalLine")]
        self.assertEqual(row.count('text-on-secondary-container mt-1"'), 2)   # the "Select size" line AND the normal size line carry the class
        flow = self.read("static/js/reservation-flow.js")
        self.assertIn("row.querySelector('.text-on-secondary-container')", flow)

    def test_the_drawer_proceed_checks_sizes_as_well_as_dates(self):
        flow = self.read("static/js/reservation-flow.js")
        self.assertIn("window.arabelaNeedsSize(items[u])", flow)
        self.assertIn("window.arabelaPromptForGaps(gaps)", flow)

    def test_the_checkout_page_holds_confirm_back_for_a_missing_size_too(self):
        page = self.read("templates/reservation.html")
        self.assertIn("window.arabelaPromptForGaps(reservationDateGaps)", page)
        self.assertIn("'a size or a rental date'", page)

    def test_the_script_is_in_the_cache_stamp_so_customers_get_the_new_version(self):
        self.assertIn('"reservation-flow.js"', self.read("arabela_admin/context_processors.py"))
