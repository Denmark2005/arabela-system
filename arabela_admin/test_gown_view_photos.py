"""Front, side and back photos in the admin: Add / Edit Gown's three slots, the photo window, the matching to the front, every size of a design
sharing the pictures, and the Audit Log lines. The pictures are drawn here and stored in memory; the clicks are in the browser check."""
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from accounts.models import AuditLog
from gowns import photos, view_fit
from gowns.models import Gown
from gowns.test_gown_views import COAT, FRONT, IN_MEMORY, picture

User = get_user_model()
OTHER = "Regular Collar Black 1"
SIDE = picture((400, 700), (150, 40, 260, 690), background=(250, 250, 250), fmt="PNG")      # a narrow side view on off-white
BACK = picture((500, 900), (90, 60, 410, 880), fmt="PNG")                                  # a back view of another size
SMALL_FRONT = picture((600, 1000), (180, 300, 420, 700))                                   # a new front with a much shorter jacket
BUSY = picture((400, 700), (150, 40, 260, 690), background=(80, 120, 190), fmt="PNG")


def upload(name, data=SIDE, content_type="image/png"):
    return SimpleUploadedFile(name, data, content_type=content_type)


def garment_box(url_or_bytes):
    data = url_or_bytes if isinstance(url_or_bytes, bytes) else photos.read(url_or_bytes)
    return view_fit.garment(view_fit.open_photo(data))[1]


@override_settings(STORAGES=IN_MEMORY)
class ViewPhotoBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="views_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def stored(self, data=FRONT, name="front.jpg"):
        return default_storage.url(default_storage.save(f"gown_photos/{name}", ContentFile(data)))

    def gown(self, number=1, name=COAT, **extra):
        fields = dict(gown_id=f"Suit-BK-{number:03d}", name=name, category=Gown.Category.SUIT, color_name="Black", color_code="BK",
                      size=Gown.Size.MEDIUM, rental_price="1000", status=Gown.Status.AVAILABLE)
        fields.update(extra)
        return Gown.objects.create(**fields)

    def fields(self, **extra):
        data = dict(name=COAT, category=Gown.Category.SUIT, color_name="Black", color_code="BK", size=Gown.Size.MEDIUM, rental_price="1000")
        data.update(extra)
        return data

    def create(self, **extra):
        return self.client.post(reverse("arabela_admin:gown_create"), data=self.fields(**extra))

    def update(self, gown, **extra):
        return self.client.post(reverse("arabela_admin:gown_update", args=[gown.id]), data=self.fields(**{"name": gown.name, "size": gown.size, **extra}))

    def photo(self, gown, **data):
        return self.client.post(reverse("arabela_admin:gown_photo_update", args=[gown.id]), data=data)

    def assertMatched(self, url, front=FRONT):
        """The picture at url has the front's picture size, and its garment the front garment's height, top and centre."""
        data = photos.read(url)
        self.assertEqual(view_fit.open_photo(data).size, view_fit.open_photo(front).size)
        box, wanted = garment_box(data), garment_box(front)
        self.assertAlmostEqual(box[3] - box[1], wanted[3] - wanted[1], delta=6)      # within 1% (the real suit photos: within 0.2%)
        self.assertAlmostEqual(box[1], wanted[1], delta=4)
        self.assertAlmostEqual((box[0] + box[2]) / 2, (wanted[0] + wanted[2]) / 2, delta=4)


