"""The shop has TWO GCash accounts, the owner's and the manager's, and the rule follows the money (2026-11-17).

  * the owner's Edit Profile holds both accounts (name, number, QR; the manager's also a staff account that "holds" it) and ONE switch,
    "Customers pay to", that says which account customers are shown;
  * a payment to the OWNER's GCash cannot be approved by staff until the owner presses Received (her own Approve counts as Received);
    a payment to the MANAGER's GCash is approved as usual, and the manager who holds it may press Received too;
  * the rule follows the account the customer PAID, not the one that is active now, and rejecting is never held back;
  * the receipt check judges each receipt against the number and the name of the account it was paid to;
  * the owner's list, bell and reminder email never carry the manager's payments (while that account is in service).
"""
import io
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts.models import AuditLog, CustomerMessage, UserProfile
from arabela_admin.test_owner_payment_check import OwnerCheckBase, make_admin
from gowns import gcash_numbers, receipt_check as rc
from gowns.models import GcashNumberHistory, Gown, SiteSettings
from gowns.test_receipt_checkout import ReceiptCheckBase, good_reading
from gowns.test_receipt_rules import NOW, STARTED, reading
from reservations import owner_check
from reservations.models import Reservation, ReservationStatusEvent

User = get_user_model()
OWNER_NO = "09488745992"
MANAGER_NO = "09171234567"
OWNER_NAME = "Denmark Concepcion"
MANAGER_NAME = "Maria Santos"
MANAGER_QR = "https://example.test/gcash_qr/manager.png"
OWNER_QR = "https://example.test/gcash_qr/owner.png"
OWNER, MANAGER = gcash_numbers.OWNER, gcash_numbers.MANAGER


def set_shop(*, active=OWNER, holder=None, owner_number=OWNER_NO, manager_number=MANAGER_NO, manager_name=MANAGER_NAME, manager_qr=MANAGER_QR):
    shop = SiteSettings.load()
    shop.gcash_account_name, shop.gcash_number, shop.gcash_qr_url = OWNER_NAME, owner_number, OWNER_QR
    shop.manager_gcash_name, shop.manager_gcash_number, shop.manager_gcash_qr_url = manager_name, manager_number, manager_qr
    shop.manager_gcash_holder = holder
    shop.gcash_active = active
    shop.save()
    return shop


def png_upload(name="qr.png"):
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "black").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


# ---- the numbers over time and per account --------------------------------------------------------------------------------------------
class AccountNumbersTests(TestCase):
    def setUp(self):
        self.t0 = timezone.now()
        set_shop()

    def switch_to_manager(self, grace):
        gcash_numbers.record_change(OWNER_NO, MANAGER_NO, grace, now=self.t0, account=MANAGER, old_account=OWNER)
        SiteSettings.objects.filter(pk=1).update(gcash_active=MANAGER)

    def test_the_owner_is_active_until_the_owner_says_otherwise(self):
        self.assertEqual(gcash_numbers.active_account(), OWNER)
        self.assertEqual(gcash_numbers.active_details(), {"name": OWNER_NAME, "number": OWNER_NO, "qr_url": OWNER_QR})
        self.assertEqual(gcash_numbers.account_details(MANAGER)["number"], MANAGER_NO)

    def test_an_unknown_active_value_counts_as_the_owner(self):
        SiteSettings.objects.filter(pk=1).update(gcash_active="nonsense")
        self.assertEqual(gcash_numbers.active_account(), OWNER)

    def test_with_no_history_only_the_active_accounts_number_is_accepted(self):
        self.assertEqual(gcash_numbers.accepted_numbers_at(), [(OWNER_NO, False)])
        SiteSettings.objects.filter(pk=1).update(gcash_active=MANAGER)
        self.assertEqual(gcash_numbers.accepted_numbers_at(), [(MANAGER_NO, False)])

    def test_switching_keeps_the_old_number_for_the_grace_the_owner_chose(self):
        self.switch_to_manager(24)
        soon = dict(gcash_numbers.accepted_numbers_at(self.t0 + timedelta(hours=1)))
        self.assertEqual(soon, {MANAGER_NO: False, OWNER_NO: True})                       # both count, the old one marked "old"
        later = dict(gcash_numbers.accepted_numbers_at(self.t0 + timedelta(hours=30)))
        self.assertEqual(later, {MANAGER_NO: False})                                      # the 24 hours are over

    def test_not_at_all_still_allows_the_ten_minute_clock_overlap_and_nothing_more(self):
        self.switch_to_manager(0)
        self.assertIn(OWNER_NO, dict(gcash_numbers.accepted_numbers_at(self.t0 + timedelta(minutes=5))))
        self.assertNotIn(OWNER_NO, dict(gcash_numbers.accepted_numbers_at(self.t0 + timedelta(minutes=11))))

    def test_switching_back_closes_the_manager_number_the_same_way(self):
        self.switch_to_manager(24)
        gcash_numbers.record_change(MANAGER_NO, OWNER_NO, 48, now=self.t0 + timedelta(days=3), account=OWNER, old_account=MANAGER)
        SiteSettings.objects.filter(pk=1).update(gcash_active=OWNER)
        at = self.t0 + timedelta(days=3, hours=5)
        self.assertEqual(dict(gcash_numbers.accepted_numbers_at(at)), {OWNER_NO: False, MANAGER_NO: True})
        self.assertEqual(gcash_numbers.account_of_number(MANAGER_NO), MANAGER)
        self.assertEqual(gcash_numbers.account_of_number(OWNER_NO), OWNER)

    def test_every_history_row_says_whose_number_it_is(self):
        self.switch_to_manager(24)
        rows = {row.number: row.account for row in GcashNumberHistory.objects.all()}
        self.assertEqual(rows, {OWNER_NO: OWNER, MANAGER_NO: MANAGER})

    def test_a_number_nobody_placed_belongs_to_no_account(self):
        self.assertEqual(gcash_numbers.account_of_number("09265411484"), "")
        self.assertEqual(gcash_numbers.account_of_number(""), "")
        self.assertEqual(gcash_numbers.account_of_number(MANAGER_NO), MANAGER)            # from the settings, with no history at all

    def test_known_numbers_has_both_accounts_even_while_one_is_not_in_use(self):
        self.assertEqual(gcash_numbers.known_numbers(), {OWNER_NO, MANAGER_NO})

    def test_each_numbers_account_name(self):
        self.assertEqual(gcash_numbers.names_by_number(), {OWNER_NO: OWNER_NAME, MANAGER_NO: MANAGER_NAME})

    def test_which_account_a_receipt_was_paid_to(self):
        self.switch_to_manager(24)
        soon = self.t0 + timedelta(hours=1)
        self.assertEqual(gcash_numbers.account_for_recipient(MANAGER_NO, soon), MANAGER)
        self.assertEqual(gcash_numbers.account_for_recipient(OWNER_NO, soon), OWNER)
        self.assertEqual(gcash_numbers.account_for_recipient("09?????5992", soon), OWNER)          # a hidden number is matched on the digits that show
        self.assertEqual(gcash_numbers.account_for_recipient("09?????4567", soon), MANAGER)
        self.assertEqual(gcash_numbers.account_for_recipient("", soon), MANAGER)                   # nothing showing: the account customers are shown
        self.assertEqual(gcash_numbers.account_for_recipient("09265411484", soon), MANAGER)

    def test_a_number_change_of_the_inactive_account_writes_no_history(self):
        self.assertEqual(GcashNumberHistory.objects.count(), 0)


