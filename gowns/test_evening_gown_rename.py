"""Ball Gown was renamed Evening Gown. These tests cover the database side (the two migrations, called directly on
shop-like rows), the customer pages and old addresses, the staff side, and a shopping bag filled before the rename."""
import importlib
import json
from decimal import Decimal
from datetime import date
from unittest.mock import patch

from django.apps import apps as real_apps
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase
from django.urls import NoReverseMatch, reverse
from PIL import Image

from accounts.models import UserProfile
from django.conf import settings
from gowns.context_processors import _CATEGORIES, _RENAMED_COLLECTION_KEYS
from gowns.models import (
    DEFAULT_CATEGORY_TAG_COLORS, CategoryCover, CustomCategory, Gown, GownRemoval, GownSequence, HiddenCategory,
    SiteSettings, all_category_names, resolve_tag_colors,
)
from reservations.models import Reservation, ReservationItem

User = get_user_model()
gowns_migration = importlib.import_module("gowns.migrations.0024_rename_ball_gown_to_evening_gown")
reservations_migration = importlib.import_module("reservations.migrations.0023_rename_ball_gown_items")

_TINY_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


def make_gown(gown_id, name, category, slug, color="Blue", code="BU"):
    return Gown.objects.create(
        gown_id=gown_id, name=name, slug=slug, category=category, color_name=color, color_code=code,
        size=Gown.Size.MEDIUM, rental_price=Decimal("1500"),
    )


class NameSwapTests(SimpleTestCase):
    """The one rule both migrations use to decide which names change."""

    CASES = (
        ("Ball Gown 79", "Evening Gown 79"),
        ("Ball Gown", "Evening Gown"),
        ("Ball Gown (2)", "Evening Gown (2)"),
        ("Ball Gown 79 (3)", "Evening Gown 79 (3)"),
        ("Ball Gown Tulle 51", "Ball Gown Tulle 51"),     # a different category
        ("Ball Gown  Tulle 51", "Ball Gown  Tulle 51"),
        ("Ball Gowns", "Ball Gowns"),                     # a different word
        ("Emerald Ball Gown", "Emerald Ball Gown"),       # only a name that STARTS with it changes
        ("ball gown 79", "ball gown 79"),
        ("Wedding Gown 18", "Wedding Gown 18"),
        ("", ""),
        (None, None),
    )

    def test_only_a_name_that_starts_with_ball_gown_changes(self):
        for before, after in self.CASES:
            with self.subTest(before=before):
                self.assertEqual(gowns_migration.swap_prefix(before, "Ball Gown", "Evening Gown"), after)
                self.assertEqual(reservations_migration.swap_prefix(before, "Ball Gown", "Evening Gown"), after)

    def test_it_also_goes_back(self):
        self.assertEqual(gowns_migration.swap_prefix("Evening Gown 79", "Evening Gown", "Ball Gown"), "Ball Gown 79")

    def test_a_gown_id_keeps_its_color_and_number(self):
        swap = gowns_migration.swap_gown_id
        self.assertEqual(swap("Ball Gown-BU-079", "Ball Gown", "Evening Gown"), "Evening Gown-BU-079")
        self.assertEqual(swap("Ball Gown Tulle-NV-051", "Ball Gown", "Evening Gown"), "Ball Gown Tulle-NV-051")
        self.assertEqual(swap("Long Gown-RD-035", "Ball Gown", "Evening Gown"), "Long Gown-RD-035")
        self.assertEqual(swap("", "Ball Gown", "Evening Gown"), "")


