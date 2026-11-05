"""Proof-of-payment pictures: the real-image check and the clean copy (gowns/proof_image.py), the upload size guard
(arabela_system/middleware.py) and the two customer endpoints that take a proof.

The checks on the picture itself need no database. The endpoint tests mock _save_proof_file like every other test that uploads a proof:
this environment's default storage can be real Cloudinary, and nothing here may ever upload to that live account."""
import io
import json
import os
import re
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from PIL import Image

from arabela_system.middleware import PROOF_REQUEST_LIMIT_BYTES, ProofUploadSizeGuardMiddleware
from gowns import proof_image
from gowns.models import Gown
from gowns.sample_images import TINY_JPEG, make_image_bytes
from reservations.models import Reservation, ReservationItem

User = get_user_model()

PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
HTML = b"<html><body><script>alert('x')</script></body></html>"
EXE = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100


def upload(name, data, content_type="image/jpeg"):
    return SimpleUploadedFile(name, data, content_type=content_type)


def noise_jpeg(size=(120, 120)):
    """A JPEG with real content (random pixels), big enough that cutting it in half really truncates the picture."""
    buffer = io.BytesIO()
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(buffer, "JPEG", quality=95)
    return buffer.getvalue()


def opened(clean_file):
    clean_file.seek(0)
    return Image.open(io.BytesIO(clean_file.read()))


