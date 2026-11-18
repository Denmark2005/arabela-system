"""The reference number and amount a customer types about their payment (gowns/typed_payment.py): the rules on their own, with no database and
no network. The checkout and the upload-later step that use them are tested in test_typed_checkout.py."""
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from gowns import typed_payment as tp


def verdict(reference="5046933479702", amount=Decimal("2000.00")):
    return SimpleNamespace(reference=reference, amount=amount)


class TheReferenceTests(SimpleTestCase):
    def test_only_the_digits_are_kept(self):
        for typed in ("5046933479702", "5046 933 479702", "  5046 9334 79702  ", "5046-933-479702", "Ref No. 5046 933 479702", "#5046933479702",
                      "５０４６９３３４７９７０２"):           # full-width digits from some phone keyboards
            with self.subTest(typed=typed):
                self.assertEqual(tp.clean_reference(typed), "5046933479702")
        self.assertEqual(tp.clean_reference(None), "")
        self.assertEqual(tp.clean_reference("abc"), "")

    def test_it_is_written_the_way_gcash_prints_it(self):
        self.assertEqual(tp.format_reference("5046933479702"), "5046 933 479702")
        self.assertEqual(tp.format_reference("5046 933 479702"), "5046 933 479702")
        self.assertEqual(tp.format_reference("586757758"), "586757758")                  # not 13 digits: shown as it is

    def test_exactly_thirteen_digits_are_needed(self):
        self.assertEqual(tp.reference_problem("5046 933 479702"), "")
        self.assertIn("13-digit reference number", tp.reference_problem(""))
        self.assertIn("13-digit reference number", tp.reference_problem("abc"))
        for typed, count in (("586757758", 9), ("1" * 12, 12), ("1" * 14, 14), ("1" * 25, 25)):
            with self.subTest(typed=typed):
                message = tp.reference_problem(typed)
                self.assertIn("has 13 digits", message)
                self.assertIn(f"you typed {count}", message)

    def test_a_gcash_bill_payment_reference_is_turned_away_at_the_typing(self):
        # the school tuition receipt that was once accepted showed a 9-digit reference
        self.assertIn("you typed 9", tp.reference_problem("586 757 758"))


class TheAmountTests(SimpleTestCase):
    def test_plain_amounts_are_understood(self):
        cases = {"2000": "2000.00", "2,000": "2000.00", "2,000.00": "2000.00", "₱2,000.00": "2000.00", "PHP 2000": "2000.00", "php2000.5": "2000.50",
                 " 2 000 ": "2000.00", "1": "1.00", "0.5": "0.50", "999999": "999999.00"}
        for typed, expected in cases.items():
            with self.subTest(typed=typed):
                self.assertEqual(tp.parse_amount(typed), Decimal(expected))

    def test_anything_that_is_not_a_plain_amount_is_refused(self):
        for typed in ("", "   ", "abc", "-5", "0", "0.00", "2000.123", "2.000,00", "1e3", "two thousand", "20 00 abc", "₱", "1000001", None):
            with self.subTest(typed=typed):
                self.assertIsNone(tp.parse_amount(typed))

    def test_the_amount_must_be_a_number_of_at_least_the_deposit(self):
        self.assertEqual(tp.amount_problem("2000", Decimal("2000")), "")
        self.assertEqual(tp.amount_problem("2,500", Decimal("2000")), "")             # more than the deposit is not a problem here
        self.assertIn("Please type the amount you paid", tp.amount_problem("", Decimal("2000")))
        self.assertIn("numbers only", tp.amount_problem("abc", Decimal("2000")))
        low = tp.amount_problem("500", Decimal("2000"))
        self.assertIn("You typed ₱500", low)
        self.assertIn("deposit for this booking is ₱2,000", low)
        self.assertIn("₱2,000.50", tp.amount_problem("1", Decimal("2000.50")))          # centavos are shown when there are some

    def test_pesos_are_written_plainly(self):
        self.assertEqual(tp.peso(Decimal("2000")), "₱2,000")
        self.assertEqual(tp.peso(Decimal("2000.50")), "₱2,000.50")
        self.assertEqual(tp.peso(Decimal("5")), "₱5")