class RenameDataTests(TestCase):
    """The migrations' own functions, run on the real tables with shop-like rows in the OLD shape."""

    def setUp(self):
        self.g79 = make_gown("Ball Gown-BU-079", "Ball Gown 79", "Ball Gown", "ball-gown-79")
        self.g80a = make_gown("Ball Gown-GD-080", "Ball Gown 80", "Ball Gown", "ball-gown-80", "Gold", "GD")
        self.g80b = make_gown("Ball Gown-GD-081", "Ball Gown 80", "Ball Gown", "ball-gown-80-2", "Gold", "GD")
        self.emerald = make_gown("Ball Gown-GN-082", "Emerald Ball Gown", "Ball Gown", "emerald-ball-gown", "Green", "GN")
        self.tulle = make_gown("Ball Gown Tulle-NV-051", "Ball Gown Tulle 51", "Ball Gown Tulle", "ball-gown-tulle-51", "Navy", "NV")
        self.princess = make_gown("Wedding Gown-WH-022", "Ball Gown Princess", "Wedding Gown", "ball-gown-princess", "White", "WH")
        self.long = make_gown("Long Gown-RD-035", "Long Gown 35", "Long Gown", "long-gown-35", "Red", "RD")
        GownSequence.objects.create(category="Ball Gown", color_code="", next_value=84)
        GownSequence.objects.create(category="Ball Gown", color_code="BU", next_value=3)
        GownSequence.objects.create(category="Ball Gown Tulle", color_code="", next_value=52)
        GownRemoval.objects.create(gown_id="Ball Gown-GD-012", name="Ball Gown 12", category="Ball Gown", tracking_number=12, reason="Damaged")
        GownRemoval.objects.create(gown_id="Ball Gown Tulle-NV-009", name="Ball Gown Tulle 9", category="Ball Gown Tulle", tracking_number=9, reason="Retired")
        HiddenCategory.objects.create(name="Ball Gown")
        SiteSettings.objects.update_or_create(pk=1, defaults={"category_tag_colors": {"Ball Gown": "Gold", "Suit": "Black"}})
        CategoryCover.objects.create(key="ball-gown", image_url="https://res.example/x", storage_name="media/category_covers/x")
        self.customer = User.objects.create_user(username="rename_customer", password="x")
        self.reservation = Reservation.objects.create(customer=self.customer, customer_name="Rename Customer")

    def forward(self):
        gowns_migration.forwards(real_apps, None)

    def fields(self, gown):
        gown.refresh_from_db()
        return gown.category, gown.name, gown.gown_id

    def test_gowns_follow_and_keep_their_numbers_and_web_addresses(self):
        self.forward()
        self.assertEqual(self.fields(self.g79), ("Evening Gown", "Evening Gown 79", "Evening Gown-BU-079"))
        self.assertEqual(self.fields(self.g80a), ("Evening Gown", "Evening Gown 80", "Evening Gown-GD-080"))
        self.assertEqual(self.fields(self.g80b), ("Evening Gown", "Evening Gown 80", "Evening Gown-GD-081"))   # same product, second unit
        self.assertEqual(self.fields(self.emerald), ("Evening Gown", "Emerald Ball Gown", "Evening Gown-GN-082"))
        self.assertEqual([g.slug for g in (self.g79, self.g80a, self.g80b, self.emerald)],
                         ["ball-gown-79", "ball-gown-80", "ball-gown-80-2", "emerald-ball-gown"])
        self.assertFalse(Gown.objects.filter(category="Ball Gown").exists())

    def test_ball_gown_tulle_and_every_other_category_are_untouched(self):
        self.forward()
        self.assertEqual(self.fields(self.tulle), ("Ball Gown Tulle", "Ball Gown Tulle 51", "Ball Gown Tulle-NV-051"))
        self.assertEqual(self.fields(self.princess), ("Wedding Gown", "Ball Gown Princess", "Wedding Gown-WH-022"))
        self.assertEqual(self.fields(self.long), ("Long Gown", "Long Gown 35", "Long Gown-RD-035"))

    def test_the_next_gown_continues_the_numbering(self):
        self.forward()
        self.assertEqual(Gown.next_tracking_number("Evening Gown"), 84)
        self.assertEqual(Gown.next_tracking_number("Evening Gown"), 85)
        self.assertEqual(GownSequence.objects.get(category="Evening Gown", color_code="BU").next_value, 3)
        self.assertFalse(GownSequence.objects.filter(category="Ball Gown").exists())
        self.assertEqual(GownSequence.objects.get(category="Ball Gown Tulle", color_code="").next_value, 52)

    def test_two_counters_never_hand_out_a_number_twice(self):
        GownSequence.objects.create(category="Evening Gown", color_code="", next_value=5)   # e.g. created while the old code was still live
        self.forward()
        self.assertEqual(GownSequence.objects.get(category="Evening Gown", color_code="").next_value, 84)
        self.assertEqual(GownSequence.objects.filter(category="Evening Gown", color_code="").count(), 1)

    def test_the_removal_log_follows_so_the_missing_number_check_still_lines_up(self):
        self.forward()
        log = {r.gown_id: (r.category, r.name, r.tracking_number) for r in GownRemoval.objects.all()}
        self.assertEqual(log["Evening Gown-GD-012"], ("Evening Gown", "Evening Gown 12", 12))
        self.assertEqual(log["Ball Gown Tulle-NV-009"], ("Ball Gown Tulle", "Ball Gown Tulle 9", 9))

    def test_the_removed_category_list_the_tag_color_and_the_picture_follow(self):
        self.forward()
        self.assertEqual(list(HiddenCategory.objects.values_list("name", flat=True)), ["Evening Gown"])
        self.assertEqual(SiteSettings.objects.get(pk=1).category_tag_colors, {"Suit": "Black", "Evening Gown": "Gold"})
        self.assertEqual(list(CategoryCover.objects.values_list("key", "storage_name")), [("evening-gown", "media/category_covers/x")])

    def test_it_changes_no_last_updated_time(self):
        before = {g.pk: Gown.objects.get(pk=g.pk).updated_at for g in (self.g79, self.tulle)}
        self.forward()
        self.assertEqual({pk: Gown.objects.get(pk=pk).updated_at for pk in before}, before)

    def test_it_stops_before_changing_anything_if_the_owner_already_made_a_category_with_the_new_name(self):
        for custom in ({"name": "evening gown", "slug": "evening-gown-by-owner"}, {"name": "Gala", "slug": "evening-gown"}):
            with self.subTest(custom=custom):
                made = CustomCategory.objects.create(**custom)
                with self.assertRaises(RuntimeError) as raised:
                    self.forward()
                self.assertIn("already added", str(raised.exception))
                self.assertEqual(self.fields(self.g79), ("Ball Gown", "Ball Gown 79", "Ball Gown-BU-079"))
                made.delete()

    def test_going_back_restores_everything(self):
        self.forward()
        gowns_migration.backwards(real_apps, None)
        self.assertEqual(self.fields(self.g79), ("Ball Gown", "Ball Gown 79", "Ball Gown-BU-079"))
        self.assertEqual(self.fields(self.emerald), ("Ball Gown", "Emerald Ball Gown", "Ball Gown-GN-082"))
        self.assertEqual(self.fields(self.tulle), ("Ball Gown Tulle", "Ball Gown Tulle 51", "Ball Gown Tulle-NV-051"))
        self.assertEqual(GownSequence.objects.get(category="Ball Gown", color_code="").next_value, 84)
        self.assertEqual(list(HiddenCategory.objects.values_list("name", flat=True)), ["Ball Gown"])
        self.assertEqual(SiteSettings.objects.get(pk=1).category_tag_colors, {"Suit": "Black", "Ball Gown": "Gold"})
        self.assertEqual(list(CategoryCover.objects.values_list("key", flat=True)), ["ball-gown"])

    def item(self, gown, name):
        return ReservationItem.objects.create(
            reservation=self.reservation, gown=gown, gown_name=name, gown_slug=gown.slug if gown else "",
            rental_date=date(2027, 1, 10), return_date=date(2027, 1, 13),
        )

    def test_bookings_of_renamed_gowns_show_the_new_name_and_history_is_kept(self):
        mine = self.item(self.g79, "Ball Gown 79")
        returned = self.item(self.g80a, "Ball Gown 80")
        gone = self.item(None, "Ball Gown 12")                    # its gown was deleted long ago
        tulle = self.item(self.tulle, "Ball Gown Tulle 51")
        princess = self.item(self.princess, "Ball Gown Princess")
        stamp = ReservationItem.objects.get(pk=mine.pk).updated_at
        self.forward()
        reservations_migration.forwards(real_apps, None)
        names = {i.pk: i.gown_name for i in ReservationItem.objects.all()}
        self.assertEqual(names[mine.pk], "Evening Gown 79")
        self.assertEqual(names[returned.pk], "Evening Gown 80")
        self.assertEqual(names[gone.pk], "Ball Gown 12")
        self.assertEqual(names[tulle.pk], "Ball Gown Tulle 51")
        self.assertEqual(names[princess.pk], "Ball Gown Princess")
        self.assertEqual(ReservationItem.objects.get(pk=mine.pk).updated_at, stamp)   # no "news" badge for the customer
        reservations_migration.backwards(real_apps, None)
        self.assertEqual(ReservationItem.objects.get(pk=mine.pk).gown_name, "Ball Gown 79")


