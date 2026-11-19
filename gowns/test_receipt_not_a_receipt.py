"""A screenshot that is NOT a GCash receipt must never be accepted by the receipt check (2026-11-17).

What happened: a customer's "receipt" was a screenshot of the admin Payment Verification page. The first reading called it "a list of
transactions", the second called it "not a payment receipt", and the code took two DIFFERENT refusals as a disagreement and sent the picture on
to the staff as "needs check". Now:

  * two refusals that both say "this is not a GCash receipt" (not a payment / a list of transactions / not GCash) AGREE and stand;
  * a picture the first reading accepted without finding the title of any GCash receipt layout on it has to be confirmed by the second reading;
  * the second reading only calls a picture a payment screen when it carries a GCash layout title (an admin page, a statement or a chat can show a
    reference, an amount and a date too);
  * a big picture is scaled down before it is sent to the reader, and the whole check keeps inside about 20 seconds.
The reader (Google Gemini) is never called here.
"""
import io
import itertools
import time
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from PIL import Image, ImageDraw

from gowns import receipt_check as rc
from gowns.models import SiteSettings
from gowns.test_receipt_checkout import ReceiptCheckBase, good_reading
from gowns.test_receipt_rules import NOW, SHOP, at, judge, reading
from gowns.test_receipt_verification import POOR, SHARP, SOFT, lines
from reservations.models import ProofRejection

# A reading that leads to each "this is not a GCash receipt" refusal.
NO_RECEIPT = {
    "not_payment": dict(is_payment_screen=False, clarity="clear"),
    "list_screen": dict(is_payment_screen=False, is_transaction_list=True, clarity="clear"),
    "not_gcash": dict(app="maya"),
}


def fresh(**changes):
    return reading(paid_at=at(minutes=5), **changes)


# What the admin Payment Verification page looks like once its text is copied: a reference, an amount, a date, the word GCash... and no title of
# any GCash receipt layout.
ADMIN_PAGE_LINES = ["Payment Verification", "RSV-2026-0101", "Maria Cruz", "GCash", "Reference No. 5046933479702", "₱1.00", "Nov 17, 2026 3:35 PM",
                    "Approve", "Reject"]


class LooksLikeAGcashReceiptTests(SimpleTestCase):
    def test_every_gcash_layout_is_recognised_by_its_layout_or_its_title(self):
        for layout in ("express_send", "sent_via_gcash", "transaction_details"):
            with self.subTest(layout=layout):
                self.assertTrue(rc.looks_like_gcash_receipt(reading(layout=layout, gcash_signs=[])))
        for sign in ("Express Send", "Sent via GCash", "Transaction Details", "Transfer from", "Total Amount Sent", "express send"):
            with self.subTest(sign=sign):
                self.assertTrue(rc.looks_like_gcash_receipt(reading(layout="unknown", gcash_signs=[sign])))

    def test_a_picture_with_no_layout_and_no_title_is_not_recognised(self):
        for layout, signs in (("unknown", []), ("other", []), ("other", ["Ref No."]), ("unknown", ["GCash"]), ("other", ["Reference Number"])):
            with self.subTest(layout=layout, signs=signs):
                self.assertFalse(rc.looks_like_gcash_receipt(reading(layout=layout, gcash_signs=signs)))
        self.assertFalse(rc.looks_like_gcash_receipt(None))


