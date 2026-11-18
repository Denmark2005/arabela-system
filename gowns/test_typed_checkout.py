"""The reference number and amount typed at checkout and when a receipt is uploaded later, end to end through the real views
(gowns/typed_payment.py has the rules; the receipt reader is the usual stand-in of test_receipt_checkout.py):

  * a box that needs fixing is answered at once, BEFORE anything is read, and is never counted against the account;
  * a reference already on another booking is refused before the picture is read (and counted, like any refused receipt);
  * typed numbers that match the receipt are stored on the booking;
  * typed numbers that differ are only a question the first time; the next try goes through as "Needs check" with a note for the staff;
  * pressing Confirm Rental again after a dropped connection is still answered with the booking already saved;
  * nothing a customer types can make a check more lenient than before.
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from accounts.test_support import agreed
from gowns.sample_images import unique_jpeg
from gowns.test_receipt_checkout import ReceiptCheckBase, good_reading
from reservations.models import ProofRejection, Reservation, ReservationItem

REF = "5046933479702"
OTHER_REF = "5046933479720"


class TheBoxesAreCheckedFirstTests(ReceiptCheckBase):
    def assert_turned_away(self, response, field):
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data["success"])
        self.assertTrue(data["typed_invalid"])
        self.assertEqual(data["field"], field)
        self.reader.assert_not_called()                      # nothing was read
        self.nothing_booked()
        self.assertEqual(ProofRejection.objects.count(), 0)   # and nothing is counted against the account

    def test_a_reference_that_is_not_thirteen_digits_is_turned_away(self):
        self.reads(good_reading())
        for typed in ("", "abc", "586757758", "1" * 12, "1" * 14, "12 34"):
            with self.subTest(typed=typed):
                self.assert_turned_away(self.checkout("a", gcash_reference=typed), "reference")
                self.reader.reset_mock()

    def test_the_nine_digit_reference_of_a_bill_payment_is_turned_away_before_it_is_read(self):
        self.reads(good_reading())
        response = self.checkout("a", gcash_reference="586757758")
        self.assert_turned_away(response, "reference")
        self.assertIn("has 13 digits, and you typed 9", response.json()["error"])

    def test_an_amount_that_is_missing_wrong_or_less_than_the_deposit_is_turned_away(self):
        self.reads(good_reading())
        for typed in ("", "abc", "-5", "0", "100", "1999.99", "2000.123"):
            with self.subTest(typed=typed):
                self.assert_turned_away(self.checkout("a", gcash_amount=typed), "amount")
                self.reader.reset_mock()

    def test_the_deposit_that_is_due_is_the_one_the_server_works_out(self):
        # two gowns: the deposit is 4,000, whatever the page says
        self.reads(good_reading(amount=Decimal("4000")))
        response = self.checkout("a", (0, 1), gcash_amount="2000")
        self.assert_turned_away(response, "amount")
        self.assertIn("deposit for this booking is ₱4,000", response.json()["error"])

    def test_a_page_that_sends_neither_box_is_told_to_reload(self):
        self.reads(good_reading())
        client = Client()
        client.force_login(self.customers["a"])
        agreed(self.customers["a"])
        item = {"gown_name": self.gowns[0].name, "gown_slug": self.gowns[0].slug, "size": "Medium",
                "rental_date": (self.event - timedelta(days=2)).isoformat(), "return_date": (self.event + timedelta(days=2)).isoformat()}
        response = client.post(reverse("gowns:reservation_submit"), data={
            "items": json.dumps([item]), "first_name": "Rita", "last_name": "A", "phone": "09171234567", "address": "1 Test St", "city": "C",
            "postal_code": "1000", "payment_method": "GCash", "expected_deposit": "2000",
            "proof_of_payment": SimpleUploadedFile("proof.jpg", unique_jpeg(), content_type="image/jpeg")})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()["typed_required"])
        self.assertIn("reload", response.json()["error"])
        self.reader.assert_not_called()
        self.nothing_booked()
        self.assertEqual(ProofRejection.objects.count(), 0)

    def test_spaces_dashes_and_peso_signs_are_fine(self):
        self.reads(good_reading())
        reservation = self.booked(self.checkout("a", gcash_reference=" 5046-933-479702 ", gcash_amount="₱2,000.00"))
        self.assertEqual((reservation.proof_typed_reference, reservation.proof_typed_amount), (REF, Decimal("2000.00")))


class AReferenceAlreadyUsedIsRefusedBeforeReadingTests(ReceiptCheckBase):
    def test_the_same_reference_cannot_pay_for_a_second_booking_and_the_picture_is_not_read(self):
        self.reads(good_reading())
        self.booked(self.checkout("a"))
        self.reader.reset_mock()
        data = self.refused(self.checkout("b", (1,), gcash_reference="5046 933 479702"))
        self.assertIn("already used for another booking", data["error"])
        self.reader.assert_not_called()
        self.assertEqual(Reservation.objects.count(), 1)
        row = ProofRejection.objects.get()
        self.assertEqual((row.user, row.source, row.reason), (self.customers["b"], ProofRejection.Source.UPLOAD, "reference already used"))

    def test_both_the_typed_number_and_the_number_read_from_the_picture_count_as_used(self):
        # a booking that went through to the staff although the typed number differed carries two numbers: both are taken
        self.reads(good_reading())
        browser = Client()
        self.checkout("a", client=browser, gcash_reference=OTHER_REF)                     # asked to look again
        self.booked(self.checkout("a", client=browser, gcash_reference=OTHER_REF))        # goes through for the staff
        for number in (OTHER_REF, REF):
            with self.subTest(number=number):
                data = self.refused(self.checkout("b", (1,), gcash_reference=number))
                self.assertIn("already used for another booking", data["error"])

    def test_pressing_confirm_again_after_a_dropped_connection_is_answered_with_the_booking_already_saved(self):
        self.reads(good_reading())
        browser, picture = Client(), unique_jpeg()
        first = self.booked(self.checkout("a", client=browser, picture=picture))
        again = self.checkout("a", client=browser, picture=picture)                      # the same request, sent a second time
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual((again.json()["reference_code"], again.json().get("already_submitted")), (first.reference_code, True))
        self.assertEqual(Reservation.objects.count(), 1)
        self.assertEqual(ProofRejection.objects.count(), 0)


class TypedNumbersThatMatchTests(ReceiptCheckBase):
    def test_they_are_stored_on_the_booking(self):
        self.reads(good_reading())
        reservation = self.booked(self.checkout("a"))
        self.assertEqual(reservation.proof_check, Reservation.ProofCheck.PASS)
        self.assertEqual(reservation.proof_match, "match")
        self.assertEqual((reservation.proof_typed_reference, reservation.proof_typed_amount), (REF, Decimal("2000.00")))
        self.assertEqual(reservation.gcash_reference, REF)

    def test_a_more_generous_amount_that_matches_the_receipt_is_a_match(self):
        self.reads(good_reading(amount=Decimal("2050")))
        reservation = self.booked(self.checkout("a"))
        self.assertEqual((reservation.proof_match, reservation.proof_typed_amount), ("match", Decimal("2050.00")))

    def test_a_reader_that_is_down_never_blocks_a_customer_who_typed_both_boxes(self):
        self.reads(None, "Gemini unavailable")
        reservation = self.booked(self.checkout("a"))
        self.assertEqual(reservation.proof_check, Reservation.ProofCheck.NEEDS_CHECK)
        self.assertEqual(reservation.proof_match, "match")                           # nothing to compare is not a difference
        self.assertTrue(reservation.proof_typed_reference)
        self.assertEqual(ProofRejection.objects.count(), 0)

    def test_an_amount_the_reader_could_not_find_is_not_a_difference(self):
        self.reads(good_reading(amount=None))
        reservation = self.booked(self.checkout("a"))
        self.assertEqual(reservation.proof_match, "match")
        self.assertEqual(reservation.proof_check, Reservation.ProofCheck.NEEDS_CHECK)    # the existing "amount could not be read" note still sends it to the staff


class TypedNumbersThatDifferTests(ReceiptCheckBase):
    def ask_then_go_through(self, **typed):
        browser = Client()
        asked = self.checkout("a", client=browser, **typed)
        return browser, asked

    def test_the_first_difference_is_only_a_question(self):
        self.reads(good_reading())
        _, asked = self.ask_then_go_through(gcash_reference=OTHER_REF)
        self.assertEqual(asked.status_code, 400)
        data = asked.json()
        self.assertEqual((data["success"], data["typed_mismatch"], data["field"]), (False, True, "reference"))
        self.assertIn("reference number you typed doesn't match your receipt", data["error"])
        self.assertNotIn(REF, data["error"])                                        # what the receipt reads is never given back
        self.assertNotIn("proof_rejected", data)
        self.nothing_booked()
        self.assertEqual(ProofRejection.objects.count(), 0)                         # never counted against the account

    def test_the_second_difference_goes_through_for_the_staff_with_a_note(self):
        self.reads(good_reading())
        browser, _ = self.ask_then_go_through(gcash_reference=OTHER_REF)
        reservation = self.booked(self.checkout("a", client=browser, gcash_reference=OTHER_REF))
        self.assertEqual(reservation.proof_check, Reservation.ProofCheck.NEEDS_CHECK)
        self.assertEqual(reservation.proof_match, "reference")
        self.assertEqual((reservation.proof_typed_reference, reservation.gcash_reference), (OTHER_REF, REF))
        self.assertIn("The customer typed the reference 5046 933 479720", reservation.proof_notes)
        self.assertIn("the receipt reads the reference 5046 933 479702", reservation.proof_notes)
        self.assertIn("Needs check", reservation.status_events.get(label="Payment receipt checked").detail)
        self.assertEqual(ProofRejection.objects.count(), 0)

    def test_fixing_the_typo_on_the_second_try_is_a_plain_match(self):
        self.reads(good_reading())
        browser, _ = self.ask_then_go_through(gcash_reference=OTHER_REF)
        reservation = self.booked(self.checkout("a", client=browser))                # now types the right number
        self.assertEqual((reservation.proof_check, reservation.proof_match), (Reservation.ProofCheck.PASS, "match"))

    def test_the_amount_and_both_boxes_are_named_too(self):
        self.reads(good_reading())
        _, asked = self.ask_then_go_through(gcash_amount="2500")
        self.assertEqual((asked.json()["typed_mismatch"], asked.json()["field"]), (True, "amount"))
        _, both = self.ask_then_go_through(gcash_reference=OTHER_REF, gcash_amount="2500")
        self.assertEqual(both.json()["field"], "both")
        self.nothing_booked()

    def test_the_second_try_may_differ_in_another_box_and_still_goes_to_the_staff(self):
        self.reads(good_reading())
        browser, _ = self.ask_then_go_through(gcash_amount="2500")
        reservation = self.booked(self.checkout("a", client=browser, gcash_reference=OTHER_REF))
        self.assertEqual((reservation.proof_check, reservation.proof_match), (Reservation.ProofCheck.NEEDS_CHECK, "reference"))

    def test_each_browser_keeps_its_own_count(self):
        self.reads(good_reading())
        _, first = self.ask_then_go_through(gcash_reference=OTHER_REF)
        _, other_browser = self.ask_then_go_through(gcash_reference=OTHER_REF)              # a different browser session: asked again, not booked
        self.assertEqual((first.status_code, other_browser.status_code), (400, 400))
        self.nothing_booked()

    def test_after_a_booking_the_next_checkout_starts_again_from_the_first_question(self):
        self.reads(good_reading())
        browser, _ = self.ask_then_go_through(gcash_reference=OTHER_REF)
        self.booked(self.checkout("a", client=browser, gcash_reference=OTHER_REF))
        self.reader.return_value = (good_reading(reference="1111111111111"), "")
        again = self.checkout("a", (1,), client=browser, gcash_reference="1111111111110")
        self.assertEqual((again.status_code, again.json()["typed_mismatch"]), (400, True))

    def test_a_new_checkout_hold_starts_again_from_the_first_question(self):
        self.reads(good_reading())
        browser = Client()
        browser.force_login(self.customers["a"])
        agreed(self.customers["a"])
        browser.post(reverse("gowns:reservation_hold_start"))
        asked = self.checkout("a", client=browser, gcash_reference=OTHER_REF)
        self.assertEqual(asked.status_code, 400)
        session = browser.session
        keys = list(session["typed_payment_attempts"])
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].startswith("checkout:2"))                            # tied to the hold's own deadline
        browser.post(reverse("gowns:reservation_hold_release"))
        browser.post(reverse("gowns:reservation_hold_start"))                        # a new hold: a new deadline, so a new count
        again = self.checkout("a", client=browser, gcash_reference=OTHER_REF)
        self.assertEqual((again.status_code, again.json().get("typed_mismatch")), (400, True))

    def test_a_refusal_always_comes_first_whatever_was_typed(self):
        self.reads(good_reading(recipient_number="09265411484"))                    # paid to somebody else
        data = self.refused(self.checkout("a", gcash_reference=OTHER_REF))
        self.assertIn("not sent to Arabela", data["error"])
        self.nothing_booked()
        self.assertEqual(ProofRejection.objects.count(), 1)

    def test_typing_can_never_turn_a_needs_check_receipt_into_a_pass(self):
        self.reads(good_reading(recipient_name="MA****A L."))                       # a different name on the right number: staff must look
        reservation = self.booked(self.checkout("a"))
        self.assertEqual((reservation.proof_check, reservation.proof_match), (Reservation.ProofCheck.NEEDS_CHECK, "match"))


class UploadingTheReceiptLaterTests(ReceiptCheckBase):
    def a_booking_without_proof(self):
        reservation = Reservation.objects.create(
            customer=self.customers["a"], customer_name="Rita A", status=Reservation.Status.PENDING,
            security_deposit=Decimal("2000"), rental_subtotal=Decimal("3000"), total_amount=Decimal("5000"))
        item = ReservationItem.objects.create(
            reservation=reservation, gown=self.gowns[2], gown_name=self.gowns[2].name, size="Medium", rental_price=Decimal("3000"),
            rental_date=self.event - timedelta(days=2), return_date=self.event + timedelta(days=2))
        return reservation, item

    def post(self, item, client=None, **typed):
        client = client or Client()
        client.force_login(self.customers["a"])
        agreed(self.customers["a"])
        return client.post(reverse("gowns:reservation_upload_proof", args=[item.pk]), data={
            "proof_of_payment": SimpleUploadedFile("late.jpg", unique_jpeg(), content_type="image/jpeg"), **self.typed(2000, **typed)})

    def test_matching_numbers_are_stored(self):
        reservation, item = self.a_booking_without_proof()
        self.reads(good_reading())
        response = self.post(item)
        self.assertEqual(response.status_code, 200, response.content)
        reservation.refresh_from_db()
        self.assertEqual((reservation.proof_check, reservation.proof_match), (Reservation.ProofCheck.PASS, "match"))
        self.assertEqual((reservation.proof_typed_reference, reservation.proof_typed_amount), (REF, Decimal("2000.00")))

    def test_the_boxes_are_checked_first_here_too(self):
        reservation, item = self.a_booking_without_proof()
        self.reads(good_reading())
        for field, typed in (("reference", {"gcash_reference": "586757758"}), ("amount", {"gcash_amount": "100"})):
            with self.subTest(field=field):
                response = self.post(item, **typed)
                self.assertEqual((response.status_code, response.json()["typed_invalid"], response.json()["field"]), (400, True, field))
        self.reader.assert_not_called()
        reservation.refresh_from_db()
        self.assertEqual((reservation.payment_proof_url, reservation.proof_check), ("", ""))
        self.assertEqual(ProofRejection.objects.count(), 0)

    def test_a_page_that_sends_neither_box_is_told_to_reload(self):
        reservation, item = self.a_booking_without_proof()
        self.reads(good_reading())
        client = Client()
        client.force_login(self.customers["a"])
        agreed(self.customers["a"])
        response = client.post(reverse("gowns:reservation_upload_proof", args=[item.pk]), data={
            "proof_of_payment": SimpleUploadedFile("late.jpg", unique_jpeg(), content_type="image/jpeg")})
        self.assertEqual((response.status_code, response.json()["typed_required"]), (400, True))

    def test_a_reference_on_another_booking_is_refused_but_never_the_booking_itself(self):
        reservation, item = self.a_booking_without_proof()
        Reservation.objects.filter(pk=reservation.pk).update(proof_typed_reference=REF)       # its own earlier number must not count against it
        self.reads(good_reading())
        self.assertEqual(self.post(item).status_code, 200)
        reservation.refresh_from_db()
        other = Reservation.objects.create(customer=self.customers["b"], customer_name="B", status=Reservation.Status.PENDING,
                                           payment_proof_url="", security_deposit=Decimal("2000"))
        item_b = ReservationItem.objects.create(reservation=other, gown=self.gowns[3], gown_name="G", size="Medium", rental_price=Decimal("3000"),
                                                rental_date=self.event - timedelta(days=2), return_date=self.event + timedelta(days=2))
        client = Client()
        client.force_login(self.customers["b"])
        agreed(self.customers["b"])
        response = client.post(reverse("gowns:reservation_upload_proof", args=[item_b.pk]), data={
            "proof_of_payment": SimpleUploadedFile("late.jpg", unique_jpeg(), content_type="image/jpeg"), **self.typed(2000)})
        self.assertEqual(response.status_code, 400)
        self.assertIn("already used for another booking", response.json()["error"])

    def test_a_difference_is_a_question_once_and_then_goes_to_the_staff(self):
        reservation, item = self.a_booking_without_proof()
        self.reads(good_reading())
        browser = Client()
        asked = self.post(item, client=browser, gcash_reference=OTHER_REF)
        self.assertEqual((asked.status_code, asked.json()["typed_mismatch"], asked.json()["field"]), (400, True, "reference"))
        reservation.refresh_from_db()
        self.assertEqual((reservation.payment_proof_url, reservation.proof_check), ("", ""))
        self.assertEqual(ProofRejection.objects.count(), 0)
        done = self.post(item, client=browser, gcash_reference=OTHER_REF)
        self.assertEqual(done.status_code, 200, done.content)
        reservation.refresh_from_db()
        self.assertEqual((reservation.proof_check, reservation.proof_match), (Reservation.ProofCheck.NEEDS_CHECK, "reference"))
        self.assertEqual((reservation.proof_typed_reference, reservation.gcash_reference), (OTHER_REF, REF))
