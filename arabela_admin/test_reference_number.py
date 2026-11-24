"""The GCash reference number in the admin panel: shown big and clear in every payment proof window, at a readable size on the Payment Verification
lines, and searchable (as the owner pastes it from her GCash app) in the search boxes, so she can find who paid.

The number shown is the one the customer typed; when it differs from the receipt picture both are shown (Reservation.reference_info)."""
from datetime import datetime
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from arabela_admin import views
from arabela_admin.test_owner_payment_check import OwnerCheckBase
from reservations.models import Reservation

REF = "5046933479702"
GROUPED = "5046 933 479702"
OTHER = "5046933479720"

# The pages with a payment proof window (Security Deposits had one too until it became the slim page: it has no proof window now).
PAGES = ("arabela_admin:pending_approval", "arabela_admin:payment_verification", "arabela_admin:dashboard")


class TheProofWindowsShowTheNumberTests(OwnerCheckBase):
    def page(self, name):
        self.client.force_login(self.owner)
        response = self.client.get(reverse(name))
        self.assertEqual(response.status_code, 200, name)
        return response.content.decode()

    def test_every_proof_window_has_the_one_shared_reference_card_and_no_small_leftover(self):
        self.booking(gcash_reference=REF, proof_typed_reference=REF, proof_typed_amount=Decimal("2000"), proof_match="match")
        for name in PAGES:
            with self.subTest(page=name):
                html = self.page(name)
                self.assertEqual(html.count("data-gcash-reference-block"), 1, "the card is in the window once")
                self.assertIn("GCash reference number", html)
                self.assertIn("arabelaCopyText", html)
                self.assertNotIn("GCash Reference No.", html)                    # the old small cell is gone from the window

    def test_the_booking_hands_its_reference_to_the_window(self):
        self.booking(0, gcash_reference=REF, proof_typed_reference=REF, proof_typed_amount=Decimal("2000"), proof_match="match", status=Reservation.Status.PENDING)
        # an approved booking, which Security Deposits lists (it shows no proof window, so it does not need the reference)
        self.booking(1, gcash_reference=REF, proof_typed_reference=REF, proof_typed_amount=Decimal("2000"), proof_match="match",
                     status=Reservation.Status.CONFIRMED, owner=Reservation.OwnerCheck.RECEIVED)
        for name in PAGES:
            with self.subTest(page=name):
                html = self.page(name)
                self.assertIn("refInfo: JSON.parse(", html)
                self.assertIn(GROUPED, html)
                self.assertIn(REF, html)

    def test_a_booking_without_a_reference_hands_over_nothing(self):
        self.booking()
        html = self.page("arabela_admin:payment_verification")
        self.assertIn("refInfo: JSON.parse('null')", html)
        self.assertNotIn(GROUPED, html)

    def test_a_difference_hands_over_both_numbers(self):
        self.booking(gcash_reference=REF, proof_typed_reference=OTHER, proof_typed_amount=Decimal("2500"), proof_amount=Decimal("2000"), proof_match="both",
                     proof_check=Reservation.ProofCheck.NEEDS_CHECK)
        html = self.page("arabela_admin:pending_approval")
        for needle in ("5046 933 479720", "5046 933 479702", "referenceDiffers", "amountDiffers"):
            self.assertIn(needle, html)
        self.assertIn("Typed numbers differ", html)                               # the chip in the reference card of the window

    def test_the_payment_verification_line_shows_the_number_at_a_readable_size_on_its_own_line(self):
        self.booking(gcash_reference=REF, proof_typed_reference=REF, proof_match="match")
        html = self.page("arabela_admin:payment_verification")
        self.assertIn(f'<div class="pv-refno" title="GCash reference number">Ref. {GROUPED}</div>', html)
        self.assertNotIn(f"GCash &middot; {REF}", html)                             # not squeezed into the small note any more
        css = html[html.index(".pv-refno {"):]
        self.assertIn("font-size: .8125rem", css[:200])                           # 13 px, not the 11.5 px of the small notes

    def test_the_card_is_the_first_thing_in_the_window_above_the_picture(self):
        self.booking(gcash_reference=REF, proof_typed_reference=REF, proof_match="match")
        for name in PAGES:
            with self.subTest(page=name):
                html = self.page(name)
                card = html.index('data-gcash-reference-block')
                picture = html.index('alt="Payment proof"')
                self.assertLess(card, picture)


