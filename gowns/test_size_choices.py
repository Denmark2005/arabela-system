"""The gown page's size row: Small to Extra Large every time, and only the sizes staff really have in Gown Catalog can be chosen.

The report (Oct 2026): the page only drew the sizes a gown comes in (a Wedding Gown in Medium showed a lone "M"). The owner wants the
whole range visible, with the sizes the shop has clickable and the rest greyed out. The list of OFFERED sizes (context `size_options`)
is untouched -- the + button, the calendar and checkout all depend on it; `size_choices` is only what the page draws."""
import re
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from gowns.models import Gown
from gowns.views import _size_choices, _size_options


def option(code, count=1):
    names = {"S": "Small", "M": "Medium", "L": "Large", "XL": "Extra Large", "FREE": "Free Size"}
    return {"code": code, "label": "Free Size" if code == "FREE" else code, "name": names[code], "count": count}


def shape(choices):
    return [(c["code"], c["offered"]) for c in choices]


class SizeChoicesTests(SimpleTestCase):
    def test_only_small_offered_greys_the_other_three(self):
        self.assertEqual(shape(_size_choices([option("S")])), [("S", True), ("M", False), ("L", False), ("XL", False)])

    def test_only_medium_offered(self):
        self.assertEqual(shape(_size_choices([option("M")])), [("S", False), ("M", True), ("L", False), ("XL", False)])

    def test_three_sizes_offered_leave_extra_large_greyed(self):
        self.assertEqual(shape(_size_choices([option("S"), option("M"), option("L")])),
                         [("S", True), ("M", True), ("L", True), ("XL", False)])

    def test_every_size_offered(self):
        self.assertEqual(shape(_size_choices([option(c) for c in ("S", "M", "L", "XL")])),
                         [("S", True), ("M", True), ("L", True), ("XL", True)])

    def test_the_row_always_runs_smallest_to_largest_whatever_order_the_sizes_arrive_in(self):
        self.assertEqual([c["code"] for c in _size_choices([option("XL"), option("S")])], ["S", "M", "L", "XL"])

    def test_a_gown_that_is_only_free_size_shows_just_that_button(self):
        self.assertEqual(shape(_size_choices([option("FREE")])), [("FREE", True)])

    def test_free_size_next_to_other_sizes_comes_last_and_is_not_added_when_the_gown_lacks_it(self):
        self.assertEqual(shape(_size_choices([option("FREE"), option("S")])),
                         [("S", True), ("M", False), ("L", False), ("XL", False), ("FREE", True)])
        self.assertNotIn("FREE", [c["code"] for c in _size_choices([option("S")])])

    def test_no_sizes_draws_nothing(self):
        self.assertEqual(_size_choices([]), [])

    def test_a_greyed_size_carries_its_full_name_and_no_stock(self):
        xl = next(c for c in _size_choices([option("S")]) if c["code"] == "XL")
        self.assertEqual((xl["name"], xl["label"], xl["count"], xl["offered"]), ("Extra Large", "XL", 0, False))

    def test_an_offered_size_keeps_its_stock_count(self):
        medium = next(c for c in _size_choices([option("M", 3)]) if c["code"] == "M")
        self.assertEqual((medium["count"], medium["offered"]), (3, True))


