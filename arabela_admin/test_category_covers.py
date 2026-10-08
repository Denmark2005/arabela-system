"""Admin -> Categories: the owner-only page where each category's picture is uploaded, replaced or reset.
The customer-facing side (which picture a tile gets) is in gowns/test_category_covers.py."""
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
