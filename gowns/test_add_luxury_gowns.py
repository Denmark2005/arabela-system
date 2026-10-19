"""python manage.py add_luxury_gowns: the owner's 21 Luxury Gown designs -- 25 gowns in all (gown IDs 001 to 025): designs 3, 9, 10 and 19 in Large AND XL, designs
1, 2, 4-8, 11 and 12 in Large only, designs 13-18, 20 and 21 in XL only; a design's two sizes share one name and one photo (ONE product with two size buttons).
Named colour first ("Burgundy Luxury Gown 2"). The upload is stubbed so nothing touches Cloudinary or media."""
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
from gowns.management.commands.add_luxury_gowns import LUXURY_GOWNS, PRICES_BY_NAME, SIZES_BY_NAME
from gowns.models import Gown
from gowns.test_add_barongs import SAVE

CATEGORY = Gown.Category.LUXURY_GOWN

# The owner's list, typed in separately from the command: (colour, price, sizes) for designs 1-21.
LIST = [
    ("Lavender", 5500, "L"), ("Black and Red", 5500, "L"), ("Red", 3500, "L XL"), ("Burgundy", 5500, "L"), ("Burgundy", 5500, "L"),
    ("Charcoal", 5500, "L"), ("Maroon", 3000, "L"), ("Silver", 3000, "L"), ("Navy Blue", 3000, "L XL"), ("Royal Blue", 3000, "L XL"),
    ("Emerald Green", 3000, "L"), ("Burgundy", 3000, "L"), ("Silver", 3500, "XL"), ("Peach", 4500, "XL"), ("Navy Blue", 4500, "XL"),
    ("Black", 3500, "XL"), ("Rose Gold", 2500, "XL"), ("Emerald Green", 2500, "XL"), ("Light Pink", 2500, "L XL"), ("Red and Gold", 10000, "XL"),
    ("Purple", 2500, "XL"),
]
SIZE_NAME = {"L": "Large", "XL": "Extra Large"}
FIELD_COLOUR = {"Navy Blue": "Navy", "Emerald Green": "Emerald"}


def make_photos(folder):
    for _name, photo, *_ in LUXURY_GOWNS:
        buffer = io.BytesIO()
        Image.new("RGB", (40, 40), (200, 180, 150)).save(buffer, "JPEG")
        (Path(folder) / photo).write_bytes(buffer.getvalue())


class TheListTests(SimpleTestCase):
    def test_twenty_one_designs_with_the_photo_of_their_own_item(self):
        self.assertEqual(len(LUXURY_GOWNS), 21)
        self.assertEqual([photo for _n, photo, *_ in LUXURY_GOWNS], [f"Luxury Gown {n:02d}.jpg" for n in range(1, 22)])

    def test_every_colour_size_and_price_is_exactly_the_owners(self):
        for index, (name, _photo, field, _sizes, price) in enumerate(LUXURY_GOWNS):
            colour, want_price, want_sizes = LIST[index]
            self.assertEqual(field, FIELD_COLOUR.get(colour, colour), f"item {index + 1}")
            self.assertEqual(int(price), want_price, f"item {index + 1}")
            self.assertEqual([str(s) for s in SIZES_BY_NAME[name]], [SIZE_NAME[c] for c in want_sizes.split()], f"item {index + 1}")
            self.assertTrue(name.startswith(colour + " Luxury Gown "), name)

    def test_the_sizes_add_up_to_25_gowns(self):
        self.assertEqual(sum(len(s) for s in SIZES_BY_NAME.values()), 25)
        counts = Counter(tuple(str(x) for x in s) for s in SIZES_BY_NAME.values())
        self.assertEqual(counts, {("Large", "Extra Large"): 4, ("Large",): 9, ("Extra Large",): 8})
        both = [i + 1 for i, (_c, _p, sizes) in enumerate(LIST) if sizes == "L XL"]
        self.assertEqual(both, [3, 9, 10, 19])

    def test_every_name_differs_and_is_numbered_per_colour_in_list_order_not_by_photo(self):
        names = [n for n, *_ in LUXURY_GOWNS]
        self.assertEqual(len({n.lower() for n in names}), 21)
        by_base = {}
        for name in names:
            base, number = re.match(r"(.+) (\d+)$", name).groups()
            by_base.setdefault(base, []).append(int(number))
        for base, numbers in by_base.items():
            self.assertEqual(numbers, list(range(1, len(numbers) + 1)), base)
        self.assertEqual(len(by_base["Burgundy Luxury Gown"]), 3)
        self.assertEqual(by_base["Purple Luxury Gown"], [1])                                 # a lone colour still gets its 1


