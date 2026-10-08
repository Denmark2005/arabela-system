"""Category pictures on the customer site: the picture-making code, which picture each tile gets, and the four
pages that draw tiles (Rentals, Women's, Men's, home). The owner-facing upload side is in
arabela_admin/test_category_covers.py."""
import json
import re
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.db import DatabaseError, connection
from django.test import Client, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from PIL import Image, ImageDraw

from gowns import cover_images
from gowns.covers import BUNDLED_COVER_KEYS, PLACEHOLDER_STATIC_PATH, cover_for, owner_cover_urls
from gowns.models import CategoryCover, CustomCategory, HiddenCategory

CATEGORY_PICTURES = Path(settings.BASE_DIR) / "static" / "images" / "categories"
GREY = (251, 251, 251)


def studio_photo(width=600, height=1000, colour=(120, 30, 60), background=(255, 255, 255), box=(0.30, 0.10, 0.70, 0.90), mode="RGB"):
    """A gown-like block on a plain backdrop -- the shape of the shop's own photos."""
    image = Image.new(mode, (width, height), background)
    ImageDraw.Draw(image).rectangle(
        [int(width * box[0]), int(height * box[1]), int(width * box[2]), int(height * box[3])], fill=colour,
    )
    return image


def content_box(image, threshold=238):
    """Where anything darker than the tile grey sits, as fractions of the picture."""
    mask = image.convert("L").point(lambda value: 255 if value < threshold else 0)
    left, top, right, bottom = mask.getbbox()
    return left / image.width, top / image.height, right / image.width, bottom / image.height


def jpeg_upload(image, name="gown.jpg", fmt="JPEG"):
    buffer = BytesIO()
    image.save(buffer, format=fmt)
    buffer.seek(0)
    buffer.name = name
    return buffer


