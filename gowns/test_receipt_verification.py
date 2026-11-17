"""The receipt check, made trustworthy for REAL receipts (2026-11-17).

A real GCash receipt was refused as "not sent to Arabela's GCash number" because GCash had hidden part of the recipient's number
(+63 9•••••5992) and the check compared it with the shop's full number. What these tests keep true:

  * a hidden number is compared on the digits that show, together with the name -- in every GCash layout (Express Send, the white "Sent via
    GCash" card, the history page "Transfer from ... to ...");
  * a picture that cannot be read (blurry, cut off, no reference number) is a RETAKE: the customer is asked for a better picture and nothing is
    counted against the account;
  * a refusal needs TWO independent readings to give the same reason on a sharp picture; anything else becomes "needs check" (the staff look) or
    a retake, never a lost customer;
  * the second reading (every line copied, the fields picked out by our own code) understands each real layout;
  * the picture's sharpness is measured on our side, and a slow or busy second reader never keeps the page waiting.
The reader (Google Gemini) is never called here.
"""
import io
import time
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from PIL import Image, ImageDraw, ImageFilter

from gowns import gcash_numbers, receipt_check as rc, receipt_quality
from gowns.models import SiteSettings
from gowns.test_receipt_rules import NOW, SHOP, STARTED, at, judge, reading

SHARP = {"label": "sharp", "sharpness": 300.0, "brightness": 200.0, "width": 1080, "height": 2340, "tiny": False, "dark": False, "poor": False}
SOFT = {**SHARP, "label": "soft", "sharpness": 50.0}
POOR = {**SHARP, "label": "blurry", "sharpness": 2.0, "poor": True}


def masked(**changes):
    """The receipt of 2026-11-17: the recipient's number is printed +63 9•••••5992."""
    values = dict(recipient_number="", recipient_pattern="09?????5992", recipient_name="DE••••K C.", layout="sent_via_gcash", clarity="clear")
    values.update(changes)
    return reading(**values)


# ---- the numbers GCash prints ---------------------------------------------------------------------------------------------------------
class PrintedNumberTests(SimpleTestCase):
    def test_every_way_a_number_can_print(self):
        cases = {
            "+63 948 874 5992": ("09488745992", ""),
            "0948 874 5992": ("09488745992", ""),
            "09488745992": ("09488745992", ""),
            "9488745992": ("09488745992", ""),
            "639488745992": ("09488745992", ""),
            "+63 9•••••5992": ("", "09?????5992"),
            "+63 9•• ••• 5992": ("", "09?????5992"),
            "09•••••5992": ("", "09?????5992"),
            "9•••••5992": ("", "09?????5992"),
            "+63 9*****5992": ("", "09?????5992"),
            "+63 9XXXXX5992": ("", "09?????5992"),
            "••••••• 5992": ("", "0??????5992"),             # all 11 characters printed, only the end shows
            "••• 5992": ("", "???????5992"),                 # a short end of the number only
            "95992": ("", "???????5992"),                  # a reader that dropped the dots: only the end can be trusted
            "0948874599": ("", ""),                       # a full-length number with a digit missing says nothing
            "+63 9•••••59": ("", ""),                     # fewer than 4 digits show
            "": ("", ""), "abc": ("", ""), None: ("", ""),
            "08488745992": ("", ""),                       # not a mobile number
        }
        for printed, expected in cases.items():
            with self.subTest(printed=printed):
                self.assertEqual(gcash_numbers.parse_printed(printed), expected)

    def test_a_pattern_matches_only_where_digits_show(self):
        self.assertTrue(gcash_numbers.pattern_matches("09?????5992", SHOP))
        self.assertTrue(gcash_numbers.pattern_matches("???????5992", SHOP))
        self.assertFalse(gcash_numbers.pattern_matches("09?????5993", SHOP))
        self.assertFalse(gcash_numbers.pattern_matches("09?????5992", "09171234567"))
        self.assertFalse(gcash_numbers.pattern_matches("09?????5992", "0948874599"))       # not 11 digits
        self.assertFalse(gcash_numbers.pattern_matches("", SHOP))

    def test_a_pattern_is_shown_the_way_it_is_printed(self):
        self.assertEqual(gcash_numbers.show_pattern("09?????5992"), "09•••••5992")
        self.assertEqual(gcash_numbers.visible_digits("09?????5992"), 5)          # the leading 0 is not counted: it tells nothing
        self.assertEqual(gcash_numbers.visible_digits("???????5992"), 4)


