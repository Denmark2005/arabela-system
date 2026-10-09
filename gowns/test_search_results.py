"""The search bar's "View all": a results page with every matching gown from EVERY category, opened already on
Best Match, with the same gowns the overlay finds, a live View all link on every page that has the search bar, and
a 5th "Best match" row in the sort drawer that exists only on that page."""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from gowns.models import CustomCategory, Gown

SEARCH = "/search/"


def make_gown(n, name, category, status=Gown.Status.AVAILABLE, price="20000"):
    return Gown.objects.create(
        gown_id=f"SRCH-{n:04d}", name=name, category=category, color_name="White", color_code="WH",
        size=Gown.Size.MEDIUM, rental_price=Decimal(price), status=status,
    )


class SearchResultsTests(TestCase):
    def search(self, q):
        return self.client.get(SEARCH, {"q": q})

    def titles(self, q):
        return [item["title"] for item in self.search(q).context["results"]]

    # ---- every category, together -----------------------------------------------------------------------------------
    def test_gowns_from_every_category_appear_together_each_linking_to_its_own_page(self):
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown", audience="women")
        wedding = make_gown(1, "Wedding Gown One", "Wedding Gown")
        long_gown = make_gown(2, "Newest Long Gown", "Long Gown")
        suit = make_gown(3, "Sweet Suit", "Suit")
        debut = make_gown(4, "Welcome Debut", "Debut Gown")
        make_gown(5, "Plain Dress", "Dresses")                      # no "we" in it

        response = self.search("we")
        self.assertEqual(response.status_code, 200)
        self.assertCountEqual(
            [item["title"] for item in response.context["results"]],
            ["Wedding Gown One", "Newest Long Gown", "Sweet Suit", "Welcome Debut"],
        )
        html = response.content.decode()
        for key, gown in (("wedding", wedding), ("long-gown", long_gown), ("suit", suit), ("debut-gown", debut)):
            with self.subTest(category=key):
                self.assertIn(reverse("gowns:product_detail", kwargs={"collection": key, "slug": gown.slug}), html)
        # (the search bar's own hidden list of every gown is on every page, so look at the result cards only)
        self.assertNotIn(">Plain Dress</h3>", html)
        self.assertEqual(html.count('class="group cursor-pointer block"'), 4)

    def test_it_finds_exactly_the_gowns_the_overlay_finds_and_the_rest(self):
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown", audience="women")
        for n, (name, category) in enumerate([
            ("Wedding Gown One", "Wedding Gown"), ("Wedding Gown 2", "Wedding Gown"), ("Newest Long Gown", "Long Gown"),
            ("Sweet Suit", "Suit"), ("Welcome Debut", "Debut Gown"), ("Plain Dress", "Dresses"), ("Guest Of Honor", "Guest Gown"),
        ], start=1):
            make_gown(n, name, category)
        make_gown(20, "Wedding Hidden", "Wedding Gown", status=Gown.Status.OUT_OF_STOCK)
        for q in ("we", "wedding gown", "gown", "s", "zzz", "o"):
            with self.subTest(q=q):
                response = self.search(q)
                overlay = {
                    (p["collection_key"], p["slug"]) for p in response.context["search_overlay_catalog"]
                    if q.lower() in p["title"].lower()          # the overlay's own rule, read from its own list
                }
                page = {(i["collection_key"], i["slug"]) for i in response.context["results"]}
                self.assertEqual(page, overlay)

    def test_a_product_with_several_units_is_one_card_that_says_how_many(self):
        make_gown(1, "Wedding Gown One", "Wedding Gown")
        make_gown(2, "Wedding Gown One", "Wedding Gown")
        response = self.search("wedding")
        self.assertEqual(len(response.context["results"]), 1)
        self.assertContains(response, "2 available")

    def test_out_of_stock_gowns_are_never_listed(self):
        make_gown(1, "Wedding Gown One", "Wedding Gown")
        make_gown(2, "Wedding Retired", "Wedding Gown", status=Gown.Status.OUT_OF_STOCK)
        self.assertEqual(self.titles("wedding"), ["Wedding Gown One"])

    # ---- Best Match -----------------------------------------------------------------------------------------------------
    def test_best_match_puts_names_that_start_with_it_first_then_words_then_anywhere_then_a_to_z(self):
        make_gown(1, "Unwed Gown", "Long Gown")               # "wed" only inside a word
        make_gown(2, "Dream Wedding", "Suit")                 # a word that starts with it
        make_gown(3, "Wedding Gown", "Wedding Gown")          # the name starts with it
        make_gown(4, "Bridal Wedding", "Dresses")             # a word that starts with it (A-Z before Dream)
        self.assertEqual(self.titles("wed"), ["Wedding Gown", "Bridal Wedding", "Dream Wedding", "Unwed Gown"])

    def test_capitals_and_spaces_around_the_search_do_not_matter(self):
        make_gown(1, "Wedding Gown One", "Wedding Gown")
        self.assertEqual(self.titles("  WEDDING  "), ["Wedding Gown One"])
        self.assertEqual(self.titles("wEdDiNg gOwN"), ["Wedding Gown One"])

    def test_the_page_opens_on_best_match_and_remembers_a_sort_per_search(self):
        make_gown(1, "Wedding Gown One", "Wedding Gown")
        html = self.search("wed").content.decode()
        self.assertIn("data-collection-sort-root", html)
        self.assertIn('data-default-sort-mode="bestMatch"', html)
        self.assertIn('data-default-sort-label="BEST MATCH"', html)
        self.assertIn("data-sort-per-query", html)

    # ---- the page's words -------------------------------------------------------------------------------------------------
    def test_it_says_how_many_results_in_the_page_and_the_tab(self):
        make_gown(1, "Wedding Gown One", "Wedding Gown")
        one = self.search("wed")
        self.assertContains(one, "1 result for &ldquo;wed&rdquo;")
        self.assertContains(one, 'Search: 1 result for "wed"')
        make_gown(2, "Wedding Gown 2", "Wedding Gown")
        self.assertContains(self.search("wed"), "2 results for &ldquo;wed&rdquo;")

    def test_nothing_typed_goes_to_the_all_page(self):
        for url in (SEARCH, SEARCH + "?q=", SEARCH + "?q=%20%20"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertRedirects(response, reverse("gowns:collection_all"), fetch_redirect_response=False)

    def test_no_match_says_so_offers_the_all_page_and_has_nothing_to_sort(self):
        make_gown(1, "Wedding Gown One", "Wedding Gown")
        response = self.search("zzz")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No gowns match &ldquo;zzz&rdquo;")
        self.assertContains(response, "0 results for")
        self.assertContains(response, reverse("gowns:collection_all"))
        self.assertNotContains(response, 'onclick="toggleSort()"')
        self.assertNotContains(response, "data-default-sort-mode")

    def test_what_was_typed_is_escaped(self):
        for q in ("<script>alert(1)</script>", '"><img src=x onerror=alert(1)>'):
            with self.subTest(q=q):
                response = self.search(q)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, q)

    def test_a_search_longer_than_any_gown_name_finds_nothing_and_does_not_break(self):
        make_gown(1, "Wedding Gown One", "Wedding Gown")
        response = self.search("a" * 400)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["results"], [])


