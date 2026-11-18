"""`add_gown_sizes`: grow the catalog to 650 gowns by adding the sizes a design lacks, inside the existing categories (2026-11-17).

What these keep true: the total and every category reach their exact target; each design gets ONE new size before any gets a second, the closest
size first; a new gown is a copy of the design's own gown (name, colour, price, photo) with a new size and the category's next tag number; nothing
that exists is touched; a second run adds nothing; --dry-run changes nothing; anything wrong means NOTHING is added; and the plan works on a
catalog shaped like the live one (390 gowns, 313 designs)."""
import io
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from gowns.management.commands import add_gown_sizes as command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from gowns.models import Gown, GownSequence, GownSlugSequence, group_gowns_by_name, size_codes_of
from reservations.models import Reservation, ReservationItem

S, M, L, XL = Gown.Size.SMALL, Gown.Size.MEDIUM, Gown.Size.LARGE, Gown.Size.EXTRA_LARGE
WEDDING, EVENING, KIDS, BARONG = Gown.Category.WEDDING_GOWN, Gown.Category.EVENING_GOWN, Gown.Category.KIDS_GOWN, Gown.Category.BARONG
PHOTO = "https://res.cloudinary.com/demo/image/upload/v1/gown_photos/%s.jpg"
_counter = {"n": 0}


def make(category, name, size, *, price="3000", color="White", code="WH", status=Gown.Status.AVAILABLE, photo=None):
    _counter["n"] += 1
    tag = Gown.next_tracking_number(category)
    return Gown.objects.create(
        gown_id=f"{category}-{code}-{tag:03d}", name=name, category=category, color_name=color, color_code=code, size=size, rental_price=Decimal(price),
        status=status, photo_url=photo or PHOTO % name.replace(" ", "-"), condition=Gown.Condition.GOOD)