class CreateWithViewsTests(ViewPhotoBase):
    def test_three_photos_are_stored_and_the_side_and_back_are_matched_to_the_front(self):
        response = self.create(photo=upload("f.jpg", FRONT, "image/jpeg"), side_photo=upload("s.png"), back_photo=upload("b.png", BACK))
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["notes"], ["Side photo matched to the front.", "Back photo matched to the front."])
        gown = Gown.objects.get(id=body["gown"]["id"])
        self.assertEqual(photos.read(gown.photo_url), FRONT)                              # the front is stored exactly as uploaded
        self.assertEqual((body["gown"]["side_photo_url"], body["gown"]["back_photo_url"]), (gown.side_photo_url, gown.back_photo_url))
        self.assertTrue(gown.side_photo_url.endswith(".jpg") and gown.back_photo_url.endswith(".jpg"))
        self.assertMatched(gown.side_photo_url)
        self.assertMatched(gown.back_photo_url)

    def test_a_side_without_any_front_is_kept_as_it_is_and_says_so(self):
        response = self.create(side_photo=upload("s.png"))
        self.assertEqual(response.json()["notes"], ["Side photo saved as it is: there is no front photo to match it to."])
        self.assertEqual(photos.read(Gown.objects.get().side_photo_url), SIDE)

    def test_a_busy_background_is_kept_as_it_is_with_the_reason(self):
        response = self.create(photo=upload("f.jpg", FRONT, "image/jpeg"), back_photo=upload("b.png", BUSY))
        self.assertEqual(response.json()["notes"], ["Back photo saved as it is: it isn't on a plain white background."])
        self.assertEqual(photos.read(Gown.objects.get().back_photo_url), BUSY)

    def test_another_size_takes_the_designs_photos_and_a_new_back_goes_to_every_size(self):
        first = self.gown(photo_url=self.stored(), side_photo_url="https://p.test/side.jpg")
        response = self.create(name_choice="same", size=Gown.Size.LARGE, back_photo=upload("b.png", BACK))
        self.assertEqual(response.status_code, 200, response.content)
        second = Gown.objects.get(id=response.json()["gown"]["id"])
        first.refresh_from_db()
        self.assertEqual((second.photo_url, second.side_photo_url), (first.photo_url, "https://p.test/side.jpg"))
        self.assertEqual(second.back_photo_url, first.back_photo_url)
        self.assertMatched(second.back_photo_url)                                       # matched to the design's own front

    def test_a_new_front_on_a_new_size_redraws_the_designs_side(self):
        first = self.gown(photo_url=self.stored())
        self.photo(first, view="side", photo=upload("s.png"))
        first.refresh_from_db()
        old_side = first.side_photo_url
        response = self.create(name_choice="same", size=Gown.Size.LARGE, photo=upload("n.jpg", SMALL_FRONT, "image/jpeg"))
        self.assertEqual(response.json()["notes"], ["Side photo matched to the new front."])
        first.refresh_from_db()
        self.assertNotEqual(first.side_photo_url, old_side)
        self.assertMatched(first.side_photo_url, SMALL_FRONT)

    def test_each_slot_is_checked_and_nothing_is_saved(self):
        cases = ((dict(side_photo=upload("s.gif", content_type="image/gif")), "Side photo must be a JPG, PNG, or WEBP image."),
                 (dict(back_photo=upload("b.png", b"x" * (5 * 1024 * 1024 + 1))), "Back photo must be 5MB or smaller."),
                 (dict(photo=upload("f.bmp", content_type="image/bmp")), "Gown photo must be a JPG, PNG, or WEBP image."))
        for files, message in cases:
            with self.subTest(message=message):
                response = self.create(**files)
                self.assertEqual((response.status_code, response.json()["error"]), (400, message))
        self.assertFalse(Gown.objects.exists())

    def test_a_storage_failure_names_the_photo_and_creates_nothing(self):
        with patch("arabela_admin.views._save_gown_photo", side_effect=OSError("no space")):
            response = self.create(side_photo=upload("s.png"))
        self.assertEqual((response.status_code, response.json()["error"]), (502, "The side photo couldn't be uploaded just now. Please try again."))
        self.assertFalse(Gown.objects.exists())

    def test_a_very_long_file_name_still_fits_the_address_column(self):
        response = self.create(side_photo=upload("s" * 240 + ".png"))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertLessEqual(len(Gown.objects.get().side_photo_url), 500)


