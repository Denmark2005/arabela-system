"""python manage.py add_mother_gowns: the owner's 30 Mother Gowns -- exactly 30 gowns (gown IDs 001 to 030), ONE physical gown per design in the owner's own size
(items 1-15 Large, 16-30 XL) and price (1,500 or 3,500), named colour first ("Gold Mother Gown 3"). No extra sizes and no extra quantity. The upload is
stubbed so nothing touches Cloudinary or media."""
import io
import re
import tempfile
from collections import Counter
from html import unescape
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from PIL import Image

from gowns.context_processors import all_categories, category_url
from gowns.management.commands.add_mother_gowns import MOTHER_GOWNS, PRICES_BY_NAME, SIZES_BY_NAME
from gowns.models import Gown
from gowns.test_add_barongs import SAVE

CATEGORY = Gown.Category.MOTHER_GOWN

# The owner's list, typed in separately from the command: (colour, price, size) for items 1-30.
LIST = [
    ("Navy Blue", 3500, "Large"), ("Gold", 3500, "Large"), ("Rose Gold", 3500, "Large"), ("Blue", 3500, "Large"), ("Silver", 3500, "Large"),
    ("Pink", 3500, "Large"), ("Champagne", 3500, "Large"), ("Beige", 3500, "Large"), ("Rose Gold", 3500, "Large"), ("Gold", 3500, "Large"),
    ("Pink", 1500, "Large"), ("Silver", 1500, "Large"), ("Beige", 1500, "Large"), ("Gold", 3500, "Large"), ("Gold", 3500, "Large"),
    ("Silver", 3500, "XL"), ("Pink", 1500, "XL"), ("Beige", 3500, "XL"), ("Champagne", 3500, "XL"), ("Gold", 3500, "XL"),
    ("Silver", 3500, "XL"), ("Purple", 3500, "XL"), ("Blue", 3500, "XL"), ("Gold", 3500, "XL"), ("Champagne", 1500, "XL"),
    ("Pink", 3500, "XL"), ("Emerald Green", 3500, "XL"), ("Emerald Green", 3500, "XL"), ("Emerald Green", 3500, "XL"), ("Blue", 3500, "XL"),
]
SIZE_NAME = {"Large": "Large", "XL": "Extra Large"}
FIELD_COLOUR = {"Navy Blue": "Navy", "Emerald Green": "Emerald"}


def make_photos(folder):
    for _name, photo, *_ in MOTHER_GOWNS:
        buffer = io.BytesIO()
        Image.new("RGB", (40, 40), (200, 180, 150)).save(buffer, "JPEG")
        (Path(folder) / photo).write_bytes(buffer.getvalue())


class TheListTests(SimpleTestCase):
    def test_thirty_gowns_with_the_photo_of_their_own_item(self):
        self.assertEqual(len(MOTHER_GOWNS), 30)
        self.assertEqual([photo for _n, photo, *_ in MOTHER_GOWNS], [f"Mother Gown {n:02d}.jpg" for n in range(1, 31)])

    def test_every_colour_size_and_price_is_exactly_the_owners(self):
        for index, (name, _photo, field, _size, price) in enumerate(MOTHER_GOWNS):
            colour, want_price, want_size = LIST[index]
            self.assertEqual(field, FIELD_COLOUR.get(colour, colour), f"item {index + 1}")
            self.assertEqual(int(price), want_price, f"item {index + 1}")
            self.assertEqual(str(SIZES_BY_NAME[name][0]), SIZE_NAME[want_size], f"item {index + 1}")
            self.assertTrue(name.startswith(colour + " Mother Gown "), name)

    def test_every_gown_has_exactly_one_size_so_there_are_30_gowns_in_all(self):
        for name, sizes in SIZES_BY_NAME.items():
            self.assertEqual(len(sizes), 1, name)
        self.assertEqual(Counter(str(s[0]) for s in SIZES_BY_NAME.values()), {"Large": 15, "Extra Large": 15})
        self.assertEqual(Counter(int(p) for p in PRICES_BY_NAME.values()), {3500: 25, 1500: 5})

    def test_every_name_differs_and_is_numbered_per_colour_in_list_order(self):
        names = [n for n, *_ in MOTHER_GOWNS]
        self.assertEqual(len({n.lower() for n in names}), 30)
        by_base = {}
        for name in names:
            base, number = re.match(r"(.+) (\d+)$", name).groups()
            by_base.setdefault(base, []).append(int(number))
        for base, numbers in by_base.items():
            self.assertEqual(numbers, list(range(1, len(numbers) + 1)), base)
        self.assertEqual(len(by_base["Gold Mother Gown"]), 6)
        self.assertEqual(by_base["Navy Blue Mother Gown"], [1])                            # a lone colour still gets its 1