def run(targets, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with patch.object(command.Command, "targets", targets):
        call_command("add_gown_sizes", *args, stdout=out, stderr=err, **kwargs)
    return out.getvalue()


def snapshot():
    return sorted(Gown.objects.values_list("id", "gown_id", "name", "size", "status", "rental_price", "photo_url", "slug"))


class TheTargetsTests(TestCase):
    def test_the_targets_add_up_to_650_and_cover_every_category_once(self):
        self.assertEqual(sum(command.TARGETS.values()), 650)
        self.assertEqual(command.TOTAL, 650)
        self.assertEqual(set(command.TARGETS), set(Gown.Category.values))
        self.assertEqual(len(command.TARGETS), 13)

    def test_every_target_is_at_least_the_catalog_has_today(self):
        today = {"Ball Gown Tulle": 50, "Suit": 45, "Kids Gown": 33, "Bridesmaid Dresses": 30, "Filipiniana": 30, "Guest Gown": 30, "Mother Gown": 30,
                 "Wedding Gown": 28, "Evening Gown": 25, "Long Gown": 25, "Luxury Gown": 25, "Barong": 21, "Dresses": 18}
        self.assertEqual(sum(today.values()), 390)
        for category, now in today.items():
            self.assertGreater(command.TARGETS[category], now, category)


class HowADesignGetsItsSizesTests(TestCase):
    def sizes(self, name):
        return sorted(Gown.objects.filter(name=name).values_list("size", flat=True), key=list(command.SIZE_ORDER).index)

    def test_every_design_gets_one_new_size_before_any_gets_a_second(self):
        for n in range(3):
            make(WEDDING, f"Wedding Gown {n + 1}", M)
        run({WEDDING: 3 + 3})                                          # need 3: one each
        self.assertEqual([len(self.sizes(f"Wedding Gown {n + 1}")) for n in range(3)], [2, 2, 2])
        run({WEDDING: 3 + 3 + 1})                                      # one more: the first design in tag order gets its second
        self.assertEqual([len(self.sizes(f"Wedding Gown {n + 1}")) for n in range(3)], [3, 2, 2])

    def test_the_closest_size_comes_first_and_ties_go_to_what_the_shop_rents_most(self):
        make(WEDDING, "Wedding Gown 1", M)
        run({WEDDING: 1 + 3})
        # Medium is closest to Small and Large (a tie) -> Large wins; then Extra Large (next to Large) before Small (two away from Large's side)
        self.assertEqual(self.sizes("Wedding Gown 1"), [S, M, L, XL])
        first = Gown.objects.filter(name="Wedding Gown 1").order_by("id")[1]
        self.assertEqual(first.size, L)

    def test_a_large_only_design_gets_extra_large_before_medium(self):
        make(EVENING, "Evening Gown 1", L)
        run({EVENING: 2})
        self.assertEqual(self.sizes("Evening Gown 1"), [L, XL])

    def test_when_the_category_rents_more_of_a_size_that_size_wins_a_tie(self):
        for n in range(3):
            make(EVENING, f"Evening Gown {n + 1}", M)                 # the first designs in tag order: they are served first
        for n in range(3, 8):
            make(EVENING, f"Evening Gown {n + 1}", L)
        # The Medium-only designs tie between Small and Large (both one step away): the category holds 5 Large and no Small, so Large wins
        run({EVENING: 8 + 3})
        for name in ("Evening Gown 1", "Evening Gown 2", "Evening Gown 3"):
            self.assertEqual(self.sizes(name), [M, L])

    def test_a_new_gown_is_a_copy_of_its_designs_gown_with_its_own_tag(self):
        original = make(WEDDING, "Wedding Gown 1", M, price="3500", color="Ivory", code="IV", photo=PHOTO % "ivory")
        before = Gown.objects.count()
        run({WEDDING: 2})
        new = Gown.objects.exclude(pk=original.pk).get()
        self.assertEqual(Gown.objects.count(), before + 1)
        self.assertEqual((new.name, new.category, new.color_name, new.color_code, new.rental_price, new.photo_url),
                         (original.name, original.category, original.color_name, original.color_code, original.rental_price, original.photo_url))
        self.assertNotEqual(new.size, original.size)
        self.assertEqual((new.status, new.condition), (Gown.Status.AVAILABLE, Gown.Condition.GOOD))
        self.assertEqual(new.tracking_number, original.tracking_number + 1)             # the category's next tag number
        self.assertEqual(new.gown_id, f"{WEDDING}-IV-{new.tracking_number:03d}")
        self.assertNotEqual(new.slug, original.slug)

    def test_tag_numbers_continue_after_the_highest_in_the_category(self):
        for n in range(4):
            make(EVENING, f"Evening Gown {n + 1}", L)
        highest = max(g.tracking_number for g in Gown.objects.filter(category=EVENING))
        run({EVENING: 4 + 3})
        new = sorted(g.tracking_number for g in Gown.objects.filter(category=EVENING))[4:]
        self.assertEqual(new, [highest + 1, highest + 2, highest + 3])
        self.assertEqual(len({g.gown_id for g in Gown.objects.all()}), Gown.objects.count())

    def test_only_the_four_sizes_are_ever_added(self):
        for n in range(3):
            make(KIDS, f"Kids Gown {n + 1}", S)
        run({KIDS: 3 + 9})
        self.assertEqual(set(Gown.objects.values_list("size", flat=True)), {S, M, L, XL})
        self.assertFalse(Gown.objects.filter(size__in=[Gown.Size.EXTRA_SMALL, Gown.Size.EXTRA_EXTRA_LARGE, Gown.Size.FREE_SIZE]).exists())

    def test_a_design_with_a_size_the_command_does_not_add_keeps_it_and_still_grows(self):
        make(KIDS, "Kids Gown 1", Gown.Size.EXTRA_SMALL)
        run({KIDS: 1 + 4})
        self.assertEqual(set(Gown.objects.filter(name="Kids Gown 1").values_list("size", flat=True)), {Gown.Size.EXTRA_SMALL, S, M, L, XL})

    def test_two_designs_with_the_same_name_in_different_capitals_are_one_design(self):
        make(WEDDING, "Wedding Gown 1", M)
        make(WEDDING, "WEDDING GOWN 1", L)
        run({WEDDING: 2 + 2})
        self.assertEqual(Gown.objects.filter(name__iexact="Wedding Gown 1").count(), 4)
        self.assertEqual(len(set(Gown.objects.filter(name__iexact="Wedding Gown 1").values_list("size", flat=True))), 4)

    def test_the_plan_is_the_same_every_time(self):
        for n in range(6):
            make(WEDDING, f"Wedding Gown {n + 1}", M)
        units = list(Gown.objects.all())
        first = [(t.name, size) for t, size in command.plan_category(WEDDING, units, 8)]
        second = [(t.name, size) for t, size in command.plan_category(WEDDING, list(reversed(units)), 8)]
        self.assertEqual(first, second)


class WhatIsNeverTouchedTests(TestCase):
    def test_existing_gowns_and_bookings_are_exactly_as_they_were(self):
        a = make(WEDDING, "Wedding Gown 1", M)
        b = make(WEDDING, "Wedding Gown 2", M, status=Gown.Status.RESERVED)
        reservation = Reservation.objects.create(customer_name="Maria", status=Reservation.Status.CONFIRMED)
        item = ReservationItem.objects.create(reservation=reservation, gown=a, gown_name=a.name, size="Medium",
                                              rental_date=timezone.localdate() + timedelta(days=5), return_date=timezone.localdate() + timedelta(days=9))
        before = {row[0]: row for row in snapshot()}
        run({WEDDING: 2 + 2})
        after = {row[0]: row for row in snapshot()}
        for pk, row in before.items():
            self.assertEqual(after[pk], row)
        item.refresh_from_db()
        self.assertEqual(item.gown_id, a.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Gown.Status.RESERVED)

    def test_a_design_that_is_out_of_stock_everywhere_gets_nothing(self):
        make(WEDDING, "Wedding Gown 1", M, status=Gown.Status.OUT_OF_STOCK)
        make(WEDDING, "Wedding Gown 2", M)
        run({WEDDING: 2 + 2})
        self.assertEqual(Gown.objects.filter(name="Wedding Gown 1").count(), 1)
        self.assertEqual(Gown.objects.filter(name="Wedding Gown 2").count(), 3)

    def test_a_category_at_or_above_its_target_and_a_category_not_listed_are_left_alone(self):
        for n in range(3):
            make(EVENING, f"Evening Gown {n + 1}", L)
        make(BARONG, "Barong 1", S)
        run({EVENING: 3})                                             # already there; Barong is not in the targets at all
        self.assertEqual(Gown.objects.filter(category=EVENING).count(), 3)
        self.assertEqual(Gown.objects.filter(category=BARONG).count(), 1)
        run({EVENING: 1})                                             # a target BELOW what the shop has never removes anything
        self.assertEqual(Gown.objects.filter(category=EVENING).count(), 3)


class RunningItAgainAndDryRunTests(TestCase):
    def setUp(self):
        for n in range(4):
            make(WEDDING, f"Wedding Gown {n + 1}", M)
        for n in range(3):
            make(EVENING, f"Evening Gown {n + 1}", L)
        self.targets = {WEDDING: 4 + 5, EVENING: 3 + 2}

    def test_a_second_run_adds_nothing(self):
        run(self.targets)
        self.assertEqual((Gown.objects.filter(category=WEDDING).count(), Gown.objects.filter(category=EVENING).count()), (9, 5))
        after = snapshot()
        text = run(self.targets)
        self.assertEqual(snapshot(), after)
        self.assertIn("nothing to add", text)
        self.assertIn("Added 0 gown(s)", text)

    def test_dry_run_prints_the_plan_and_changes_nothing(self):
        before = snapshot()
        text = run(self.targets, dry_run=True)
        self.assertEqual(snapshot(), before)
        self.assertIn("DRY RUN", text)
        self.assertIn("Would add 7 gown(s): 7 -> 14", text)
        self.assertIn("Wedding Gown: Wedding Gown 1 +", text)

    def test_the_real_run_reaches_each_target_exactly(self):
        text = run(self.targets)
        self.assertIn("Added 7 gown(s)", text)
        self.assertEqual(Gown.objects.count(), 14)


class AnythingWrongAddsNothingTests(TestCase):
    def test_a_category_with_too_few_empty_sizes_stops_everything(self):
        for n in range(2):
            make(WEDDING, f"Wedding Gown {n + 1}", M)
        for n in range(2):
            make(EVENING, f"Evening Gown {n + 1}", L)
        before = snapshot()
        with self.assertRaises(CommandError) as raised:
            run({WEDDING: 2 + 2, EVENING: 2 + 7})                   # Evening has only 6 empty slots (2 designs x 3 sizes)
        self.assertIn("Nothing was added", str(raised.exception))
        self.assertEqual(snapshot(), before)                        # not even the Wedding Gowns, whose part was fine

    def test_a_gown_that_cannot_be_saved_rolls_everything_back_including_the_reserved_numbers(self):
        make(WEDDING, "Wedding Gown 1", M)
        make(EVENING, "Evening Gown 1", L)
        before = snapshot()
        sequences = sorted(GownSequence.objects.values_list("category", "color_code", "next_value"))
        slug_rows = sorted(GownSlugSequence.objects.values_list("base", "next_suffix"))
        with patch.object(Gown.objects, "bulk_create", side_effect=command.IntegrityError("boom")):
            with self.assertRaises(CommandError) as raised:
                run({WEDDING: 1 + 2, EVENING: 1 + 2})
        self.assertIn("Nothing was added", str(raised.exception))
        self.assertEqual(snapshot(), before)
        self.assertEqual(sorted(GownSequence.objects.values_list("category", "color_code", "next_value")), sequences)      # no tag number was used up
        self.assertEqual(sorted(GownSlugSequence.objects.values_list("base", "next_suffix")), slug_rows)

    def test_a_tag_number_that_is_already_taken_stops_everything(self):
        make(WEDDING, "Wedding Gown 1", M)
        before = snapshot()
        with patch.object(GownSequence, "reserve_block", classmethod(lambda cls, category, count: [1] * count)):     # hands out tag 1 again
            with self.assertRaises(CommandError) as raised:
                run({WEDDING: 1 + 2})
        self.assertIn("already used", str(raised.exception))
        self.assertEqual(snapshot(), before)

    def test_a_copy_that_fails_the_checks_stops_everything(self):
        make(WEDDING, "Wedding Gown 1", M)
        make(WEDDING, "Wedding Gown 2", M)
        before = snapshot()
        with patch.object(command, "check_copy", return_value="the colour code must be 1-2 letters"):
            with self.assertRaises(CommandError) as raised:
                run({WEDDING: 2 + 2})
        self.assertIn("Nothing was added", str(raised.exception))
        self.assertEqual(snapshot(), before)


class TheCopyChecksTests(TestCase):
    """check_copy looks only at what a copy could get wrong, in memory (nothing is saved: a database refuses most of these by itself)."""

    def gown(self, **changes):
        values = dict(name="Wedding Gown 1", category=WEDDING, color_name="White", color_code="WH", design_variant="")
        values.update(changes)
        return Gown(**values)

    def test_a_normal_gown_passes_in_every_size_the_command_adds(self):
        for size in command.SIZE_ORDER:
            self.assertEqual(command.check_copy(self.gown(), size), "")

    def test_each_thing_that_would_make_a_bad_copy_is_named(self):
        cases = {
            "name": self.gown(name=""), "long name": self.gown(name="x" * 151), "colour": self.gown(color_name=""), "long colour": self.gown(color_name="x" * 41),
            "code": self.gown(color_code=""), "long code": self.gown(color_code="XXX"), "variant": self.gown(design_variant="x" * 61),
        }
        for label, gown in cases.items():
            with self.subTest(label):
                self.assertNotEqual(command.check_copy(gown, L), "")

    def test_a_size_the_command_never_adds_is_refused(self):
        for size in (Gown.Size.EXTRA_SMALL, Gown.Size.EXTRA_EXTRA_LARGE, Gown.Size.FREE_SIZE, "Huge"):
            self.assertNotEqual(command.check_copy(self.gown(), size), "")

    def test_it_costs_no_database_query(self):
        with self.assertNumQueries(0):
            command.check_copy(self.gown(), M)


class TheCustomerSiteSeesMoreSizesTests(TestCase):
    def test_one_product_with_all_its_size_buttons_and_unique_pages(self):
        make(WEDDING, "Wedding Gown 1", M)
        run({WEDDING: 1 + 3})
        group = group_gowns_by_name(list(Gown.objects.all()))
        self.assertEqual(len(group), 1)                                        # still ONE product on the customer site
        self.assertEqual(size_codes_of(list(Gown.objects.all())), ["S", "M", "L", "XL"])
        self.assertEqual(len({g.slug for g in Gown.objects.all()}), 4)


# ---- the real targets on a catalog shaped like the live one ----------------------------------------------------------------------------
def live_shaped_catalog():
    """390 gowns in 313 designs, the shape of the live shop (2026-11-17): per category the number of designs, and the sizes each design has."""
    def single(category, sizes_count, label):
        n = 0
        for size, count in sizes_count:
            for _ in range(count):
                n += 1
                make(category, f"{label} {n}", size, code="WH")

    def pairs(category, label, sets):
        for n, sizes in enumerate(sets, 1):
            for size in sizes:
                make(category, f"{label} {n}", size, code="WH")

    single(Gown.Category.BALL_GOWN_TULLE, [(L, 50)], "Ball Gown Tulle")                       # 50 designs, all Large
    pairs(Gown.Category.SUIT, "Suit", [(S, M)] * 8 + [(M, L)] * 6 + [(L, XL)] * 8 + [(XL,)] * 1)      # 23 designs, 45 gowns
    single(Gown.Category.KIDS_GOWN, [(S, 8), (M, 6), (L, 13), (XL, 6)], "Kids Gown")           # 33 designs, one size each
    pairs(Gown.Category.BRIDESMAID_DRESSES, "Bridesmaid Dress", [(L, XL)] * 15)               # 15 designs x 2
    single(Gown.Category.FILIPINIANA, [(L, 11), (XL, 19)], "Filipiniana")                      # 30 designs
    pairs(Gown.Category.GUEST_GOWN, "Guest Gown", [(L, XL)] * 13 + [(L,)] * 2 + [(XL,)] * 2)   # 17 designs, 30 gowns
    single(Gown.Category.MOTHER_GOWN, [(L, 15), (XL, 15)], "Mother Gown")                      # 30 designs
    single(Gown.Category.WEDDING_GOWN, [(M, 28)], "Wedding Gown")                              # 28 designs, all Medium
    single(Gown.Category.EVENING_GOWN, [(L, 25)], "Evening Gown")
    single(Gown.Category.LONG_GOWN, [(L, 25)], "Long Gown")
    pairs(Gown.Category.LUXURY_GOWN, "Luxury Gown", [(L, XL)] * 4 + [(L,)] * 9 + [(XL,)] * 8)  # 21 designs, 25 gowns
    pairs(Gown.Category.BARONG, "Barong", [(S, M, L)] * 7)                                     # 7 designs x 3
    pairs(Gown.Category.DRESSES, "Dress", [(L, XL)] * 9)                                       # 9 designs x 2


class TheRealTargetsOnALiveShapedCatalogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        live_shaped_catalog()

    def test_the_fixture_has_the_live_shape(self):
        self.assertEqual(Gown.objects.count(), 390)
        self.assertEqual(len({(g.category, g.name.casefold()) for g in Gown.objects.all()}), 313)

    def test_one_run_makes_every_category_and_the_total_exact(self):
        out = io.StringIO()
        call_command("add_gown_sizes", stdout=out)                   # the real targets, as shipped
        counts = {c: Gown.objects.filter(category=c).count() for c in command.TARGETS}
        self.assertEqual(counts, command.TARGETS)
        self.assertEqual(Gown.objects.count(), 650)
        self.assertIn("The catalog now holds 650", out.getvalue())

    def test_no_design_ends_up_with_a_size_twice_and_every_size_is_one_of_the_four(self):
        call_command("add_gown_sizes", stdout=io.StringIO())
        pairs = list(Gown.objects.values_list("category", "name", "size"))
        self.assertEqual(len(pairs), len(set(pairs)))
        self.assertTrue({size for _, _, size in pairs} <= {S, M, L, XL})
        self.assertEqual(len({g.gown_id for g in Gown.objects.all()}), 650)
        self.assertEqual(len({g.slug for g in Gown.objects.all()}), 650)

    def test_barong_ends_with_every_design_in_all_four_sizes(self):
        call_command("add_gown_sizes", stdout=io.StringIO())
        for n in range(1, 8):
            self.assertEqual(Gown.objects.filter(name=f"Barong {n}").count(), 4)

    def test_running_it_again_adds_nothing(self):
        call_command("add_gown_sizes", stdout=io.StringIO())
        after = snapshot()
        call_command("add_gown_sizes", stdout=io.StringIO())
        self.assertEqual(snapshot(), after)
        self.assertEqual(Gown.objects.count(), 650)

    def test_the_dry_run_total_matches_what_the_real_run_does(self):
        out = io.StringIO()
        call_command("add_gown_sizes", "--dry-run", stdout=out)
        self.assertIn("Would add 260 gown(s): 390 -> 650", out.getvalue())
        self.assertEqual(Gown.objects.count(), 390)


class SavingIsFastAndLeavesTheCountersRightTests(TestCase):
    """One trip to the database per gown made 260 gowns take minutes on the remote database (and look frozen): the numbers are reserved in blocks
    and the gowns saved in batches, so the number of queries does not grow with the number of gowns."""

    def catalog(self, designs):
        for n in range(designs):
            make(WEDDING, f"Wedding Gown {n + 1}", M)

    def count_queries(self, targets):
        with CaptureQueriesContext(connection) as queries:
            run(targets)
        return len(queries)

    def test_the_number_of_queries_does_not_grow_with_the_number_of_gowns(self):
        self.catalog(40)
        small = self.count_queries({WEDDING: 40 + 4})
        large = self.count_queries({WEDDING: 40 + 4 + 36})
        self.assertLessEqual(large, small + 6)                    # 9 times as many new gowns, (almost) the same number of queries
        self.assertLess(large, 60)

    def test_it_prints_its_progress(self):
        self.catalog(3)
        text = run({WEDDING: 3 + 3})
        for line in ("Writing 3 gown(s)", "reserving the tag numbers", "reserving the web addresses", "saving 3 gowns", "Added 3 gown(s) in"):
            self.assertIn(line, text)

    def test_the_tag_counter_moves_past_the_new_tags_so_add_gown_never_reuses_one(self):
        self.catalog(3)
        run({WEDDING: 3 + 3})
        highest = max(g.tracking_number for g in Gown.objects.filter(category=WEDDING))
        self.assertEqual(Gown.next_tracking_number(WEDDING), highest + 1)

    def test_a_gown_added_afterwards_by_hand_gets_a_free_web_address(self):
        self.catalog(2)
        run({WEDDING: 2 + 2})
        slugs = set(Gown.objects.values_list("slug", flat=True))
        extra = make(WEDDING, "Wedding Gown 1", XL)               # the same name once more, through the normal save()
        self.assertNotIn(extra.slug, slugs)
        self.assertEqual(len(set(Gown.objects.values_list("slug", flat=True))), Gown.objects.count())

    def test_the_new_gowns_have_their_timestamps(self):
        self.catalog(2)
        run({WEDDING: 2 + 2})
        self.assertFalse(Gown.objects.filter(created_at__isnull=True).exists())
        self.assertFalse(Gown.objects.filter(updated_at__isnull=True).exists())


class ReservingNumbersInBlocksTests(TestCase):
    def test_a_block_of_tag_numbers_is_consecutive_and_the_counter_moves_past_it(self):
        make(WEDDING, "Wedding Gown 1", M)
        make(WEDDING, "Wedding Gown 2", M)
        highest = max(g.tracking_number for g in Gown.objects.all())
        block = GownSequence.reserve_block(WEDDING, 5)
        self.assertEqual(block, list(range(highest + 1, highest + 6)))
        self.assertEqual(GownSequence.next_value_for(WEDDING), highest + 6)
        self.assertEqual(GownSequence.reserve_block(WEDDING, 0), [])

    def test_a_brand_new_category_starts_above_every_number_already_used(self):
        make(EVENING, "Evening Gown 1", L)
        make(EVENING, "Evening Gown 2", L)
        block = GownSequence.reserve_block(EVENING, 2)
        self.assertEqual(min(block), 3)

    def test_slugs_in_bulk_skip_every_taken_string_and_never_repeat(self):
        taken = {"white-gown-1", "white-gown-1-2", "white-gown-1-4"}
        chosen = GownSlugSequence.reserve_many({"white-gown-1": 3, "black-gown-1": 2}, taken)
        self.assertEqual(chosen["white-gown-1"], ["white-gown-1-3", "white-gown-1-5", "white-gown-1-6"])
        self.assertEqual(chosen["black-gown-1"], ["black-gown-1-2", "black-gown-1-3"])
        every = [slug for slugs in chosen.values() for slug in slugs]
        self.assertEqual(len(every), len(set(every)))
        self.assertTrue(set(every) <= taken)
        self.assertEqual(dict(GownSlugSequence.objects.filter(base__in=["white-gown-1", "black-gown-1"]).values_list("base", "next_suffix")),
                         {"white-gown-1": 7, "black-gown-1": 4})

    def test_an_existing_counter_continues_where_it_was(self):
        GownSlugSequence.objects.create(base="red-gown-1", next_suffix=5)
        chosen = GownSlugSequence.reserve_many({"red-gown-1": 2}, {"red-gown-1"})
        self.assertEqual(chosen["red-gown-1"], ["red-gown-1-5", "red-gown-1-6"])
        self.assertEqual(GownSlugSequence.objects.get(base="red-gown-1").next_suffix, 7)
