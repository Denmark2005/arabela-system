"""Admin -> Categories: the owner-only page where each category's picture is uploaded, replaced or reset.
The customer-facing side (which picture a tile gets) is in gowns/test_category_covers.py."""
import base64
import html as htmllib
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError
from django.test import Client, TestCase
from django.urls import reverse
from PIL import Image, ImageDraw

from accounts.models import UserProfile
from gowns import cover_images
from gowns.models import CategoryCover, CustomCategory, HiddenCategory

User = get_user_model()

OLD = "https://files.example/category_covers/old.jpg"


def photo(name="gown.png", fmt="PNG", size=(600, 1000)):
    """A gown-like block on white, as an uploaded file."""
    image = Image.new("RGB", size, (255, 255, 255))
    ImageDraw.Draw(image).rectangle([size[0] * 0.3, size[1] * 0.1, size[0] * 0.7, size[1] * 0.9], fill=(150, 30, 60))
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=f"image/{fmt.lower()}")


def stored(storage, name="category_covers/abc.jpg"):
    storage.save.return_value = name
    storage.url.return_value = f"https://files.example/{name}"
    return f"https://files.example/{name}"


class OwnerCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="cover_owner", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.owner, role=UserProfile.Role.OWNER)
        cls.staff = User.objects.create_user(username="cover_staff", password="x", is_staff=True)
        UserProfile.objects.create(user=cls.staff, role=UserProfile.Role.STAFF)

    def setUp(self):
        self.client.force_login(self.owner)

    def upload(self, key, upload):
        data = {"key": key}
        if upload is not None:
            data["image"] = upload
        return self.client.post(reverse("arabela_admin:category_cover_upload"), data=data)

    def reset(self, key):
        return self.client.post(
            reverse("arabela_admin:category_cover_reset"), data=json.dumps({"key": key}), content_type="application/json",
        )


