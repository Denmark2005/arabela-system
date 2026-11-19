"""Receipt check, Phase 1 (2026-11-19): a clearly old receipt is refused on ONE sharp reading; a refusal that could not be confirmed in time asks the
customer to try again (and goes to the staff on the second try); the first reading of a picture is kept for that second try; and the quota guard
(per account and per internet address) stops anybody from using up the shop's free Gemini allowance.

The reader (Google Gemini) is never called here: every reading is a stand-in, so these tests spend none of the shop's quota."""
import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import rate_limit
from accounts.models import LoginThrottle
from accounts.test_support import agreed
from gowns import receipt_check as rc
from gowns import receipt_tries
from gowns.models import SiteSettings
from gowns.sample_images import unique_jpeg
from gowns.test_receipt_checkout import ReceiptCheckBase, good_reading
from gowns.test_receipt_rules import NOW, SHOP, at, reading
from gowns.test_receipt_verification import POOR, SHARP, SOFT
from reservations.models import ProofRejection, Reservation

User = get_user_model()
FIVE_DAYS = timedelta(days=5)


def old(**changes):
    return reading(paid_at=NOW - FIVE_DAYS, **changes)


class CheckBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        shop = SiteSettings.load()
        shop.gcash_number = SHOP
        shop.gcash_account_name = "Denmark Concepcion"
        shop.save()

    def run_check(self, first, second=None, *, quality=SHARP, **flags):
        verify_value = (second, "") if second is not None else (None, "timeout")
        with patch("gowns.receipt_check.read_receipt", return_value=(first, "" if first is not None else "timeout")) as read, \
                patch("gowns.receipt_check.verify_read", return_value=verify_value) as verify, \
                patch("gowns.receipt_quality.picture_quality", return_value=quality):
            self.read, self.verify = read, verify
            return rc.check_receipt(b"picture", "receipt.png", expected_amount=Decimal("2000"), now=NOW, **flags)


# ---- 1. a clearly old receipt needs one reading ----------------------------------------------------------------------------------------
class AClearlyOldReceiptIsRefusedOnOneReadingTests(CheckBase):
    def test_a_receipt_five_days_old_on_a_sharp_picture_is_refused_without_a_second_reading(self):
        verdict = self.run_check(old(), second=None, single_old_ok=True)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["too_old"]))
        self.assertTrue(verdict.single_reading)
        self.verify.assert_not_called()                                           # no second reading, so no extra wait
        self.assertEqual(verdict.message, "This receipt is from Nov 7, 2026. Please upload the receipt of today's payment.")

    def test_the_facts_found_are_kept_for_the_log(self):
        verdict = self.run_check(old(), single_old_ok=True)
        self.assertEqual(verdict.reference, "5046933479702")
        self.assertEqual(len(verdict.image_hash), 64)

    def test_without_the_switch_the_older_two_reading_rule_still_applies(self):
        verdict = self.run_check(old(), second=old())
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["too_old"]))
        self.assertFalse(verdict.single_reading)
        self.verify.assert_called_once()

    def test_three_days_is_the_line(self):
        just_under = reading(paid_at=NOW - timedelta(days=3) + timedelta(minutes=1))
        just_over = reading(paid_at=NOW - timedelta(days=3) - timedelta(minutes=1))
        under = self.run_check(just_under, second=just_under, single_old_ok=True)
        self.assertFalse(under.single_reading)
        self.verify.assert_called_once()                                          # under three days: two readings must still agree
        over = self.run_check(just_over, second=None, single_old_ok=True)
        self.assertTrue(over.single_reading)
        self.verify.assert_not_called()

    def test_a_receipt_a_day_and_a_half_old_still_needs_two_readings(self):
        one_and_a_half = reading(paid_at=NOW - timedelta(hours=36))
        verdict = self.run_check(one_and_a_half, second=one_and_a_half, single_old_ok=True)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["too_old"]))
        self.assertFalse(verdict.single_reading)
        self.verify.assert_called_once()

    def test_a_soft_picture_never_refuses_on_one_reading(self):
        verdict = self.run_check(old(), second=old(), quality=SOFT, single_old_ok=True)
        self.assertFalse(verdict.single_reading)
        self.verify.assert_called_once()
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["picture_not_sharp"]))        # the older rule: staff look

    def test_a_blurry_picture_is_a_retake_not_a_refusal(self):
        verdict = self.run_check(old(), second=old(), quality=POOR, single_old_ok=True)
        self.assertFalse(verdict.single_reading)
        self.assertNotEqual(verdict.status, rc.STATUS_REJECT)

    def test_a_receipt_with_no_date_found_is_never_refused_this_way(self):
        verdict = self.run_check(reading(paid_at=None), single_old_ok=True)
        self.assertFalse(verdict.single_reading)
        self.assertNotEqual(verdict.codes[:1], ["too_old"])

    def test_no_other_refusal_gets_the_shortcut(self):
        for changes in ({"recipient_number": "09265411484"}, {"app": "maya"}, {"direction": "received"}, {"is_payment_screen": False}):
            with self.subTest(changes=changes):
                verdict = self.run_check(reading(**changes), second=reading(**changes), single_old_ok=True)
                self.assertFalse(verdict.single_reading)
                self.verify.assert_called_once()

    def test_a_fresh_receipt_is_untouched(self):
        verdict = self.run_check(reading(), single_old_ok=True)
        self.assertEqual(verdict.status, rc.STATUS_PASS)
        self.verify.assert_not_called()                                           # one reading, as before

    def test_an_old_receipt_to_somebody_else_is_still_refused_for_the_wrong_recipient_first(self):
        verdict = self.run_check(old(recipient_number="09265411484"), second=old(recipient_number="09265411484"), single_old_ok=True)
        self.assertEqual(verdict.codes, ["wrong_recipient"])