class CoverImageTests(SimpleTestCase):
    """gowns.cover_images: one function makes every tile picture, bundled or uploaded."""

    def test_a_studio_photo_becomes_the_tile_shape_on_the_tile_grey(self):
        cover = cover_images.build_cover(studio_photo())
        self.assertEqual(cover.size, (cover_images.COVER_WIDTH, cover_images.COVER_HEIGHT))
        self.assertEqual((cover.width * 3, cover.height * 2), (cover.height * 2, cover.width * 3))  # 2:3
        for corner in ((2, 2), (cover.width - 3, 2), (2, cover.height - 3), (cover.width - 3, cover.height - 3)):
            self.assertEqual(cover.getpixel(corner), GREY)

    def test_the_white_background_is_shaded_to_the_tile_grey_but_the_gown_keeps_its_colour(self):
        cover = cover_images.build_cover(studio_photo(colour=(200, 20, 40)))
        centre = cover.getpixel((cover.width // 2, cover.height // 2))
        for got, want in zip(centre, (200, 20, 40)):
            self.assertLessEqual(abs(got - round(want * 251 / 255)), 3)

    def test_the_gown_fills_the_stage_and_the_bottom_strip_stays_free_for_the_name(self):
        left, top, right, bottom = content_box(cover_images.build_cover(studio_photo()))
        self.assertGreaterEqual(left, 0.05)
        self.assertLessEqual(right, 0.95)
        self.assertGreaterEqual(top, 0.03)
        self.assertLessEqual(bottom, 0.86)   # the strip below 86% is where the name is drawn
        self.assertGreaterEqual(bottom - top, 0.60)   # and the gown is not tiny

    def test_a_wide_gown_is_never_cut_off_at_the_sides(self):
        left, top, right, bottom = content_box(cover_images.build_cover(studio_photo(box=(0.05, 0.35, 0.95, 0.70))))
        self.assertGreater(left, 0.04)
        self.assertLess(right, 0.96)

    def test_a_photo_with_a_transparent_background_is_treated_as_white(self):
        photo = Image.new("RGBA", (600, 1000), (0, 0, 0, 0))
        ImageDraw.Draw(photo).rectangle([180, 100, 420, 900], fill=(30, 90, 160, 255))
        cover = cover_images.build_cover(photo)
        self.assertEqual(cover.getpixel((2, 2)), GREY)

    def test_a_phone_photo_with_a_rotation_flag_is_stood_upright(self):
        landscape = studio_photo(width=1000, height=600, box=(0.1, 0.30, 0.90, 0.70))
        exif = Image.Exif()
        exif[274] = 6  # "rotate 90 degrees clockwise to view"
        buffer = BytesIO()
        landscape.save(buffer, format="JPEG", exif=exif)
        buffer.seek(0)
        cover = cover_images.build_cover(Image.open(buffer))
        self.assertEqual(cover.getpixel((2, 2)), GREY)   # upright portrait -> recognised as a studio photo

    def test_a_scene_photo_fills_the_whole_tile(self):
        scene = Image.new("RGB", (900, 600), (20, 80, 140))   # landscape, coloured backdrop
        cover = cover_images.build_cover(scene)
        self.assertEqual(cover.size, (cover_images.COVER_WIDTH, cover_images.COVER_HEIGHT))
        self.assertEqual(cover.getpixel((2, 2)), (20, 80, 140))
        portrait_scene = Image.new("RGB", (600, 1000), (30, 120, 60))
        self.assertEqual(cover_images.build_cover(portrait_scene).getpixel((300, 880)), (30, 120, 60))

    def test_a_blank_white_photo_does_not_crash(self):
        cover = cover_images.build_cover(Image.new("RGB", (500, 900), (255, 255, 255)))
        self.assertEqual(cover.size, (cover_images.COVER_WIDTH, cover_images.COVER_HEIGHT))

    def test_an_upload_comes_back_as_a_jpeg_of_the_tile_size(self):
        data = cover_images.cover_from_upload(jpeg_upload(studio_photo()))
        result = Image.open(BytesIO(data))
        self.assertEqual((result.format, result.size), ("JPEG", (600, 900)))

    def test_png_and_webp_uploads_work_too(self):
        for fmt, name in (("PNG", "gown.png"), ("WEBP", "gown.webp")):
            with self.subTest(fmt=fmt):
                data = cover_images.cover_from_upload(jpeg_upload(studio_photo(), name=name, fmt=fmt))
                self.assertEqual(Image.open(BytesIO(data)).size, (600, 900))

    def test_a_file_that_is_not_an_image_is_refused_with_a_plain_message(self):
        fake = BytesIO(b"this is definitely not a picture")
        with self.assertRaises(cover_images.CoverImageError) as raised:
            cover_images.cover_from_upload(fake)
        self.assertIn("valid image", str(raised.exception))

    def test_a_tiny_photo_is_refused(self):
        with self.assertRaises(cover_images.CoverImageError) as raised:
            cover_images.cover_from_upload(jpeg_upload(Image.new("RGB", (120, 180), (255, 255, 255))))
        self.assertIn("too small", str(raised.exception))

    def test_an_enormous_photo_is_refused_before_it_is_decoded(self):
        huge = Image.new("1", (7000, 7000))   # 49 megapixels, but only a few KB as a PNG
        with self.assertRaises(cover_images.CoverImageError) as raised:
            cover_images.cover_from_upload(jpeg_upload(huge, name="huge.png", fmt="PNG"))
        self.assertIn("too large", str(raised.exception))


class BundledPictureTests(SimpleTestCase):
    """The pictures that ship with the site (static/images/categories/)."""

    def test_the_twelve_categories_with_a_photo_each_have_a_tile_picture(self):
        self.assertEqual(len(BUNDLED_COVER_KEYS), 12)
        for key in sorted(BUNDLED_COVER_KEYS):
            with self.subTest(key=key):
                path = CATEGORY_PICTURES / f"{key}.jpg"
                self.assertTrue(path.exists(), f"missing {path}")
                with Image.open(path) as picture:
                    self.assertEqual(picture.size, (600, 900))
                    self.assertEqual(picture.convert("RGB").getpixel((2, 2)), GREY)
                    self.assertEqual(picture.convert("RGB").getpixel((597, 897)), GREY)

    def test_each_bundled_gown_clears_the_name_strip_and_the_sides(self):
        for key in sorted(BUNDLED_COVER_KEYS):
            with self.subTest(key=key):
                with Image.open(CATEGORY_PICTURES / f"{key}.jpg") as picture:
                    left, top, right, bottom = content_box(picture.convert("RGB"))
                self.assertGreaterEqual(left, 0.08)
                self.assertLessEqual(right, 0.92)
                self.assertGreaterEqual(top, 0.04)
                self.assertLessEqual(bottom, 0.86)

    def test_barong_has_no_photo_of_its_own_and_keeps_the_original_placeholder(self):
        self.assertNotIn("barong", BUNDLED_COVER_KEYS)
        self.assertFalse((CATEGORY_PICTURES / "barong.jpg").exists())
        self.assertTrue((CATEGORY_PICTURES.parent / PLACEHOLDER_STATIC_PATH.split("/", 1)[1]).exists())


class CoverChoiceTests(TestCase):
    """gowns.covers: which picture a category's tile gets."""

    def test_a_built_in_category_gets_its_bundled_picture(self):
        cover = cover_for("long-gown", {})
        self.assertEqual(cover, {"url": "/static/images/categories/long-gown.jpg", "is_photo": True, "owner": False})

    def test_barong_and_a_new_category_get_the_plain_placeholder(self):
        for key in ("barong", "debut-gown"):
            with self.subTest(key=key):
                cover = cover_for(key, {})
                self.assertEqual(cover["url"], "/static/images/categories/placeholder.jpg")
                self.assertFalse(cover["is_photo"])
                self.assertFalse(cover["owner"])

    def test_the_owners_picture_wins_over_the_bundled_one(self):
        url = "https://res.cloudinary.com/demo/image/upload/category_covers/mine"
        self.assertEqual(cover_for("long-gown", {"long-gown": url}), {"url": url, "is_photo": True, "owner": True})
        self.assertEqual(cover_for("barong", {"barong": url}), {"url": url, "is_photo": True, "owner": True})

    def test_the_owners_pictures_are_read_from_the_database(self):
        CategoryCover.objects.create(key="suit", image_url="https://example.test/suit.jpg")
        self.assertEqual(owner_cover_urls(), {"suit": "https://example.test/suit.jpg"})

    def test_an_unreadable_table_just_means_no_owner_pictures(self):
        broken = MagicMock()
        broken.objects.values_list.side_effect = DatabaseError("relation does not exist")
        with patch("gowns.covers.CategoryCover", broken):
            self.assertEqual(owner_cover_urls(), {})
        # ...and the surrounding transaction is still usable afterwards
        self.assertEqual(CategoryCover.objects.count(), 0)

    def test_one_picture_per_category(self):
        CategoryCover.objects.create(key="suit", image_url="https://example.test/a.jpg")
        with self.assertRaises(Exception):
            CategoryCover.objects.create(key="suit", image_url="https://example.test/b.jpg")


def tile_images(html):
    """{label: src} of every category tile picture on a page (tiles are the images the shared partial draws)."""
    return {alt: src for src, alt in re.findall(r'<img src="([^"]*)" alt="([^"]*)" loading="lazy" decoding="async" class="absolute inset-0 h-full w-full object-cover', html)}


class CategoryTilePagesTests(TestCase):
    """The Rentals page, the Women's and Men's pages and the home page all draw the same tiles."""

    def setUp(self):
        self.client = Client()

    def test_the_rentals_page_shows_every_category_with_its_picture(self):
        images = tile_images(self.client.get(reverse("gowns:collections")).content.decode())
        self.assertEqual(len(images), 13)
        for label, key in (("Wedding Gown", "wedding"), ("Evening Gown", "evening-gown"), ("Long Gown", "long-gown"),
                           ("Luxury Gown", "luxury-gown"), ("Mother Gown", "mother-gown"), ("Suit", "suit"),
                           ("Filipiniana", "filipiniana"), ("Guest Gown", "guest-gown"), ("Dresses", "dresses"),
                           ("Kids Gown", "kids-gown"), ("Ball Gown Tulle", "ball-gown-tulle"),
                           ("Bridesmaid Dresses", "bridesmaid-dresses")):
            with self.subTest(label=label):
                self.assertEqual(images[label], f"/static/images/categories/{key}.jpg")
        self.assertEqual(images["Barong"], "/static/images/categories/placeholder.jpg")

    def test_no_tile_still_points_at_the_old_hosted_stock_picture(self):
        for name in ("gowns:collections", "gowns:featured_women_collections", "gowns:featured_men_collections", "gowns:homepage"):
            with self.subTest(page=name):
                html = self.client.get(reverse(name)).content.decode()
                self.assertEqual([s for s in tile_images(html).values() if "googleusercontent" in s], [])

    def test_the_women_page_lists_the_women_categories_and_the_men_page_the_men_ones(self):
        women = tile_images(self.client.get(reverse("gowns:featured_women_collections")).content.decode())
        men = tile_images(self.client.get(reverse("gowns:featured_men_collections")).content.decode())
        self.assertEqual(len(women), 11)
        self.assertEqual(set(men), {"Suit", "Barong"})
        self.assertEqual(men["Suit"], "/static/images/categories/suit.jpg")
        self.assertEqual(men["Barong"], "/static/images/categories/placeholder.jpg")
        self.assertNotIn("Suit", women)

    def test_the_home_page_shows_its_four_categories_in_order_with_the_same_pictures(self):
        html = self.client.get(reverse("gowns:homepage")).content.decode()
        self.assertEqual(
            [alt for src, alt in re.findall(r'<img src="([^"]*categories/[^"]*)" alt="([^"]*)"', html)],
            ["Long Gown", "Filipiniana", "Evening Gown", "Suit"],
        )
        images = tile_images(html)
        self.assertEqual(images["Long Gown"], "/static/images/categories/long-gown.jpg")
        self.assertEqual(images["Suit"], "/static/images/categories/suit.jpg")

    def test_each_tile_links_to_its_collection_page_exactly_as_before(self):
        html = self.client.get(reverse("gowns:featured_men_collections")).content.decode()
        for url_name in ("collection_suit", "collection_barong"):
            self.assertIn(f'href="{reverse(f"gowns:{url_name}")}" class="group cursor-pointer flex flex-col items-center"', html)

    def test_a_real_picture_has_its_name_on_a_light_strip_and_the_placeholder_keeps_the_white_label(self):
        html = self.client.get(reverse("gowns:featured_men_collections")).content.decode()
        suit_tile = html.split('alt="Suit"')[1].split("</a>")[0]
        barong_tile = html.split('alt="Barong"')[1].split("</a>")[0]
        self.assertIn("bg-gradient-to-t", suit_tile)
        self.assertIn("text-secondary", suit_tile)
        self.assertNotIn("text-white", suit_tile)
        self.assertIn("text-white", barong_tile)
        self.assertNotIn("bg-gradient-to-t", barong_tile)

    def test_a_picture_the_owner_uploaded_shows_on_every_page_that_draws_the_tile(self):
        url = "https://res.cloudinary.com/demo/image/upload/category_covers/new-suit"
        CategoryCover.objects.create(key="suit", image_url=url)
        for name in ("gowns:collections", "gowns:featured_men_collections", "gowns:homepage"):
            with self.subTest(page=name):
                images = tile_images(self.client.get(reverse(name)).content.decode())
                self.assertEqual(images["Suit"], url)
        # every other category is untouched
        images = tile_images(self.client.get(reverse("gowns:collections")).content.decode())
        self.assertEqual(images["Long Gown"], "/static/images/categories/long-gown.jpg")

    def test_barong_shows_a_picture_the_owner_gives_it_in_the_new_style(self):
        CategoryCover.objects.create(key="barong", image_url="https://res.cloudinary.com/demo/image/upload/category_covers/barong")
        html = self.client.get(reverse("gowns:featured_men_collections")).content.decode()
        barong_tile = html.split('alt="Barong"')[1].split("</a>")[0]
        self.assertIn("bg-gradient-to-t", barong_tile)
        self.assertNotIn("text-white", barong_tile)

    def test_a_category_the_owner_added_gets_a_tile_and_then_its_own_picture(self):
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown")
        images = tile_images(self.client.get(reverse("gowns:collections")).content.decode())
        self.assertEqual(images["Debut Gown"], "/static/images/categories/placeholder.jpg")
        CategoryCover.objects.create(key="debut-gown", image_url="https://res.cloudinary.com/demo/image/upload/category_covers/debut")
        images = tile_images(self.client.get(reverse("gowns:collections")).content.decode())
        self.assertEqual(images["Debut Gown"], "https://res.cloudinary.com/demo/image/upload/category_covers/debut")

    def test_a_removed_category_loses_its_tile_on_every_page(self):
        HiddenCategory.objects.create(name="Suit")
        self.assertNotIn("Suit", tile_images(self.client.get(reverse("gowns:collections")).content.decode()))
        self.assertNotIn("Suit", tile_images(self.client.get(reverse("gowns:featured_men_collections")).content.decode()))
        home = self.client.get(reverse("gowns:homepage")).content.decode()
        self.assertNotIn("Suit", tile_images(home))
        grid = re.search(r'gap-x-4[^"]*hp-cards">(.*?)</section>', home, re.S).group(1)
        self.assertIn(reverse("gowns:collection_long_gown"), grid)   # the grid was found, and the others are still there
        self.assertNotIn(reverse("gowns:collection_suit"), grid)

    def test_the_pages_still_work_when_the_pictures_table_cannot_be_read(self):
        broken = MagicMock()
        broken.objects.values_list.side_effect = DatabaseError("relation does not exist")
        with patch("gowns.covers.CategoryCover", broken):
            for name in ("gowns:collections", "gowns:featured_women_collections", "gowns:featured_men_collections", "gowns:homepage"):
                with self.subTest(page=name):
                    response = self.client.get(reverse(name))
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(tile_images(response.content.decode()))
            images = tile_images(self.client.get(reverse("gowns:collections")).content.decode())
            self.assertEqual(images["Long Gown"], "/static/images/categories/long-gown.jpg")

    def test_the_search_overlay_data_is_still_plain_json_without_pictures(self):
        html = self.client.get(reverse("gowns:homepage")).content.decode()
        blob = re.search(r'<script id="search-collections-data" type="application/json">(.*?)</script>', html, re.S).group(1)
        rows = json.loads(blob)
        self.assertTrue(rows)
        self.assertTrue(all("cover" not in row for row in rows))

    def test_pages_that_draw_no_tiles_never_read_the_pictures_table(self):
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get(reverse("gowns:faqs")).status_code, 200)
        self.assertEqual([q["sql"] for q in queries.captured_queries if "gowns_categorycover" in q["sql"]], [])

    def test_a_page_with_tiles_reads_the_pictures_table_only_once(self):
        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse("gowns:featured_women_collections"))
        self.assertEqual(len([q for q in queries.captured_queries if "gowns_categorycover" in q["sql"]]), 1)
