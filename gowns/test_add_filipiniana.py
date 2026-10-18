"""python manage.py add_filipiniana: the owner's 30 Filipiniana gowns -- exactly 30 gowns (gown IDs 001 to 030), ONE physical gown per design in the owner's
own size (Large or XL) and price, named colour first ("Beige Filipiniana 3"). No extra sizes and no extra quantity. The upload is stubbed so nothing
touches Cloudinary or media."""
import io
import json
import re
import tempfile
from collections import Counter
from datetime import timedelta
from html import unescape
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from gowns.context_processors import all_categories, category_url
from gowns.management.commands.add_filipiniana import FILIPINIANAS, PRICES_BY_NAME, SIZES_BY_NAME
from gowns.models import Gown
from gowns.test_add_barongs import SAVE

CATEGORY = Gown.Category.FILIPINIANA
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"

# The owner's list, typed in separately from the command: (colour, price, size) for items 1-30.
LIST = [("Beige", 1500, "Large"), ("Beige", 3000, "XL"), ("Black and Beige", 3000, "XL"), ("Black", 1500, "Large"), ("Beige", 1500, "Large"),
        ("Gray", 3000, "XL"), ("Pink", 3000, "XL"), ("Beige", 1500, "Large"), ("Beige", 1500, "Large"), ("Beige", 1500, "Large"),
        ("Beige", 3000, "XL"), ("Beige", 1500, "Large"), ("Black and White", 3000, "XL"), ("Beige", 1500, "Large"), ("Pink", 1500, "Large"),
        ("Pink", 1500, "Large"), ("Black", 3000, "XL"), ("Peach", 3000, "XL"), ("Pink", 3000, "XL"), ("Cream", 1500, "Large"),
        ("Cream", 3000, "XL"), ("Navy Blue", 3000, "XL"), ("Black", 3000, "XL"), ("Gold", 3000, "XL"), ("Multicolored", 3000, "XL"),
        ("Silver", 3000, "XL"), ("Blue", 3000, "XL"), ("Silver", 3000, "XL"), ("Black and Purple", 3000, "XL"), ("Red", 3000, "XL")]
SIZE_NAME = {"Large": "Large", "XL": "Extra Large"}
FIELD_COLOUR = {"Gray": "Grey", "Navy Blue": "Navy", "Multicolored": "Multicolor"}


def make_photos(folder):
    for _n, photo, *_ in FILIPINIANAS:
        buffer = io.BytesIO()
        Image.new("RGB", (40, 40), (200, 180, 150)).save(buffer, "JPEG")
        (Path(folder) / photo).write_bytes(buffer.getvalue())


class TheListTests(SimpleTestCase):
    def test_thirty_gowns_with_the_photo_of_their_own_item(self):
        self.assertEqual(len(FILIPINIANAS), 30)
        self.assertEqual([photo for _n, photo, *_ in FILIPINIANAS], [f"Filipiniana {n:02d}.jpg" for n in range(1, 31)])

    def test_every_colour_size_and_price_is_exactly_the_owners(self):
        for index, (name, _photo, field, size, price) in enumerate(FILIPINIANAS):
            colour, want_price, want_size = LIST[index]
            self.assertEqual(field, FIELD_COLOUR.get(colour, colour), f"item {index + 1}")
            self.assertEqual(int(price), want_price, f"item {index + 1}")
            self.assertEqual(str(SIZES_BY_NAME[name][0]), SIZE_NAME[want_size], f"item {index + 1}")
            self.assertTrue(name.startswith(colour + " Filipiniana "), name)

    def test_every_gown_has_exactly_one_size_so_there_are_30_gowns_in_all(self):
        for name, sizes in SIZES_BY_NAME.items():
            self.assertEqual(len(sizes), 1, name)
        self.assertEqual(Counter(str(s[0]) for s in SIZES_BY_NAME.values()), {"Large": 11, "Extra Large": 19})
        self.assertEqual({int(PRICES_BY_NAME[n]) for n, s in SIZES_BY_NAME.items() if str(s[0]) == "Large"}, {1500})
        self.assertEqual({int(PRICES_BY_NAME[n]) for n, s in SIZES_BY_NAME.items() if str(s[0]) == "Extra Large"}, {3000})

    def test_every_name_differs_and_is_numbered_per_colour_in_list_order(self):
        names = [n for n, *_ in FILIPINIANAS]
        self.assertEqual(len({n.lower() for n in names}), 30)
        by_base = {}
        for name in names:
            base, number = re.match(r"(.+) (\d+)$", name).groups()
            by_base.setdefault(base, []).append(int(number))
        for base, numbers in by_base.items():
            self.assertEqual(numbers, list(range(1, len(numbers) + 1)), base)
        self.assertEqual(len(by_base["Beige Filipiniana"]), 9)
        self.assertEqual(by_base["Red Filipiniana"], [1])                                  # a lone colour still gets its 1