# ---- 2. ask again instead of booking -----------------------------------------------------------------------------------------------------
class AskAgainInsteadOfBookingTests(CheckBase):
    WRONG = {"recipient_number": "09265411484"}                                   # the reader would refuse this receipt

    def test_a_refusal_the_second_reading_could_not_confirm_asks_to_try_again(self):
        first = reading(**self.WRONG)
        verdict = self.run_check(first, second=None, ask_again_when_unsure=True)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_TRY_AGAIN, ["check_unfinished"]))
        self.assertTrue(verdict.try_again)
        self.assertFalse(verdict.rejected)
        self.assertEqual(verdict.message, "We couldn't check your receipt just now. Please try again in a moment.")
        self.assertIs(verdict.reading, first)                                       # kept for the second try

    def test_on_the_final_try_it_goes_to_the_staff_instead(self):
        verdict = self.run_check(reading(**self.WRONG), second=None, ask_again_when_unsure=True, final_try=True)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["second_read_unavailable"]))
        self.assertIn("second reading could not be made", " ".join(verdict.notes))

    def test_the_older_behaviour_is_the_default(self):
        verdict = self.run_check(reading(**self.WRONG), second=None)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["second_read_unavailable"]))

    def test_it_only_applies_when_the_first_reading_would_have_refused(self):
        low = reading(amount=Decimal("100"))                                         # staff must look, but nothing is refused: no second reading at all
        verdict = self.run_check(low, second=None, ask_again_when_unsure=True)
        self.assertEqual(verdict.status, rc.STATUS_NEEDS_CHECK)
        self.verify.assert_not_called()

    def test_a_picture_to_take_again_stays_a_retake(self):
        blurry = reading(reference="", clarity="blurry")
        verdict = self.run_check(blurry, second=None, ask_again_when_unsure=True)
        self.assertEqual(verdict.status, rc.STATUS_RETAKE)

    def test_an_unusual_layout_that_was_accepted_goes_to_the_staff_not_back_to_the_customer(self):
        unusual = reading(layout="other", gcash_signs=[])
        verdict = self.run_check(unusual, second=None, ask_again_when_unsure=True)
        self.assertEqual(verdict.status, rc.STATUS_NEEDS_CHECK)

    def test_two_agreeing_refusals_still_refuse(self):
        verdict = self.run_check(reading(**self.WRONG), second=reading(**self.WRONG), ask_again_when_unsure=True)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["wrong_recipient"]))

    def test_a_second_reading_that_accepts_still_sends_it_to_the_staff(self):
        verdict = self.run_check(reading(**self.WRONG), second=reading(), ask_again_when_unsure=True)
        self.assertEqual(verdict.status, rc.STATUS_NEEDS_CHECK)
        self.assertIn("readings_disagree", verdict.codes)

    def test_the_reader_being_down_never_asks_the_customer_to_try_again(self):
        verdict = self.run_check(None, second=None, ask_again_when_unsure=True)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["reader_unavailable"]))