class AddMotherGownsTests(TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        make_photos(self.folder.name)
        self.uploads = []

        def fake_save(photo_file):
            self.uploads.append(photo_file.name)
            return f"https://photos.test/gown_photos/{photo_file.name}"
        patcher = mock.patch(SAVE, side_effect=fake_save)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_command(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        call_command("add_mother_gowns", f"--photos={self.folder.name}", *extra, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def gowns(self):
        return Gown.objects.filter(category=CATEGORY)

    def test_adds_exactly_30_gowns_one_per_design_with_the_owners_size_and_price(self):
        out, err = self.run_command()
        self.assertEqual((self.gowns().count(), err), (30, ""))
        self.assertIn("Added 30 gown(s); 0 size(s) were already there.", out)
        for name, sizes in SIZES_BY_NAME.items():
            got = list(self.gowns().filter(name=name).values_list("size", "rental_price"))
            self.assertEqual(len(got), 1, name)                                              # exactly one gown per design
            self.assertEqual(got[0][0], str(sizes[0]), name)
            self.assertEqual(int(got[0][1]), int(PRICES_BY_NAME[name]), name)
        self.assertEqual(self.gowns().values("name").distinct().count(), 30)

    def test_gown_ids_run_001_to_030_in_the_owners_order_with_one_photo_each(self):
        self.run_command()
        self.assertEqual(sorted(int(g.gown_id.rsplit("-", 1)[1]) for g in self.gowns()), list(range(1, 31)))
        for index, (name, photo, *_rest) in enumerate(MOTHER_GOWNS, start=1):
            gown = self.gowns().get(name=name)
            self.assertTrue(gown.gown_id.endswith(f"-{index:03d}"), name)
            self.assertTrue(gown.photo_url.endswith(photo.lower().replace(" ", "-")), name)
        self.assertEqual(len(self.uploads), 30)

    def test_only_photos_01_to_30_are_used(self):
        for n in range(31, 39):
            (Path(self.folder.name) / f"Mother Gown {n}.jpg").write_bytes(b"not used")
        self.run_command()
        self.assertFalse(any(re.search(r"mother-gown-(3[1-8])", u) for u in self.uploads))
        self.assertEqual(self.gowns().count(), 30)

    def test_colour_names_and_codes_are_all_presets(self):
        self.run_command()
        by_colour = {}
        for colour, code in self.gowns().values_list("color_name", "color_code"):
            by_colour.setdefault(colour, set()).add(code)
        presets = {"Navy": "NV", "Gold": "GD", "Rose Gold": "RG", "Blue": "BU", "Silver": "SV", "Pink": "PK", "Champagne": "CH", "Beige": "BG",
                   "Purple": "PU", "Emerald": "EM"}
        self.assertEqual({c: next(iter(codes)) for c, codes in by_colour.items()}, presets)
        self.assertTrue(all(len(codes) == 1 for codes in by_colour.values()))

    def test_run_again_adds_nothing_and_a_dry_run_changes_nothing(self):
        self.run_command()
        out, _e = self.run_command()
        self.assertIn("Added 0 gown(s); 30 size(s) were already there.", out)
        self.assertEqual(self.gowns().count(), 30)                                         # running it again never adds quantity
        self.gowns().filter(name="Purple Mother Gown 1").delete()
        out, _e = self.run_command("--dry-run")
        self.assertEqual(self.gowns().count(), 29)
        self.assertIn("Would add 1 gown(s); 29 size(s) were already there.", out)

    def test_a_missing_photo_stops_only_that_gown(self):
        (Path(self.folder.name) / "Mother Gown 09.jpg").unlink()
        with self.assertRaises(CommandError) as raised:
            self.run_command()
        self.assertIn("1 Mother Gown(s) could not be added", str(raised.exception))
        self.assertEqual(self.gowns().count(), 29)
        self.assertFalse(self.gowns().filter(name="Rose Gold Mother Gown 2").exists())

    def test_the_customer_site_lists_all_30_with_only_the_owners_size_clickable(self):
        self.run_command()
        self.assertFalse(Gown.objects.exclude(category=CATEGORY).exists())
        url = next(category_url(row) for row in all_categories() if row["label"] == "Mother Gown")
        first = self.client.get(url).content.decode()
        pages = int(re.search(r"Page 1 of (\d+)", first).group(1))
        seen = []
        for page in range(1, pages + 1):
            html = first if page == 1 else self.client.get(url, {"page": page}).content.decode()
            main = html[html.index("<main"):html.index("</main>")]
            seen += [unescape(t) for t in re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', main)]
        self.assertEqual(len(seen), 30)
        self.assertEqual(set(seen), {name for name, *_ in MOTHER_GOWNS})
        slug = self.gowns().get(name="Navy Blue Mother Gown 1").slug                         # Large, 3,500
        html = self.client.get(f"{url}products/{slug}/").content.decode()
        self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', html), ["L"])
        self.assertEqual(re.findall(r'data-size-not-offered="([A-Z]+)"', html), ["S", "M", "XL", "XXL"])
        self.assertIn("₱3,500", html)
        slug = self.gowns().get(name="Pink Mother Gown 3").slug                              # XL, 1,500
        html = self.client.get(f"{url}products/{slug}/").content.decode()
        self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', html), ["XL"])
        self.assertIn("₱1,500", html)

    def test_search_finds_a_colour(self):
        self.run_command()
        html = self.client.get("/search/", {"q": "purple"}).content.decode()
        found = {unescape(t) for t in re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', html[html.index("<main"):html.index("</main>")])}
        self.assertEqual(found, {"Purple Mother Gown 1"})
