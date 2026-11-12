"""The receipt check's RULES, with no database and no reader: every rule of the agreed plan, one test each, plus the six real screenshots the
owner sent (as the readings the reader would give), judged exactly as the plan says.

The shop in these tests: GCash number 09488745992, account name shown masked as "DE****K C.", deposit for the booking 2,000.
"""
import json
from datetime import datetime, timedelta
from decimal import Decimal

from django.test import SimpleTestCase
from django.utils import timezone

from gowns import receipt_check as rc

SHOP = "09488745992"
OLD_SHOP = "09171234567"
NOW = timezone.make_aware(datetime(2026, 11, 12, 15, 0))
STARTED = NOW - timedelta(minutes=15)      # the checkout hold began 15 minutes ago


def at(**delta):
    return NOW - timedelta(**delta)


def reading(**changes):
    """A receipt that passes every rule; each test changes the one thing it is about."""
    values = dict(
        is_payment_screen=True, is_transaction_list=False, app="gcash", direction="sent", recipient_number=SHOP,
        recipient_name="DE****K C.", sender_number="09167957938", amount=Decimal("2000.00"), reference="5046933479702",
        paid_at=at(minutes=5), status="completed", gcash_signs=["Express Send"],
    )
    values.update(changes)
    return rc.Reading(**values)


def judge(r, *, expected=Decimal("2000"), started=STARTED, shop_name="DE****K C.", reference_used_by="", image_used_by="",
          fingerprint_used_by="", accepted=None, known=None):
    accepted = accepted if accepted is not None else [(SHOP, False)]
    return rc.evaluate(
        r, expected_amount=expected, started_at=started, now=NOW, shop_name=shop_name,
        reference_used_by=reference_used_by, image_used_by=image_used_by, fingerprint_used_by=fingerprint_used_by,
        accepted_at=lambda moment: accepted, known_numbers=set(known if known is not None else [n for n, _ in accepted]),
    )


class PassTests(SimpleTestCase):
    def test_a_good_receipt_passes_with_nothing_to_say(self):
        verdict = judge(reading())
        self.assertEqual(verdict.status, rc.STATUS_PASS)
        self.assertEqual(verdict.codes, [])
        self.assertEqual(verdict.reference, "5046933479702")
        self.assertEqual(verdict.recipient, SHOP)
        self.assertEqual(verdict.amount, Decimal("2000.00"))

    def test_a_history_screen_with_numbers_only_and_no_name_still_passes(self):
        # "Transfer from 09167957938 to 09488745992": GCash's own history shows no names.
        self.assertEqual(judge(reading(recipient_name="")).status, rc.STATUS_PASS)

    def test_the_shops_name_is_compared_the_way_gcash_masks_it(self):
        self.assertEqual(judge(reading(recipient_name="DE••••K C."), shop_name="Denmark Concepcion").status, rc.STATUS_PASS)
        self.assertEqual(judge(reading(recipient_name="DE****K C."), shop_name="").status, rc.STATUS_PASS)   # no shop name set: nothing to compare


class NotAPaymentTests(SimpleTestCase):
    def test_a_random_picture_is_refused(self):
        verdict = judge(reading(is_payment_screen=False))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["not_payment"]))
        self.assertIn("doesn't look like a payment receipt", verdict.message)

    def test_a_list_of_transactions_is_refused_with_what_to_do(self):
        verdict = judge(reading(is_transaction_list=True, is_payment_screen=False))
        self.assertEqual(verdict.codes, ["list_screen"])
        self.assertIn("open your payment", verdict.message)

    def test_other_apps_are_refused_with_the_facebook_hint(self):
        for app in ("maya", "bank", "other"):
            verdict = judge(reading(app=app))
            self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["not_gcash"]), app)
            self.assertIn("Only GCash receipts are accepted", verdict.message)
            self.assertIn("Facebook", verdict.message)

    def test_a_failed_payment_is_refused(self):
        self.assertEqual(judge(reading(status="failed")).codes, ["failed"])

    def test_money_received_is_not_a_payment_the_customer_sent(self):
        verdict = judge(reading(direction="received"))
        self.assertEqual(verdict.codes, ["received"])
        self.assertIn("money you received", verdict.message)

    def test_a_payment_to_a_store_is_refused(self):
        verdict = judge(reading(direction="merchant_payment", recipient_number=""))
        self.assertEqual(verdict.codes, ["merchant"])
        self.assertIn("store or biller", verdict.message)

    def test_no_reference_number_is_refused(self):
        verdict = judge(reading(reference=""))
        self.assertEqual(verdict.codes, ["no_reference"])
        self.assertIn("Ref No.", verdict.message)


