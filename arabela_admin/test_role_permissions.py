"""The three admin roles -- Staff, Manager, Owner (accounts/roles.py).

A Staff account does the daily work. A Manager or the owner can also do the sensitive actions: delete gowns (one or in bulk), cancel a
confirmed booking or some of its gowns, flag/unflag a customer and remove a cancellation lock, and delete a receipt photo. This file checks,
for each of those actions: a Staff account is refused (403, "Only a Manager or the owner can do that.") and NOTHING changes; a Manager, an
owner (by role) and a superuser can do it; nobody signed in is still refused with 401. It also checks that the buttons are hidden for Staff
and shown to a Manager/owner, that everyday work still works for Staff, that the owner-only pages stay owner-only, and that the proof
windows show who uploaded each shop receipt."""
import json
from datetime import timedelta
from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomerMessage, UserProfile
from accounts.roles import MANAGER_ONLY_MESSAGE, can_manage, staff_display_name
from arabela_admin.test_cancel_booking import Base, make_gown
from gowns.models import Gown, GownRemoval
from reservations.models import ReceiptRecord, Reservation, ReservationItem

User = get_user_model()
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"role-test" + b"\xff\xd9"


class RoleBase(Base):
    """Base (arabela_admin/test_cancel_booking.py) already has a Manager (`staff`), a superuser owner and a customer."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.manager = cls.staff
        cls.plain_staff = User.objects.create_user("rp_staff", password="x", is_staff=True, first_name="Sam", last_name="Staff")
        UserProfile.objects.update_or_create(user=cls.plain_staff, defaults={"role": UserProfile.Role.STAFF})
        cls.owner_by_role = User.objects.create_user("rp_owner_role", password="x", is_staff=True, first_name="Olga", last_name="Owner")
        UserProfile.objects.update_or_create(user=cls.owner_by_role, defaults={"role": UserProfile.Role.OWNER})
        cls.bare = User.objects.create_user("rp_bare", password="x", is_staff=True)      # an admin account with no profile at all
        cls.refused_accounts = {"Staff": cls.plain_staff, "admin account with no profile": cls.bare}
        cls.allowed_accounts = {"Manager": cls.manager, "owner by role": cls.owner_by_role, "superuser owner": cls.owner}

    def setUp(self):
        super().setUp()
        self.serial = count(1, 10)           # a fresh block of gown numbers for every scenario

    # ---------------------------------------------------------------- the eight Manager-only actions, each with fresh data
    def post(self, url, body=None):
        return self.client.post(url, data=json.dumps(body or {}), content_type="application/json")

    def scenario_delete_a_gown(self):
        gown = make_gown(next(self.serial))
        send = lambda: self.post(reverse("arabela_admin:gown_delete", args=[gown.id]), {"reason": "Damaged"})
        return send, lambda: not Gown.objects.filter(id=gown.id).exists()

    def scenario_bulk_delete_gowns(self):
        gowns = [make_gown(next(self.serial)), make_gown(next(self.serial))]
        send = lambda: self.post(reverse("arabela_admin:gown_bulk_action"), {"action": "delete", "ids": [g.id for g in gowns], "reason": "Damaged"})
        return send, lambda: not Gown.objects.filter(id__in=[g.id for g in gowns]).exists()

    def scenario_cancel_a_booking(self):
        reservation, _items = self.booking(number_from=next(self.serial))
        send = lambda: self.post(reverse("arabela_admin:reservation_cancel", args=[reservation.pk]),
                                 {"reason": "no_show", "note": "", "confirm": reservation.reference_code})
        return send, lambda: Reservation.objects.get(pk=reservation.pk).status == Reservation.Status.CANCELLED

    def scenario_cancel_some_gowns(self):
        reservation, items = self.booking(gowns=2, number_from=next(self.serial))
        send = lambda: self.post(reverse("arabela_admin:reservation_cancel_gowns", args=[reservation.pk]),
                                 {"reason": "no_show", "note": "", "items": [items[0].pk]})
        return send, lambda: ReservationItem.objects.get(pk=items[0].pk).stage == ReservationItem.Stage.CANCELLED

    def scenario_flag_a_customer(self):
        customer = User.objects.create_user(f"rp_flag_{next(self.serial)}", password="x")
        send = lambda: self.post(reverse("arabela_admin:customer_flag", args=[customer.id]), {"flagged": True, "reason": "Scam"})
        return send, lambda: UserProfile.objects.filter(user=customer, is_flagged=True).exists() and CustomerMessage.objects.filter(recipient=customer).exists()

    def scenario_unflag_a_customer(self):
        customer = User.objects.create_user(f"rp_unflag_{next(self.serial)}", password="x")
        UserProfile.objects.update_or_create(user=customer, defaults={"is_flagged": True})
        send = lambda: self.post(reverse("arabela_admin:customer_flag", args=[customer.id]), {"flagged": False, "reason": "Issue Resolved"})
        return send, lambda: not UserProfile.objects.get(user=customer).is_flagged

    def scenario_remove_a_lock(self):
        customer = User.objects.create_user(f"rp_lock_{next(self.serial)}", password="x")
        UserProfile.objects.update_or_create(user=customer, defaults={"cancel_lockout_until": timezone.now() + timedelta(minutes=30)})
        send = lambda: self.post(reverse("arabela_admin:customer_unlock", args=[customer.id]))
        return send, lambda: UserProfile.objects.get(user=customer).cancel_lockout_until is None

    def scenario_delete_a_receipt(self):
        reservation, _items = self.booking(number_from=next(self.serial))
        receipt = ReceiptRecord.objects.create(reservation=reservation, photo_url="https://files.example/receipt_photos/role_test.jpg",
                                               uploaded_by=self.plain_staff)

        def send():
            with patch("arabela_admin.views.default_storage"):          # never touch real photo storage
                return self.client.post(reverse("arabela_admin:receipt_delete", args=[receipt.id]))
        return send, lambda: not ReceiptRecord.objects.filter(id=receipt.id).exists()

    SCENARIOS = ("delete_a_gown", "bulk_delete_gowns", "cancel_a_booking", "cancel_some_gowns", "flag_a_customer", "unflag_a_customer",
                 "remove_a_lock", "delete_a_receipt")

    def scenario(self, name):
        return getattr(self, "scenario_" + name)()

    # ---------------------------------------------------------------- Staff is refused, and nothing changes
    def test_a_staff_account_is_refused_every_manager_only_action_and_nothing_changes(self):
        for who, account in self.refused_accounts.items():
            for name in self.SCENARIOS:
                with self.subTest(account=who, action=name):
                    send, changed = self.scenario(name)
                    self.client.force_login(account)
                    response = send()
                    self.assertEqual(response.status_code, 403, response.content)
                    self.assertEqual(response.json()["error"], MANAGER_ONLY_MESSAGE)
                    self.assertFalse(changed())

    def test_a_refused_request_writes_nothing_at_all(self):
        sends = [self.scenario(name)[0] for name in self.SCENARIOS]

        def snapshot():
            return (Gown.objects.count(), Reservation.objects.filter(status=Reservation.Status.CANCELLED).count(), GownRemoval.objects.count(),
                    ReceiptRecord.objects.count(), CustomerMessage.objects.count(), UserProfile.objects.filter(is_flagged=True).count(),
                    ReservationItem.objects.filter(stage=ReservationItem.Stage.CANCELLED).count())

        before = snapshot()
        self.client.force_login(self.plain_staff)
        for send in sends:
            self.assertEqual(send().status_code, 403)
        self.assertEqual(snapshot(), before)

    # ---------------------------------------------------------------- Manager and owner can do all of them
    def test_a_manager_an_owner_and_a_superuser_can_do_each_of_them(self):
        for who, account in self.allowed_accounts.items():
            for name in self.SCENARIOS:
                with self.subTest(account=who, action=name):
                    send, changed = self.scenario(name)
                    self.assertFalse(changed())
                    self.client.force_login(account)
                    response = send()
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertTrue(changed())

    def test_nobody_signed_in_and_no_customer_can_do_them_either(self):
        for name in self.SCENARIOS:
            with self.subTest(action=name):
                send, changed = self.scenario(name)
                self.client.logout()
                self.assertEqual(send().status_code, 401)
                self.client.force_login(self.customer)
                self.assertEqual(send().status_code, 401)
                self.assertFalse(changed())

    def test_a_manager_who_deletes_a_gown_is_the_name_in_the_removal_log(self):
        gown = make_gown(next(self.serial))
        self.client.force_login(self.manager)
        self.assertEqual(self.post(reverse("arabela_admin:gown_delete", args=[gown.id]), {"reason": "Damaged"}).status_code, 200)
        self.assertEqual(GownRemoval.objects.get().removed_by_name, "Maria Cruz")

    # ---------------------------------------------------------------- everyday work is still Staff's
    def test_staff_still_do_the_daily_work(self):
        self.client.force_login(self.plain_staff)
        gown = make_gown(next(self.serial))
        other = make_gown(next(self.serial))
        # change a gown's status, and the bulk status / "mark as checked" actions (only the bulk DELETE is Manager-only)
        self.assertEqual(self.post(reverse("arabela_admin:gown_status_update", args=[gown.id]), {"status": "Out-of-Stock"}).status_code, 200)
        bulk = reverse("arabela_admin:gown_bulk_action")
        self.assertEqual(self.post(bulk, {"action": "status", "ids": [gown.id, other.id], "status": "Available"}).status_code, 200)
        self.assertEqual(self.post(bulk, {"action": "checked", "ids": [gown.id, other.id]}).status_code, 200)
        self.assertEqual(Gown.objects.filter(id__in=[gown.id, other.id]).count(), 2)
        # approve and reject new bookings in Pending Approval
        pending, _ = self.booking(status=Reservation.Status.PENDING, verified=False, days_late=-5, number_from=next(self.serial))
        to_reject, _ = self.booking(status=Reservation.Status.PENDING, verified=False, days_late=-5, number_from=next(self.serial))
        self.assertEqual(self.post(reverse("arabela_admin:reservation_approve", args=[pending.pk])).status_code, 200)
        self.assertEqual(self.post(reverse("arabela_admin:reservation_reject", args=[to_reject.pk]), {"reason": "Payment unclear"}).status_code, 200)
        self.assertEqual(Reservation.objects.get(pk=pending.pk).status, Reservation.Status.CONFIRMED)
        self.assertEqual(Reservation.objects.get(pk=to_reject.pk).status, Reservation.Status.REJECTED)
        # mark a gown picked up
        confirmed, [item] = self.booking(number_from=next(self.serial))
        self.assertEqual(self.client.post(reverse("arabela_admin:reservation_item_mark_picked_up", args=[item.id])).status_code, 200)
        self.assertEqual(ReservationItem.objects.get(pk=item.pk).stage, ReservationItem.Stage.RESERVED)
        # upload a receipt
        photo = SimpleUploadedFile("receipt.jpg", _JPEG, content_type="image/jpeg")
        with patch("arabela_admin.views._save_receipt_photo", return_value="https://files.example/receipt_photos/staff_upload.jpg"):
            response = self.client.post(reverse("arabela_admin:receipt_upload"), data={"reference_code": confirmed.reference_code, "photo": photo})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(ReceiptRecord.objects.get(reservation=confirmed).uploaded_by, self.plain_staff)

    def test_the_admin_pages_a_staff_account_uses_all_open(self):
        self.client.force_login(self.plain_staff)
        for name in ("dashboard", "gown_catalog", "active_reservations", "pending_approval", "payment_verification", "security_deposits",
                     "rental_history", "reservation_records", "rental_schedule", "clients", "new_booking"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(f"arabela_admin:{name}")).status_code, 200)

    # ---------------------------------------------------------------- the owner-only things stay owner-only (a Manager can't either)
    def test_staff_management_categories_and_tag_colors_stay_owner_only(self):
        for who, account in (("Manager", self.manager), ("Staff", self.plain_staff)):
            self.client.force_login(account)
            for page in ("staff_management", "categories"):
                with self.subTest(account=who, page=page):
                    self.assertEqual(self.client.get(reverse(f"arabela_admin:{page}")).status_code, 302)
            with self.subTest(account=who, action="add a category"):
                response = self.post(reverse("arabela_admin:gown_category_create"), {"name": "Prom Dress", "audience": "women"})
                self.assertEqual(response.status_code, 403)
            with self.subTest(account=who, action="tag colors"):
                self.assertEqual(self.post(reverse("arabela_admin:gown_tag_colors_update"), {"colors": {}}).status_code, 403)
        self.client.force_login(self.owner)
        for page in ("staff_management", "categories"):
            self.assertEqual(self.client.get(reverse(f"arabela_admin:{page}")).status_code, 200)

    # ---------------------------------------------------------------- the buttons
    def pages(self, account):
        """The HTML of the five pages where a Manager-only button lives, as `account` sees them."""
        self.client.force_login(account)
        reservation, [item, _second] = self.booking(gowns=2, number_from=next(self.serial))      # two gowns: only then is there a per-gown "Cancel gown"
        ReceiptRecord.objects.create(reservation=reservation, photo_url="https://files.example/receipt_photos/btn.jpg", uploaded_by=self.manager)
        get = lambda name: self.client.get(reverse(f"arabela_admin:{name}")).content.decode()
        return {
            "catalog": get("gown_catalog"), "active": get("active_reservations"), "clients": get("clients"), "records": get("reservation_records"),
            "reservation": reservation, "item": item,
        }

    def buttons_in(self, pages):
        reservation = pages["reservation"]
        return {
            "Remove Gown": '@click="deleteGown()"' in pages["catalog"],
            "bulk Remove": '@click="bulkDelete()"' in pages["catalog"],
            "Cancel booking": f'data-url="{reverse("arabela_admin:reservation_cancel", args=[reservation.id])}"' in pages["active"],
            "Cancel gown": f'data-url="{reverse("arabela_admin:reservation_cancel_gowns", args=[reservation.id])}"' in pages["active"],
            "Flag Account": "pendingFlagAction = 'flag'" in pages["clients"],
            "Unflag Account": "pendingFlagAction = 'unflag'" in pages["clients"],
            "Remove lock": "unlockCustomer(viewingCustomer.userId, viewingCustomer.name)" in pages["clients"],
            "Delete Receipt": "confirmingReceiptDelete = true" in pages["records"],
        }

    def test_a_staff_account_does_not_see_the_manager_only_buttons(self):
        for who, account in self.refused_accounts.items():
            with self.subTest(account=who):
                shown = self.buttons_in(self.pages(account))
                self.assertEqual([name for name, visible in shown.items() if visible], [])

    def test_a_manager_and_the_owner_see_every_one_of_them(self):
        for who, account in self.allowed_accounts.items():
            with self.subTest(account=who):
                shown = self.buttons_in(self.pages(account))
                self.assertEqual([name for name, visible in shown.items() if not visible], [])

    def test_a_staff_account_still_sees_the_everyday_buttons(self):
        pages = self.pages(self.plain_staff)
        self.assertIn("Mark Picked Up", pages["active"])
        self.assertIn("Remind", pages["active"])
        self.assertIn("Upload Receipt", pages["records"])
        self.assertIn("Mark as checked now", pages["catalog"])
        self.assertIn("bulkStatus('Available')", pages["catalog"])
        self.assertIn("markChecked(selectedGownIds)", pages["catalog"])
        self.assertIn("View Reservation History", pages["clients"])
        self.assertIn("Change pick-up date", pages["active"])

    def test_the_catalog_keeps_its_layout_when_the_remove_button_is_hidden(self):
        """The single-gown window has Remove on the left and Close on the right (justify-between): a hidden Remove leaves an empty span,
        so Close stays on the right instead of jumping to the left."""
        self.client.force_login(self.plain_staff)
        html = self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()
        marker = '<div class="mt-6 flex items-center justify-between border-t border-gray-100 px-2 pt-6 dark:border-gray-800">'
        at = html.index(marker)
        self.assertIn("<span></span>", html[at:at + 600])

    def test_the_template_flag_is_the_same_rule_the_server_uses(self):
        for who, account in {**self.refused_accounts, **self.allowed_accounts}.items():
            with self.subTest(account=who):
                self.client.force_login(account)
                flag = self.client.get(reverse("arabela_admin:dashboard")).context["admin_can_manage"]
                send, _changed = self.scenario("delete_a_gown")
                self.assertEqual(flag, send().status_code == 200)

    def test_the_add_account_note_says_what_each_role_does(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("arabela_admin:staff_management")).content.decode()
        self.assertIn("Staff do the daily work. A Manager can also delete gowns, cancel bookings, flag customers, and delete receipt photos.", html)
        self.assertIn("Neither can open Staff Management.", html)
        self.assertNotIn("Manager and Staff have the same access", html)
        self.assertIn('<option value="Manager">Manager</option>', html)         # the owner still just picks the role
        self.assertIn('<option value="Staff">Staff</option>', html)

    # ---------------------------------------------------------------- the rule itself
    def test_can_manage_for_every_kind_of_account(self):
        self.assertTrue(can_manage(self.owner))
        self.assertTrue(can_manage(self.owner_by_role))
        self.assertTrue(can_manage(self.manager))
        self.assertFalse(can_manage(self.plain_staff))
        self.assertFalse(can_manage(self.bare))
        self.assertFalse(can_manage(self.customer))
        self.assertFalse(can_manage(None))
        from django.contrib.auth.models import AnonymousUser
        self.assertFalse(can_manage(AnonymousUser()))

    def test_changing_a_role_changes_the_access_straight_away(self):
        gown = make_gown(next(self.serial))
        self.client.force_login(self.plain_staff)
        self.assertEqual(self.post(reverse("arabela_admin:gown_delete", args=[gown.id]), {"reason": "Damaged"}).status_code, 403)
        UserProfile.objects.filter(user=self.plain_staff).update(role=UserProfile.Role.MANAGER)
        self.assertEqual(self.post(reverse("arabela_admin:gown_delete", args=[gown.id]), {"reason": "Damaged"}).status_code, 200)
        UserProfile.objects.filter(user=self.plain_staff).update(role=UserProfile.Role.STAFF)
        again = make_gown(next(self.serial))
        self.assertEqual(self.post(reverse("arabela_admin:gown_delete", args=[again.id]), {"reason": "Damaged"}).status_code, 403)

    def test_the_staff_name_rule_is_shared(self):
        self.assertEqual(staff_display_name(self.manager), "Maria Cruz")
        self.assertEqual(staff_display_name(self.bare), "rp_bare")          # no name entered: the username
        from arabela_admin.views import _staff_display_name
        self.assertEqual(_staff_display_name(self.manager), staff_display_name(self.manager))


class ReceiptUploaderInProofWindowsTests(Base):
    """The four proof windows (Payment Verification, Security Deposits, Rental History, Dashboard Quick Verify) say who attached each
    shop receipt -- "by <staff name>" -- the same name Reservation Records shows."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.uploader = User.objects.create_user("rp_uploader", password="x", is_staff=True, first_name="Rita", last_name="Uploader")
        UserProfile.objects.update_or_create(user=cls.uploader, defaults={"role": UserProfile.Role.STAFF})

    def booking_with_receipts(self, *, status=Reservation.Status.CONFIRMED, number_from=1, uploaders=(None,)):
        reservation, items = self.booking(status=status, number_from=number_from)
        Reservation.objects.filter(pk=reservation.pk).update(rental_subtotal=Decimal("5000"), security_deposit=Decimal("2000"),
                                                             total_amount=Decimal("7000"), payment_proof_url="")
        for n, who in enumerate(uploaders):
            ReceiptRecord.objects.create(reservation=reservation, photo_url=f"https://files.example/receipt_photos/r{number_from}_{n}.jpg",
                                         covers="Rental + deposit", uploaded_by=who or self.uploader,
                                         uploaded_at=timezone.now() + timedelta(seconds=n))
        return Reservation.objects.get(pk=reservation.pk), items

    def test_the_receipt_data_carries_the_name_and_a_removed_account_says_so(self):
        reservation, _ = self.booking_with_receipts()
        row = reservation.receipt_proofs()[0]
        self.assertEqual(row["by"], "Rita Uploader")
        self.assertEqual(row["label"], "Paid in full receipt")
        self.uploader.delete()                          # uploaded_by is SET_NULL: the receipt stays, the name goes
        row = Reservation.objects.get(pk=reservation.pk).receipt_proofs()[0]
        self.assertEqual(row["by"], "Removed account")

    def test_each_window_gets_the_name_for_every_receipt_and_shows_it(self):
        self.booking_with_receipts(status=Reservation.Status.PENDING, number_from=1)          # Dashboard Quick Verify lists Pending ones
        self.booking_with_receipts(number_from=11)                                            # the others list confirmed ones
        self.client.force_login(self.owner)
        for name in ("payment_verification", "security_deposits", "rental_history", "dashboard"):
            with self.subTest(page=name):
                html = self.client.get(reverse(f"arabela_admin:{name}")).content.decode()
                self.assertIn("Rita Uploader", html)                                           # the name is in the data the window reads ...
                self.assertIn("(rc.by ? ' · by ' + rc.by : '')", html)                           # ... and the window prints it after the date

    def test_a_customers_gcash_screenshot_gets_no_staff_name(self):
        reservation, _ = self.booking()
        Reservation.objects.filter(pk=reservation.pk).update(payment_proof_url="https://files.example/proof.jpg")
        self.client.force_login(self.owner)
        html = self.client.get(reverse("arabela_admin:payment_verification")).content.decode()
        self.assertIn("https://files.example/proof.jpg", html)
        self.assertNotIn("Rita Uploader", html)
        self.assertEqual(Reservation.objects.get(pk=reservation.pk).receipt_proofs(), [])

    def test_the_pages_do_not_ask_the_database_once_per_receipt(self):
        def queries(name):
            with CaptureQueriesContext(connection) as captured:
                self.assertEqual(self.client.get(reverse(f"arabela_admin:{name}")).status_code, 200)
            return len(captured.captured_queries)

        pages = ("payment_verification", "security_deposits", "rental_history", "dashboard")
        self.client.force_login(self.owner)
        self.booking_with_receipts(status=Reservation.Status.PENDING, number_from=1, uploaders=(self.uploader, self.staff))
        few = {name: queries(name) for name in pages}
        for k in range(2, 7):
            self.booking_with_receipts(status=Reservation.Status.PENDING, number_from=k * 10, uploaders=(self.uploader, self.staff, self.owner))
        for name in pages:
            self.assertLessEqual(queries(name), few[name] + 1, name)