class TheListsFindABookingByItsReferenceTests(OwnerCheckBase):
    def page(self, name):
        self.client.force_login(self.owner)
        return self.client.get(reverse(name)).content.decode()

    def test_the_search_words_of_a_line_carry_the_plain_and_the_grouped_number(self):
        self.booking(gcash_reference=REF, proof_typed_reference=REF, proof_match="match")
        for name in ("arabela_admin:pending_approval", "arabela_admin:payment_verification"):
            with self.subTest(page=name):
                html = self.page(name)
                self.assertIn(f"{REF} 5046 933 479702", html)
                self.assertIn("GCash reference", html)                              # the search box says so

    def test_a_typed_number_that_differs_is_found_under_both(self):
        self.booking(gcash_reference=REF, proof_typed_reference=OTHER, proof_match="reference", proof_check=Reservation.ProofCheck.NEEDS_CHECK)
        html = self.page("arabela_admin:pending_approval")
        for needle in (REF, OTHER, "5046 933 479720", "5046 933 479702"):
            self.assertIn(needle, html)

    def test_reservation_records_matches_the_number_too(self):
        self.booking(gcash_reference=REF, proof_typed_reference=REF, proof_match="match")
        html = self.page("arabela_admin:reservation_records")
        self.assertIn(REF, html)
        self.assertIn(GROUPED, html)

    def test_reservation_records_shows_the_number_as_the_first_line_of_the_payment_breakdown(self):
        self.booking(gcash_reference=REF, proof_typed_reference=REF, proof_match="match")
        html = self.page("arabela_admin:reservation_records")
        self.assertIn('class="rrd-refrow" x-show="viewing.gcashReference"', html)
        self.assertLess(html.index('class="rrd-refrow"'), html.index("<dt>Rental fee</dt>"))
        self.assertIn("gcashReference", html)
        self.assertIn(GROUPED, html)
        self.assertIn("font-size: 1.0625rem; font-weight: 700", html[html.index(".rrd-lines .rrd-refno"):][:200])

    def test_a_booking_without_a_reference_still_renders_the_page(self):
        self.booking()
        html = self.page("arabela_admin:reservation_records")
        self.assertIn('class="rrd-refrow"', html)          # the line is in the page; the window hides it for a booking with no number


class TheSearchBoxesAreAskedForANumberTests(OwnerCheckBase):
    def search(self, q, user=None, name="arabela_admin:receipt_reservation_search"):
        self.client.force_login(user or self.owner)
        response = self.client.get(reverse(name), {"q": q})
        self.assertEqual(response.status_code, 200, (q, response.content))
        return response.json()["results"]

    def test_the_owner_pastes_the_number_the_way_her_gcash_app_shows_it(self):
        reservation, _ = self.booking(gcash_reference=REF, proof_typed_reference=REF, proof_match="match")
        for q in (REF, GROUPED, "5046-933-479702", "933 479702", "479702", "  5046 933 479702  "):
            with self.subTest(q=q):
                results = self.search(q)
                self.assertEqual([r["reference"] for r in results], [reservation.reference_code])

    def test_a_number_the_customer_typed_wrongly_is_found_too_and_so_is_the_one_on_the_picture(self):
        reservation, _ = self.booking(gcash_reference=REF, proof_typed_reference=OTHER, proof_match="reference")
        for q in (REF, OTHER):
            with self.subTest(q=q):
                self.assertEqual([r["reference"] for r in self.search(q)], [reservation.reference_code])

    def test_a_number_nobody_paid_finds_nothing(self):
        self.booking(gcash_reference=REF, proof_typed_reference=REF, proof_match="match")
        self.assertEqual(self.search("1111111111111"), [])
        self.assertEqual(self.search("111111"), [])

    def test_too_short_a_number_or_a_name_or_a_booking_code_still_work_as_before(self):
        reservation, _ = self.booking(gcash_reference=REF, proof_typed_reference=REF, proof_match="match")
        self.assertEqual(self.search("50469"), [])                                  # 5 digits are not searched as a reference
        self.assertEqual([r["reference"] for r in self.search(reservation.reference_code)], [reservation.reference_code])
        self.assertEqual([r["reference"] for r in self.search("Rita")], [reservation.reference_code])

    def test_two_bookings_with_the_same_trailing_digits_are_both_listed(self):
        first, _ = self.booking(0, gcash_reference=REF, proof_typed_reference=REF, proof_match="match")
        second, _ = self.booking(1, gcash_reference="1111111479702", proof_typed_reference="1111111479702", proof_match="match")
        self.assertEqual({r["reference"] for r in self.search("479702")}, {first.reference_code, second.reference_code})

    def test_staff_can_search_but_a_customer_cannot(self):
        self.booking(gcash_reference=REF, proof_typed_reference=REF, proof_match="match")
        self.assertEqual(len(self.search(REF, user=self.staff)), 1)
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(reverse("arabela_admin:receipt_reservation_search"), {"q": REF}).status_code, 401)

    def test_the_top_bar_search_sends_the_owner_to_the_page_filtered_by_the_number(self):
        reservation, _ = self.booking(gcash_reference=REF, proof_typed_reference=REF, proof_match="match")
        results = self.search(GROUPED, name="arabela_admin:admin_search")
        mine = [r for r in results if r.get("reference_code") == reservation.reference_code]
        self.assertEqual(len(mine), 1)
        self.assertIn("search=5046933479702", mine[0]["target_url"])
        self.assertIn(reverse("arabela_admin:pending_approval"), mine[0]["target_url"])
        by_name = self.search("Rita", name="arabela_admin:admin_search")
        self.assertIn("search=Rita", [r for r in by_name if r.get("reference_code") == reservation.reference_code][0]["target_url"])


