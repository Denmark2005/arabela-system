"""Shop receipt photos: who may add, replace and delete one (arabela_admin/views.py: receipt_upload_view, receipt_replace_view,
receipt_delete_view).

  * Add:     every staff account. Once a booking has ALL its receipts (an online booking: one; a walk-in: one full receipt, or the deposit
             one plus the rental one) a further photo is refused -- a wrong photo is fixed with Replace, not by piling on another.
  * Replace: every staff account, always. The old picture's link goes to the Audit Log.
  * Delete:  the owner, a staff account the owner allowed ("Delete receipt photos") -- and the staff member who attached THAT photo, for
             the first 15 minutes (the undo). Nobody else. The stored picture is kept either way."""
import json
from datetime import timedelta
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditLog, UserProfile
from accounts.roles import RECEIPT_UNDO_MINUTES
from arabela_admin.test_cancel_booking import Base
from arabela_admin.test_role_permissions import make_staff
from arabela_admin.views import RECEIPT_DELETE_REFUSED_MESSAGE, RECEIPTS_COMPLETE_MESSAGE
from reservations.models import ReceiptRecord, Reservation

User = get_user_model()
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"receipt-rules" + b"\xff\xd9"


class ReceiptBase(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.maria = make_staff("rr_maria", first="Maria", last="Cruz")           # Staff, no permissions
        cls.pedro = make_staff("rr_pedro", first="Pedro", last="Reyes")
        cls.allowed = make_staff("rr_allowed", ["delete_receipts"], first="Ada", last="Allowed")

    def setUp(self):
        super().setUp()
        self.serial = count(1, 10)
        self.photos = count(1)

    def walk_in(self, status=Reservation.Status.CONFIRMED):
        reservation, _items = self.booking(status=status, number_from=next(self.serial))
        Reservation.objects.filter(pk=reservation.pk).update(channel=Reservation.Channel.WALK_IN)
        return Reservation.objects.get(pk=reservation.pk)

    def upload(self, user, reservation, covers=None):
        self.client.force_login(user)
        photo = SimpleUploadedFile("receipt.jpg", _JPEG + str(next(self.photos)).encode(), content_type="image/jpeg")
        data = {"reference_code": reservation.reference_code, "photo": photo}
        if covers:
            data["covers"] = covers
        with patch("arabela_admin.views._save_receipt_photo", side_effect=lambda f: f"https://files.example/receipt_photos/r{next(self.photos)}.jpg"):
            return self.client.post(reverse("arabela_admin:receipt_upload"), data=data)

    def attach(self, reservation, by, minutes_ago=0, covers=""):
        return ReceiptRecord.objects.create(
            reservation=reservation, photo_url=f"https://files.example/receipt_photos/a{next(self.photos)}.jpg", uploaded_by=by,
            uploaded_at=timezone.now() - timedelta(minutes=minutes_ago), covers=covers)

    def delete(self, user, receipt):
        self.client.force_login(user)
        with patch("arabela_admin.views.default_storage") as storage:
            response = self.client.post(reverse("arabela_admin:receipt_delete", args=[receipt.id]))
        self.assertFalse(storage.delete.called, "the stored picture must be kept so the Audit Log link still opens")
        return response


class CompleteBookingTests(ReceiptBase):
    def test_an_online_booking_takes_one_receipt_then_refuses_another(self):
        reservation, _ = self.booking(number_from=next(self.serial))
        self.assertEqual(self.upload(self.maria, reservation).status_code, 200)
        response = self.upload(self.maria, reservation)
        self.assertEqual((response.status_code, response.json()["error"]), (409, RECEIPTS_COMPLETE_MESSAGE))
        self.assertEqual(ReceiptRecord.objects.filter(reservation=reservation).count(), 1)

    def test_a_walk_in_paid_in_full_takes_one_receipt(self):
        reservation = self.walk_in()
        self.assertEqual(self.upload(self.maria, reservation).status_code, 200)
        self.assertEqual(ReceiptRecord.objects.get(reservation=reservation).covers, ReceiptRecord.Covers.BOTH)
        self.assertEqual(self.upload(self.pedro, reservation).status_code, 409)

    def test_a_walk_in_that_paid_the_deposit_only_takes_the_deposit_then_the_rental_then_no_more(self):
        reservation = self.walk_in(status=Reservation.Status.PENDING)
        self.assertEqual(self.upload(self.maria, reservation, covers=ReceiptRecord.Covers.DEPOSIT).status_code, 200)
        self.assertFalse(Reservation.objects.get(pk=reservation.pk).receipts_complete())
        self.assertEqual(self.upload(self.maria, reservation).status_code, 200)                  # the system labels it: the rental
        self.assertEqual(sorted(ReceiptRecord.objects.filter(reservation=reservation).values_list("covers", flat=True)), ["Deposit", "Rental"])
        self.assertTrue(Reservation.objects.get(pk=reservation.pk).receipts_complete())
        self.assertEqual(self.upload(self.maria, reservation).status_code, 409)
        self.assertEqual(ReceiptRecord.objects.filter(reservation=reservation).count(), 2)

    def test_one_unlabelled_walk_in_receipt_does_not_complete_the_booking(self):
        reservation = self.walk_in()
        self.attach(reservation, self.maria, covers="")
        self.assertFalse(Reservation.objects.get(pk=reservation.pk).receipts_complete())

    def test_a_complete_booking_can_still_replace_a_photo(self):
        reservation, _ = self.booking(number_from=next(self.serial))
        receipt = self.attach(reservation, self.maria, minutes_ago=600, covers="Rental + deposit")
        self.client.force_login(self.pedro)                      # not the uploader, no permission: Replace is open to everyone
        photo = SimpleUploadedFile("again.jpg", _JPEG + b"replacement", content_type="image/jpeg")
        with patch("arabela_admin.views._save_receipt_photo", return_value="https://files.example/receipt_photos/fixed.jpg"):
            response = self.client.post(reverse("arabela_admin:receipt_replace", args=[receipt.id]), data={"photo": photo})
        self.assertEqual(response.status_code, 200, response.content)
        receipt.refresh_from_db()
        self.assertEqual(receipt.photo_url, "https://files.example/receipt_photos/fixed.jpg")
        self.assertEqual(receipt.uploaded_by, self.maria)                                         # the record is fixed, not re-created
        row = AuditLog.objects.get(action="receipt.replace")
        self.assertEqual(row.details["photo before"] if "photo before" in row.details else row.details.get("Photo before"),
                         "https://files.example/receipt_photos/a1.jpg")
        self.assertEqual(row.actor, self.pedro)

    def test_two_uploads_at_the_same_moment_cannot_both_get_in(self):
        """The booking row is locked and the rule is asked again under the lock: simulate the second upload losing the race."""
        reservation, _ = self.booking(number_from=next(self.serial))
        answers = iter([False, True])           # the early check says "room for one", the check under the lock says "someone just filled it"
        with patch("reservations.models.Reservation.receipts_complete", side_effect=lambda *a, **k: next(answers)):
            response = self.upload(self.maria, reservation)
        self.assertEqual((response.status_code, response.json()["error"]), (409, RECEIPTS_COMPLETE_MESSAGE))
        self.assertEqual(ReceiptRecord.objects.filter(reservation=reservation).count(), 0)

    def test_the_records_page_hands_the_screen_the_completeness_and_hides_the_upload_buttons_by_it(self):
        reservation, _ = self.booking(number_from=next(self.serial))
        self.attach(reservation, self.maria, covers="Rental + deposit")
        self.client.force_login(self.maria)
        html = self.client.get(reverse("arabela_admin:reservation_records")).content.decode()
        self.assertIn('"receiptsComplete": true', html.replace('"receiptsComplete":true', '"receiptsComplete": true'))
        self.assertIn('x-show="!receiptsComplete(r)"', html)
        self.assertIn('x-show="!receiptsComplete(viewing)"', html)

    def test_pending_approval_hides_attach_receipt_once_the_walk_in_has_all_its_receipts(self):
        reservation = self.walk_in(status=Reservation.Status.PENDING)
        Reservation.objects.filter(pk=reservation.pk).update(payment_proof_url="")
        self.client.force_login(self.maria)
        html = self.client.get(reverse("arabela_admin:pending_approval")).content.decode()
        self.assertIn(f'data-ref="{reservation.reference_code}"', html)                       # nothing attached yet: the button is there
        self.attach(reservation, self.maria, covers="Rental + deposit")
        html = self.client.get(reverse("arabela_admin:pending_approval")).content.decode()
        self.assertNotIn(f'class="ar-open-btn" data-ref="{reservation.reference_code}"', html)


class DeleteRulesTests(ReceiptBase):
    def test_the_uploader_can_take_back_their_own_photo_for_fifteen_minutes(self):
        reservation, _ = self.booking(number_from=next(self.serial))
        receipt = self.attach(reservation, self.maria, minutes_ago=RECEIPT_UNDO_MINUTES - 1)
        response = self.delete(self.maria, receipt)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["undo"])
        self.assertFalse(ReceiptRecord.objects.filter(pk=receipt.pk).exists())
        event = reservation.status_events.get(label="Receipt photo deleted")
        self.assertTrue(event.staff_only)
        self.assertIn("Maria Cruz", event.detail)

    def test_after_fifteen_minutes_the_uploader_is_refused(self):
        reservation, _ = self.booking(number_from=next(self.serial))
        receipt = self.attach(reservation, self.maria, minutes_ago=RECEIPT_UNDO_MINUTES + 1)
        response = self.delete(self.maria, receipt)
        self.assertEqual((response.status_code, response.json()["error"]), (403, RECEIPT_DELETE_REFUSED_MESSAGE))
        self.assertTrue(ReceiptRecord.objects.filter(pk=receipt.pk).exists())

    def test_another_staff_member_cannot_undo_someone_elses_photo_even_a_fresh_one(self):
        reservation, _ = self.booking(number_from=next(self.serial))
        receipt = self.attach(reservation, self.maria, minutes_ago=1)
        self.assertEqual(self.delete(self.pedro, receipt).status_code, 403)
        self.assertTrue(ReceiptRecord.objects.filter(pk=receipt.pk).exists())

    def test_an_allowed_staff_account_and_the_owner_can_delete_any_photo_at_any_time(self):
        for who in (self.allowed, self.owner, self.staff):         # self.staff = an old Manager row, which counts as all four
            reservation, _ = self.booking(number_from=next(self.serial))
            receipt = self.attach(reservation, self.maria, minutes_ago=60 * 24 * 30)
            response = self.delete(who, receipt)
            self.assertEqual(response.status_code, 200, (who.username, response.content))
            self.assertFalse(response.json()["undo"])
            self.assertFalse(ReceiptRecord.objects.filter(pk=receipt.pk).exists())

    def test_a_photo_whose_uploader_was_removed_can_only_be_deleted_with_the_permission(self):
        reservation, _ = self.booking(number_from=next(self.serial))
        receipt = self.attach(reservation, self.maria, minutes_ago=1)
        ReceiptRecord.objects.filter(pk=receipt.pk).update(uploaded_by=None)
        self.assertEqual(self.delete(self.maria, receipt).status_code, 403)

    def test_deleting_something_already_gone_is_a_404_and_nobody_signed_in_is_a_401(self):
        self.client.force_login(self.maria)
        self.assertEqual(self.client.post(reverse("arabela_admin:receipt_delete", args=[987654])).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.post(reverse("arabela_admin:receipt_delete", args=[987654])).status_code, 401)

    def test_deleting_leaves_room_to_add_the_right_photo(self):
        reservation, _ = self.booking(number_from=next(self.serial))
        receipt = self.attach(reservation, self.maria, minutes_ago=2, covers="Rental + deposit")
        self.assertEqual(self.upload(self.maria, reservation).status_code, 409)
        self.assertEqual(self.delete(self.maria, receipt).status_code, 200)
        self.assertEqual(self.upload(self.maria, reservation).status_code, 200)

    def test_the_can_delete_flag_follows_the_same_two_tests(self):
        reservation, _ = self.booking(number_from=next(self.serial))
        fresh = self.attach(reservation, self.maria, minutes_ago=3)
        old = self.attach(reservation, self.maria, minutes_ago=300)
        from arabela_admin.views import _receipt_row
        fresh, old = ReceiptRecord.objects.get(pk=fresh.pk), ReceiptRecord.objects.get(pk=old.pk)
        self.assertEqual((_receipt_row(fresh, user=self.maria)["canDelete"], _receipt_row(old, user=self.maria)["canDelete"]), (True, False))
        self.assertEqual((_receipt_row(fresh, user=self.pedro)["canDelete"], _receipt_row(old, user=self.pedro)["canDelete"]), (False, False))
        self.assertEqual((_receipt_row(fresh, user=self.allowed)["canDelete"], _receipt_row(old, user=self.allowed)["canDelete"]), (True, True))
        self.assertFalse(_receipt_row(fresh)["canDelete"])