class CategoriesPageTests(OwnerCase):
    def page(self):
        return self.client.get(reverse("arabela_admin:categories"))

    @staticmethod
    def card(html, key):
        return html.split(f'data-key="{key}"')[1].split("</article>")[0]

    def test_the_owner_sees_a_card_for_every_category_the_customer_site_has(self):
        response = self.page()
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("<title>Categories | Arabela System Admin Panel</title>", html)
        self.assertEqual(html.count('<article class="cvr-card"'), 13)
        for label in ("Wedding Gown", "Evening Gown", "Long Gown", "Luxury Gown", "Mother Gown", "Suit", "Filipiniana",
                      "Guest Gown", "Dresses", "Kids Gown", "Barong", "Ball Gown Tulle", "Bridesmaid Dresses"):
            with self.subTest(label=label):
                self.assertIn(f'aria-label="{label}"', html)
        self.assertIn(f'data-upload-url="{reverse("arabela_admin:category_cover_upload")}"', html)
        self.assertIn(f'data-reset-url="{reverse("arabela_admin:category_cover_reset")}"', html)

    def test_the_page_stays_short_on_words(self):
        """The first version explained itself on every card and the owner found it cluttered: one short line now, the
        longer explanations only appear when they are needed (the question before a reset, an error)."""
        html = self.page().content.decode()
        main = html.split("<main>")[1].split("</main>")[0]
        for wordy in ("The standard picture is showing", "Customers see a new picture straight away", "Every category has a picture",
                      "Added by you", 'class="cvr-where"', "cvr-intro", "cvr-state"):
            with self.subTest(wordy=wordy):
                self.assertNotIn(wordy, main)
        self.assertEqual(main.count('class="cvr-hint"'), 1)
        self.assertEqual(main.count("Upload<"), 13)       # one short button per card

    def test_a_standard_card_is_the_picture_and_one_button_and_a_placeholder_card_says_so(self):
        html = htmllib.unescape(self.page().content.decode())
        long_gown = self.card(html, "long-gown")
        self.assertIn("/static/images/categories/long-gown.jpg", long_gown)
        self.assertIn("data-upload-label>Upload<", long_gown)
        self.assertIn("data-reset hidden", long_gown)                       # nothing of the owner's to reset
        self.assertIn('data-kind="none" hidden', long_gown)                 # a standard picture needs no badge
        self.assertIn("Shown on: Rentals page \u00b7 Women's collection page \u00b7 Home page", long_gown)   # as a hover tip
        barong = self.card(html, "barong")
        self.assertIn('data-photo="0"', barong)
        self.assertIn('data-kind="none">No picture<', barong)               # the one thing worth a word
        self.assertIn("Shown on: Rentals page \u00b7 Men's collection page", barong)
        self.assertNotIn("Home page", barong)
        self.assertIn("Men's collection page \u00b7 Home page", self.card(html, "suit"))

    def test_a_picture_the_owner_uploaded_gets_a_custom_badge_replace_and_a_way_back(self):
        CategoryCover.objects.create(key="barong", image_url="https://files.example/category_covers/b.jpg")
        CategoryCover.objects.create(key="suit", image_url="https://files.example/category_covers/s.jpg")
        html = self.page().content.decode()
        barong = self.card(html, "barong")
        self.assertIn("https://files.example/category_covers/b.jpg", barong)
        self.assertIn('data-kind="custom">Custom<', barong)
        self.assertIn("data-upload-label>Replace<", barong)
        self.assertNotIn("data-reset hidden", barong)
        self.assertIn("data-reset>Remove<", barong)                          # Barong has no standard picture to go back to
        self.assertIn("data-reset>Reset<", self.card(html, "suit"))          # Suit goes back to the one it came with

    def test_a_category_the_owner_added_has_a_card_and_a_removed_one_does_not(self):
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown", audience="women")
        HiddenCategory.objects.create(name="Dresses")
        html = self.page().content.decode()
        self.assertIn('data-key="debut-gown"', html)
        self.assertIn('aria-label="Debut Gown"', html)
        self.assertNotIn('data-key="dresses"', html)
        self.assertEqual(html.count('<article class="cvr-card"'), 13)

    def test_uploads_are_off_with_one_short_notice_until_the_database_update_is_applied(self):
        broken = MagicMock()
        broken.objects.exists.side_effect = DatabaseError("relation does not exist")
        broken.objects.values_list.side_effect = DatabaseError("relation does not exist")
        with patch("gowns.covers.CategoryCover", broken):
            response = self.page()
        html = htmllib.unescape(response.content.decode())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(html.count("Uploads aren't ready yet."), 1)
        self.assertIn('data-ready="0"', html)
        self.assertEqual(html.count('accept="image/jpeg,image/png,image/webp" disabled'), 13)   # nothing to press that would fail
        self.assertEqual(html.count('<article class="cvr-card"'), 13)                          # the standard pictures still show
        self.assertIn("/static/images/categories/long-gown.jpg", html)

    def test_when_uploads_are_ready_there_is_no_notice_and_nothing_is_disabled(self):
        html = htmllib.unescape(self.page().content.decode())
        self.assertNotIn("Uploads aren't ready yet.", html)
        self.assertIn('data-ready="1"', html)
        self.assertNotIn('accept="image/jpeg,image/png,image/webp" disabled', html)

    def test_every_card_has_save_and_cancel_ready_but_hidden_until_a_photo_is_chosen(self):
        html = self.page().content.decode()
        self.assertIn(f'data-preview-url="{reverse("arabela_admin:category_cover_preview")}"', html)
        self.assertEqual(html.count("data-save hidden"), 13)
        self.assertEqual(html.count("data-cancel hidden"), 13)
        self.assertIn("at least 400 &times; 650 px", html)           # the rules in one short line
        self.assertEqual(html.split("<main>")[1].split("</main>")[0].count('class="cvr-hint"'), 1)

    def test_staff_who_are_not_the_owner_are_sent_back_to_the_dashboard_and_signed_out_to_the_login(self):
        self.client.force_login(self.staff)
        response = self.page()
        self.assertRedirects(response, reverse("arabela_admin:dashboard"), fetch_redirect_response=False)
        self.client.logout()
        self.assertRedirects(self.page(), reverse("arabela_admin:admin_login"), fetch_redirect_response=False)

    def test_the_page_sets_the_csrf_cookie_its_buttons_need(self):
        self.assertIn("csrftoken", self.page().cookies)

    def test_the_sidebar_opens_on_inventory_management_for_this_page(self):
        html = self.page().content.decode()
        self.assertIn("page === 'gown' || page === 'categories'", html)
        self.assertIn("x-data=\"{ page: 'categories'", html)


