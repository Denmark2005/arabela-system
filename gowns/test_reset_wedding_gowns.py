"""python manage.py reset_wedding_gowns: the Wedding Gown category becomes 28 gowns in a row -- Wedding Gown 1-28, IDs 001-028, all White, the owner's
prices -- because the old numbers (the photo numbers 01-33) had holes at 18, 19, 21, 22 and 23. The gown itself must survive untouched (bookings, blocks,
photo, slug, size, status), the next gown added must get 029, and anything that is not exactly the 28 described gowns must be refused with NOTHING changed."""
import io
import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from gowns.management.commands.reset_wedding_gowns import OLD_LIST, TOTAL, final_gown_id, final_name, number_in_name
from gowns.models import Gown, GownRemoval, GownSequence, GownUnavailability
from reservations.models import Reservation, ReservationItem

User = get_user_model()
WEDDING = Gown.Category.WEDDING_GOWN
OLD_NUMBERS = [old for old, _price in OLD_LIST]
# Most of the live gowns are already White/WH with an ID = their photo number (that is what makes final IDs collide mid-change); a few are not.
WRONG_COLOURS = [("Ivory", "IV"), ("White", "WH"), ("White", "WH"), ("Champagne", "CH")]


def build_old_shop():
    """The shop as it is today: the 28 gowns named after their photo numbers, IDs = the photo numbers, assorted colours and prices."""
    gowns = {}
    for i, number in enumerate(OLD_NUMBERS):
        colour, code = WRONG_COLOURS[i % 4]
        name = "Wedding Gown One" if number == 1 else f"Wedding Gown {number}"
        gowns[number] = Gown.objects.create(
            gown_id=f"{WEDDING}-{code}-{number:03d}", name=name, category=WEDDING, color_name=colour, color_code=code, size=Gown.Size.MEDIUM,
            rental_price=Decimal("20000"), status=Gown.Status.AVAILABLE, photo_url=f"https://photos.test/wedding-{number:02d}.jpg", notes=f"note {number}")
    return gowns


def run(*extra):
    out = io.StringIO()
    call_command("reset_wedding_gowns", *extra, stdout=out, stderr=io.StringIO())
    return out.getvalue()


def snapshot():
    return {g.pk: (g.gown_id, g.name, g.color_name, g.color_code, g.rental_price, g.slug) for g in Gown.objects.all()}


class TheListTests(TestCase):
    def test_the_list_is_the_28_numbers_the_owner_gave_in_order_with_their_prices(self):
        self.assertEqual(TOTAL, 28)
        self.assertEqual(OLD_NUMBERS, list(range(1, 18)) + [20] + list(range(24, 34)))
        self.assertEqual(dict(OLD_LIST)[5], 17000)
        self.assertEqual(dict(OLD_LIST)[16], 4500)
        self.assertEqual(dict(OLD_LIST)[33], 12000)
        self.assertEqual(sum(price for _n, price in OLD_LIST), 282500)      # 167,000 for gowns 1-17 + 115,500 for the other eleven

    def test_names_are_read_as_numbers_including_the_word_one(self):
        for name, number in (("Wedding Gown 17", 17), ("wedding gown 017", 17), ("Wedding Gown One", 1), (" Wedding  Gown   5 ", 5)):
            self.assertEqual(number_in_name(name), number, name)
        for bad in ("Wedding Gown", "Barong 7", "Wedding Gown 5A", "Bridal Gown 5", "", None):
            self.assertIsNone(number_in_name(bad), bad)


