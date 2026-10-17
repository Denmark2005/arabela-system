"""python manage.py add_suits: the 23 suits from the owner's table -- 45 gowns, each suit in its own sizes and price, named with its TYPE and
colour ("Regular Collar Light Gray 1"). The upload is stubbed so nothing touches Cloudinary or the media folder."""
import io
import json
import re
import tempfile
from collections import Counter
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from html import unescape
from PIL import Image

from gowns.context_processors import all_categories, category_url
from gowns.management.commands.add_suits import PRICES_BY_NAME, SIZES_BY_NAME, SUITS
from gowns.models import Gown
from gowns.test_add_barongs import SAVE

CATEGORY = Gown.Category.SUIT
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


def make_photos(folder):
    for _n, photo, _c, _s, _p in SUITS:
        buffer = io.BytesIO()
        Image.new("RGB", (40, 40), (90, 90, 120)).save(buffer, "JPEG")
        (Path(folder) / photo).write_bytes(buffer.getvalue())


class TheTableTests(SimpleTestCase):
    def test_twenty_three_suits_with_the_photo_of_their_own_row(self):
        self.assertEqual(len(SUITS), 23)
        self.assertEqual([photo for _n, photo, *_ in SUITS], [f"Armani Suit {n:02d}.jpg" for n in range(1, 24)])

    def test_every_name_differs_and_carries_its_type(self):
        names = [name for name, *_ in SUITS]
        self.assertEqual(len({n.lower() for n in names}), 23)                              # no two suits merge into one product
        types = Counter(re.match(r"(Chinese Collar|Regular Collar|Coat Sequence) ", n).group(1) for n in names)
        self.assertEqual(types, {"Chinese Collar": 11, "Regular Collar": 9, "Coat Sequence": 3})
        for name in names:
            self.assertTrue(re.search(r" \d+$", name), name)                              # every name ends in its number (a lone one is "1")

    def test_the_numbers_count_per_name_in_table_order(self):
        by_base = {}
        for name, *_ in SUITS:
            base, number = re.match(r"(.+) (\d+)$", name).groups()
            by_base.setdefault(base, []).append(int(number))
        for base, numbers in by_base.items():
            self.assertEqual(numbers, list(range(1, len(numbers) + 1)), base)
        self.assertEqual(by_base["Chinese Collar Light Gray"], [1, 2])
        self.assertEqual(by_base["Chinese Collar Red"], [1, 2])

    def test_the_approved_sizes_total_45_gowns(self):
        sizes = [size for name in SIZES_BY_NAME for size in SIZES_BY_NAME[name]]
        self.assertEqual(len(sizes), 45)
        self.assertEqual(Counter(sizes), {Gown.Size.SMALL: 8, Gown.Size.MEDIUM: 14, Gown.Size.LARGE: 14, Gown.Size.EXTRA_LARGE: 9})
        self.assertTrue(all(len(set(v)) == len(v) and v for v in SIZES_BY_NAME.values()))   # 1-4 different sizes each

    def test_the_coat_sequence_suits_are_large_only_at_1000_and_every_other_suit_is_1500(self):
        for name, sizes in SIZES_BY_NAME.items():
            if name.startswith("Coat Sequence"):
                self.assertEqual((sizes, PRICES_BY_NAME[name]), ((Gown.Size.LARGE,), "1000"), name)
            else:
                self.assertEqual(PRICES_BY_NAME[name], "1500", name)

    def test_sizes_are_listed_smallest_to_largest(self):
        order = [Gown.Size.SMALL, Gown.Size.MEDIUM, Gown.Size.LARGE, Gown.Size.EXTRA_LARGE]
        for name, sizes in SIZES_BY_NAME.items():
            self.assertEqual(list(sizes), sorted(sizes, key=order.index), name)