class ReceiptAuditTests(ReceiptBase):
    def test_add_replace_and_delete_each_leave_a_line_with_the_old_photo_link(self):
        reservation, _ = self.booking(number_from=next(self.serial))
        self.assertEqual(self.upload(self.maria, reservation).status_code, 200)
        receipt = ReceiptRecord.objects.get(reservation=reservation)
        old_url = receipt.photo_url
        self.client.force_login(self.maria)
        with patch("arabela_admin.views._save_receipt_photo", return_value="https://files.example/receipt_photos/new.jpg"):
            self.client.post(reverse("arabela_admin:receipt_replace", args=[receipt.id]),
                             data={"photo": SimpleUploadedFile("n.jpg", _JPEG + b"zzz", content_type="image/jpeg")})
        self.assertEqual(self.delete(self.maria, receipt).status_code, 200)
        rows = {row.action: row for row in AuditLog.objects.filter(action__startswith="receipt.")}
        self.assertEqual(set(rows), {"receipt.upload", "receipt.replace", "receipt.delete"})
        for row in rows.values():
            self.assertEqual((row.actor, row.actor_name, row.category, row.target_label), (self.maria, "Maria Cruz", "Payments", reservation.reference_code))
        self.assertEqual(rows["receipt.replace"].details["photo before"], old_url)
        self.assertEqual(rows["receipt.replace"].details["photo after"], "https://files.example/receipt_photos/new.jpg")
        self.assertTrue(rows["receipt.delete"].sensitive)
        self.assertTrue(rows["receipt.delete"].details["deleted within the 15-minute undo"])

    def test_a_refused_delete_is_written_as_refused_and_sensitive_with_the_address(self):
        reservation, _ = self.booking(number_from=next(self.serial))
        receipt = self.attach(reservation, self.maria, minutes_ago=500)
        self.client.force_login(self.pedro)
        self.client.post(reverse("arabela_admin:receipt_delete", args=[receipt.id]), REMOTE_ADDR="203.0.113.7")
        row = AuditLog.objects.get(action="receipt.delete")
        self.assertEqual((row.outcome, row.sensitive, row.ip_address), ("refused", True, "203.0.113.7"))
        self.assertTrue(row.summary.startswith("Pedro Reyes's attempt was refused"))
