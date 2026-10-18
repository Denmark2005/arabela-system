"""python manage.py add_ball_gown_tulle: the owner's 50 Ball Gown Tulle gowns -- exactly 50 gowns (gown IDs 001 to 050), ONE physical gown per design, ALL in
Large, at the owner's price (items 1-30 at 1,500, 31-50 at 4,000), named colour first ("Red Ball Gown Tulle 3"). No extra sizes (the list's XL for items 39-50
was NOT wanted) and no extra quantity. The upload is stubbed so nothing touches Cloudinary or media."""
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
from gowns.management.commands.add_ball_gown_tulle import BALL_GOWN_TULLES, PRICES_BY_NAME, SIZES_BY_NAME
from gowns.models import Gown
from gowns.test_add_barongs import SAVE

CATEGORY = Gown.Category.BALL_GOWN_TULLE

# The owner's list, typed in separately from the command: (colour, price) for items 1-50.
LIST = [
    ("Yellow", 1500), ("Old Rose", 1500), ("White", 1500), ("Old Rose", 1500), ("Red", 1500), ("Beige", 1500), ("Yellow", 1500),
    ("Light Yellow", 1500), ("Black", 1500), ("Red", 1500), ("Light Pink", 1500), ("Purple", 1500), ("Old Rose", 1500),
    ("Emerald Green", 1500), ("Yellow", 1500), ("Red", 1500), ("Red", 1500), ("Emerald Green", 1500), ("Red & Black", 1500),
    ("Red", 1500), ("Yellow", 1500), ("Pink", 1500), ("Peach", 1500), ("Light Blue", 1500), ("Red", 1500), ("Navy Blue", 1500),
    ("Light Blue", 1500), ("Yellow & Black", 1500), ("Green", 1500), ("Red", 1500),
    ("Pink", 4000), ("Green", 4000), ("Black & Beige", 4000), ("Light Purple", 4000), ("Pink", 4000), ("White", 4000), ("White", 4000),
    ("Light Green", 4000), ("Pink & Light Blue", 4000), ("Light Blue", 4000), ("Dusty Blue", 4000), ("Pink & Beige", 4000),
    ("Pink & Gray", 4000), ("Purple", 4000), ("Pink & Light Blue", 4000), ("Light Blue & Pink", 4000), ("Pink", 4000),
    ("Pink & Gray", 4000), ("Light Green", 4000), ("Pink", 4000),
]
FIELD_COLOUR = {"Emerald Green": "Emerald", "Navy Blue": "Navy", "Red & Black": "Red and Black", "Yellow & Black": "Yellow and Black",
                "Black & Beige": "Black and Beige", "Pink & Light Blue": "Pink and Light Blue", "Pink & Beige": "Pink and Beige",
                "Pink & Gray": "Pink and Grey", "Light Blue & Pink": "Light Blue and Pink"}


def make_photos(folder):
    for _name, photo, *_ in BALL_GOWN_TULLES:
        buffer = io.BytesIO()
        Image.new("RGB", (40, 40), (200, 180, 150)).save(buffer, "JPEG")
        (Path(folder) / photo).write_bytes(buffer.getvalue())