class EditViewsTests(ViewPhotoBase):
    def setUp(self):
        super().setUp()
        front = self.stored()
        self.medium = self.gown(1, photo_url=front)
        self.large = self.gown(2, size=Gown.Size.LARGE, photo_url=front)
        self.other = self.gown(3, name=OTHER, photo_url=self.stored(name="other.jpg"), side_photo_url="https://p.test/other-side.jpg")

    def reload(self):
        for gown in (self.medium, self.large, self.other):
            gown.refresh_from_db()

    def test_adding_a_side_and_back_matches_them_and_shares_them_with_every_size(self):
        response = self.update(self.medium, side_photo=upload("s.png"), back_photo=upload("b.png", BACK))
        self.assertEqual(response.status_code, 200, response.content)
        self.reload()
        self.assertEqual((self.medium.side_photo_url, self.medium.back_photo_url), (self.large.side_photo_url, self.large.back_photo_url))
        self.assertMatched(self.large.side_photo_url)
        self.assertMatched(self.large.back_photo_url)
        self.assertEqual(self.other.side_photo_url, "https://p.test/other-side.jpg")
        self.assertEqual(response.json()["design_units"], 2)

    def test_removing_a_side_takes_it_from_every_size_and_an_upload_wins_over_a_removal(self):
        self.update(self.medium, side_photo=upload("s.png"), back_photo=upload("b.png", BACK))
        self.update(self.large, remove_side="1")
        self.reload()
        self.assertEqual((self.medium.side_photo_url, self.large.side_photo_url), ("", ""))
        self.assertTrue(self.medium.back_photo_url)
        old_back = self.medium.back_photo_url
        self.update(self.medium, remove_back="1", back_photo=upload("b2.png", BACK))
        self.reload()
        self.assertTrue(self.large.back_photo_url)
        self.assertNotEqual(self.large.back_photo_url, old_back)

    def test_an_edit_without_photos_leaves_every_picture_alone(self):
        self.update(self.medium, side_photo=upload("s.png"))
        self.reload()
        before = (self.medium.photo_url, self.medium.side_photo_url, self.medium.back_photo_url)
        response = self.update(self.large, notes="Checked the lining")
        self.assertEqual((response.status_code, response.json()["notes"]), (200, []))
        self.reload()
        self.assertEqual((self.large.photo_url, self.large.side_photo_url, self.large.back_photo_url), before)

    def test_a_new_front_redraws_the_side_and_back_to_it(self):
        self.update(self.medium, side_photo=upload("s.png"), back_photo=upload("b.png", BACK))
        response = self.update(self.medium, photo=upload("n.jpg", SMALL_FRONT, "image/jpeg"))
        self.assertEqual(response.json()["notes"], ["Side photo matched to the new front.", "Back photo matched to the new front."])
        self.reload()
        self.assertEqual(self.medium.photo_url, self.large.photo_url)
        self.assertEqual(photos.read(self.large.photo_url), SMALL_FRONT)
        self.assertMatched(self.large.side_photo_url, SMALL_FRONT)
        self.assertMatched(self.large.back_photo_url, SMALL_FRONT)

    def test_a_front_that_cannot_be_matched_to_keeps_the_old_side_and_says_why(self):
        self.update(self.medium, side_photo=upload("s.png"))
        self.reload()
        old_side = self.medium.side_photo_url
        response = self.update(self.medium, photo=upload("room.png", BUSY))
        self.assertEqual(response.json()["notes"], ["Side photo kept as it is: the front photo isn't on a plain white background."])
        self.reload()
        self.assertEqual(self.medium.side_photo_url, old_side)

    def test_moving_a_gown_into_another_design_takes_that_designs_pictures_only(self):
        self.update(self.medium, side_photo=upload("s.png"), back_photo=upload("b.png", BACK))
        self.reload()
        response = self.update(self.medium, name=OTHER)
        self.assertEqual(response.status_code, 200, response.content)
        self.reload()
        self.assertEqual((self.medium.photo_url, self.medium.side_photo_url, self.medium.back_photo_url),
                         (self.other.photo_url, "https://p.test/other-side.jpg", ""))          # the old back would be another dress's
        self.assertTrue(self.large.back_photo_url)                                             # its old design keeps its own

    def test_a_remove_pressed_while_moving_never_touches_the_new_designs_pictures(self):
        self.update(self.medium, side_photo=upload("s.png"))
        response = self.update(self.medium, name=OTHER, remove_side="1")
        self.assertEqual(response.status_code, 200, response.content)
        self.reload()
        self.assertEqual((self.medium.side_photo_url, self.other.side_photo_url), ("https://p.test/other-side.jpg",) * 2)

    def test_a_photo_added_while_moving_is_matched_to_the_new_designs_front(self):
        Gown.objects.filter(pk=self.other.pk).update(photo_url=self.stored(SMALL_FRONT, "small.jpg"))
        response = self.update(self.medium, name=OTHER, back_photo=upload("b.png", BACK))
        self.assertEqual(response.json()["notes"], ["Back photo matched to the front."])
        self.reload()
        self.assertEqual(self.medium.back_photo_url, self.other.back_photo_url)
        self.assertMatched(self.other.back_photo_url, SMALL_FRONT)

    def test_renaming_to_a_new_name_keeps_the_pictures(self):
        self.update(self.medium, side_photo=upload("s.png"))
        self.reload()
        before = (self.medium.photo_url, self.medium.side_photo_url)
        self.update(self.medium, name="A Brand New Name")
        self.medium.refresh_from_db()
        self.assertEqual((self.medium.photo_url, self.medium.side_photo_url), before)

    def test_a_bad_file_saves_nothing(self):
        response = self.update(self.medium, side_photo=upload("s.txt", content_type="text/plain"), notes="Should not save")
        self.assertEqual((response.status_code, response.json()["error"]), (400, "Side photo must be a JPG, PNG, or WEBP image."))
        self.reload()
        self.assertEqual((self.medium.notes, self.medium.side_photo_url), ("", ""))


