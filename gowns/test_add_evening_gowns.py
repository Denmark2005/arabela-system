"""python manage.py add_evening_gowns: the owner's 25 Evening Gowns -- exactly 25 gowns (gown IDs 001 to 025), ONE physical gown per design, all Large, at the
owner's price (1,500 or 3,500), named colour first ("Black Evening Gown 3"). The list's numbers are PHOTO numbers (list item 25 uses "Evening Gown 92.jpg").
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
from gowns.management.commands.add_evening_gowns import EVENING_GOWNS, PRICES_BY_NAME, SIZES_BY_NAME
from gowns.models import Gown
from gowns.test_add_barongs import SAVE

CATEGORY = Gown.Category.EVENING_GOWN

# The owner's list, typed in separately from the command: (photo number, colour, price) for items 1-25.
LIST = [
    (1, "Burgundy", 3500), (2, "Black", 1500), (3, "Rust Brown", 1500), (4, "Burgundy", 3500), (5, "Navy Blue", 3500), (6, "White", 1500),
    (7, "Maroon", 3500), (8, "White", 1500), (9, "White", 1500), (10, "Silver", 3500), (21, "Rose Gold", 3500), (22, "Champagne", 3500),
    (23, "Black", 1500), (24, "Black", 3500), (25, "White", 1500), (26, "Gold", 1500), (27, "Champagne", 1500), (28, "Dusty Rose", 1500),
    (71, "Emerald Green", 3500), (72, "Burgundy", 3500), (82, "Black", 3500), (87, "Blush Pink", 1500), (89, "Dusty Rose", 1500),
    (91, "Cream", 3500), (92, "Burgundy", 3500),
]
FIELD_COLOUR = {"Navy Blue": "Navy", "Emerald Green": "Emerald", "Blush Pink": "Blush"}


def make_photos(folder):
    for _name, photo, *_ in EVENING_GOWNS:
        buffer = io.BytesIO()
        Image.new("RGB", (40, 40), (200, 180, 150)).save(buffer, "JPEG")
        (Path(folder) / photo).write_bytes(buffer.getvalue())


class TheListTests(SimpleTestCase):
    def test_twenty_five_gowns_each_with_the_photo_number_the_owner_gave(self):
        self.assertEqual(len(EVENING_GOWNS), 25)
        self.assertEqual([photo for _n, photo, *_ in EVENING_GOWNS], [f"Evening Gown {n:02d}.jpg" for n, _c, _p in LIST])

    def test_list_item_25_is_photo_92(self):
        self.assertEqual(EVENING_GOWNS[24][1], "Evening Gown 92.jpg")
        self.assertEqual(EVENING_GOWNS[10][1], "Evening Gown 21.jpg")                    # item 11 jumps from photo 10 to photo 21

    def test_every_colour_and_price_is_exactly_the_owners(self):
        for index, (name, _photo, field, price) in enumerate(EVENING_GOWNS):
            _number, colour, want_price = LIST[index]
            self.assertEqual(field, FIELD_COLOUR.get(colour, colour), f"item {index + 1}")
            self.assertEqual(int(price), want_price, f"item {index + 1}")
            self.assertTrue(name.startswith(colour + " Evening Gown "), name)

    def test_every_gown_is_one_large(self):
        for name, sizes in SIZES_BY_NAME.items():
            self.assertEqual(tuple(str(s) for s in sizes), ("Large",), name)
        self.assertEqual(Counter(int(p) for p in PRICES_BY_NAME.values()), {3500: 13, 1500: 12})

    def test_every_name_differs_and_is_numbered_per_colour_in_list_order_not_by_photo(self):
        names = [n for n, *_ in EVENING_GOWNS]
        self.assertEqual(len({n.lower() for n in names}), 25)
        by_base = {}
        for name in names:
            base, number = re.match(r"(.+) (\d+)$", name).groups()
            by_base.setdefault(base, []).append(int(number))
        for base, numbers in by_base.items():
            self.assertEqual(numbers, list(range(1, len(numbers) + 1)), base)
        self.assertEqual(len(by_base["Burgundy Evening Gown"]), 4)
        self.assertEqual(len(by_base["Black Evening Gown"]), 4)
        self.assertEqual(by_base["Cream Evening Gown"], [1])                            # a lone colour still gets its 1
        self.assertEqual(EVENING_GOWNS[24][0], "Burgundy Evening Gown 4")


class AddEveningGownsTests(TestCase):
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
        call_command("add_evening_gowns", f"--photos={self.folder.name}", *extra, stdout=out, stderr=err)
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
        for index, (name, photo, *_rest) in enumerate(EVENING_GOWNS, start=1):
            gown = self.gowns().get(name=name)
            self.assertTrue(gown.gown_id.endswith(f"-{index:03d}"), name)
            self.assertTrue(gown.photo_url.endswith(photo.lower().replace(" ", "-")), name)
        self.assertEqual(self.gowns().get(gown_id__endswith="-025").photo_url.rsplit("/", 1)[1], "evening-gown-92.jpg")
        self.assertEqual(len(self.uploads), 25)

    def test_only_the_listed_photos_are_used(self):
        for n in (11, 12, 29, 93, 98):
            (Path(self.folder.name) / f"Evening Gown {n}.jpg").write_bytes(b"not used")
        self.run_command()
        self.assertFalse(any(re.search(r"evening-gown-(11|12|29|93|98)\.", u) for u in self.uploads))
        self.assertEqual(self.gowns().count(), 25)

    def test_colour_names_and_codes(self):
        self.run_command()
        by_colour = {}
        for colour, code in self.gowns().values_list("color_name", "color_code"):
            by_colour.setdefault(colour, set()).add(code)
        self.assertTrue(all(len(codes) == 1 for codes in by_colour.values()), by_colour)       # one colour, one code
        self.assertEqual(len({next(iter(c)) for c in by_colour.values()}), len(by_colour))       # no code shared by two colours
        presets = {"Burgundy": "BD", "Black": "BK", "Navy": "NV", "White": "WH", "Maroon": "MR", "Silver": "SV", "Rose Gold": "RG", "Champagne": "CH",
                   "Gold": "GD", "Emerald": "EM", "Blush": "BL"}
        for colour, code in presets.items():
            self.assertEqual(by_colour[colour], {code}, colour)
        for colour in ("Rust Brown", "Dusty Rose", "Cream"):                                    # no preset: a free 2-letter code each
            self.assertRegex(next(iter(by_colour[colour])), r"^[A-Z]{2}$", colour)
        self.assertEqual(len(by_colour), 14)

    def test_run_again_adds_nothing_and_a_dry_run_changes_nothing(self):
        self.run_command()
        out, _e = self.run_command()
        self.assertIn("Added 0 gown(s); 25 size(s) were already there.", out)
        self.assertEqual(self.gowns().count(), 25)
        self.gowns().filter(name="Cream Evening Gown 1").delete()
        out, _e = self.run_command("--dry-run")
        self.assertEqual(self.gowns().count(), 24)
        self.assertIn("Would add 1 gown(s); 24 size(s) were already there.", out)

    def test_a_missing_photo_stops_only_that_gown(self):
        (Path(self.folder.name) / "Evening Gown 92.jpg").unlink()
        with self.assertRaises(CommandError) as raised:
            self.run_command()
        self.assertIn("1 Evening Gown(s) could not be added", str(raised.exception))
        self.assertEqual(self.gowns().count(), 24)
        self.assertFalse(self.gowns().filter(name="Burgundy Evening Gown 4").exists())

    def test_the_customer_site_lists_all_25_with_only_large_clickable(self):
        self.run_command()
        self.assertFalse(Gown.objects.exclude(category=CATEGORY).exists())
        url = next(category_url(row) for row in all_categories() if row["label"] == "Evening Gown")
        first = self.client.get(url).content.decode()
        pages = int(re.search(r"Page 1 of (\d+)", first).group(1)) if "Page 1 of" in first else 1
        seen = []
        for page in range(1, pages + 1):
            html = first if page == 1 else self.client.get(url, {"page": page}).content.decode()
            main = html[html.index("<main"):html.index("</main>")]
            seen += [unescape(t) for t in re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', main)]
        self.assertEqual(len(seen), 25)
        self.assertEqual(set(seen), {name for name, *_ in EVENING_GOWNS})
        slug = self.gowns().get(name="Burgundy Evening Gown 1").slug                          # Large, 3,500
        html = self.client.get(f"{url}products/{slug}/").content.decode()
        self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', html), ["L"])
        self.assertEqual(re.findall(r'data-size-not-offered="([A-Z]+)"', html), ["S", "M", "XL", "XXL"])
        self.assertIn("₱3,500", html)
        slug = self.gowns().get(name="Black Evening Gown 1").slug                             # Large, 1,500
        self.assertIn("₱1,500", self.client.get(f"{url}products/{slug}/").content.decode())

    def test_search_finds_a_colour(self):
        self.run_command()
        html = self.client.get("/search/", {"q": "cream"}).content.decode()
        found = {unescape(t) for t in re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', html[html.index("<main"):html.index("</main>")])}
        self.assertEqual(found, {"Cream Evening Gown 1"})
