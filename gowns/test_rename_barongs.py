"""python manage.py rename_barongs: Barong 1-7 (the photo numbers they were first added under) become Beige Barong 1-3, White Barong 1-3 and
White & Light Blue Barong, every size of a design together, and nothing else about them changes.

The owner's table named the gowns by colour. Gowns with the same name are ONE product on the customer site (group_gowns_by_name), so plain
"Beige" x3 would merge three designs into one card with one photo -- the names carry a number per colour to stay distinct. The shop already
had the Barongs under their first names, so the live fix is a rename, never a re-add (that would make 21 duplicates)."""
import io
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from django.utils.html import escape

from gowns.management.commands.add_barongs import BARONGS, FORMER_NAMES
from gowns.models import Gown
from gowns.test_add_barongs import SAVE, make_photos
from reservations.models import Reservation, ReservationItem

CODES = {"Beige": "BG", "White": "WH", "White & Light Blue": "WL"}


def make_old_barongs():
    """The 21 Barongs as the live shop has them: first names, three sizes each, a shared photo per design, slugs from those names."""
    number = 0
    for new_name, photo, color in BARONGS:
        for size in (Gown.Size.SMALL, Gown.Size.MEDIUM, Gown.Size.LARGE):
            number += 1
            Gown.objects.create(
                gown_id=f"Barong-{CODES[color]}-{number:03d}", name=FORMER_NAMES[new_name], category=Gown.Category.BARONG,
                color_name=color, color_code=CODES[color], size=size, rental_price=Decimal("1000"), condition=Gown.Condition.GOOD,
                status=Gown.Status.AVAILABLE, photo_url=f"https://photos.test/{photo.lower().replace(' ', '-')}")


def snapshot():
    return {g.gown_id: {k: getattr(g, k) for k in ("slug", "color_name", "color_code", "size", "rental_price", "condition", "status",
                                                    "photo_url", "category", "design_variant", "notes", "is_verified")}
            for g in Gown.objects.filter(category=Gown.Category.BARONG)}


class TheNamesTests(SimpleTestCase):
    def test_every_design_has_its_own_name_so_no_two_merge_into_one_product(self):
        names = [name.lower() for name, _photo, _color in BARONGS]
        self.assertEqual(len(set(names)), 7)

    def test_the_colour_is_in_the_name_and_a_colour_with_several_designs_is_numbered(self):
        for name, _photo, color in BARONGS:
            self.assertIn(color.split(" &")[0].lower(), name.lower(), name)
        self.assertEqual([n for n, _p, c in BARONGS if c == "Beige"], ["Beige Barong 1", "Beige Barong 2", "Beige Barong 3"])
        self.assertEqual([n for n, _p, c in BARONGS if c == "White"], ["White Barong 1", "White Barong 2", "White Barong 3"])
        self.assertEqual([n for n, _p, c in BARONGS if c == "White & Light Blue"], ["White & Light Blue Barong 1"])

    def test_the_old_names_cover_exactly_the_new_ones_and_never_overlap_them(self):
        self.assertEqual(set(FORMER_NAMES), {name for name, _p, _c in BARONGS})
        self.assertEqual(len(set(FORMER_NAMES.values())), 7)
        self.assertFalse(set(v.lower() for v in FORMER_NAMES.values()) & set(k.lower() for k in FORMER_NAMES))


