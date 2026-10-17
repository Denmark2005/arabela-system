"""Sort by sorts the WHOLE category (or the whole All list), then pages it -- not just the 8 cards that happen to be on screen.

The owner's words: "when a customer in the Wedding category clicks Sort by it will show the lowest price in that category, not just the 8
products that show". The sort used to re-order the cards in the browser, so each page was sorted on its own and the cheapest gown of page 5
never reached page 1. Now the choice lives in the address (?sort=priceAsc), the server sorts every product, and Previous / Next keep it."""
import re
from decimal import Decimal

from django.test import TestCase

from gowns.context_processors import all_categories, category_url
from gowns.models import CustomCategory, Gown

WEDDING = Gown.Category.WEDDING_GOWN


def make(number, name, category=WEDDING, price="20000", status=Gown.Status.AVAILABLE, size=Gown.Size.MEDIUM):
    return Gown.objects.create(gown_id=f"SORT-{number:04d}", name=name, category=category, color_name="White", color_code="WH", size=size,
                               rental_price=Decimal(price), status=status)


def titles(response):
    html = response.content.decode()
    main = html[html.index("<main"):html.index("</main>")]
    return re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', main)


class ServerSortTests(TestCase):
    def setUp(self):
        # 20 wedding gowns with prices NOT in name order, so "first 8 by name" and "cheapest 8" are different sets
        self.prices = {}
        for number in range(1, 21):
            price = 30000 - (number * 937) % 17000                              # scattered, all different
            self.prices[f"Gown {number:02d}"] = price
            make(number, f"Gown {number:02d}", price=str(price))

    def page(self, n, **params):
        query = "&".join([f"{k}={v}" for k, v in params.items()] + [f"page={n}"])
        return self.client.get(f"/collections/wedding/?{query}")

    def walk(self, **params):
        out = []
        for n in (1, 2, 3):
            out += titles(self.page(n, **params))
        return out

    def test_price_low_to_high_puts_the_cheapest_of_the_whole_category_on_page_one(self):
        cheapest = sorted(self.prices, key=lambda name: (self.prices[name], name))
        first, second, third = (titles(self.page(n, sort="priceAsc")) for n in (1, 2, 3))
        self.assertEqual(first, [n for n in cheapest if n in set(first)])          # in price order on the page ...
        self.assertEqual(set(first), set(cheapest[:8]))                            # ... and they ARE the 8 cheapest of all 20
        self.assertEqual(set(second), set(cheapest[8:16]))
        self.assertEqual(set(third), set(cheapest[16:]))

    def test_price_high_to_low_puts_the_dearest_first(self):
        first = titles(self.page(1, sort="priceDesc"))
        prices = [self.prices[n] for n in first]
        self.assertEqual(prices, sorted(prices, reverse=True))
        self.assertEqual(max(prices), max(self.prices.values()))                  # the dearest gown in the category is on page 1
        self.assertEqual(set(prices), set(sorted(self.prices.values(), reverse=True)[:8]))

    def test_alphabetical_in_both_directions_across_every_page(self):
        az = self.walk(sort="az")
        self.assertEqual(az, sorted(self.prices, key=str.casefold))
        self.assertEqual(self.walk(sort="za"), sorted(self.prices, key=str.casefold, reverse=True))

    def test_every_sort_shows_every_product_exactly_once_across_the_pages(self):
        for mode in ("az", "za", "priceAsc", "priceDesc", ""):
            with self.subTest(sort=mode):
                everything = self.walk(sort=mode) if mode else self.walk()
                self.assertEqual(len(everything), 20)
                self.assertEqual(set(everything), set(self.prices))

    def test_the_pager_links_keep_the_sort(self):
        html = self.page(2, sort="priceAsc").content.decode()
        self.assertIn('href="?page=1&amp;sort=priceAsc"', html)                   # Previous
        self.assertIn('href="?page=3&amp;sort=priceAsc"', html)                   # Next
        plain = self.client.get("/collections/wedding/").content.decode()
        self.assertIn('href="?page=2"', plain)                                    # no sort chosen: the links stay plain
        self.assertNotIn("sort=", plain[plain.index("Page 1 of"):])

    def test_equal_prices_keep_their_normal_order(self):
        Gown.objects.all().delete()
        for number in range(1, 13):
            make(number, f"Same {number:02d}", price="20000")
        normal = titles(self.client.get("/collections/wedding/"))
        self.assertEqual(titles(self.client.get("/collections/wedding/?sort=priceAsc")), normal)
        self.assertEqual(titles(self.client.get("/collections/wedding/?sort=priceDesc")), normal)

    def test_a_wrong_sort_or_page_never_breaks_the_page(self):
        normal = titles(self.client.get("/collections/wedding/"))
        for bad in ("nope", "PRICEASC", "", "priceAsc;drop", "<script>"):
            with self.subTest(sort=bad):
                response = self.client.get("/collections/wedding/", {"sort": bad})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(titles(response), normal)                         # ignored: the normal order
                self.assertNotIn("sort=", response.content.decode()[response.content.decode().index("Page 1 of"):])
        for bad_page in ("99", "0", "-1", "abc"):
            self.assertEqual(self.client.get(f"/collections/wedding/?sort=priceAsc&page={bad_page}").status_code, 200)
        self.assertEqual(len(titles(self.client.get("/collections/wedding/?sort=priceAsc&page=99"))), 4)    # clamps to the last page

    def test_out_of_stock_gowns_stay_out_whatever_the_sort(self):
        make(50, "Gone Gown", price="1", status=Gown.Status.OUT_OF_STOCK)
        self.assertNotIn("Gone Gown", self.walk(sort="priceAsc"))

    def test_the_page_tells_the_sort_script_to_use_the_address(self):
        marker = re.search(r'<span id="collection-sort-server" data-sort-mode="([^"]*)" hidden></span>',
                           self.client.get("/collections/wedding/?sort=priceDesc").content.decode())
        self.assertEqual(marker.group(1), "priceDesc")
        none = re.search(r'<span id="collection-sort-server" data-sort-mode="([^"]*)" hidden>', self.client.get("/collections/wedding/").content.decode())
        self.assertEqual(none.group(1), "")