class SidebarLinkTests(OwnerCase):
    """Every admin page that has the sidebar links to Categories -- for the owner only."""

    PAGES = (
        ("dashboard", {}), ("rental_schedule", {}), ("payment_verification", {}), ("rental_history", {}),
        ("security_deposits", {}), ("reservation_records", {}), ("active_reservations", {}), ("pending_approval", {}),
        ("gown_catalog", {}), ("clients", {}), ("staff_management", {}),
        ("page", {"page": "profile"}), ("page", {"page": "account-settings"}), ("categories", {}),
    )

    def link(self):
        return f'href="{reverse("arabela_admin:categories")}"'

    def test_the_owner_sees_the_link_on_every_admin_page(self):
        for name, kwargs in self.PAGES:
            with self.subTest(view=name, args=str(kwargs)):
                response = self.client.get(reverse(f"arabela_admin:{name}", kwargs=kwargs))
                self.assertEqual(response.status_code, 200)
                self.assertIn(self.link(), response.content.decode())

    def test_staff_never_see_the_link(self):
        self.client.force_login(self.staff)
        for name, kwargs in self.PAGES:
            if name in ("staff_management", "categories"):
                continue   # owner-only pages: staff are redirected, covered elsewhere
            with self.subTest(view=name, args=str(kwargs)):
                response = self.client.get(reverse(f"arabela_admin:{name}", kwargs=kwargs))
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(self.link(), response.content.decode())

    def test_no_admin_template_with_the_sidebar_was_left_without_the_link(self):
        folder = Path(settings.BASE_DIR) / "templates" / "arabela_admin"
        with_sidebar = []
        for path in sorted(folder.glob("*.html")):
            text = path.read_text(encoding="utf-8")
            if "arabela_admin:gown_catalog" in text and "menu-dropdown-item" in text:
                with_sidebar.append(path.name)
                self.assertIn("{% url 'arabela_admin:categories' %}", text, f"{path.name} has the sidebar but no Categories link")
        self.assertGreaterEqual(len(with_sidebar), 14)