# ---- the receipt is judged against the account it was paid to --------------------------------------------------------------------------
class ReceiptPerAccountTests(TestCase):
    NAMES = {OWNER_NO: OWNER_NAME, MANAGER_NO: MANAGER_NAME}

    def judge(self, r, accepted):
        return rc.evaluate(
            r, expected_amount=Decimal("2000"), started_at=STARTED, now=NOW, shop_name=OWNER_NAME, accepted_at=lambda moment: accepted,
            known_numbers={OWNER_NO, MANAGER_NO}, names_by_number=self.NAMES)

    def test_a_receipt_to_the_managers_number_is_compared_with_the_managers_name(self):
        verdict = self.judge(reading(recipient_number=MANAGER_NO, recipient_name="MA••••S S."), [(MANAGER_NO, False)])
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_PASS, []))

    def test_the_owners_name_on_the_managers_number_looks_edited(self):
        verdict = self.judge(reading(recipient_number=MANAGER_NO, recipient_name="DE••••K C."), [(MANAGER_NO, False)])
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_NEEDS_CHECK, ["name_differs"]))

    def test_the_managers_name_on_the_owners_number_looks_edited_too(self):
        verdict = self.judge(reading(recipient_number=OWNER_NO, recipient_name="MA••••S S."), [(OWNER_NO, False)])
        self.assertEqual(verdict.codes, ["name_differs"])

    def test_a_receipt_to_the_owners_number_still_passes_with_her_name(self):
        verdict = self.judge(reading(recipient_number=OWNER_NO, recipient_name="DE••••K C."), [(OWNER_NO, False), (MANAGER_NO, False)])
        self.assertEqual(verdict.status, rc.STATUS_PASS)

    def test_a_hidden_number_on_the_managers_receipt_matches_the_managers_digits_and_name(self):
        verdict = self.judge(reading(recipient_number="", recipient_pattern="09?????4567", recipient_name="MA••••S S.", layout="sent_via_gcash"),
                             [(OWNER_NO, False), (MANAGER_NO, False)])
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_PASS, []))
        self.assertTrue(any("name matches" in note for note in verdict.notes))

    def test_a_number_that_is_not_in_use_is_refused_as_an_old_number(self):
        verdict = self.judge(reading(recipient_number=OWNER_NO), [(MANAGER_NO, False)])        # the owner's account was switched off long ago
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["old_number"]))

    def test_with_no_name_map_every_receipt_is_compared_with_the_shop_name_as_before(self):
        verdict = rc.evaluate(reading(recipient_number=OWNER_NO, recipient_name="DE••••K C."), expected_amount=Decimal("2000"), started_at=STARTED, now=NOW,
                              shop_name=OWNER_NAME, accepted_at=lambda moment: [(OWNER_NO, False)], known_numbers={OWNER_NO})
        self.assertEqual(verdict.status, rc.STATUS_PASS)


class TheWholeCheckSaysWhichAccountWasPaidTests(TestCase):
    def setUp(self):
        set_shop()

    SHARP = {"label": "sharp", "sharpness": 300.0, "brightness": 200.0, "width": 1080, "height": 2340, "tiny": False, "dark": False, "poor": False}

    def check(self, seen):
        """Both readings see `seen` (a refusal needs the second reading to agree); the picture measures as sharp."""
        second = (seen, "") if seen is not None else (None, "timeout")
        with patch("gowns.receipt_check.read_receipt", return_value=(seen, "")), patch("gowns.receipt_check.verify_read", return_value=second),                 patch("gowns.receipt_quality.picture_quality", return_value=self.SHARP):
            return rc.check_receipt(b"picture", "receipt.png", expected_amount=Decimal("2000"), now=NOW)

    def paid(self, **changes):
        return reading(**{"paid_at": NOW - timedelta(minutes=5), **changes})

    def switch_to_manager(self, grace):
        gcash_numbers.record_change(OWNER_NO, MANAGER_NO, grace, now=NOW - timedelta(minutes=30), account=MANAGER, old_account=OWNER)
        SiteSettings.objects.filter(pk=1).update(gcash_active=MANAGER)

    def test_a_payment_to_the_active_owner_account(self):
        verdict = self.check(self.paid(recipient_number=OWNER_NO, recipient_name="DE••••K C."))
        self.assertEqual((verdict.status, verdict.account), (rc.STATUS_PASS, OWNER))

    def test_a_payment_to_the_managers_account_once_it_is_active(self):
        self.switch_to_manager(24)
        verdict = self.check(self.paid(recipient_number=MANAGER_NO, recipient_name="MA••••S S."))
        self.assertEqual((verdict.status, verdict.codes, verdict.account), (rc.STATUS_PASS, [], MANAGER))

    def test_a_customer_who_paid_the_old_owner_qr_a_few_minutes_before_the_switch_is_still_accepted_and_still_waits_for_the_owner(self):
        self.switch_to_manager(24)
        verdict = self.check(self.paid(recipient_number=OWNER_NO, recipient_name="DE••••K C."))
        self.assertEqual(verdict.status, rc.STATUS_PASS)
        self.assertEqual(verdict.account, OWNER)                                  # the rule follows the account paid, not the active one

    def test_with_not_at_all_the_old_account_is_refused(self):
        self.switch_to_manager(0)
        verdict = self.check(self.paid(recipient_number=OWNER_NO, paid_at=NOW - timedelta(minutes=2)))
        self.assertEqual((verdict.status, verdict.codes), (rc.STATUS_REJECT, ["old_number"]))

    def test_a_receipt_that_shows_no_number_is_taken_to_be_the_account_customers_are_shown(self):
        self.switch_to_manager(24)
        verdict = self.check(self.paid(recipient_number="", recipient_name="MA••••S S."))
        self.assertEqual((verdict.status, verdict.account), (rc.STATUS_NEEDS_CHECK, MANAGER))

    def test_the_reader_being_down_still_names_the_active_account(self):
        verdict = self.check(None)
        self.assertEqual((verdict.status, verdict.account), (rc.STATUS_NEEDS_CHECK, OWNER))


