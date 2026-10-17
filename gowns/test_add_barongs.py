"""python manage.py add_barongs: 7 Barongs x Small/Medium/Large = 21 gowns, made the way Add Gown makes them, safe to run twice, and a
--dry-run that changes nothing. The photo upload is replaced by a stand-in so no test ever touches Cloudinary or the media folder."""
import io
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from PIL import Image

from gowns.management.commands import add_barongs
from gowns.models import Gown

SAVE = "gowns.management.commands.add_barongs._save_gown_photo"


def make_photos(folder, names=None):
    for name, photo_name, _color in add_barongs.BARONGS:
        if names is not None and photo_name not in names:
            continue
        buffer = io.BytesIO()
        Image.new("RGB", (40, 40), (230, 220, 200)).save(buffer, "JPEG")
        (Path(folder) / photo_name).write_bytes(buffer.getvalue())


class AddBarongsTests(TestCase):
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
        out = io.StringIO()
        call_command("add_barongs", f"--photos={self.folder.name}", *extra, stdout=out, stderr=io.StringIO())
        return out.getvalue()

    def barongs(self):
        return Gown.objects.filter(category=Gown.Category.BARONG)

    def test_adds_seven_designs_in_three_sizes_each(self):
        self.run_command()
        self.assertEqual(self.barongs().count(), 21)
        for name, _photo, _color in add_barongs.BARONGS:
            with self.subTest(name=name):
                sizes = set(self.barongs().filter(name=name).values_list("size", flat=True))
                self.assertEqual(sizes, {"Small", "Medium", "Large"})

    def test_colours_codes_price_and_defaults(self):
        self.run_command()
        expected = {"Beige Barong 1": ("Beige", "BG"), "Beige Barong 3": ("Beige", "BG"), "White Barong 1": ("White", "WH"),
                    "White Barong 3": ("White", "WH"), "White & Light Blue Barong 1": ("White & Light Blue", "WL")}
        for name, (color, code) in expected.items():
            for gown in self.barongs().filter(name=name):
                with self.subTest(name=name, size=gown.size):
                    self.assertEqual((gown.color_name, gown.color_code), (color, code))
                    self.assertEqual(int(gown.rental_price), 1000)
                    self.assertEqual(gown.status, Gown.Status.AVAILABLE)
                    self.assertEqual(gown.condition, Gown.Condition.GOOD)
                    self.assertTrue(gown.gown_id.startswith(f"Barong-{code}-"))

    def test_each_tag_number_is_unique_and_counts_across_the_whole_category(self):
        self.run_command()
        numbers = sorted(int(g.gown_id.rsplit("-", 1)[1]) for g in self.barongs())
        self.assertEqual(numbers, list(range(1, 22)))
        self.assertEqual(self.barongs().values("gown_id").distinct().count(), 21)

    def test_slugs_are_unique(self):
        self.run_command()
        slugs = list(self.barongs().values_list("slug", flat=True))
        self.assertEqual(len(set(slugs)), 21)
        self.assertTrue(all(slugs))

    def test_one_photo_upload_per_design_shared_by_its_three_sizes(self):
        self.run_command()
        self.assertEqual(len(self.uploads), 7)
        for name, photo_name, _color in add_barongs.BARONGS:
            urls = set(self.barongs().filter(name=name).values_list("photo_url", flat=True))
            with self.subTest(name=name):
                self.assertEqual(urls, {f"https://photos.test/gown_photos/{photo_name.lower().replace(' ', '-')}"})

    def test_the_customer_list_shows_each_design_once_with_all_three_sizes(self):
        self.run_command()
        response = self.client.get("/collections/barong/")
        self.assertEqual(response.status_code, 200)
        for name, _photo, _color in add_barongs.BARONGS:
            self.assertContains(response, f"/collections/barong/products/{self.barongs().filter(name=name).order_by('id').first().slug}/")
        page = self.client.get(f"/collections/barong/products/{self.barongs().get(name='Beige Barong 1', size='Small').slug}/")
        self.assertEqual(page.status_code, 200)
        for size in ("S", "M", "L"):
            self.assertContains(page, f'data-product-size="{size}"')

    def test_running_it_twice_adds_nothing_the_second_time(self):
        self.run_command()
        ids = set(self.barongs().values_list("gown_id", flat=True))
        uploads = len(self.uploads)
        output = self.run_command()
        self.assertEqual(self.barongs().count(), 21)
        self.assertEqual(set(self.barongs().values_list("gown_id", flat=True)), ids)
        self.assertEqual(len(self.uploads), uploads)
        self.assertIn("Added 0 gown(s); 21 size(s) were already there.", output)

    def test_a_missing_size_is_added_and_reuses_the_photo_already_on_file(self):
        self.run_command()
        large = self.barongs().get(name="Beige Barong 2", size="Large")
        photo = large.photo_url
        large.delete()
        uploads = len(self.uploads)
        output = self.run_command()
        self.assertEqual(self.barongs().count(), 21)
        self.assertEqual(self.barongs().get(name="Beige Barong 2", size="Large").photo_url, photo)
        self.assertEqual(len(self.uploads), uploads)               # no new upload for a size added later
        self.assertIn("Added 1 gown(s); 20 size(s) were already there.", output)

    def test_dry_run_changes_nothing_and_uploads_nothing(self):
        output = self.run_command("--dry-run")
        self.assertEqual(self.barongs().count(), 0)
        self.assertEqual(self.uploads, [])
        self.assertIn("DRY RUN", output)
        self.assertIn("Would add 21 gown(s)", output)

    def test_a_missing_photo_stops_only_that_design_and_reports_it(self):
        (Path(self.folder.name) / "Barong 05.jpg").unlink()
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual(self.barongs().count(), 18)                # the other six designs went in
        self.assertFalse(self.barongs().filter(name="White Barong 2").exists())

    def test_a_missing_photo_folder_is_refused_before_anything_happens(self):
        with self.assertRaises(CommandError):
            call_command("add_barongs", "--photos=" + str(Path(self.folder.name) / "nope"), stdout=io.StringIO())
        self.assertEqual(self.barongs().count(), 0)

    def test_a_failed_upload_leaves_nothing_behind_for_that_design(self):
        with mock.patch(SAVE, side_effect=RuntimeError("storage is down")):
            with self.assertRaises(CommandError):
                self.run_command()
        self.assertEqual(self.barongs().count(), 0)

    def test_another_colour_already_using_the_code_is_never_reused(self):
        Gown.objects.create(gown_id="Gown-WL-001", name="Other", category="Wedding Gown", color_name="Wine Lilac", color_code="WL",
                            size=Gown.Size.MEDIUM, rental_price=1, status=Gown.Status.AVAILABLE)
        self.run_command()
        codes = set(self.barongs().filter(name="White & Light Blue Barong 1").values_list("color_code", flat=True))
        self.assertEqual(codes, {"WB"})                              # WL already means a different colour, so the next free one is used
