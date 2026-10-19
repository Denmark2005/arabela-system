"""python manage.py add_kids_gowns: the owner's 33 Kids Gowns -- exactly 33 gowns (gown IDs 001 to 033), ONE physical gown per design with ONE size
(a kids' age band) and its own price, named colour first ("Pink Kids Gown 3"); no extra sizes and no extra quantity (the first version gave
many gowns several sizes, which made 71 -- the owner meant only the 33). The upload is stubbed so nothing touches Cloudinary or media."""
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
from gowns.management.commands.add_kids_gowns import AGE_BANDS, KIDS_GOWNS, PRICES_BY_NAME, SIZES_BY_NAME
from gowns.models import Gown
from gowns.test_add_barongs import SAVE

CATEGORY = Gown.Category.KIDS_GOWN
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"

# The owner's list, item by item: colour and price exactly as written.
LIST = [("Red", 2500), ("Rose Gold", 2500), ("White", 1800), ("Lavender", 2800), ("Pink", 2800), ("Red", 2800), ("Rose", 2200), ("Pink", 2500),
        ("Violet", 3000), ("Pink", 3000), ("Blue", 3000), ("Lavender", 2500), ("Red", 2800), ("Pink", 2500), ("Pink", 2800), ("Peach", 2200),
        ("Pink and Violet", 2800), ("Yellow", 2500), ("Pink", 2800), ("Lavender", 3000), ("Yellow", 3000), ("Maroon", 2800),
        ("Pink and Blue", 2800), ("Pink", 2500), ("Purple", 3000), ("Pink", 2800), ("Blue", 2200), ("Pink", 2000), ("Purple", 2200),
        ("Mint Green", 2200), ("Pink", 2800), ("Purple", 2800), ("Blue", 2800)]


def make_photos(folder):
    for _n, photo, *_ in KIDS_GOWNS:
        buffer = io.BytesIO()
        Image.new("RGB", (40, 40), (240, 190, 210)).save(buffer, "JPEG")
        (Path(folder) / photo).write_bytes(buffer.getvalue())


class TheListTests(SimpleTestCase):
    def test_thirty_three_gowns_with_the_photo_of_their_own_item(self):
        self.assertEqual(len(KIDS_GOWNS), 33)
        self.assertEqual([photo for _n, photo, *_ in KIDS_GOWNS], [f"Kids Gown {n:02d}.jpg" for n in range(1, 34)])

    def test_every_colour_and_price_is_exactly_the_owners(self):
        for index, (name, _photo, color, _sizes, price) in enumerate(KIDS_GOWNS):
            want_color, want_price = LIST[index]
            shown = "Mint Green" if color == "Mint" else color                          # "Mint Green" is the preset Mint in the colour field
            self.assertEqual(shown, want_color, f"item {index + 1}")
            self.assertEqual(int(price), want_price, f"item {index + 1}")
            self.assertTrue(name.startswith(want_color + " Kids Gown "), name)

    def test_every_name_differs_and_is_numbered_per_colour_in_list_order(self):
        names = [n for n, *_ in KIDS_GOWNS]
        self.assertEqual(len({n.lower() for n in names}), 33)
        by_base = {}
        for name in names:
            base, number = re.match(r"(.+) (\d+)$", name).groups()
            by_base.setdefault(base, []).append(int(number))
        for base, numbers in by_base.items():
            self.assertEqual(numbers, list(range(1, len(numbers) + 1)), base)
        self.assertEqual(len(by_base["Pink Kids Gown"]), 10)                              # ten pink gowns -> 1..10
        self.assertEqual(by_base["Mint Green Kids Gown"], [1])                           # a lone colour still gets its 1

    def test_every_gown_has_exactly_one_size_so_there_are_33_gowns_in_all(self):
        for name, sizes in SIZES_BY_NAME.items():
            self.assertEqual(len(sizes), 1, name)                                          # one size each: no extra quantity
        self.assertEqual(sum(len(s) for s in SIZES_BY_NAME.values()), 33)
        self.assertEqual(Counter(s[0] for s in SIZES_BY_NAME.values()),
                         {Gown.Size.SMALL: 8, Gown.Size.MEDIUM: 6, Gown.Size.LARGE: 13, Gown.Size.EXTRA_LARGE: 6})   # the owner's own sizes

    def test_every_size_is_exactly_the_one_the_owner_gave_for_that_gown(self):
        owners = ("Small Small Small Large Large Large Medium Medium XL XL XL Medium Large Medium Large Small Large Medium Large XL XL Large "
                  "Large Small XL Large Small Small Medium Small Large Large Large").split()
        owners = [{"Small": "Small", "Medium": "Medium", "Large": "Large", "XL": "Extra Large"}[w] for w in owners]
        self.assertEqual(len(owners), 33)
        for index, (name, *_rest) in enumerate(KIDS_GOWNS):
            self.assertEqual([str(s) for s in SIZES_BY_NAME[name]], [owners[index]], f"Kids Gown {index + 1:02d} {name}")

    def test_the_age_bands_are_the_ones_on_the_owners_size_chart(self):
        self.assertEqual(AGE_BANDS, {"S": "5-6 years", "M": "7-8 years", "L": "9-10 years", "XL": "11-12 years"})