class PhotoWindowTests(ViewPhotoBase):
    def setUp(self):
        super().setUp()
        front = self.stored()
        self.medium = self.gown(1, photo_url=front)
        self.large = self.gown(2, size=Gown.Size.LARGE, photo_url=front)

    def test_a_side_is_matched_and_given_to_every_size(self):
        response = self.photo(self.medium, view="side", photo=upload("s.png"))
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body["view"], body["removed"], body["design_units"], body["notes"]),
                         ("side", False, 2, ["Side photo matched to the front."]))
        self.large.refresh_from_db()
        self.assertEqual(self.large.side_photo_url, body["side_photo_url"])
        self.assertMatched(self.large.side_photo_url)

    def test_without_a_view_it_is_the_front_as_before(self):
        response = self.photo(self.large, photo=upload("n.jpg", SMALL_FRONT, "image/jpeg"))
        self.assertEqual(response.status_code, 200, response.content)
        self.medium.refresh_from_db()
        self.assertEqual(photos.read(self.medium.photo_url), SMALL_FRONT)
        self.assertEqual(response.json()["photo_url"], self.medium.photo_url)

    def test_replacing_the_front_redraws_the_side_and_back(self):
        self.photo(self.medium, view="side", photo=upload("s.png"))
        self.photo(self.medium, view="back", photo=upload("b.png", BACK))
        response = self.photo(self.medium, view="front", photo=upload("n.jpg", SMALL_FRONT, "image/jpeg"))
        body = response.json()
        self.assertEqual(body["notes"], ["Side photo matched to the new front.", "Back photo matched to the new front."])
        self.large.refresh_from_db()
        self.assertEqual((self.large.photo_url, self.large.side_photo_url, self.large.back_photo_url),
                         (body["photo_url"], body["side_photo_url"], body["back_photo_url"]))
        self.assertMatched(self.large.side_photo_url, SMALL_FRONT)
        self.assertMatched(self.large.back_photo_url, SMALL_FRONT)

    def test_removing_the_back(self):
        self.photo(self.medium, view="back", photo=upload("b.png", BACK))
        response = self.photo(self.large, view="back", remove="1")
        self.assertEqual((response.status_code, response.json()["removed"], response.json()["back_photo_url"]), (200, True, ""))
        self.assertEqual(set(Gown.objects.values_list("back_photo_url", flat=True)), {""})

    def test_what_is_refused(self):
        for data, message in ((dict(view="front", remove="1"), "The front photo can be replaced, not removed."),
                              (dict(view="top", photo=upload("t.png")), "Choose the Front, Side or Back photo."),
                              (dict(view="side"), "Please choose a photo to upload."),
                              (dict(view="back", photo=upload("b.gif", content_type="image/gif")), "Back photo must be a JPG, PNG, or WEBP image.")):
            with self.subTest(message=message):
                response = self.photo(self.medium, **data)
                self.assertEqual((response.status_code, response.json()["error"]), (400, message))
        self.assertEqual(set(Gown.objects.values_list("side_photo_url", "back_photo_url")), {("", "")})

    def test_a_signed_out_person_changes_nothing(self):
        self.client.logout()
        self.assertEqual(self.photo(self.medium, view="side", photo=upload("s.png")).status_code, 401)
        self.assertEqual(Gown.objects.filter(side_photo_url="").count(), 2)