class ReaderAnswerTests(SimpleTestCase):
    def test_a_hidden_number_becomes_a_pattern_never_a_wrong_number(self):
        r = rc.reading_from_dict({"is_payment_screen": True, "app": "gcash", "direction": "sent", "recipient_number": "",
                                  "recipient_number_printed": "+63 9•••••5992", "recipient_name": "DE••••K C.", "amount": "1.00",
                                  "reference": "1047 370 872947", "date_time": "2026-11-17 15:35", "layout": "sent_via_gcash", "picture": "screenshot",
                                  "clarity": "clear", "cut_off": False})
        self.assertEqual((r.recipient_number, r.recipient_pattern), ("", "09?????5992"))
        self.assertEqual((r.layout, r.picture, r.clarity, r.cut_off), ("sent_via_gcash", "screenshot", "clear", False))
        self.assertEqual(r.recipient_shown, "09?????5992")

    def test_an_old_style_answer_with_only_recipient_number_still_works(self):
        r = rc.reading_from_dict({"is_payment_screen": True, "recipient_number": "+63 948 874 5992"})
        self.assertEqual((r.recipient_number, r.recipient_pattern), (SHOP, ""))
        r = rc.reading_from_dict({"is_payment_screen": True, "recipient_number": "95992"})
        self.assertEqual((r.recipient_number, r.recipient_pattern), ("", "???????5992"))

    def test_unknown_layout_picture_and_clarity_words_become_unknown(self):
        r = rc.reading_from_dict({"layout": "weird", "picture": 5, "clarity": "kind of", "cut_off": "yes"})
        self.assertEqual((r.layout, r.picture, r.clarity, r.cut_off), ("unknown", "unknown", "unknown", False))

    def test_the_new_facts_are_kept_in_the_stored_json(self):
        stored = masked().to_json()
        self.assertIn('"recipient_pattern": "09?????5992"', stored)
        self.assertIn('"clarity": "clear"', stored)


# ---- the rules with a hidden number ---------------------------------------------------------------------------------------------------
class HiddenNumberRuleTests(SimpleTestCase):
    def test_the_hidden_number_that_ends_like_the_shops_passes_with_a_note(self):
        verdict = judge(masked())
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_PASS, []))
        self.assertEqual(verdict.recipient, "09?????5992")
        self.assertTrue(any("09•••••5992" in n and "name matches" in n for n in verdict.notes), verdict.notes)

    def test_only_the_end_showing_is_enough_when_the_name_matches(self):
        self.assertEqual(judge(masked(recipient_pattern="???????5992")).status, rc.STATUS_PASS)

    def test_a_hidden_number_with_no_name_goes_to_the_staff(self):
        verdict = judge(masked(recipient_name=""), shop_name="DE****K C.")
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["number_hidden"]))
        self.assertTrue(any("matched on the digits that show only" in n for n in verdict.notes))

    def test_a_hidden_number_with_another_name_goes_to_the_staff(self):
        verdict = judge(masked(recipient_name="MA••••A L."))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["name_differs"]))

    def test_a_hidden_number_that_ends_differently_is_someone_elses(self):
        verdict = judge(masked(recipient_pattern="09?????1234"))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["wrong_recipient"]))
        self.assertIn("not sent to Arabela's GCash number", verdict.message)
        self.assertIn("09•••••1234", verdict.message)                  # the customer is told what was read

    def test_the_refusal_of_a_full_number_says_what_was_read_too(self):
        verdict = judge(reading(recipient_number="09265411484"))
        self.assertIn("The receipt shows 09265411484.", verdict.message)

    def test_too_few_visible_digits_say_nothing_either_way(self):
        verdict = judge(masked(recipient_pattern="??????????2"), shop_name="DE****K C.")
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["recipient_not_visible"]))

    def test_a_hidden_number_matching_the_old_shop_number_inside_its_grace_passes_with_the_old_number_note(self):
        accepted = [(SHOP, False), ("09171234567", True)]
        verdict = judge(masked(recipient_pattern="09?????4567"), accepted=accepted)
        self.assertEqual(verdict.status, rc.STATUS_PASS)
        self.assertTrue(verdict.old_number)

    def test_a_hidden_number_matching_an_old_number_after_its_grace_is_refused_as_old(self):
        verdict = judge(masked(recipient_pattern="09?????4567"), accepted=[(SHOP, False)], known=[SHOP, "09171234567"])
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["old_number"]))

    def test_a_shop_with_no_number_saved_never_refuses_a_hidden_number(self):
        verdict = judge(masked(), accepted=[], known=[])
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["shop_number_not_set"]))

    def test_the_hidden_number_is_part_of_the_same_payment_fingerprint(self):
        verdict = judge(masked(), fingerprint_used_by="RSV-2026-0009")
        self.assertIn("same_amount_and_time", verdict.codes)
        self.assertIn("09?????5992", verdict.fingerprint)