# ---- Edit Profile endpoints -----------------------------------------------------------------------------------------------------------
class GcashEndpointBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_admin("tg_owner", UserProfile.Role.OWNER)
        cls.staff = make_admin("tg_staff", UserProfile.Role.STAFF)
        cls.holder = make_admin("tg_holder", UserProfile.Role.STAFF)
        User.objects.filter(pk=cls.holder.pk).update(first_name="Ana", last_name="Holder")
        cls.other_staff = make_admin("tg_other", UserProfile.Role.STAFF)
        cls.customer = User.objects.create_user("tg_customer", "tg_customer@gmail.com", "Pass-12345!")

    def setUp(self):
        self.client.force_login(self.owner)

    def details(self, **data):
        return self.client.post(reverse("arabela_admin:gcash_details_update"), data=json.dumps(data), content_type="application/json")

    def switch(self, active, grace=None):
        data = {"active": active}
        if grace is not None:
            data["old_number_grace_hours"] = grace
        return self.client.post(reverse("arabela_admin:gcash_active_update"), data=json.dumps(data), content_type="application/json")

    def upload(self, account=None, name="qr.png"):
        data = {"qr": png_upload(name)}
        if account:
            data["account"] = account
        with patch("arabela_admin.views.default_storage") as storage:
            storage.save.return_value = "gcash_qr/new.png"
            storage.url.return_value = "https://example.test/gcash_qr/new.png"
            storage.exists.return_value = False
            return self.client.post(reverse("arabela_admin:gcash_qr_upload"), data=data)

    def remove(self, **data):
        with patch("arabela_admin.views.default_storage") as storage:
            storage.exists.return_value = False
            return self.client.post(reverse("arabela_admin:gcash_qr_remove"), data=json.dumps(data), content_type="application/json")


class SavingEachAccountTests(GcashEndpointBase):
    def test_the_owners_account_is_saved_as_before(self):
        response = self.details(name=" Denmark Concepcion ", number="0948 874 5992")
        self.assertEqual(response.status_code, 200, response.content)
        shop = SiteSettings.load()
        self.assertEqual((shop.gcash_account_name, shop.gcash_number), (OWNER_NAME, OWNER_NO))
        self.assertEqual(shop.manager_gcash_number, "")

    def test_the_managers_account_is_saved_with_the_staff_account_that_holds_it(self):
        response = self.details(account="manager", name=MANAGER_NAME, number="0917 123 4567", holder_id=str(self.holder.pk))
        self.assertEqual(response.status_code, 200, response.content)
        shop = SiteSettings.load()
        self.assertEqual((shop.manager_gcash_name, shop.manager_gcash_number, shop.manager_gcash_holder_id), (MANAGER_NAME, MANAGER_NO, self.holder.pk))
        self.assertEqual(shop.gcash_number, "")                                       # the owner's account was not touched
        body = response.json()
        self.assertEqual((body["account"], body["active"], body["manager"]["holder_name"]), ("manager", OWNER, "Ana Holder"))

    def test_saving_the_inactive_account_changes_what_customers_see_in_no_way_and_writes_no_history(self):
        set_shop()
        self.details(account="manager", name="New Name", number="09175550000", holder_id=str(self.holder.pk))
        self.assertEqual(gcash_numbers.active_details()["number"], OWNER_NO)
        self.assertEqual(GcashNumberHistory.objects.count(), 0)

    def test_changing_the_number_of_the_account_in_use_remembers_the_old_one(self):
        set_shop(holder=self.holder)
        self.details(account="owner", name=OWNER_NAME, number="09175550000", old_number_grace_hours=24)
        rows = {row.number: (row.account, row.grace_hours) for row in GcashNumberHistory.objects.all()}
        self.assertEqual(rows[OWNER_NO], (OWNER, 24))
        self.assertEqual(rows["09175550000"], (OWNER, 0))

    def test_changing_the_managers_number_while_it_is_in_use_remembers_the_old_one_as_the_managers(self):
        set_shop(active=MANAGER, holder=self.holder)
        gcash_numbers.record_change(OWNER_NO, MANAGER_NO, 24, account=MANAGER, old_account=OWNER)
        self.details(account="manager", name=MANAGER_NAME, number="09175550000", holder_id=str(self.holder.pk), old_number_grace_hours=48)
        self.assertEqual({row.number: row.account for row in GcashNumberHistory.objects.all()}[MANAGER_NO], MANAGER)

    def test_the_two_accounts_cannot_share_a_number(self):
        set_shop(holder=self.holder)
        response = self.details(account="manager", name=MANAGER_NAME, number=OWNER_NO, holder_id=str(self.holder.pk))
        self.assertEqual(response.status_code, 400)
        self.assertIn("different numbers", response.json()["error"])
        response = self.details(account="owner", name=OWNER_NAME, number=MANAGER_NO)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SiteSettings.load().gcash_number, OWNER_NO)

    def test_only_an_active_staff_account_can_hold_the_managers_gcash(self):
        set_shop()
        User.objects.filter(pk=self.other_staff.pk).update(is_active=False)
        for bad in (self.owner.pk, self.customer.pk, self.other_staff.pk, 99999, "abc"):
            with self.subTest(holder=bad):
                response = self.details(account="manager", name=MANAGER_NAME, number=MANAGER_NO, holder_id=str(bad))
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("staff accounts", response.json()["error"])
        self.assertIsNone(SiteSettings.load().manager_gcash_holder_id)

    def test_the_holder_can_be_cleared_while_the_account_is_not_in_use(self):
        set_shop(holder=self.holder)
        self.assertEqual(self.details(account="manager", name=MANAGER_NAME, number=MANAGER_NO, holder_id="").status_code, 200)
        self.assertIsNone(SiteSettings.load().manager_gcash_holder_id)

    def test_the_managers_account_in_use_cannot_lose_its_name_number_or_holder(self):
        set_shop(active=MANAGER, holder=self.holder)
        for changes in ({"name": ""}, {"number": ""}, {"holder_id": ""}):
            with self.subTest(changes=changes):
                data = dict(account="manager", name=MANAGER_NAME, number=MANAGER_NO, holder_id=str(self.holder.pk))
                data.update(changes)
                response = self.details(**data)
                self.assertEqual(response.status_code, 400)
                self.assertIn("paying the manager's GCash right now", response.json()["error"])

    def test_an_unknown_account_is_refused(self):
        self.assertEqual(self.details(account="boss", name="x", number="").status_code, 400)

    def test_only_the_owner_can_change_either_account(self):
        for user in (self.staff, self.holder):
            self.client.force_login(user)
            self.assertEqual(self.details(account="manager", name="x", number=MANAGER_NO).status_code, 403)
            self.assertEqual(self.switch("manager").status_code, 403)
            self.assertEqual(self.upload("manager").status_code, 403)
            self.assertEqual(self.remove(account="manager").status_code, 403)
        self.client.force_login(self.customer)
        self.assertEqual(self.switch("manager").status_code, 401)
        self.assertEqual(SiteSettings.load().manager_gcash_number, "")