class AuditViewPhotoTests(ViewPhotoBase):
    def setUp(self):
        super().setUp()
        front = self.stored()
        self.medium = self.gown(1, photo_url=front)
        self.gown(2, size=Gown.Size.LARGE, photo_url=front)

    def last(self):
        return AuditLog.objects.order_by("-id").first()

    def test_the_photo_window_names_the_view_and_the_sizes(self):
        self.photo(self.medium, view="side", photo=upload("s.png"))
        self.assertIn("changed the side photo of gown Suit-BK-001", self.last().summary)
        self.assertIn("(all 2 gowns with this name)", self.last().summary)
        self.photo(self.medium, view="side", remove="1")
        self.assertIn("removed the side photo of gown Suit-BK-001", self.last().summary)
        self.photo(self.medium, view="front", photo=upload("n.jpg", SMALL_FRONT, "image/jpeg"))
        self.assertIn("changed the photo of gown Suit-BK-001", self.last().summary)

    def test_an_edit_lists_the_pictures_that_changed(self):
        self.update(self.medium, side_photo=upload("s.png"))
        row = self.last()
        self.assertEqual(row.details["side photo"], ["none", "added"])
        self.assertNotIn("back photo", row.details)
        self.assertIn("(photos are shared by all 2 gowns with this name)", row.summary)


class CatalogPageViewSlotsTests(ViewPhotoBase):
    def test_the_list_window_and_add_box_carry_the_three_pictures(self):
        self.gown(photo_url="https://p.test/f.jpg", side_photo_url="https://p.test/s.jpg", back_photo_url="https://p.test/b.jpg")
        html = self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()
        for attribute in ('data-photo="https://p.test/f.jpg"', 'data-side="https://p.test/s.jpg"', 'data-back="https://p.test/b.jpg"'):
            self.assertIn(attribute, html)
        for view, name in (("front", "Front"), ("side", "Side"), ("back", "Back")):
            self.assertIn(f'x-ref="photo{name}"', html)
            self.assertIn(f"pickGownPhoto('{view}', $event.dataTransfer.files[0])", html)
            self.assertIn(f"showPhotoView('{view}')", html)
        self.assertIn('class="gc-add-grid"', html)
        self.assertNotIn("To change the photo, click", html)
