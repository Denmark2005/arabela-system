"""python manage.py reset_kids_gowns: the live Kids Gown category ended up with 62 gowns for 33 designs (an earlier upload with placeholder sizes, then the
owner's real sizes added NEXT to them). The owner wants exactly 33: one per design in the owner's size, IDs 001-033. These tests rebuild that exact state
(29 designs with a wrong placeholder size at IDs 1-33 plus the owner's size added at IDs 34-62, and 4 designs that happened to match at the start)."""
import io
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from unittest import mock
from django.utils import timezone

from gowns.management.commands.add_kids_gowns import KIDS_GOWNS, SIZES_BY_NAME
from gowns.management.commands.reset_kids_gowns import NOTE, TOTAL, final_gown_id
from gowns.models import Gown, GownRemoval, GownSequence, GownUnavailability
from reservations.models import Reservation, ReservationItem

User = get_user_model()
KIDS = Gown.Category.KIDS_GOWN
CODES = ["RD", "RG", "WH", "LV", "PK", "BU", "PE", "YL"]
MATCHING_AT_FIRST = {5, 8, 16, 19}                                   # designs whose placeholder size happened to be the owner's (as on the live shop)
OTHER_SIZE = {"Small": "Medium", "Medium": "Large", "Large": "Extra Large", "Extra Large": "Small"}


def code_of(index):
    return CODES[index % len(CODES)]


def build_live_state():
    """The 62-gown mess. Returns {design number: (first-run gown, second-run gown or None)}."""
    made, tag = {}, 0
    for index, (name, _photo, colour, _sizes, price) in enumerate(KIDS_GOWNS, start=1):
        owner = str(SIZES_BY_NAME[name][0])
        size = owner if index in MATCHING_AT_FIRST else OTHER_SIZE[owner]
        tag += 1
        first = Gown.objects.create(gown_id=f"{KIDS}-{code_of(index)}-{tag:03d}", name=name, category=KIDS, color_name=colour, color_code=code_of(index),
                                    size=size, rental_price=Decimal(price), photo_url=f"https://photos.test/kids-{index:02d}.jpg")
        made[index] = [first, None]
    for index, (name, _photo, colour, _sizes, price) in enumerate(KIDS_GOWNS, start=1):
        if index in MATCHING_AT_FIRST:
            continue
        tag += 1
        made[index][1] = Gown.objects.create(gown_id=f"{KIDS}-{code_of(index)}-{tag:03d}", name=name, category=KIDS, color_name=colour, color_code=code_of(index),
                                             size=str(SIZES_BY_NAME[name][0]), rental_price=Decimal(price), photo_url=f"https://photos.test/kids-{index:02d}.jpg")
    return made


def run(*extra):
    out = io.StringIO()
    call_command("reset_kids_gowns", *extra, stdout=out, stderr=io.StringIO())
    return out.getvalue()


def snapshot():
    return {g.pk: (g.gown_id, g.name, g.size, g.slug) for g in Gown.objects.all()}