class AddKidsGownsTests(TestCase):
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
        call_command("add_kids_gowns", f"--photos={self.folder.name}", *extra, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def gowns(self):
        return Gown.objects.filter(category=CATEGORY)

    def test_adds_exactly_33_gowns_one_per_design_each_with_its_own_size_and_price(self):
        out, err = self.run_command()
        self.assertEqual((self.gowns().count(), err), (33, ""))
        self.assertIn("Added 33 gown(s); 0 size(s) were already there.", out)
        for name, sizes in SIZES_BY_NAME.items():
            got = list(self.gowns().filter(name=name).order_by("id").values_list("size", flat=True))
            self.assertEqual(got, [str(s) for s in sizes], name)                          # exactly one gown per design, in its one size
            self.assertEqual({int(g.rental_price) for g in self.gowns().filter(name=name)}, {int(PRICES_BY_NAME[name])}, name)
        self.assertEqual(self.gowns().values("name").distinct().count(), 33)

    def test_colour_names_and_codes(self):
        self.run_command()
        by_colour = {}
        for colour, code in self.gowns().values_list("color_name", "color_code"):
            by_colour.setdefault(colour, set()).add(code)
        self.assertTrue(all(len(codes) == 1 for codes in by_colour.values()), by_colour)       # one colour, one code
        self.assertEqual(len({next(iter(c)) for c in by_colour.values()}), len(by_colour))       # no code shared by two colours
        presets = {"Red": "RD", "Rose Gold": "RG", "White": "WH", "Lavender": "LV", "Pink": "PK", "Blue": "BU", "Peach": "PE",
                   "Yellow": "YL", "Maroon": "MR", "Purple": "PU", "Mint": "MT"}
        for colour, code in presets.items():
            self.assertEqual(by_colour[colour], {code}, colour)
        self.assertEqual(by_colour["Pink and Violet"], {"PV"})
        self.assertEqual(by_colour["Pink and Blue"], {"PB"})
        for colour in ("Rose", "Violet"):
            self.assertRegex(next(iter(by_colour[colour])), r"^[A-Z]{2}$")

    def test_gown_ids_run_001_to_033_in_the_owners_order_and_one_photo_per_gown(self):
        self.run_command()
        self.assertEqual(sorted(int(g.gown_id.rsplit("-", 1)[1]) for g in self.gowns()), list(range(1, 34)))
        for index, (name, *_rest) in enumerate(KIDS_GOWNS, start=1):                      # list item n is gown number n
            self.assertTrue(self.gowns().get(name=name).gown_id.endswith(f"-{index:03d}"), name)
        self.assertEqual(len(self.uploads), 33)
        for name, photo, *_ in KIDS_GOWNS:
            urls = set(self.gowns().filter(name=name).values_list("photo_url", flat=True))
            self.assertEqual(urls, {f"https://photos.test/gown_photos/{photo.lower().replace(' ', '-')}"})

    def test_run_again_adds_nothing_and_a_dry_run_changes_nothing(self):
        self.run_command()
        out, _e = self.run_command()
        self.assertIn("Added 0 gown(s); 33 size(s) were already there.", out)
        self.assertEqual(self.gowns().count(), 33)                                         # running it again never adds quantity
        self.gowns().filter(name="Lavender Kids Gown 1").delete()
        out, _e = self.run_command("--dry-run")
        self.assertEqual(self.gowns().count(), 32)
        self.assertIn("Would add 1 gown(s); 32 size(s) were already there.", out)

    def test_a_missing_photo_stops_only_that_gown(self):
        (Path(self.folder.name) / "Kids Gown 09.jpg").unlink()
        with self.assertRaises(CommandError) as raised:
            self.run_command()
        self.assertIn("1 Kids gown(s) could not be added", str(raised.exception))
        self.assertFalse(self.gowns().filter(name="Violet Kids Gown 1").exists())
        self.assertEqual(self.gowns().count(), 32)

    def test_the_customer_site_shows_33_gowns_over_five_pages_with_only_each_gowns_own_sizes_clickable(self):
        self.run_command()
        self.assertFalse(Gown.objects.exclude(category=CATEGORY).exists())
        url = next(category_url(row) for row in all_categories() if row["label"] == "Kids Gown")
        seen = []
        for page in range(1, 6):
            html = self.client.get(url, {"page": page}).content.decode()
            main = html[html.index("<main"):html.index("</main>")]
            seen += [unescape(t) for t in re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', main)]
        self.assertEqual(len(seen), 33)
        self.assertEqual(set(seen), {name for name, *_ in KIDS_GOWNS})
        self.assertIn("Page 5 of 5", self.client.get(url, {"page": 5}).content.decode())
        slug = self.gowns().filter(name="Red Kids Gown 1").first().slug                    # Small only
        html = self.client.get(f"{url}products/{slug}/").content.decode()
        self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', html), ["S"])
        self.assertEqual(re.findall(r'data-size-not-offered="([A-Z]+)"', html), ["M", "L", "XL", "XXL"])
        self.assertIn("₱2,500", html)

    def test_a_kids_gown_cannot_be_booked_in_a_size_it_does_not_come_in(self):
        self.run_command()
        gown = self.gowns().get(name="Rose Gold Kids Gown 1")                              # Small only
        customer = get_user_model().objects.create_user("kids_customer", password="x")
        self.client.force_login(customer)
        start = timezone.localdate() + timedelta(days=20)
        item = {"gown_name": gown.name, "gown_slug": gown.slug, "size": "XL", "rental_date": start.isoformat(), "return_date": (start + timedelta(days=4)).isoformat()}

        def submit():
            with mock.patch("gowns.views._save_proof_file", return_value="https://example.test/p.jpg"):
                return self.client.post(reverse("gowns:reservation_submit"), data={
                    "items": json.dumps([item]), "first_name": "K", "last_name": "C", "phone": "09171234567", "address": "1 St", "city": "C",
                    "postal_code": "1000", "payment_method": "GCash", "proof_of_payment": SimpleUploadedFile("p.jpg", _JPEG, content_type="image/jpeg")})
        refused = submit()
        self.assertEqual(refused.status_code, 400)
        self.assertIn("doesn't come in Extra Large", refused.json()["error"])
        item["size"] = "S"
        self.assertEqual(submit().status_code, 200)

    def test_search_finds_a_colour(self):
        self.run_command()
        html = self.client.get("/search/", {"q": "lavender"}).content.decode()
        found = {unescape(t) for t in re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', html[html.index("<main"):html.index("</main>")])}
        self.assertEqual(found, {"Lavender Kids Gown 1", "Lavender Kids Gown 2", "Lavender Kids Gown 3"})