class RetakeRuleTests(SimpleTestCase):
    def test_a_picture_the_reader_cannot_read_is_a_retake(self):
        verdict = judge(reading(clarity="unreadable", reference="", recipient_number="", amount=None))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_RETAKE, ["blurry"]))
        self.assertIn("couldn't read this picture clearly", verdict.message)
        self.assertFalse(verdict.rejected)
        self.assertTrue(verdict.retake)

    def test_a_blurry_picture_with_no_reference_and_no_number_is_a_retake(self):
        verdict = judge(reading(clarity="blurry", reference="", recipient_number=""))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_RETAKE, ["blurry"]))

    def test_a_blurry_picture_that_still_shows_the_reference_is_judged_normally(self):
        self.assertEqual(judge(reading(clarity="blurry")).status, rc.STATUS_PASS)

    def test_a_blurry_picture_that_does_not_look_like_a_payment_is_a_retake_not_a_refusal(self):
        verdict = judge(reading(is_payment_screen=False, clarity="blurry"))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_RETAKE, ["blurry"]))

    def test_a_clear_picture_that_is_not_a_payment_is_still_refused(self):
        verdict = judge(reading(is_payment_screen=False, clarity="clear"))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["not_payment"]))

    def test_a_missing_reference_is_a_retake_that_says_which_part_to_include(self):
        verdict = judge(reading(reference=""))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_RETAKE, ["no_reference"]))
        self.assertIn("Ref No.", verdict.message)
        cut = judge(reading(reference="", cut_off=True))
        self.assertEqual((cut.status, cut.codes), (rc.STATUS_RETAKE, ["cut_off"]))
        self.assertIn("Part of this receipt is missing", cut.message)


# ---- the second reading: what our own code makes of the copied lines ------------------------------------------------------------------
def lines(*text):
    return {"lines": list(text), "picture": "screenshot", "clarity": "clear", "cut_off": False}