class TheListTests(SimpleTestCase):
    def test_fifty_gowns_with_the_photo_of_their_own_item(self):
        self.assertEqual(len(BALL_GOWN_TULLES), 50)
        self.assertEqual([photo for _n, photo, *_ in BALL_GOWN_TULLES], [f"Ball Gown Tulle {n:02d}.jpg" for n in range(1, 51)])

    def test_every_colour_and_price_is_exactly_the_owners(self):
        for index, (name, _photo, field, price) in enumerate(BALL_GOWN_TULLES):
            colour, want_price = LIST[index]
            self.assertEqual(field, FIELD_COLOUR.get(colour, colour), f"item {index + 1}")
            self.assertEqual(int(price), want_price, f"item {index + 1}")
            self.assertTrue(name.startswith(colour.replace(" & ", " and ") + " Ball Gown Tulle "), name)

    def test_every_gown_is_one_large_so_there_are_50_gowns_in_all(self):
        for name, sizes in SIZES_BY_NAME.items():
            self.assertEqual(tuple(str(s) for s in sizes), ("Large",), name)
        self.assertEqual(Counter(int(p) for p in PRICES_BY_NAME.values()), {1500: 30, 4000: 20})

    def test_every_name_differs_and_is_numbered_per_colour_in_list_order(self):
        names = [n for n, *_ in BALL_GOWN_TULLES]
        self.assertEqual(len({n.lower() for n in names}), 50)
        by_base = {}
        for name in names:
            base, number = re.match(r"(.+) (\d+)$", name).groups()
            by_base.setdefault(base, []).append(int(number))
        for base, numbers in by_base.items():
            self.assertEqual(numbers, list(range(1, len(numbers) + 1)), base)
        self.assertEqual(len(by_base["Red Ball Gown Tulle"]), 7)
        self.assertEqual(len(by_base["Pink Ball Gown Tulle"]), 5)
        self.assertEqual(by_base["Dusty Blue Ball Gown Tulle"], [1])                      # a lone colour still gets its 1