class CleanProofImageTests(SimpleTestCase):
    """What counts as a real picture, and what is stored instead of the uploaded bytes."""

    def test_a_normal_jpeg_png_and_webp_are_accepted_and_keep_their_format(self):
        for fmt, ext in (("JPEG", ".jpg"), ("PNG", ".png"), ("WEBP", ".webp")):
            with self.subTest(fmt=fmt):
                clean, error = proof_image.clean_proof_image(upload("receipt" + ext, make_image_bytes(fmt, (60, 40))))
                self.assertEqual(error, "")
                self.assertRegex(clean.name, r"^[0-9a-f]{32}" + re.escape(ext) + "$")      # a random name, whatever the upload was called
                image = opened(clean)
                self.assertEqual((image.format, image.size), (fmt, (60, 40)))

    def test_a_screenshot_stays_a_png_so_the_small_text_stays_sharp(self):
        clean, error = proof_image.clean_proof_image(upload("Screenshot.png", make_image_bytes("PNG", (1080, 2400)), "image/png"))
        self.assertEqual(error, "")
        self.assertEqual(opened(clean).format, "PNG")

    def test_whatever_the_file_is_called_and_claims_to_be_it_is_judged_by_what_it_is(self):
        """A PDF, a web page, a Windows program and a video, each renamed to look like a picture, are refused."""
        for label, data in (("a PDF", PDF), ("a web page", HTML), ("a Windows program", EXE), ("a video", MP4)):
            for name, claimed in (("receipt.jpg", "image/jpeg"), ("receipt.png", "image/png"), ("receipt.webp", "image/webp"), ("receipt.jpg", "")):
                with self.subTest(file=label, name=name, claimed=claimed):
                    clean, error = proof_image.clean_proof_image(upload(name, data, claimed))
                    self.assertIsNone(clean)
                    self.assertEqual(error, proof_image.NOT_AN_IMAGE)

    def test_the_refusal_tells_the_customer_what_to_do_about_a_pdf_receipt(self):
        self.assertIn("take a screenshot", proof_image.NOT_AN_IMAGE)

    def test_other_picture_formats_are_not_accepted_even_when_they_are_real_pictures(self):
        for fmt in ("GIF", "BMP", "TIFF"):
            with self.subTest(fmt=fmt):
                buffer = io.BytesIO()
                Image.new("RGB", (20, 20), (200, 10, 10)).save(buffer, fmt)
                clean, error = proof_image.clean_proof_image(upload("receipt.jpg", buffer.getvalue()))
                self.assertEqual((clean, error), (None, proof_image.NOT_AN_IMAGE))

    def test_an_empty_file_is_refused(self):
        self.assertEqual(proof_image.clean_proof_image(upload("empty.jpg", b"")), (None, proof_image.EMPTY))

    def test_a_damaged_picture_is_refused(self):
        data = noise_jpeg()
        self.assertEqual(proof_image.clean_proof_image(upload("cut.jpg", data[:len(data) // 2])), (None, proof_image.NOT_AN_IMAGE))
        self.assertEqual(proof_image.clean_proof_image(upload("junk.png", b"\x89PNG\r\n\x1a\n" + b"not really")), (None, proof_image.NOT_AN_IMAGE))

    def test_anything_over_5mb_is_refused_before_it_is_even_opened(self):
        big = TINY_JPEG + b"\x00" * (proof_image.MAX_BYTES + 1)
        self.assertEqual(proof_image.clean_proof_image(upload("big.jpg", big)), (None, proof_image.TOO_BIG))

    def test_a_tiny_file_that_holds_an_enormous_picture_is_refused(self):
        """A picture of 26 million pixels is a few kilobytes of PNG but would take hundreds of megabytes of memory to open."""
        data = make_image_bytes("PNG", (5200, 5200), color=(255, 255, 255))
        self.assertLess(len(data), proof_image.MAX_BYTES)
        self.assertEqual(proof_image.clean_proof_image(upload("bomb.png", data, "image/png")), (None, proof_image.TOO_LARGE_PICTURE))

    def test_a_very_wide_picture_is_scaled_down_to_a_sensible_size(self):
        buffer = io.BytesIO()
        Image.new("L", (5000, 300), 255).save(buffer, "PNG")
        clean, error = proof_image.clean_proof_image(upload("wide.png", buffer.getvalue(), "image/png"))
        self.assertEqual(error, "")
        self.assertEqual(max(opened(clean).size), proof_image.MAX_SIDE)

    def test_something_hidden_after_the_picture_does_not_survive(self):
        """A JPEG with a PDF glued on behind it opens fine as a picture -- and the stored copy has no PDF in it."""
        sneaky = noise_jpeg() + PDF
        clean, error = proof_image.clean_proof_image(upload("receipt.jpg", sneaky))
        self.assertEqual(error, "")
        stored = clean.read()
        self.assertNotIn(b"%PDF", stored)
        self.assertNotEqual(stored, sneaky)

    def test_the_phones_hidden_details_are_dropped_and_a_sideways_photo_is_stood_upright(self):
        buffer = io.BytesIO()
        exif = Image.Exif()
        exif[0x010F] = "SecretPhoneMaker"     # camera make
        exif[0x0112] = 6                      # orientation: stored sideways, to be shown rotated
        Image.new("RGB", (40, 20), (10, 98, 255)).save(buffer, "JPEG", exif=exif.tobytes())
        clean, error = proof_image.clean_proof_image(upload("photo.jpg", buffer.getvalue()))
        self.assertEqual(error, "")
        image = opened(clean)
        self.assertEqual(image.size, (20, 40))                  # now upright (width and height swapped)
        self.assertEqual(len(image.getexif()), 0)               # nothing about the phone or the place is kept
        clean.seek(0)
        self.assertNotIn(b"SecretPhoneMaker", clean.read())

    def test_less_common_but_real_pictures_still_work(self):
        cases = []
        cmyk = io.BytesIO()
        Image.new("CMYK", (30, 30), (10, 20, 30, 40)).save(cmyk, "JPEG")
        cases.append(("a CMYK JPEG", cmyk.getvalue(), "JPEG"))
        alpha = io.BytesIO()
        Image.new("RGBA", (30, 30), (10, 98, 255, 120)).save(alpha, "PNG")
        cases.append(("a PNG with transparency", alpha.getvalue(), "PNG"))
        palette = io.BytesIO()
        Image.new("P", (30, 30), 3).save(palette, "PNG")
        cases.append(("a palette PNG", palette.getvalue(), "PNG"))
        grey = io.BytesIO()
        Image.new("L", (30, 30), 90).save(grey, "JPEG")
        cases.append(("a grey JPEG", grey.getvalue(), "JPEG"))
        for label, data, fmt in cases:
            with self.subTest(label):
                clean, error = proof_image.clean_proof_image(upload("receipt." + fmt.lower(), data))
                self.assertEqual(error, "")
                self.assertEqual(opened(clean).format, fmt)

    def test_the_uploaded_file_can_still_be_read_afterwards(self):
        """The checkout still reads the upload's name and size afterwards (the repeat-submission check), so it is rewound."""
        original = upload("receipt.jpg", TINY_JPEG)
        proof_image.clean_proof_image(original)
        self.assertEqual(original.read(), TINY_JPEG)


class ProofUploadSizeGuardTests(SimpleTestCase):
    def setUp(self):
        self.rf = RequestFactory()
        self.reached = []
        self.guard = ProofUploadSizeGuardMiddleware(lambda request: (self.reached.append(request.path), HttpResponse("ok"))[1])

    def post(self, path, announced):
        request = self.rf.post(path, data={"a": "b"})
        request.META["CONTENT_LENGTH"] = str(announced)
        return self.guard(request)

    def test_a_huge_proof_upload_is_turned_away_before_anything_is_read(self):
        for path in (reverse("gowns:reservation_submit"), reverse("gowns:reservation_upload_proof", args=[7])):
            with self.subTest(path=path):
                response = self.post(path, PROOF_REQUEST_LIMIT_BYTES + 1)
                self.assertEqual(response.status_code, 413)
                self.assertEqual(json.loads(response.content)["error"], "Proof of payment must be 5MB or smaller.")
        self.assertEqual(self.reached, [])

    def test_a_normal_upload_and_every_other_request_pass_through(self):
        self.assertEqual(self.post(reverse("gowns:reservation_submit"), 6 * 1024 * 1024).status_code, 200)                 # a 5 MB picture plus the form
        self.assertEqual(self.post(reverse("gowns:reservation_upload_proof", args=[7]), 1000).status_code, 200)
        self.assertEqual(self.post("/admin-panel/", PROOF_REQUEST_LIMIT_BYTES * 3).status_code, 200)                    # not a proof upload
        self.assertEqual(self.guard(self.rf.get(reverse("gowns:reservation_submit"))).status_code, 200)               # not a POST
        self.assertEqual(self.post(reverse("gowns:reservation_submit"), "not a number").status_code, 200)             # junk header: let it through
        self.assertEqual(len(self.reached), 5)


class ProofUploadLaterEndpointTests(TestCase):
    """The upload-proof-later endpoint: refused files never reach storage and never set the proof; a real picture is stored as a clean copy."""

    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="proof_cust", password="x")

    def setUp(self):
        self.client.force_login(self.customer)
        self.reservation = Reservation.objects.create(customer=self.customer, customer_name="Proof Cust",
                                                      status=Reservation.Status.PENDING, payment_proof_url="")
        item = ReservationItem.objects.create(reservation=self.reservation, gown_name="Gown",
                                              rental_date="2027-02-01", return_date="2027-02-04")
        self.url = reverse("gowns:reservation_upload_proof", args=[item.id])

    def send(self, file):
        with patch("gowns.views._save_proof_file", return_value="https://example.test/proof.jpg") as saved:
            response = self.client.post(self.url, data={"proof_of_payment": file})
        self.reservation.refresh_from_db()
        return response, saved

    def test_a_pdf_a_video_and_disguised_files_are_refused_and_nothing_is_stored(self):
        cases = (("an honest PDF", upload("receipt.pdf", PDF, "application/pdf")), ("an honest video", upload("clip.mp4", MP4, "video/mp4")),
                 ("a PDF renamed .jpg", upload("receipt.jpg", PDF)), ("a web page renamed .png", upload("page.png", HTML, "image/png")),
                 ("a program renamed .jpg", upload("setup.jpg", EXE)), ("a PDF with no type sent", upload("receipt.jpg", PDF, "")),
                 ("a script name trick", upload("proof.php.jpg", HTML)), ("an empty file", upload("empty.jpg", b"")))
        for label, file in cases:
            with self.subTest(label):
                response, saved = self.send(file)
                self.assertEqual(response.status_code, 400)
                self.assertIn(response.json()["error"], (proof_image.NOT_AN_IMAGE, proof_image.EMPTY))
                saved.assert_not_called()
                self.assertEqual(self.reservation.payment_proof_url, "")

    def test_a_real_picture_is_stored_as_a_clean_copy_under_a_random_name(self):
        response, saved = self.send(upload("My GCash receipt (1).JPG", noise_jpeg()))
        self.assertEqual(response.status_code, 200, response.content)
        saved.assert_called_once()
        stored = saved.call_args.args[0]
        self.assertIsInstance(stored, ContentFile)
        self.assertRegex(stored.name, r"^[0-9a-f]{32}\.jpg$")
        self.assertEqual(Image.open(io.BytesIO(stored.read())).format, "JPEG")
        self.assertEqual(self.reservation.payment_proof_url, "https://example.test/proof.jpg")

    def test_a_png_screenshot_is_stored_as_a_png(self):
        response, saved = self.send(upload("Screenshot_2026.png", make_image_bytes("PNG", (540, 1200)), "image/png"))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertRegex(saved.call_args.args[0].name, r"^[0-9a-f]{32}\.png$")


class ProofAtCheckoutTests(TestCase):
    """Checkout (Confirm Rental) uses the same check: a renamed PDF stops the booking before anything is saved."""

    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="checkout_proof_cust", password="x")
        cls.gown = Gown.objects.create(gown_id="PROOF-0001", name="Proof Test Gown", category=Gown.Category.GUEST_GOWN, color_name="Red",
                                       color_code="RD", size=Gown.Size.MEDIUM, rental_price=Decimal("4500.00"), status=Gown.Status.AVAILABLE)

    def setUp(self):
        self.client.force_login(self.customer)

    def submit(self, file):
        items = [{"gown_name": self.gown.name, "gown_slug": self.gown.slug, "size": "Medium",
                  "rental_date": "2027-03-10", "return_date": "2027-03-13"}]
        with patch("gowns.views._save_proof_file", return_value="https://example.test/proof.jpg") as saved:
            response = self.client.post(reverse("gowns:reservation_submit"), data={
                "items": json.dumps(items), "first_name": "Test", "last_name": "Buyer", "phone": "09171234567", "address": "123 Test St",
                "city": "Test City", "postal_code": "1000", "payment_method": "GCash", "proof_of_payment": file})
        return response, saved

    def test_a_renamed_pdf_is_refused_and_no_booking_is_made(self):
        response, saved = self.submit(upload("receipt.jpg", PDF))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], proof_image.NOT_AN_IMAGE)
        saved.assert_not_called()
        self.assertFalse(Reservation.objects.filter(customer=self.customer).exists())

    def test_a_real_picture_books_normally_and_the_clean_copy_is_what_is_stored(self):
        response, saved = self.submit(upload("receipt.jpg", noise_jpeg()))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertRegex(saved.call_args.args[0].name, r"^[0-9a-f]{32}\.jpg$")
        self.assertEqual(Reservation.objects.get(customer=self.customer).payment_proof_url, "https://example.test/proof.jpg")