class RecipientTests(SimpleTestCase):
    def test_another_number_is_refused(self):
        verdict = judge(reading(recipient_number="09265411484"))
        self.assertEqual(verdict.codes, ["wrong_recipient"])
        self.assertIn("not sent to Arabela's GCash number", verdict.message)

    def test_the_shop_as_the_sender_is_not_a_payment_to_the_shop(self):
        # "Transfer from 09488745992 (the shop) to 09265411484": the recipient read is the OTHER number.
        self.assertEqual(judge(reading(recipient_number="09265411484", sender_number=SHOP)).codes, ["wrong_recipient"])

    def test_no_recipient_goes_to_the_staff_not_the_bin(self):
        verdict = judge(reading(recipient_number="", recipient_name=""))
        self.assertEqual(verdict.status, rc.STATUS_NEEDS_CHECK)
        self.assertIn("recipient_not_visible", verdict.codes)
        self.assertTrue(any("neither" in note for note in verdict.notes))

    def test_a_matching_name_without_a_number_still_needs_a_check(self):
        verdict = judge(reading(recipient_number=""))
        self.assertEqual(verdict.status, rc.STATUS_NEEDS_CHECK)
        self.assertTrue(any("matches the shop's" in note for note in verdict.notes))

    def test_a_different_name_without_a_number_needs_a_check(self):
        verdict = judge(reading(recipient_number="", recipient_name="MA****A L."))
        self.assertEqual(verdict.status, rc.STATUS_NEEDS_CHECK)
        self.assertTrue(any("different name" in note for note in verdict.notes))

    def test_the_right_number_with_a_different_name_needs_a_check(self):
        verdict = judge(reading(recipient_name="MA****A L."))
        self.assertEqual(verdict.status, rc.STATUS_NEEDS_CHECK)
        self.assertIn("name_differs", verdict.codes)

    def test_only_the_amount_date_and_reference_needs_a_check(self):
        verdict = judge(reading(recipient_number="", recipient_name=""))
        self.assertEqual(verdict.status, rc.STATUS_NEEDS_CHECK)

    def test_the_old_number_inside_its_grace_passes_with_a_note_for_the_staff(self):
        verdict = judge(reading(recipient_number=OLD_SHOP, recipient_name=""), accepted=[(SHOP, False), (OLD_SHOP, True)])
        self.assertEqual(verdict.status, rc.STATUS_PASS)
        self.assertTrue(verdict.old_number)
        self.assertTrue(any("OLD GCash number" in note for note in verdict.notes))

    def test_the_old_number_after_its_grace_is_refused_with_its_own_message(self):
        verdict = judge(reading(recipient_number=OLD_SHOP), accepted=[(SHOP, False)], known=[SHOP, OLD_SHOP])
        self.assertEqual(verdict.codes, ["old_number"])
        self.assertIn("no longer used", verdict.message)

    def test_a_shop_with_no_number_saved_never_refuses_a_customer_for_it(self):
        verdict = judge(reading(recipient_number="09265411484"), accepted=[], known=[])
        self.assertEqual(verdict.status, rc.STATUS_NEEDS_CHECK)
        self.assertIn("shop_number_not_set", verdict.codes)
        self.assertTrue(any("not saved in the settings" in note for note in verdict.notes))

    def test_a_number_the_shop_never_had_is_just_another_number(self):
        self.assertEqual(judge(reading(recipient_number="09999999999"), known=[SHOP, OLD_SHOP]).codes, ["wrong_recipient"])