class TheFirstReadingIsKeptForTheSecondTryTests(CheckBase):
    def test_a_kept_first_reading_is_not_read_again_and_the_second_reading_gets_its_own_time(self):
        kept = reading(recipient_number="09265411484")
        verdict = self.run_check(None, second=kept, prior_first_reading=kept, ask_again_when_unsure=True, final_try=True)
        self.read.assert_not_called()                                              # the first reading is not asked for again
        self.verify.assert_called_once()
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["wrong_recipient"]))

    def test_the_kept_reading_is_judged_by_todays_rules_not_the_old_verdict(self):
        kept = reading(amount=Decimal("100"))
        verdict = self.run_check(None, prior_first_reading=kept)
        self.assertEqual(verdict.status, rc.STATUS_NEEDS_CHECK)                    # the deposit is 2,000: judged again now
        self.assertIn("amount_low", verdict.codes)

    def test_a_picture_already_used_on_another_booking_is_still_refused_without_reading(self):
        Reservation.objects.create(customer=None, customer_name="X", proof_hash=hashlib.sha256(b"picture").hexdigest())
        verdict = self.run_check(None, prior_first_reading=reading())
        self.assertEqual(verdict.codes, ["image_used"])
        self.read.assert_not_called()
        self.verify.assert_not_called()


class ReadingsComeBackWholeTests(SimpleTestCase):
    def test_what_to_json_writes_comes_back_equal_date_included(self):
        original = rc.Reading(
            is_payment_screen=True, app="gcash", direction="sent", recipient_number=SHOP, recipient_name="DE****K C.", sender_number="09167957938",
            amount=Decimal("2000.50"), reference="5046933479702", paid_at=at(days=5), status="completed", gcash_signs=["Express Send", "Ref No."],
            recipient_pattern="", layout="express_send", picture="screenshot", clarity="clear", cut_off=False)
        back = rc.reading_from_stored(json.loads(original.to_json()))
        self.assertEqual(back, original)
        self.assertEqual(back.paid_at, original.paid_at)

    def test_a_reading_with_nothing_found_comes_back_too(self):
        empty = rc.Reading()
        self.assertEqual(rc.reading_from_stored(json.loads(empty.to_json())), empty)

    def test_a_hidden_number_pattern_survives(self):
        hidden = rc.Reading(recipient_pattern="09?????5992", amount=Decimal("1"), reference="1" * 13)
        self.assertEqual(rc.reading_from_stored(json.loads(hidden.to_json())).recipient_pattern, "09?????5992")

    def test_a_date_without_a_time_zone_is_made_aware(self):
        data = json.loads(reading().to_json())
        data["paid_at"] = "2026-11-07T10:20:00"
        self.assertIsNotNone(rc.reading_from_stored(data).paid_at.tzinfo)

    def test_anything_that_cannot_be_read_back_is_none(self):
        for junk in (None, "text", 5, [], {"paid_at": "not a date"}, {"amount": "abc"}, {"paid_at": 5}):
            with self.subTest(junk=junk):
                self.assertIsNone(rc.reading_from_stored(junk))


