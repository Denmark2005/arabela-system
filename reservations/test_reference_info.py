"""What a booking says about its GCash reference number (Reservation.reference_info, proof_search_text, the duplicate warning and the badge):
the typed number and the number the receipt reader found, shown together when they differ."""
import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

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


class ThePaidDateAndTheReasonTests(TestCase):
    """The reference card also tells the owner WHEN the receipt says it was paid and why the receipt check sent the booking to the staff (2026-11-19)."""

    def paid(self, **fields):
        from datetime import datetime
        return booking(gcash_reference="9046723594550", proof_typed_reference="9046723594550", proof_match="match",
                       proof_paid_at=timezone.make_aware(datetime(2026, 10, 31, 10, 20)), **fields)

    def test_the_date_is_written_the_way_the_owner_reads_it(self):
        self.assertEqual(self.paid().reference_info["paidAt"], "Oct 31, 2026, 10:20 AM")
        from datetime import datetime
        evening = booking(gcash_reference="9046723594550", proof_paid_at=timezone.make_aware(datetime(2026, 11, 2, 15, 5)))
        self.assertEqual(evening.reference_info["paidAt"], "Nov 2, 2026, 3:05 PM")
        noon = booking(gcash_reference="9046723594550", proof_paid_at=timezone.make_aware(datetime(2026, 11, 2, 12, 0)))
        self.assertEqual(noon.reference_info["paidAt"], "Nov 2, 2026, 12:00 PM")

    def test_a_receipt_more_than_a_day_old_when_the_booking_was_made_is_marked_old(self):
        old = self.paid()
        Reservation.objects.filter(pk=old.pk).update(created_at=timezone.make_aware(__import__("datetime").datetime(2026, 11, 18, 23, 17)))
        old.refresh_from_db()
        self.assertTrue(old.reference_info["paidOld"])

    def test_the_line_is_exactly_twenty_four_hours(self):
        from datetime import datetime, timedelta
        paid = timezone.make_aware(datetime(2026, 11, 18, 10, 0))
        edge = booking(gcash_reference="1" * 13, proof_paid_at=paid)
        for delta, expected in ((timedelta(hours=24), False), (timedelta(hours=24, minutes=1), True), (timedelta(hours=2), False)):
            with self.subTest(delta=delta):
                Reservation.objects.filter(pk=edge.pk).update(created_at=paid + delta)
                edge.refresh_from_db()
                self.assertEqual(edge.reference_info["paidOld"], expected)

    def test_a_fresh_receipt_is_not_old_and_no_date_is_nothing(self):
        fresh = booking(gcash_reference="1" * 13, proof_paid_at=timezone.now() - timezone.timedelta(minutes=5))
        self.assertFalse(fresh.reference_info["paidOld"])
        none = booking(gcash_reference="1" * 13)
        self.assertEqual((none.reference_info["paidAt"], none.reference_info["paidOld"]), ("", False))

    def test_the_checks_own_words_are_shown_only_when_it_sent_the_booking_to_the_staff(self):
        notes = "The reader would have refused this receipt (receipt too old), but a second reading could not be made.\nSecond line.\nThird line."
        flagged = self.paid(proof_check="needs_check", proof_notes=notes)
        self.assertEqual(flagged.reference_info["notes"], notes.split("\n")[:2])               # at most two lines: the card stays short
        self.assertEqual(flagged.reference_info["check"], "needs_check")
        fine = self.paid(proof_check="pass", proof_notes="Paid ₱2,050; the deposit is ₱2,000 (+₱50 more than required).")
        self.assertEqual(fine.reference_info["notes"], [])
        self.assertEqual(fine.reference_info["check"], "pass")
        self.assertEqual(self.paid(proof_check="needs_check", proof_notes="").reference_info["notes"], [])
        self.assertEqual(self.paid().reference_info["check"], "")

    def test_it_is_still_valid_json_for_the_pages(self):
        data = json.loads(self.paid(proof_check="needs_check", proof_notes="a").reference_info_json)
        self.assertEqual((data["paidAt"], data["notes"], data["check"]), ("Oct 31, 2026, 10:20 AM", ["a"], "needs_check"))