class TheQrOfEachAccountTests(GcashEndpointBase):
    def test_a_qr_goes_to_the_owner_by_default_and_to_the_manager_when_asked(self):
        self.assertEqual(self.upload().status_code, 200)
        shop = SiteSettings.load()
        self.assertEqual((shop.gcash_qr_url, shop.manager_gcash_qr_url), ("https://example.test/gcash_qr/new.png", ""))
        self.assertEqual(self.upload("manager").status_code, 200)
        shop = SiteSettings.load()
        self.assertEqual(shop.manager_gcash_qr_url, "https://example.test/gcash_qr/new.png")

    def test_an_unknown_account_is_refused_before_the_file_is_read(self):
        self.assertEqual(self.upload("boss").status_code, 400)

    def test_each_account_can_remove_its_own_qr_and_the_other_is_kept(self):
        set_shop(holder=self.holder)
        self.assertEqual(self.remove(account="manager").status_code, 200)
        shop = SiteSettings.load()
        self.assertEqual((shop.manager_gcash_qr_url, shop.gcash_qr_url), ("", OWNER_QR))
        self.assertEqual(self.remove().status_code, 200)                              # no account named = the owner's, as before
        self.assertEqual(SiteSettings.load().gcash_qr_url, "")

    def test_the_managers_qr_cannot_be_removed_while_customers_are_paying_it(self):
        set_shop(active=MANAGER, holder=self.holder)
        response = self.remove(account="manager")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SiteSettings.load().manager_gcash_qr_url, MANAGER_QR)

    def test_the_old_form_with_no_body_still_removes_the_owners_qr(self):
        set_shop()
        with patch("arabela_admin.views.default_storage") as storage:
            storage.exists.return_value = False
            response = self.client.post(reverse("arabela_admin:gcash_qr_remove"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(SiteSettings.load().gcash_qr_url, "")


class SwitchingTheActiveAccountTests(GcashEndpointBase):
    def test_the_switch_needs_the_manager_account_to_be_ready(self):
        for fields, missing in (
            (dict(manager_number=""), "a GCash number"),
            (dict(manager_name=""), "an account name"),
            (dict(manager_qr=""), "a QR picture"),
            (dict(holder=None), "an active staff account that holds it"),
        ):
            with self.subTest(missing=missing):
                set_shop(**{"holder": self.holder, **fields})
                response = self.switch("manager", 24)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("cannot be switched on yet", response.json()["error"])
                self.assertIn(missing, response.json()["error"])
                self.assertEqual(SiteSettings.load().gcash_active, OWNER)
                self.assertEqual(GcashNumberHistory.objects.count(), 0)

    def test_every_missing_piece_is_named_once(self):
        set_shop(holder=None, manager_number="", manager_name="", manager_qr="")
        message = self.switch("manager").json()["error"]
        for piece in ("a GCash number", "an account name", "a QR picture", "an active staff account that holds it"):
            self.assertEqual(message.count(piece), 1, message)

    def test_a_holder_whose_account_was_switched_off_does_not_count(self):
        set_shop(holder=self.holder)
        User.objects.filter(pk=self.holder.pk).update(is_active=False)
        self.assertEqual(self.switch("manager").status_code, 400)

    def test_switching_to_the_manager_changes_what_customers_see_and_keeps_the_old_number_for_the_grace(self):
        set_shop(holder=self.holder)
        response = self.switch("manager", 24)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()["active"], response.json()["changed"]), (MANAGER, True))
        self.assertEqual(gcash_numbers.active_details(), {"name": MANAGER_NAME, "number": MANAGER_NO, "qr_url": MANAGER_QR})
        rows = {row.number: (row.account, row.grace_hours, row.valid_until is not None) for row in GcashNumberHistory.objects.all()}
        self.assertEqual(rows, {OWNER_NO: (OWNER, 24, True), MANAGER_NO: (MANAGER, 0, False)})

    def test_the_grace_choices_are_zero_24_or_48_and_anything_else_becomes_48(self):
        for given, kept in ((0, 0), (24, 24), (48, 48), (7, 48), ("x", 48), (None, 48)):
            with self.subTest(given=given):
                GcashNumberHistory.objects.all().delete()
                set_shop(holder=self.holder)
                data = {"active": "manager"}
                if given is not None:
                    data["old_number_grace_hours"] = given
                self.client.post(reverse("arabela_admin:gcash_active_update"), data=json.dumps(data), content_type="application/json")
                self.assertEqual(GcashNumberHistory.objects.get(number=OWNER_NO).grace_hours, kept)

    def test_switching_back_to_the_owner_needs_only_her_number(self):
        set_shop(active=MANAGER, holder=self.holder)
        gcash_numbers.record_change(OWNER_NO, MANAGER_NO, 24, account=MANAGER, old_account=OWNER)
        response = self.switch("owner", 24)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(gcash_numbers.active_account(), OWNER)
        self.assertEqual(gcash_numbers.account_of_number(MANAGER_NO), MANAGER)

    def test_switching_to_the_account_already_in_use_does_nothing(self):
        set_shop(holder=self.holder)
        response = self.switch("owner", 24)
        self.assertEqual((response.status_code, response.json()["changed"]), (200, False))
        self.assertEqual(GcashNumberHistory.objects.count(), 0)

    def test_bad_requests_change_nothing(self):
        set_shop(holder=self.holder)
        for data in ({}, {"active": "boss"}, {"active": ""}, {"active": None}):
            response = self.client.post(reverse("arabela_admin:gcash_active_update"), data=json.dumps(data), content_type="application/json")
            self.assertEqual(response.status_code, 400, data)
        self.assertEqual(self.client.post(reverse("arabela_admin:gcash_active_update"), data="{oops", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.get(reverse("arabela_admin:gcash_active_update")).status_code, 405)
        self.assertEqual(SiteSettings.load().gcash_active, OWNER)

    def test_the_owner_with_no_number_of_her_own_can_still_switch_to_a_ready_manager(self):
        set_shop(holder=self.holder, owner_number="")
        self.assertEqual(self.switch("manager", 24).status_code, 200)

    def test_the_audit_log_keeps_every_change(self):
        set_shop(holder=self.holder)
        self.switch("manager", 0)
        self.details(account="manager", name="Maria S", number=MANAGER_NO, holder_id=str(self.holder.pk))
        lines = {row.action: row for row in AuditLog.objects.filter(action__startswith="settings.gcash")}
        self.assertIn("switched the GCash customers pay to the manager's GCash", lines["settings.gcash_active"].summary)
        self.assertTrue(lines["settings.gcash_active"].sensitive)
        self.assertEqual(lines["settings.gcash_active"].details["Customers pay to"], ["the owner's", "the manager's"])
        self.assertEqual(lines["settings.gcash_active"].details["The old GCash still counted for"], "not at all")
        self.assertIn("the manager's GCash details", lines["settings.gcash"].summary)

    def test_a_refused_attempt_by_staff_is_logged_as_refused(self):
        set_shop(holder=self.holder)
        self.client.force_login(self.staff)
        self.switch("manager")
        row = AuditLog.objects.get(action="settings.gcash_active")
        self.assertEqual(row.outcome, "refused")


# ---- the rule on Approve ---------------------------------------------------------------------------------------------------------------
class TheOwnersGcashHoldsApproveTests(OwnerCheckBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.holder = make_admin("oc_holder", UserProfile.Role.STAFF)
        User.objects.filter(pk=cls.holder.pk).update(first_name="Ana", last_name="Holder")

    def setUp(self):
        super().setUp()
        set_shop(holder=self.holder)

    def approve(self, user, reservation):
        self.client.force_login(user)
        return self.client.post(reverse("arabela_admin:reservation_approve", args=[reservation.pk]), data="{}", content_type="application/json")

    def reject(self, user, reservation, reason="not paid"):
        self.client.force_login(user)
        return self.client.post(reverse("arabela_admin:reservation_reject", args=[reservation.pk]), data=json.dumps({"reason": reason}),
                                content_type="application/json")

    def paid(self, account, **fields):
        return self.booking(proof_account=account, **fields)[0]

    # -- staff must wait for a payment to the owner's GCash
    def test_staff_cannot_approve_a_payment_to_the_owners_gcash_until_she_confirms_it(self):
        reservation = self.paid(OWNER)
        for user in (self.staff, self.manager, self.holder, self.no_profile):
            response = self.approve(user, reservation)
            self.assertEqual(response.status_code, 409, (user, response.content))
            self.assertTrue(response.json()["waiting_for_owner"])
            self.assertIn("Waiting for the owner to confirm the payment in GCash", response.json()["error"])
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)

    def test_a_refused_approve_changes_nothing_at_all(self):
        reservation = self.paid(OWNER)
        before = (ReservationStatusEvent.objects.count(), CustomerMessage.objects.count())
        self.approve(self.staff, reservation)
        self.assertEqual((ReservationStatusEvent.objects.count(), CustomerMessage.objects.count()), before)
        self.assertEqual(Gown.objects.filter(status=Gown.Status.RESERVED).count(), 0)
        reservation.refresh_from_db()
        self.assertIsNone(reservation.reviewed_at)
        self.assertEqual(reservation.owner_check, Reservation.OwnerCheck.PENDING)

    def test_after_the_owner_presses_received_staff_can_approve(self):
        reservation = self.paid(OWNER)
        self.assertEqual(self.answer(self.owner, reservation, "received").status_code, 200)
        response = self.approve(self.staff, reservation)
        self.assertEqual(response.status_code, 200, response.content)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)

    def test_the_owners_own_approve_counts_as_received(self):
        reservation = self.paid(OWNER)
        for user in (self.owner, self.superuser):
            reservation.delete()
            reservation = self.paid(OWNER)
            response = self.approve(user, reservation)
            self.assertEqual(response.status_code, 200, (user, response.content))
            reservation.refresh_from_db()
            self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
            self.assertEqual((reservation.owner_check, reservation.owner_checked_by_id), (Reservation.OwnerCheck.RECEIVED, user.pk))
            self.assertIsNotNone(reservation.owner_checked_at)
            event = ReservationStatusEvent.objects.filter(reservation=reservation, label="Payment confirmed by the owner").get()
            self.assertIn("approved this booking", event.detail)
            self.assertTrue(event.staff_only)

    def test_the_owners_approve_of_a_booking_she_does_not_have_to_confirm_does_not_touch_the_check(self):
        for account in (MANAGER, ""):
            reservation = self.paid(account)
            self.assertEqual(self.approve(self.owner, reservation).status_code, 200)
            reservation.refresh_from_db()
            self.assertEqual(reservation.owner_check, Reservation.OwnerCheck.PENDING, account)

    def test_rejecting_is_never_held_back(self):
        reservation = self.paid(OWNER)
        response = self.reject(self.staff, reservation)
        self.assertEqual(response.status_code, 200, response.content)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.REJECTED)

    def test_the_owner_saying_not_found_still_cancels_the_booking(self):
        reservation = self.paid(OWNER)
        self.assertEqual(self.answer(self.owner, reservation, "not_found").status_code, 200)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.REJECTED)

    # -- a payment to the manager's GCash is approved as usual
    def test_a_payment_to_the_managers_gcash_is_approved_as_usual(self):
        reservation = self.paid(MANAGER)
        response = self.approve(self.staff, reservation)
        self.assertEqual(response.status_code, 200, response.content)
        reservation.refresh_from_db()
        self.assertEqual((reservation.status, reservation.owner_check), (Reservation.Status.CONFIRMED, Reservation.OwnerCheck.PENDING))

    def test_older_bookings_and_walk_ins_are_never_held_back(self):
        for check in (Reservation.OwnerCheck.PENDING, ""):
            reservation = self.paid("", owner=check)
            self.assertEqual(self.approve(self.staff, reservation).status_code, 200, check)

    def test_a_booking_the_owner_already_confirmed_is_approved_by_anyone(self):
        reservation = self.paid(OWNER, owner=Reservation.OwnerCheck.RECEIVED)
        self.assertEqual(self.approve(self.staff, reservation).status_code, 200)

    # -- the rule follows the account paid, not the one in use
    def test_switching_the_active_account_never_frees_an_older_owner_payment(self):
        reservation = self.paid(OWNER)
        SiteSettings.objects.filter(pk=1).update(gcash_active=MANAGER)
        self.assertEqual(self.approve(self.staff, reservation).status_code, 409)
        SiteSettings.objects.filter(pk=1).update(gcash_active=OWNER)
        manager_paid = self.paid(MANAGER)
        self.assertEqual(self.approve(self.staff, manager_paid).status_code, 200)             # the owner being active again does not trap it either

    # -- the manager's account out of service
    def test_when_the_managers_account_is_out_of_service_its_payments_fall_back_to_the_owner(self):
        reservation = self.paid(MANAGER)
        for change, why in (
            (lambda: User.objects.filter(pk=self.holder.pk).update(is_active=False), "holder switched off"),
            (lambda: SiteSettings.objects.filter(pk=1).update(manager_gcash_holder=None), "no holder"),
        ):
            set_shop(holder=self.holder)
            User.objects.filter(pk=self.holder.pk).update(is_active=True)
            change()
            response = self.approve(self.staff, reservation)
            self.assertEqual(response.status_code, 409, why)
            self.assertTrue(response.json()["waiting_for_owner"], why)
        set_shop(holder=self.holder, manager_number="")
        self.assertEqual(self.approve(self.staff, reservation).status_code, 409)               # no number: out of service too

    def test_a_deleted_holder_account_puts_the_payments_back_on_the_owner(self):
        reservation = self.paid(MANAGER)
        self.holder.delete()
        self.assertIsNone(SiteSettings.load().manager_gcash_holder_id)
        self.assertEqual(self.approve(self.staff, reservation).status_code, 409)

    def test_the_owner_can_approve_those_and_that_counts_as_her_confirmation(self):
        reservation = self.paid(MANAGER)
        User.objects.filter(pk=self.holder.pk).update(is_active=False)
        self.assertEqual(self.approve(self.owner, reservation).status_code, 200)
        reservation.refresh_from_db()
        self.assertEqual(reservation.owner_check, Reservation.OwnerCheck.RECEIVED)

    # -- double clicks and old pages
    def test_the_second_approve_of_the_same_booking_is_still_told_it_was_already_decided(self):
        reservation = self.paid(MANAGER)
        self.assertEqual(self.approve(self.staff, reservation).status_code, 200)
        again = self.approve(self.staff, reservation)
        self.assertEqual(again.status_code, 409)
        self.assertNotIn("waiting_for_owner", again.json())
        self.assertIn("already approved", again.json()["error"])

    def test_a_missing_booking_is_a_404_not_a_waiting_message(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(reverse("arabela_admin:reservation_approve", args=[999999]), data="{}", content_type="application/json").status_code, 404)

    def test_the_audit_log_has_no_line_for_the_refused_approve(self):
        from accounts.models import AuditLog
        reservation = self.paid(OWNER)
        self.approve(self.staff, reservation)
        self.assertFalse(AuditLog.objects.filter(action="booking.approve").exists())
        self.approve(self.owner, reservation)
        self.assertEqual(AuditLog.objects.filter(action="booking.approve").count(), 1)


