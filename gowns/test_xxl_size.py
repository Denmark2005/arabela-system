"""The XXL (Extra Extra Large) size, end to end: the gown page, the bag/checkout script, the booking, and the admin.

The owner's request (Oct 2026): the gown page shows Small to XXL, a size the shop does not have is greyed and cannot be clicked, and the day
staff add an XXL unit in Gown Catalog it is clickable and bookable with no other change. Everything draws from Gown.Size / SIZE_CODES, so
these tests follow one XXL gown through every place a size travels."""
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
from gowns.models import SIZE_CODES, Gown, size_codes_of
from gowns.views import (
    _UNIT_ASSIGNED, _UNIT_SIZE_NOT_OFFERED, _UNIT_SIZE_REQUIRED, _find_available_unit, _normalize_size,
)
from reservations.models import Reservation, ReservationItem

User = get_user_model()
FLOW_JS = Path(settings.BASE_DIR) / "static" / "js" / "reservation-flow.js"
_TINY_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


def make_unit(n, size, name="Big Size Gown", category=Gown.Category.WEDDING_GOWN, status=Gown.Status.AVAILABLE):
    return Gown.objects.create(
        gown_id=f"XXL-{n:04d}", name=name, category=category, color_name="White", color_code="WH", size=size,
        rental_price=Decimal("5000"), status=status)


class XxlModelTests(TestCase):
    def test_the_size_exists_and_fits_the_column(self):
        self.assertIn("Extra Extra Large", Gown.Size.values)
        self.assertLessEqual(len("Extra Extra Large"), Gown.size.field.max_length)
        self.assertEqual(SIZE_CODES[Gown.Size.EXTRA_EXTRA_LARGE], "XXL")

    def test_xxl_comes_after_xl_and_before_free_size_in_every_list_of_sizes(self):
        units = [make_unit(1, Gown.Size.FREE_SIZE), make_unit(2, Gown.Size.EXTRA_EXTRA_LARGE), make_unit(3, Gown.Size.EXTRA_LARGE),
                 make_unit(4, Gown.Size.SMALL)]
        self.assertEqual(size_codes_of(units), ["S", "XL", "XXL", "FREE"])

    def test_a_gown_can_be_saved_in_xxl(self):
        unit = make_unit(5, Gown.Size.EXTRA_EXTRA_LARGE)
        unit.full_clean(exclude=["slug", "photo_url"])
        unit.refresh_from_db()
        self.assertEqual(unit.size, "Extra Extra Large")

    def test_every_way_of_writing_it_means_xxl_and_xl_stays_xl(self):
        for raw in ("XXL", "xxl", " Xxl ", "2XL", "2xl", "Extra Extra Large", "EXTRA EXTRA LARGE"):
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_size(raw), Gown.Size.EXTRA_EXTRA_LARGE)
        for raw in ("XL", "Extra Large"):
            self.assertEqual(_normalize_size(raw), Gown.Size.EXTRA_LARGE)
        for raw in ("XXXL", "3XL", "XX", ""):
            self.assertEqual(_normalize_size(raw), "")                                # still never a guess


class XxlGownPageTests(TestCase):
    def page(self, unit):
        return self.client.get(reverse("gowns:product_detail", kwargs={"collection": "wedding", "slug": unit.slug}))

    def row(self, html):
        return re.findall(r'data-(?:product-size|size-not-offered)="([A-Z]+)"', html)

    def test_every_gown_page_shows_the_xxl_button_and_greys_it_when_the_shop_has_none(self):
        unit = make_unit(1, Gown.Size.MEDIUM)
        html = self.page(unit).content.decode()
        self.assertEqual(self.row(html), ["S", "M", "L", "XL", "XXL"])
        grey = re.search(r'<button[^>]*data-size-not-offered="XXL"[^>]*>', html).group(0)
        self.assertIn('aria-disabled="true"', grey)
        self.assertIn("This gown is not available in Extra Extra Large", grey)
        self.assertNotIn("data-product-size", grey)                                   # the page's script never counts it as a size
        self.assertIn("min-w-12", grey)                                               # three letters fit the button

    def test_the_day_staff_add_an_xxl_unit_it_is_clickable_with_no_other_change(self):
        unit = make_unit(2, Gown.Size.LARGE)
        self.assertNotIn('data-product-size="XXL"', self.page(unit).content.decode())
        make_unit(3, Gown.Size.EXTRA_EXTRA_LARGE)
        response = self.page(unit)
        html = response.content.decode()
        self.assertIn('data-product-size="XXL"', html)
        self.assertNotIn('data-size-not-offered="XXL"', html)
        self.assertEqual([o["code"] for o in response.context["size_options"]], ["L", "XXL"])
        offered = re.search(r'<button[^>]*data-product-size="XXL"[^>]*>', html).group(0)
        self.assertNotIn("aria-disabled", offered)
        self.assertIn('data-size-name="Extra Extra Large"', offered)
        self.assertIn("min-w-12", offered)

    def test_the_page_calendar_and_busy_dates_are_per_size_for_xxl_too(self):
        unit = make_unit(4, Gown.Size.EXTRA_EXTRA_LARGE)
        make_unit(5, Gown.Size.MEDIUM)
        response = self.page(unit)
        plans = response.context["calendar_data"]["plans"]
        self.assertIn("XXL", plans)
        self.assertIn("M", plans)

    def test_an_xxl_only_gown_still_offers_just_xxl(self):
        unit = make_unit(6, Gown.Size.EXTRA_EXTRA_LARGE, name="Plus Size Gown")
        html = self.page(unit).content.decode()
        self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', html), ["XXL"])
        self.assertEqual(re.findall(r'data-size-not-offered="([A-Z]+)"', html), ["S", "M", "L", "XL"])

    def test_the_card_plus_button_data_carries_xxl_in_order(self):
        make_unit(7, Gown.Size.EXTRA_EXTRA_LARGE, name="Catalog Gown")
        make_unit(8, Gown.Size.SMALL, name="Catalog Gown")
        row = next(item for item in _build_search_catalog() if item["title"] == "Catalog Gown")
        self.assertEqual(row["sizes"], ["S", "XXL"])