class AddSuitsTests(TestCase):
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
        call_command("add_suits", f"--photos={self.folder.name}", *extra, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def suits(self):
        return Gown.objects.filter(category=CATEGORY)

    def test_adds_45_gowns_each_suit_in_its_own_sizes_and_price(self):
        out, err = self.run_command()
        self.assertEqual((self.suits().count(), err), (45, ""))
        self.assertIn("Added 45 gown(s); 0 size(s) were already there.", out)
        for name, sizes in SIZES_BY_NAME.items():
            got = list(self.suits().filter(name=name).order_by("id").values_list("size", flat=True))
            self.assertEqual(got, [str(s) for s in sizes], name)
            self.assertEqual({int(g.rental_price) for g in self.suits().filter(name=name)}, {int(PRICES_BY_NAME[name])}, name)
        self.assertEqual(Counter(self.suits().values_list("size", flat=True)), {"Small": 8, "Medium": 14, "Large": 14, "Extra Large": 9})

    def test_colour_names_and_codes(self):
        self.run_command()
        got = {n: c for n, c in self.suits().values_list("name", "color_name") for _ in [0]}
        self.assertEqual(got["Chinese Collar Navy Blue 1"], "Navy")
        self.assertEqual(got["Chinese Collar Khaki 1"], "Khaki")
        self.assertEqual(got["Regular Collar Gray 1"], "Grey")
        pairs = set(self.suits().values_list("color_name", "color_code"))
        by_colour = {}
        for colour, code in pairs:
            by_colour.setdefault(colour, set()).add(code)
        self.assertTrue(all(len(codes) == 1 for codes in by_colour.values()), by_colour)       # one colour, one code
        self.assertEqual(len({next(iter(c)) for c in by_colour.values()}), len(by_colour))       # no code shared by two colours
        presets = {"Navy": "NV", "Royal Blue": "RB", "Mint": "MT", "Black": "BK", "Red": "RD", "White": "WH", "Purple": "PU", "Grey": "GY"}
        for colour, code in presets.items():
            self.assertEqual(by_colour[colour], {code}, colour)
        for colour in ("Light Gray", "Light Blue", "Khaki", "Black & Gold", "Black, Green & Gray", "Old Rose", "Dusty Blue"):
            self.assertRegex(next(iter(by_colour[colour])), r"^[A-Z]{2}$")

    def test_old_rose_and_dusty_blue_reuse_the_codes_the_shop_already_has(self):
        for number, (colour, code) in enumerate((("Old Rose", "OL"), ("Dusty Blue", "DB")), start=1):
            Gown.objects.create(gown_id=f"BD-{code}-00{number}", name=f"{colour} Bridesmaid Dress 1", category=Gown.Category.BRIDESMAID_DRESSES,
                                color_name=colour, color_code=code, size=Gown.Size.LARGE, rental_price=1800)
        self.run_command()
        self.assertEqual(set(self.suits().filter(color_name="Old Rose").values_list("color_code", flat=True)), {"OL"})
        self.assertEqual(set(self.suits().filter(color_name="Dusty Blue").values_list("color_code", flat=True)), {"DB"})

    def test_tag_numbers_one_to_forty_five_and_one_photo_per_suit(self):
        self.run_command()
        self.assertEqual(sorted(int(g.gown_id.rsplit("-", 1)[1]) for g in self.suits()), list(range(1, 46)))
        self.assertEqual(len(self.uploads), 23)
        for name, photo, *_ in SUITS:
            urls = set(self.suits().filter(name=name).values_list("photo_url", flat=True))
            self.assertEqual(urls, {f"https://photos.test/gown_photos/{photo.lower().replace(' ', '-')}"})

    def test_run_again_adds_nothing_and_a_dry_run_changes_nothing(self):
        self.run_command()
        out, _e = self.run_command()
        self.assertIn("Added 0 gown(s); 45 size(s) were already there.", out)
        self.assertIn("already has Large -- left alone.", out)                                 # a one-size suit reads properly
        self.suits().filter(name="Chinese Collar Old Rose 1", size="Small").delete()
        out, _e = self.run_command("--dry-run")
        self.assertEqual(self.suits().count(), 44)
        self.assertIn("Would add 1 gown(s); 44 size(s) were already there.", out)

    def test_a_missing_photo_stops_only_that_suit(self):
        (Path(self.folder.name) / "Armani Suit 09.jpg").unlink()
        with self.assertRaises(CommandError) as raised:
            self.run_command()
        self.assertIn("1 Suit(s) could not be added", str(raised.exception))
        self.assertEqual(self.suits().count(), 43)
        self.assertFalse(self.suits().filter(name="Regular Collar Black 1").exists())

    def test_the_customer_site_shows_23_suits_and_only_each_suits_own_sizes_are_clickable(self):
        self.run_command()
        self.assertFalse(Gown.objects.exclude(category=CATEGORY).exists())
        url = next(category_url(row) for row in all_categories() if row["label"] == "Suit")
        seen = set()
        for page in (1, 2, 3):
            html = self.client.get(url, {"page": page}).content.decode()
            main = html[html.index("<main"):html.index("</main>")]
            seen |= set(re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', main))
        seen = {unescape(title) for title in seen}                                           # the page escapes the & of "Black & Gold"
        self.assertEqual(seen, {name for name, *_ in SUITS})                                 # all 23, each once, none missing
        slug = self.suits().filter(name="Chinese Collar Light Gray 1").first().slug
        html = self.client.get(f"{url}products/{slug}/").content.decode()
        self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', html), ["M", "XL"])
        self.assertEqual(re.findall(r'data-size-not-offered="([A-Z]+)"', html), ["S", "L"])
        self.assertIn("₱1,500", html)
        coat = self.suits().filter(name="Coat Sequence Black 1").first().slug
        html = self.client.get(f"{url}products/{coat}/").content.decode()
        self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', html), ["L"])
        self.assertIn("₱1,000", html)

    def test_search_finds_a_type_and_a_colour(self):
        self.run_command()
        def results(q):
            html = self.client.get("/search/", {"q": q}).content.decode()
            return set(re.findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', html[html.index("<main"):html.index("</main>")]))
        self.assertEqual(len(results("chinese collar")), 11)
        self.assertEqual(len(results("coat sequence")), 3)
        self.assertEqual(len(results("royal blue")), 2)

    def test_a_suit_cannot_be_booked_in_a_size_it_does_not_come_in(self):
        self.run_command()
        suit = self.suits().get(name="Regular Collar Light Blue 1")                        # Medium only
        customer = get_user_model().objects.create_user("suit_customer", password="x")
        self.client.force_login(customer)
        start = timezone.localdate() + timedelta(days=20)
        item = {"gown_name": suit.name, "gown_slug": suit.slug, "size": "L", "rental_date": start.isoformat(), "return_date": (start + timedelta(days=4)).isoformat()}
        with mock.patch("gowns.views._save_proof_file", return_value="https://example.test/p.jpg"):
            response = self.client.post(reverse("gowns:reservation_submit"), data={
                "items": json.dumps([item]), "first_name": "S", "last_name": "C", "phone": "09171234567", "address": "1 St", "city": "C",
                "postal_code": "1000", "payment_method": "GCash", "proof_of_payment": SimpleUploadedFile("p.jpg", _JPEG, content_type="image/jpeg")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("doesn't come in Large", response.json()["error"])
        item["size"] = "M"
        with mock.patch("gowns.views._save_proof_file", return_value="https://example.test/p.jpg"):
            ok = self.client.post(reverse("gowns:reservation_submit"), data={
                "items": json.dumps([item]), "first_name": "S", "last_name": "C", "phone": "09171234567", "address": "1 St", "city": "C",
                "postal_code": "1000", "payment_method": "GCash", "proof_of_payment": SimpleUploadedFile("p.jpg", _JPEG, content_type="image/jpeg")})
        self.assertEqual(ok.status_code, 200, ok.content)