class AmountTests(SimpleTestCase):
    def test_the_exact_deposit_passes(self):
        self.assertEqual(judge(reading(amount=Decimal("2000"))).status, rc.STATUS_PASS)

    def test_a_little_over_passes_with_a_note(self):
        verdict = judge(reading(amount=Decimal("2001")))
        self.assertEqual(verdict.status, rc.STATUS_PASS)
        self.assertTrue(any("+₱1 more" in note for note in verdict.notes))

    def test_up_to_100_over_still_passes(self):
        self.assertEqual(judge(reading(amount=Decimal("2100"))).status, rc.STATUS_PASS)

    def test_much_more_than_the_deposit_needs_a_check(self):
        verdict = judge(reading(amount=Decimal("2101")))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["amount_high"]))

    def test_less_than_the_deposit_needs_a_check(self):
        verdict = judge(reading(amount=Decimal("1999")))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["amount_low"]))
        self.assertTrue(any("₱1,999" in note and "₱2,000" in note for note in verdict.notes))

    def test_an_unreadable_amount_needs_a_check(self):
        self.assertEqual(judge(reading(amount=None)).codes, ["amount_unreadable"])

    def test_the_deposit_the_owner_set_is_what_counts(self):
        # The owner set 1 peso per gown and the customer booked 5 gowns: the deposit is 5.
        self.assertEqual(judge(reading(amount=Decimal("5")), expected=Decimal("5")).status, rc.STATUS_PASS)
        self.assertEqual(judge(reading(amount=Decimal("4")), expected=Decimal("5")).codes, ["amount_low"])


class DateTests(SimpleTestCase):
    def test_paid_after_the_booking_started_passes(self):
        self.assertEqual(judge(reading(paid_at=at(minutes=3))).status, rc.STATUS_PASS)

    def test_paid_a_few_minutes_before_the_booking_started_still_passes(self):
        self.assertEqual(judge(reading(paid_at=STARTED - timedelta(minutes=9))).status, rc.STATUS_PASS)

    def test_paid_earlier_the_same_day_needs_a_check(self):
        verdict = judge(reading(paid_at=STARTED - timedelta(minutes=11)))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["paid_before_booking"]))

    def test_paid_23_hours_ago_needs_a_check_not_a_refusal(self):
        self.assertEqual(judge(reading(paid_at=at(hours=23))).codes, ["paid_before_booking"])

    def test_a_receipt_older_than_24_hours_is_refused_with_its_date(self):
        verdict = judge(reading(paid_at=timezone.make_aware(datetime(2026, 9, 11, 10, 0))))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["too_old"]))
        self.assertEqual(verdict.message, "This receipt is from Sep 11, 2026. Please upload the receipt of today's payment.")

    def test_exactly_at_the_24_hour_edge(self):
        self.assertEqual(judge(reading(paid_at=at(hours=24, seconds=1))).codes, ["too_old"])
        self.assertEqual(judge(reading(paid_at=at(hours=24))).status, rc.STATUS_NEEDS_CHECK)

    def test_a_receipt_dated_in_the_future_needs_a_check(self):
        self.assertEqual(judge(reading(paid_at=NOW + timedelta(minutes=11))).codes, ["paid_in_future"])
        self.assertEqual(judge(reading(paid_at=NOW + timedelta(minutes=5))).status, rc.STATUS_PASS)

    def test_a_date_that_cannot_be_read_needs_a_check(self):
        self.assertEqual(judge(reading(paid_at=None)).codes, ["date_unreadable"])

    def test_without_a_running_hold_only_the_24_hour_rule_applies(self):
        self.assertEqual(judge(reading(paid_at=at(hours=3)), started=None).status, rc.STATUS_PASS)