class CategoryCoverUploadTests(OwnerCase):
    def test_the_owner_can_give_a_category_a_picture(self):
        with patch("arabela_admin.views.default_storage") as storage:
            url = stored(storage)
            response = self.upload("barong", photo())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"success": True, "url": url, "is_photo": True, "has_upload": True})
        saved = CategoryCover.objects.get(key="barong")
        self.assertEqual((saved.image_url, saved.storage_name), (url, "category_covers/abc.jpg"))

    def test_what_is_stored_is_the_finished_tile_picture_not_the_raw_upload(self):
        with patch("arabela_admin.views.default_storage") as storage:
            stored(storage)
            self.upload("barong", photo())
        name, content = storage.save.call_args.args
        self.assertRegex(name, r"^category_covers/[0-9a-f]{32}\.jpg$")
        result = Image.open(io.BytesIO(content.read()))
        self.assertEqual((result.format, result.size), ("JPEG", (600, 900)))
        self.assertEqual(result.convert("RGB").getpixel((3, 3)), (251, 251, 251))   # the white backdrop is now the tile grey

    def test_jpg_and_webp_uploads_work_too(self):
        for name, fmt in (("a.jpg", "JPEG"), ("a.jpeg", "JPEG"), ("a.webp", "WEBP")):
            with self.subTest(name=name):
                with patch("arabela_admin.views.default_storage") as storage:
                    stored(storage)
                    self.assertEqual(self.upload("suit", photo(name=name, fmt=fmt)).status_code, 200)

    def test_replacing_a_picture_removes_the_old_file(self):
        CategoryCover.objects.create(key="barong", image_url=OLD, storage_name="category_covers/old.jpg")
        with patch("arabela_admin.views.default_storage") as storage:
            url = stored(storage, "category_covers/new.jpg")
            self.assertEqual(self.upload("barong", photo()).status_code, 200)
            storage.delete.assert_called_once_with("category_covers/old.jpg")
        saved = CategoryCover.objects.get(key="barong")
        self.assertEqual((saved.image_url, saved.storage_name), (url, "category_covers/new.jpg"))
        self.assertEqual(CategoryCover.objects.filter(key="barong").count(), 1)

    def test_a_category_the_owner_added_can_have_a_picture_too(self):
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        with patch("arabela_admin.views.default_storage") as storage:
            stored(storage)
            self.assertEqual(self.upload("debut-gown", photo()).status_code, 200)
        self.assertTrue(CategoryCover.objects.filter(key="debut-gown").exists())

    def test_a_bad_request_is_refused_and_nothing_is_stored(self):
        too_big = SimpleUploadedFile("big.png", b"x" * (5 * 1024 * 1024 + 1), content_type="image/png")
        cases = {
            "no category": ("", photo(), 404),
            "unknown category": ("not-a-category", photo(), 404),
            "no file": ("barong", None, 400),
            "wrong type": ("barong", SimpleUploadedFile("a.gif", b"GIF89a", content_type="image/gif"), 400),
            "renamed text file": ("barong", SimpleUploadedFile("a.png", b"not an image at all", content_type="image/png"), 400),
            "too big": ("barong", too_big, 400),
            "too small": ("barong", photo(size=(100, 150)), 400),
        }
        with patch("arabela_admin.views.default_storage") as storage:
            for label, (key, upload, status) in cases.items():
                with self.subTest(case=label):
                    response = self.upload(key, upload)
                    self.assertEqual(response.status_code, status, response.content)
                    self.assertIn("error", response.json())
            storage.save.assert_not_called()
        self.assertEqual(CategoryCover.objects.count(), 0)

    def test_a_category_that_was_removed_cannot_be_given_a_picture(self):
        HiddenCategory.objects.create(name="Barong")
        with patch("arabela_admin.views.default_storage") as storage:
            self.assertEqual(self.upload("barong", photo()).status_code, 404)
            storage.save.assert_not_called()

    def test_staff_who_are_not_the_owner_are_refused_and_nothing_is_stored(self):
        self.client.force_login(self.staff)
        with patch("arabela_admin.views.default_storage") as storage:
            self.assertEqual(self.upload("barong", photo()).status_code, 403)
            self.assertEqual(self.reset("barong").status_code, 403)
            storage.save.assert_not_called()
        self.assertEqual(CategoryCover.objects.count(), 0)

    def test_signed_out_gets_json_401(self):
        self.client.logout()
        with patch("arabela_admin.views.default_storage") as storage:
            self.assertEqual(self.upload("barong", photo()).status_code, 401)
            self.assertEqual(self.reset("barong").status_code, 401)
            storage.save.assert_not_called()

    def test_get_requests_are_not_allowed(self):
        self.assertEqual(self.client.get(reverse("arabela_admin:category_cover_upload")).status_code, 405)
        self.assertEqual(self.client.get(reverse("arabela_admin:category_cover_reset")).status_code, 405)

    def test_a_storage_failure_is_a_clean_502_and_keeps_the_old_picture(self):
        CategoryCover.objects.create(key="barong", image_url=OLD)
        with patch("arabela_admin.views.default_storage") as storage:
            storage.save.side_effect = RuntimeError("storage down")
            response = self.upload("barong", photo())
            storage.delete.assert_not_called()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(CategoryCover.objects.get(key="barong").image_url, OLD)

    def test_a_database_that_is_not_ready_is_a_clean_503_and_leaves_no_orphan_file(self):
        with patch("arabela_admin.views.default_storage") as storage:
            stored(storage, "category_covers/orphan.jpg")
            with patch.object(CategoryCover.objects, "update_or_create", side_effect=DatabaseError("no such table")):
                response = self.upload("barong", photo())
            storage.delete.assert_called_once_with("category_covers/orphan.jpg")
        self.assertEqual(response.status_code, 503)
        self.assertIn("database update", response.json()["error"])

    def test_the_new_picture_is_what_customers_see_straight_away(self):
        with patch("arabela_admin.views.default_storage") as storage:
            url = stored(storage)
            self.upload("barong", photo())
        html = Client().get(reverse("gowns:featured_men_collections")).content.decode()
        self.assertIn(f'<img src="{url}" alt="Barong"', html)
        self.assertIn(f'<img src="/static/images/categories/suit.jpg" alt="Suit"', html)