class AddFilipinianaTests(TestCase):
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
        call_command("add_filipiniana", f"--photos={self.folder.name}", *extra, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def gowns(self):
        return Gown.objects.filter(category=CATEGORY)

    def test_adds_exactly_30_gowns_one_per_design_with_the_owners_size_and_price(self):
        out, err = self.run_command()
        self.assertEqual((self.gowns().count(), err), (30, ""))
        self.assertIn("Added 30 gown(s); 0 size(s) were already there.", out)
        for name, sizes in SIZES_BY_NAME.items():
            got = list(self.gowns().filter(name=name).values_list("size", "rental_price"))
            self.assertEqual(got, [(str(sizes[0]), got[0][1])], name)                      # exactly one gown, in the owner's size
            self.assertEqual(int(got[0][1]), int(PRICES_BY_NAME[name]), name)
        self.assertEqual(self.gowns().values("name").distinct().count(), 30)

    def test_gown_ids_run_001_to_030_in_the_owners_order(self):
        self.run_command()
        self.assertEqual(sorted(int(g.gown_id.rsplit("-", 1)[1]) for g in self.gowns()), list(range(1, 31)))
        for index, (name, *_rest) in enumerate(FILIPINIANAS, start=1):
            self.assertTrue(self.gowns().get(name=name).gown_id.endswith(f"-{index:03d}"), name)
        self.assertEqual(len(self.uploads), 30)

    def test_colour_names_and_codes(self):
        self.run_command()
        by_colour = {}
        for colour, code in self.gowns().values_list("color_name", "color_code"):
            by_colour.setdefault(colour, set()).add(code)
        self.assertTrue(all(len(codes) == 1 for codes in by_colour.values()), by_colour)       # one colour, one code
        self.assertEqual(len({next(iter(c)) for c in by_colour.values()}), len(by_colour))       # no code shared by two colours
        presets = {"Beige": "BG", "Black": "BK", "Pink": "PK", "Peach": "PE", "Gold": "GD", "Silver": "SV", "Blue": "BU", "Red": "RD",
                   "Grey": "GY", "Navy": "NV", "Multicolor": "MC"}
        for colour, code in presets.items():
            self.assertEqual(by_colour[colour], {code}, colour)
        self.assertEqual(by_colour["Black and Beige"], {"BB"})
        self.assertEqual(by_colour["Black and White"], {"BW"})
        self.assertEqual(by_colour["Black and Purple"], {"BP"})
        self.assertRegex(next(iter(by_colour["Cream"])), r"^[A-Z]{2}$")

    def test_run_again_adds_nothing_and_a_dry_run_changes_nothing(self):
        self.run_command()
        out, _e = self.run_command()
        self.assertIn("Added 0 gown(s); 30 size(s) were already there.", out)
        self.assertEqual(self.gowns().count(), 30)                                         # running it again never adds quantity
        self.gowns().filter(name="Red Filipiniana 1").delete()
        out, _e = self.run_command("--dry-run")
        self.assertEqual(self.gowns().count(), 29)
        self.assertIn("Would add 1 gown(s); 29 size(s) were already there.", out)

    def test_a_missing_photo_stops_only_that_gown(self):
        (Path(self.folder.name) / "Filipiniana 09.jpg").unlink()
        with self.assertRaises(CommandError) as raised:
            self.run_command()
        self.assertIn("1 Filipiniana gown(s) could not be added", str(raised.exception))
        self.assertEqual(self.gowns().count(), 29)
        self.assertFalse(self.gowns().filter(name="Beige Filipiniana 5").exists())

    def test_the_customer_site_shows_30_gowns_over_four_pages_with_only_the_one_size_clickable(self):
        self.run_command()
        self.assertFalse(Gown.objects.exclude(category=CATEGORY).exists())
        url = next(category_url(row) for row in all_categories() if row["label"] == "Filipiniana")
        seen = []
        for page in range(1, 5):
            html = self.client.get(url, {"page": page}).content.decode()
            main = html[html.index("<main"):html.index("</main>")]
            seen += [unescape(t) for t in re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', main)]
        self.assertEqual(len(seen), 30)
        self.assertEqual(set(seen), {name for name, *_ in FILIPINIANAS})
        self.assertIn("Page 4 of 4", self.client.get(url, {"page": 4}).content.decode())
        slug = self.gowns().get(name="Beige Filipiniana 1").slug                             # Large, 1,500
        html = self.client.get(f"{url}products/{slug}/").content.decode()
        self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', html), ["L"])
        self.assertEqual(re.findall(r'data-size-not-offered="([A-Z]+)"', html), ["S", "M", "XL", "XXL"])
        self.assertIn("₱1,500", html)
        slug = self.gowns().get(name="Gold Filipiniana 1").slug                              # XL, 3,000
        html = self.client.get(f"{url}products/{slug}/").content.decode()
        self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', html), ["XL"])
        self.assertIn("₱3,000", html)

    def test_a_gown_cannot_be_booked_in_a_size_it_does_not_come_in(self):
        self.run_command()
        gown = self.gowns().get(name="Red Filipiniana 1")                                    # XL only
        customer = get_user_model().objects.create_user("filipiniana_customer", password="x")
        self.client.force_login(customer)
        start = timezone.localdate() + timedelta(days=20)
        item = {"gown_name": gown.name, "gown_slug": gown.slug, "size": "L", "rental_date": start.isoformat(), "return_date": (start + timedelta(days=4)).isoformat()}

        def submit():
            with mock.patch("gowns.views._save_proof_file", return_value="https://example.test/p.jpg"):
                return self.client.post(reverse("gowns:reservation_submit"), data={
                    "items": json.dumps([item]), "first_name": "F", "last_name": "C", "phone": "09171234567", "address": "1 St", "city": "C",
                    "postal_code": "1000", "payment_method": "GCash", "proof_of_payment": SimpleUploadedFile("p.jpg", _JPEG, content_type="image/jpeg")})
        refused = submit()
        self.assertEqual(refused.status_code, 400)
        self.assertIn("doesn't come in Large", refused.json()["error"])
        item["size"] = "XL"
        self.assertEqual(submit().status_code, 200)

    def test_search_finds_a_colour(self):
        self.run_command()
        html = self.client.get("/search/", {"q": "silver"}).content.decode()
        found = {unescape(t) for t in re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', html[html.index("<main"):html.index("</main>")])}
        self.assertEqual(found, {"Silver Filipiniana 1", "Silver Filipiniana 2"})