class ResetKidsGownsTests(TestCase):
    def setUp(self):
        self.made = build_live_state()

    def kids(self):
        return Gown.objects.filter(category=KIDS)

    def keeper(self, index):
        first, second = self.made[index]
        return first if index in MATCHING_AT_FIRST else second

    def test_the_test_state_is_the_live_state_62_gowns_29_wrong(self):
        self.assertEqual(self.kids().count(), 62)
        wrong = [g for g in self.kids() if g.size != str(SIZES_BY_NAME[g.name][0])]
        self.assertEqual(len(wrong), 29)
        self.assertEqual(sorted(g.tracking_number for g in wrong), [n for n in range(1, 34) if n not in MATCHING_AT_FIRST])

    def test_a_dry_run_shows_the_plan_and_changes_nothing(self):
        before = snapshot()
        out = run("--dry-run")
        self.assertEqual(snapshot(), before)
        self.assertEqual(GownRemoval.objects.count(), 0)
        self.assertIn("Removing 29 extra gown(s)", out)
        self.assertIn("Would remove 29 gown(s) and renumber 33.", out)

    def test_exactly_33_gowns_remain_one_per_design_each_in_the_owners_size(self):
        out = run()
        self.assertEqual(self.kids().count(), 33)
        self.assertEqual(self.kids().values("name").distinct().count(), 33)
        for name, *_rest in KIDS_GOWNS:
            self.assertEqual([g.size for g in self.kids().filter(name=name)], [str(SIZES_BY_NAME[name][0])], name)
        self.assertIn("Done: 29 extra gown(s) removed; 33 Kids Gowns remain", out)

    def test_ids_run_001_to_033_in_the_owners_order(self):
        run()
        self.assertEqual(sorted(g.tracking_number for g in self.kids()), list(range(1, 34)))
        for index, (name, *_rest) in enumerate(KIDS_GOWNS, start=1):
            gown = self.kids().get(name=name)
            self.assertEqual(gown.gown_id, final_gown_id(code_of(index), index), name)         # Kids Gown 05 is 005, Kids Gown 33 is 033
        self.assertFalse(Gown.objects.filter(gown_id__startswith="RESET-").exists())

    def test_the_kept_gowns_are_the_same_records_nothing_else_about_them_changes(self):
        before = {i: (self.keeper(i).pk, self.keeper(i).slug, self.keeper(i).photo_url, self.keeper(i).rental_price, self.keeper(i).color_name, self.keeper(i).name)
                  for i in range(1, 34)}
        run()
        for index in range(1, 34):
            fresh = Gown.objects.get(pk=before[index][0])
            self.assertEqual((fresh.pk, fresh.slug, fresh.photo_url, fresh.rental_price, fresh.color_name, fresh.name), before[index], index)

    def test_the_removed_gowns_are_in_the_removal_log_with_the_reason(self):
        run()
        log = GownRemoval.objects.filter(category=KIDS)
        self.assertEqual(log.count(), 29)
        self.assertTrue(all(r.reason == GownRemoval.Reason.OTHER and r.note == NOTE for r in log))
        self.assertEqual(sorted(r.tracking_number for r in log), [n for n in range(1, 34) if n not in MATCHING_AT_FIRST])

    def test_the_counter_is_reset_so_the_next_kids_gown_is_034(self):
        run()
        self.assertEqual(GownSequence.objects.get(category=KIDS, color_code=GownSequence.CATEGORY_WIDE).next_value, TOTAL + 1)
        self.assertEqual(Gown.next_tracking_number(KIDS), 34)

    def test_other_categories_are_untouched(self):
        other = Gown.objects.create(gown_id="Barong-BG-001", name="Beige Barong 1", category=Gown.Category.BARONG, color_name="Beige", color_code="BG",
                                    size=Gown.Size.SMALL, rental_price=Decimal("1000"))
        run()
        other.refresh_from_db()
        self.assertEqual((other.gown_id, other.name), ("Barong-BG-001", "Beige Barong 1"))

    def test_a_second_run_says_it_is_already_done_and_changes_nothing(self):
        run()
        after = snapshot()
        out = run()
        self.assertIn("already reset", out)
        self.assertEqual(snapshot(), after)
        self.assertEqual(GownRemoval.objects.filter(category=KIDS).count(), 29)

    def test_it_makes_a_handful_of_database_calls_not_hundreds(self):
        # the live run crashed half-way: about 250 one-row calls over the internet link; a dropped connection rolls the whole thing back
        with CaptureQueriesContext(connection) as queries:
            run()
        self.assertLess(len(queries), 40, f"{len(queries)} database calls")

    def test_the_removal_log_entry_is_the_one_the_admin_writes_for_a_removed_gown(self):
        from arabela_admin.views import _remove_gown
        probe = self.made[2][0]                                            # a gown the command is about to remove (design 2's placeholder size)
        fields = ("tracking_number", "name", "category", "color_name", "size", "photo_url", "reason", "note", "removed_by_id", "removed_by_name")
        clone_id = probe.gown_id.replace(f"-{probe.color_code}-", "-XX-")        # same number, another colour code (the real ID now belongs to a kept gown)
        clone = dict(gown_id=clone_id, name=probe.name, category=probe.category, color_name=probe.color_name, color_code=probe.color_code,
                     size=probe.size, rental_price=probe.rental_price, photo_url=probe.photo_url)
        run()
        ours = GownRemoval.objects.get(gown_id=probe.gown_id)
        ours_values = {f: getattr(ours, f) for f in fields}
        ours.delete()
        again = Gown.objects.create(**clone)                               # the same gown again, removed the way the admin's Remove Gown does it
        _remove_gown(again, GownRemoval.Reason.OTHER, NOTE, None)
        by_admin = GownRemoval.objects.get(gown_id=clone_id)
        self.assertEqual({f: getattr(by_admin, f) for f in fields}, ours_values)

    def test_a_dropped_connection_part_way_rolls_everything_back_and_says_so_plainly(self):
        before = snapshot()
        original = Gown.objects.bulk_update
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:                                            # the second step: after the removals and the temporary IDs
                raise OperationalError("SSL connection has been closed unexpectedly")
            return original(*args, **kwargs)

        with mock.patch.object(type(Gown.objects), "bulk_update", side_effect=flaky):
            with self.assertRaises(CommandError) as raised:
                run()
        self.assertIn("rolled back and NOTHING was changed", str(raised.exception))
        self.assertIn("OperationalError", str(raised.exception))
        self.assertEqual(snapshot(), before)                               # 62 gowns, original IDs, nothing removed
        self.assertEqual(GownRemoval.objects.count(), 0)
        self.assertFalse(Gown.objects.filter(gown_id__startswith="RESET-").exists())
        out = run()                                                        # and simply running it again works
        self.assertIn("Done: 29 extra gown(s) removed", out)

    def test_the_customer_site_shows_33_products_with_one_size_each(self):
        run()
        import re
        html = self.client.get("/collections/kids-gown/").content.decode()
        self.assertIn("Page 1 of 5", html)
        slug = self.kids().get(name="Red Kids Gown 1").slug
        page = self.client.get(f"/collections/kids-gown/products/{slug}/").content.decode()
        self.assertEqual(re.findall(r'data-product-size="([A-Z]+)"', page), ["S"])