class CategoryCoverResetTests(OwnerCase):
    def test_reset_takes_the_owners_picture_away_and_the_standard_one_comes_back(self):
        CategoryCover.objects.create(key="suit", image_url="https://files.example/category_covers/s.jpg", storage_name="category_covers/s.jpg")
        with patch("arabela_admin.views.default_storage") as storage:
            response = self.reset("suit")
            storage.delete.assert_called_once_with("category_covers/s.jpg")
        self.assertEqual(response.json(), {
            "success": True, "url": "/static/images/categories/suit.jpg", "is_photo": True, "has_upload": False,
        })
        self.assertFalse(CategoryCover.objects.filter(key="suit").exists())

    def test_a_category_with_no_standard_picture_goes_back_to_the_placeholder(self):
        CategoryCover.objects.create(key="barong", image_url="https://files.example/category_covers/b.jpg")
        with patch("arabela_admin.views.default_storage"):
            response = self.reset("barong")
        self.assertEqual(response.json(), {
            "success": True, "url": "/static/images/categories/placeholder.jpg", "is_photo": False, "has_upload": False,
        })

    def test_resetting_a_category_that_has_no_picture_of_its_own_is_harmless(self):
        with patch("arabela_admin.views.default_storage") as storage:
            response = self.reset("long-gown")
            storage.delete.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["url"], "/static/images/categories/long-gown.jpg")

    def test_a_bad_reset_is_refused(self):
        self.assertEqual(self.reset("not-a-category").status_code, 404)
        url = reverse("arabela_admin:category_cover_reset")
        for body in (b"not json", b"[]", b"5"):
            with self.subTest(body=body):
                self.assertIn(self.client.post(url, data=body, content_type="application/json").status_code, (400, 404))

    def test_an_outside_url_is_never_deleted_from_storage(self):
        CategoryCover.objects.create(key="barong", image_url="https://elsewhere.example/barong.jpg")
        with patch("arabela_admin.views.default_storage") as storage:
            self.assertEqual(self.reset("barong").status_code, 200)
            storage.delete.assert_not_called()

    def test_only_files_in_this_features_own_folder_are_ever_deleted(self):
        for index, name in enumerate(("gown_photos/precious.jpg", "category_covers/../gown_photos/precious.jpg", "gcash_qr/qr.png")):
            with self.subTest(name=name):
                CategoryCover.objects.update_or_create(key="barong", defaults={"image_url": f"https://files.example/{index}.jpg", "storage_name": name})
                with patch("arabela_admin.views.default_storage") as storage:
                    self.assertEqual(self.reset("barong").status_code, 200)
                    storage.delete.assert_not_called()

    def test_a_cloudinary_style_name_is_deleted_exactly_as_the_storage_gave_it(self):
        """On Cloudinary the saved name carries a folder prefix the public address does not show, so the file must be
        deleted by the name save() returned, not by one worked out from the address."""
        with patch("arabela_admin.views.default_storage") as storage:
            storage.save.return_value = "media/category_covers/abc_x1y2z3"
            storage.url.return_value = "https://res.cloudinary.com/demo/image/upload/v1/media/category_covers/abc_x1y2z3"
            self.assertEqual(self.upload("barong", photo()).status_code, 200)
            self.assertEqual(self.reset("barong").status_code, 200)
            storage.delete.assert_called_once_with("media/category_covers/abc_x1y2z3")


