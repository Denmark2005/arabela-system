"""The receipt check, end to end through the real checkout and the real "upload proof later" step.

The reader (Google Gemini) is replaced by a stand-in that returns what it "saw" in the picture, so the tests are free and exact; the RULES
that judge the reading are the real ones (their full matrix is in test_receipt_rules.py). What these prove is what a customer, the staff
and the dates actually experience:

  * a refused receipt books NOTHING, holds NO dates, and counts against the account;
  * a good receipt (or one the staff must look at) becomes a Pending booking with the facts the check found stored on it;
  * the same reference / the same picture cannot pay for a second booking;
  * 4 refused proofs inside 7 days pause the account until the owner lifts it;
  * a reader that is down never blocks a customer.
"""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts import services
from accounts.models import CustomerMessage, UserProfile
from accounts.test_support import agreed
from gowns import gcash_numbers, receipt_check as rc
from gowns.models import Gown, GownUnavailability, SiteSettings
from gowns.sample_images import unique_jpeg
from reservations.models import ProofRejection, Reservation, ReservationItem

User = get_user_model()
SHOP = "09488745992"
OLD_SHOP = "09171234567"


def make_gown(number, name="Receipt Gown"):
    return Gown.objects.create(
        gown_id=f"Wedding Gown-RC-{number:03d}", name=f"{name} {number}", category=Gown.Category.WEDDING_GOWN, color_name="White",
        color_code="WH", size=Gown.Size.MEDIUM, rental_price=Decimal("3000"), status=Gown.Status.AVAILABLE)


def good_reading(**changes):
    """What the reader would see on a perfect GCash receipt for the shop (paid 2 minutes ago)."""
    values = dict(
        is_payment_screen=True, is_transaction_list=False, app="gcash", direction="sent", recipient_number=SHOP, recipient_name="DE****K C.",
        sender_number="09167957938", amount=Decimal("2000.00"), reference="5046933479702", paid_at=timezone.now() - timedelta(minutes=2),
        status="completed", gcash_signs=["Express Send"],
    )
    values.update(changes)
    return rc.Reading(**values)


class ReceiptCheckBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customers = {key: User.objects.create_user(f"rc_buyer_{key}", f"rc_buyer_{key}@example.com", "Pass-12345!") for key in "abcd"}
        cls.gowns = [make_gown(n) for n in range(1, 9)]
        shop = SiteSettings.load()
        shop.gcash_number = SHOP
        shop.gcash_account_name = "Denmark Concepcion"
        shop.save()

    def setUp(self):
        saver = patch("gowns.views._save_proof_file", return_value="https://example.test/fake-proof.jpg")
        saver.start()
        self.addCleanup(saver.stop)
        self.event = timezone.localdate() + timedelta(days=15)
        self.reader = None

    # ---- the stand-in reader ----
    def reads(self, reading, why=""):
        """From now on the reader 'sees' `reading` in every picture (None = the reader is down)."""
        patcher = patch("gowns.receipt_check.read_receipt", return_value=(reading, why))
        self.reader = patcher.start()
        self.addCleanup(patcher.stop)

    # ---- the checkout ----
    def checkout(self, who, gown_indexes=(0,), *, picture=None, event=None):
        event = event or self.event
        items = [{"gown_name": self.gowns[i].name, "gown_slug": self.gowns[i].slug, "size": "Medium",
                  "rental_date": (event - timedelta(days=2)).isoformat(), "return_date": (event + timedelta(days=2)).isoformat()}
                 for i in gown_indexes]
        client = Client()
        client.force_login(self.customers[who])
        agreed(self.customers[who])
        return client.post(reverse("gowns:reservation_submit"), data={
            "items": json.dumps(items), "first_name": "Rita", "last_name": who.upper(), "phone": "09171234567", "address": "1 Test St",
            "city": "Test City", "postal_code": "1000", "payment_method": "GCash",
            "proof_of_payment": SimpleUploadedFile("proof.jpg", picture or unique_jpeg(), content_type="image/jpeg"),
            "expected_deposit": str(2000 * len(gown_indexes)),
        })

    def booked(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        return Reservation.objects.get(reference_code=response.json()["reference_code"])

    def refused(self, response):
        self.assertEqual(response.status_code, 400, response.content)
        data = response.json()
        self.assertFalse(data["success"])
        self.assertTrue(data["proof_rejected"])
        return data

    def nothing_booked(self):
        self.assertEqual(Reservation.objects.count(), 0)
        self.assertEqual(ReservationItem.objects.count(), 0)
        self.assertEqual(GownUnavailability.objects.count(), 0)


class AGoodReceiptTests(ReceiptCheckBase):
    def test_a_good_receipt_books_and_stores_what_the_check_found(self):
        self.reads(good_reading())
        reservation = self.booked(self.checkout("a"))
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertEqual(reservation.proof_check, Reservation.ProofCheck.PASS)
        self.assertEqual(reservation.gcash_reference, "5046933479702")
        self.assertEqual(reservation.proof_amount, Decimal("2000.00"))
        self.assertEqual(reservation.proof_recipient, SHOP)
        self.assertFalse(reservation.proof_old_number)
        self.assertEqual(len(reservation.proof_hash), 64)
        self.assertTrue(reservation.proof_fingerprint)
        self.assertEqual(reservation.owner_check, Reservation.OwnerCheck.PENDING)       # the owner still has to look in her GCash
        self.assertIn("5046933479702", reservation.proof_reading)
        event = reservation.status_events.get(label="Payment receipt checked")
        self.assertTrue(event.staff_only)
        self.assertIn("Pre-checked", event.detail)

    def test_the_gowns_are_held_once_the_receipt_passes(self):
        self.reads(good_reading())
        self.booked(self.checkout("a"))
        self.assertTrue(GownUnavailability.objects.filter(gown=self.gowns[0]).exists())      # the automatic rest after the return

    def test_a_little_over_the_deposit_passes_with_a_note_for_the_staff(self):
        self.reads(good_reading(amount=Decimal("2050")))
        reservation = self.booked(self.checkout("a"))
        self.assertEqual(reservation.proof_check, Reservation.ProofCheck.PASS)
        self.assertIn("+₱50 more", reservation.proof_notes)

    def test_the_old_shop_number_inside_its_grace_passes_and_is_marked_for_the_staff(self):
        gcash_numbers.record_change(OLD_SHOP, SHOP, 48, now=timezone.now() - timedelta(hours=5))
        self.reads(good_reading(recipient_number=OLD_SHOP, recipient_name="", paid_at=timezone.now() - timedelta(hours=6)))
        reservation = self.booked(self.checkout("a"))
        self.assertEqual(reservation.proof_check, Reservation.ProofCheck.PASS)
        self.assertTrue(reservation.proof_old_number)
        self.assertIn("OLD GCash number", reservation.proof_notes)
        self.assertIn("Old GCash number", [text for kind, text in reservation.proof_badges])


class ARefusedReceiptTests(ReceiptCheckBase):
    def assert_refused_with(self, reading, text):
        self.reads(reading)
        data = self.refused(self.checkout("a"))
        self.assertIn(text, data["error"])
        self.assertFalse(data["account_blocked"])
        self.nothing_booked()
        self.assertEqual(ProofRejection.objects.filter(user=self.customers["a"], source=ProofRejection.Source.UPLOAD).count(), 1)

    def test_a_picture_that_is_not_a_payment(self):
        self.assert_refused_with(good_reading(is_payment_screen=False), "doesn't look like a payment receipt")

    def test_a_list_of_transactions(self):
        self.assert_refused_with(good_reading(is_payment_screen=False, is_transaction_list=True), "open your payment")

    def test_a_maya_receipt(self):
        self.assert_refused_with(good_reading(app="maya"), "Only GCash receipts are accepted")

    def test_money_received(self):
        self.assert_refused_with(good_reading(direction="received"), "money you received")

    def test_a_store_payment(self):
        self.assert_refused_with(good_reading(direction="merchant_payment", recipient_number=""), "store or biller")

    def test_no_reference_number(self):
        self.assert_refused_with(good_reading(reference=""), "Ref No.")

    def test_another_persons_number(self):
        self.assert_refused_with(good_reading(recipient_number="09265411484"), "not sent to Arabela's GCash number")

    def test_an_old_receipt_names_its_date(self):
        self.reads(good_reading(paid_at=timezone.now() - timedelta(days=60)))
        data = self.refused(self.checkout("a"))
        self.assertIn("This receipt is from", data["error"])
        self.assertIn("today's payment", data["error"])
        self.nothing_booked()

    def test_the_old_shop_number_after_its_grace_has_its_own_message(self):
        gcash_numbers.record_change(OLD_SHOP, SHOP, 0, now=timezone.now() - timedelta(days=3))
        self.reads(good_reading(recipient_number=OLD_SHOP, recipient_name="", paid_at=timezone.now() - timedelta(hours=5)))
        data = self.refused(self.checkout("a"))
        self.assertIn("no longer used", data["error"])

    def test_a_refused_receipt_leaves_the_gown_free_for_the_next_customer(self):
        self.reads(good_reading(app="maya"))
        self.refused(self.checkout("a"))
        self.nothing_booked()
        self.reader.return_value = (good_reading(), "")
        self.assertEqual(self.checkout("b").status_code, 200)             # the very same gown and dates

    def test_the_refusal_is_kept_with_its_reason(self):
        self.reads(good_reading(app="maya"))
        self.refused(self.checkout("a"))
        row = ProofRejection.objects.get()
        self.assertEqual((row.user, row.reservation, row.source), (self.customers["a"], None, ProofRejection.Source.UPLOAD))
        self.assertTrue(row.reason)


class NeedsAPersonToLookTests(ReceiptCheckBase):
    def test_too_little_money_still_books_but_is_marked_for_the_staff(self):
        self.reads(good_reading(amount=Decimal("100")))
        reservation = self.booked(self.checkout("a"))
        self.assertEqual(reservation.proof_check, Reservation.ProofCheck.NEEDS_CHECK)
        self.assertIn("₱100", reservation.proof_notes)
        self.assertIn("₱2,000", reservation.proof_notes)
        self.assertIn("Needs check", reservation.status_events.get(label="Payment receipt checked").detail)
        self.assertEqual(ProofRejection.objects.count(), 0)

    def test_a_different_name_on_the_right_number_is_marked(self):
        self.reads(good_reading(recipient_name="MA****A L."))
        self.assertEqual(self.booked(self.checkout("a")).proof_check, Reservation.ProofCheck.NEEDS_CHECK)

    def test_paid_before_the_booking_started_is_marked(self):
        client = Client()
        client.force_login(self.customers["a"])
        agreed(self.customers["a"])
        client.post(reverse("gowns:reservation_hold_start"))                # the 20-minute hold begins now
        self.reads(good_reading(paid_at=timezone.now() - timedelta(hours=3)))
        items = [{"gown_name": self.gowns[0].name, "gown_slug": self.gowns[0].slug, "size": "Medium",
                  "rental_date": (self.event - timedelta(days=2)).isoformat(), "return_date": (self.event + timedelta(days=2)).isoformat()}]
        response = client.post(reverse("gowns:reservation_submit"), data={
            "items": json.dumps(items), "first_name": "Rita", "last_name": "A", "phone": "09171234567", "address": "1 Test St", "city": "C",
            "postal_code": "1000", "payment_method": "GCash",
            "proof_of_payment": SimpleUploadedFile("proof.jpg", unique_jpeg(), content_type="image/jpeg")})
        reservation = self.booked(response)
        self.assertEqual(reservation.proof_check, Reservation.ProofCheck.NEEDS_CHECK)
        self.assertIn("before this booking was started", reservation.proof_notes)

    def test_a_reader_that_is_down_never_blocks_the_customer(self):
        self.reads(None, "Gemini unavailable")
        reservation = self.booked(self.checkout("a"))
        self.assertEqual(reservation.proof_check, Reservation.ProofCheck.NEEDS_CHECK)
        self.assertIn("could not be read", reservation.proof_notes.lower())
        self.assertEqual(ProofRejection.objects.count(), 0)
        self.assertEqual(reservation.owner_check, Reservation.OwnerCheck.PENDING)

    def test_the_same_amount_minute_and_number_on_two_bookings_is_a_warning_not_a_refusal(self):
        moment = timezone.now() - timedelta(minutes=3)
        self.reads(good_reading(reference="1111111111111", paid_at=moment))
        self.assertEqual(self.booked(self.checkout("a")).proof_check, Reservation.ProofCheck.PASS)
        self.reader.return_value = (good_reading(reference="2222222222222", paid_at=moment), "")
        second = self.booked(self.checkout("b", (1,)))
        self.assertEqual(second.proof_check, Reservation.ProofCheck.NEEDS_CHECK)
        self.assertIn("RSV-", second.proof_notes)


class OneReceiptOneBookingTests(ReceiptCheckBase):
    def test_the_same_reference_cannot_pay_for_a_second_booking(self):
        self.reads(good_reading())
        self.booked(self.checkout("a"))
        data = self.refused(self.checkout("b", (1,)))                        # another picture, another account, the same Ref No.
        self.assertIn("already used for another booking", data["error"])
        self.assertEqual(Reservation.objects.count(), 1)

    def test_the_same_picture_cannot_pay_for_a_second_booking_and_is_not_even_read(self):
        picture = unique_jpeg()
        self.reads(good_reading())
        self.booked(self.checkout("a", picture=picture))
        self.reader.reset_mock()
        data = self.refused(self.checkout("b", (1,), picture=picture))
        self.assertIn("already used for another booking", data["error"])
        self.reader.assert_not_called()

    def test_a_different_reference_and_picture_is_fine(self):
        self.reads(good_reading(reference="1111111111111", paid_at=timezone.now() - timedelta(minutes=9)))
        self.booked(self.checkout("a"))
        self.reader.return_value = (good_reading(reference="2222222222222", amount=Decimal("2000.00"), paid_at=timezone.now() - timedelta(minutes=1)), "")
        self.assertEqual(self.booked(self.checkout("b", (1,))).proof_check, Reservation.ProofCheck.PASS)


class FourRefusedProofsPauseTheAccountTests(ReceiptCheckBase):
    def refuse_once(self, who="a"):
        self.reads(good_reading(app="maya"))
        return self.refused(self.checkout(who))

    def test_three_refusals_do_not_pause_the_fourth_does(self):
        for _ in range(3):
            self.assertFalse(self.refuse_once()["account_blocked"])
        profile = UserProfile.objects.get(user=self.customers["a"])
        self.assertFalse(profile.proof_blocked)
        fourth = self.refuse_once()
        self.assertTrue(fourth["account_blocked"])
        self.assertIn("account is now paused", fourth["error"])
        profile.refresh_from_db()
        self.assertTrue(profile.proof_blocked)
        self.assertTrue(profile.is_flagged)
        self.assertTrue(CustomerMessage.objects.filter(
            recipient=self.customers["a"], category=CustomerMessage.Category.ACCOUNT_FLAGGED, body__contains="paused").exists())

    def test_a_paused_account_cannot_book_or_start_a_hold_even_with_a_perfect_receipt(self):
        for _ in range(4):
            self.refuse_once()
        self.reads(good_reading())
        response = self.checkout("a")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.json()["account_blocked"])
        self.assertIn("paused", response.json()["error"])
        client = Client()
        client.force_login(self.customers["a"])
        hold = client.post(reverse("gowns:reservation_hold_start"))
        self.assertEqual(hold.status_code, 403)
        self.assertTrue(hold.json()["account_blocked"])
        self.nothing_booked()

    def test_another_customer_is_not_affected(self):
        for _ in range(4):
            self.refuse_once("a")
        self.reads(good_reading())
        self.assertEqual(self.checkout("b", (1,)).status_code, 200)

    def test_refusals_older_than_seven_days_do_not_count(self):
        for _ in range(3):
            self.refuse_once()
        ProofRejection.objects.filter(user=self.customers["a"]).update(created_at=timezone.now() - timedelta(days=8))
        self.assertFalse(self.refuse_once()["account_blocked"])
        self.assertEqual(services.count_recent_rejected_proofs(self.customers["a"]), 1)

    def test_unflagging_lifts_the_pause_and_the_count_starts_again(self):
        for _ in range(4):
            self.refuse_once()
        owner = User.objects.create_superuser("rc_owner", "rc_owner@example.com", "Pass-12345!")
        client = Client()
        client.force_login(owner)
        response = client.post(
            reverse("arabela_admin:customer_flag", args=[self.customers["a"].pk]),
            data=json.dumps({"flagged": False, "reason": "Spoke with her, it was a mistake."}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        profile = UserProfile.objects.get(user=self.customers["a"])
        self.assertFalse(profile.proof_blocked)
        self.assertFalse(profile.is_flagged)
        self.assertEqual(services.count_recent_rejected_proofs(self.customers["a"]), 0)
        self.reads(good_reading())
        self.assertEqual(self.checkout("a").status_code, 200)                # she can book again
        # and three more refusals do not pause her yet -- the count restarted
        for _ in range(3):
            self.assertFalse(self.refuse_once()["account_blocked"])

    def test_a_staff_rejection_of_an_online_booking_with_a_proof_counts_too(self):
        self.reads(good_reading())
        reservation = self.booked(self.checkout("a"))
        staff = User.objects.create_user("rc_staff", "rc_staff@example.com", "Pass-12345!", is_staff=True)
        UserProfile.objects.update_or_create(user=staff, defaults={"role": UserProfile.Role.STAFF})
        client = Client()
        client.force_login(staff)
        response = client.post(
            reverse("arabela_admin:reservation_reject", args=[reservation.pk]),
            data=json.dumps({"reason": "Receipt is edited."}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        row = ProofRejection.objects.get(user=self.customers["a"])
        self.assertEqual((row.source, row.reservation), (ProofRejection.Source.STAFF, reservation))
        self.assertEqual(services.count_recent_rejected_proofs(self.customers["a"]), 1)


class UploadTheProofLaterTests(ReceiptCheckBase):
    def a_booking_without_proof(self, who="a"):
        reservation = Reservation.objects.create(
            customer=self.customers[who], customer_name="Rita A", status=Reservation.Status.PENDING,
            security_deposit=Decimal("2000"), rental_subtotal=Decimal("3000"), total_amount=Decimal("5000"))
        item = ReservationItem.objects.create(
            reservation=reservation, gown=self.gowns[2], gown_name=self.gowns[2].name, size="Medium", rental_price=Decimal("3000"),
            rental_date=self.event - timedelta(days=2), return_date=self.event + timedelta(days=2))
        return reservation, item

    def upload(self, who, item, picture=None):
        client = Client()
        client.force_login(self.customers[who])
        agreed(self.customers[who])
        return client.post(reverse("gowns:reservation_upload_proof", args=[item.pk]), data={
            "proof_of_payment": SimpleUploadedFile("late.jpg", picture or unique_jpeg(), content_type="image/jpeg")})

    def test_a_good_receipt_uploaded_later_is_checked_and_stored(self):
        reservation, item = self.a_booking_without_proof()
        self.reads(good_reading())
        response = self.upload("a", item)
        self.assertEqual(response.status_code, 200, response.content)
        reservation.refresh_from_db()
        self.assertEqual(reservation.payment_proof_url, "https://example.test/fake-proof.jpg")
        self.assertEqual(reservation.proof_check, Reservation.ProofCheck.PASS)
        self.assertEqual(reservation.gcash_reference, "5046933479702")
        self.assertEqual(reservation.owner_check, Reservation.OwnerCheck.PENDING)
        self.assertTrue(reservation.status_events.filter(label="Payment receipt checked", staff_only=True).exists())

    def test_a_refused_receipt_uploaded_later_changes_nothing_and_counts(self):
        reservation, item = self.a_booking_without_proof()
        self.reads(good_reading(app="maya"))
        response = self.upload("a", item)
        data = self.refused(response)
        self.assertIn("Only GCash receipts", data["error"])
        reservation.refresh_from_db()
        self.assertEqual(reservation.payment_proof_url, "")
        self.assertEqual(reservation.proof_check, "")
        self.assertEqual(reservation.owner_check, "")
        row = ProofRejection.objects.get()
        self.assertEqual((row.source, row.reservation), (ProofRejection.Source.UPLOAD, reservation))

    def test_the_booking_is_judged_against_its_own_deposit(self):
        reservation, item = self.a_booking_without_proof()
        Reservation.objects.filter(pk=reservation.pk).update(security_deposit=Decimal("5"))      # made when the owner's deposit was 1 per gown x 5
        self.reads(good_reading(amount=Decimal("5")))
        self.assertEqual(self.upload("a", item).status_code, 200)
        reservation.refresh_from_db()
        self.assertEqual(reservation.proof_check, Reservation.ProofCheck.PASS)

    def test_a_receipt_already_used_by_another_booking_is_refused_here_too(self):
        self.reads(good_reading())
        self.booked(self.checkout("b", (0,)))
        _, item = self.a_booking_without_proof()
        data = self.refused(self.upload("a", item))
        self.assertIn("already used for another booking", data["error"])

    def test_nobody_else_can_upload_to_my_booking(self):
        _, item = self.a_booking_without_proof()
        self.reads(good_reading())
        self.assertEqual(self.upload("b", item).status_code, 404)
