"""The selection page's "Continue Shopping": it goes back to the category of the gown added last, and to the All page when there is
no such category. It used to be a dead link (href="#").

Three layers: the server's list of category addresses (continue_shopping_links), the page that carries it, and the page's own small
script, taken straight out of templates/selection.html and run under Node, so the test can never drift from what the page really does."""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from gowns.context_processors import _CATEGORIES, all_categories, category_url, continue_shopping_links
from gowns.models import CustomCategory, HiddenCategory

SELECTION_TEMPLATE = Path(settings.BASE_DIR) / "templates" / "selection.html"
ALL_URL = "/collections/all/"


class ContinueShoppingLinksTests(TestCase):
    def test_every_category_the_shop_has_leads_to_its_own_page(self):
        links = continue_shopping_links()
        for row in _CATEGORIES:
            self.assertEqual(links[row["key"]], reverse(f"gowns:{row['url_name']}"), row["key"])
        self.assertEqual(links["guest-gown"], "/collections/guest-gown/")
        self.assertEqual(links["suit"], "/collections/suit/")

    def test_a_category_the_owner_added_is_included(self):
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        self.assertEqual(continue_shopping_links()["debut-gown"], "/collections/debut-gown/")

    def test_a_category_the_owner_removed_is_left_out_so_the_page_uses_the_all_page(self):
        HiddenCategory.objects.create(name="Suit")
        links = continue_shopping_links()
        self.assertNotIn("suit", links)
        self.assertIn("barong", links)

    def test_a_bag_saved_before_ball_gown_became_evening_gown_still_lands_on_the_right_page(self):
        links = continue_shopping_links()
        self.assertEqual(links["ball-gown"], links["evening-gown"])
        self.assertEqual(links["ball-gown"], "/collections/evening-gown/")

    def test_the_old_ball_gown_key_is_dropped_when_evening_gown_itself_is_removed(self):
        HiddenCategory.objects.create(name="Evening Gown")
        self.assertNotIn("ball-gown", continue_shopping_links())

    def test_it_matches_the_category_list_the_menus_use(self):
        links = continue_shopping_links()
        for row in all_categories():
            self.assertEqual(links[row["key"]], category_url(row))