# ---- the memory between two tries --------------------------------------------------------------------------------------------------------
class ReceiptTriesTests(SimpleTestCase):
    def test_a_picture_nobody_has_tried_before_starts_fresh(self):
        self.assertEqual(receipt_tries.state({}, "h1"), {"count": 0, "old_refused": False, "first": ""})

    def test_a_try_again_counts_and_keeps_the_first_reading(self):
        session = {}
        self.assertEqual(receipt_tries.remember_try_again(session, "h1", '{"a": 1}'), 1)
        self.assertEqual(receipt_tries.state(session, "h1"), {"count": 1, "old_refused": False, "first": '{"a": 1}'})
        self.assertEqual(receipt_tries.remember_try_again(session, "h1", '{"a": 2}'), 2)
        self.assertEqual(receipt_tries.state(session, "h1")["first"], '{"a": 2}')

    def test_a_different_picture_has_its_own_story(self):
        session = {}
        receipt_tries.remember_try_again(session, "h1", "x")
        self.assertEqual(receipt_tries.state(session, "h2")["count"], 0)

    def test_an_old_refusal_is_remembered(self):
        session = {}
        receipt_tries.remember_old_refusal(session, "h1")
        self.assertTrue(receipt_tries.state(session, "h1")["old_refused"])
        self.assertEqual(receipt_tries.state(session, "h1")["count"], 0)

    def test_forgetting_clears_one_picture_only(self):
        session = {}
        receipt_tries.remember_try_again(session, "h1", "x")
        receipt_tries.remember_try_again(session, "h2", "y")
        receipt_tries.forget(session, "h1")
        self.assertEqual(receipt_tries.state(session, "h1")["count"], 0)
        self.assertEqual(receipt_tries.state(session, "h2")["count"], 1)
        receipt_tries.forget(session, "never-seen")                                  # forgetting what is not there is fine

    def test_only_the_latest_few_pictures_are_kept(self):
        session = {}
        for n in range(receipt_tries.MAX_KEPT + 3):
            receipt_tries.remember_try_again(session, f"h{n}", "x")
        self.assertEqual(len(session[receipt_tries.SESSION_KEY]), receipt_tries.MAX_KEPT)
        self.assertEqual(receipt_tries.state(session, "h0")["count"], 0)             # the oldest were dropped
        self.assertEqual(receipt_tries.state(session, f"h{receipt_tries.MAX_KEPT + 2}")["count"], 1)

    def test_the_kept_reading_is_rebuilt_with_the_builder_or_none_when_it_cannot_be(self):
        session = {}
        receipt_tries.remember_try_again(session, "h1", reading().to_json())
        back = receipt_tries.first_reading(session, "h1", rc.reading_from_stored)
        self.assertEqual(back, reading())
        self.assertIsNone(receipt_tries.first_reading(session, "nope", rc.reading_from_stored))
        receipt_tries.remember_try_again(session, "h2", "{not json")
        self.assertIsNone(receipt_tries.first_reading(session, "h2", rc.reading_from_stored))
        receipt_tries.remember_try_again(session, "h3", "")
        self.assertIsNone(receipt_tries.first_reading(session, "h3", rc.reading_from_stored))

    def test_damaged_session_data_is_ignored(self):
        for junk in ("text", 5, ["x"], None, {"h1": "text"}, {"h1": {"n": "x", "old": 1, "first": 5}}, {"h1": {"n": -4}}):
            with self.subTest(junk=junk):
                session = {receipt_tries.SESSION_KEY: junk}
                self.assertEqual(receipt_tries.state(session, "h1")["count"], 0)
                self.assertEqual(receipt_tries.remember_try_again(session, "h1", "x"), 1)