class ResetWeddingGownsTests(TestCase):
    def setUp(self):
        self.old = build_old_shop()

    def wedding(self):
        return Gown.objects.filter(category=WEDDING)

    def test_a_dry_run_shows_the_plan_and_changes_nothing(self):
        before = snapshot()
        out = run("--dry-run")
        self.assertEqual(snapshot(), before)
        self.assertEqual(GownSequence.objects.count(), 0)
        self.assertIn("DRY RUN", out)
        self.assertIn("Would renumber 28 gowns.", out)
        self.assertIn(self.old[33].gown_id + " Wedding Gown 33", out)
        self.assertIn("Wedding Gown-WH-028 Wedding Gown 28", out)

    def test_the_gowns_become_1_to_28_in_the_owners_order_with_ids_001_to_028(self):
        out = run()
        self.assertEqual(self.wedding().count(), 28)
        self.assertEqual(sorted(g.tracking_number for g in self.wedding()), list(range(1, 29)))
        for new, (old, _price) in enumerate(OLD_LIST, start=1):
            gown = self.old[old]
            gown.refresh_from_db()
            self.assertEqual((gown.name, gown.gown_id), (final_name(new), final_gown_id(new)), f"old {old}")
        self.assertEqual(self.old[33].gown_id, "Wedding Gown-WH-028")                  # "the last wedding gown 33 will be the 28"
        self.old[33].refresh_from_db()
        self.assertEqual((self.old[33].name, self.old[33].gown_id), ("Wedding Gown 28", "Wedding Gown-WH-028"))
        self.old[20].refresh_from_db()
        self.assertEqual((self.old[20].name, self.old[20].gown_id), ("Wedding Gown 18", "Wedding Gown-WH-018"))   # the hole closes
        self.old[1].refresh_from_db()
        self.assertEqual(self.old[1].name, "Wedding Gown 1")                          # "One" becomes a plain number like the others
        self.assertIn("Done: 28 Wedding Gowns", out)

    def test_every_gown_is_white_and_has_the_owners_price(self):
        run()
        for new, (old, price) in enumerate(OLD_LIST, start=1):
            gown = Gown.objects.get(pk=self.old[old].pk)
            self.assertEqual((gown.color_name, gown.color_code, int(gown.rental_price)), ("White", "WH", price), f"old {old}")

    def test_the_gown_itself_is_untouched_row_photo_slug_size_status_notes(self):
        slugs = {n: g.slug for n, g in self.old.items()}
        run()
        for number, gown in self.old.items():
            fresh = Gown.objects.get(pk=gown.pk)                                      # same row: the primary key survived
            self.assertEqual((fresh.slug, fresh.photo_url, fresh.size, fresh.status, fresh.notes),
                             (slugs[number], f"https://photos.test/wedding-{number:02d}.jpg", Gown.Size.MEDIUM, Gown.Status.AVAILABLE, f"note {number}"))
        self.assertEqual(self.client.get(f"/collections/wedding/products/{slugs[33]}/").status_code, 200)       # the old web address still works

    def test_a_final_id_that_another_gown_still_holds_is_no_problem(self):
        # photo 25 becomes 020 while photo 20 still holds 020: gown IDs are unique, so this needs the temporary-ID step
        self.assertEqual(self.old[20].gown_id, "Wedding Gown-WH-020")                      # photo 20 holds exactly the ID photo 25 is about to get
        run()
        self.assertEqual(Gown.objects.get(pk=self.old[25].pk).gown_id, "Wedding Gown-WH-020")
        self.assertEqual(Gown.objects.get(pk=self.old[20].pk).gown_id, "Wedding Gown-WH-018")
        self.assertFalse(Gown.objects.filter(gown_id__startswith="RESET-").exists())

    def test_the_old_ids_may_be_in_any_order_even_back_to_front(self):
        # the live ID numbers need not follow the photo numbers; here they run BACKWARDS, so the very first gown is about to take the ID the last one holds
        for number in OLD_NUMBERS:                                                       # (this set-up needs the same two steps itself)
            Gown.objects.filter(pk=self.old[number].pk).update(gown_id=f"SETUP-{number}")
        for index, number in enumerate(OLD_NUMBERS):
            Gown.objects.filter(pk=self.old[number].pk).update(gown_id=f"{WEDDING}-WH-{TOTAL - index:03d}", color_name="White", color_code="WH")
        before = {g.pk: g.slug for g in self.wedding()}
        run()
        for new, (old, _price) in enumerate(OLD_LIST, start=1):
            self.assertEqual(Gown.objects.get(pk=self.old[old].pk).gown_id, final_gown_id(new), f"old {old}")
        self.assertEqual({g.pk: g.slug for g in self.wedding()}, before)
        self.assertFalse(Gown.objects.filter(gown_id__startswith="RESET-").exists())

    def test_bookings_blocks_and_checks_still_point_at_the_same_gown_and_show_its_new_name(self):
        customer = User.objects.create_user("reset_customer", password="x")
        reservation = Reservation.objects.create(customer=customer, customer_name="Reset Customer", status=Reservation.Status.CONFIRMED)
        day = timezone.localdate() + timedelta(days=15)
        item = ReservationItem.objects.create(reservation=reservation, gown=self.old[33], gown_name="Wedding Gown 33", size="Medium", rental_date=day,
                                              event_date=day + timedelta(days=2), return_date=day + timedelta(days=4), overdue_date=day + timedelta(days=4))
        block = GownUnavailability.objects.create(gown=self.old[33], start_date=day + timedelta(days=10), end_date=day + timedelta(days=12))
        out = run()
        item.refresh_from_db()
        self.assertEqual(item.gown_id, self.old[33].pk)
        self.assertEqual(item.gown.gown_id, "Wedding Gown-WH-028")
        self.assertEqual(item.gown_name, "Wedding Gown 28")                            # the booking shows the gown that is on the rack
        block.refresh_from_db()
        self.assertEqual(block.gown_id, self.old[33].pk)
        self.assertEqual(Reservation.objects.get().status, Reservation.Status.CONFIRMED)
        self.assertEqual(ReservationItem.objects.count(), 1)

    def test_the_counter_is_reset_so_the_next_gown_added_is_029(self):
        GownSequence.objects.create(category=WEDDING, color_code="WH", next_value=40)      # an old per-colour counter, left alone
        GownSequence.objects.create(category=WEDDING, color_code=GownSequence.CATEGORY_WIDE, next_value=34)
        run()
        self.assertEqual(GownSequence.objects.get(category=WEDDING, color_code=GownSequence.CATEGORY_WIDE).next_value, 29)
        self.assertEqual(GownSequence.objects.get(category=WEDDING, color_code="WH").next_value, 40)
        staff = User.objects.create_user("reset_staff", password="x", is_staff=True)
        client = Client()
        client.force_login(staff)
        response = client.post(reverse("arabela_admin:gown_create"), data={
            "name": "Wedding Gown 29", "category": WEDDING, "color_name": "White", "color_code": "WH", "size": "Medium", "rental_price": "9000"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["gown"]["gown_id"], "Wedding Gown-WH-029")

    def test_the_counter_is_created_when_the_category_never_had_one(self):
        self.assertFalse(GownSequence.objects.filter(category=WEDDING).exists())
        run()
        self.assertEqual(GownSequence.objects.get(category=WEDDING, color_code=GownSequence.CATEGORY_WIDE).next_value, 29)

    def test_other_categories_and_the_removal_log_are_left_alone(self):
        other = Gown.objects.create(gown_id="Barong-BG-001", name="Wedding Gown 33", category=Gown.Category.BARONG, color_name="Beige", color_code="BG",
                                    size=Gown.Size.SMALL, rental_price=Decimal("1000"))
        log = GownRemoval.objects.create(gown_id="Wedding Gown-WH-018", tracking_number=18, name="Wedding Gown 18", category=WEDDING, color_name="White",
                                         size="Medium", reason="Retired", removed_at=timezone.now())
        run()
        other.refresh_from_db()
        self.assertEqual((other.gown_id, other.name), ("Barong-BG-001", "Wedding Gown 33"))
        log.refresh_from_db()
        self.assertEqual((log.gown_id, log.tracking_number), ("Wedding Gown-WH-018", 18))
        self.assertEqual(GownRemoval.objects.count(), 1)

    def test_a_second_run_says_it_is_already_done_and_changes_nothing(self):
        run()
        after = snapshot()
        out = run()
        self.assertIn("already reset", out)
        self.assertEqual(snapshot(), after)

    def test_the_customer_site_and_admin_show_the_new_numbers(self):
        run()
        html = self.client.get("/collections/wedding/").content.decode()
        self.assertIn("Wedding Gown 1", html)
        names = set()
        for page in (1, 2, 3, 4):
            body = self.client.get("/collections/wedding/", {"page": page}).content.decode()
            main = body[body.index("<main"):body.index("</main>")]
            names |= set(__import__("re").findall(r'<h3[^>]*>\s*([^<]+?)\s*</h3>', main))
        self.assertEqual(names, {f"Wedding Gown {n}" for n in range(1, 29)})
        staff = User.objects.create_user("reset_staff2", password="x", is_staff=True)
        client = Client()
        client.force_login(staff)
        catalog = client.get(reverse("arabela_admin:gown_catalog")).content.decode()
        self.assertIn("Wedding Gown-WH-028", catalog)
        self.assertNotIn("Wedding Gown-WH-033", catalog)                              # its old ID is gone from the admin


class RefusalTests(TestCase):
    """Anything that is not exactly the 28 described gowns: refuse, say why, and change NOTHING."""

    def check_refused(self, fragment):
        before = snapshot()
        sequences = GownSequence.objects.count()
        with self.assertRaises(CommandError) as raised:
            run()
        self.assertIn(fragment, str(raised.exception))
        self.assertEqual(snapshot(), before)
        self.assertEqual(GownSequence.objects.count(), sequences)

    def test_a_gown_numbered_18_that_the_owner_says_does_not_exist(self):
        build_old_shop()
        Gown.objects.create(gown_id="Wedding Gown-WH-018", name="Wedding Gown 18", category=WEDDING, color_name="White", color_code="WH",
                            size=Gown.Size.MEDIUM, rental_price=Decimal("5000"))
        self.check_refused("there is no Wedding Gown 18 in the owner's list")

    def test_a_missing_gown(self):
        gowns = build_old_shop()
        gowns[33].delete()
        self.check_refused("not in the shop: 33")

    def test_two_gowns_with_the_same_number(self):
        build_old_shop()
        Gown.objects.create(gown_id="Wedding Gown-WH-099", name="Wedding Gown 7", category=WEDDING, color_name="White", color_code="WH",
                            size=Gown.Size.LARGE, rental_price=Decimal("5000"))
        self.check_refused("Wedding Gown 7 is on 2 gowns")

    def test_a_name_that_is_not_a_wedding_gown_number(self):
        gowns = build_old_shop()
        Gown.objects.filter(pk=gowns[12].pk).update(name="Lace Princess")
        self.check_refused('the name "Lace Princess" is not "Wedding Gown <number>"')

    def test_an_empty_category_is_refused_not_pretended(self):
        with self.assertRaises(CommandError) as raised:
            run()
        self.assertIn("not in the shop", str(raised.exception))

    def test_all_the_problems_are_listed_together(self):
        gowns = build_old_shop()
        gowns[3].delete()
        Gown.objects.filter(pk=gowns[4].pk).update(name="Mystery")
        with self.assertRaises(CommandError) as raised:
            run()
        text = str(raised.exception)
        self.assertIn("Mystery", text)
        self.assertIn("not in the shop: 3", text)