class SelectionPageTests(TestCase):
    def html(self):
        response = self.client.get(reverse("gowns:selection"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_link_is_a_real_address_even_before_the_script_runs(self):
        html = self.html()
        link = re.search(r'<a id="continue-shopping-link"[^>]*>', html)
        self.assertIsNotNone(link)
        self.assertIn(f'href="{ALL_URL}"', link.group(0))
        self.assertNotIn('href="#"', link.group(0))

    def test_the_page_carries_every_category_address_as_data(self):
        html = self.html()
        match = re.search(r'<script id="continue-shopping-links" type="application/json">(.*?)</script>', html, re.S)
        self.assertIsNotNone(match)
        self.assertEqual(json.loads(match.group(1)), continue_shopping_links())

    def test_a_removed_category_is_not_offered_on_the_page(self):
        HiddenCategory.objects.create(name="Barong")
        html = self.html()
        data = json.loads(re.search(r'id="continue-shopping-links" type="application/json">(.*?)</script>', html, re.S).group(1))
        self.assertNotIn("barong", data)
        self.assertIn("suit", data)

    def test_a_signed_in_customer_gets_the_same_page(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(username="cs-customer", password="pw-12345-abc")
        self.client.force_login(user)
        self.assertIn('id="continue-shopping-link"', self.html())


NODE_DRIVER = r"""
const vm = require('vm'), fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const links = JSON.parse(process.argv[2]);

function run(cart, { withData = true, withLink = true } = {}) {
  const link = { href: '(untouched)' };
  const ctx = {
    document: {
      getElementById(id) {
        if (id === 'continue-shopping-links') return withData ? { textContent: JSON.stringify(links) } : null;
        if (id === 'continue-shopping-link') return withLink ? link : null;
        return null;
      },
    },
  };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  vm.runInContext('updateContinueShoppingLink(__cart)', Object.assign(ctx, { __cart: cart }));
  return link.href;
}

const row = (collection) => ({ id: 'x', name: 'Gown', collection });
const out = {};
out.empty = run([]);
out.notAnArray = [run(null), run(undefined), run('nope'), run({})];
out.one = run([row('guest-gown')]);
out.lastWins = run([row('suit'), row('barong'), row('guest-gown')]);
out.afterRemovingLast = run([row('suit'), row('barong')]);
out.noCategory = [run([row('suit'), row('')]), run([row('suit'), { id: 'x', name: 'Gown' }]), run([row('suit'), row(null)])];
out.unknown = run([row('suit'), row('no-such-category')]);
out.messy = [run([row('  Guest-Gown ')]), run([row('GUEST-GOWN')])];
out.protoKeys = [run([row('constructor')]), run([row('__proto__')]), run([row('toString')]), run([row('hasOwnProperty')])];
out.oldBallGown = run([row('ball-gown')]);
out.numeric = run([row(5)]);
out.noDataOnPage = run([row('suit')], { withData: false });
out.noLinkOnPage = run([row('suit')], { withLink: false });
console.log(JSON.stringify(out));
"""


def _script_part():
    """The part of the page's script that decides where Continue Shopping goes: from its constant to the start of renderCart()."""
    text = SELECTION_TEMPLATE.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index("const ALL_COLLECTIONS_URL")
    end = text.index("function renderCart()")
    part = text[start:end]
    return part.replace("{% url 'gowns:collection_all' %}", ALL_URL)


class ContinueShoppingScriptTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.result = None
        node = shutil.which("node")
        if not node:
            return
        links = {row["key"]: f"/collections/{row['key']}/" for row in _CATEGORIES}
        links["ball-gown"] = links["evening-gown"]
        with tempfile.TemporaryDirectory() as folder:
            scratch = Path(folder) / "continue_shopping_part.js"
            scratch.write_text(_script_part(), encoding="utf-8")
            done = subprocess.run(
                [node, "-e", NODE_DRIVER, str(scratch), json.dumps(links)],
                capture_output=True, text=True, timeout=60,
            )
        if done.returncode != 0:
            raise AssertionError(done.stderr or done.stdout)
        cls.result = json.loads(done.stdout)

    def setUp(self):
        if self.result is None:
            self.skipTest("Node is not installed")

    def test_the_part_the_test_runs_is_really_in_the_page(self):
        part = _script_part()
        self.assertIn("function continueShoppingUrl(cart)", part)
        self.assertIn("function updateContinueShoppingLink(cart)", part)

    def test_an_empty_bag_goes_to_the_all_page(self):
        self.assertEqual(self.result["empty"], ALL_URL)

    def test_anything_that_is_not_a_bag_goes_to_the_all_page(self):
        self.assertEqual(self.result["notAnArray"], [ALL_URL] * 4)

    def test_one_gown_goes_to_its_category(self):
        self.assertEqual(self.result["one"], "/collections/guest-gown/")

    def test_the_gown_added_last_decides(self):
        self.assertEqual(self.result["lastWins"], "/collections/guest-gown/")

    def test_removing_the_last_gown_moves_the_link_to_the_one_before(self):
        self.assertEqual(self.result["afterRemovingLast"], "/collections/barong/")

    def test_a_last_gown_with_no_category_goes_to_the_all_page(self):
        self.assertEqual(self.result["noCategory"], [ALL_URL] * 3)

    def test_a_category_the_shop_no_longer_has_goes_to_the_all_page(self):
        self.assertEqual(self.result["unknown"], ALL_URL)

    def test_capital_letters_and_spaces_in_the_saved_key_do_not_matter(self):
        self.assertEqual(self.result["messy"], ["/collections/guest-gown/"] * 2)

    def test_object_property_names_never_count_as_a_category(self):
        self.assertEqual(self.result["protoKeys"], [ALL_URL] * 4)

    def test_a_bag_saved_before_the_ball_gown_rename_goes_to_evening_gown(self):
        self.assertEqual(self.result["oldBallGown"], "/collections/evening-gown/")

    def test_a_non_text_category_is_handled(self):
        self.assertEqual(self.result["numeric"], ALL_URL)

    def test_a_page_without_the_data_still_gets_the_all_page(self):
        self.assertEqual(self.result["noDataOnPage"], ALL_URL)

    def test_a_page_without_the_link_does_not_break(self):
        self.assertEqual(self.result["noLinkOnPage"], "(untouched)")