class ReuseTests(SimpleTestCase):
    def test_a_reference_already_on_another_booking_is_refused(self):
        verdict = judge(reading(), reference_used_by="RSV-2026-0042")
        self.assertEqual(verdict.codes, ["reference_used"])
        self.assertIn("already used for another booking", verdict.message)

    def test_the_same_picture_is_refused_even_when_the_reader_was_not_used(self):
        verdict = judge(None, image_used_by="RSV-2026-0042")
        self.assertEqual(verdict.codes, ["image_used"])

    def test_same_amount_minute_and_recipient_is_only_a_warning(self):
        verdict = judge(reading(), fingerprint_used_by="RSV-2026-0042")
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["same_amount_and_time"]))
        self.assertTrue(any("RSV-2026-0042" in note for note in verdict.notes))


class UnreadableTests(SimpleTestCase):
    def test_when_the_reader_could_not_run_the_staff_look_at_it_and_the_customer_is_not_blocked(self):
        verdict = judge(None)
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["reader_unavailable"]))
        self.assertFalse(verdict.rejected)

    def test_a_reference_with_the_wrong_number_of_digits_needs_a_check(self):
        verdict = judge(reading(reference="504693347970"))
        self.assertEqual(verdict.codes, ["reference_length"])

    def test_an_unclear_app_needs_a_check(self):
        self.assertEqual(judge(reading(app="unknown")).codes, ["app_unclear"])


class TheOwnersScreenshotsTests(SimpleTestCase):
    """The six real screenshots (readings written from what is printed on them), judged as the plan says. The shop's number is 0948 874 5992."""

    def judge_at(self, r, now):
        return rc.evaluate(
            r, expected_amount=Decimal("2000"), started_at=now - timedelta(minutes=15), now=now, shop_name="DE****K C.",
            accepted_at=lambda moment: [(SHOP, False)], known_numbers={SHOP},
        )

    def test_gcash_express_send_to_another_number_is_refused(self):      # screenshot 1: +63 916 795 7938, 500.00, Nov 5 11:52 PM
        r = reading(recipient_number="09167957938", recipient_name="JO•E MI•••L T.", amount=Decimal("500"),
                    reference="5046933479702", paid_at=timezone.make_aware(datetime(2026, 11, 5, 23, 52)))
        self.assertEqual(self.judge_at(r, timezone.make_aware(datetime(2026, 11, 5, 23, 55))).codes, ["wrong_recipient"])

    def test_gcash_express_send_to_the_shop_for_100_is_short_of_the_deposit(self):   # screenshot 2: +63 948 874 5992, 100.00, Nov 7 1:13 PM
        r = reading(recipient_name="DE••••K C.", amount=Decimal("100"), reference="1046986077734",
                    paid_at=timezone.make_aware(datetime(2026, 11, 7, 13, 13)))
        verdict = self.judge_at(r, timezone.make_aware(datetime(2026, 11, 7, 13, 20)))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["amount_low"]))

    def test_the_same_receipt_days_later_is_too_old(self):
        r = reading(amount=Decimal("2000"), reference="1046986077734", paid_at=timezone.make_aware(datetime(2026, 11, 7, 13, 13)))
        self.assertEqual(self.judge_at(r, NOW).codes, ["too_old"])

    def test_payment_to_bancnet_p2m_is_a_store_payment(self):            # screenshot 3: "Payment to Bancnet P2M Send", 600.00, ref 570660725
        r = reading(direction="merchant_payment", recipient_number="", recipient_name="", amount=Decimal("600"), reference="570660725",
                    paid_at=timezone.make_aware(datetime(2026, 11, 11, 17, 25)))
        self.assertEqual(self.judge_at(r, timezone.make_aware(datetime(2026, 11, 11, 17, 30))).codes, ["merchant"])

    def test_a_transfer_between_two_other_numbers_is_refused(self):      # screenshot 4: "Transfer from 09488745992 to 09265411484", 300.00
        r = reading(recipient_number="09265411484", recipient_name="", sender_number=SHOP, amount=Decimal("300"), reference="5047145610474",
                    paid_at=timezone.make_aware(datetime(2026, 11, 11, 18, 57)))
        self.assertEqual(self.judge_at(r, timezone.make_aware(datetime(2026, 11, 11, 19, 0))).codes, ["wrong_recipient"])

    def test_both_maya_screenshots_are_refused_as_not_gcash(self):
        received = reading(app="maya", direction="received", recipient_number="", reference="", amount=Decimal("86"))
        sent = reading(app="maya", direction="sent", recipient_number="09105950144", amount=Decimal("87"), reference="")
        self.assertEqual(judge(received).codes, ["not_gcash"])
        self.assertEqual(judge(sent).codes, ["not_gcash"])


