"""The Fabric and Description customers read on each gown's page (gowns/design_details.py): one text per design (name + category) shared by every
size; typed in Gown Catalog's Add/Edit; shown on the product page instead of the old made-up paragraph; staff notes stay private. Also the
add_gown_descriptions command that filled the catalog from gowns/gown_descriptions.json (only empty boxes, never over staff's own text)."""
import io
import json
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from accounts.models import AuditLog
from gowns import design_details
from gowns.management.commands import add_gown_descriptions as command
from gowns.models import Gown

User = get_user_model()
WEDDING, EVENING, SUIT = Gown.Category.WEDDING_GOWN, Gown.Category.EVENING_GOWN, Gown.Category.SUIT
_counter = {"n": 0}


def make(name, size=Gown.Size.MEDIUM, *, category=WEDDING, fabric="", description="", notes=""):
    _counter["n"] += 1
    tag = Gown.next_tracking_number(category)
    return Gown.objects.create(gown_id=f"{category}-WH-{tag:03d}", name=name, category=category, color_name="White", color_code="WH", size=size,
                               rental_price=Decimal("5000"), fabric=fabric, description=description, notes=notes,
                               photo_url="https://res.cloudinary.com/demo/image/upload/v1/gown_photos/x.jpg")


def text(gown):
    gown.refresh_from_db()
    return gown.fabric, gown.description


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("gd_owner", "gd_owner@example.com", "x")

    def setUp(self):
        self.client.force_login(self.owner)

    def fields(self, **changes):
        data = {"name": "Wedding Gown 1", "category": WEDDING, "color_name": "White", "color_code": "WH", "size": Gown.Size.LARGE,
                "rental_price": "5000", "condition": "Good", "status": "Available", "notes": "", "fabric": "", "description": ""}
        data.update(changes)
        return data

    def add(self, **changes):
        return self.client.post(reverse("arabela_admin:gown_create"), self.fields(**changes))

    def edit(self, gown, drop=(), **changes):
        data = self.fields(name=gown.name, category=gown.category, size=gown.size, fabric=gown.fabric, description=gown.description)
        data.update(changes)
        for field in drop:
            data.pop(field)
        return self.client.post(reverse("arabela_admin:gown_update", args=[gown.pk]), data)


class CleaningTests(TestCase):
    def test_spacing_is_tidied_and_line_breaks_kept(self):
        self.assertEqual(design_details.clean_fabric("  Layered \r\n tulle   with lace "), "Layered tulle with lace")
        self.assertEqual(design_details.clean_description("  Off-shoulder   gown.\r\n\r\n\r\n\r\nFull skirt.  "), "Off-shoulder gown.\n\nFull skirt.")
        self.assertEqual(design_details.clean_description(None), "")

    def test_the_limits(self):
        self.assertEqual(design_details.problem("x" * 200, "y" * 1000), "")
        self.assertIn("200", design_details.problem("x" * 201, ""))
        self.assertIn("1,000", design_details.problem("", "y" * 1001))


