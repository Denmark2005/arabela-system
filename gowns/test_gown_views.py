"""Front, side and back photos: matching a view to its front (gowns/view_fit.py), the design sharing them, the product page's Front / Side /
Back pictures, and the add_gown_views command."""
import tempfile
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from PIL import Image, ImageDraw

from gowns import design_details, photos, view_fit
from gowns.management.commands.add_gown_views import SUITS
from gowns.models import Gown

IN_MEMORY = {**settings.STORAGES, "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"}}
COAT = "Coat Sequence Black, Green & Gray 1"


def picture(size, box, *, colour=(30, 30, 40), background=(255, 255, 255), line=False, hook=False, trim=None, fmt="JPEG"):
    """A studio photo: a jacket-like shape in `box` (left, top, right, bottom) on a plain background."""
    image = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = box
    inset = (right - left) // 6
    shoulders = (bottom - top) // 6
    draw.polygon([(left + inset, top), (right - inset, top), (right, top + shoulders), (right - inset // 2, bottom), (left + inset // 2, bottom),
                  (left, top + shoulders)], fill=colour, outline=trim or colour)
    if trim:
        draw.line([((left + right) // 2, top), ((left + right) // 2, bottom)], fill=trim, width=4)
    if hook:                                        # a hanger hook just above the shoulders (its own small shape)
        draw.arc([(left + right) // 2 - 12, top - 40, (left + right) // 2 + 12, top - 16], 150, 30, fill=(60, 50, 40), width=5)
    if line:                                        # a faint stray line along the bottom edge
        draw.line([(0, size[1] - 2), (size[0], size[1] - 2)], fill=(215, 215, 215), width=2)
    out = BytesIO()
    image.save(out, fmt, quality=95) if fmt == "JPEG" else image.save(out, fmt)
    return out.getvalue()


FRONT = picture((600, 1000), (120, 200, 480, 780))


class FitToFrontTests(SimpleTestCase):
    def fitted(self, view, front=FRONT):
        return Image.open(BytesIO(view_fit.fit_to_front(view, front))).convert("RGB")

    def box(self, image):
        return view_fit.garment(image)[1]

    def height(self, image):
        box = self.box(image)
        return box[3] - box[1]

    def front_height(self, front=FRONT):
        """The front garment's height as the module measures it (the drawn 580, plus the hair of margin it adds around every outline)."""
        height = self.height(view_fit.open_photo(front))
        self.assertAlmostEqual(height, 580, delta=12)
        return height

    def test_the_view_gets_the_fronts_size_height_top_and_centre_on_white(self):
        view = picture((400, 700), (150, 40, 260, 690), background=(250, 250, 250), line=True)
        out = self.fitted(view)
        self.assertEqual(out.size, (600, 1000))
        left, top, right, bottom = self.box(out)
        front = self.box(view_fit.open_photo(FRONT))
        self.assertAlmostEqual(bottom - top, self.front_height(), delta=6)           # within 1% (the real suit photos: within 0.2%)
        self.assertAlmostEqual(top, front[1], delta=4)
        self.assertAlmostEqual((left + right) / 2, (front[0] + front[2]) / 2, delta=3)
        for corner in ((2, 2), (597, 2), (2, 997), (597, 997), (300, 996)):
            self.assertEqual(out.getpixel(corner), (255, 255, 255), corner)

    def test_the_view_keeps_its_own_shape(self):
        out = self.fitted(picture((600, 1000), (250, 100, 350, 900)))          # a narrow side view
        left, top, right, bottom = self.box(out)
        self.assertAlmostEqual((right - left) / (bottom - top), 100 / 800, delta=0.02)

    def test_a_white_garment_is_not_washed_out(self):
        front = picture((600, 1000), (120, 200, 480, 780), colour=(232, 228, 222), trim=(40, 40, 40))
        out = self.fitted(picture((600, 1000), (200, 100, 400, 900), colour=(232, 228, 222), trim=(40, 40, 40)), front)
        centre = out.getpixel((280, 500))
        self.assertLess(max(centre), 245, centre)

    def test_a_hanger_above_the_shoulders_stays(self):
        front = picture((600, 1000), (120, 200, 480, 780), hook=True)
        out = self.fitted(picture((600, 1000), (200, 200, 400, 780), hook=True), front)
        self.assertAlmostEqual(self.box(out)[1], self.box(view_fit.open_photo(front))[1], delta=4)
        self.assertLess(self.box(out)[1], 190)                                  # the hook is above the shoulders

    def test_a_pale_part_of_the_garment_on_a_clean_background_is_kept(self):
        image = Image.new("RGB", (600, 1000), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle((200, 200, 400, 780), fill=(120, 120, 120))
        draw.rectangle((401, 200, 460, 420), fill=(243, 242, 240))      # a white suit's shoulder: barely off white, on the outline
        data = BytesIO()
        image.save(data, "JPEG", quality=95)
        out = self.fitted(data.getvalue())
        left, top, right, bottom = self.box(out)
        self.assertAlmostEqual(right - left, 260, delta=12)
        self.assertLess(max(out.getpixel((right - 20, top + 60))), 250)

    def test_a_see_through_png_becomes_white(self):
        image = Image.new("RGBA", (600, 1000), (0, 0, 0, 0))
        ImageDraw.Draw(image).rectangle((200, 150, 400, 850), fill=(20, 20, 20, 255))
        data = BytesIO()
        image.save(data, "PNG")
        out = self.fitted(data.getvalue())
        self.assertEqual(out.getpixel((2, 2)), (255, 255, 255))
        self.assertAlmostEqual(self.height(out), self.front_height(), delta=6)

    def test_a_photo_that_is_not_on_a_plain_background_is_refused_with_the_reason(self):
        with self.assertRaisesMessage(view_fit.CannotFit, "it isn't on a plain white background"):
            view_fit.fit_to_front(picture((600, 1000), (200, 100, 400, 900), background=(90, 120, 200)), FRONT)
        with self.assertRaisesMessage(view_fit.CannotFit, "the front photo isn't on a plain white background"):
            view_fit.fit_to_front(FRONT, picture((600, 1000), (200, 100, 400, 900), background=(90, 120, 200)))

    def test_an_unreadable_or_empty_picture_is_refused(self):
        with self.assertRaisesMessage(view_fit.CannotFit, "the picture couldn't be opened"):
            view_fit.fit_to_front(b"not a picture", FRONT)
        blank = BytesIO()
        Image.new("RGB", (600, 1000), (255, 255, 255)).save(blank, "JPEG")
        with self.assertRaisesMessage(view_fit.CannotFit, "no garment was found in it"):
            view_fit.fit_to_front(blank.getvalue(), FRONT)

    def test_a_huge_photo_is_brought_down_to_size(self):
        self.assertEqual(max(view_fit.open_photo(picture((3000, 4800), (900, 900, 2100, 3900))).size), view_fit.MAX_SIDE)
        self.assertEqual(view_fit.open_photo(picture((5200, 8000), (900, 900, 4100, 7000))).size, (1560, 2400))     # read reduced, then fitted

    def test_an_enormous_picture_is_not_opened(self):
        data = BytesIO()
        Image.new("L", (9000, 7000), 255).save(data, "PNG")                              # 63 million pixels in a small file
        with self.assertRaisesMessage(view_fit.CannotFit, "the picture is too large to work with"):
            view_fit.fit_to_front(data.getvalue(), FRONT)


def gown(name=COAT, *, size=Gown.Size.MEDIUM, number=1, category=Gown.Category.SUIT, front="", side="", back=""):
    return Gown.objects.create(gown_id=f"GV-{category[:4]}-{number:03d}", name=name, category=category, color_name="Black", color_code="BK",
                               size=size, rental_price=Decimal("1000"), status=Gown.Status.AVAILABLE,
                               photo_url=front, side_photo_url=side, back_photo_url=back)


class DesignPhotoTests(TestCase):
    def test_a_design_uses_the_first_photo_any_size_has_and_shares_changes_with_all(self):
        first = gown(number=1, side="https://p.test/side-old.jpg")
        second = gown(number=2, size=Gown.Size.LARGE, front="https://p.test/front.jpg", side="https://p.test/side-new.jpg")
        other = gown("Regular Collar Black 1", number=3)
        self.assertEqual(design_details.photos_of([second, first]),
                         {"photo_url": "https://p.test/front.jpg", "side_photo_url": "https://p.test/side-old.jpg", "back_photo_url": ""})
        self.assertEqual(design_details.share_photos(Gown.Category.SUIT, COAT, {"back_photo_url": "https://p.test/back.jpg"}), 2)
        self.assertEqual(set(Gown.objects.filter(name=COAT).values_list("back_photo_url", flat=True)), {"https://p.test/back.jpg"})
        other.refresh_from_db()
        self.assertEqual(other.back_photo_url, "")

    def test_reading_a_stored_photo(self):
        with override_settings(STORAGES=IN_MEMORY):
            name = default_storage.save("gown_photos/a b.jpg", ContentFile(b"photo bytes"))
            self.assertEqual(photos.read(default_storage.url(name)), b"photo bytes")
            with self.assertLogs("gowns.photos", level="WARNING"):
                self.assertIsNone(photos.read("/media/gown_photos/missing.jpg"))
        self.assertIsNone(photos.read(""))
        self.assertIsNone(photos.read("ftp://p.test/x.jpg"))


CLOUD = "https://res.cloudinary.com/demo/image/upload/v1/gown_photos/%s.jpg"


class ProductPageViewTests(TestCase):
    def page(self, unit):
        response = self.client.get(f"/collections/suit/products/{unit.slug}/")
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_front_side_and_back_with_their_small_pictures(self):
        unit = gown(front=CLOUD % "front", side=CLOUD % "side", back=CLOUD % "back")
        html = self.page(unit)
        self.assertIn('id="product-stage"', html)
        for key, label in (("front", "Front"), ("side", "Side"), ("back", "Back")):
            self.assertIn(f'data-view-button="{key}"', html)
            self.assertIn(f'aria-label="{label} view"', html)
            self.assertIn(f"f_auto,q_auto,c_limit,w_240/v1/gown_photos/{key}.jpg", html)          # the small picture
            self.assertIn(f"f_auto,q_auto,c_limit,w_1600/v1/gown_photos/{key}.jpg", html)         # the big one
        self.assertEqual(html.count('class="w-full h-full object-cover pv-view'), 3)
        self.assertEqual(html.count('aria-pressed="true"'), 1)
        self.assertIn("document.getElementById('product-stage')", html)

    def test_the_views_come_from_any_size_of_the_design(self):
        shown = gown(number=1, front=CLOUD % "front")                                   # the page's representative has no side / back itself
        gown(number=2, size=Gown.Size.LARGE, front=CLOUD % "front", side=CLOUD % "side")
        html = self.page(shown)
        self.assertEqual(html.count("data-view-button="), 2)
        self.assertNotIn('data-view-button="back"', html)

    def test_a_gown_with_only_a_front_photo_looks_as_before(self):
        html = self.page(gown(front=CLOUD % "front"))
        self.assertNotIn("product-stage", html)
        self.assertNotIn("data-view-button", html)
        self.assertNotIn("pv-thumbs\"", html)
        self.assertIn('<img id="product-main-image"', html)

    def test_side_without_a_front_shows_no_switcher(self):
        html = self.page(gown(side=CLOUD % "side"))
        self.assertNotIn("data-view-button", html)


@override_settings(STORAGES=IN_MEMORY)
class AddGownViewsCommandTests(TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        front = default_storage.save("gown_photos/coat.jpg", ContentFile(FRONT))
        self.units = [gown(number=1, front=default_storage.url(front)), gown(number=2, size=Gown.Size.LARGE, front=default_storage.url(front))]
        number = SUITS.index(COAT) + 1
        self.suit = Path(self.folder.name) / f"Suit_{number:02d}"
        self.suit.mkdir()
        self.write(f"Suit_{number:02d}_Side.png", picture((400, 700), (160, 40, 240, 690), fmt="PNG"))
        self.write(f"Suit_{number:02d}_Back.png", picture((400, 700), (40, 40, 360, 690), fmt="PNG"))
        self.number = number

    def write(self, name, data):
        (self.suit / name).write_bytes(data)

    def run_command(self, *args):
        out = StringIO()
        call_command("add_gown_views", f"--folder={self.folder.name}", f"--design={COAT}", *args, stdout=out)
        return out.getvalue()

    def urls(self):
        return [(u.side_photo_url, u.back_photo_url) for u in Gown.objects.filter(name=COAT).order_by("id")]

    def stored(self, url):
        return view_fit.open_photo(photos.read(url))

    def test_the_views_are_matched_uploaded_and_set_on_every_size(self):
        out = self.run_command()
        self.assertIn("side and back uploaded to 2 gown(s)", out)
        first, second = self.urls()
        self.assertEqual(first, second)
        self.assertTrue(all(first))
        for url in first:
            image = self.stored(url)
            self.assertEqual(image.size, (600, 1000))
            box, front = view_fit.garment(image)[1], view_fit.garment(view_fit.open_photo(FRONT))[1]
            self.assertAlmostEqual(box[3] - box[1], front[3] - front[1], delta=6)
            self.assertAlmostEqual(box[1], front[1], delta=4)
        self.assertEqual({u.photo_url for u in Gown.objects.filter(name=COAT)}, {self.units[0].photo_url})   # the front is never changed

    def test_a_dry_run_saves_nothing_but_can_write_previews(self):
        preview = Path(self.folder.name) / "preview"
        out = self.run_command("--dry-run", f"--preview={preview}")
        self.assertIn("would upload the side and back to 2 gown(s)", out)
        self.assertIn("Nothing was saved (dry run).", out)
        self.assertEqual(self.urls(), [("", ""), ("", "")])
        self.assertEqual(sorted(p.name for p in preview.iterdir()), [f"Suit_{self.number:02d}_Back.jpg", f"Suit_{self.number:02d}_Side.jpg"])
        self.assertEqual(default_storage.listdir("gown_photos")[1], ["coat.jpg"])

    def test_running_again_skips_and_replace_uploads_anew(self):
        self.run_command()
        before = self.urls()
        self.assertIn("already has its side and back, skipped", self.run_command())
        self.assertEqual(self.urls(), before)
        self.run_command("--replace")
        self.assertNotEqual(self.urls(), before)

    def test_files_labelled_the_wrong_way_round_are_swapped(self):
        wide, narrow = picture((400, 700), (40, 40, 360, 690), fmt="PNG"), picture((400, 700), (160, 40, 240, 690), fmt="PNG")
        self.write(f"Suit_{self.number:02d}_Side.png", wide)
        self.write(f"Suit_{self.number:02d}_Back.png", narrow)
        self.assertIn("wrong way round", self.run_command())
        side, back = self.urls()[0]
        side_box, back_box = view_fit.garment(self.stored(side))[1], view_fit.garment(self.stored(back))[1]
        self.assertLess(side_box[2] - side_box[0], back_box[2] - back_box[0])

    def test_a_missing_picture_is_reported_and_the_other_one_still_goes(self):
        (self.suit / f"Suit_{self.number:02d}_Back.png").unlink()
        out = self.run_command()
        self.assertIn(f"no Back picture in Suit_{self.number:02d}", out)
        side, back = self.urls()[0]
        self.assertTrue(side)
        self.assertEqual(back, "")

    def test_the_live_database_with_pictures_kept_on_this_computer_is_refused(self):
        local_disk = {**settings.STORAGES, "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}}
        with patch.dict(settings.DATABASES["default"], {"ENGINE": "django.db.backends.postgresql"}), override_settings(STORAGES=local_disk):
            with self.assertRaisesMessage(CommandError, "no CLOUDINARY_ settings"):
                call_command("add_gown_views", f"--folder={self.folder.name}", f"--design={COAT}", stdout=StringIO())
        self.assertEqual(self.urls(), [("", ""), ("", "")])

    def test_an_unknown_name_stops_with_the_list(self):
        with self.assertRaisesMessage(CommandError, "Not one of the 23 suits: Nothing 1"):
            call_command("add_gown_views", f"--folder={self.folder.name}", "--design=Nothing 1", stdout=StringIO())
        with self.assertRaisesMessage(CommandError, "There is no folder"):
            call_command("add_gown_views", "--folder=Z:/nowhere", stdout=StringIO())

    def test_an_unreadable_front_skips_the_design(self):
        Gown.objects.filter(name=COAT).update(photo_url="/media/gown_photos/gone.jpg")
        with self.assertLogs("gowns.photos", level="WARNING"):
            self.assertIn("its front photo couldn't be read just now, skipped", self.run_command())
        self.assertEqual(self.urls(), [("", ""), ("", "")])