# ---- through the real checkout -----------------------------------------------------------------------------------------------------------
class TheCheckoutKeepsItsPromisesTests(ReceiptCheckBase):
    def old_reading(self, **changes):
        return good_reading(paid_at=timezone.now() - timedelta(days=5), **changes)

    # clearly old
    def test_a_receipt_five_days_old_is_refused_at_once_and_never_gets_a_second_reading(self):
        self.reads(self.old_reading())
        data = self.refused(self.checkout("a"))
        self.assertIn("This receipt is from", data["error"])
        self.assertIn("Please upload the receipt of today's payment", data["error"])
        self.verifier.assert_not_called()
        self.nothing_booked()
        self.assertEqual(ProofRejection.objects.count(), 1)                          # counted like any refused receipt

    def test_the_same_picture_sent_again_takes_the_two_reading_path(self):
        self.reads(self.old_reading())
        browser, picture = Client(), unique_jpeg()
        self.refused(self.checkout("a", client=browser, picture=picture))
        self.verifier.assert_not_called()
        again = self.refused(self.checkout("a", client=browser, picture=picture))
        self.assertIn("This receipt is from", again["error"])
        self.verifier.assert_called_once()                                             # now the second reading looked too
        self.nothing_booked()

    def test_the_same_picture_with_no_second_reading_possible_goes_to_the_staff_so_nobody_is_stuck(self):
        self.reads(self.old_reading(), second=None)
        browser, picture = Client(), unique_jpeg()
        self.refused(self.checkout("a", client=browser, picture=picture))
        reservation = self.booked(self.checkout("a", client=browser, picture=picture))
        self.assertEqual(reservation.proof_check, Reservation.ProofCheck.NEEDS_CHECK)
        self.assertIn("second reading could not be made", reservation.proof_notes)

    def test_a_different_picture_gets_the_one_reading_shortcut_again(self):
        self.reads(self.old_reading())
        browser = Client()
        self.refused(self.checkout("a", client=browser))
        self.refused(self.checkout("a", client=browser))                                # another picture (a new one every call)
        self.verifier.assert_not_called()

    # try again
    def test_try_again_keeps_the_first_reading_and_the_second_try_does_not_read_it_twice(self):
        self.reads(good_reading(recipient_number="09265411484"), second=None)
        browser, picture = Client(), unique_jpeg()
        asked = self.checkout("a", client=browser, picture=picture)
        self.assertTrue(asked.json()["proof_try_again"])
        self.assertEqual(self.reader.call_count, 1)
        reservation = self.booked(self.checkout("a", client=browser, picture=picture))
        self.assertEqual(self.reader.call_count, 1)                                    # still one: the first reading was reused
        self.assertEqual(self.verifier.call_count, 2)                                  # the second reading was tried on each try
        self.assertEqual(reservation.proof_check, Reservation.ProofCheck.NEEDS_CHECK)
        self.assertFalse(browser.session.get(receipt_tries.SESSION_KEY))             # and the memory of that picture is cleared

    def test_a_try_again_that_the_second_try_confirms_is_a_plain_refusal(self):
        self.reads(good_reading(recipient_number="09265411484"), second=None)
        browser, picture = Client(), unique_jpeg()
        self.checkout("a", client=browser, picture=picture)
        self.verifier.return_value = (good_reading(recipient_number="09265411484"), "")       # the second reading works this time
        data = self.refused(self.checkout("a", client=browser, picture=picture))
        self.assertIn("not sent to Arabela", data["error"])
        self.assertEqual(self.reader.call_count, 1)
        self.assertEqual(ProofRejection.objects.count(), 1)

    def test_a_different_picture_after_a_try_again_starts_fresh(self):
        self.reads(good_reading(recipient_number="09265411484"), second=None)
        browser = Client()
        self.checkout("a", client=browser)
        self.checkout("a", client=browser)                                              # another picture: asked again, not booked
        self.assertEqual(self.reader.call_count, 2)
        self.nothing_booked()

    def test_try_again_never_counts_against_the_account(self):
        self.reads(good_reading(recipient_number="09265411484"), second=None)
        for _ in range(6):
            self.checkout("a", client=Client())
        self.assertEqual(ProofRejection.objects.count(), 0)
        self.nothing_booked()

    def test_the_memory_is_cleared_once_the_picture_is_settled(self):
        self.reads(good_reading(recipient_number="09265411484"), second=None)
        browser, picture = Client(), unique_jpeg()
        self.checkout("a", client=browser, picture=picture)
        self.assertTrue(browser.session.get(receipt_tries.SESSION_KEY))
        self.booked(self.checkout("a", client=browser, picture=picture))
        self.assertFalse(browser.session.get(receipt_tries.SESSION_KEY))

    def test_a_reader_that_is_down_still_never_blocks_a_customer(self):
        self.reads(None, "Gemini unavailable")
        reservation = self.booked(self.checkout("a"))
        self.assertEqual(reservation.proof_check, Reservation.ProofCheck.NEEDS_CHECK)

    def test_a_good_receipt_is_untouched(self):
        self.reads(good_reading())
        reservation = self.booked(self.checkout("a"))
        self.assertEqual(reservation.proof_check, Reservation.ProofCheck.PASS)
        self.verifier.assert_not_called()

    def test_the_try_again_answer_is_not_a_refusal_for_the_page(self):
        self.reads(good_reading(recipient_number="09265411484"), second=None)
        data = self.checkout("a").json()
        self.assertEqual((data["success"], data["proof_rejected"], data["proof_retake"], data["proof_try_again"], data["account_blocked"]),
                         (False, True, True, True, False))
        self.assertNotIn("typed_mismatch", data)