class AddGownTests(Base):
    def test_a_new_gown_keeps_what_was_typed(self):
        response = self.add(fabric="  Tulle   with lace ", description="Off-shoulder gown.\r\nFull skirt.")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(text(Gown.objects.get()), ("Tulle with lace", "Off-shoulder gown.\nFull skirt."))
        self.assertEqual(AuditLog.objects.get(action="gown.create").details["fabric"].split(), ["Tulle", "with", "lace"])     # (as typed)

    def test_another_size_with_empty_boxes_takes_the_designs_text(self):
        first = make("Wedding Gown 1", fabric="Tulle", description="Full skirt.")
        response = self.add(name_choice="same")
        self.assertEqual(response.status_code, 200, response.content)
        new = Gown.objects.exclude(pk=first.pk).get()
        self.assertEqual((new.name, text(new), text(first)), ("Wedding Gown 1", ("Tulle", "Full skirt."), ("Tulle", "Full skirt.")))

    def test_another_size_with_typed_text_gives_it_to_every_size(self):
        small = make("Wedding Gown 1", Gown.Size.SMALL, fabric="Tulle", description="Old words.")
        medium = make("wedding gown 1", Gown.Size.MEDIUM, fabric="Tulle", description="Old words.")           # one design whatever the capitals
        response = self.add(name_choice="same", description="New words.")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual({text(g) for g in Gown.objects.all()}, {("Tulle", "New words.")})
        self.assertEqual((text(small), text(medium)), (("Tulle", "New words."), ("Tulle", "New words.")))

    def test_a_different_gown_with_the_same_name_gets_its_own_text(self):
        old = make("Wedding Gown 1", fabric="Tulle", description="Full skirt.")
        response = self.add(name_choice="different", fabric="Satin")
        self.assertEqual(response.status_code, 200, response.content)
        new = Gown.objects.exclude(pk=old.pk).get()
        self.assertEqual((new.name, text(new), text(old)), ("Wedding Gown 1 (2)", ("Satin", ""), ("Tulle", "Full skirt.")))

    def test_the_same_name_in_another_category_is_another_design(self):
        old = make("White", category=EVENING, fabric="Satin")
        self.assertEqual(self.add(name="White", fabric="Lace").status_code, 200)
        self.assertEqual(text(old), ("Satin", ""))

    def test_too_long_is_refused_and_nothing_is_saved(self):
        for changes, words in (({"fabric": "x" * 201}, "Fabric"), ({"description": "y" * 1001}, "Description")):
            response = self.add(**changes)
            self.assertEqual(response.status_code, 400, changes.keys())
            self.assertIn(words, response.json()["error"])
        self.assertFalse(Gown.objects.exists())


class EditGownTests(Base):
    def test_saving_one_size_changes_every_size_and_only_that_design(self):
        small = make("Wedding Gown 1", Gown.Size.SMALL, fabric="Tulle", description="Old.")
        large = make("Wedding Gown 1", Gown.Size.LARGE, fabric="Tulle", description="Old.", notes="Zipper fixed")
        other = make("Wedding Gown 2", fabric="Lace", description="Other.")
        response = self.edit(small, fabric="Beaded tulle", description="New.")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()["gown"]["fabric"], response.json()["design_units"]), ("Beaded tulle", 2))
        self.assertEqual((text(small), text(large), text(other)), (("Beaded tulle", "New."), ("Beaded tulle", "New."), ("Lace", "Other.")))
        large.refresh_from_db()
        self.assertEqual(large.notes, "Zipper fixed")                     # staff notes stay per gown
        line = AuditLog.objects.get(action="gown.update")
        self.assertIn("shared by all 2 gowns", line.summary)
        self.assertEqual(line.details["fabric"], ["Tulle", "Beaded tulle"])

    def test_emptying_the_boxes_empties_every_size(self):
        small = make("Wedding Gown 1", Gown.Size.SMALL, fabric="Tulle", description="Old.")
        large = make("Wedding Gown 1", Gown.Size.LARGE, fabric="Tulle", description="Old.")
        self.assertEqual(self.edit(small, fabric="", description="").status_code, 200)
        self.assertEqual((text(small), text(large)), (("", ""), ("", "")))

    def test_an_edit_window_without_the_boxes_changes_no_text(self):
        small = make("Wedding Gown 1", Gown.Size.SMALL, fabric="Tulle", description="Keep.")
        large = make("Wedding Gown 1", Gown.Size.LARGE)                    # (not yet filled)
        self.assertEqual(self.edit(small, drop=("fabric", "description"), rental_price="6000").status_code, 200)
        self.assertEqual((text(small), text(large)), (("Tulle", "Keep."), ("", "")))

    def test_moving_into_another_design_never_overwrites_it_with_the_old_text(self):
        mover = make("Wedding Gown 1", fabric="Satin", description="Mine.")
        target = make("Wedding Gown 2", fabric="Lace", description="Theirs.")
        response = self.edit(mover, name="Wedding Gown 2")                   # the boxes left exactly as they were
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((text(mover), text(target)), (("Lace", "Theirs."), ("Lace", "Theirs.")))

    def test_moving_with_emptied_boxes_takes_the_designs_text(self):
        mover = make("Wedding Gown 1", fabric="Satin", description="Mine.")
        target = make("Wedding Gown 2", fabric="Lace", description="Theirs.")
        self.assertEqual(self.edit(mover, name="Wedding Gown 2", fabric="", description="").status_code, 200)
        self.assertEqual((text(mover), text(target)), (("Lace", "Theirs."), ("Lace", "Theirs.")))

    def test_moving_with_new_text_typed_gives_it_to_the_whole_design(self):
        mover = make("Wedding Gown 1", fabric="Satin", description="Mine.")
        target = make("Wedding Gown 2", fabric="Lace", description="Theirs.")
        self.assertEqual(self.edit(mover, name="Wedding Gown 2", description="Typed now.").status_code, 200)
        self.assertEqual((text(mover), text(target)), (("Lace", "Typed now."), ("Lace", "Typed now.")))

    def test_a_brand_new_name_takes_only_this_gown(self):
        moved = make("Wedding Gown 1", Gown.Size.SMALL, fabric="Tulle", description="Full skirt.")
        stays = make("Wedding Gown 1", Gown.Size.LARGE, fabric="Tulle", description="Full skirt.")
        self.assertEqual(self.edit(moved, name="Royal Gown", description="Royal.").status_code, 200)
        self.assertEqual((text(moved), text(stays)), (("Tulle", "Royal."), ("Tulle", "Full skirt.")))

    def test_too_long_is_refused_and_nothing_changes(self):
        gown = make("Wedding Gown 1", fabric="Tulle")
        self.assertEqual(self.edit(gown, description="y" * 1001).status_code, 400)
        self.assertEqual(text(gown), ("Tulle", ""))


