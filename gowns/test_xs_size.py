"""The Extra Small (XS) size -- for KIDS GOWN ONLY, end to end: the model, the admin's Add/Edit Gown rules, the customer gown page, the bag/checkout script and the
booking itself.

The owner's request (Oct 2026): a Kids Gown can come in XS (3-4 years on the Kids chart) the day staff want to add one, and no other category has an XS. So the admin's
size list offers it only for Kids Gown and the server refuses it for everything else; the gown page shows an XS button only on Kids Gown pages (greyed until staff add
one, like every other missing size); and a booked XS gown records its real size like any other."""
import json
import re
import shutil
import subprocess
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from gowns.context_processors import _build_search_catalog
from gowns.models import EXTRA_SMALL_CATEGORY, SIZE_CODES, Gown, size_codes_of
from gowns.size_guides import size_guide_for
from gowns.views import (
    _UNIT_ASSIGNED, _UNIT_SIZE_NOT_OFFERED, _UNIT_SIZE_REQUIRED, _find_available_unit, _normalize_size, _size_choices,
)
from reservations.models import Reservation, ReservationItem

User = get_user_model()
FLOW_JS = Path(settings.BASE_DIR) / "static" / "js" / "reservation-flow.js"
_TINY_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
KIDS = Gown.Category.KIDS_GOWN


def make_unit(n, size, name="Little Gown", category=KIDS):
    return Gown.objects.create(
        gown_id=f"XS-{n:04d}", name=name, category=category, color_name="Pink", color_code="PK", size=size,
        rental_price=Decimal("1800"), status=Gown.Status.AVAILABLE)


class XsModelTests(TestCase):
    def test_the_size_exists_fits_the_column_and_comes_first(self):
        self.assertIn("Extra Small", Gown.Size.values)
        self.assertLessEqual(len("Extra Small"), Gown.size.field.max_length)
        self.assertEqual(SIZE_CODES[Gown.Size.EXTRA_SMALL], "XS")
        self.assertEqual(list(SIZE_CODES.values()), ["XS", "S", "M", "L", "XL", "XXL", "FREE"])

    def test_only_kids_gown_may_have_it(self):
        self.assertEqual(EXTRA_SMALL_CATEGORY, "Kids Gown")

    def test_xs_sorts_before_small_in_every_list_of_sizes(self):
        units = [make_unit(1, Gown.Size.SMALL), make_unit(2, Gown.Size.EXTRA_SMALL), make_unit(3, Gown.Size.EXTRA_LARGE)]
        self.assertEqual(size_codes_of(units), ["XS", "S", "XL"])

    def test_every_way_of_writing_it_means_xs_and_small_stays_small(self):
        for raw in ("XS", "xs", " Xs ", "Extra Small", "EXTRA SMALL"):
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_size(raw), Gown.Size.EXTRA_SMALL)
        for raw in ("S", "Small", "small"):
            self.assertEqual(_normalize_size(raw), Gown.Size.SMALL)
        for raw in ("XXS", "2XS", "X Small", ""):
            self.assertEqual(_normalize_size(raw), "")                                    # still never a guess


class XsAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="xs_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def create(self, **overrides):
        data = dict(name="Tiny Gown", category=KIDS, color_name="Pink", color_code="PK", size=Gown.Size.EXTRA_SMALL, rental_price="1800")
        data.update(overrides)
        return self.client.post(reverse("arabela_admin:gown_create"), data=data)

    def update(self, gown, **overrides):
        data = dict(name=gown.name, category=gown.category, color_name=gown.color_name, color_code=gown.color_code, size=gown.size,
                    rental_price=str(gown.rental_price))
        data.update(overrides)
        return self.client.post(reverse("arabela_admin:gown_update", kwargs={"gown_id": gown.id}), data=data)

    def test_the_size_list_has_the_xs_option_but_only_enabled_for_kids_gown(self):
        html = self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()
        option = re.search(r'<option value="Extra Small"[^>]*>.*?</option>', html).group(0)
        self.assertIn("Extra Small (XS)", option)
        self.assertIn(":hidden=\"newGownCategory !== 'Kids Gown'\"", option)
        self.assertIn(":disabled=\"newGownCategory !== 'Kids Gown'\"", option)
        order = re.findall(r'<option value="(Extra Small|Small|Medium|Large|Extra Large|Extra Extra Large|Free Size)"', html)
        self.assertEqual(order[:7], ["Extra Small", "Small", "Medium", "Large", "Extra Large", "Extra Extra Large", "Free Size"])

    def test_changing_the_category_away_from_kids_resets_a_picked_xs(self):
        html = self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()
        self.assertIn("$event.target.value !== 'Kids Gown' && newGownSize === 'Extra Small'", html)

    def test_staff_can_add_an_xs_kids_gown_and_it_shows_on_the_customer_page(self):
        response = self.create()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["success"])
        gown = Gown.objects.get(id=response.json()["gown"]["id"])
        self.assertEqual((gown.size, gown.category), ("Extra Small", "Kids Gown"))
        page = self.client.get(reverse("gowns:product_detail", kwargs={"collection": "kids-gown", "slug": gown.slug}))
        self.assertContains(page, 'data-product-size="XS"')

    def test_xs_is_refused_for_every_other_category(self):
        for category in (Gown.Category.WEDDING_GOWN, Gown.Category.SUIT, Gown.Category.EVENING_GOWN, Gown.Category.BARONG, Gown.Category.MOTHER_GOWN):
            with self.subTest(category=category):
                response = self.create(category=category, name=f"No XS in {category}")
                self.assertEqual(response.status_code, 400)
                self.assertIn("Extra Small is only for Kids Gown", response.json()["error"])
        self.assertFalse(Gown.objects.filter(size=Gown.Size.EXTRA_SMALL).exists())

    def test_xs_is_refused_for_a_category_the_owner_added_too(self):
        from gowns.models import CustomCategory
        CustomCategory.objects.create(name="Debut Gown", audience=CustomCategory.Audience.WOMEN)
        response = self.create(category="Debut Gown", name="Debut XS")
        self.assertEqual(response.status_code, 400)
        self.assertIn("only for Kids Gown", response.json()["error"])

    def test_staff_can_change_a_kids_gown_to_xs_but_not_a_wedding_gown(self):
        kid = make_unit(1, Gown.Size.SMALL, name="Edit Kid")
        response = self.update(kid, size=Gown.Size.EXTRA_SMALL)
        self.assertEqual(response.status_code, 200, response.content)
        kid.refresh_from_db()
        self.assertEqual(kid.size, "Extra Small")
        bride = make_unit(2, Gown.Size.MEDIUM, name="Edit Bride", category=Gown.Category.WEDDING_GOWN)
        refused = self.update(bride, size=Gown.Size.EXTRA_SMALL)
        self.assertEqual(refused.status_code, 400)
        bride.refresh_from_db()
        self.assertEqual(bride.size, "Medium")

    def test_an_xs_kids_gown_cannot_be_moved_into_another_category(self):
        kid = make_unit(3, Gown.Size.EXTRA_SMALL, name="Stay Kid")
        refused = self.update(kid, category=Gown.Category.WEDDING_GOWN)
        self.assertEqual(refused.status_code, 400)
        kid.refresh_from_db()
        self.assertEqual((kid.category, kid.size), ("Kids Gown", "Extra Small"))
        self.assertEqual(self.update(kid, notes="still fine").status_code, 200)             # editing anything else about it works

    def test_the_same_kids_design_in_xs_and_small_is_one_product_with_two_sizes(self):
        self.assertEqual(self.create(name="Twin Kid", size=Gown.Size.SMALL).status_code, 200)
        self.assertEqual(self.create(name="Twin Kid", size=Gown.Size.EXTRA_SMALL, name_choice="same").status_code, 200)
        units = list(Gown.objects.filter(name="Twin Kid"))
        self.assertEqual(sorted(g.size for g in units), ["Extra Small", "Small"])
        self.assertEqual(size_codes_of(units), ["XS", "S"])

    def test_a_made_up_size_is_still_refused(self):
        for bad in ("XXS", "Extra  Small", "Tiny", ""):
            with self.subTest(size=bad):
                response = self.create(size=bad, name=f"Bad {bad!r}")
                self.assertEqual(response.status_code, 400)
                self.assertIn("valid size", response.json()["error"])


