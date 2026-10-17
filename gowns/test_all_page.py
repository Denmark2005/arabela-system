"""The All page (Rent -> All) lists every product of EVERY category together.

The report (Oct 2026): the page only ever showed the Wedding Gowns. It was built as a pager -- one category's grid at a time with a Next
button to the following category -- so a customer landing on All saw the first category and nothing hinting the rest existed."""
import re
from decimal import Decimal

from django.test import TestCase

from gowns.context_processors import all_categories
from gowns.models import CustomCategory, Gown, HiddenCategory


def make(number, name, category, size=Gown.Size.LARGE, status=Gown.Status.AVAILABLE, price="1800"):
    return Gown.objects.create(gown_id=f"ALLP-{number:04d}", name=name, category=category, color_name="White", color_code="WH", size=size,
                               rental_price=Decimal(price), status=status)


class AllPageTests(TestCase):
    def walk(self, url):
        """Every card title across ALL the pages of a listing, following its Next links."""
        titles, page = [], 1
        while page <= 20:
            html = self.client.get(f"{url}?page={page}").content.decode()
            titles += self.cards(html)
            if "Next" not in html[html.index("<main"):html.index("</main>")]:
                break
            page += 1
        return titles

    def cards(self, html=None):
        html = html or self.client.get("/collections/all/").content.decode()
        main = html[html.index("<main"):html.index("</main>")]
        return re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', main)

    def test_every_category_shows_up_not_only_the_first(self):
        make(1, "Wedding Gown One", Gown.Category.WEDDING_GOWN)
        make(2, "Black Bridesmaid Dress 1", Gown.Category.BRIDESMAID_DRESSES)
        make(3, "Beige Barong 1", Gown.Category.BARONG, size=Gown.Size.MEDIUM)
        make(4, "Yellow Dress 1", Gown.Category.DRESSES)
        make(5, "Navy Suit", Gown.Category.SUIT, size=Gown.Size.MEDIUM)
        titles = self.cards()
        for expected in ("Wedding Gown One", "Black Bridesmaid Dress 1", "Beige Barong 1", "Yellow Dress 1", "Navy Suit"):
            self.assertIn(expected, titles)

    def test_one_card_per_product_however_many_sizes_it_has(self):
        for number, size in enumerate((Gown.Size.LARGE, Gown.Size.EXTRA_LARGE), start=1):
            make(number, "White Bridesmaid Dress 1", Gown.Category.BRIDESMAID_DRESSES, size=size)
        make(3, "Wedding Gown One", Gown.Category.WEDDING_GOWN)
        titles = self.cards()
        self.assertEqual(titles.count("White Bridesmaid Dress 1"), 1)
        self.assertIn("2 available", self.client.get("/collections/all/").content.decode())

    def test_the_order_is_the_category_order_then_each_categorys_own_order(self):
        wanted = {"Wedding Gown": "W Gown", "Barong": "B Gown", "Bridesmaid Dresses": "D Gown", "Dresses": "E Gown"}
        for number, (category, name) in enumerate(wanted.items(), start=1):
            make(number, name, category)
        order = [category["label"] for category in all_categories() if category["label"] in wanted]
        titles = self.cards()
        positions = [titles.index(wanted[label]) for label in order]
        self.assertEqual(positions, sorted(positions))

    def test_a_gown_staff_took_out_of_stock_is_not_listed_but_its_siblings_are(self):
        make(1, "Pink Dress 1", Gown.Category.DRESSES, size=Gown.Size.LARGE)
        make(2, "Pink Dress 1", Gown.Category.DRESSES, size=Gown.Size.EXTRA_LARGE, status=Gown.Status.OUT_OF_STOCK)
        make(3, "Gone Dress 1", Gown.Category.DRESSES, status=Gown.Status.OUT_OF_STOCK)
        titles = self.cards()
        self.assertIn("Pink Dress 1", titles)
        self.assertNotIn("Gone Dress 1", titles)

    def test_the_page_is_one_grid_with_no_category_pager_and_sorts_as_a_whole(self):
        make(1, "Wedding Gown One", Gown.Category.WEDDING_GOWN)
        make(2, "Yellow Dress 1", Gown.Category.DRESSES)
        html = self.client.get("/collections/all/").content.decode()
        for gone in ("all-pager-next", "all-pager-prev", "all-collection-panel", "Next collection"):
            self.assertNotIn(gone, html)
        main = html[html.index("<main"):html.index("</main>")]
        self.assertEqual(main.count("data-collection-sort-root"), 1)                     # one grid, so the sort drawer sorts the whole list
        self.assertEqual(len(re.findall(r'<img alt="[^"]*" loading="lazy"', html[html.index("<main"):html.index("</main>")])), 2)

    def test_every_card_links_to_its_own_product_page(self):
        make(1, "Wedding Gown One", Gown.Category.WEDDING_GOWN)
        make(2, "Yellow Dress 1", Gown.Category.DRESSES)
        html = self.client.get("/collections/all/").content.decode()
        main = html[html.index("<main"):html.index("</main>")]
        links = set(re.findall(r'href="(/collections/[a-z-]+/products/[^"]+)"', main))
        self.assertEqual({link.split("/")[2] for link in links}, {"wedding", "dresses"})
        for link in links:
            self.assertEqual(self.client.get(link).status_code, 200, link)

    # ---- it follows the shop automatically, the same way every category page does ----
    def test_a_new_product_in_any_category_appears_with_nothing_else_to_do(self):
        make(1, "Wedding Gown One", Gown.Category.WEDDING_GOWN)
        self.assertNotIn("Fresh Dress 1", self.cards())
        make(2, "Fresh Dress 1", Gown.Category.DRESSES)                                    # staff add a gown ...
        self.assertIn("Fresh Dress 1", self.cards())                                        # ... it is on the All page straight away

    def test_a_category_the_owner_adds_shows_its_products_on_the_all_page(self):
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        make(1, "Rose Debut 1", "Debut Gown", size=Gown.Size.MEDIUM)
        self.assertIn("Rose Debut 1", self.cards())
        page = self.client.get("/collections/debut-gown/")
        self.assertContains(page, "Rose Debut 1")                                           # the same product its own page shows

    def test_a_category_the_owner_removed_is_not_on_the_all_page_and_comes_back_when_restored(self):
        make(1, "Black Suit 1", Gown.Category.SUIT, size=Gown.Size.MEDIUM)
        make(2, "Wedding Gown One", Gown.Category.WEDDING_GOWN)
        self.assertIn("Black Suit 1", self.cards())
        hidden = HiddenCategory.objects.create(name="Suit")
        self.assertNotIn("Black Suit 1", self.cards())                                      # a removed category's gowns are not listed anywhere
        self.assertIn("Wedding Gown One", self.cards())
        hidden.delete()
        self.assertIn("Black Suit 1", self.cards())

    def test_the_all_page_is_exactly_every_category_pages_products_put_together(self):
        names = {"Wedding Gown": "W", "Barong": "B", "Bridesmaid Dresses": "BD", "Dresses": "D", "Suit": "S"}
        number = 0
        for category, tag in names.items():
            for i in range(1, 11 if category == "Dresses" else 3):                         # Dresses has 10 -> two pages of its own
                number += 1
                make(number, f"{tag} Item {i}", category)
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        for i in range(1, 4):
            number += 1
            make(number, f"Debut Item {i}", "Debut Gown", size=Gown.Size.MEDIUM)
        on_all = self.walk("/collections/all/")
        union = set()
        for category in all_categories():
            union |= set(self.walk(f"/collections/{category['key']}/"))
        self.assertEqual(len(on_all), len(set(on_all)))                                    # no product listed twice across the pages
        self.assertEqual(set(on_all), union)
        on_all = set(on_all)
        self.assertEqual(len(on_all), 21)                                                   # 2+2+2 (three categories) + 10 dresses + 2 suits + 3 debut

    # ---- 8 to a page with Previous / Next, like a collection page ----
    def fill(self, count):
        categories = ["Wedding Gown", "Barong", "Bridesmaid Dresses", "Dresses"]
        for number in range(1, count + 1):
            make(number, f"Item {number:02d}", categories[number % len(categories)])

    def test_eight_to_a_page_with_next_and_previous_and_nothing_lost_or_repeated(self):
        self.fill(20)
        first = self.client.get("/collections/all/").content.decode()
        second = self.client.get("/collections/all/?page=2").content.decode()
        third = self.client.get("/collections/all/?page=3").content.decode()
        self.assertEqual([len(self.cards(h)) for h in (first, second, third)], [8, 8, 4])
        self.assertIn("Page 1 of 3", first)
        self.assertIn('href="?page=2"', first)
        self.assertNotIn("Previous", first[first.index("<main"):first.index("</main>")])         # nothing before page 1
        self.assertIn('href="?page=1"', second)                                                    # Previous
        self.assertIn('href="?page=3"', second)                                                    # Next
        self.assertNotIn("Next", third[third.index("<main"):third.index("</main>")])               # nothing after the last page
        self.assertIn('href="?page=2"', third)
        together = self.cards(first) + self.cards(second) + self.cards(third)
        self.assertEqual(len(together), 20)
        self.assertEqual(len(set(together)), 20)

    def test_the_page_size_is_the_same_as_a_collection_pages(self):
        self.fill(20)
        for number in range(30, 50):
            make(number, f"Dress {number}", Gown.Category.DRESSES)
        self.assertEqual(len(self.cards(self.client.get("/collections/dresses/").content.decode())), 8)
        self.assertEqual(len(self.cards(self.client.get("/collections/all/").content.decode())), 8)

    def test_a_shop_with_eight_or_fewer_products_shows_no_pager(self):
        self.fill(8)
        html = self.client.get("/collections/all/").content.decode()
        self.assertEqual(len(self.cards(html)), 8)
        self.assertNotIn("Page 1 of", html)

    def test_a_wrong_page_number_never_breaks_the_page(self):
        self.fill(20)
        for bad in ("99", "0", "-3", "abc", "", "2.5"):
            with self.subTest(page=bad):
                response = self.client.get(f"/collections/all/?page={bad}")
                self.assertEqual(response.status_code, 200)
                self.assertGreater(len(self.cards(response.content.decode())), 0)
        self.assertEqual(len(self.cards(self.client.get("/collections/all/?page=99").content.decode())), 4)    # clamps to the last page

    def test_the_pager_button_reads_properly_with_no_category_name(self):
        self.fill(12)
        html = self.client.get("/collections/all/").content.decode()
        self.assertIn("Explore More Pieces", html)
        self.assertNotIn("Explore More  Pieces", html)
        for number in range(60, 70):                                                        # a category page keeps its own wording
            make(number, f"Dress {number}", Gown.Category.DRESSES)
        self.assertIn("Explore More Dresses Pieces", self.client.get("/collections/dresses/").content.decode())

    def test_an_empty_shop_says_so_instead_of_showing_a_blank_page(self):
        response = self.client.get("/collections/all/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No gowns available right now")