# ---- the quota guard ----------------------------------------------------------------------------------------------------------------------
@override_settings(RECEIPT_READER_ENABLED=True, GEMINI_API_KEY="stand-in")
class TheQuotaGuardTests(ReceiptCheckBase):
    BLURRY = dict(reference="", clarity="blurry")                                 # a picture to take again: read, never counted as a refusal

    def retake(self, who="a", client=None, **kw):
        return self.checkout(who, client=client or Client(), **kw)

    def setUp(self):
        super().setUp()
        self.reads(good_reading(**self.BLURRY))

    def pk(self, who="a"):
        return self.customers[who].pk

    def fill(self, name, failures, age=timedelta(0)):
        LoginThrottle.objects.update_or_create(key=f"rl:{name}", defaults={"failures": failures, "last_failure_at": timezone.now() - age})

    def test_the_numbers_are_the_ones_agreed(self):
        from gowns import views
        self.assertEqual(views._SCAN_LIMITS_ACCOUNT, ((10, 3600), (25, 86400)))
        self.assertEqual(views._SCAN_LIMITS_ADDRESS, ((14, 3600), (60, 86400)))
        self.assertEqual(views.TOO_MANY_SCANS_MESSAGE, "Too many tries. Please wait, or message the shop on Facebook.")

    def test_ten_readings_an_hour_are_fine_and_the_eleventh_is_stopped_before_it_is_read(self):
        self.fill(f"scan:u:{self.pk()}:h", 9)                                          # nine readings already done this hour
        self.assertEqual(self.retake().status_code, 400)                               # the tenth is read
        self.assertEqual(self.reader.call_count, 1)
        eleventh = self.retake()
        self.assertEqual(eleventh.status_code, 429)
        self.assertEqual(eleventh.json(), {"success": False, "too_many_tries": True, "error": "Too many tries. Please wait, or message the shop on Facebook."})
        self.assertEqual(self.reader.call_count, 1)                                    # the eleventh never reached Gemini
        self.nothing_booked()
        self.assertEqual(ProofRejection.objects.count(), 0)                           # being stopped is not a refused proof

    def test_twenty_five_a_day(self):
        self.fill(f"scan:u:{self.pk()}:d", 25)
        blocked = self.retake()
        self.assertEqual((blocked.status_code, blocked.json()["too_many_tries"]), (429, True))
        self.assertEqual(self.reader.call_count, 0)

    def test_the_account_limit_is_per_account(self):
        self.fill(f"scan:u:{self.pk('a')}:h", 10)
        self.assertEqual(self.retake("a").status_code, 429)
        self.assertEqual(self.retake("b").status_code, 400)                            # another customer is untouched
        self.assertEqual(self.reader.call_count, 1)

    def test_fourteen_readings_an_hour_from_one_address_then_everybody_on_it_is_stopped(self):
        extra = {key: User.objects.create_user(f"quota_{key}", f"quota_{key}@example.com", "Pass-12345!") for key in "efghijklmnopqrs"}
        self.customers.update(extra)
        shared = Client(HTTP_X_FORWARDED_FOR="198.51.100.9")
        users = ["a", "b", "c", "d"] + list(extra)
        for number, who in enumerate(users[:14]):
            self.assertEqual(self.checkout(who, client=shared).status_code, 400, number)
        self.assertEqual(self.reader.call_count, 14)
        fifteenth = self.checkout(users[14], client=shared)
        self.assertEqual((fifteenth.status_code, fifteenth.json()["too_many_tries"]), (429, True))
        self.assertEqual(self.reader.call_count, 14)

    def test_sixty_a_day_from_one_address(self):
        self.fill("scan:ip:198.51.100.10:d", 60)
        blocked = self.checkout("a", client=Client(HTTP_X_FORWARDED_FOR="198.51.100.10"))
        self.assertEqual((blocked.status_code, blocked.json()["too_many_tries"]), (429, True))
        self.assertEqual(self.checkout("a", client=Client(HTTP_X_FORWARDED_FOR="198.51.100.11")).status_code, 400)       # another address is fine

    def test_the_counts_start_again_when_the_hour_has_passed(self):
        self.fill(f"scan:u:{self.pk()}:h", 10, age=timedelta(hours=1, minutes=1))
        self.assertEqual(self.retake().status_code, 400)
        self.assertEqual(rate_limit.count(f"scan:u:{self.pk()}:h"), 1)

    def test_a_retake_counts(self):
        self.retake()
        self.assertEqual(rate_limit.count(f"scan:u:{self.pk()}:h"), 1)
        self.assertEqual(rate_limit.count(f"scan:u:{self.pk()}:d"), 1)

    def test_a_box_that_needs_fixing_never_reaches_the_reader_so_it_never_counts(self):
        response = self.checkout("a", client=Client(), gcash_reference="123")
        self.assertEqual((response.status_code, response.json()["typed_invalid"]), (400, True))
        self.assertEqual(rate_limit.count(f"scan:u:{self.pk()}:h"), 0)
        self.assertEqual(self.reader.call_count, 0)

    def test_a_reference_already_used_never_reaches_the_reader_so_it_never_counts(self):
        Reservation.objects.create(customer=None, customer_name="X", gcash_reference="5046933479702")
        self.reads(good_reading())
        data = self.refused(self.checkout("a", client=Client()))
        self.assertIn("already used", data["error"])
        self.assertEqual(rate_limit.count(f"scan:u:{self.pk()}:h"), 0)

    def test_a_good_booking_uses_one_reading(self):
        self.reads(good_reading())
        self.booked(self.checkout("a", client=Client()))
        self.assertEqual(rate_limit.count(f"scan:u:{self.pk()}:h"), 1)

    def test_the_limit_stops_a_later_upload_too(self):
        reservation = Reservation.objects.create(
            customer=self.customers["a"], customer_name="Rita A", status=Reservation.Status.PENDING, security_deposit=Decimal("2000"))
        from reservations.models import ReservationItem
        item = ReservationItem.objects.create(
            reservation=reservation, gown=self.gowns[2], gown_name="G", size="Medium", rental_price=Decimal("3000"),
            rental_date=self.event - timedelta(days=2), return_date=self.event + timedelta(days=2))
        self.fill(f"scan:u:{self.pk()}:h", 10)
        client = Client()
        client.force_login(self.customers["a"])
        agreed(self.customers["a"])
        response = client.post(reverse("gowns:reservation_upload_proof", args=[item.pk]), data={
            "proof_of_payment": SimpleUploadedFile("late.jpg", unique_jpeg(), content_type="image/jpeg"), **self.typed(2000)})
        self.assertEqual((response.status_code, response.json()["too_many_tries"]), (429, True))
        self.assertEqual(self.reader.call_count, 0)

    def test_a_blocked_customer_can_still_browse_and_everything_else_works(self):
        self.fill(f"scan:u:{self.pk()}:h", 10)
        self.retake()
        client = Client()
        client.force_login(self.customers["a"])
        self.assertEqual(client.get("/").status_code, 200)

    def test_an_outage_is_not_a_limit(self):
        self.reads(None, "Gemini unavailable")
        reservation = self.booked(self.checkout("a", client=Client()))
        self.assertEqual(reservation.proof_check, Reservation.ProofCheck.NEEDS_CHECK)