class AddBallGownTulleTests(TestCase):
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
        call_command("add_ball_gown_tulle", f"--photos={self.folder.name}", *extra, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def gowns(self):
        return Gown.objects.filter(category=CATEGORY)

    def test_adds_exactly_50_gowns_all_large_at_the_owners_price(self):
        out, err = self.run_command()
        self.assertEqual((self.gowns().count(), err), (50, ""))
        self.assertIn("Added 50 gown(s); 0 size(s) were already there.", out)
        self.assertEqual(set(self.gowns().values_list("size", flat=True)), {"Large"})        # no XL, even for items 39-50
        for name in SIZES_BY_NAME:
            got = list(self.gowns().filter(name=name).values_list("size", "rental_price"))
            self.assertEqual(len(got), 1, name)
            self.assertEqual(int(got[0][1]), int(PRICES_BY_NAME[name]), name)
        self.assertEqual(self.gowns().values("name").distinct().count(), 50)

    def test_gown_ids_run_001_to_050_in_the_owners_order_with_one_photo_each(self):
        self.run_command()
        self.assertEqual(sorted(int(g.gown_id.rsplit("-", 1)[1]) for g in self.gowns()), list(range(1, 51)))
        for index, (name, photo, *_rest) in enumerate(BALL_GOWN_TULLES, start=1):
            gown = self.gowns().get(name=name)
            self.assertTrue(gown.gown_id.endswith(f"-{index:03d}"), name)
            self.assertTrue(gown.photo_url.endswith(photo.lower().replace(" ", "-")), name)
        self.assertEqual(len(self.uploads), 50)

    def test_only_photos_01_to_50_are_used(self):
        (Path(self.folder.name) / "Ball Gown Tulle 51.jpg").write_bytes(b"not used")
        (Path(self.folder.name) / "Ball Gown Tulle 52.jpg").write_bytes(b"not used")
        self.run_command()
        self.assertFalse(any("51" in u or "52" in u for u in self.uploads))
        self.assertEqual(self.gowns().count(), 50)

    def test_colour_names_and_codes(self):
        self.run_command()
        by_colour = {}
        for colour, code in self.gowns().values_list("color_name", "color_code"):
            by_colour.setdefault(colour, set()).add(code)
        self.assertTrue(all(len(codes) == 1 for codes in by_colour.values()), by_colour)       # one colour, one code
        self.assertEqual(len({next(iter(c)) for c in by_colour.values()}), len(by_colour))       # no code shared by two colours
        presets = {"Yellow": "YL", "White": "WH", "Red": "RD", "Beige": "BG", "Black": "BK", "Purple": "PU", "Emerald": "EM", "Pink": "PK",
                   "Peach": "PE", "Navy": "NV", "Green": "GN"}
        for colour, code in presets.items():
            self.assertEqual(by_colour[colour], {code}, colour)
        self.assertEqual(by_colour["Old Rose"], {"OL"})
        self.assertEqual(by_colour["Dusty Blue"], {"DB"})
        self.assertEqual(by_colour["Light Pink"], {"LP"})
        self.assertEqual(by_colour["Light Blue"], {"LB"})
        self.assertEqual(len(by_colour), 25)
        for code in {next(iter(c)) for c in by_colour.values()}:
            self.assertRegex(code, r"^[A-Z]{2}$")

    def test_an_existing_colour_in_the_shop_keeps_its_code(self):
        # the live shop already has "Black and Beige" (Filipiniana) and "Light Gray" (suits, code LG): neither may be reused for a different colour
        Gown.objects.create(gown_id="Filipiniana-BB-001", name="Black and Beige Filipiniana 1", category=Gown.Category.FILIPINIANA, color_name="Black and Beige",
                            color_code="BB", size="Large", rental_price=1500)
        Gown.objects.create(gown_id="Suit-LG-001", name="Light Gray Suit 1", category=Gown.Category.SUIT, color_name="Light Gray", color_code="LG",
                            size="Large", rental_price=1500)
        out, err = self.run_command()
        self.assertEqual(err, "")
        self.assertEqual(set(self.gowns().filter(color_name="Black and Beige").values_list("color_code", flat=True)), {"BB"})
        self.assertNotEqual(next(iter(self.gowns().filter(color_name="Light Green").values_list("color_code", flat=True))), "LG")

    def test_run_again_adds_nothing_and_a_dry_run_changes_nothing(self):
        self.run_command()
        out, _e = self.run_command()
        self.assertIn("Added 0 gown(s); 50 size(s) were already there.", out)
        self.assertEqual(self.gowns().count(), 50)                                         # running it again never adds quantity
        self.gowns().filter(name="Dusty Blue Ball Gown Tulle 1").delete()
        out, _e = self.run_command("--dry-run")
        self.assertEqual(self.gowns().count(), 49)
        self.assertIn("Would add 1 gown(s); 49 size(s) were already there.", out)

    def test_a_missing_photo_stops_only_that_gown(self):
        (Path(self.folder.name) / "Ball Gown Tulle 09.jpg").unlink()
        with self.assertRaises(CommandError) as raised:
            self.run_command()
        self.assertIn("1 Ball Gown Tulle gown(s) could not be added", str(raised.exception))
        self.assertEqual(self.gowns().count(), 49)
        self.assertFalse(self.gowns().filter(name="Black Ball Gown Tulle 1").exists())

    def test_the_customer_site_lists_all_50_with_only_large_clickable(self):
        self.run_command()
        self.assertFalse(Gown.objects.exclude(category=CATEGORY).exists())
        url = next(category_url(row) for row in all_categories() if row["label"] == "Ball Gown Tulle")
        first = self.client.get(url).content.decode()
        pages = int(re.search(r"Page 1 of (\d+)", first).group(1))
        seen = []
        for page in range(1, pages + 1):
            html = first if page == 1 else self.client.get(url, {"page": page}).content.decode()
            main = html[html.index("<main"):html.index("</main>")]
            seen += [unescape(t) for t in re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', main)]
        self.assertEqual(len(seen), 50)
        self.assertEqual(set(seen), {name for name, *_ in BALL_GOWN_TULLES})
        slug = self.gowns().get(name="Pink and Gray Ball Gown Tulle 1").slug                  # item 43: the list said L, XL; only Large is made
        html = self.client.get(f"{url}products/{slug}/").content.decode()
        self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', html), ["L"])
        self.assertEqual(re.findall(r'data-size-not-offered="([A-Z]+)"', html), ["S", "M", "XL", "XXL"])
        self.assertIn("₱4,000", html)
        slug = self.gowns().get(name="Yellow Ball Gown Tulle 1").slug
        html = self.client.get(f"{url}products/{slug}/").content.decode()
        self.assertIn("₱1,500", html)

    def test_search_finds_a_colour(self):
        self.run_command()
        html = self.client.get("/search/", {"q": "dusty"}).content.decode()
        found = {unescape(t) for t in re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', html[html.index("<main"):html.index("</main>")])}
        self.assertEqual(found, {"Dusty Blue Ball Gown Tulle 1"})