class GownPageSizeRowTests(TestCase):
    def make_unit(self, number, size, name="Row Test Gown", status=Gown.Status.AVAILABLE):
        return Gown.objects.create(
            gown_id=f"ROW-{number:04d}", name=name, category=Gown.Category.WEDDING_GOWN, color_name="White", color_code="WH",
            size=size, rental_price=Decimal("5000"), status=status)

    def page(self, unit):
        return self.client.get(reverse("gowns:product_detail", kwargs={"collection": "wedding", "slug": unit.slug}))

    def chosen(self, html):
        return re.findall(r'data-product-size="([A-Z]+)"', html)

    def greyed(self, html):
        return re.findall(r'data-size-not-offered="([A-Z]+)"', html)

    def test_a_medium_only_gown_shows_all_four_but_only_medium_can_be_chosen(self):
        unit = self.make_unit(1, Gown.Size.MEDIUM)
        response = self.page(unit)
        html = response.content.decode()
        self.assertEqual(self.chosen(html), ["M"])
        self.assertEqual(self.greyed(html), ["S", "L", "XL"])
        self.assertEqual([o["code"] for o in response.context["size_options"]], ["M"])     # the offered list is unchanged

    def test_a_small_only_gown_makes_small_the_clickable_one(self):
        unit = self.make_unit(2, Gown.Size.SMALL, name="Small Only Gown")
        html = self.page(unit).content.decode()
        self.assertEqual(self.chosen(html), ["S"])
        self.assertEqual(self.greyed(html), ["M", "L", "XL"])

    def test_the_row_reads_small_to_extra_large_left_to_right(self):
        unit = self.make_unit(3, Gown.Size.LARGE, name="Order Gown")
        html = self.page(unit).content.decode()
        order = re.findall(r'data-(?:product-size|size-not-offered)="([A-Z]+)"', html)
        self.assertEqual(order, ["S", "M", "L", "XL"])

    def test_three_sizes_in_the_shop_leave_only_extra_large_greyed(self):
        units = [self.make_unit(i, size, name="Barong Like") for i, size in
                 enumerate((Gown.Size.SMALL, Gown.Size.MEDIUM, Gown.Size.LARGE), start=4)]
        html = self.page(units[0]).content.decode()
        self.assertEqual(self.chosen(html), ["S", "M", "L"])
        self.assertEqual(self.greyed(html), ["XL"])

    def test_every_size_in_the_shop_greys_nothing(self):
        units = [self.make_unit(i, size, name="Full Range Gown") for i, size in
                 enumerate((Gown.Size.SMALL, Gown.Size.MEDIUM, Gown.Size.LARGE, Gown.Size.EXTRA_LARGE), start=8)]
        html = self.page(units[0]).content.decode()
        self.assertEqual(self.chosen(html), ["S", "M", "L", "XL"])
        self.assertEqual(self.greyed(html), [])

    def test_a_size_staff_marked_out_of_stock_is_greyed_not_offered(self):
        unit = self.make_unit(12, Gown.Size.MEDIUM, name="Stock Gown")
        self.make_unit(13, Gown.Size.LARGE, name="Stock Gown", status=Gown.Status.OUT_OF_STOCK)
        html = self.page(unit).content.decode()
        self.assertEqual(self.chosen(html), ["M"])
        self.assertIn("L", self.greyed(html))

    def test_a_size_staff_adds_later_becomes_clickable_with_no_other_change(self):
        unit = self.make_unit(14, Gown.Size.SMALL, name="Growing Gown")
        self.assertEqual(self.chosen(self.page(unit).content.decode()), ["S"])
        self.make_unit(15, Gown.Size.EXTRA_LARGE, name="Growing Gown")
        html = self.page(unit).content.decode()
        self.assertEqual(self.chosen(html), ["S", "XL"])
        self.assertEqual(self.greyed(html), ["M", "L"])

    def test_a_free_size_gown_shows_only_its_own_button(self):
        unit = self.make_unit(16, Gown.Size.FREE_SIZE, name="Free Gown")
        html = self.page(unit).content.decode()
        self.assertEqual(self.chosen(html), ["FREE"])
        self.assertEqual(self.greyed(html), [])
        self.assertIn(">Free Size</button>", html)

    def test_a_greyed_size_cannot_be_mistaken_for_a_choosable_one(self):
        unit = self.make_unit(17, Gown.Size.MEDIUM, name="Mark Gown")
        html = self.page(unit).content.decode()
        greyed_button = re.search(r'<button[^>]*data-size-not-offered="XL"[^>]*>', html).group(0)
        self.assertIn('aria-disabled="true"', greyed_button)
        self.assertIn("product-size-na", greyed_button)
        self.assertNotIn("data-product-size", greyed_button)          # the page's script only ever counts data-product-size buttons
        self.assertNotIn("product-size-btn", greyed_button)           # and the line-through look stays for "rented on your dates"
        offered_button = re.search(r'<button[^>]*data-product-size="M"[^>]*>', html).group(0)
        self.assertNotIn("aria-disabled", offered_button)

    def test_a_fully_unavailable_gown_has_no_size_row_at_all(self):
        unit = self.make_unit(18, Gown.Size.MEDIUM, name="Gone Gown", status=Gown.Status.OUT_OF_STOCK)
        html = self.page(unit).content.decode()
        self.assertEqual(self.chosen(html), [])
        self.assertEqual(self.greyed(html), [])

    def test_no_template_comment_leaks_onto_the_page(self):
        # Found in the browser check: a comment spread over three lines is not a comment ({# #} must close on its own line), and its
        # words showed up on the gown page above the size buttons.
        unit = self.make_unit(20, Gown.Size.MEDIUM, name="Leak Gown")
        html = self.page(unit).content.decode()
        for leaked in ("{#", "#}", "{%", "%}", "carry data-product-size", "never treats them"):
            self.assertNotIn(leaked, html, leaked)

    def test_the_offered_list_helper_itself_is_unchanged(self):
        unit = self.make_unit(19, Gown.Size.SMALL, name="Helper Gown")
        self.assertEqual([o["code"] for o in _size_options([unit])], ["S"])


class GownPageScriptTests(TestCase):
    """The script counts, restores and re-enables only data-product-size buttons; the greyed ones are wired for a note, nothing more."""

    def source(self):
        return (Path(settings.BASE_DIR) / "templates" / "products.html").read_text(encoding="utf-8")

    def test_the_script_still_counts_only_the_choosable_buttons(self):
        self.assertIn("document.querySelectorAll('[data-product-size]')", self.source())

    def test_tapping_a_greyed_size_says_why_and_selects_nothing(self):
        source = self.source()
        self.assertIn("document.querySelectorAll('[data-size-not-offered]')", source)
        self.assertIn("This gown is not available in ", source)