class TranscriptParsingTests(SimpleTestCase):
    """The lines are what the reader copies from each REAL receipt the owner sent."""

    def test_the_white_card_with_the_number_hidden(self):
        r = rc.reading_from_transcript(lines("DE••••K C.", "+63 9•••••5992", "Sent via GCash", "Amount", "1.00", "Total Amount Sent", "₱1.00",
                                             "Ref No. 1047 370 872947", "Nov 17, 2026 3:35 PM", "279g (gCO2e)",
                                             "By going digital, you reduce your carbon footprint from transportation, paper, and plastic."))
        self.assertEqual((r.app, r.direction, r.layout), ("gcash", "sent", "sent_via_gcash"))
        self.assertEqual((r.recipient_number, r.recipient_pattern, r.recipient_name), ("", "09?????5992", "DE••••K C."))
        self.assertEqual((r.amount, r.reference), (Decimal("1.00"), "1047370872947"))
        self.assertEqual(r.paid_at, timezone.make_aware(datetime(2026, 11, 17, 15, 35)))
        self.assertTrue(r.is_payment_screen)
        self.assertFalse(r.is_transaction_list)

    def test_the_white_card_with_the_full_number(self):
        r = rc.reading_from_transcript(lines("DE••••K C.", "+63 948 874 5992", "Sent via GCash", "Amount", "600.00", "Total Amount Sent", "₱600.00",
                                             "Ref No.", "9046 631 869248", "Oct 28, 2026 8:14 PM"))
        self.assertEqual((r.recipient_number, r.amount, r.reference), (SHOP, Decimal("600.00"), "9046631869248"))
        self.assertEqual(r.paid_at, timezone.make_aware(datetime(2026, 10, 28, 20, 14)))

    def test_a_camera_photo_of_the_express_send_screen_with_its_buttons(self):
        r = rc.reading_from_transcript(lines("Express Send", "DE••••K C.", "+63 948 874 5992", "Sent via GCash", "Amount", "1,000.00",
                                             "Total Amount Sent", "₱1,000.00", "Ref No. 3047 307 194512 Nov 15, 2026 9:03 PM", "Download", "Share"))
        self.assertEqual((r.layout, r.recipient_number, r.amount, r.reference), ("express_send", SHOP, Decimal("1000.00"), "3047307194512"))
        self.assertEqual(r.paid_at, timezone.make_aware(datetime(2026, 11, 15, 21, 3)))

    def test_the_history_page_for_money_sent(self):
        r = rc.reading_from_transcript(lines("Transaction Details", "Transfer from 09488745992", "to 09167957938", "Amount", "-500.00",
                                             "Date & Time", "Nov 5, 2026 11:52 PM", "Reference Number", "5046933479702"))
        self.assertEqual((r.layout, r.direction, r.recipient_number, r.sender_number), ("transaction_details", "sent", "09167957938", SHOP))
        self.assertEqual((r.amount, r.reference), (Decimal("500.00"), "5046933479702"))
        self.assertEqual(r.paid_at, timezone.make_aware(datetime(2026, 11, 5, 23, 52)))

    def test_the_history_page_for_money_received(self):
        r = rc.reading_from_transcript(lines("Transaction Details", "Transfer from 09912332372 to 09488745992", "Amount", "+600.00",
                                             "Date & Time", "Nov 11, 2026 5:23 PM", "Reference Number", "9047141636417", "Have questions? Get Help"))
        self.assertEqual((r.direction, r.recipient_number, r.amount), ("received", SHOP, Decimal("600.00")))
        self.assertEqual(r.paid_at, timezone.make_aware(datetime(2026, 11, 11, 17, 23)))

    def test_twelve_hour_times(self):
        for text, hour, minute in (("Oct 31, 2026 10:20 AM", 10, 20), ("Oct 31, 2026 12:05 AM", 0, 5), ("Oct 31, 2026 12:30 PM", 12, 30),
                                   ("Oct 31, 2026 11:59 PM", 23, 59), ("Oct 31, 2026 22:15", 22, 15)):
            with self.subTest(text=text):
                r = rc.reading_from_transcript(lines("Ref No. 1046 242 390663", text, "Total Amount Sent ₱5.00"))
                self.assertEqual(r.paid_at, timezone.make_aware(datetime(2026, 10, 31, hour, minute)))

    def test_a_maya_receipt_and_a_bank_receipt_are_not_gcash(self):
        maya = rc.reading_from_transcript(lines("maya", "Sent money to", "09105950144", "₱87.00", "Reference ID 1234567890123", "Nov 7, 2026 1:13 PM"))
        self.assertEqual((maya.app, maya.amount), ("maya", Decimal("87.00")))
        self.assertTrue(maya.is_payment_screen)
        bank = rc.reading_from_transcript(lines("BDO", "InstaPay transfer", "₱500.00", "Reference No. 1234567890123", "Nov 7, 2026 1:13 PM"))
        self.assertEqual(bank.app, "bank")

    def test_a_payment_to_a_store(self):
        r = rc.reading_from_transcript(lines("Payment to SM Supermalls", "Total Amount Sent ₱300.00", "Ref No. 1234 567 890123", "Nov 7, 2026 1:13 PM"))
        self.assertEqual(r.direction, "merchant_payment")

    def test_a_list_of_transactions(self):
        r = rc.reading_from_transcript(lines("Transactions", "Express Send Nov 7, 2026 1:13 PM -100.00", "Express Send Nov 6, 2026 9:00 AM -200.00",
                                             "Express Send Nov 5, 2026 8:00 AM -300.00"))
        self.assertTrue(r.is_transaction_list)
        self.assertFalse(r.is_payment_screen)

    def test_a_picture_that_is_not_a_receipt(self):
        r = rc.reading_from_transcript(lines("Sunset at the beach"))
        self.assertFalse(r.is_payment_screen)
        self.assertEqual((r.app, r.reference, r.amount, r.paid_at), ("unknown", "", None, None))

    def test_a_failed_payment(self):
        r = rc.reading_from_transcript(lines("Transaction failed", "Total Amount Sent ₱5.00", "Ref No. 1046 242 390663", "Nov 7, 2026 1:13 PM"))
        self.assertEqual(r.status, "failed")

    def test_the_names_gcash_hides_in_different_ways(self):
        for name in ("DE••••K C.", "JO•E MI•••L T.", "MA****A L."):
            with self.subTest(name=name):
                r = rc.reading_from_transcript(lines(name, "+63 948 874 5992", "Sent via GCash", "Total Amount Sent ₱5.00", "Ref No. 1046 242 390663"))
                self.assertEqual(r.recipient_name, name)

    def test_a_reference_longer_than_thirteen_digits_is_cut_to_thirteen(self):
        r = rc.reading_from_transcript(lines("Ref No. 1046 242 390663 2026", "Total Amount Sent ₱5.00"))
        self.assertEqual(r.reference, "1046242390663")

    def test_the_readers_own_view_of_the_picture_is_kept(self):
        r = rc.reading_from_transcript({"lines": ["Ref No. 1046 242 390663"], "picture": "photo_of_screen", "clarity": "blurry", "cut_off": True})
        self.assertEqual((r.picture, r.clarity, r.cut_off), ("photo_of_screen", "blurry", True))

    def test_an_answer_that_is_not_a_list_of_lines_is_no_answer_and_never_raises(self):
        for data in (None, "text", [], {}, {"lines": "x"}, {"lines": None}, {"picture": "screenshot"}):
            with self.subTest(data=data):
                self.assertIsNone(rc.reading_from_transcript(data))
        self.assertIsNotNone(rc.reading_from_transcript({"lines": [12345, "Ref No. 1046 242 390663"]}))

    def test_a_picture_with_no_text_at_all_is_an_answer_it_is_not_a_receipt(self):
        for data in ({"lines": []}, {"lines": [""]}, {"lines": [None, {}, []]}):
            with self.subTest(data=data):
                r = rc.reading_from_transcript(data)
                self.assertFalse(r.is_payment_screen)
                self.assertEqual(judge(r).codes, ["not_payment"])          # refused (when the first reading agrees), never "could not be read"
        r = rc.reading_from_transcript({"lines": [], "clarity": "unreadable"})
        self.assertEqual(judge(r).status, rc.STATUS_RETAKE)

    def test_what_the_second_reading_finds_is_judged_by_the_same_rules(self):
        r = rc.reading_from_transcript(lines("DE••••K C.", "+63 9•••••5992", "Sent via GCash", "Total Amount Sent ₱2,000.00",
                                             "Ref No. 1047 370 872947", "Nov 12, 2026 2:55 PM"))
        self.assertEqual(judge(r).status, rc.STATUS_PASS)