class TheComparisonTests(SimpleTestCase):
    def test_equal_numbers_match(self):
        self.assertEqual(tp.compare("5046933479702", Decimal("2000"), verdict()), tp.MATCH)
        self.assertEqual(tp.compare("5046 933 479702", Decimal("2000.00"), verdict("5046 933 479702")), tp.MATCH)

    def test_each_difference_is_named(self):
        self.assertEqual(tp.compare("5046933479720", Decimal("2000"), verdict()), tp.REFERENCE_DIFFERS)
        self.assertEqual(tp.compare("5046933479702", Decimal("2500"), verdict()), tp.AMOUNT_DIFFERS)
        self.assertEqual(tp.compare("5046933479720", Decimal("2500"), verdict()), tp.BOTH_DIFFER)

    def test_a_field_the_reader_could_not_find_is_not_a_difference(self):
        self.assertEqual(tp.compare("5046933479702", Decimal("2000"), verdict(amount=None)), tp.MATCH)
        self.assertEqual(tp.compare("5046933479702", Decimal("2000"), verdict(reference="")), tp.MATCH)
        self.assertEqual(tp.compare("5046933479702", Decimal("2000"), verdict(reference="", amount=None)), tp.MATCH)
        self.assertEqual(tp.compare("5046933479720", Decimal("2000"), verdict(amount=None)), tp.REFERENCE_DIFFERS)

    def test_the_amount_is_compared_to_the_centavo(self):
        self.assertEqual(tp.compare("5046933479702", Decimal("2000.01"), verdict()), tp.AMOUNT_DIFFERS)
        self.assertEqual(tp.compare("5046933479702", Decimal("2000.00"), verdict(amount=Decimal("2000"))), tp.MATCH)

    def test_the_customer_is_told_which_box_differs_but_never_what_the_receipt_reads(self):
        for kind, field, words in ((tp.REFERENCE_DIFFERS, "reference", "reference number you typed"), (tp.AMOUNT_DIFFERS, "amount", "amount you typed"),
                                   (tp.BOTH_DIFFER, "both", "reference number and the amount")):
            with self.subTest(kind=kind):
                message = tp.mismatch_message(kind)
                self.assertIn(words, message)
                self.assertEqual(tp.mismatch_field(kind), field)
                self.assertNotRegex(message, r"\d{4}")                     # no digits of the receipt are given away
        self.assertEqual(tp.mismatch_field(tp.MATCH), "")

    def test_the_staff_note_gives_both_sides(self):
        note = tp.mismatch_note(tp.REFERENCE_DIFFERS, "5046933479720", Decimal("2000"), verdict())
        self.assertIn("The customer typed the reference 5046 933 479720 and ₱2,000", note)
        self.assertIn("the receipt reads the reference 5046 933 479702 and ₱2,000", note)
        self.assertIn("reference number typed does not match the picture after two tries", note)
        both = tp.mismatch_note(tp.BOTH_DIFFER, "5046933479720", Decimal("2500"), verdict())
        self.assertIn("reference number and amount typed", both)
        nothing = tp.mismatch_note(tp.AMOUNT_DIFFERS, "5046933479702", Decimal("2500"), verdict(reference="", amount=None))
        self.assertIn("the receipt reads something else", nothing)


class TheTriesTests(SimpleTestCase):
    def test_a_difference_is_remembered_per_checkout_and_can_be_forgotten(self):
        session = {}
        self.assertEqual(tp.tries(session, "checkout:a"), 0)
        self.assertEqual(tp.remember_difference(session, "checkout:a"), 1)
        self.assertEqual(tp.tries(session, "checkout:a"), 1)
        self.assertEqual(tp.tries(session, "checkout:b"), 0)                    # another checkout starts again from zero
        self.assertEqual(tp.remember_difference(session, "checkout:a"), 2)
        tp.forget(session, "checkout:a")
        self.assertEqual(tp.tries(session, "checkout:a"), 0)
        tp.forget(session, "never-set")                                          # forgetting what is not there is fine

    def test_damaged_session_data_is_ignored(self):
        for junk in ("text", 5, ["x"], None):
            with self.subTest(junk=junk):
                session = {tp.SESSION_KEY: junk}
                self.assertEqual(tp.tries(session, "k"), 0)
                self.assertEqual(tp.remember_difference(session, "k"), 1)