class TheSameNumberOnTwoBookingsTests(OwnerCheckBase):
    def test_the_warning_sees_both_the_typed_number_and_the_one_on_the_picture(self):
        a, _ = self.booking(0, gcash_reference=REF, proof_typed_reference=REF, proof_match="match")
        b, _ = self.booking(1, gcash_reference="1111111111111", proof_typed_reference=REF, proof_match="reference")
        c, _ = self.booking(2, gcash_reference="2222222222222", proof_typed_reference="3333333333333", proof_match="reference")
        d, _ = self.booking(3)
        found = views._same_gcash_number_elsewhere([a, b, c, d])
        self.assertEqual(set(found[a.id].split(", ")), {b.reference_code})
        self.assertEqual(set(found[b.id].split(", ")), {a.reference_code})
        self.assertEqual(found[c.id], "")
        self.assertNotIn(d.id, found)
        self.assertEqual(views._same_gcash_number_elsewhere([]), {})
        self.assertEqual(views._same_gcash_number_elsewhere([d]), {})


class TheOwnersCardShowsTheDateAndTheReasonTests(OwnerCheckBase):
    """2026-11-19: an old receipt's reference is real in the owner's GCash, so the card must show the date and why the check flagged it, and the green
    chip must never read as "payment OK"."""

    def page(self, name):
        self.client.force_login(self.owner)
        response = self.client.get(reverse(name))
        self.assertEqual(response.status_code, 200, name)
        return response.content.decode()

    def old_booking(self, **fields):
        reservation, item = self.booking(
            gcash_reference="9046723594550", proof_typed_reference="9046723594550", proof_typed_amount=Decimal("500"), proof_amount=Decimal("500"),
            proof_match="match", proof_check=Reservation.ProofCheck.NEEDS_CHECK,
            proof_paid_at=timezone.make_aware(datetime(2026, 10, 31, 10, 20)),
            proof_notes="The reader would have refused this receipt (receipt too old), but a second reading could not be made (the first reading had already used the time), so it was sent to the staff to look at.",
            **fields)
        Reservation.objects.filter(pk=reservation.pk).update(created_at=timezone.make_aware(datetime(2026, 11, 18, 23, 17)))
        return reservation

    def test_every_window_carries_the_date_the_old_flag_and_the_reason(self):
        self.old_booking()
        self.old_booking(status=Reservation.Status.CONFIRMED, owner=Reservation.OwnerCheck.RECEIVED)           # an approved one too
        for name in PAGES:
            with self.subTest(page=name):
                html = self.page(name)
                self.assertIn("Oct 31, 2026, 10:20 AM", html)
                self.assertIn("paidOld", html)
                self.assertIn("would have refused this receipt", html)
                self.assertIn('class="gr-paid"', html)
                self.assertIn('class="gr-warn"', html)

    def test_the_chip_never_says_the_old_green_words_any_more(self):
        self.old_booking()
        for name in PAGES:
            with self.subTest(page=name):
                html = self.page(name)
                self.assertNotIn("Matches the receipt", html)
                self.assertIn("Typed number matches the picture", html)

    def test_the_chip_is_green_only_when_the_check_passed(self):
        self.old_booking()
        html = self.page("arabela_admin:pending_approval")
        green = "gr-chip gr-chip--ok\" x-show=\"viewingPayment.refInfo.typed && viewingPayment.refInfo.match === 'match' && viewingPayment.refInfo.check === 'pass'\""
        neutral = "gr-chip gr-chip--muted\" x-show=\"viewingPayment.refInfo.typed && viewingPayment.refInfo.match === 'match' && viewingPayment.refInfo.check !== 'pass'\""
        self.assertIn(green, html)
        self.assertIn(neutral, html)

    def test_the_old_date_is_red_and_the_reason_is_amber(self):
        self.old_booking()
        html = self.page("arabela_admin:payment_verification")
        css = html[html.index(".gr-paid {"):]
        self.assertIn(".gr-paid.is-old { color: #b42318; }", css[:900])
        self.assertIn(".gr-warn {", css)
        self.assertIn("background: #fffaeb", css[css.index(".gr-warn {"):][:260])
        self.assertIn(".dark .gr-paid.is-old", css)

    def test_the_help_sentence_is_only_for_a_booking_with_nothing_to_warn_about(self):
        html = self.page("arabela_admin:payment_verification")
        self.assertIn("refInfo.notes || []).length)\">Look for this number in the GCash transactions", html.replace("\r\n", "\n").replace("\n", ""))