class RemovingACategoryAlsoRemovesItsPictureTests(OwnerCase):
    def test_removing_a_category_the_owner_added_removes_its_picture_and_file(self):
        category = CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        CategoryCover.objects.create(key="debut-gown", image_url="https://files.example/category_covers/debut.jpg", storage_name="category_covers/debut.jpg")
        with patch("arabela_admin.views.default_storage") as storage:
            response = self.client.post(reverse("arabela_admin:gown_category_delete", args=[category.id]))
            storage.delete.assert_called_once_with("category_covers/debut.jpg")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CustomCategory.objects.filter(pk=category.pk).exists())
        self.assertFalse(CategoryCover.objects.filter(key="debut-gown").exists())

    def test_a_new_category_with_the_same_name_does_not_inherit_the_old_picture(self):
        category = CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        CategoryCover.objects.create(key="debut-gown", image_url="https://files.example/category_covers/debut.jpg")
        with patch("arabela_admin.views.default_storage"):
            self.client.post(reverse("arabela_admin:gown_category_delete", args=[category.id]))
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        html = Client().get(reverse("gowns:collections")).content.decode()
        self.assertIn('<img src="/static/images/categories/placeholder.jpg" alt="Debut Gown"', html)

    def test_removing_a_category_still_works_when_the_pictures_table_is_not_ready(self):
        category = CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        broken = MagicMock()
        broken.objects.filter.side_effect = DatabaseError("no such table")
        with patch("arabela_admin.views.CategoryCover", broken):
            response = self.client.post(reverse("arabela_admin:gown_category_delete", args=[category.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CustomCategory.objects.filter(pk=category.pk).exists())

    def test_removing_a_built_in_category_keeps_its_picture_so_adding_it_back_restores_it(self):
        CategoryCover.objects.create(key="dresses", image_url="https://files.example/category_covers/d.jpg")
        response = self.client.post(
            reverse("arabela_admin:gown_builtin_category_remove"), data=json.dumps({"name": "Dresses"}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(CategoryCover.objects.filter(key="dresses").exists())


def photo_of(size=(1200, 2000), box=(0.30, 0.10, 0.70, 0.90), background=(255, 255, 255), name="gown.png", fmt="PNG", colour=(150, 30, 60)):
    """A gown-like block on a backdrop, as an uploaded file -- the knobs let a test break exactly one picture rule."""
    image = Image.new("RGB", size, background)
    ImageDraw.Draw(image).rectangle([size[0] * box[0], size[1] * box[1], size[0] * box[2], size[1] * box[3]], fill=colour)
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=f"image/{fmt.lower()}")


# one photo per picture rule, and the exact message the owner must see
BROKEN_RULES = (
    ("a wide photo", lambda: photo_of(size=(2000, 1300)), cover_images.MSG_NOT_TALL),
    ("a square photo", lambda: photo_of(size=(1500, 1500)), cover_images.MSG_NOT_TALL),
    ("a small photo", lambda: photo_of(size=(390, 700)), cover_images.MSG_TOO_SMALL),
    ("a photo taken in a room", lambda: photo_of(background=(60, 90, 120)), cover_images.MSG_BACKGROUND),
    ("a gown cut off at the bottom", lambda: photo_of(box=(0.30, 0.10, 0.70, 1.00)), cover_images.MSG_CUT_OFF),
    ("a gown far too small in the photo", lambda: photo_of(box=(0.40, 0.45, 0.60, 0.55)), cover_images.MSG_GOWN_SMALL),
)


class CategoryCoverPreviewTests(OwnerCase):
    """Choosing a photo shows the finished picture in its card before anything is saved."""

    def preview(self, key, upload):
        data = {"key": key}
        if upload is not None:
            data["image"] = upload
        return self.client.post(reverse("arabela_admin:category_cover_preview"), data=data)

    def test_a_good_photo_previews_the_finished_picture_and_stores_nothing(self):
        with patch("arabela_admin.views.default_storage") as storage:
            response = self.preview("barong", photo_of())
            storage.save.assert_not_called()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["warnings"], [])
        self.assertTrue(body["preview"].startswith("data:image/jpeg;base64,"))
        picture = Image.open(io.BytesIO(base64.b64decode(body["preview"].split(",", 1)[1])))
        self.assertEqual((picture.format, picture.size), ("JPEG", (600, 900)))
        self.assertEqual(picture.convert("RGB").getpixel((3, 3)), (251, 251, 251))
        self.assertEqual(CategoryCover.objects.count(), 0)

    def test_a_small_but_acceptable_photo_comes_with_a_note(self):
        body = self.preview("barong", photo_of(size=(500, 800))).json()
        self.assertEqual(body["warnings"], [cover_images.NOTE_SOFT])

    def test_each_picture_rule_has_its_own_short_message_and_nothing_is_stored(self):
        with patch("arabela_admin.views.default_storage") as storage:
            for label, make, message in BROKEN_RULES:
                with self.subTest(photo=label):
                    response = self.preview("barong", make())
                    self.assertEqual(response.status_code, 400, response.content)
                    self.assertEqual(response.json(), {"error": message})
            storage.save.assert_not_called()
        self.assertEqual(CategoryCover.objects.count(), 0)

    def test_a_bad_request_is_refused(self):
        too_big = SimpleUploadedFile("big.png", b"x" * (5 * 1024 * 1024 + 1), content_type="image/png")
        cases = {
            "no category": ("", photo_of(), 404),
            "unknown category": ("not-a-category", photo_of(), 404),
            "no file": ("barong", None, 400),
            "wrong type": ("barong", SimpleUploadedFile("a.gif", b"GIF89a", content_type="image/gif"), 400),
            "a text file named .png": ("barong", SimpleUploadedFile("a.png", b"not an image at all", content_type="image/png"), 400),
            "over 5 MB": ("barong", too_big, 400),
        }
        for label, (key, upload, status) in cases.items():
            with self.subTest(case=label):
                response = self.preview(key, upload)
                self.assertEqual(response.status_code, status, response.content)
                self.assertIn("error", response.json())

    def test_only_the_owner_can_preview(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.preview("barong", photo_of()).status_code, 403)
        self.client.logout()
        self.assertEqual(self.preview("barong", photo_of()).status_code, 401)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("arabela_admin:category_cover_preview")).status_code, 405)

    def test_the_preview_never_touches_the_pictures_table_so_it_works_before_the_database_update(self):
        broken = MagicMock()
        broken.objects.filter.side_effect = DatabaseError("relation does not exist")
        with patch("arabela_admin.views.CategoryCover", broken):
            self.assertEqual(self.preview("barong", photo_of()).status_code, 200)