class WhoCanPressReceivedTests(OwnerCheckBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.holder = make_admin("oc_holder2", UserProfile.Role.STAFF)

    def setUp(self):
        super().setUp()
        set_shop(holder=self.holder)

    def test_the_holder_may_answer_for_a_payment_to_the_managers_gcash(self):
        reservation, _ = self.booking(proof_account=MANAGER)
        response = self.answer(self.holder, reservation, "received")
        self.assertEqual(response.status_code, 200, response.content)
        reservation.refresh_from_db()
        self.assertEqual((reservation.owner_check, reservation.owner_checked_by_id), (Reservation.OwnerCheck.RECEIVED, self.holder.pk))
        event = ReservationStatusEvent.objects.get(reservation=reservation, label="Payment confirmed in the manager's GCash")
        self.assertIn("manager's GCash account", event.detail)

    def test_the_holder_saying_not_found_cancels_the_booking_like_the_owner_would(self):
        reservation, _ = self.booking(proof_account=MANAGER)
        self.assertEqual(self.answer(self.holder, reservation, "not_found").status_code, 200)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.REJECTED)

    def test_the_holder_may_not_answer_for_the_owners_gcash_or_an_older_booking(self):
        for account in (OWNER, ""):
            reservation, _ = self.booking(proof_account=account)
            response = self.answer(self.holder, reservation, "received")
            self.assertEqual(response.status_code, 403, account)
            self.assertIn("Only the owner", response.json()["error"])
            reservation.refresh_from_db()
            self.assertEqual(reservation.owner_check, Reservation.OwnerCheck.PENDING)

    def test_other_staff_may_not_answer_even_for_the_managers_gcash(self):
        reservation, _ = self.booking(proof_account=MANAGER)
        for user in (self.staff, self.manager, self.no_profile):
            self.assertEqual(self.answer(user, reservation, "received").status_code, 403)

    def test_a_holder_who_is_no_longer_in_service_cannot_answer(self):
        reservation, _ = self.booking(proof_account=MANAGER)
        SiteSettings.objects.filter(pk=1).update(manager_gcash_number="")
        self.assertEqual(self.answer(self.holder, reservation, "received").status_code, 403)

    def test_the_owner_may_answer_for_either_account(self):
        for account in (OWNER, MANAGER, ""):
            reservation, _ = self.booking(proof_account=account)
            self.assertEqual(self.answer(self.owner, reservation, "received").status_code, 200, account)