class TheSecondReadingNeedsATitleTests(SimpleTestCase):
    def test_a_page_with_a_reference_an_amount_and_a_date_but_no_layout_title_is_no_payment_screen(self):
        r = rc.reading_from_transcript(lines(*ADMIN_PAGE_LINES))
        self.assertEqual((r.reference, r.amount), ("5046933479702", Decimal("1.00")))        # it did find the numbers...
        self.assertIsNotNone(r.paid_at)
        self.assertFalse(r.is_payment_screen)                                                # ...but there is no GCash layout title
        self.assertEqual(judge(r).codes, ["not_payment"])

    def test_every_layout_title_makes_the_same_page_a_payment_screen(self):
        for title in ("Express Send", "Sent via GCash", "Transaction Details", "Transfer from 09167957938 to 09488745992", "Total Amount Sent ₱1.00"):
            with self.subTest(title=title):
                r = rc.reading_from_transcript(lines(title, "Amount 1.00", "Ref No. 5046 933 479702", "Nov 17, 2026 3:35 PM"))
                self.assertTrue(r.is_payment_screen)

    def test_a_maya_or_bank_receipt_is_still_read_as_a_payment_screen_so_it_is_refused_as_not_gcash(self):
        maya = rc.reading_from_transcript(lines("maya", "Sent money to", "09105950144", "₱87.00", "Reference ID 1234567890123", "Nov 7, 2026 1:13 PM"))
        self.assertEqual(judge(maya).codes, ["not_gcash"])
        bank = rc.reading_from_transcript(lines("BDO", "InstaPay transfer", "₱500.00", "Reference No. 1234567890123", "Nov 7, 2026 1:13 PM"))
        self.assertEqual(judge(bank).codes, ["not_gcash"])

    def test_a_real_receipt_in_each_layout_still_passes_the_rules(self):
        cases = {
            "express_send": ["Express Send", "DE••••K C.", "+63 948 874 5992", "Amount 1.00", "Total Amount Sent ₱1.00", "Ref No. 3047 307 194512", "Nov 12, 2026 2:55 PM"],
            "sent_via_gcash": ["DE••••K C.", "+63 9•••••5992", "Sent via GCash", "Amount 1.00", "Total Amount Sent ₱1.00", "Ref No. 1047 370 872947", "Nov 12, 2026 2:55 PM"],
            "transaction_details": ["Transaction Details", "Transfer from 09167957938 to 09488745992", "Amount -1.00", "Date & Time Nov 12, 2026 2:55 PM",
                                    "Reference Number 5046933479702"],
        }
        for layout, text in cases.items():
            with self.subTest(layout=layout):
                r = rc.reading_from_transcript(lines(*text))
                self.assertTrue(r.is_payment_screen, layout)
                self.assertTrue(rc.looks_like_gcash_receipt(r), layout)


