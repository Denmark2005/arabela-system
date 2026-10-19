"""python manage.py add_long_gowns: the owner's 25 Long Gowns -- exactly 25 gowns (gown IDs 001 to 025), ONE physical gown per design, all Large, at the owner's
price (1,000 or 2,500), named colour first ("Black Long Gown 3"). The list's numbers are PHOTO numbers (31-40, 42-56: list item 11 uses "Long Gown 42.jpg").
No extra sizes and no extra quantity. The upload is stubbed so nothing touches Cloudinary or media."""
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
from gowns.management.commands.add_long_gowns import LONG_GOWNS, PRICES_BY_NAME, SIZES_BY_NAME
from gowns.models import Gown
from gowns.test_add_barongs import SAVE

CATEGORY = Gown.Category.LONG_GOWN

# The owner's list, typed in separately from the command: (photo number, colour, price) for items 1-25.
LIST = [
    (31, "Rose Gold", 2500), (32, "Burgundy", 2500), (33, "White", 1000), (34, "Black", 2500), (35, "Burgundy", 2500), (36, "Silver", 2500),
    (37, "Rose Gold", 2500), (38, "Navy Blue", 2500), (39, "Black", 2500), (40, "Champagne", 2500), (42, "Silver", 2500),
    (43, "Black and White", 2500), (44, "Champagne", 2500), (45, "Black", 2500), (46, "Rose Gold", 2500), (47, "Black", 2500),
    (48, "Silver", 2500), (49, "Rose Gold", 2500), (50, "Dusty Pink", 2500), (51, "White", 1000), (52, "Rose Gold", 2500),
    (53, "Black", 2500), (54, "Blue", 2500), (55, "White", 2500), (56, "Silver", 2500),
]
FIELD_COLOUR = {"Navy Blue": "Navy"}


def make_photos(folder):
    for _name, photo, *_ in LONG_GOWNS:
        buffer = io.BytesIO()
        Image.new("RGB", (40, 40), (200, 180, 150)).save(buffer, "JPEG")
        (Path(folder) / photo).write_bytes(buffer.getvalue())


class TheListTests(SimpleTestCase):
    def test_twenty_five_gowns_each_with_the_photo_number_the_owner_gave(self):
        self.assertEqual(len(LONG_GOWNS), 25)
        self.assertEqual([photo for _n, photo, *_ in LONG_GOWNS], [f"Long Gown {n:02d}.jpg" for n, _c, _p in LIST])
        self.assertNotIn("Long Gown 41.jpg", [photo for _n, photo, *_ in LONG_GOWNS])           # photo 41 is not on the list
        self.assertEqual(LONG_GOWNS[10][1], "Long Gown 42.jpg")

    def test_every_colour_and_price_is_exactly_the_owners(self):
        for index, (name, _photo, field, price) in enumerate(LONG_GOWNS):
            _number, colour, want_price = LIST[index]
            self.assertEqual(field, FIELD_COLOUR.get(colour, colour), f"item {index + 1}")
            self.assertEqual(int(price), want_price, f"item {index + 1}")
            self.assertTrue(name.startswith(colour + " Long Gown "), name)

    def test_every_gown_is_one_large_and_the_prices_split_as_listed(self):
        for name, sizes in SIZES_BY_NAME.items():
            self.assertEqual(tuple(str(s) for s in sizes), ("Large",), name)
        self.assertEqual(Counter(int(p) for p in PRICES_BY_NAME.values()), {2500: 23, 1000: 2})

    def test_every_name_differs_and_is_numbered_per_colour_in_list_order_not_by_photo(self):
        names = [n for n, *_ in LONG_GOWNS]
        self.assertEqual(len({n.lower() for n in names}), 25)
        by_base = {}
        for name in names:
            base, number = re.match(r"(.+) (\d+)$", name).groups()
            by_base.setdefault(base, []).append(int(number))
        for base, numbers in by_base.items():
            self.assertEqual(numbers, list(range(1, len(numbers) + 1)), base)
        self.assertEqual(len(by_base["Rose Gold Long Gown"]), 5)
        self.assertEqual(len(by_base["Black Long Gown"]), 5)
        self.assertEqual(by_base["Dusty Pink Long Gown"], [1])                           # a lone colour still gets its 1


