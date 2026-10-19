"""python manage.py add_guest_gowns: the owner's 17 Guest Gown designs -- 30 gowns in all (gown IDs 001 to 030): designs 1-13 in Large AND XL, designs 14-15 in
Large only, designs 16-17 in XL only; a design's two sizes share one name and one photo (ONE product with two size buttons). Named colour first ("Sage Green
Guest Gown 2"). The upload is stubbed so nothing touches Cloudinary or media."""
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
from gowns.management.commands.add_guest_gowns import GUEST_GOWNS, PRICES_BY_NAME, SIZES_BY_NAME
from gowns.models import Gown
from gowns.test_add_barongs import SAVE

CATEGORY = Gown.Category.GUEST_GOWN

# The owner's list, typed in separately from the command: (colour, price, sizes) for designs 1-17.
LIST = [
    ("Sage Green", 1500, "L XL"), ("Slate Blue", 3500, "L XL"), ("Champagne", 3500, "L XL"), ("Slate Blue", 3500, "L XL"),
    ("Champagne", 1500, "L XL"), ("Pink", 1500, "L XL"), ("Purple", 3500, "L XL"), ("Blush Pink", 1500, "L XL"), ("Blush Pink", 3500, "L XL"),
    ("Sage Green", 3500, "L XL"), ("Blue", 1500, "L XL"), ("Silver", 1500, "L XL"), ("Red", 3500, "L XL"), ("Rose Gold", 3500, "L"),
    ("Brown", 1500, "L"), ("Navy Blue", 3500, "XL"), ("Sage Green", 3500, "XL"),
]
SIZE_NAME = {"L": "Large", "XL": "Extra Large"}
FIELD_COLOUR = {"Blush Pink": "Blush", "Navy Blue": "Navy"}


def make_photos(folder):
    for _name, photo, *_ in GUEST_GOWNS:
        buffer = io.BytesIO()
        Image.new("RGB", (40, 40), (200, 180, 150)).save(buffer, "JPEG")
        (Path(folder) / photo).write_bytes(buffer.getvalue())


class TheListTests(SimpleTestCase):
    def test_seventeen_designs_with_the_photo_of_their_own_item(self):
        self.assertEqual(len(GUEST_GOWNS), 17)
        self.assertEqual([photo for _n, photo, *_ in GUEST_GOWNS], [f"Guest Gown {n:02d}.jpg" for n in range(1, 18)])

    def test_every_colour_size_and_price_is_exactly_the_owners(self):
        for index, (name, _photo, field, _sizes, price) in enumerate(GUEST_GOWNS):
            colour, want_price, want_sizes = LIST[index]
            self.assertEqual(field, FIELD_COLOUR.get(colour, colour), f"item {index + 1}")
            self.assertEqual(int(price), want_price, f"item {index + 1}")
            self.assertEqual([str(s) for s in SIZES_BY_NAME[name]], [SIZE_NAME[c] for c in want_sizes.split()], f"item {index + 1}")
            self.assertTrue(name.startswith(colour + " Guest Gown "), name)

    def test_the_sizes_add_up_to_30_gowns(self):
        self.assertEqual(sum(len(s) for s in SIZES_BY_NAME.values()), 30)
        counts = Counter(tuple(str(x) for x in s) for s in SIZES_BY_NAME.values())
        self.assertEqual(counts, {("Large", "Extra Large"): 13, ("Large",): 2, ("Extra Large",): 2})

    def test_every_name_differs_and_is_numbered_per_colour_in_list_order(self):
        names = [n for n, *_ in GUEST_GOWNS]
        self.assertEqual(len({n.lower() for n in names}), 17)
        by_base = {}
        for name in names:
            base, number = re.match(r"(.+) (\d+)$", name).groups()
            by_base.setdefault(base, []).append(int(number))
        for base, numbers in by_base.items():
            self.assertEqual(numbers, list(range(1, len(numbers) + 1)), base)
        self.assertEqual(len(by_base["Sage Green Guest Gown"]), 3)
        self.assertEqual(by_base["Brown Guest Gown"], [1])                                  # a lone colour still gets its 1