class SearchBarAndSortDrawerTests(TestCase):
    PAGES = (
        reverse("gowns:homepage"), reverse("gowns:collection_all"), reverse("gowns:collection_wedding"),
        reverse("gowns:collections"), reverse("gowns:about"), reverse("gowns:faqs"), SEARCH + "?q=x",
    )

    def test_view_all_is_a_live_link_to_the_results_page_on_every_page_with_the_search_bar(self):
        for page in self.PAGES:
            with self.subTest(page=page):
                html = self.client.get(page).content.decode()
                self.assertIn('<a href="/search/" id="search-view-all-btn"', html)
                self.assertIn('var SEARCH_RESULTS_URL = "/search/";', html)
                self.assertNotIn('id="search-view-all-btn" class="hidden px-8', html)     # the old disabled button
                self.assertNotIn("aria-disabled", html.split('id="search-view-all-btn"')[1].split(">")[0])

    def test_no_template_comment_text_leaks_onto_any_page(self):
        # A {# #} comment must sit on ONE line; a longer one is printed on the page as text.
        make_gown(1, "Wedding Gown One", "Wedding Gown")
        for page in (*self.PAGES, SEARCH + "?q=zzz", SEARCH + "?q=wed"):
            with self.subTest(page=page):
                html = self.client.get(page).content.decode()
                for leak in ("{#", "#}", "{% comment", "{% endcomment", "What the search bar"):
                    self.assertNotIn(leak, html)

    def test_enter_and_the_click_use_what_is_in_the_box_right_now(self):
        html = self.client.get(reverse("gowns:homepage")).content.decode()
        self.assertIn('e.key !== "Enter"', html)
        self.assertIn("encodeURIComponent", html)
        self.assertIn("viewAllBtn.href = searchResultsUrl(typed);", html)

    def test_the_best_match_row_is_in_the_sort_drawer_only_on_the_search_page(self):
        make_gown(1, "Wedding Gown One", "Wedding Gown")
        self.assertIn('data-sort-mode="bestMatch"', self.client.get(SEARCH, {"q": "wed"}).content.decode())
        for page in self.PAGES[:-1]:
            with self.subTest(page=page):
                self.assertNotIn('data-sort-mode="bestMatch"', self.client.get(page).content.decode())

    def test_best_match_is_the_fifth_option_after_the_four_that_were_there(self):
        make_gown(1, "Wedding Gown One", "Wedding Gown")
        html = self.client.get(SEARCH, {"q": "wed"}).content.decode()
        order = [html.index(f'data-sort-row data-sort-mode="{m}"') for m in ("az", "za", "priceAsc", "priceDesc", "bestMatch")]
        self.assertEqual(order, sorted(order))
        self.assertIn('data-sort-label="BEST MATCH">Best match</button>', html)