class RenameBarongsTests(TestCase):
    def run_command(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        call_command("rename_barongs", *extra, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def barongs(self):
        return Gown.objects.filter(category=Gown.Category.BARONG)

    def test_every_size_of_every_design_gets_the_new_name(self):
        make_old_barongs()
        out, _err = self.run_command()
        self.assertEqual(set(self.barongs().values_list("name", flat=True)), {name for name, _p, _c in BARONGS})
        for name, _photo, _color in BARONGS:
            self.assertEqual(sorted(self.barongs().filter(name=name).values_list("size", flat=True)), ["Large", "Medium", "Small"], name)
        self.assertIn("Renamed 21 gown(s).", out)

    def test_nothing_but_the_name_changes(self):
        make_old_barongs()
        before = snapshot()
        self.run_command()
        self.assertEqual(snapshot(), before)         # IDs, slugs (so page web addresses), photos, sizes, prices, status, colour: all as they were
        self.assertEqual(self.barongs().get(gown_id="Barong-BG-001").slug, "barong-1")

    def test_the_updated_stamp_moves_with_the_name(self):
        make_old_barongs()
        Gown.objects.update(updated_at=timezone.now() - timedelta(days=30))
        stale = timezone.now() - timedelta(days=1)
        self.run_command()
        self.assertTrue(all(g.updated_at > stale for g in self.barongs()))

    def test_dry_run_changes_nothing_and_says_what_it_would_do(self):
        make_old_barongs()
        before = list(self.barongs().order_by("id").values_list("name", "updated_at"))
        out, _err = self.run_command("--dry-run")
        self.assertEqual(list(self.barongs().order_by("id").values_list("name", "updated_at")), before)
        self.assertIn("DRY RUN", out)
        self.assertIn('"Barong 1" -> "Beige Barong 1": would rename Barong-BG-001 (Small), Barong-BG-002 (Medium), Barong-BG-003 (Large)', out)
        self.assertIn("Would rename 21 gown(s).", out)

    def test_running_it_again_changes_nothing(self):
        make_old_barongs()
        self.run_command()
        done = snapshot()
        names = list(self.barongs().order_by("id").values_list("name", "updated_at"))
        out, _err = self.run_command()
        self.assertEqual(snapshot(), done)
        self.assertEqual(list(self.barongs().order_by("id").values_list("name", "updated_at")), names)
        self.assertIn("Renamed 0 gown(s).", out)
        self.assertEqual(out.count("already has its name"), 7)

    def test_a_half_done_shop_finishes_the_rest(self):
        make_old_barongs()
        self.barongs().filter(name="Barong 3").update(name="Beige Barong 3")
        out, _err = self.run_command()
        self.assertEqual(set(self.barongs().values_list("name", flat=True)), {name for name, _p, _c in BARONGS})
        self.assertIn("Renamed 18 gown(s).", out)

    def test_old_names_are_found_whatever_their_capitals(self):
        make_old_barongs()
        self.barongs().filter(name="Barong 1").update(name="barong 1")
        self.run_command()
        self.assertEqual(self.barongs().filter(name="Beige Barong 1").count(), 3)

    def test_a_design_whose_new_name_is_already_taken_is_refused_and_the_rest_still_go(self):
        make_old_barongs()
        Gown.objects.create(gown_id="Barong-BG-099", name="Beige Barong 2", category=Gown.Category.BARONG, color_name="Beige",
                            color_code="BG", size=Gown.Size.SMALL, rental_price=Decimal("1000"))
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual(self.barongs().filter(name="Barong 2").count(), 3)             # untouched: renaming would have merged two products
        self.assertEqual(self.barongs().filter(name="Beige Barong 2").count(), 1)       # and the one that was already there stays alone
        self.assertEqual(self.barongs().filter(name="Beige Barong 1").count(), 3)       # every other design was renamed

    def test_a_design_whose_colour_no_longer_matches_is_refused(self):
        make_old_barongs()
        self.barongs().filter(gown_id="Barong-BG-005").update(color_name="Red", color_code="RD")     # Barong 2's Medium was recoloured
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual(self.barongs().filter(name="Barong 2").count(), 3)             # all three sizes stay together under the old name
        self.assertEqual(self.barongs().filter(name="Beige Barong 1").count(), 3)

    def test_only_barongs_are_touched(self):
        make_old_barongs()
        Gown.objects.create(gown_id="WG-WH-001", name="Barong 1", category=Gown.Category.WEDDING_GOWN, color_name="White", color_code="WH",
                            size=Gown.Size.MEDIUM, rental_price=Decimal("5000"))
        Gown.objects.create(gown_id="Barong-BG-098", name="Heirloom Barong", category=Gown.Category.BARONG, color_name="Beige",
                            color_code="BG", size=Gown.Size.MEDIUM, rental_price=Decimal("1000"))
        self.run_command()
        self.assertEqual(Gown.objects.get(gown_id="WG-WH-001").name, "Barong 1")
        self.assertEqual(Gown.objects.get(gown_id="Barong-BG-098").name, "Heirloom Barong")

    def test_a_shop_with_no_barongs_is_not_an_error(self):
        out, err = self.run_command()
        self.assertEqual(err, "")
        self.assertEqual(out.count("nothing to rename"), 7)
        self.assertIn("Renamed 0 gown(s).", out)

    def test_a_booking_keeps_pointing_at_its_exact_gown(self):
        make_old_barongs()
        medium = self.barongs().get(gown_id="Barong-BG-002")
        customer = get_user_model().objects.create_user("rename_customer", password="x")
        reservation = Reservation.objects.create(customer=customer, customer_name="Rename Customer", status=Reservation.Status.CONFIRMED)
        day = date.today() + timedelta(days=20)
        item = ReservationItem.objects.create(reservation=reservation, gown=medium, gown_name="Barong 1", size="Medium", rental_date=day,
                                              event_date=day + timedelta(days=2), return_date=day + timedelta(days=4), overdue_date=day + timedelta(days=4))
        self.run_command()
        item.refresh_from_db()
        self.assertEqual(item.gown_id, medium.id)
        self.assertEqual(item.gown.gown_id, "Barong-BG-002")
        self.assertEqual(item.gown.name, "Beige Barong 1")
        self.assertEqual(item.gown_name, "Barong 1")             # the name printed on that booking at the time stays as it was

    def test_the_customer_site_shows_the_new_names_one_product_per_design_and_old_links_still_work(self):
        make_old_barongs()
        self.run_command()
        page = self.client.get("/collections/barong/")
        self.assertEqual(page.status_code, 200)
        for name, _photo, _color in BARONGS:
            self.assertContains(page, escape(name))               # the page escapes the & of "White & Light Blue Barong"; the browser shows it as &
        for old in ("Barong 4", "Barong 5", "Barong 6", "Barong 7"):
            self.assertNotContains(page, old)
        hrefs = {h for h in page.content.decode().split('"') if h.startswith("/collections/barong/products/")}
        self.assertEqual(len(hrefs), 7)
        old_link = self.client.get("/collections/barong/products/barong-1/")             # a link saved before the rename
        self.assertEqual(old_link.status_code, 200)
        self.assertContains(old_link, "Beige Barong 1")
        self.assertContains(self.client.get("/collections/barong/products/barong-7/"), escape("White & Light Blue Barong 1"))
        for size in ("S", "M", "L"):
            self.assertContains(old_link, f'data-product-size="{size}"')


class AddBarongsAndTheRenameTests(TestCase):
    """add_barongs must never make a second copy of a design the shop already has under its first name."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        make_photos(self.folder.name)
        patcher = mock.patch(SAVE, side_effect=lambda f: f"https://photos.test/gown_photos/{f.name}")
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self):
        out, err = io.StringIO(), io.StringIO()
        try:
            call_command("add_barongs", f"--photos={self.folder.name}", stdout=out, stderr=err)
        except CommandError as exc:
            return out.getvalue(), err.getvalue() + str(exc)
        return out.getvalue(), err.getvalue()

    def test_adding_before_the_rename_is_refused_and_adds_nothing(self):
        make_old_barongs()
        _out, err = self.add()
        self.assertEqual(Gown.objects.filter(category=Gown.Category.BARONG).count(), 21)             # not 42
        self.assertIn('still has this design under its earlier name "Barong 1". Run rename_barongs first', err)
        self.assertEqual(err.count("Run rename_barongs first"), 7)

    def test_the_dry_run_tells_the_same_truth(self):
        make_old_barongs()
        out, err = io.StringIO(), io.StringIO()
        with self.assertRaises(CommandError):
            call_command("add_barongs", f"--photos={self.folder.name}", "--dry-run", stdout=out, stderr=err)
        self.assertIn("Run rename_barongs first", err.getvalue())
        self.assertEqual(Gown.objects.filter(category=Gown.Category.BARONG).count(), 21)

    def test_adding_after_the_rename_adds_nothing_because_every_design_is_already_there(self):
        make_old_barongs()
        call_command("rename_barongs", stdout=io.StringIO(), stderr=io.StringIO())
        out, err = self.add()
        self.assertEqual(Gown.objects.filter(category=Gown.Category.BARONG).count(), 21)
        self.assertIn("Added 0 gown(s); 21 size(s) were already there.", out)
        self.assertEqual(err, "")

    def test_a_fresh_shop_gets_the_new_names_straight_away(self):
        out, _err = self.add()
        self.assertEqual(set(Gown.objects.filter(category=Gown.Category.BARONG).values_list("name", flat=True)),
                         {name for name, _p, _c in BARONGS})
        self.assertIn("Added 21 gown(s)", out)
