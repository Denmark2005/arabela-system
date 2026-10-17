"""python manage.py add_dresses: the 9 Dresses from the owner's table, Large and Extra Large (18 gowns, 1,800 each), named colour-first and
numbered per colour ("Light Pink Dress 1-3", lone colours get their "1"). The upload is stubbed so nothing touches Cloudinary or media."""
import io
import re
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from PIL import Image

from gowns.management.commands.add_dresses import DRESSES
from gowns.models import Gown
from gowns.test_add_barongs import SAVE

CATEGORY = Gown.Category.DRESSES
TABLE = ["old rose", "yellow", "beige", "light pink", "light pink", "light pink", "pink", "navy blue", "white"]


def make_photos(folder):
    for _n, photo, _c in DRESSES:
        buffer = io.BytesIO()
        Image.new("RGB", (40, 40), (220, 190, 200)).save(buffer, "JPEG")
        (Path(folder) / photo).write_bytes(buffer.getvalue())


class TheTableTests(SimpleTestCase):
    def test_nine_designs_with_the_photo_of_their_own_row(self):
        self.assertEqual(len(DRESSES), 9)
        self.assertEqual([p for _n, p, _c in DRESSES], [f"Dresses {n:02d}.jpg" for n in range(1, 10)])

    def test_every_name_differs_starts_with_the_tables_colour_and_is_numbered_per_colour(self):
        names = [n for n, _p, _c in DRESSES]
        self.assertEqual(len({n.lower() for n in names}), 9)
        by_color = {}
        for row, name in enumerate(names):
            self.assertTrue(name.lower().startswith(TABLE[row]), name)
            by_color.setdefault(TABLE[row], []).append(name)
        for color, group in by_color.items():
            self.assertEqual([re.search(r"(\d+)$", n).group(1) for n in group], [str(i) for i in range(1, len(group) + 1)], color)
        self.assertEqual(by_color["light pink"], ["Light Pink Dress 1", "Light Pink Dress 2", "Light Pink Dress 3"])
        self.assertEqual(by_color["yellow"], ["Yellow Dress 1"])                       # a lone colour still gets its 1


class AddDressesTests(TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        make_photos(self.folder.name)
        patcher = mock.patch(SAVE, side_effect=lambda f: f"https://photos.test/gown_photos/{f.name}")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_command(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        call_command("add_dresses", f"--photos={self.folder.name}", *extra, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def dresses(self):
        return Gown.objects.filter(category=CATEGORY)

    def test_adds_nine_dresses_in_large_and_extra_large_at_1800(self):
        out, err = self.run_command()
        self.assertEqual((self.dresses().count(), err), (18, ""))
        self.assertEqual(set(self.dresses().values_list("size", flat=True)), {"Large", "Extra Large"})
        self.assertEqual({int(g.rental_price) for g in self.dresses()}, {1800})
        for name, _p, _c in DRESSES:
            self.assertEqual(self.dresses().filter(name=name).count(), 2, name)
        self.assertIn("Added 18 gown(s); 0 size(s) were already there.", out)

    def test_colours_and_codes(self):
        self.run_command()
        got = {g.name: (g.color_name, g.color_code) for g in self.dresses()}
        self.assertEqual(got["Yellow Dress 1"], ("Yellow", "YL"))
        self.assertEqual(got["Beige Dress 1"], ("Beige", "BG"))
        self.assertEqual(got["Pink Dress 1"], ("Pink", "PK"))
        self.assertEqual(got["Navy Blue Dress 1"], ("Navy", "NV"))
        self.assertEqual(got["White Dress 1"], ("White", "WH"))
        light = {got[f"Light Pink Dress {i}"] for i in (1, 2, 3)}
        self.assertEqual(light, {("Light Pink", "LP")})                                 # one colour, one code
        pairs = set(self.dresses().values_list("color_name", "color_code"))
        self.assertEqual(len({c for _n, c in pairs}), len(pairs))                       # no code shared by two colours

    def test_old_rose_reuses_the_code_the_shop_already_has_for_it(self):
        Gown.objects.create(gown_id="BD-OL-001", name="Old Rose Bridesmaid Dress 1", category=Gown.Category.BRIDESMAID_DRESSES,
                            color_name="Old Rose", color_code="OL", size=Gown.Size.LARGE, rental_price=1800)
        self.run_command()
        self.assertEqual(self.dresses().get(name="Old Rose Dress 1", size="Large").color_code, "OL")

    def test_tag_numbers_one_to_eighteen_and_one_photo_per_dress(self):
        self.run_command()
        self.assertEqual(sorted(int(g.gown_id.rsplit("-", 1)[1]) for g in self.dresses()), list(range(1, 19)))
        for name, photo, _c in DRESSES:
            urls = set(self.dresses().filter(name=name).values_list("photo_url", flat=True))
            self.assertEqual(urls, {f"https://photos.test/gown_photos/{photo.lower().replace(' ', '-')}"})

    def test_run_again_adds_nothing_and_dry_run_changes_nothing(self):
        self.run_command()
        out, _e = self.run_command()
        self.assertIn("Added 0 gown(s); 18 size(s) were already there.", out)
        self.dresses().filter(name="Pink Dress 1", size="Extra Large").delete()
        out, _e = self.run_command("--dry-run")
        self.assertEqual(self.dresses().count(), 17)
        self.assertIn("Would add 1 gown(s); 17 size(s) were already there.", out)

    def test_a_missing_photo_stops_only_that_dress(self):
        (Path(self.folder.name) / "Dresses 06.jpg").unlink()
        with self.assertRaises(CommandError) as raised:
            self.run_command()
        self.assertIn("1 Dress(s) could not be added", str(raised.exception))
        self.assertEqual(self.dresses().count(), 16)

    def test_the_other_categories_are_untouched_and_the_customer_site_shows_nine_products(self):
        self.run_command()
        self.assertFalse(Gown.objects.exclude(category=CATEGORY).exists())
        page = self.client.get("/collections/dresses/")
        self.assertEqual(page.status_code, 200)
        def products(response):                                  # the cards only (the page also carries the search overlay's featured list)
            html = response.content.decode()
            return {h for h in html[html.index("<main"):html.index("</main>")].split('"') if h.startswith("/collections/dresses/products/")}
        hrefs = products(page)
        self.assertEqual(len(hrefs), 8)                                                 # 8 per page, page 1 of 2
        more = products(self.client.get("/collections/dresses/?page=2"))
        self.assertEqual(len(hrefs | more), 9)
        slug = self.dresses().get(name="Light Pink Dress 2", size="Large").slug
        html = self.client.get(f"/collections/dresses/products/{slug}/").content.decode()
        self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', html), ["L", "XL"])
        self.assertEqual(re.findall(r'data-size-not-offered="([A-Z]+)"', html), ["S", "M"])