class AddLongGownsTests(TestCase):
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
        call_command("add_long_gowns", f"--photos={self.folder.name}", *extra, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def gowns(self):
        return Gown.objects.filter(category=CATEGORY)

    def test_adds_exactly_25_gowns_all_large_at_the_owners_price(self):
        out, err = self.run_command()
        self.assertEqual((self.gowns().count(), err), (25, ""))
        self.assertIn("Added 25 gown(s); 0 size(s) were already there.", out)
        self.assertEqual(set(self.gowns().values_list("size", flat=True)), {"Large"})
        for name in SIZES_BY_NAME:
            got = list(self.gowns().filter(name=name).values_list("rental_price", flat=True))
            self.assertEqual([int(p) for p in got], [int(PRICES_BY_NAME[name])], name)
        self.assertEqual(self.gowns().values("name").distinct().count(), 25)

    def test_gown_ids_run_001_to_025_and_each_gown_has_the_photo_of_its_listed_number(self):
        self.run_command()
        self.assertEqual(sorted(int(g.gown_id.rsplit("-", 1)[1]) for g in self.gowns()), list(range(1, 26)))
        for index, (name, photo, *_rest) in enumerate(LONG_GOWNS, start=1):
            gown = self.gowns().get(name=name)
            self.assertTrue(gown.gown_id.endswith(f"-{index:03d}"), name)
            self.assertTrue(gown.photo_url.endswith(photo.lower().replace(" ", "-")), name)
        self.assertEqual(len(self.uploads), 25)

    def test_only_the_listed_photos_are_used(self):
        for n in (41, 57, 94):
            (Path(self.folder.name) / f"Long Gown {n}.jpg").write_bytes(b"not used")
        self.run_command()
        self.assertFalse(any(re.search(r"long-gown-(41|57|94)\.", u) for u in self.uploads))
        self.assertEqual(self.gowns().count(), 25)

    def test_colour_names_and_codes(self):
        self.run_command()
        by_colour = {}
        for colour, code in self.gowns().values_list("color_name", "color_code"):
            by_colour.setdefault(colour, set()).add(code)
        self.assertTrue(all(len(codes) == 1 for codes in by_colour.values()), by_colour)       # one colour, one code
        self.assertEqual(len({next(iter(c)) for c in by_colour.values()}), len(by_colour))       # no code shared by two colours
        presets = {"Rose Gold": "RG", "Burgundy": "BD", "White": "WH", "Black": "BK", "Silver": "SV", "Navy": "NV", "Champagne": "CH", "Blue": "BU"}
        for colour, code in presets.items():
            self.assertEqual(by_colour[colour], {code}, colour)
        for colour in ("Black and White", "Dusty Pink"):                                        # no preset: a free 2-letter code each
            self.assertRegex(next(iter(by_colour[colour])), r"^[A-Z]{2}$", colour)
        self.assertEqual(len(by_colour), 10)

    def test_the_shops_existing_black_and_white_code_is_reused(self):
        Gown.objects.create(gown_id="Filipiniana-BW-001", name="Black and White Filipiniana 1", category=Gown.Category.FILIPINIANA, color_name="Black and White",
                            color_code="BW", size="Extra Large", rental_price=3000)
        self.run_command()
        self.assertEqual(set(self.gowns().filter(color_name="Black and White").values_list("color_code", flat=True)), {"BW"})

    def test_run_again_adds_nothing_and_a_dry_run_changes_nothing(self):
        self.run_command()
        out, _e = self.run_command()
        self.assertIn("Added 0 gown(s); 25 size(s) were already there.", out)
        self.assertEqual(self.gowns().count(), 25)
        self.gowns().filter(name="Dusty Pink Long Gown 1").delete()
        out, _e = self.run_command("--dry-run")
        self.assertEqual(self.gowns().count(), 24)
        self.assertIn("Would add 1 gown(s); 24 size(s) were already there.", out)

    def test_a_missing_photo_stops_only_that_gown(self):
        (Path(self.folder.name) / "Long Gown 56.jpg").unlink()
        with self.assertRaises(CommandError) as raised:
            self.run_command()
        self.assertIn("1 Long Gown(s) could not be added", str(raised.exception))
        self.assertEqual(self.gowns().count(), 24)
        self.assertFalse(self.gowns().filter(name="Silver Long Gown 4").exists())

    def test_the_customer_site_lists_all_25_with_only_large_clickable(self):
        self.run_command()
        self.assertFalse(Gown.objects.exclude(category=CATEGORY).exists())
        url = next(category_url(row) for row in all_categories() if row["label"] == "Long Gown")
        first = self.client.get(url).content.decode()
        pages = int(re.search(r"Page 1 of (\d+)", first).group(1)) if "Page 1 of" in first else 1
        seen = []
        for page in range(1, pages + 1):
            html = first if page == 1 else self.client.get(url, {"page": page}).content.decode()
            main = html[html.index("<main"):html.index("</main>")]
            seen += [unescape(t) for t in re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', main)]
        self.assertEqual(len(seen), 25)
        self.assertEqual(set(seen), {name for name, *_ in LONG_GOWNS})
        slug = self.gowns().get(name="Rose Gold Long Gown 1").slug                            # Large, 2,500
        html = self.client.get(f"{url}products/{slug}/").content.decode()
        self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', html), ["L"])
        self.assertEqual(re.findall(r'data-size-not-offered="([A-Z]+)"', html), ["S", "M", "XL", "XXL"])
        self.assertIn("₱2,500", html)
        slug = self.gowns().get(name="White Long Gown 1").slug                                # Large, 1,000
        self.assertIn("₱1,000", self.client.get(f"{url}products/{slug}/").content.decode())

    def test_search_finds_a_colour(self):
        self.run_command()
        html = self.client.get("/search/", {"q": "dusty"}).content.decode()
        found = {unescape(t) for t in re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', html[html.index("<main"):html.index("</main>")])}
        self.assertEqual(found, {"Dusty Pink Long Gown 1"})
