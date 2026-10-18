"""python manage.py add_bridesmaids: the 15 Bridesmaid Dresses from the owner's table, in Large and Extra Large (30 gowns, 1,800 each),
named colour-first like the Barongs ("Black Bridesmaid Dress 1" ... "Navy Blue Bridesmaid Dress 2"), made the way Add Gown makes them.
The upload is replaced by a stand-in so no test touches Cloudinary or the media folder."""
import io
import re
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from django.utils.html import escape
from PIL import Image

from gowns.management.commands import add_bridesmaids
from gowns.management.commands.add_bridesmaids import BRIDESMAIDS, Command
from gowns.models import Gown
from gowns.test_add_barongs import SAVE

CATEGORY = Gown.Category.BRIDESMAID_DRESSES

# The owner's table, row by row: colour as she wrote it.
TABLE = ["sage green", "black", "navy blue", "black", "white", "dusty blue", "beige", "navy blue", "black", "black", "white", "old rose",
         "emerald green", "dusty blue", "white"]


def make_photos(folder):
    for _name, photo_name, _color in BRIDESMAIDS:
        buffer = io.BytesIO()
        Image.new("RGB", (40, 40), (200, 190, 180)).save(buffer, "JPEG")
        (Path(folder) / photo_name).write_bytes(buffer.getvalue())


class TheTableTests(SimpleTestCase):
    def test_fifteen_designs_with_the_photo_of_their_own_row(self):
        self.assertEqual(len(BRIDESMAIDS), 15)
        self.assertEqual([photo for _n, photo, _c in BRIDESMAIDS], [f"Bridesmaid Dresses {n:02d}.jpg" for n in range(1, 16)])

    def test_every_name_is_different_so_no_two_dresses_merge_into_one_product(self):
        names = [name.lower() for name, _p, _c in BRIDESMAIDS]
        self.assertEqual(len(set(names)), 15)

    def test_each_name_starts_with_the_colour_the_table_says(self):
        for row, (name, _photo, _color) in enumerate(BRIDESMAIDS):
            self.assertTrue(name.lower().startswith(TABLE[row]), f"row {row + 1}: {name} vs {TABLE[row]}")
            self.assertIn("Bridesmaid Dress", name)

    def test_every_colour_is_numbered_in_table_order_and_a_lone_one_still_gets_its_1(self):
        by_color = {}
        for row, (name, _photo, _color) in enumerate(BRIDESMAIDS):
            by_color.setdefault(TABLE[row], []).append(name)
        for color, names in by_color.items():
            self.assertEqual([re.search(r"(\d+)$", n).group(1) for n in names], [str(i) for i in range(1, len(names) + 1)], color)
        for color in ("sage green", "beige", "old rose", "emerald green"):                # the colours with a single dress
            self.assertEqual(len(by_color[color]), 1)
            self.assertTrue(by_color[color][0].endswith(" Bridesmaid Dress 1"), by_color[color])
        self.assertEqual(by_color["black"], [f"Black Bridesmaid Dress {i}" for i in (1, 2, 3, 4)])
        self.assertEqual(by_color["white"], [f"White Bridesmaid Dress {i}" for i in (1, 2, 3)])

    def test_the_other_batch_is_unchanged_by_sharing_the_engine(self):
        self.assertEqual(Command.category, CATEGORY)
        from gowns.management.commands import add_barongs
        self.assertEqual(add_barongs.Command.category, Gown.Category.BARONG)
        self.assertEqual(add_barongs.Command().sizes_text(), "Small, Medium and Large")
        self.assertEqual(Command().sizes_text(), "Large and Extra Large")


class CandidateCodeTests(SimpleTestCase):
    def codes(self, color):
        return Command().candidate_codes(color)

    def test_initials_come_first_for_a_two_word_colour(self):
        self.assertEqual(self.codes("Dusty Blue")[:2], ["DB", "DU"])
        self.assertEqual(self.codes("Old Rose")[:2], ["OR", "OL"])

    def test_every_candidate_is_two_letters_and_none_repeats(self):
        for color in ("Dusty Blue", "Old Rose", "Teal", "X", "Light  Blue & Gold"):
            codes = self.codes(color)
            self.assertEqual(len(codes), len(set(codes)))
            self.assertTrue(all(re.fullmatch(r"[A-Z]{2}", c) for c in codes), (color, codes))

    def test_the_barong_hint_still_wins_for_its_colour(self):
        from gowns.management.commands import add_barongs
        self.assertEqual(add_barongs.Command().candidate_codes("White & Light Blue")[0], "WL")