# ---- the picture's sharpness ----------------------------------------------------------------------------------------------------------
def receipt_picture(size=(900, 1500)):
    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    y = 60
    for text in ["Sent via GCash", "Amount 1.00", "Total Amount Sent 1.00", "Ref No. 1047 370 872947", "Nov 17, 2026 3:35 PM", "+63 9xxxxx5992", "DE....K C."] * 3:
        draw.text((60, y), text, fill=(20, 40, 120))
        draw.text((61, y), text, fill=(20, 40, 120))
        y += 60
    return image


def png(image):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


class PictureQualityTests(SimpleTestCase):
    def test_a_crisp_screenshot_is_sharp(self):
        result = receipt_quality.picture_quality(png(receipt_picture()))
        self.assertEqual((result["label"], result["poor"]), ("sharp", False))
        self.assertEqual((result["width"], result["height"]), (900, 1500))

    def test_blurring_lowers_the_measure_step_by_step_until_it_is_blurry(self):
        crisp = receipt_picture()
        measured = [receipt_quality.picture_quality(png(crisp.filter(ImageFilter.GaussianBlur(radius))))["sharpness"] for radius in (0.001, 1, 2, 3, 5)]
        self.assertEqual(measured, sorted(measured, reverse=True))
        heavy = receipt_quality.picture_quality(png(crisp.filter(ImageFilter.GaussianBlur(5))))
        self.assertEqual((heavy["label"], heavy["poor"]), ("blurry", True))

    def test_a_tiny_picture_is_poor_even_when_it_is_sharp(self):
        result = receipt_quality.picture_quality(png(receipt_picture().resize((200, 333))))
        self.assertTrue(result["tiny"])
        self.assertTrue(result["poor"])

    def test_a_dark_picture_is_noted_but_not_poor_by_itself(self):
        dark = receipt_picture().point(lambda value: int(value * 0.15))
        result = receipt_quality.picture_quality(png(dark))
        self.assertTrue(result["dark"])
        self.assertFalse(result["poor"])

    def test_the_background_colour_does_not_change_the_measure(self):
        white = receipt_quality.picture_quality(png(receipt_picture()))["sharpness"]
        blue = receipt_picture()
        blue = Image.eval(blue, lambda value: min(255, value // 2 + 30))
        self.assertGreater(receipt_quality.picture_quality(png(blue))["sharpness"], 100)
        self.assertGreater(white, 100)
        flat = receipt_quality.picture_quality(png(Image.new("RGB", (900, 1500), (30, 90, 240))))
        self.assertEqual(flat["label"], "blurry")            # a flat colour has no edges at all, whatever its colour

    def test_something_that_is_not_a_picture_is_unknown_and_never_raises(self):
        result = receipt_quality.picture_quality(b"this is not a picture")
        self.assertEqual((result["label"], result["poor"]), ("unknown", False))


# ---- the whole check: refusals are verified ------------------------------------------------------------------------------------------
class VerifiedRefusalTests(TestCase):
    """check_receipt with a stand-in for both readings and for the picture's sharpness."""

    @classmethod
    def setUpTestData(cls):
        shop = SiteSettings.load()
        shop.gcash_number = SHOP
        shop.gcash_account_name = "Denmark Concepcion"
        shop.save()

    def run_check(self, first, second="same", *, quality=SHARP, second_why="timeout"):
        """`first` / `second` are what the two readings see (None = not available). Returns the Verdict."""
        value = first if isinstance(second, str) and second == "same" else second
        with patch("gowns.receipt_check.read_receipt", return_value=(first, "" if first is not None else "timeout")) as read, \
                patch("gowns.receipt_check.verify_read", return_value=(value, "") if value is not None else (None, second_why)) as verify, \
                patch("gowns.receipt_quality.picture_quality", return_value=quality):
            self.read, self.verify = read, verify
            return rc.check_receipt(b"picture", "receipt.png", expected_amount=Decimal("2000"), started_at=None, now=NOW)

    def fresh(self, **changes):
        return reading(paid_at=at(minutes=5), **changes)

    def test_a_good_receipt_is_read_once_and_never_double_checked(self):
        verdict = self.run_check(self.fresh())
        self.assertEqual(verdict.status, rc.STATUS_PASS)
        self.verify.assert_not_called()

    def test_a_receipt_that_needs_a_look_is_not_double_checked_either(self):
        verdict = self.run_check(self.fresh(amount=Decimal("100")))
        self.assertEqual(verdict.status, rc.STATUS_NEEDS_CHECK)
        self.verify.assert_not_called()

    def test_a_refusal_both_readings_give_on_a_sharp_picture_stands(self):
        verdict = self.run_check(self.fresh(recipient_number="09265411484"))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["wrong_recipient"]))
        self.verify.assert_called_once()

    def test_a_refusal_the_second_reading_does_not_share_is_sent_to_the_staff_instead(self):
        verdict = self.run_check(self.fresh(recipient_number="09265411484"), self.fresh())
        self.assertEqual(verdict.status, rc.STATUS_NEEDS_CHECK)
        self.assertIn("readings_disagree", verdict.codes)
        self.assertTrue(any("disagreed" in n for n in verdict.notes))
        self.assertEqual(verdict.message, "")
        self.assertEqual(verdict.reference, "5046933479702")            # the staff still get the facts the second reading found

    def test_two_readings_that_find_different_problems_do_not_refuse_either(self):
        verdict = self.run_check(self.fresh(recipient_number="09265411484"), self.fresh(app="maya"))
        self.assertEqual(verdict.status, rc.STATUS_NEEDS_CHECK)
        self.assertEqual(verdict.codes, ["readings_disagree"])
        self.assertEqual(verdict.reference, "5046933479702")

    def test_a_second_reading_that_could_not_be_made_never_refuses(self):
        verdict = self.run_check(self.fresh(recipient_number="09265411484"), None)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["second_read_unavailable"]))
        self.assertTrue(any("second reading could not be made (timeout)" in n for n in verdict.notes))
        self.assertEqual(verdict.reference, "5046933479702")

    def test_a_refusal_on_a_blurry_picture_becomes_a_retake(self):
        verdict = self.run_check(self.fresh(recipient_number="09265411484"), quality=POOR)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_RETAKE, ["blurry"]))
        self.assertIn("couldn't read this picture clearly", verdict.message)

    def test_a_refusal_that_depends_on_digits_needs_a_sharp_picture(self):
        for changes in ({"recipient_number": "09265411484"}, {"paid_at": NOW - timedelta(days=60)}):
            with self.subTest(changes=changes):
                verdict = self.run_check(reading(**changes), quality=SOFT)
                self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["picture_not_sharp"]))
                self.assertTrue(any("not sharp enough" in n for n in verdict.notes))

    def test_a_picture_both_readings_call_clear_and_no_receipt_is_refused_even_when_it_has_few_edges(self):
        # a flat photo (a gown, a wall) measures as "blurry" without being unreadable; it is still no payment receipt
        for code_reading in (self.fresh(is_payment_screen=False, clarity="clear"), self.fresh(is_payment_screen=False, is_transaction_list=True)):
            verdict = self.run_check(code_reading, quality=POOR)
            self.assertEqual(verdict.status, rc.STATUS_REJECT, verdict.codes)

    def test_a_picture_with_nothing_to_read_is_no_receipt_unless_the_picture_itself_is_blurry(self):
        nothing = rc.reading_from_transcript({"lines": [], "picture": "screenshot", "clarity": "unreadable", "cut_off": True})
        first = self.fresh(is_payment_screen=False, clarity="unknown")
        for quality in (SHARP, SOFT):
            verdict = self.run_check(first, nothing, quality=quality)
            self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["not_payment"]))
        blurry = self.run_check(first, nothing, quality=POOR)                  # a blurred receipt reads as "nothing" too: ask for another picture
        self.assertEqual((blurry.status, blurry.codes), (rc.STATUS_RETAKE, ["blurry"]))

    def test_a_refusal_that_rests_on_small_words_needs_a_picture_that_is_not_blurry(self):
        for changes in ({"app": "maya"}, {"direction": "received"}, {"direction": "merchant_payment"}, {"status": "failed"}):
            with self.subTest(changes=changes):
                verdict = self.run_check(self.fresh(**changes), quality=POOR)
                self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_RETAKE, ["blurry"]))

    def test_a_refusal_that_does_not_depend_on_digits_stands_on_a_soft_picture(self):
        verdict = self.run_check(self.fresh(app="maya"), quality=SOFT)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["not_gcash"]))

    def test_a_second_reading_that_cannot_read_the_picture_turns_the_refusal_into_a_retake(self):
        verdict = self.run_check(self.fresh(recipient_number="09265411484"), self.fresh(clarity="unreadable", reference="", recipient_number=""))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_RETAKE, ["blurry"]))

    def test_a_first_reading_that_could_not_read_is_rescued_by_the_second(self):
        verdict = self.run_check(self.fresh(reference="", clarity="blurry"), self.fresh())
        self.assertEqual(verdict.status, rc.STATUS_PASS)
        self.assertEqual(verdict.reference, "5046933479702")
        self.assertTrue(any("first reading could not read" in n for n in verdict.notes))

    def test_a_picture_neither_reading_can_read_is_a_retake(self):
        unreadable = self.fresh(reference="", clarity="unreadable", recipient_number="")
        self.assertEqual(self.run_check(unreadable).status, rc.STATUS_RETAKE)
        self.assertEqual(self.run_check(unreadable, None).status, rc.STATUS_RETAKE)
        self.assertEqual(self.run_check(unreadable, self.fresh(recipient_number="09265411484")).status, rc.STATUS_RETAKE)

    def test_a_reader_that_is_down_never_blocks_and_is_not_double_checked(self):
        verdict = self.run_check(None)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["reader_unavailable"]))
        self.verify.assert_not_called()

    def test_the_picture_already_used_is_refused_without_reading_anything(self):
        from reservations.models import Reservation
        with patch.object(Reservation.objects, "filter") as lookup:
            lookup.return_value.values_list.return_value.first.return_value = "RSV-2026-0001"
            with patch("gowns.receipt_check.read_receipt") as read, patch("gowns.receipt_check.verify_read") as verify:
                verdict = rc.check_receipt(b"picture", "receipt.png", expected_amount=Decimal("2000"), now=NOW)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["image_used"]))
        read.assert_not_called()
        verify.assert_not_called()

    def test_a_masked_receipt_passes_through_the_whole_check(self):
        verdict = self.run_check(masked(paid_at=at(minutes=5)))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_PASS, []))
        self.assertEqual(verdict.recipient, "09?????5992")
        self.verify.assert_not_called()

    def test_the_decision_is_written_to_the_log_with_both_readings(self):
        with self.assertLogs("gowns.receipt_check", level="WARNING") as logged:
            self.run_check(self.fresh(recipient_number="09265411484"))
        text = " ".join(logged.output)
        self.assertIn("first reading reject", text)
        self.assertIn("09265411484", text)
        self.assertIn("picture", text)


