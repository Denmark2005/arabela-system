"""A colour with only one gown still gets a "1" ("Beige Bridesmaid Dress 1", "White & Light Blue Barong 1"), so adding a second one later never
means renaming the first. A shop may already hold the lone gown under its in-between name (no number) -- or, for the Barongs, under its photo
number ("Barong 7") -- so both rename commands recognise every earlier name, and the add commands refuse to make a second copy."""
import io
import tempfile
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from gowns.management.commands import add_barongs, add_bridesmaids
from gowns.models import Gown
from gowns.test_add_barongs import SAVE

BARONG = Gown.Category.BARONG
DRESS = Gown.Category.BRIDESMAID_DRESSES


def lone(category, name, color, code, sizes, start=1):
    for i, size in enumerate(sizes, start=start):
        Gown.objects.create(gown_id=f"{category}-{code}-{i:03d}", name=name, category=category, color_name=color, color_code=code, size=size,
                            rental_price=Decimal("1800"), photo_url="https://photos.test/x.jpg")


def run(name, *extra):
    out, err = io.StringIO(), io.StringIO()
    try:
        call_command(name, *extra, stdout=out, stderr=err)
    except CommandError as exc:
        err.write(str(exc))
    return out.getvalue(), err.getvalue()


class LoneBarongTests(TestCase):
    def names(self):
        return set(Gown.objects.filter(category=BARONG).values_list("name", flat=True))

    def test_the_lone_barong_is_numbered_in_the_command_and_in_the_old_name_list(self):
        self.assertEqual(add_barongs.BARONGS[-1][0], "White & Light Blue Barong 1")
        self.assertEqual(add_barongs.FORMER_NAMES["White & Light Blue Barong 1"], "Barong 7")
        self.assertEqual(add_barongs.Command().old_names("White & Light Blue Barong 1"), ("Barong 7", "White & Light Blue Barong"))

    def test_a_shop_holding_the_in_between_name_gets_the_final_one(self):
        lone(BARONG, "White & Light Blue Barong", "White & Light Blue", "WL", [Gown.Size.SMALL, Gown.Size.MEDIUM, Gown.Size.LARGE])
        out, err = run("rename_barongs")
        self.assertEqual(self.names(), {"White & Light Blue Barong 1"})
        self.assertIn('"White & Light Blue Barong" -> "White & Light Blue Barong 1": renamed', out)
        self.assertEqual(Gown.objects.filter(name="White & Light Blue Barong 1").count(), 3)

    def test_a_shop_holding_the_photo_number_name_gets_the_final_one_directly(self):
        lone(BARONG, "Barong 7", "White & Light Blue", "WL", [Gown.Size.SMALL, Gown.Size.MEDIUM, Gown.Size.LARGE])
        run("rename_barongs")
        self.assertEqual(self.names(), {"White & Light Blue Barong 1"})

    def test_a_shop_already_final_is_left_alone(self):
        lone(BARONG, "White & Light Blue Barong 1", "White & Light Blue", "WL", [Gown.Size.SMALL])
        out, _err = run("rename_barongs")
        self.assertIn("already has its name -- left alone.", out)
        self.assertEqual(self.names(), {"White & Light Blue Barong 1"})

    def test_gowns_under_two_old_names_at_once_are_refused_not_guessed(self):
        lone(BARONG, "Barong 7", "White & Light Blue", "WL", [Gown.Size.SMALL])
        lone(BARONG, "White & Light Blue Barong", "White & Light Blue", "WL", [Gown.Size.MEDIUM], start=2)
        _out, err = run("rename_barongs")
        self.assertIn("more than one of these names", err)
        self.assertEqual(self.names(), {"Barong 7", "White & Light Blue Barong"})

    def test_adding_while_the_in_between_name_is_still_there_makes_no_second_copy(self):
        lone(BARONG, "White & Light Blue Barong", "White & Light Blue", "WL", [Gown.Size.SMALL, Gown.Size.MEDIUM, Gown.Size.LARGE])
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        from gowns.test_add_barongs import make_photos
        make_photos(folder.name)
        before = Gown.objects.count()
        with mock.patch(SAVE, side_effect=lambda f: "https://photos.test/" + f.name):
            _out, err = run("add_barongs", f"--photos={folder.name}")
        self.assertIn('earlier name "White & Light Blue Barong". Run rename_barongs first', err)
        self.assertEqual(Gown.objects.filter(name="White & Light Blue Barong 1").count(), 0)
        self.assertEqual(Gown.objects.filter(category=BARONG, name__startswith="White & Light").count(), 3)
        self.assertGreaterEqual(Gown.objects.count(), before)                           # the other six designs were still added