class AddBridesmaidsTests(TestCase):
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

    def run_command(self, *extra, name="add_bridesmaids"):
        out, err = io.StringIO(), io.StringIO()
        call_command(name, f"--photos={self.folder.name}", *extra, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def dresses(self):
        return Gown.objects.filter(category=CATEGORY)

    def test_adds_fifteen_dresses_in_large_and_extra_large(self):
        out, err = self.run_command()
        self.assertEqual(self.dresses().count(), 30)
        self.assertEqual(err, "")
        for name, _photo, _color in BRIDESMAIDS:
            self.assertEqual(sorted(self.dresses().filter(name=name).values_list("size", flat=True)), ["Extra Large", "Large"], name)
        self.assertEqual(set(self.dresses().values_list("size", flat=True)), {"Large", "Extra Large"})     # no Small / Medium
        self.assertIn("Added 30 gown(s); 0 size(s) were already there.", out)

    def test_price_condition_status_and_category(self):
        self.run_command()
        for gown in self.dresses():
            self.assertEqual(int(gown.rental_price), 1800)
            self.assertEqual((gown.condition, gown.status), (Gown.Condition.GOOD, Gown.Status.AVAILABLE))
            self.assertEqual(gown.category, "Bridesmaid Dresses")

    def test_colour_names_and_codes(self):
        self.run_command()
        got = {g.name: (g.color_name, g.color_code) for g in self.dresses()}
        expected = {"Sage Green Bridesmaid Dress 1": ("Sage Green", "SG"), "Black Bridesmaid Dress 1": ("Black", "BK"),
                    "Navy Blue Bridesmaid Dress 1": ("Navy", "NV"), "White Bridesmaid Dress 2": ("White", "WH"),
                    "Beige Bridesmaid Dress 1": ("Beige", "BG"), "Emerald Green Bridesmaid Dress 1": ("Emerald", "EM")}
        for name, pair in expected.items():
            self.assertEqual(got[name], pair, name)
        dusty = {got["Dusty Blue Bridesmaid Dress 1"], got["Dusty Blue Bridesmaid Dress 2"]}
        self.assertEqual(len(dusty), 1)                                   # one colour, one code
        self.assertEqual(dusty.pop(), ("Dusty Blue", "DB"))
        self.assertEqual(got["Old Rose Bridesmaid Dress 1"], ("Old Rose", "OL"))   # OR already means Orange, so not OR

    def test_every_colour_has_its_own_code_and_every_code_one_colour(self):
        self.run_command()
        pairs = set(self.dresses().values_list("color_name", "color_code"))
        self.assertEqual(len({c for _n, c in pairs}), len(pairs))
        self.assertEqual(len({n for n, _c in pairs}), len(pairs))

    def test_two_new_colours_in_one_run_never_pick_the_same_code_even_in_a_dry_run(self):
        Gown.objects.create(gown_id="X-DU-001", name="Denim", category=Gown.Category.LONG_GOWN, color_name="Denim Blue", color_code="DB",
                            size=Gown.Size.MEDIUM, rental_price=1)
        out, _err = self.run_command("--dry-run")
        self.assertIn("Dusty Blue / DU", out)                             # DB already means Denim Blue, so Dusty Blue uses DU
        self.assertIn("Old Rose / OL", out)
        self.assertEqual(self.dresses().count(), 0)

    def test_an_existing_code_for_the_colour_is_reused(self):
        Gown.objects.create(gown_id="X-OS-001", name="Rosy", category=Gown.Category.LONG_GOWN, color_name="Old Rose", color_code="RO",
                            size=Gown.Size.MEDIUM, rental_price=1)
        self.run_command()
        self.assertEqual(set(self.dresses().filter(color_name="Old Rose").values_list("color_code", flat=True)), {"RO"})

    def test_tag_numbers_count_across_the_whole_category_from_one(self):
        self.run_command()
        numbers = sorted(int(g.gown_id.rsplit("-", 1)[1]) for g in self.dresses())
        self.assertEqual(numbers, list(range(1, 31)))
        self.assertTrue(all(g.gown_id.startswith(f"{CATEGORY}-") for g in self.dresses()))

    def test_one_photo_upload_per_dress_shared_by_its_two_sizes(self):
        self.run_command()
        self.assertEqual(len(self.uploads), 15)
        for name, photo, _color in BRIDESMAIDS:
            urls = set(self.dresses().filter(name=name).values_list("photo_url", flat=True))
            self.assertEqual(urls, {f"https://photos.test/gown_photos/{photo.lower().replace(' ', '-')}"})

    def test_running_it_again_adds_nothing_and_a_dry_run_changes_nothing(self):
        self.run_command()
        ids = set(self.dresses().values_list("gown_id", flat=True))
        uploads = len(self.uploads)
        out, _err = self.run_command()
        self.assertEqual(set(self.dresses().values_list("gown_id", flat=True)), ids)
        self.assertEqual(len(self.uploads), uploads)
        self.assertIn("Added 0 gown(s); 30 size(s) were already there.", out)
        self.assertIn("already has Large and Extra Large -- left alone.", out)
        self.dresses().filter(name="Beige Bridesmaid Dress 1", size="Extra Large").delete()
        out, _err = self.run_command("--dry-run")
        self.assertEqual(self.dresses().count(), 29)
        self.assertIn("Would add 1 gown(s); 29 size(s) were already there.", out)

    def test_a_missing_size_is_added_and_a_missing_photo_stops_only_that_dress(self):
        (Path(self.folder.name) / "Bridesmaid Dresses 09.jpg").unlink()
        with self.assertRaises(CommandError) as raised:
            self.run_command()
        self.assertIn("1 Bridesmaid dress(s) could not be added", str(raised.exception))
        self.assertEqual(self.dresses().count(), 28)
        self.assertFalse(self.dresses().filter(name="Black Bridesmaid Dress 3").exists())

    def test_the_barongs_and_the_bridesmaids_keep_separate_tag_numbers(self):
        # the Barong command needs its own photos
        from gowns.test_add_barongs import make_photos as barong_photos
        barong_folder = tempfile.TemporaryDirectory()
        self.addCleanup(barong_folder.cleanup)
        barong_photos(barong_folder.name)
        call_command("add_barongs", f"--photos={barong_folder.name}", stdout=io.StringIO(), stderr=io.StringIO())
        self.run_command()
        self.assertEqual(Gown.objects.filter(category=Gown.Category.BARONG).count(), 21)
        self.assertEqual(self.dresses().count(), 30)
        self.assertEqual(min(int(g.gown_id.rsplit("-", 1)[1]) for g in self.dresses()), 1)

    def test_the_customer_site_shows_each_dress_once_with_l_and_xl_clickable_and_s_m_greyed(self):
        self.run_command()
        page = self.client.get("/collections/bridesmaid-dresses/")
        self.assertEqual(page.status_code, 200)
        for name, _photo, _color in BRIDESMAIDS:
            self.assertContains(page, escape(name))
        hrefs = {h for h in page.content.decode().split('"') if h.startswith("/collections/bridesmaid-dresses/products/")}
        self.assertEqual(len(hrefs), 15)
        slug = self.dresses().get(name="Black Bridesmaid Dress 3", size="Large").slug
        html = self.client.get(f"/collections/bridesmaid-dresses/products/{slug}/").content.decode()
        self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', html), ["L", "XL"])
        self.assertEqual(re.findall(r'data-size-not-offered="([A-Z]+)"', html), ["S", "M", "XXL"])
        self.assertIn("Black Bridesmaid Dress 3", html)

    def test_search_finds_them_by_colour(self):
        self.run_command()
        html = self.client.get("/search/?q=dusty").content.decode()
        body = html[html.index("<main"):html.index("</main>")]            # the results only (the page also carries the search overlay's featured list)
        self.assertIn("Dusty Blue Bridesmaid Dress 1", body)
        self.assertIn("Dusty Blue Bridesmaid Dress 2", body)
        self.assertNotIn("Black Bridesmaid Dress", body)