class TwoRefusalsThatSayNoReceiptAgreeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        shop = SiteSettings.load()
        shop.gcash_number = SHOP
        shop.gcash_account_name = "Denmark Concepcion"
        shop.save()

    def run_check(self, first, second, *, quality=SHARP):
        value = second
        with patch("gowns.receipt_check.read_receipt", return_value=(first, "")), \
                patch("gowns.receipt_check.verify_read", return_value=(value, "") if value is not None else (None, "timeout")) as verify, \
                patch("gowns.receipt_quality.picture_quality", return_value=quality):
            self.verify = verify
            return rc.check_receipt(b"picture", "receipt.png", expected_amount=Decimal("2000"), now=NOW)

    def test_every_pair_of_no_receipt_answers_is_a_refusal_in_the_first_ones_words(self):
        for a, b in itertools.product(NO_RECEIPT, repeat=2):
            with self.subTest(first=a, second=b):
                verdict = self.run_check(fresh(**NO_RECEIPT[a]), fresh(**NO_RECEIPT[b]))
                self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, [a]))
                self.assertEqual(verdict.message, rc.MESSAGES[a])

    def test_the_admin_page_that_was_once_let_through_is_refused(self):
        first = fresh(**NO_RECEIPT["list_screen"])                                              # "a list of transactions"
        second = rc.reading_from_transcript(lines(*ADMIN_PAGE_LINES))                           # copied text, no layout title
        verdict = self.run_check(first, second)
        self.assertEqual(verdict.status, rc.STATUS_REJECT)
        self.assertEqual(verdict.codes, ["list_screen"])
        self.assertNotEqual(verdict.status, rc.STATUS_NEEDS_CHECK)

    def test_no_receipt_pairs_stand_on_a_flat_picture_but_a_blurry_gcash_claim_is_a_retake(self):
        for a, b in itertools.product(("not_payment", "list_screen"), repeat=2):
            with self.subTest(first=a, second=b):
                self.assertEqual(self.run_check(fresh(**NO_RECEIPT[a]), fresh(**NO_RECEIPT[b]), quality=POOR).status, rc.STATUS_REJECT)
        for a, b in (("not_gcash", "not_gcash"), ("not_gcash", "not_payment")):
            with self.subTest(first=a, second=b):
                verdict = self.run_check(fresh(**NO_RECEIPT[a]), fresh(**NO_RECEIPT[b]), quality=POOR)
                self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_RETAKE, ["blurry"]))

    def test_a_no_receipt_answer_against_a_different_kind_of_refusal_is_still_sent_to_the_staff(self):
        # one reading found no receipt, the other found a receipt for somebody else: a real receipt may have been misread by one of them
        for first, second in ((fresh(**NO_RECEIPT["not_payment"]), fresh(recipient_number="09265411484")),
                              (fresh(recipient_number="09265411484"), fresh(**NO_RECEIPT["list_screen"])),
                              (fresh(**NO_RECEIPT["not_gcash"]), fresh(direction="received"))):
            verdict = self.run_check(first, second)
            self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["readings_disagree"]))

    def test_a_no_receipt_answer_the_second_reading_does_not_share_is_still_sent_to_the_staff(self):
        # the second reading found a real GCash receipt (it carries a layout title): that is the one case where a real receipt may be in the picture
        verdict = self.run_check(fresh(**NO_RECEIPT["not_payment"]), fresh())
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["readings_disagree"]))

    # ---- a real GCash payment to somebody else's biller (a school's tuition through GCash GBills), 2026-11-18 ----
    BILLER = dict(direction="merchant_payment", recipient_number="")

    def test_a_payment_to_a_biller_and_a_no_receipt_answer_agree_in_either_order(self):
        for second in NO_RECEIPT:
            with self.subTest(first="merchant", second=second):
                verdict = self.run_check(fresh(**self.BILLER), fresh(**NO_RECEIPT[second]))
                self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["merchant"]))
                self.assertEqual(verdict.message, rc.MESSAGES["merchant"])
        for first in NO_RECEIPT:
            with self.subTest(first=first, second="merchant"):
                verdict = self.run_check(fresh(**NO_RECEIPT[first]), fresh(**self.BILLER))
                self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["merchant"]))
                self.assertEqual(verdict.message, rc.MESSAGES["merchant"])             # the more specific words win, whichever reading said them

    def test_two_biller_answers_still_agree(self):
        verdict = self.run_check(fresh(**self.BILLER), fresh(**self.BILLER))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["merchant"]))

    def test_a_biller_answer_on_a_blurry_picture_is_a_retake_not_a_refusal(self):
        for first, second in ((fresh(**self.BILLER), fresh(**NO_RECEIPT["not_payment"])), (fresh(**NO_RECEIPT["not_payment"]), fresh(**self.BILLER))):
            with self.subTest(first=first.direction, second=second.direction):
                verdict = self.run_check(first, second, quality=POOR)
                self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_RETAKE, ["blurry"]))

    def test_a_biller_answer_against_a_different_kind_of_refusal_or_an_accepted_receipt_still_goes_to_the_staff(self):
        # a real receipt may have been misread by one of the readings: only "not for the shop" against "not for the shop" is a refusal
        for second in (fresh(recipient_number="09265411484"), fresh(direction="received"), fresh()):
            with self.subTest(second=second.recipient_number or second.direction):
                verdict = self.run_check(fresh(**self.BILLER), second)
                self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["readings_disagree"]))

    def test_the_tuition_receipt_as_the_two_readers_really_saw_it_is_refused(self):
        # First reading (the one that looks at the picture): a payment to a biller, no recipient number, the name "STI", a 9-digit reference.
        first = rc.reading_from_dict({
            "is_payment_screen": True, "is_transaction_list": False, "app": "gcash", "direction": "merchant_payment", "recipient_number": "",
            "recipient_pattern": "", "recipient_name": "STI", "sender_number": "", "amount": "6437.06", "reference": "586757758",
            "paid_at": "2026-11-16T00:00:00+08:00", "status": "completed", "gcash_signs": ["Paid via GCASH", "GCash Reference No.", "GCash GBills"],
            "layout": "other", "picture": "screenshot", "clarity": "clear", "cut_off": False})
        # Second reading (the one that copies the text): none of the GCash receipt layout titles is on it.
        second = rc.reading_from_transcript(lines(
            "STI", "Paid via GCASH", "Campus", "Sample Campus", "Student Name", "Juan Dela Cruz", "Student ID or App No", "02000000000", "School Year",
            "2026-2027", "Term", "1st Term", "Remarks", "Tuition Fee", "Email Address", "student@example.com", "Bill Amount", "6,412.06", "Fee", "25.00",
            "Total", "₱ 6,437.06", "Date", "Nov 16, 2026", "GCash Reference No.", "586757758", "GCash GBills"))
        self.assertFalse(second.is_payment_screen)
        verdict = self.run_check(first, second)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["merchant"]))
        self.assertIn("store or biller", verdict.message)


