"""Faster pages: one page visit asks each category question once (gowns/request_memo.py) and the All page / Search fetch the gown
list once -- with exactly the same results as before, nothing remembered between visits, and any category change seen at once."""
from decimal import Decimal
from unittest.mock import patch

from django.http import HttpResponse
from django.test import RequestFactory, TestCase

from gowns import views
from gowns.context_processors import all_categories
from gowns.models import CustomCategory, Gown, HiddenCategory, hidden_category_names
from gowns.request_memo import RequestMemoMiddleware


def inside_a_visit(work):
    """Runs work() the way a real page visit does: inside the middleware, so the memory is on."""
    result = {}

    def view(request):
        result["value"] = work()
        return HttpResponse("ok")

    RequestMemoMiddleware(view)(RequestFactory().get("/"))
    return result["value"]


def make_gown(n, name, category, status=Gown.Status.AVAILABLE, color="WH"):
    return Gown.objects.create(
        gown_id=f"MEMO-{n:04d}", name=name, category=category, color_name="White", color_code=color,
        size=Gown.Size.MEDIUM, rental_price=Decimal("1000") * n, status=status)


class RequestMemoTests(TestCase):
    def test_outside_a_page_visit_nothing_is_remembered(self):
        with self.assertNumQueries(4):                   # 2 questions per call, as before
            all_categories()
            all_categories()

    def test_inside_a_visit_each_category_question_is_asked_once(self):
        def many_calls():
            for _ in range(15):
                all_categories()
                hidden_category_names()
        with self.assertNumQueries(2):
            inside_a_visit(many_calls)

    def test_nothing_is_carried_over_from_one_visit_to_the_next(self):
        inside_a_visit(all_categories)
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown", audience="women")
        labels = inside_a_visit(lambda: [c["label"] for c in all_categories()])
        self.assertIn("Debut Gown", labels)

    def test_a_category_added_during_a_visit_is_seen_by_the_rest_of_that_visit(self):
        def add_then_read():
            before = [c["label"] for c in all_categories()]
            CustomCategory.objects.create(name="Debut Gown", slug="debut-gown", audience="women")
            return before, [c["label"] for c in all_categories()]
        before, after = inside_a_visit(add_then_read)
        self.assertNotIn("Debut Gown", before)
        self.assertIn("Debut Gown", after)

    def test_a_category_removed_or_brought_back_during_a_visit_is_seen_at_once(self):
        def hide_then_restore():
            seen = [("Suit" in [c["label"] for c in all_categories()])]
            hidden = HiddenCategory.objects.create(name="Suit")
            seen.append("Suit" in [c["label"] for c in all_categories()])
            hidden.delete()
            seen.append("Suit" in [c["label"] for c in all_categories()])
            return seen
        self.assertEqual(inside_a_visit(hide_then_restore), [True, False, True])

    def test_callers_get_their_own_copy_so_one_cannot_spoil_the_next(self):
        def mutate_then_read():
            first = all_categories()
            first.clear()
            names = hidden_category_names()
            names.add("Wedding Gown")
            return all_categories(), hidden_category_names()
        rows, hidden = inside_a_visit(mutate_then_read)
        self.assertTrue(rows)
        self.assertNotIn("Wedding Gown", hidden)


class OneGownQueryForManyCategoriesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown", audience="women")
        make_gown(1, "Wedding Gown One", "Wedding Gown", color="WH")
        make_gown(2, "Wedding Gown One", "Wedding Gown", color="IV")            # second unit, different color
        make_gown(3, "Alpha Wedding", "Wedding Gown", color="BK")
        make_gown(4, "Gone Wedding", "Wedding Gown", status=Gown.Status.OUT_OF_STOCK)
        make_gown(5, "Reserved Suit", "Suit", status=Gown.Status.RESERVED)
        make_gown(6, "Welcome Debut", "Debut Gown")

    def test_the_cards_are_exactly_what_each_category_page_shows(self):
        categories = all_categories()
        together = views._products_by_category(categories)
        for category in categories:
            with self.subTest(category=category["label"]):
                self.assertEqual(together[category["key"]], views._products_for_category(category["key"]))

    def test_the_all_page_and_search_ask_the_database_only_a_few_times(self):
        self.client.get("/collections/all/")          # first visit warms up the site settings row
        with self.assertNumQueries(5):
            self.assertEqual(self.client.get("/collections/all/").status_code, 200)
        # The search speed limit keeps its own small counter row (tested in accounts/test_rate_limit.py); left out here so this
        # counts only the page's own questions.
        with patch("gowns.views.rate_limit.allowed", return_value=True):
            with self.assertNumQueries(5):
                self.assertEqual(self.client.get("/search/", {"q": "we"}).status_code, 200)

    def test_a_category_page_asks_the_database_only_a_few_times(self):
        self.client.get("/collections/wedding/")
        with self.assertNumQueries(5):
            self.assertEqual(self.client.get("/collections/wedding/").status_code, 200)
