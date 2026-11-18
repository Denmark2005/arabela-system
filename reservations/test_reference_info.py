"""What a booking says about its GCash reference number (Reservation.reference_info, proof_search_text, the duplicate warning and the badge):
the typed number and the number the receipt reader found, shown together when they differ."""
import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from reservations.models import Reservation

User = get_user_model()


def booking(**fields):
    return Reservation.objects.create(customer=None, customer_name="Rita", status=Reservation.Status.PENDING, **fields)


class TheReferenceInfoTests(TestCase):
    def test_a_booking_with_no_reference_has_nothing_to_show(self):
        info = booking().reference_info
        self.assertIsNone(info)
        self.assertEqual(booking().reference_info_json, "null")

    def test_a_typed_reference_is_shown_grouped_and_copied_as_plain_digits(self):
        r = booking(gcash_reference="5046933479702", proof_typed_reference="5046933479702", proof_typed_amount=Decimal("2000"),
                    proof_amount=Decimal("2000"), proof_match="match")
        info = r.reference_info
        self.assertEqual((info["reference"], info["digits"], info["typed"], info["match"]), ("5046 933 479702", "5046933479702", True, "match"))
        self.assertEqual((info["referenceDiffers"], info["amountDiffers"]), (False, False))
        self.assertEqual((info["receiptReference"], info["receiptAmount"]), ("", ""))          # nothing extra when they agree
        self.assertEqual(info["typedAmount"], "₱2,000")

    def test_a_booking_made_before_the_boxes_existed_shows_the_number_read_from_the_receipt(self):
        info = booking(gcash_reference="5046933479702").reference_info
        self.assertEqual((info["reference"], info["typed"], info["match"]), ("5046 933 479702", False, ""))

    def test_a_difference_shows_both_numbers(self):
        r = booking(gcash_reference="5046933479702", proof_typed_reference="5046933479720", proof_typed_amount=Decimal("2500"),
                    proof_amount=Decimal("2000"), proof_match="both")
        info = r.reference_info
        self.assertEqual(info["reference"], "5046 933 479720")                          # the number the customer typed is the one shown
        self.assertEqual((info["referenceDiffers"], info["amountDiffers"]), (True, True))
        self.assertEqual((info["receiptReference"], info["typedAmount"], info["receiptAmount"]), ("5046 933 479702", "₱2,500", "₱2,000"))

    def test_only_the_box_that_differs_is_flagged(self):
        only_amount = booking(gcash_reference="5046933479702", proof_typed_reference="5046933479702", proof_typed_amount=Decimal("2500"),
                              proof_amount=Decimal("2000"), proof_match="amount").reference_info
        self.assertEqual((only_amount["referenceDiffers"], only_amount["amountDiffers"], only_amount["receiptReference"]), (False, True, ""))
        self.assertEqual(only_amount["receiptAmount"], "₱2,000")

    def test_it_is_valid_json_for_a_page_script(self):
        r = booking(gcash_reference="5046933479702", proof_typed_reference="5046933479702", proof_match="match")
        self.assertEqual(json.loads(r.reference_info_json)["digits"], "5046933479702")

    def test_a_reference_that_is_not_thirteen_digits_is_shown_as_it_is(self):
        self.assertEqual(booking(gcash_reference="586757758").reference_info["reference"], "586757758")


class TheSearchTextTests(TestCase):
    def test_both_the_plain_and_the_grouped_form_are_searchable(self):
        text = booking(gcash_reference="5046933479702", proof_typed_reference="5046933479702").proof_search_text
        self.assertEqual(text.split(), ["5046933479702", "5046", "933", "479702"])
        self.assertIn("5046933479702", text)
        self.assertIn("5046 933 479702", text)

    def test_a_typed_number_that_differs_from_the_receipt_is_searchable_under_both(self):
        text = booking(gcash_reference="5046933479702", proof_typed_reference="5046933479720").proof_search_text
        for needle in ("5046933479702", "5046 933 479702", "5046933479720", "5046 933 479720", "479720", "479702"):
            self.assertIn(needle, text)

    def test_a_booking_without_a_reference_has_no_search_words(self):
        self.assertEqual(booking().proof_search_text, "")


class TheDuplicateWarningTests(TestCase):
    def test_a_typed_number_on_another_booking_is_a_duplicate_whichever_field_carries_it(self):
        a = booking(gcash_reference="5046933479702", proof_typed_reference="5046933479702")
        b = booking(gcash_reference="1111111111111", proof_typed_reference="5046933479702")
        c = booking(gcash_reference="5046933479702")
        d = booking(gcash_reference="2222222222222", proof_typed_reference="3333333333333")
        self.assertEqual(set(a.gcash_duplicate_codes.split(", ")), {b.reference_code, c.reference_code})
        self.assertEqual(d.gcash_duplicate_codes, "")
        self.assertEqual(booking().gcash_duplicate_codes, "")


class TheBadgeTests(TestCase):
    def test_a_difference_adds_no_third_chip_to_the_lists_the_window_explains_it(self):
        # The lists already say "Needs check"; the reference card in the proof window says what differs (a third chip crowded the narrow column).
        for kind in ("reference", "amount", "both"):
            with self.subTest(kind=kind):
                badges = booking(proof_check="needs_check", proof_match=kind).proof_badges
                self.assertEqual(badges, [("warn", "Needs check")])
        matching = booking(proof_check="pass", proof_match="match").proof_badges
        self.assertEqual(matching, [("ok", "Pre-checked")])