class AddLuxuryGownsTests(TestCase):
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
        call_command("add_luxury_gowns", f"--photos={self.folder.name}", *extra, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def gowns(self):
        return Gown.objects.filter(category=CATEGORY)

    def test_adds_25_gowns_with_each_designs_own_sizes_and_price(self):
        out, err = self.run_command()
        self.assertEqual((self.gowns().count(), err), (25, ""))
        self.assertIn("Added 25 gown(s); 0 size(s) were already there.", out)
        for name, sizes in SIZES_BY_NAME.items():
            got = sorted(self.gowns().filter(name=name).values_list("size", flat=True))
            self.assertEqual(got, sorted(str(s) for s in sizes), name)
            self.assertEqual({int(p) for p in self.gowns().filter(name=name).values_list("rental_price", flat=True)}, {int(PRICES_BY_NAME[name])}, name)
        self.assertEqual(self.gowns().values("name").distinct().count(), 21)

    def test_gown_ids_run_001_to_025_and_a_design_shares_one_photo(self):
        self.run_command()
        self.assertEqual(sorted(int(g.gown_id.rsplit("-", 1)[1]) for g in self.gowns()), list(range(1, 26)))
        number = 1
        for name, photo, _c, _s, _p in LUXURY_GOWNS:
            units = list(self.gowns().filter(name=name))
            self.assertEqual(len({u.photo_url for u in units}), 1, name)
            self.assertTrue(units[0].photo_url.endswith(photo.lower().replace(" ", "-")), name)
            self.assertEqual(sorted(int(u.gown_id.rsplit("-", 1)[1]) for u in units), list(range(number, number + len(units))), name)
            number += len(units)
        self.assertEqual(len(self.uploads), 21)                                              # one upload per design, not per size

    def test_colour_names_and_codes(self):
        self.run_command()
        by_colour = {}
        for colour, code in self.gowns().values_list("color_name", "color_code"):
            by_colour.setdefault(colour, set()).add(code)
        self.assertTrue(all(len(codes) == 1 for codes in by_colour.values()), by_colour)       # one colour, one code
        self.assertEqual(len({next(iter(c)) for c in by_colour.values()}), len(by_colour))       # no code shared by two colours
        presets = {"Lavender": "LV", "Red": "RD", "Burgundy": "BD", "Charcoal": "CL", "Maroon": "MR", "Silver": "SV", "Navy": "NV", "Royal Blue": "RB",
                   "Emerald": "EM", "Peach": "PE", "Black": "BK", "Rose Gold": "RG", "Purple": "PU"}
        for colour, code in presets.items():
            self.assertEqual(by_colour[colour], {code}, colour)
        for colour in ("Light Pink", "Black and Red", "Red and Gold"):                          # no preset: a free 2-letter code each
            self.assertRegex(next(iter(by_colour[colour])), r"^[A-Z]{2}$", colour)
        self.assertEqual(len(by_colour), 16)

    def test_the_shops_existing_light_pink_code_is_reused(self):
        Gown.objects.create(gown_id="Dresses-LP-007", name="Light Pink Dress 1", category=Gown.Category.DRESSES, color_name="Light Pink", color_code="LP",
                            size="Large", rental_price=1800)
        self.run_command()
        self.assertEqual(set(self.gowns().filter(color_name="Light Pink").values_list("color_code", flat=True)), {"LP"})

    def test_run_again_adds_nothing_and_a_dry_run_changes_nothing(self):
        self.run_command()
        out, _e = self.run_command()
        self.assertIn("Added 0 gown(s); 25 size(s) were already there.", out)
        self.assertEqual(self.gowns().count(), 25)
        self.gowns().filter(name="Red Luxury Gown 1", size="Extra Large").delete()
        out, _e = self.run_command("--dry-run")
        self.assertEqual(self.gowns().count(), 24)
        self.assertIn("Would add 1 gown(s); 24 size(s) were already there.", out)

    def test_a_missing_photo_stops_only_that_design(self):
        (Path(self.folder.name) / "Luxury Gown 10.jpg").unlink()
        with self.assertRaises(CommandError) as raised:
            self.run_command()
        self.assertIn("1 Luxury Gown(s) could not be added", str(raised.exception))
        self.assertEqual(self.gowns().count(), 23)                                         # design 10's two sizes are the only ones missing
        self.assertFalse(self.gowns().filter(name="Royal Blue Luxury Gown 1").exists())

    def test_the_customer_site_shows_21_products_with_the_right_size_buttons(self):
        self.run_command()
        self.assertFalse(Gown.objects.exclude(category=CATEGORY).exists())
        url = next(category_url(row) for row in all_categories() if row["label"] == "Luxury Gown")
        first = self.client.get(url).content.decode()
        pages = int(re.search(r"Page 1 of (\d+)", first).group(1)) if "Page 1 of" in first else 1
        seen = []
        for page in range(1, pages + 1):
            html = first if page == 1 else self.client.get(url, {"page": page}).content.decode()
            main = html[html.index("<main"):html.index("</main>")]
            seen += [unescape(t) for t in re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', main)]
        self.assertEqual(len(seen), 21)                                                      # 25 gowns = 21 products
        self.assertEqual(set(seen), {name for name, *_ in LUXURY_GOWNS})
        for name, offered in (("Red Luxury Gown 1", ["L", "XL"]), ("Lavender Luxury Gown 1", ["L"]), ("Purple Luxury Gown 1", ["XL"])):
            slug = self.gowns().filter(name=name).first().slug
            html = self.client.get(f"{url}products/{slug}/").content.decode()
            self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', html), offered, name)
        slug = self.gowns().filter(name="Red and Gold Luxury Gown 1").first().slug
        self.assertIn("₱10,000", self.client.get(f"{url}products/{slug}/").content.decode())

    def test_search_finds_a_colour(self):
        self.run_command()
        html = self.client.get("/search/", {"q": "lavender"}).content.decode()
        found = {unescape(t) for t in re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', html[html.index("<main"):html.index("</main>")])}
        self.assertEqual(found, {"Lavender Luxury Gown 1"})