class TheOwnersListCarriesOnlyHerPaymentsTests(OwnerCheckBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.holder = make_admin("oc_holder3", UserProfile.Role.STAFF)

    def setUp(self):
        super().setUp()
        set_shop(holder=self.holder)

    def test_a_payment_to_the_managers_gcash_is_not_on_her_list_or_in_her_count(self):
        mine, _ = self.booking(0, proof_account=OWNER)
        legacy, _ = self.booking(1, proof_account="")
        manager_paid, _ = self.booking(2, proof_account=MANAGER)
        ids = set(owner_check.awaiting().values_list("pk", flat=True))
        self.assertEqual(ids, {mine.pk, legacy.pk})
        self.assertNotIn(manager_paid.pk, ids)

    def test_when_the_managers_account_is_out_of_service_those_payments_come_back_to_her(self):
        manager_paid, _ = self.booking(2, proof_account=MANAGER)
        User.objects.filter(pk=self.holder.pk).update(is_active=False)
        self.assertIn(manager_paid.pk, set(owner_check.awaiting().values_list("pk", flat=True)))

    def test_her_reminder_email_does_not_list_the_managers_payments(self):
        self.booking(0, proof_account=OWNER, age=timedelta(hours=13))
        manager_paid, _ = self.booking(1, proof_account=MANAGER, age=timedelta(hours=13))
        due = set(owner_check.due_for_reminder().values_list("pk", flat=True))
        self.assertNotIn(manager_paid.pk, due)
        self.assertEqual(len(due), 1)

    def test_the_owners_bell_counts_only_her_payments(self):
        from arabela_admin import notifications
        self.booking(0, proof_account=OWNER)
        self.booking(1, proof_account=MANAGER)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("arabela_admin:payment_verification"))
        self.assertEqual(response.context["owner_check_count"], 1)

    def test_the_rule_helpers_agree_with_each_other(self):
        for account, owner_check_value, status, expected in (
            (OWNER, "pending", "Pending", True), (OWNER, "received", "Pending", False), (OWNER, "pending", "Confirmed", False),
            (MANAGER, "pending", "Pending", False), ("", "pending", "Pending", False), (OWNER, "", "Pending", False),
        ):
            row = Reservation(proof_account=account, owner_check=owner_check_value, status=status)
            self.assertEqual(owner_check.blocks_approval(row), expected, (account, owner_check_value, status))
        self.assertEqual(owner_check.owner_must_confirm(Reservation(proof_account=OWNER, owner_check="pending", status="Confirmed")), True)

    def test_one_look_at_the_managers_account_serves_a_whole_list(self):
        rows = [self.booking(n, proof_account=MANAGER)[0] for n in range(3)]
        fresh = list(Reservation.objects.filter(pk__in=[r.pk for r in rows]))
        with self.assertNumQueries(2):                                                      # the settings row and the holder, once for all three
            owner_check.annotate_waiting(fresh)
        for row in fresh:
            with self.assertNumQueries(0):
                self.assertFalse(row.waiting_for_owner)