# ---- asking Google: the second reader must never keep the page waiting -----------------------------------------------------------------
def gemini_answer(status=200, payload=None):
    response = MagicMock()
    response.status_code = status
    body = {"candidates": [{"content": {"parts": [{"text": __import__("json").dumps(payload or {"lines": ["Ref No. 1046 242 390663", "Total Amount Sent ₱5.00"]})}]}}]}
    response.json.return_value = body
    return response


@override_settings(RECEIPT_READER_ENABLED=True, GEMINI_API_KEY="test-key", GEMINI_MODEL="main-lite", GEMINI_FALLBACK_MODEL="backup-lite", GEMINI_VERIFY_MODEL="")
class AskingTheReaderTests(SimpleTestCase):
    def test_the_second_reading_uses_a_different_model_than_the_first(self):
        self.assertEqual(rc._models(), ["main-lite", "backup-lite"])
        self.assertEqual(rc._verify_models(), ["backup-lite", "main-lite"])

    @override_settings(GEMINI_VERIFY_MODEL="strong-flash")
    def test_a_model_chosen_for_the_second_reading_goes_first(self):
        self.assertEqual(rc._verify_models(), ["strong-flash", "backup-lite", "main-lite"])

    @override_settings(GEMINI_FALLBACK_MODEL="main-lite")
    def test_the_same_model_is_never_listed_twice(self):
        self.assertEqual(rc._verify_models(), ["main-lite"])

    def test_a_slow_chosen_model_gets_one_short_try_and_the_next_model_answers(self):
        calls = []

        def post(url, **kwargs):
            calls.append((url.split("/models/")[1].split(":")[0], kwargs["timeout"]))
            if "strong-flash" in url:
                raise requests.Timeout()
            return gemini_answer()

        with override_settings(GEMINI_VERIFY_MODEL="strong-flash"), patch("gowns.receipt_check.requests.post", side_effect=post):
            result, why = rc.verify_read(b"x", "image/png", started=time.monotonic())
        self.assertEqual(why, "")
        self.assertEqual(result.reference, "1046242390663")
        self.assertEqual([name for name, _ in calls], ["strong-flash", "backup-lite"])
        self.assertLessEqual(calls[0][1], rc.SECOND_READ_STRONGER_MODEL_SECONDS)       # the stronger model was never waited for longer than this

    def test_a_busy_chosen_model_is_not_retried(self):
        calls = []

        def post(url, **kwargs):
            calls.append(url.split("/models/")[1].split(":")[0])
            return gemini_answer(503) if "strong-flash" in url else gemini_answer()

        with override_settings(GEMINI_VERIFY_MODEL="strong-flash"), patch("gowns.receipt_check.requests.post", side_effect=post), \
                patch("gowns.receipt_check.time.sleep"):
            result, _ = rc.verify_read(b"x", "image/png", started=time.monotonic())
        self.assertIsNotNone(result)
        self.assertEqual(calls, ["strong-flash", "backup-lite"])

    def test_the_second_reading_is_skipped_when_the_first_already_took_long(self):
        with patch("gowns.receipt_check.requests.post") as post:
            result, why = rc.verify_read(b"x", "image/png", started=time.monotonic() - 40)
        self.assertEqual((result, why), (None, "no_time"))
        post.assert_not_called()

    def test_the_first_reading_still_gives_up_on_a_timeout_as_before(self):
        with patch("gowns.receipt_check.requests.post", side_effect=requests.Timeout()) as post:
            result, why = rc.read_receipt(b"x", "image/png")
        self.assertEqual((result, why), (None, "timeout"))
        self.assertEqual(post.call_count, 1)

    def test_nothing_is_asked_when_the_reader_is_off_or_has_no_key(self):
        with override_settings(RECEIPT_READER_ENABLED=False), patch("gowns.receipt_check.requests.post") as post:
            self.assertEqual(rc.verify_read(b"x", "image/png"), (None, "disabled"))
            self.assertEqual(rc.read_receipt(b"x", "image/png"), (None, "disabled"))
        with override_settings(GEMINI_API_KEY=""), patch("gowns.receipt_check.requests.post") as post:
            self.assertEqual(rc.verify_read(b"x", "image/png"), (None, "not_configured"))
        post.assert_not_called()

    def test_an_answer_that_is_not_json_moves_on_to_the_next_model(self):
        garbage = MagicMock()
        garbage.status_code = 200
        garbage.json.return_value = {"candidates": [{"content": {"parts": [{"text": "I cannot help with that"}]}}]}
        with patch("gowns.receipt_check.requests.post", side_effect=[garbage, gemini_answer()]) as post:
            result, why = rc.verify_read(b"x", "image/png", started=time.monotonic())
        self.assertEqual(why, "")
        self.assertIsNotNone(result)
        self.assertEqual(post.call_count, 2)

    def test_the_two_prompts_ask_for_what_the_rules_need(self):
        for key in ("recipient_number_printed", "layout", "picture", "clarity", "cut_off", "reference", "date_time"):
            self.assertIn(f'"{key}"', rc.PROMPT)
        self.assertIn('"lines"', rc.PROMPT_TRANSCRIBE)
        self.assertIn("Never fill in", rc.PROMPT_TRANSCRIBE)