class UploadFollowsTheSameRulesTests(OwnerCase):
    """The preview can't be skipped: Save goes through the very same rules on the server."""

    def test_a_photo_that_breaks_a_rule_is_refused_and_nothing_is_stored(self):
        with patch("arabela_admin.views.default_storage") as storage:
            for label, make, message in BROKEN_RULES:
                with self.subTest(photo=label):
                    response = self.upload("barong", make())
                    self.assertEqual(response.status_code, 400, response.content)
                    self.assertEqual(response.json(), {"error": message})
            storage.save.assert_not_called()
        self.assertEqual(CategoryCover.objects.count(), 0)

    def test_what_is_saved_is_exactly_what_the_preview_showed(self):
        upload_bytes = photo_of().read()
        preview = self.client.post(reverse("arabela_admin:category_cover_preview"), data={
            "key": "barong", "image": SimpleUploadedFile("gown.png", upload_bytes, content_type="image/png"),
        }).json()
        with patch("arabela_admin.views.default_storage") as storage:
            stored(storage)
            response = self.upload("barong", SimpleUploadedFile("gown.png", upload_bytes, content_type="image/png"))
            _name, content = storage.save.call_args.args
            saved_bytes = content.read()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(saved_bytes, base64.b64decode(preview["preview"].split(",", 1)[1]))