class EveningGownNamingTests(TestCase):
    """What the code now says: the choice, the tag color, the address and the old address."""

    def test_the_category_is_evening_gown_and_ball_gown_tulle_is_still_there(self):
        self.assertFalse(hasattr(Gown.Category, "BALL_GOWN"))
        self.assertEqual(Gown.Category.EVENING_GOWN.value, "Evening Gown")
        self.assertNotIn("Ball Gown", Gown.Category.values)
        self.assertIn("Ball Gown Tulle", Gown.Category.values)
        self.assertEqual(len(Gown.Category.values), 13)
        self.assertEqual(set(DEFAULT_CATEGORY_TAG_COLORS), set(Gown.Category.values))
        self.assertEqual(resolve_tag_colors({})["Evening Gown"], "Blue")
        self.assertIn("Evening Gown", all_category_names())
        self.assertNotIn("Ball Gown", all_category_names())

    def test_the_registry_row(self):
        row = next(c for c in _CATEGORIES if c["label"] == "Evening Gown")
        self.assertEqual((row["key"], row["url_name"]), ("evening-gown", "collection_evening_gown"))
        self.assertEqual(_RENAMED_COLLECTION_KEYS, {"ball-gown": "evening-gown"})
        self.assertEqual(reverse("gowns:collection_evening_gown"), "/collections/evening-gown/")
        with self.assertRaises(NoReverseMatch):
            reverse("gowns:collection_ball_gown")
        self.assertEqual(reverse("gowns:collection_ball_gown_tulle"), "/collections/ball-gown-tulle/")

    def test_the_picture_is_the_same_one_under_the_new_name(self):
        folder = settings.BASE_DIR / "static" / "images" / "categories"
        self.assertFalse((folder / "ball-gown.jpg").exists())
        with Image.open(folder / "evening-gown.jpg") as picture:
            self.assertEqual(picture.size, (600, 900))
        self.assertTrue((folder / "ball-gown-tulle.jpg").exists())