class XsGownPageTests(TestCase):
    def page(self, unit, collection="kids-gown"):
        return self.client.get(reverse("gowns:product_detail", kwargs={"collection": collection, "slug": unit.slug}))

    def row(self, html):
        return re.findall(r'data-(?:product-size|size-not-offered)="([A-Z]+)"', html)

    def test_a_kids_gown_page_shows_a_greyed_xs_in_front_of_its_real_sizes(self):
        unit = make_unit(1, Gown.Size.LARGE)
        html = self.page(unit).content.decode()
        self.assertEqual(self.row(html), ["XS", "S", "M", "L", "XL"])                       # no greyed XXL on a kids page
        grey = re.search(r'<button[^>]*data-size-not-offered="XS"[^>]*>', html).group(0)
        self.assertIn('aria-disabled="true"', grey)
        self.assertIn("This gown is not available in Extra Small", grey)
        self.assertNotIn("data-product-size", grey)
        self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', html), ["L"])

    def test_the_day_staff_add_an_xs_unit_it_is_clickable_with_no_other_change(self):
        unit = make_unit(2, Gown.Size.LARGE)
        self.assertNotIn('data-product-size="XS"', self.page(unit).content.decode())
        make_unit(3, Gown.Size.EXTRA_SMALL)
        response = self.page(unit)
        html = response.content.decode()
        self.assertIn('data-product-size="XS"', html)
        self.assertNotIn('data-size-not-offered="XS"', html)
        self.assertEqual([o["code"] for o in response.context["size_options"]], ["XS", "L"])
        offered = re.search(r'<button[^>]*data-product-size="XS"[^>]*>', html).group(0)
        self.assertNotIn("aria-disabled", offered)
        self.assertIn('data-size-name="Extra Small"', offered)

    def test_an_xs_only_kids_gown_offers_just_xs(self):
        unit = make_unit(4, Gown.Size.EXTRA_SMALL, name="Wee Gown")
        html = self.page(unit).content.decode()
        self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', html), ["XS"])
        self.assertEqual(re.findall(r'data-size-not-offered="([A-Z]+)"', html), ["S", "M", "L", "XL"])

    def test_every_other_category_never_shows_an_xs_button(self):
        for n, category in enumerate((Gown.Category.WEDDING_GOWN, Gown.Category.SUIT, Gown.Category.EVENING_GOWN, Gown.Category.BARONG), start=10):
            with self.subTest(category=category):
                unit = make_unit(n, Gown.Size.MEDIUM, name=f"{category} Plain", category=category)
                collection = {"Wedding Gown": "wedding", "Suit": "suit", "Evening Gown": "evening-gown", "Barong": "barong"}[category]
                self.assertEqual(self.row(self.page(unit, collection).content.decode()), ["S", "M", "L", "XL", "XXL"])

    def test_a_kids_xxl_shows_only_when_staff_really_have_one(self):
        unit = make_unit(20, Gown.Size.MEDIUM, name="Big Kid")
        self.assertNotIn("XXL", self.row(self.page(unit).content.decode()))
        make_unit(21, Gown.Size.EXTRA_EXTRA_LARGE, name="Big Kid")
        html = self.page(unit).content.decode()
        self.assertEqual(self.row(html), ["XS", "S", "M", "L", "XL", "XXL"])
        self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', html), ["M", "XXL"])

    def test_a_kids_gown_in_free_size_only_still_shows_just_free_size(self):
        unit = make_unit(22, Gown.Size.FREE_SIZE, name="One Size Kid")
        self.assertEqual(self.row(self.page(unit).content.decode()), ["FREE"])

    def test_size_choices_gives_the_plain_ladder_when_no_category_is_given(self):
        options = [{"code": "M", "label": "M", "name": "Medium", "count": 1}]
        self.assertEqual([c["code"] for c in _size_choices(options)], ["S", "M", "L", "XL", "XXL"])
        self.assertEqual([c["code"] for c in _size_choices(options, "Kids Gown")], ["XS", "S", "M", "L", "XL"])

    def test_the_calendar_and_busy_dates_are_per_size_for_xs_too(self):
        unit = make_unit(5, Gown.Size.EXTRA_SMALL)
        make_unit(6, Gown.Size.MEDIUM)
        plans = self.page(unit).context["calendar_data"]["plans"]
        self.assertIn("XS", plans)
        self.assertIn("M", plans)

    def test_the_card_plus_button_data_carries_xs_first(self):
        make_unit(7, Gown.Size.SMALL, name="Catalog Kid")
        make_unit(8, Gown.Size.EXTRA_SMALL, name="Catalog Kid")
        row = next(item for item in _build_search_catalog() if item["title"] == "Catalog Kid")
        self.assertEqual(row["sizes"], ["XS", "S"])

    def test_an_xs_kids_gown_gets_its_size_guide_with_the_xs_row_matching_its_button(self):
        unit = make_unit(9, Gown.Size.EXTRA_SMALL, name="Guide Kid")
        response = self.page(unit)
        self.assertContains(response, 'id="size-guide-open"')
        self.assertContains(response, '<tr data-size-code="XS">')
        guide = size_guide_for("Kids Gown", response.context["size_choices"])
        self.assertEqual(guide["rows"][0]["code"], "XS")
        self.assertIn("XS", {c["code"] for c in response.context["size_choices"] if c["offered"]})       # the guide's XS row now follows a real button