# ---- the developer command -------------------------------------------------------------------------------------------------------------
class CheckReceiptsCommandTests(TestCase):
    def folder(self):
        import tempfile
        from pathlib import Path
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [p.unlink() for p in folder.iterdir()] and folder.rmdir() or None)
        for name in ("masked.png", "notes.txt"):
            (folder / name).write_bytes(png(receipt_picture((600, 1000))) if name.endswith(".png") else b"not a picture")
        return folder

    def test_it_runs_each_picture_through_the_check_and_saves_nothing(self):
        folder = self.folder()
        out = io.StringIO()
        seen = masked(paid_at=at(minutes=5))
        before = SiteSettings.objects.count()
        with patch("gowns.receipt_check.read_receipt", return_value=(seen, "")),                 patch("gowns.receipt_check.verify_read", return_value=(None, "disabled")) as verify:
            call_command("check_receipts", str(folder), "--shop-number", SHOP, "--shop-name", "DE****K C.", "--deposit", "2000", "--fresh",
                         "--read-live", stdout=out)
        text = out.getvalue()
        self.assertIn("=== masked.png", text)
        self.assertNotIn("notes.txt", text)
        self.assertIn("VERDICT: PASS", text)
        self.assertIn("pattern=09?????5992", text)
        verify.assert_not_called()
        self.assertEqual(SiteSettings.objects.count(), before)          # the shop settings it pretended with were never saved

    def test_it_refuses_a_bad_shop_number_and_a_missing_folder(self):
        with self.assertRaises(CommandError):
            call_command("check_receipts", str(self.folder()), "--shop-number", "12345", "--read-live")
        with self.assertRaises(CommandError):
            call_command("check_receipts", "no/such/folder", "--read-live")