class EveningGownPagesTests(TestCase):
    """Customers: the page, the tiles, the menu -- and the address people already have."""

    @classmethod
    def setUpTestData(cls):
        cls.gown = make_gown("Evening Gown-BU-079", "Evening Gown 79", "Evening Gown", "ball-gown-79")

    def setUp(self):
        self.client = Client()

    def test_the_category_page_lists_its_gowns(self):
        response = self.client.get("/collections/evening-gown/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("ARABELA | Evening Gown Collection", html)
        self.assertIn("Evening Gown 79", html)
        self.assertIn("/collections/evening-gown/products/ball-gown-79/", html)

    def test_the_old_address_still_works_and_keeps_the_page_number(self):
        self.assertRedirects(self.client.get("/collections/ball-gown/"), "/collections/evening-gown/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/collections/ball-gown/?page=2"), "/collections/evening-gown/?page=2", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/ball-gown/"), "/collections/evening-gown/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/collections/ball-gown/", follow=True).status_code, 200)

    def test_an_old_product_link_opens_the_product_and_leads_back_to_evening_gown(self):
        response = self.client.get("/collections/ball-gown/products/ball-gown-79/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["collection"], "evening-gown")
        self.assertEqual(response.context["collection_url"], "/collections/evening-gown/")
        self.assertContains(response, "Evening Gown 79")
        new = self.client.get("/collections/evening-gown/products/ball-gown-79/")
        self.assertEqual(new.context["collection_url"], "/collections/evening-gown/")

    def test_the_tiles_and_menus_say_evening_gown_and_keep_the_same_picture(self):
        for path in ("/collections/", "/featured/women/", "/"):
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertIn('<img src="/static/images/categories/evening-gown.jpg" alt="Evening Gown"', html)
                self.assertNotIn('alt="Ball Gown"', html)
                self.assertIn(">Evening Gown<", html)
        collections = self.client.get("/collections/").content.decode()
        self.assertIn('alt="Ball Gown Tulle"', collections)                  # the other category is still there
        women = self.client.get("/featured/women/").content.decode()
        men = self.client.get("/featured/men/").content.decode()
        tile = 'href="/collections/evening-gown/" class="group cursor-pointer flex flex-col items-center"'
        self.assertIn(tile, women)
        self.assertNotIn(tile, men)

    def test_the_home_page_keeps_its_four_tiles_in_order(self):
        import re
        html = self.client.get("/").content.decode()
        self.assertEqual(re.findall(r'<img src="/static/images/categories/[^"]*" alt="([^"]*)"', html),
                         ["Long Gown", "Filipiniana", "Evening Gown", "Suit"])

    def test_search_and_the_all_page_know_the_new_name(self):
        html = self.client.get("/collections/all/").content.decode()
        self.assertIn("Evening Gown", html)
        search_data = self.client.get("/").content.decode()
        self.assertIn("Evening Gown 79", search_data)


class EveningGownStaffTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="evening_owner", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.owner, role=UserProfile.Role.OWNER)

    def setUp(self):
        self.client.force_login(self.owner)

    def add_gown(self, category):
        return self.client.post(reverse("arabela_admin:gown_create"), data={
            "name": "Evening Gown 1", "category": category, "color_name": "Blue", "color_code": "BU",
            "size": "Medium", "rental_price": "20000", "condition": "Good", "status": "Available",
        })

    def test_a_gown_can_be_added_as_an_evening_gown_and_not_as_a_ball_gown(self):
        response = self.add_gown("Evening Gown")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["gown"]["gown_id"], "Evening Gown-BU-001")
        self.assertEqual(self.add_gown("Ball Gown").status_code, 400)
        self.assertEqual(self.add_gown("Ball Gown Tulle").status_code, 200)

    def test_the_catalog_and_the_pictures_page_use_the_new_name(self):
        make_gown("Evening Gown-BU-079", "Evening Gown 79", "Evening Gown", "ball-gown-79")
        catalog = self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()
        self.assertIn("Evening Gown-BU-079", catalog)
        self.assertIn("Evening Gown", catalog)
        pictures = self.client.get(reverse("arabela_admin:categories")).content.decode()
        self.assertIn('data-key="evening-gown"', pictures)
        self.assertNotIn('data-key="ball-gown"', pictures)

    def test_a_category_cannot_be_added_with_the_old_or_new_name(self):
        def add(name):
            return self.client.post(
                reverse("arabela_admin:gown_category_create"), data=json.dumps({"name": name}), content_type="application/json",
            )
        self.assertEqual(add("Ball Gown").status_code, 400)       # the old address stays reserved for the renamed category
        self.assertIn("too close", add("Ball Gown").json()["error"])
        self.assertIn("already a category", add("Evening Gown").json()["error"])
        self.assertEqual(CustomCategory.objects.count(), 0)