class OwnerOnlyCategoryToolsTests(OwnerCase):
    """Adding and removing categories, and the Categories picture page, belong to the owner alone. Staff and managers,
    signed-in or not, are refused by the SERVER (not only hidden), and nothing changes."""

    def setUp(self):
        super().setUp()
        self.custom = CustomCategory.objects.create(name="Debut Gown", slug="debut-gown", audience="women")
        self.manager = User.objects.create_user(username="cover_manager", password="x", is_staff=True)
        UserProfile.objects.create(user=self.manager, role=UserProfile.Role.MANAGER)
        self.no_profile = User.objects.create_user(username="cover_no_profile", password="x", is_staff=True)

    def calls(self):
        """(label, a function that makes the request) for every endpoint the owner-only rule covers."""
        client = self.client

        def post_json(name, payload, args=()):
            return lambda: client.post(reverse(f"arabela_admin:{name}", args=args), data=json.dumps(payload), content_type="application/json")

        return (
            ("add a category", post_json("gown_category_create", {"name": "Prom Gown"})),
            ("remove a category the owner added", post_json("gown_category_delete", {}, args=[self.custom.id])),
            ("move a category between collections", post_json("gown_category_audience", {"audience": "men"}, args=[self.custom.id])),
            ("remove an original category", post_json("gown_builtin_category_remove", {"name": "Dresses"})),
            ("preview a category picture", lambda: client.post(reverse("arabela_admin:category_cover_preview"), data={"key": "barong", "image": photo_of()})),
            ("upload a category picture", lambda: client.post(reverse("arabela_admin:category_cover_upload"), data={"key": "barong", "image": photo_of()})),
            ("reset a category picture", post_json("category_cover_reset", {"key": "barong"})),
        )

    def assert_nothing_changed(self):
        self.assertEqual(list(CustomCategory.objects.values_list("name", flat=True)), ["Debut Gown"])
        self.assertEqual(CustomCategory.objects.get().audience, "women")
        self.assertEqual(HiddenCategory.objects.count(), 0)
        self.assertEqual(CategoryCover.objects.count(), 0)

    def test_staff_managers_and_profile_less_staff_are_refused_by_the_server(self):
        with patch("arabela_admin.views.default_storage") as storage:
            for user in (self.staff, self.manager, self.no_profile):
                self.client.force_login(user)
                for label, call in self.calls():
                    with self.subTest(user=user.username, action=label):
                        response = call()
                        self.assertEqual(response.status_code, 403, response.content)
                        self.assertIn("owner", response.json()["error"])
            storage.save.assert_not_called()
        self.assert_nothing_changed()

    def test_signed_out_visitors_are_refused_too(self):
        self.client.logout()
        with patch("arabela_admin.views.default_storage") as storage:
            for label, call in self.calls():
                with self.subTest(action=label):
                    self.assertEqual(call().status_code, 401)
            storage.save.assert_not_called()
        self.assert_nothing_changed()

    def test_the_owner_can_do_all_of_it(self):
        self.client.force_login(self.owner)

        def post_json(name, payload, args=()):
            return self.client.post(reverse(f"arabela_admin:{name}", args=args), data=json.dumps(payload), content_type="application/json")

        with patch("arabela_admin.views.default_storage") as storage:
            stored(storage)
            results = {
                "add a category": post_json("gown_category_create", {"name": "Prom Gown"}),
                "move a category between collections": post_json("gown_category_audience", {"audience": "men"}, args=[self.custom.id]),
                "preview a category picture": self.client.post(reverse("arabela_admin:category_cover_preview"), data={"key": "barong", "image": photo_of()}),
                "upload a category picture": self.client.post(reverse("arabela_admin:category_cover_upload"), data={"key": "barong", "image": photo_of()}),
                "reset a category picture": post_json("category_cover_reset", {"key": "barong"}),
                "remove an original category": post_json("gown_builtin_category_remove", {"name": "Dresses"}),
                "remove a category the owner added": post_json("gown_category_delete", {}, args=[self.custom.id]),
            }
        self.assertEqual({label: response.status_code for label, response in results.items()}, {label: 200 for label in results})
        self.assertTrue(CustomCategory.objects.filter(name="Prom Gown").exists())
        self.assertFalse(CustomCategory.objects.filter(pk=self.custom.pk).exists())
        self.assertTrue(HiddenCategory.objects.filter(name="Dresses").exists())

    def test_the_superuser_with_no_profile_is_the_owner(self):
        """The real `admin` account is a superuser; that must be enough."""
        root = User.objects.create_superuser("cover_root", "root@example.test", "x")
        self.client.force_login(root)
        self.assertEqual(self.client.get(reverse("arabela_admin:categories")).status_code, 200)
        response = self.client.post(
            reverse("arabela_admin:gown_category_create"), data=json.dumps({"name": "Prom Gown"}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_staff_do_not_even_see_the_buttons_or_the_pictures_page(self):
        self.client.force_login(self.owner)
        owner_catalog = self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()
        self.assertIn("+ Add Category", owner_catalog)
        self.assertIn("brought back by adding a category with the same name", owner_catalog)
        self.assertIn(reverse("arabela_admin:categories"), self.client.get(reverse("arabela_admin:dashboard")).content.decode())
        for user in (self.staff, self.manager, self.no_profile):
            self.client.force_login(user)
            with self.subTest(user=user.username):
                catalog = self.client.get(reverse("arabela_admin:gown_catalog"))
                self.assertEqual(catalog.status_code, 200)
                html = catalog.content.decode()
                self.assertNotIn("+ Add Category", html)
                self.assertNotIn("brought back by adding a category with the same name", html)
                self.assertRedirects(self.client.get(reverse("arabela_admin:categories")), reverse("arabela_admin:dashboard"), fetch_redirect_response=False)
        # the sidebar of an ordinary staff or manager account has no Categories link
        for user in (self.staff, self.manager):
            self.client.force_login(user)
            with self.subTest(sidebar=user.username):
                self.assertNotIn(reverse("arabela_admin:categories"), self.client.get(reverse("arabela_admin:dashboard")).content.decode())

