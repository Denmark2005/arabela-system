"""The camera ("Take photo") button is for phones and tablets only.

A computer's browser ignores the camera hint (`capture="environment"`) and just opens the file chooser, so on a PC the button was a second file
button that promised a camera. Every camera button is now marked `touch-only` (one shared CSS rule hides it when the device's main pointer is a mouse
or trackpad; a laptop with a touch screen counts as a computer), and the file button says "Choose photo" on a computer (`pc-only`) and keeps
"Choose from gallery" on a phone or tablet. This pins that so a new camera button cannot be added without the mark, and checks the shared rule,
the labels and the keyboard-focus fix. How photos are picked, shrunk and uploaded keeps its own tests."""
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

User = get_user_model()
ROOT = Path(settings.BASE_DIR) / "templates"
ADMIN = ROOT / "arabela_admin"
HEADER = ADMIN / "partials" / "notifications.html"


def read(path):
    return path.read_text(encoding="utf8")


def camera_inputs(text):
    """(kind, name) for every <input ... capture=...>: the Alpine x-ref or the id the camera button is wired to."""
    found = []
    for tag in re.findall(r"<input\b[^>]*\bcapture=[^>]*>", text):
        ref = re.search(r'x-ref="([^"]+)"', tag)
        ident = re.search(r'\bid="([^"]+)"', tag)
        found.append(("ref", ref.group(1)) if ref else ("id", ident.group(1)))
    return found


def button_tags(text):
    return re.findall(r"<button\b[^>]*>", text, flags=re.S)


class EveryCameraButtonIsMarkedTests(SimpleTestCase):
    def test_every_camera_input_has_a_button_that_is_hidden_on_a_computer(self):
        total = 0
        for path in sorted(ADMIN.rglob("*.html")):
            text = read(path)
            for kind, name in camera_inputs(text):
                total += 1
                with self.subTest(template=path.name, input=name):
                    if kind == "ref":
                        buttons = [b for b in button_tags(text) if f"$refs.{name}.click()" in b]
                    else:
                        buttons = [b for b in button_tags(text) if f'id="{name}-btn"' in b]
                    self.assertGreaterEqual(len(buttons), 1, "a camera input nobody can press")
                    for button in buttons:
                        self.assertRegex(button, r'class="(?:[^"]* )?touch-only[ "]')
        self.assertEqual(total, 4)                                    # Attach receipt, New Booking, Upload Receipt, Replace photo

    def test_the_customer_pages_never_open_a_camera(self):
        for path in sorted(ROOT.glob("*.html")):
            with self.subTest(template=path.name):
                self.assertNotIn("capture=", read(path))

    def test_no_other_admin_photo_upload_has_a_camera_button(self):
        """Gown photos, the GCash QR, the profile photo and the category pictures are file-only."""
        for name in ("gown-catalog.html", "profile.html", "categories.html"):
            with self.subTest(template=name):
                self.assertNotIn("capture=", read(ADMIN / name))
                self.assertNotIn("touch-only", read(ADMIN / name))


class SharedRuleTests(SimpleTestCase):
    def test_the_two_rules_live_once_in_the_header_every_admin_page_includes(self):
        text = read(HEADER)
        self.assertEqual(text.count("@media (hover: hover) and (pointer: fine) { .touch-only { display: none !important; } }"), 1)
        self.assertEqual(text.count("@media not all and (hover: hover) and (pointer: fine) { .pc-only { display: none !important; } }"), 1)
        for other in ADMIN.rglob("*.html"):
            if other != HEADER:
                self.assertNotIn(".touch-only {", read(other))        # one copy only

    def test_a_page_that_uses_the_marks_includes_that_header(self):
        for path in sorted(ADMIN.glob("*.html")):
            text = read(path)
            uses = "touch-only" in text or "pc-only" in text or "partials/attach_receipt.html" in text
            if uses:
                with self.subTest(template=path.name):
                    self.assertIn("arabela_admin/partials/notifications.html", text)


class LabelsAndFocusTests(SimpleTestCase):
    def test_attach_receipt_says_choose_photo_on_a_computer_and_gallery_on_a_phone(self):
        text = read(ADMIN / "partials" / "attach_receipt.html")
        self.assertIn('<button type="button" class="ar-btn ar-btn--ghost touch-only" id="ar-camera-btn">Take photo</button>', text)
        self.assertIn('<span class="touch-only">Choose from gallery</span><span class="pc-only">Choose photo</span>', text)
        self.assertIn('id="ar-camera" accept="image/png,image/jpeg,image/webp" capture="environment"', text)       # the input itself is untouched

    def test_attach_receipt_focuses_the_button_that_can_be_seen(self):
        text = read(ADMIN / "partials" / "attach_receipt.html")
        self.assertIn("(cameraBtn.offsetParent !== null ? cameraBtn : document.getElementById('ar-file-btn')).focus();", text)
        self.assertNotIn("document.getElementById('ar-camera-btn').focus();", text)
        self.assertIn("document.getElementById('ar-camera-btn').addEventListener('click'", text)                  # still wired on a phone

    def test_reservation_records_labels(self):
        text = read(ADMIN / "reservation-records.html")
        self.assertEqual(text.count('<span class="touch-only">Choose from Gallery</span><span class="pc-only">Choose Photo</span>'), 1)
        self.assertEqual(text.count("""<span class="touch-only" x-text="photoReplaceBusy ? 'Uploading...' : 'Choose from Gallery'"></span>"""), 1)
        self.assertEqual(text.count("""<span class="pc-only" x-text="photoReplaceBusy ? 'Uploading...' : 'Choose Photo'"></span>"""), 1)
        self.assertEqual(text.count("Take Photo"), 2)                                                              # both camera buttons still say it (on a phone)

    def test_new_booking_gallery_icon_has_a_wording_true_on_every_device(self):
        text = read(ADMIN / "new-booking.html")
        self.assertIn('title="Choose the receipt photo" aria-label="Choose the receipt photo"', text)
        self.assertNotIn("from the gallery", text)
        self.assertIn('class="nb-ib touch-only" @click="$refs.nbReceiptCamera.click()"', text)


class RenderedPagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("cam_owner", "cam_owner@example.com", "Pass-12345!")

    def test_the_pages_that_carry_a_camera_button_still_open_with_the_marks_and_the_rule(self):
        self.client.force_login(self.owner)
        for name in ("pending_approval", "new_booking", "reservation_records"):
            with self.subTest(page=name):
                response = self.client.get(reverse("arabela_admin:" + name))
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertIn("touch-only", html)
                self.assertEqual(html.count(".touch-only { display: none !important; }"), 1)
                self.assertEqual(html.count(".pc-only { display: none !important; }"), 1)
                self.assertNotIn("{%", html)