class EveryListingSortsTests(TestCase):
    def test_every_collection_page_and_the_all_page_accept_a_sort(self):
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        for number, category in enumerate(["Wedding Gown", "Barong", "Bridesmaid Dresses", "Dresses", "Suit", "Debut Gown"], start=1):
            make(number, f"Item {number}", category=category, price=str(1000 + number * 100))
        urls = [category_url(row) for row in all_categories()] + ["/collections/all/"]
        for url in urls:
            for mode in ("az", "za", "priceAsc", "priceDesc"):
                with self.subTest(url=url, sort=mode):
                    response = self.client.get(url, {"sort": mode})
                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, f'data-sort-mode="{mode}" hidden')

    def test_the_all_page_sorts_across_every_category_together(self):
        cheap = {"Barong": "Cheap Barong", "Suit": "Cheap Suit"}
        make(1, "Dear Wedding", price="25000")
        for number in range(2, 12):
            make(number, f"Mid Dress {number}", category=Gown.Category.DRESSES, price="1800")
        make(20, "Cheap Barong", category=Gown.Category.BARONG, price="500")
        make(21, "Cheap Suit", category=Gown.Category.SUIT, price="300")
        first = titles(self.client.get("/collections/all/?sort=priceAsc"))
        self.assertEqual(first[:2], ["Cheap Suit", "Cheap Barong"])                 # the cheapest of ALL categories lead page 1
        dear = titles(self.client.get("/collections/all/?sort=priceDesc"))
        self.assertEqual(dear[0], "Dear Wedding")
        self.assertEqual(len(first), 8)
        every = []
        for n in (1, 2):
            every += titles(self.client.get(f"/collections/all/?sort=priceAsc&page={n}"))
        self.assertEqual(len(every), 13)
        self.assertEqual(len(set(every)), 13)

    def test_the_search_page_still_sorts_in_the_browser(self):
        make(1, "Wedding One", price="100")
        html = self.client.get("/search/?q=wed").content.decode()
        self.assertIn("data-collection-sort-root", html)
        self.assertNotIn("collection-sort-server", html)                             # no marker: the browser re-orders these cards