class AnUnusualLayoutHasToBeConfirmedTests(TestCase):
    """The first reading accepted a picture but found no GCash layout title on it (looks_like_gcash_receipt is False)."""

    @classmethod
    def setUpTestData(cls):
        shop = SiteSettings.load()
        shop.gcash_number = SHOP
        shop.gcash_account_name = "Denmark Concepcion"
        shop.save()

    UNUSUAL = dict(layout="other", gcash_signs=[])

    def run_check(self, first, second, *, quality=SHARP):
        with patch("gowns.receipt_check.read_receipt", return_value=(first, "" if first is not None else "timeout")) as read, \
                patch("gowns.receipt_check.verify_read", return_value=(second, "") if second is not None else (None, "timeout")) as verify, \
                patch("gowns.receipt_quality.picture_quality", return_value=quality):
            self.read, self.verify = read, verify
            return rc.check_receipt(b"picture", "receipt.png", expected_amount=Decimal("2000"), now=NOW)

    def test_a_picture_the_second_reading_calls_no_receipt_is_refused(self):
        for key in ("not_payment", "list_screen"):
            with self.subTest(second=key):
                verdict = self.run_check(fresh(**self.UNUSUAL), fresh(**NO_RECEIPT[key]))
                self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["not_payment"]))
                self.assertEqual(verdict.message, rc.MESSAGES["not_payment"])
                self.verify.assert_called_once()

    def test_the_admin_page_read_as_a_payment_by_the_first_reading_is_refused_by_the_second(self):
        verdict = self.run_check(fresh(**self.UNUSUAL), rc.reading_from_transcript(lines(*ADMIN_PAGE_LINES)))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["not_payment"]))

    def test_a_real_receipt_in_a_layout_the_first_reading_did_not_name_passes_when_the_second_finds_the_title(self):
        verdict = self.run_check(fresh(**self.UNUSUAL), fresh(layout="sent_via_gcash", gcash_signs=["Sent via GCash"]))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_PASS, []))
        self.assertEqual(verdict.reference, "5046933479702")

    def test_a_second_reading_that_could_not_be_made_sends_it_to_the_staff_with_a_note(self):
        verdict = self.run_check(fresh(**self.UNUSUAL), None)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["layout_unconfirmed"]))
        self.assertTrue(any("second reading could not be made" in note for note in verdict.notes))
        self.assertEqual(verdict.reference, "5046933479702")

    def test_a_second_reading_that_cannot_read_the_picture_sends_it_to_the_staff(self):
        # an unreadable second reading proves nothing either way, so the picture is neither refused nor waved through: the staff look
        unreadable = fresh(clarity="unreadable", reference="", recipient_number="", is_payment_screen=False)
        verdict = self.run_check(fresh(**self.UNUSUAL), unreadable)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["layout_unconfirmed"]))

    def test_a_blurry_picture_is_asked_for_again_not_refused(self):
        verdict = self.run_check(fresh(**self.UNUSUAL), fresh(**NO_RECEIPT["not_payment"]), quality=POOR)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_RETAKE, ["blurry"]))

    def test_a_second_reading_that_finds_a_receipt_for_somebody_else_goes_to_the_staff(self):
        verdict = self.run_check(fresh(**self.UNUSUAL), fresh(recipient_number="09265411484"))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["layout_unconfirmed"]))

    def test_a_receipt_with_a_known_layout_or_title_is_never_read_twice(self):
        for changes in ({"layout": "sent_via_gcash", "gcash_signs": []}, {"layout": "other", "gcash_signs": ["Total Amount Sent"]}):
            with self.subTest(changes=changes):
                verdict = self.run_check(fresh(**changes), None)
                self.assertEqual(verdict.status, rc.STATUS_PASS)
                self.verify.assert_not_called()

    def test_a_reader_that_is_down_is_not_double_checked(self):
        verdict = self.run_check(None, None)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["reader_unavailable"]))
        self.verify.assert_not_called()

    def test_a_receipt_that_needs_a_look_anyway_is_still_confirmed_first(self):
        # a different amount (needs check) in an unusual layout: still has to be a receipt first
        verdict = self.run_check(fresh(amount=Decimal("100"), **self.UNUSUAL), fresh(**NO_RECEIPT["not_payment"]))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["not_payment"]))
        verdict = self.run_check(fresh(amount=Decimal("100"), **self.UNUSUAL), fresh(layout="express_send", gcash_signs=["Express Send"]))
        self.assertEqual(verdict.status, rc.STATUS_NEEDS_CHECK)
        self.assertIn("amount_low", verdict.codes)