class ProductPageTests(Base):
    def page(self, gown):
        response = self.client.get(reverse("gowns:product_detail", args=["wedding", gown.slug]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_every_size_shows_the_same_fabric_and_description_and_no_made_up_text(self):
        small = make("Wedding Gown 1", Gown.Size.SMALL, fabric="Layered tulle", description="Off-shoulder.\nFull <b>skirt</b>.", notes="Small stain")
        large = make("Wedding Gown 1", Gown.Size.LARGE, fabric="Layered tulle", description="Off-shoulder.\nFull <b>skirt</b>.")
        for gown in (small, large):
            html = self.page(gown)
            self.assertIn('id="product-fabric"', html)
            self.assertIn("Layered tulle", html)
            self.assertIn("Off-shoulder.<br>Full &lt;b&gt;skirt&lt;/b&gt;.", html)           # typed text is shown as text, never as markup
            self.assertNotIn("Small stain", html)                                             # staff notes are private
            self.assertNotIn("Hand-draped from heritage textiles", html)

    def test_nothing_written_means_nothing_shown(self):
        html = self.page(make("Wedding Gown 1"))
        for gone in ('id="product-fabric"', 'id="product-description"', "Hand-draped"):
            self.assertNotIn(gone, html)


class CatalogPageTests(Base):
    def test_the_rows_carry_the_text_for_the_view_and_edit_windows(self):
        make("Wedding Gown 1", Gown.Size.SMALL, fabric='Tulle "soft"', description="It's <full>.\nSecond line.")
        make("Wedding Gown 1", Gown.Size.LARGE, fabric='Tulle "soft"', description="It's <full>.\nSecond line.")
        html = self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()
        self.assertEqual(html.count('data-fabric="Tulle &quot;soft&quot;"'), 2)
        self.assertEqual(html.count('data-description="It&#x27;s &lt;full&gt;.\nSecond line."'), 2)
        self.assertEqual(html.count('data-sizes="2"'), 2)
        self.assertIn("...rowDetails($el)", html)
        for field in ('id="gc-fabric"', 'id="gc-description"', "form.append('fabric'", "form.append('description'", "Notes <span", "only staff see this"):
            self.assertIn(field, html, field)


class FillCommandTests(TestCase):
    def run_command(self, rows, *args):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "texts.json"
            path.write_text(json.dumps(rows), encoding="utf-8")
            out = io.StringIO()
            with patch.object(command.Command, "data_file", path):
                call_command("add_gown_descriptions", *args, stdout=out)
            return out.getvalue()

    def entry(self, name, fabric="Tulle", description="Full skirt.", category=WEDDING):
        return {"category": category, "name": name, "fabric": fabric, "description": description}

    def test_empty_gowns_are_filled_for_every_size_and_a_dry_run_changes_nothing(self):
        small = make("Wedding Gown 1", Gown.Size.SMALL)
        large = make("Wedding Gown 1", Gown.Size.LARGE)
        rows = [self.entry("Wedding Gown 1")]
        self.assertIn("Would fill 2 gown(s) in 1 design(s)", self.run_command(rows, "--dry-run"))
        self.assertEqual((text(small), text(large)), (("", ""), ("", "")))
        self.assertIn("Filled 2 gown(s) in 1 design(s)", self.run_command(rows))
        self.assertEqual((text(small), text(large)), (("Tulle", "Full skirt."), ("Tulle", "Full skirt.")))
        self.assertIn("Filled 0 gown(s)", self.run_command(rows))                        # a second run changes nothing

    def test_staff_text_is_never_overwritten_and_its_other_sizes_follow_it(self):
        typed = make("Wedding Gown 1", Gown.Size.SMALL, fabric="Silk I typed")
        empty = make("Wedding Gown 1", Gown.Size.LARGE)
        output = self.run_command([self.entry("Wedding Gown 1")])
        self.assertEqual((text(typed), text(empty)), (("Silk I typed", "Full skirt."), ("Silk I typed", "Full skirt.")))
        self.assertIn("already had text typed", output)

    def test_gowns_not_in_the_file_and_entries_with_no_gown_are_listed(self):
        make("Wedding Gown 9")
        output = self.run_command([self.entry("Gone Gown")])
        self.assertIn("Wedding Gown: Wedding Gown 9", output)
        self.assertIn("Wedding Gown: Gone Gown", output)
        self.assertEqual(text(Gown.objects.get()), ("", ""))

    def test_a_bad_file_changes_nothing(self):
        gown = make("Wedding Gown 1")
        for rows in ([self.entry("Wedding Gown 1", fabric="")], [self.entry("Wedding Gown 1"), self.entry("wedding gown 1")],
                     [self.entry("Wedding Gown 1", category="Nonsense")], [self.entry("Wedding Gown 1", description="y" * 1001)]):
            with self.assertRaises(CommandError):
                self.run_command(rows)
        self.assertEqual(text(gown), ("", ""))


class TheShippedFileTests(TestCase):
    def test_every_entry_can_be_saved_and_every_suit_says_what_comes_with_it(self):
        texts = command.load(command.DATA_FILE)
        self.assertEqual(len(texts), 313)
        self.assertEqual({category for category, _ in texts}, set(Gown.Category.values))
        for (category, name), (fabric, description, _) in texts.items():
            if category == SUIT:
                self.assertTrue("vest" in description and "pants" in description, name)
            self.assertNotIn("necklace", (fabric + description).lower(), name)


class AddSizesCopiesTheTextTests(TestCase):
    def test_a_new_size_is_made_with_the_designs_fabric_and_description(self):
        from gowns.management.commands import add_gown_sizes
        make("Wedding Gown 1", fabric="Tulle", description="Full skirt.")
        with patch.object(add_gown_sizes.Command, "targets", {WEDDING: 2}):
            call_command("add_gown_sizes", stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(Gown.objects.count(), 2)
        self.assertEqual({(g.fabric, g.description) for g in Gown.objects.all()}, {("Tulle", "Full skirt.")})