class AddGuestGownsTests(TestCase):
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
        call_command("add_guest_gowns", f"--photos={self.folder.name}", *extra, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def gowns(self):
        return Gown.objects.filter(category=CATEGORY)

    def test_adds_30_gowns_with_each_designs_own_sizes_and_price(self):
        out, err = self.run_command()
        self.assertEqual((self.gowns().count(), err), (30, ""))
        self.assertIn("Added 30 gown(s); 0 size(s) were already there.", out)
        for name, sizes in SIZES_BY_NAME.items():
            got = sorted(self.gowns().filter(name=name).values_list("size", flat=True))
            self.assertEqual(got, sorted(str(s) for s in sizes), name)                      # exactly the owner's sizes, one gown each
            self.assertEqual({int(p) for p in self.gowns().filter(name=name).values_list("rental_price", flat=True)}, {int(PRICES_BY_NAME[name])}, name)
        self.assertEqual(self.gowns().values("name").distinct().count(), 17)

    def test_gown_ids_run_001_to_030_in_the_owners_order_and_a_design_shares_one_photo(self):
        self.run_command()
        self.assertEqual(sorted(int(g.gown_id.rsplit("-", 1)[1]) for g in self.gowns()), list(range(1, 31)))
        number = 1
        for name, photo, _c, _s, _p in GUEST_GOWNS:
            units = list(self.gowns().filter(name=name))
            self.assertEqual(len({u.photo_url for u in units}), 1, name)                    # both sizes use the one photo
            self.assertTrue(units[0].photo_url.endswith(photo.lower().replace(" ", "-")), name)
            self.assertEqual(sorted(int(u.gown_id.rsplit("-", 1)[1]) for u in units), list(range(number, number + len(units))), name)
            number += len(units)
        self.assertEqual(len(self.uploads), 17)                                              # one upload per design, not per size

    def test_colour_names_and_codes(self):
        self.run_command()
        by_colour = {}
        for colour, code in self.gowns().values_list("color_name", "color_code"):
            by_colour.setdefault(colour, set()).add(code)
        self.assertTrue(all(len(codes) == 1 for codes in by_colour.values()), by_colour)       # one colour, one code
        self.assertEqual(len({next(iter(c)) for c in by_colour.values()}), len(by_colour))       # no code shared by two colours
        presets = {"Sage Green": "SG", "Champagne": "CH", "Pink": "PK", "Purple": "PU", "Blush": "BL", "Blue": "BU", "Silver": "SV", "Red": "RD",
                   "Rose Gold": "RG", "Brown": "BR", "Navy": "NV"}
        for colour, code in presets.items():
            self.assertEqual(by_colour[colour], {code}, colour)
        self.assertRegex(next(iter(by_colour["Slate Blue"])), r"^[A-Z]{2}$")                  # the one colour with no preset

    def test_run_again_adds_nothing_and_a_dry_run_changes_nothing(self):
        self.run_command()
        out, _e = self.run_command()
        self.assertIn("Added 0 gown(s); 30 size(s) were already there.", out)
        self.assertEqual(self.gowns().count(), 30)                                         # running it again never adds quantity
        self.gowns().filter(name="Purple Guest Gown 1", size="Extra Large").delete()
        out, _e = self.run_command("--dry-run")
        self.assertEqual(self.gowns().count(), 29)
        self.assertIn("Would add 1 gown(s); 29 size(s) were already there.", out)

    def test_a_missing_photo_stops_only_that_design(self):
        (Path(self.folder.name) / "Guest Gown 09.jpg").unlink()
        with self.assertRaises(CommandError) as raised:
            self.run_command()
        self.assertIn("1 Guest Gown(s) could not be added", str(raised.exception))
        self.assertEqual(self.gowns().count(), 28)                                         # design 9's two sizes are the only ones missing
        self.assertFalse(self.gowns().filter(name="Blush Pink Guest Gown 2").exists())

    def test_the_customer_site_shows_17_products_with_the_right_size_buttons(self):
        self.run_command()
        self.assertFalse(Gown.objects.exclude(category=CATEGORY).exists())
        url = next(category_url(row) for row in all_categories() if row["label"] == "Guest Gown")
        first = self.client.get(url).content.decode()
        pages = int(re.search(r"Page 1 of (\d+)", first).group(1)) if "Page 1 of" in first else 1
        seen = []
        for page in range(1, pages + 1):
            html = first if page == 1 else self.client.get(url, {"page": page}).content.decode()
            main = html[html.index("<main"):html.index("</main>")]
            seen += [unescape(t) for t in re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', main)]
        self.assertEqual(len(seen), 17)                                                      # 30 gowns = 17 products
        self.assertEqual(set(seen), {name for name, *_ in GUEST_GOWNS})
        for name, sizes, offered in (("Sage Green Guest Gown 1", 2, ["L", "XL"]), ("Rose Gold Guest Gown 1", 1, ["L"]), ("Navy Blue Guest Gown 1", 1, ["XL"])):
            slug = self.gowns().filter(name=name).first().slug
            html = self.client.get(f"{url}products/{slug}/").content.decode()
            self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', html), offered, name)
            self.assertEqual(len(offered), sizes)
        self.assertIn("₱1,500", self.client.get(f"{url}products/{self.gowns().get(name='Sage Green Guest Gown 1', size='Large').slug}/").content.decode())

    def test_search_finds_a_colour(self):
        self.run_command()
        html = self.client.get("/search/", {"q": "brown"}).content.decode()
        found = {unescape(t) for t in re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', html[html.index("<main"):html.index("</main>")])}
        self.assertEqual(found, {"Brown Guest Gown 1"})