class BagFilledBeforeTheRenameTests(TestCase):
    """A customer who put "Ball Gown 79" in the bag yesterday checks out after the rename."""

    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="stale_bag_customer", password="x")
        cls.gown = make_gown("Evening Gown-BU-079", "Evening Gown 79", "Evening Gown", "ball-gown-79")

    def setUp(self):
        self.client.force_login(self.customer)
        patcher = patch("gowns.views._save_proof_file", return_value="https://example.test/fake-proof.jpg")
        self.addCleanup(patcher.stop)
        patcher.start()

    def submit(self, item):
        return self.client.post(reverse("gowns:reservation_submit"), data={
            "items": json.dumps([item]), "first_name": "Test", "last_name": "Buyer", "phone": "09171234567",
            "address": "123 Test St", "city": "Test City", "postal_code": "1000", "payment_method": "GCash",
            "proof_of_payment": SimpleUploadedFile("proof.jpg", _TINY_JPEG, content_type="image/jpeg"),
        })

    def test_the_old_name_in_the_bag_still_books_the_right_gown_under_its_new_name(self):
        response = self.submit({
            "gown_name": "Ball Gown 79", "gown_slug": "ball-gown-79", "size": "Medium",
            "rental_date": "2027-01-10", "return_date": "2027-01-13",
        })
        self.assertEqual(response.status_code, 200, response.content)
        item = Reservation.objects.get(reference_code=response.json()["reference_code"]).items.get()
        self.assertEqual(item.gown_id, self.gown.id)
        self.assertEqual(item.gown_name, "Evening Gown 79")
        self.assertEqual(item.gown_slug, "ball-gown-79")

    def test_a_name_typed_in_other_capitals_is_stored_as_the_gowns_own_name(self):
        response = self.submit({
            "gown_name": "evening gown 79", "gown_slug": "ball-gown-79", "size": "Medium",
            "rental_date": "2027-02-10", "return_date": "2027-02-13",
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Reservation.objects.get(reference_code=response.json()["reference_code"]).items.get().gown_name, "Evening Gown 79")


class AiChatHelperSeesTheRenameTests(TestCase):
    """The "Arabela Recommends" chat helper is told about the shop from the live database on every message, so it
    must list Evening Gown (and only that) and link to its new pages."""

    def test_the_helper_is_told_about_evening_gown_and_never_links_the_old_address(self):
        from ai_recommendation import views as ai_views
        make_gown("Evening Gown-BU-079", "Evening Gown 79", "Evening Gown", "ball-gown-79")
        make_gown("Ball Gown Tulle-NV-051", "Ball Gown Tulle 51", "Ball Gown Tulle", "ball-gown-tulle-51", "Navy", "NV")
        catalog, allowed = ai_views._live_catalog()
        prompt = ai_views._system_prompt((catalog, allowed))
        self.assertIn("- Evening Gown (women's collection) -- browse all: [Evening Gown](/collections/evening-gown/)", catalog)
        self.assertIn("[Evening Gown 79](/collections/evening-gown/products/ball-gown-79/)", catalog)
        self.assertIn("- Ball Gown Tulle (women's collection)", catalog)
        self.assertEqual(prompt.replace("Ball Gown Tulle", "").count("Ball Gown"), 0)
        self.assertIn("/collections/evening-gown/", allowed)
        self.assertNotIn("/collections/ball-gown/", allowed)
        reply = "Try [Evening Gown 79](/collections/evening-gown/products/ball-gown-79/) or [Ball Gowns](/collections/ball-gown/)."
        cleaned = ai_views._sanitize_reply(reply, allowed, "arabela.example.com")
        self.assertIn("[Evening Gown 79](/collections/evening-gown/products/ball-gown-79/)", cleaned)
        self.assertNotIn("/collections/ball-gown/", cleaned)