class ThroughTheCheckoutTests(ReceiptCheckBase):
    def test_the_admin_screenshot_books_nothing_holds_no_dates_and_counts_as_a_refusal(self):
        self.reads(good_reading(**NO_RECEIPT["list_screen"]), second=rc.reading_from_transcript(lines(*ADMIN_PAGE_LINES)))
        data = self.refused(self.checkout("a"))
        self.assertIn("many transactions", data["error"])
        self.nothing_booked()
        self.assertEqual(ProofRejection.objects.filter(user=self.customers["a"]).count(), 1)

    def test_a_non_receipt_the_first_reading_took_for_a_payment_is_refused_too(self):
        self.reads(good_reading(layout="other", gcash_signs=[]), second=rc.reading_from_transcript(lines(*ADMIN_PAGE_LINES)))
        data = self.refused(self.checkout("b"))
        self.assertIn("doesn't look like a payment receipt", data["error"])
        self.nothing_booked()

    def test_a_real_receipt_is_still_booked(self):
        self.reads(good_reading())
        reservation = self.booked(self.checkout("c"))
        self.assertEqual(reservation.proof_check, "pass")


# ---- speed -----------------------------------------------------------------------------------------------------------------------------
def picture(size, mode="RGB", fmt="PNG"):
    image = Image.new(mode, size, (255, 255, 255, 255) if mode == "RGBA" else (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for n in range(0, size[1], 70):
        draw.text((40, n), "Sent via GCash  Ref No. 1047 370 872947", fill=(20, 40, 120))
    buffer = io.BytesIO()
    image.save(buffer, fmt)
    return buffer.getvalue()


class ThePictureSentToTheReaderTests(TestCase):
    def test_a_big_screenshot_is_scaled_down_and_sent_as_a_jpeg(self):
        original = picture((1440, 3200))
        sent, mime = rc._for_reader(original, "image/png")
        self.assertEqual(mime, "image/jpeg")
        self.assertLess(len(sent), len(original))
        opened = Image.open(io.BytesIO(sent))
        self.assertEqual(max(opened.size), rc.READER_MAX_SIDE)
        self.assertAlmostEqual(opened.size[0] / opened.size[1], 1440 / 3200, places=2)           # the shape is kept

    def test_a_small_picture_goes_exactly_as_it_is(self):
        original = picture((400, 800))
        self.assertEqual(rc._for_reader(original, "image/png"), (original, "image/png"))

    def test_a_picture_with_transparency_is_drawn_on_white(self):
        sent, mime = rc._for_reader(picture((1200, 2600), mode="RGBA"), "image/png")
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(Image.open(io.BytesIO(sent)).mode, "RGB")

    def test_something_that_is_not_a_picture_is_sent_unchanged_and_never_raises(self):
        for junk in (b"picture", b"", b"\x89PNG\r\n\x1a\nnot really"):
            with self.subTest(junk=junk):
                self.assertEqual(rc._for_reader(junk, "image/png"), (junk, "image/png"))

    def test_the_stored_picture_and_its_hash_are_not_the_scaled_one(self):
        original = picture((1440, 3200))
        seen = {}

        def fake_read(image_bytes, mime):
            seen["bytes"], seen["mime"] = image_bytes, mime
            return fresh(), ""

        with patch("gowns.receipt_check.read_receipt", side_effect=fake_read):
            verdict = rc.check_receipt(original, "receipt.png", expected_amount=Decimal("2000"), now=NOW)
        import hashlib
        self.assertEqual(verdict.image_hash, hashlib.sha256(original).hexdigest())             # the hash is of what the customer uploaded
        self.assertEqual(seen["mime"], "image/jpeg")
        self.assertLess(len(seen["bytes"]), len(original))


class TheWholeCheckStaysInsideAboutFifteenSecondsTests(SimpleTestCase):
    def test_the_limits_add_up_to_fifteen_seconds(self):
        self.assertLessEqual(rc.READER_TIMEOUT_SECONDS, 15)
        self.assertLessEqual(rc.SECOND_READ_TIMEOUT_SECONDS, 15)
        self.assertLess(rc.READER_CUTOFF_SECONDS, rc.READER_TIMEOUT_SECONDS)
        self.assertLess(rc.SECOND_READ_CUTOFF_SECONDS, rc.SECOND_READ_TIMEOUT_SECONDS)
        self.assertLessEqual(rc.SECOND_READ_SKIP_AFTER_SECONDS, 8)

    def test_a_first_reading_that_took_long_leaves_no_time_for_a_second(self):
        with patch("gowns.receipt_check.requests.post") as post:
            result, why = rc.verify_read(b"x", "image/png", started=time.monotonic() - (rc.SECOND_READ_SKIP_AFTER_SECONDS + 1))
        self.assertEqual((result, why), (None, "no_time"))
        post.assert_not_called()

    def test_the_second_reading_never_gets_more_time_than_is_left(self):
        timeouts = []

        def post(url, **kwargs):
            timeouts.append(kwargs["timeout"])
            raise __import__("requests").Timeout()

        with self.settings(RECEIPT_READER_ENABLED=True, GEMINI_API_KEY="k", GEMINI_MODEL="a", GEMINI_FALLBACK_MODEL="b", GEMINI_VERIFY_MODEL=""):
            with patch("gowns.receipt_check.requests.post", side_effect=post):
                rc.verify_read(b"x", "image/png", started=time.monotonic() - 5)
        self.assertTrue(timeouts)
        self.assertLessEqual(timeouts[0], rc.SECOND_READ_TIMEOUT_SECONDS - 4)                  # 5 seconds were already used

    def test_the_first_prompt_says_a_website_or_admin_screenshot_is_no_payment_screen(self):
        self.assertIn("admin / dashboard page", rc.PROMPT)
        self.assertIn("website", rc.PROMPT)


# ---- the loading screen ----------------------------------------------------------------------------------------------------------------
class TheLoadingScreenIsASpinnerWithNoWordsTests(TestCase):
    """While a receipt is checked the customer sees a plain white loading screen with a spinner and no text (the owner's choice, 2026-11-18)."""

    @staticmethod
    def source(name):
        from django.template.loader import get_template
        return get_template(name).template.source

    def test_the_script_is_on_every_customer_page_and_prints_no_words(self):
        from django.contrib.staticfiles import finders
        self.assertIn("js/receipt-checking.js", self.client.get("/").content.decode())
        with open(finders.find("js/receipt-checking.js"), encoding="utf-8") as handle:
            script = handle.read()
        for text in ("up to 20 seconds", "Still reading it", "keep this page open", "receipt-checking-text", "textContent = text", "STEPS"):
            self.assertNotIn(text, script)
        for name in ("start", "stop", "show", "hide"):
            self.assertIn(f"{name}: {name}", script)              # the pages' calls keep working
        self.assertIn("createElement('div')", script)
        self.assertNotIn("createElement('p')", script)             # no text element is built
        self.assertIn("aria-label", script)                        # screen readers still hear what is happening

    def test_the_checkout_loading_screen_is_only_a_spinner(self):
        source = self.source("reservation.html")
        start = source.index('<div id="loading-overlay"')
        overlay = source[start:source.index("{% endblock %}", start)]          # the overlay is the last thing in the content block
        self.assertIn("loading-spinner", overlay)
        self.assertNotIn("<p", overlay)
        self.assertNotIn("receipt-checking-text", source)
        self.assertNotIn("Checking your receipt&hellip;", source)
        self.assertIn('aria-label="Checking your receipt"', overlay)
        self.assertIn("arabelaReceiptChecking.start()", source)
        self.assertIn("arabelaReceiptChecking.stop()", source)

    def test_uploading_a_proof_later_shows_the_same_screen(self):
        for name in ("reservations.html", "reservation_item_detail.html"):
            with self.subTest(page=name):
                source = self.source(name)
                self.assertIn("arabelaReceiptChecking.show()", source)
                self.assertIn("arabelaReceiptChecking.hide()", source)
                self.assertIn("js/receipt-checking.js", source)                # these two pages do not extend base.html: they load the script themselves

    def test_the_checkout_gets_the_script_from_the_base_page(self):
        self.assertIn("{% extends", self.source("reservation.html")[:200])
        self.assertIn("js/receipt-checking.js", self.source("base.html"))


# ---- a picture with nothing to read in it --------------------------------------------------------------------------------------------
class NothingToReadIsNoReceiptTests(TwoRefusalsThatSayNoReceiptAgreeTests):
    """A drawing or a photo of a gown has no text at all: the reader calls it "unreadable" (a retake), but when NEITHER reading found a single field,
    layout title or payment word and the picture is not blurry, there is no receipt in it, so it is refused instead of asked for again."""

    def nothing(self, **changes):
        return rc.Reading(**{"clarity": "unreadable", **changes})

    def test_nothing_found_by_either_reading_on_a_picture_that_is_not_blurry_is_refused(self):
        for quality in (SHARP, SOFT):
            with self.subTest(label=quality["label"]):
                verdict = self.run_check(self.nothing(), self.nothing(), quality=quality)
                self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["not_payment"]))
                self.assertEqual(verdict.message, rc.MESSAGES["not_payment"])

    def test_the_same_picture_when_it_is_blurry_is_asked_for_again(self):
        verdict = self.run_check(self.nothing(), self.nothing(), quality=POOR)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_RETAKE, ["blurry"]))

    def test_a_blurred_receipt_always_gives_the_readers_something_so_it_is_still_asked_for_again(self):
        for first, second in (
            (self.nothing(reference="5046933479702"), self.nothing()),
            (self.nothing(), self.nothing(amount=Decimal("100"))),
            (self.nothing(gcash_signs=["Sent via GCash"]), self.nothing()),
            (self.nothing(), self.nothing(paid_at=at(minutes=5))),
            (self.nothing(recipient_pattern="09?????5992"), self.nothing()),
            (self.nothing(app="gcash"), self.nothing()),
        ):
            verdict = self.run_check(first, second, quality=SOFT)
            self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_RETAKE, ["blurry"]), (first, second))

    def test_a_second_reading_that_could_not_be_made_never_refuses_it(self):
        verdict = self.run_check(self.nothing(), None, quality=SHARP)
        self.assertEqual(verdict.status, rc.STATUS_RETAKE)

    def test_a_second_reading_that_found_the_receipt_still_rescues_it(self):
        verdict = self.run_check(self.nothing(), fresh(), quality=SOFT)
        self.assertEqual(verdict.status, rc.STATUS_PASS)