class LoneBridesmaidTests(TestCase):
    LONE = {
        "Sage Green Bridesmaid Dress": ("Sage Green", "SG"), "Beige Bridesmaid Dress": ("Beige", "BG"),
        "Old Rose Bridesmaid Dress": ("Old Rose", "OL"), "Emerald Green Bridesmaid Dress": ("Emerald", "EM"),
    }

    def build_in_between_shop(self):
        number = 0
        for name, (color, code) in self.LONE.items():
            lone(DRESS, name, color, code, [Gown.Size.LARGE, Gown.Size.EXTRA_LARGE], start=number + 1)
            number += 2

    def test_the_lone_dresses_are_numbered_and_the_earlier_names_are_known(self):
        names = {n for n, _p, _c in add_bridesmaids.BRIDESMAIDS}
        for earlier in self.LONE:
            self.assertNotIn(earlier, names)
            self.assertIn(earlier + " 1", names)
            self.assertEqual(add_bridesmaids.EARLIER_NAMES[earlier + " 1"], (earlier,))

    def test_the_rename_gives_each_lone_dress_its_1_and_nothing_else_changes(self):
        self.build_in_between_shop()
        before = {g.gown_id: (g.slug, g.color_code, g.size, g.rental_price, g.photo_url) for g in Gown.objects.all()}
        out, err = run("rename_bridesmaids")
        self.assertEqual(err, "")
        self.assertEqual(set(Gown.objects.values_list("name", flat=True)), {n + " 1" for n in self.LONE})
        self.assertEqual({g.gown_id: (g.slug, g.color_code, g.size, g.rental_price, g.photo_url) for g in Gown.objects.all()}, before)
        self.assertIn("Renamed 8 gown(s).", out)
        again, _ = run("rename_bridesmaids")
        self.assertIn("Renamed 0 gown(s).", again)

    def test_a_dry_run_changes_nothing(self):
        self.build_in_between_shop()
        out, _err = run("rename_bridesmaids", "--dry-run")
        self.assertEqual(set(Gown.objects.values_list("name", flat=True)), set(self.LONE))
        self.assertIn("Would rename 8 gown(s).", out)

    def test_the_numbered_colours_are_never_touched_and_a_new_second_dress_keeps_the_first_name(self):
        self.build_in_between_shop()
        lone(DRESS, "Beige Bridesmaid Dress 2", "Beige", "BG", [Gown.Size.LARGE], start=50)
        lone(DRESS, "Black Bridesmaid Dress 1", "Black", "BK", [Gown.Size.LARGE], start=60)
        run("rename_bridesmaids")
        names = set(Gown.objects.values_list("name", flat=True))
        self.assertIn("Beige Bridesmaid Dress 1", names)
        self.assertIn("Beige Bridesmaid Dress 2", names)                                # the staff-added second one is its own product
        self.assertIn("Black Bridesmaid Dress 1", names)
        self.assertEqual(Gown.objects.filter(name="Beige Bridesmaid Dress 1").count(), 2)

    def test_an_empty_shop_and_a_shop_already_final_are_not_errors(self):
        out, err = run("rename_bridesmaids")
        self.assertEqual(err, "")
        self.assertIn("Renamed 0 gown(s).", out)
        lone(DRESS, "Beige Bridesmaid Dress 1", "Beige", "BG", [Gown.Size.LARGE])
        out, err = run("rename_bridesmaids")
        self.assertEqual(err, "")
        self.assertIn("already has its name", out)

    def test_a_dress_whose_final_name_is_already_taken_is_refused(self):
        lone(DRESS, "Beige Bridesmaid Dress", "Beige", "BG", [Gown.Size.LARGE])
        lone(DRESS, "Beige Bridesmaid Dress 1", "Beige", "BG", [Gown.Size.EXTRA_LARGE], start=2)
        _out, err = run("rename_bridesmaids")
        self.assertIn("would merge two different products into one", err)
        self.assertEqual(Gown.objects.filter(name="Beige Bridesmaid Dress").count(), 1)

    def test_adding_while_an_in_between_name_is_still_there_makes_no_second_copy(self):
        self.build_in_between_shop()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        from gowns.test_add_bridesmaids import make_photos
        make_photos(folder.name)
        with mock.patch(SAVE, side_effect=lambda f: "https://photos.test/" + f.name):
            _out, err = run("add_bridesmaids", f"--photos={folder.name}")
        self.assertEqual(err.count("Run rename_bridesmaids first"), 4)
        self.assertEqual(Gown.objects.filter(name__startswith="Beige Bridesmaid Dress").count(), 2)    # no "... 1" copy was added
        self.assertEqual(Gown.objects.filter(name="Black Bridesmaid Dress 1").count(), 2)              # the other dresses went in