class NameTests(SimpleTestCase):
    def test_names_match_by_the_parts_gcash_never_hides(self):
        self.assertTrue(rc.names_match("DE****K C.", "Denmark Concepcion"))
        self.assertTrue(rc.names_match("DE••••K C.", "DE****K C."))
        self.assertTrue(rc.names_match("JO•E MI•••L T.", "Jose Miguel Tolentino"))
        self.assertFalse(rc.names_match("MA****A L.", "Denmark Concepcion"))
        self.assertFalse(rc.names_match("DE****K R.", "Denmark Concepcion"))     # same start, different last-name initial

    def test_a_missing_name_is_not_a_mismatch(self):
        self.assertIsNone(rc.names_match("", "Denmark Concepcion"))
        self.assertIsNone(rc.names_match("DE****K C.", ""))


class ReadingParsingTests(SimpleTestCase):
    def test_what_the_reader_says_becomes_a_clean_reading(self):
        r = rc.reading_from_dict({
            "is_payment_screen": True, "is_transaction_list": False, "app": "GCash", "direction": "sent", "recipient_number": "+63 948 874 5992",
            "recipient_name": "DE****K C.", "sender_number": "", "amount": "-300.00", "reference": "5046 933 479702",
            "date_time": "2026-11-05 23:52", "status": "completed", "gcash_signs": ["Express Send"],
        })
        self.assertTrue(r.is_payment_screen)
        self.assertEqual(r.app, "gcash")
        self.assertEqual(r.recipient_number, "09488745992")
        self.assertEqual(r.amount, Decimal("300.00"))            # the minus sign of a history screen is dropped
        self.assertEqual(r.reference, "5046933479702")
        self.assertEqual(r.paid_at, timezone.make_aware(datetime(2026, 11, 5, 23, 52)))

    def test_a_number_the_reader_returns_without_its_leading_zero_is_still_the_shops(self):
        # Seen with the real reader on a GCash receipt printed as "+63 948 874 5992": it answered 9488745992.
        r = rc.reading_from_dict({"is_payment_screen": True, "app": "gcash", "direction": "sent", "recipient_number": "9488745992",
                                  "amount": "2000", "reference": "5046933479702", "date_time": "2026-11-12 14:55", "status": "completed"})
        self.assertEqual(r.recipient_number, SHOP)

    def test_odd_or_missing_values_never_raise(self):
        r = rc.reading_from_dict({"amount": "abc", "date_time": "yesterday", "reference": None, "is_payment_screen": "yes"})
        self.assertIsNone(r.amount)
        self.assertIsNone(r.paid_at)
        self.assertEqual(r.reference, "")
        self.assertFalse(r.is_payment_screen)                     # only a real true counts
        self.assertIsNone(rc.reading_from_dict("not an object"))

    def test_the_reading_is_kept_as_json(self):
        stored = json.loads(reading().to_json())
        self.assertEqual(stored["reference"], "5046933479702")
        self.assertEqual(stored["amount"], "2000.00")

    def test_json_inside_a_code_fence_is_understood(self):
        data = {"candidates": [{"content": {"parts": [{"text": "```json\n{\"app\": \"gcash\"}\n```"}]}}]}
        self.assertEqual(rc._extract_json(data), {"app": "gcash"})
        self.assertIsNone(rc._extract_json({"candidates": []}))