class RefusalTests(TestCase):
    """Anything unsafe: refuse, say why, and change NOTHING."""

    def check_refused(self, fragment):
        before = snapshot()
        with self.assertRaises(CommandError) as raised:
            run()
        self.assertIn(fragment, str(raised.exception))
        self.assertEqual(snapshot(), before)
        self.assertEqual(GownRemoval.objects.count(), 0)

    def test_an_extra_gown_with_a_booking_is_never_removed(self):
        made = build_live_state()
        extra = made[1][0]                                                   # design 1's placeholder-size gown, about to be removed
        customer = User.objects.create_user("kids_reset_customer", password="x")
        reservation = Reservation.objects.create(customer=customer, customer_name="Kid Customer", status=Reservation.Status.CONFIRMED)
        day = timezone.localdate() + timedelta(days=15)
        ReservationItem.objects.create(reservation=reservation, gown=extra, gown_name=extra.name, size=extra.size, rental_date=day, event_date=day + timedelta(days=2),
                                       return_date=day + timedelta(days=4), overdue_date=day + timedelta(days=4))
        self.check_refused("would be removed but has 1 booking line(s)")

    def test_an_extra_gown_with_a_blocked_date_is_never_removed(self):
        made = build_live_state()
        day = timezone.localdate() + timedelta(days=20)
        GownUnavailability.objects.create(gown=made[2][0], start_date=day, end_date=day + timedelta(days=2))
        self.check_refused("1 blocked date(s)")

    def test_a_gown_that_is_not_one_of_the_33(self):
        build_live_state()
        Gown.objects.create(gown_id="Kids Gown-RD-099", name="Mystery Kids Gown", category=KIDS, color_name="Red", color_code="RD", size="Small", rental_price=Decimal("1"))
        self.check_refused('"Mystery Kids Gown" is not one of the owner\'s 33 Kids Gowns')

    def test_a_design_with_no_gown_in_the_owners_size(self):
        made = build_live_state()
        made[1][1].delete()                                                  # design 1 loses its owner's-size gown
        self.check_refused("has no gown in the owner's size")

    def test_a_design_with_two_gowns_in_the_owners_size(self):
        made = build_live_state()
        Gown.objects.create(gown_id="Kids Gown-RD-098", name=made[5][0].name, category=KIDS, color_name="Red", color_code="RD", size=made[5][0].size, rental_price=Decimal("1"))
        self.check_refused("keeping one needs a decision")

    def test_an_empty_category_is_refused_not_pretended(self):
        with self.assertRaises(CommandError):
            run()