# ---- what each person sees -------------------------------------------------------------------------------------------------------------
class ThePagesShowTheRuleTests(OwnerCheckBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.holder = make_admin("oc_holder4", UserProfile.Role.STAFF)
        User.objects.filter(pk=cls.holder.pk).update(first_name="Ana", last_name="Holder")

    def setUp(self):
        super().setUp()
        set_shop(holder=self.holder)

    def page(self, user, name):
        self.client.force_login(user)
        response = self.client.get(reverse(f"arabela_admin:{name}"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_staff_see_a_locked_approve_on_a_payment_to_the_owners_gcash_on_every_page(self):
        reservation, _ = self.booking(proof_account=OWNER)
        approve_url = reverse("arabela_admin:reservation_approve", args=[reservation.pk])
        for name in ("pending_approval", "payment_verification", "dashboard"):
            html = self.page(self.staff, name)
            self.assertIn('data-waiting-owner="1"', html, name)
            self.assertIn("Waiting for the owner to confirm the payment in GCash", html, name)
            self.assertNotIn(f'data-url="{approve_url}"', html, name)                       # the live Approve is not on the page at all
            self.assertIn(reverse("arabela_admin:reservation_reject", args=[reservation.pk]), html, name)     # Reject is

    def test_the_owner_sees_a_working_approve_for_the_same_booking(self):
        reservation, _ = self.booking(proof_account=OWNER)
        approve_url = reverse("arabela_admin:reservation_approve", args=[reservation.pk])
        for name in ("pending_approval", "payment_verification", "dashboard"):
            html = self.page(self.owner, name)
            self.assertNotIn('data-waiting-owner="1"', html, name)
            self.assertIn(f'data-url="{approve_url}"', html, name)

    def test_a_payment_to_the_managers_gcash_has_a_normal_approve_for_staff(self):
        reservation, _ = self.booking(proof_account=MANAGER)
        approve_url = reverse("arabela_admin:reservation_approve", args=[reservation.pk])
        for name in ("pending_approval", "payment_verification", "dashboard"):
            html = self.page(self.staff, name)
            self.assertNotIn('data-waiting-owner="1"', html, name)
            self.assertIn(f'data-url="{approve_url}"', html, name)

    def test_the_lock_opens_the_moment_the_owner_confirms(self):
        reservation, _ = self.booking(proof_account=OWNER)
        self.assertIn('data-waiting-owner="1"', self.page(self.staff, "pending_approval"))
        self.answer(self.owner, reservation, "received")
        self.assertNotIn('data-waiting-owner="1"', self.page(self.staff, "pending_approval"))

    def test_the_proof_window_data_says_when_a_booking_is_waiting(self):
        waiting, _ = self.booking(0, proof_account=OWNER)
        free, _ = self.booking(1, proof_account=MANAGER)
        self.assertTrue(waiting.proof_window["waiting"])
        self.assertFalse(free.proof_window["waiting"])
        self.assertEqual((waiting.proof_window["account"], free.proof_window["account"]), (OWNER, MANAGER))
        self.assertEqual(free.proof_window["accountLabel"], "Manager’s GCash")

    def test_the_badges_say_whose_gcash_it_is(self):
        waiting, _ = self.booking(0, proof_account=OWNER)
        free, _ = self.booking(1, proof_account=MANAGER)
        self.assertIn(("warn", "Waiting for the owner to confirm"), waiting.proof_badges)
        self.assertIn(("muted", "Paid to the manager’s GCash"), free.proof_badges)

    def test_the_banner_says_whose_gcash_customers_pay(self):
        html = self.page(self.staff, "pending_approval")
        self.assertIn("Payments go to:", html)
        self.assertIn("Owner&rsquo;s GCash", html)
        SiteSettings.objects.filter(pk=1).update(gcash_active=MANAGER)
        for name in ("pending_approval", "payment_verification", "dashboard"):
            html = self.page(self.staff, name)
            self.assertIn("Manager&rsquo;s GCash (held by Ana Holder)", html, name)
            self.assertIn('data-active="manager"', html, name)

    def test_only_the_owner_and_the_holder_get_the_received_buttons(self):
        reservation, _ = self.booking(proof_account=MANAGER)
        marker = "Received &#10003;</button>"
        self.assertIn(marker, self.page(self.owner, "payment_verification"))
        holder_html = self.page(self.holder, "payment_verification")
        self.assertIn(marker, holder_html)
        self.assertNotIn("To check in my GCash", holder_html)                               # the owner's own strip is hers alone
        for user in (self.staff, self.manager, self.no_profile):
            self.assertNotIn(marker, self.page(user, "payment_verification"), user)

    def test_each_row_says_who_may_answer_it(self):
        reservation, _ = self.booking(proof_account=OWNER)
        free, _ = self.booking(1, proof_account=MANAGER)
        self.client.force_login(self.holder)
        rows = {r.pk: r.can_answer_check for r in self.client.get(reverse("arabela_admin:payment_verification")).context["reservations"]}
        self.assertEqual(rows, {reservation.pk: False, free.pk: True})


class TheEditProfileCardTests(GcashEndpointBase):
    def page(self, user):
        self.client.force_login(user)
        response = self.client.get(reverse("arabela_admin:page", kwargs={"page": "profile"}))
        self.assertEqual(response.status_code, 200)
        return response

    def test_the_owner_sees_both_accounts_the_switch_and_the_staff_to_choose_from(self):
        set_shop(holder=self.holder)
        response = self.page(self.owner)
        html = response.content.decode()
        for marker in ('id="gcash-card"', 'id="gcash-pay-owner"', 'id="gcash-pay-manager"', 'id="gcash-switch-panel"', 'id="gcash-holder"',
                       'id="gcash-switch-grace"', "Customers pay to", "Held by", "In use now"):
            self.assertIn(marker, html, marker)
        self.assertIn(f'data-manager-holder="{self.holder.pk}"', html)
        self.assertIn(f'data-manager-number="{MANAGER_NO}"', html)
        self.assertIn(f'data-owner-number="{OWNER_NO}"', html)
        self.assertIn('data-active="owner"', html)
        self.assertEqual([row["name"] for row in response.context["gcash_holders"]].count("Ana Holder"), 1)

    def test_the_choice_of_staff_has_no_owner_no_customer_and_no_switched_off_account(self):
        User.objects.filter(pk=self.other_staff.pk).update(is_active=False)
        names = {row["id"] for row in self.page(self.owner).context["gcash_holders"]}
        self.assertIn(self.holder.pk, names)
        self.assertNotIn(self.owner.pk, names)
        self.assertNotIn(self.customer.pk, names)
        self.assertNotIn(self.other_staff.pk, names)

    def test_the_active_account_is_what_the_card_starts_on(self):
        set_shop(active=MANAGER, holder=self.holder)
        self.assertIn('data-active="manager"', self.page(self.owner).content.decode())

    def test_staff_never_get_the_card_or_the_staff_list(self):
        response = self.page(self.staff)
        self.assertNotIn('id="gcash-card"', response.content.decode())
        self.assertEqual(response.context["gcash_holders"], [])

    def test_the_card_has_no_template_code_left_in_it(self):
        html = self.page(self.owner).content.decode()
        card = html[html.index('id="gcash-card"'):]
        for leak in ("{%", "{{"):
            self.assertNotIn(leak, card.split("Security deposit")[0])


class WhatCustomersSeeTests(GcashEndpointBase):
    def test_the_checkout_and_the_qr_page_show_the_active_account_only(self):
        set_shop(holder=self.holder)
        self.client.force_login(self.customer)
        html = self.client.get(reverse("gowns:gcash_qr")).content.decode()
        self.assertIn(OWNER_NO, html)
        self.assertIn(OWNER_QR, html)
        self.assertNotIn(MANAGER_NO, html)
        self.assertNotIn(MANAGER_QR, html)
        SiteSettings.objects.filter(pk=1).update(gcash_active=MANAGER)
        html = self.client.get(reverse("gowns:gcash_qr")).content.decode()
        self.assertIn(MANAGER_NO, html)
        self.assertIn(MANAGER_QR, html)
        self.assertIn(MANAGER_NAME, html)
        self.assertNotIn(OWNER_NO, html)
        self.assertNotIn(OWNER_QR, html)


# ---- the checkout remembers which account was paid ---------------------------------------------------------------------------------------
class TheCheckoutRemembersTheAccountPaidTests(ReceiptCheckBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.holder = make_admin("rc_holder", UserProfile.Role.STAFF)

    def test_a_payment_to_the_owners_gcash(self):
        self.reads(good_reading())
        reservation = self.booked(self.checkout("a"))
        self.assertEqual((reservation.proof_account, reservation.owner_check), (OWNER, Reservation.OwnerCheck.PENDING))
        self.assertTrue(reservation.waiting_for_owner)

    def test_a_payment_to_the_managers_gcash_after_the_switch(self):
        set_shop(holder=self.holder)
        gcash_numbers.record_change(OWNER_NO, MANAGER_NO, 24, account=MANAGER, old_account=OWNER)
        SiteSettings.objects.filter(pk=1).update(gcash_active=MANAGER)
        self.reads(good_reading(recipient_number=MANAGER_NO, recipient_name="MA****S S."))
        reservation = self.booked(self.checkout("b"))
        self.assertEqual(reservation.proof_account, MANAGER)
        self.assertFalse(reservation.waiting_for_owner)
        self.assertEqual(reservation.proof_check, Reservation.ProofCheck.PASS)

    def test_the_old_owner_qr_paid_just_before_the_switch_still_books_and_still_waits_for_her(self):
        set_shop(holder=self.holder)
        gcash_numbers.record_change(OWNER_NO, MANAGER_NO, 24, account=MANAGER, old_account=OWNER)
        SiteSettings.objects.filter(pk=1).update(gcash_active=MANAGER)
        self.reads(good_reading(recipient_number=OWNER_NO, recipient_name="DE****K C."))
        reservation = self.booked(self.checkout("c"))
        self.assertEqual(reservation.proof_account, OWNER)
        self.assertTrue(reservation.waiting_for_owner)

    def test_a_receipt_to_a_number_no_longer_used_is_refused_and_books_nothing(self):
        set_shop(holder=self.holder)
        gcash_numbers.record_change(OWNER_NO, MANAGER_NO, 0, now=timezone.now() - timedelta(hours=2), account=MANAGER, old_account=OWNER)
        SiteSettings.objects.filter(pk=1).update(gcash_active=MANAGER)
        self.reads(good_reading(recipient_number=OWNER_NO))
        data = self.refused(self.checkout("d"))
        self.assertIn("no longer used", data["error"])
        self.nothing_booked()

    def test_a_walk_in_booking_has_no_account_and_is_never_held_back(self):
        from reservations import staff_booking
        self.assertFalse(hasattr(staff_booking, "proof_account"))
        row = Reservation(customer_name="Walk In", status=Reservation.Status.PENDING, owner_check="")
        self.assertEqual(row.proof_account, "")
        self.assertFalse(owner_check.blocks_approval(row))