class TheQuotaGuardIsOffWhenNothingIsSpentTests(ReceiptCheckBase):
    def test_with_the_reader_switched_off_nothing_is_counted_or_limited(self):
        self.reads(good_reading(reference="", clarity="blurry"))
        LoginThrottle.objects.create(key=f"rl:scan:u:{self.customers['a'].pk}:h", failures=500, last_failure_at=timezone.now())      # far over the limit
        for _ in range(3):
            self.assertEqual(self.checkout("a", client=Client()).status_code, 400)               # still answered normally
        self.assertEqual(rate_limit.count(f"scan:u:{self.customers['a'].pk}:h"), 500)           # and nothing was added

    @override_settings(RECEIPT_READER_ENABLED=True, GEMINI_API_KEY="")
    def test_with_no_key_nothing_is_counted_either(self):
        self.reads(good_reading(reference="", clarity="blurry"))
        LoginThrottle.objects.create(key=f"rl:scan:u:{self.customers['a'].pk}:h", failures=500, last_failure_at=timezone.now())
        for _ in range(3):
            self.assertEqual(self.checkout("a", client=Client()).status_code, 400)
        self.assertEqual(rate_limit.count(f"scan:u:{self.customers['a'].pk}:h"), 500)


class TheReasonIsWrittenInWordsTests(CheckBase):
    """The staff and the owner read the note, so the reason a second reading could not be made is in words, never a code like "no_time"."""

    def test_each_reason_has_its_words(self):
        for code, words in (("no_time", "had already used the time"), ("timeout", "Google took too long"), ("network", "connection to Google failed"),
                            ("disabled", "switched off"), ("not_configured", "no key"), ("unreadable", "could not be understood"),
                            ("http_503", "Google was busy or refused the request (503)"), ("http_429", "(429)"), ("", "no reason given")):
            with self.subTest(code=code):
                self.assertIn(words, rc._why_words(code))

    def test_the_note_the_owner_reads_has_no_code_in_it(self):
        verdict = self.run_check(reading(recipient_number="09265411484"), second=None)
        note = " ".join(verdict.notes)
        self.assertIn("a second reading could not be made (Google took too long to answer)", note)
        self.assertNotIn("(timeout)", note)

    def test_the_unusual_layout_note_uses_words_too(self):
        verdict = self.run_check(reading(layout="other", gcash_signs=[]), second=None)
        self.assertIn("could not be made (Google took too long to answer)", " ".join(verdict.notes))