class XsBookingTests(TestCase):
    def setUp(self):
        self.start = timezone.localdate() + timedelta(days=20)
        self.end = self.start + timedelta(days=4)

    def test_xs_is_picked_when_chosen_in_any_spelling_and_never_given_for_small(self):
        small, xs = make_unit(1, Gown.Size.SMALL), make_unit(2, Gown.Size.EXTRA_SMALL)
        for raw in ("XS", "xs", "Extra Small"):
            self.assertEqual(_find_available_unit(small.name, self.start, self.end, size=raw), (_UNIT_ASSIGNED, xs), raw)
        self.assertEqual(_find_available_unit(small.name, self.start, self.end, size="S"), (_UNIT_ASSIGNED, small))

    def test_xs_is_refused_when_the_gown_does_not_come_in_it(self):
        unit = make_unit(3, Gown.Size.MEDIUM)
        self.assertEqual(_find_available_unit(unit.name, self.start, self.end, size="XS"), (_UNIT_SIZE_NOT_OFFERED, None))

    def test_a_gown_with_xs_and_other_sizes_needs_one_chosen(self):
        unit = make_unit(4, Gown.Size.MEDIUM)
        make_unit(5, Gown.Size.EXTRA_SMALL)
        self.assertEqual(_find_available_unit(unit.name, self.start, self.end, size=""), (_UNIT_SIZE_REQUIRED, None))


class XsCheckoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="xs_customer", password="x")

    def setUp(self):
        self.client.force_login(self.customer)
        patcher = patch("gowns.views._save_proof_file", return_value="https://example.test/fake-proof.jpg")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.start = timezone.localdate() + timedelta(days=30)

    def submit(self, unit, size):
        items = [{"gown_name": unit.name, "gown_slug": unit.slug, "size": size,
                  "rental_date": self.start.isoformat(), "return_date": (self.start + timedelta(days=4)).isoformat()}]
        return self.client.post(reverse("gowns:reservation_submit"), data={
            "items": json.dumps(items), "first_name": "Tiny", "last_name": "Buyer", "phone": "09171234567",
            "address": "1 Test St", "city": "Test City", "postal_code": "1000", "payment_method": "GCash",
            "proof_of_payment": SimpleUploadedFile("proof.jpg", _TINY_JPEG, content_type="image/jpeg"),
        })

    def test_an_xs_kids_gown_books_end_to_end_and_records_the_real_size(self):
        make_unit(1, Gown.Size.MEDIUM)
        xs = make_unit(2, Gown.Size.EXTRA_SMALL)
        response = self.submit(xs, "XS")
        self.assertEqual(response.status_code, 200, response.content)
        item = ReservationItem.objects.get(reservation__reference_code=response.json()["reference_code"])
        self.assertEqual((item.gown_id, item.size), (xs.id, "Extra Small"))
        self.assertLessEqual(len(item.size), ReservationItem._meta.get_field("size").max_length)

    def test_the_xs_unit_is_taken_for_those_dates_and_the_next_customer_is_told(self):
        make_unit(3, Gown.Size.MEDIUM)
        xs = make_unit(4, Gown.Size.EXTRA_SMALL)
        self.assertEqual(self.submit(xs, "XS").status_code, 200)
        second = Client()
        second.force_login(User.objects.create_user(username="xs_second", password="x"))
        self.client = second
        again = self.submit(xs, "XS")
        self.assertNotEqual(again.status_code, 200)
        self.assertEqual(Reservation.objects.count(), 1)

    def test_asking_for_xs_on_a_gown_that_has_none_is_refused_clearly(self):
        unit = make_unit(5, Gown.Size.MEDIUM)
        response = self.submit(unit, "XS")
        self.assertEqual(response.status_code, 400)
        self.assertIn("doesn't come in", response.json()["error"])
        self.assertFalse(Reservation.objects.exists())

    def test_the_other_sizes_still_book_as_before(self):
        make_unit(6, Gown.Size.SMALL)
        large = make_unit(7, Gown.Size.LARGE)
        response = self.submit(large, "L")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(ReservationItem.objects.get().size, "Large")


NODE_SCRIPT = r"""
const vm = require('vm'), fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const ctx = { console };
ctx.window = ctx;
ctx.location = { href: '' };
ctx.document = { body: { dataset: {} }, readyState: 'complete', addEventListener() {}, getElementById: () => null,
  createElement: () => ({ set textContent(v) { this._t = String(v); }, get innerHTML() { return this._t; } }) };
ctx.addEventListener = () => {};
ctx.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
ctx.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
vm.createContext(ctx);
vm.runInContext(source, ctx);
const out = {
  xs: ['XS', 'xs', 'Size: XS', 'Extra Small', 'EXTRA SMALL'].map((s) => ctx.arabelaSizeCode(s)),
  small: ['S', 'Small', 'small', 'Size: S'].map((s) => ctx.arabelaSizeCode(s)),
  notSizes: ['XXS', 'X Small', ''].map((s) => ctx.arabelaSizeCode(s)),
  label: ctx.arabelaSizeLabel('Size: XS'),
  others: ['M', 'Medium', 'L', 'Large', 'XL', 'Extra Large', 'XXL', 'Free Size', 'FREE'].map((s) => ctx.arabelaSizeCode(s)),
};
console.log(JSON.stringify(out));
"""


class XsScriptTests(TestCase):
    def test_the_bag_and_checkout_script_understands_xs_in_every_shape(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is not installed")
        run = subprocess.run([node, "-e", NODE_SCRIPT, str(FLOW_JS)], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(out["xs"], ["XS"] * 5)
        self.assertEqual(out["small"], ["S"] * 4)                                           # Small is still Small, never XS
        self.assertEqual(out["notSizes"], ["", "", ""])
        self.assertEqual(out["label"], "XS")
        self.assertEqual(out["others"], ["M", "M", "L", "L", "XL", "XL", "XXL", "FREE", "FREE"])