class XxlBookingTests(TestCase):
    def setUp(self):
        self.start = timezone.localdate() + timedelta(days=20)
        self.end = self.start + timedelta(days=4)

    def test_xxl_is_picked_when_chosen_in_any_spelling_and_never_given_for_xl(self):
        xl, xxl = make_unit(1, Gown.Size.EXTRA_LARGE), make_unit(2, Gown.Size.EXTRA_EXTRA_LARGE)
        for raw in ("XXL", "2XL", "Extra Extra Large"):
            self.assertEqual(_find_available_unit(xl.name, self.start, self.end, size=raw), (_UNIT_ASSIGNED, xxl), raw)
        self.assertEqual(_find_available_unit(xl.name, self.start, self.end, size="XL"), (_UNIT_ASSIGNED, xl))

    def test_xxl_is_refused_when_the_gown_does_not_come_in_it(self):
        unit = make_unit(3, Gown.Size.MEDIUM)
        self.assertEqual(_find_available_unit(unit.name, self.start, self.end, size="XXL"), (_UNIT_SIZE_NOT_OFFERED, None))

    def test_a_gown_with_xxl_and_other_sizes_needs_one_chosen(self):
        unit = make_unit(4, Gown.Size.MEDIUM)
        make_unit(5, Gown.Size.EXTRA_EXTRA_LARGE)
        self.assertEqual(_find_available_unit(unit.name, self.start, self.end, size=""), (_UNIT_SIZE_REQUIRED, None))


class XxlCheckoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="xxl_customer", password="x")

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
            "items": json.dumps(items), "first_name": "Big", "last_name": "Buyer", "phone": "09171234567",
            "address": "1 Test St", "city": "Test City", "postal_code": "1000", "payment_method": "GCash",
            "proof_of_payment": SimpleUploadedFile("proof.jpg", _TINY_JPEG, content_type="image/jpeg"),
        })

    def test_an_xxl_gown_books_end_to_end_and_records_the_real_size(self):
        make_unit(1, Gown.Size.MEDIUM)
        xxl = make_unit(2, Gown.Size.EXTRA_EXTRA_LARGE)
        response = self.submit(xxl, "XXL")
        self.assertEqual(response.status_code, 200, response.content)
        item = ReservationItem.objects.get(reservation__reference_code=response.json()["reference_code"])
        self.assertEqual((item.gown_id, item.size), (xxl.id, "Extra Extra Large"))
        self.assertLessEqual(len(item.size), ReservationItem._meta.get_field("size").max_length)

    def test_the_xxl_unit_is_taken_for_those_dates_and_the_next_customer_is_told(self):
        make_unit(3, Gown.Size.MEDIUM)
        xxl = make_unit(4, Gown.Size.EXTRA_EXTRA_LARGE)
        self.assertEqual(self.submit(xxl, "XXL").status_code, 200)
        second = Client()
        second.force_login(User.objects.create_user(username="xxl_second", password="x"))
        self.client = second                                                          # another customer wants the same XXL, same dates
        again = self.submit(xxl, "XXL")
        self.assertNotEqual(again.status_code, 200)                                   # refused: it is the only XXL unit, and it is taken
        self.assertEqual(Reservation.objects.count(), 1)                              # one booking, no double-booking

    def test_asking_for_xxl_on_a_gown_that_has_none_is_refused_clearly(self):
        unit = make_unit(5, Gown.Size.MEDIUM)
        response = self.submit(unit, "XXL")
        self.assertEqual(response.status_code, 400)
        self.assertIn("doesn't come in", response.json()["error"])
        self.assertFalse(Reservation.objects.exists())

    def test_the_other_sizes_still_book_as_before(self):
        make_unit(6, Gown.Size.MEDIUM)
        large = make_unit(7, Gown.Size.LARGE)
        response = self.submit(large, "L")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(ReservationItem.objects.get().size, "Large")


class XxlAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="xxl_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def create(self, **overrides):
        data = dict(name="Plus Gown", category=Gown.Category.WEDDING_GOWN, color_name="White", color_code="WH",
                    size=Gown.Size.EXTRA_EXTRA_LARGE, rental_price="4500")
        data.update(overrides)
        return self.client.post(reverse("arabela_admin:gown_create"), data=data)

    def test_the_add_and_edit_gown_form_offers_xxl(self):
        response = self.client.get(reverse("arabela_admin:gown_catalog"))
        self.assertContains(response, '<option value="Extra Extra Large">Extra Extra Large (XXL)</option>')
        html = response.content.decode()
        order = re.findall(r'<option value="(Small|Medium|Large|Extra Large|Extra Extra Large|Free Size)">', html)
        self.assertEqual(order[:6], ["Small", "Medium", "Large", "Extra Large", "Extra Extra Large", "Free Size"])

    def test_staff_can_add_an_xxl_gown_and_it_shows_on_the_customer_page(self):
        response = self.create()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["success"])
        gown = Gown.objects.get(id=response.json()["gown"]["id"])
        self.assertEqual(gown.size, "Extra Extra Large")
        page = self.client.get(reverse("gowns:product_detail", kwargs={"collection": "wedding", "slug": gown.slug}))
        self.assertContains(page, 'data-product-size="XXL"')

    def test_staff_can_change_a_gown_to_xxl(self):
        gown = make_unit(1, Gown.Size.MEDIUM, name="Edit Me Gown")
        response = self.client.post(reverse("arabela_admin:gown_update", kwargs={"gown_id": gown.id}), data=dict(
            name=gown.name, category=gown.category, color_name=gown.color_name, color_code=gown.color_code,
            size=Gown.Size.EXTRA_EXTRA_LARGE, rental_price="5000"))
        self.assertEqual(response.status_code, 200, response.content)
        gown.refresh_from_db()
        self.assertEqual(gown.size, "Extra Extra Large")

    def test_a_made_up_size_is_still_refused(self):
        for bad in ("XXXL", "XXL ", "Huge", ""):
            with self.subTest(size=bad):
                response = self.create(size=bad, name=f"Bad {bad!r}")
                self.assertEqual(response.status_code, 400)
                self.assertIn("valid size", response.json()["error"])

    def test_the_same_design_in_xl_and_xxl_is_one_product_with_two_sizes(self):
        self.assertEqual(self.create(name="Twin Design", size=Gown.Size.EXTRA_LARGE).status_code, 200)
        # the same name again asks staff "same gown or a different one?" -- they answer "same gown (another size)"
        self.assertEqual(self.create(name="Twin Design", size=Gown.Size.EXTRA_EXTRA_LARGE, name_choice="same").status_code, 200)
        units = list(Gown.objects.filter(name="Twin Design"))
        self.assertEqual(sorted(g.size for g in units), ["Extra Extra Large", "Extra Large"])
        self.assertEqual(size_codes_of(units), ["XL", "XXL"])


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
  codes: ['XXL', 'xxl', 'Size: XXL', 'Extra Extra Large', 'EXTRA EXTRA LARGE', '2XL', 'XL', 'Extra Large', 'XXXL', ''].map((s) => ctx.arabelaSizeCode(s)),
  label: ctx.arabelaSizeLabel('Size: XXL'),
  others: ['S', 'Small', 'M', 'Medium', 'L', 'Large', 'Free Size', 'FREE'].map((s) => ctx.arabelaSizeCode(s)),
};
console.log(JSON.stringify(out));
"""


class XxlScriptTests(TestCase):
    def test_the_bag_and_checkout_script_understands_xxl_in_every_shape(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is not installed")
        run = subprocess.run([node, "-e", NODE_SCRIPT, str(FLOW_JS)], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(out["codes"], ["XXL", "XXL", "XXL", "XXL", "XXL", "XXL", "XL", "XL", "", ""])
        self.assertEqual(out["label"], "XXL")
        self.assertEqual(out["others"], ["S", "S", "M", "M", "L", "L", "FREE", "FREE"])