class ProofClientWiringTests(SimpleTestCase):
    """The pages that take a proof tell the customer about a PDF at once; these read the files, no database."""

    ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
    PAGES = ("reservation.html", "reservation_item_detail.html", "reservations.html")

    def test_every_page_that_takes_a_proof_checks_the_pick_and_then_the_size_after_shrinking(self):
        for name in self.PAGES:
            with self.subTest(page=name):
                html = (self.ROOT / "templates" / name).read_text(encoding="utf8")
                self.assertIn("arabelaProofPhotoProblem(", html)
                self.assertIn(", true)", html)          # the second call, size only, on the prepared copy
                self.assertIn("arabelaPreparePhotoForUpload(", html)

    def test_the_size_is_judged_only_after_the_photo_has_been_shrunk(self):
        """A big camera photo is shrunk by the page before it is sent, so the check on the picked file must not look at its size
        (it once did, and would have turned away ordinary phone photos that always worked)."""
        js = (self.ROOT / "static" / "js" / "picked-file.js").read_text(encoding="utf8")
        body = js[js.index("window.arabelaProofPhotoProblem = function"):]
        first_phase = body[body.index("if (!sizeOnly) {"):body.index("if (file.size > PROOF_MAX_BYTES)")]
        self.assertNotIn("PROOF_MAX_BYTES", first_phase)
        self.assertIn("return '';", first_phase)

    def test_the_size_guard_runs_before_anything_reads_the_body(self):
        from django.conf import settings
        order = settings.MIDDLEWARE
        guard = order.index("arabela_system.middleware.ProofUploadSizeGuardMiddleware")
        self.assertLess(guard, order.index("django.middleware.csrf.CsrfViewMiddleware"))
        self.assertLess(guard, order.index("arabela_system.middleware.SplitSessionMiddleware"))
