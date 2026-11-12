"""Owner and Staff, with four sensitive actions the owner can allow one staff account at a time (accounts/roles.py).

Every staff account does the daily work. Four actions need a permission the owner ticks in Staff Management: delete gowns (one or in bulk),
cancel a booking or some of its gowns, flag/unflag a customer and remove a cancellation lock, and delete a receipt photo. This file checks,
for each of those actions: an account without THAT permission is refused (403) and NOTHING changes -- including an account that has the
other three; an account with it, an owner (by role) and a superuser can do it; nobody signed in is still refused with 401. It also checks
that the buttons follow the permissions, that everyday work still works for every staff account, that the owner-only pages stay owner-only,
that Staff Management saves permissions (and only the owner can), that an old Manager row still works until it is converted, and that the
migration converts it."""
import importlib
import json
from datetime import timedelta
from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomerMessage, UserProfile
from accounts.roles import PERMISSION_KEYS, can, granted_permissions, join_permissions, parse_permissions, permission_message, staff_display_name
from arabela_admin.test_cancel_booking import Base, make_gown
from gowns.models import Gown, GownRemoval
from reservations.models import ReceiptRecord, Reservation, ReservationItem

User = get_user_model()
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"role-test" + b"\xff\xd9"


def make_staff(username, permissions=(), first="Sam", last="Staff", role=UserProfile.Role.STAFF):
    user = User.objects.create_user(username, password="x", is_staff=True, first_name=first, last_name=last)
    UserProfile.objects.update_or_create(user=user, defaults={"role": role, "permissions": join_permissions(permissions)})
    return user


class RoleBase(Base):
    """Base (arabela_admin/test_cancel_booking.py) already has an old-style Manager row (`staff`), a superuser owner and a customer."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.legacy_manager = cls.staff
        cls.plain_staff = make_staff("rp_staff")
        cls.only = {key: make_staff(f"rp_only_{key}", [key]) for key in PERMISSION_KEYS}                 # exactly one permission each
        cls.all_four = make_staff("rp_all", PERMISSION_KEYS, first="Ada", last="Senior")
        cls.owner_by_role = User.objects.create_user("rp_owner_role", password="x", is_staff=True, first_name="Olga", last_name="Owner")
        UserProfile.objects.update_or_create(user=cls.owner_by_role, defaults={"role": UserProfile.Role.OWNER})
        cls.bare = User.objects.create_user("rp_bare", password="x", is_staff=True)      # an admin account with no profile at all
        cls.refused_accounts = {"Staff with no permission": cls.plain_staff, "admin account with no profile": cls.bare}
        cls.allowed_accounts = {"old Manager row": cls.legacy_manager, "owner by role": cls.owner_by_role, "superuser owner": cls.owner,
                                "staff with all four": cls.all_four}

    def setUp(self):
        super().setUp()
        self.serial = count(1, 10)           # a fresh block of gown numbers for every scenario

    # ---------------------------------------------------------------- the eight guarded actions, each with fresh data
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
        # attached long ago by someone else: only the permission can delete it (the 15-minute undo is tested in test_receipt_rules.py)
        receipt = ReceiptRecord.objects.create(reservation=reservation, photo_url="https://files.example/receipt_photos/role_test.jpg",
                                               uploaded_by=self.legacy_manager, uploaded_at=timezone.now() - timedelta(days=2))
        send = lambda: self.client.post(reverse("arabela_admin:receipt_delete", args=[receipt.id]))
        return send, lambda: not ReceiptRecord.objects.filter(id=receipt.id).exists()

    # name -> the permission it needs
    NEEDS = {"delete_a_gown": "delete_gowns", "bulk_delete_gowns": "delete_gowns", "cancel_a_booking": "cancel_bookings",
             "cancel_some_gowns": "cancel_bookings", "flag_a_customer": "flag_customers", "unflag_a_customer": "flag_customers",
             "remove_a_lock": "flag_customers", "delete_a_receipt": "delete_receipts"}
    SCENARIOS = tuple(NEEDS)

    def scenario(self, name):
        return getattr(self, "scenario_" + name)()

    def refusal_for(self, name):
        """What the server says to an account that may not do `name` (the receipt delete has its own wording: it also explains the 15-minute undo)."""
        from arabela_admin.views import RECEIPT_DELETE_REFUSED_MESSAGE
        return RECEIPT_DELETE_REFUSED_MESSAGE if name == "delete_a_receipt" else permission_message(self.NEEDS[name])


class PermissionMatrixTests(RoleBase):
    def test_an_account_without_the_permission_is_refused_and_nothing_changes(self):
        for who, account in self.refused_accounts.items():
            for name in self.SCENARIOS:
                with self.subTest(account=who, action=name):
                    send, changed = self.scenario(name)
                    self.client.force_login(account)
                    response = send()
                    self.assertEqual(response.status_code, 403, response.content)
                    self.assertEqual(response.json()["error"], self.refusal_for(name))
                    self.assertFalse(changed())

    def test_having_a_different_permission_does_not_help(self):
        """Staff with one permission can do exactly that one's actions and are refused the rest."""
        for owned, account in self.only.items():
            for name in self.SCENARIOS:
                with self.subTest(owned=owned, action=name):
                    send, changed = self.scenario(name)
                    self.client.force_login(account)
                    response = send()
                    if self.NEEDS[name] == owned:
                        self.assertEqual(response.status_code, 200, response.content)
                        self.assertTrue(changed())
                    else:
                        self.assertEqual(response.status_code, 403, response.content)
                        self.assertEqual(response.json()["error"], self.refusal_for(name))
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

    def test_an_allowed_account_an_owner_and_a_superuser_can_do_each_of_them(self):
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

    def test_a_deactivated_account_with_every_permission_can_do_nothing(self):
        User.objects.filter(pk=self.all_four.pk).update(is_active=False)
        self.all_four.refresh_from_db()
        self.assertEqual(granted_permissions(self.all_four), frozenset())

    def test_the_staff_member_who_deletes_a_gown_is_the_name_in_the_removal_log(self):
        gown = make_gown(next(self.serial))
        self.client.force_login(self.only["delete_gowns"])
        self.assertEqual(self.post(reverse("arabela_admin:gown_delete", args=[gown.id]), {"reason": "Damaged"}).status_code, 200)
        self.assertEqual(GownRemoval.objects.get().removed_by_name, "Sam Staff")

    def test_changing_the_permissions_changes_the_access_straight_away(self):
        gown = make_gown(next(self.serial))
        self.client.force_login(self.plain_staff)
        self.assertEqual(self.post(reverse("arabela_admin:gown_delete", args=[gown.id]), {"reason": "Damaged"}).status_code, 403)
        UserProfile.objects.filter(user=self.plain_staff).update(permissions="delete_gowns")
        self.assertEqual(self.post(reverse("arabela_admin:gown_delete", args=[gown.id]), {"reason": "Damaged"}).status_code, 200)
        UserProfile.objects.filter(user=self.plain_staff).update(permissions="")
        again = make_gown(next(self.serial))
        self.assertEqual(self.post(reverse("arabela_admin:gown_delete", args=[again.id]), {"reason": "Damaged"}).status_code, 403)


class DailyWorkTests(RoleBase):
    def test_every_staff_account_still_does_the_daily_work(self):
        self.client.force_login(self.plain_staff)
        gown = make_gown(next(self.serial))
        other = make_gown(next(self.serial))
        self.assertEqual(self.post(reverse("arabela_admin:gown_status_update", args=[gown.id]), {"status": "Out-of-Stock"}).status_code, 200)
        bulk = reverse("arabela_admin:gown_bulk_action")
        self.assertEqual(self.post(bulk, {"action": "status", "ids": [gown.id, other.id], "status": "Available"}).status_code, 200)
        self.assertEqual(self.post(bulk, {"action": "checked", "ids": [gown.id, other.id]}).status_code, 200)
        self.assertEqual(Gown.objects.filter(id__in=[gown.id, other.id]).count(), 2)
        pending, _ = self.booking(status=Reservation.Status.PENDING, verified=False, days_late=-5, number_from=next(self.serial))
        to_reject, _ = self.booking(status=Reservation.Status.PENDING, verified=False, days_late=-5, number_from=next(self.serial))
        self.assertEqual(self.post(reverse("arabela_admin:reservation_approve", args=[pending.pk])).status_code, 200)
        self.assertEqual(self.post(reverse("arabela_admin:reservation_reject", args=[to_reject.pk]), {"reason": "Payment unclear"}).status_code, 200)
        self.assertEqual(Reservation.objects.get(pk=pending.pk).status, Reservation.Status.CONFIRMED)
        self.assertEqual(Reservation.objects.get(pk=to_reject.pk).status, Reservation.Status.REJECTED)
        confirmed, [item] = self.booking(number_from=next(self.serial))
        self.assertEqual(self.client.post(reverse("arabela_admin:reservation_item_mark_picked_up", args=[item.id])).status_code, 200)
        self.assertEqual(ReservationItem.objects.get(pk=item.pk).stage, ReservationItem.Stage.RESERVED)
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

    def test_staff_management_categories_and_the_audit_log_stay_owner_only_even_with_every_permission(self):
        for who, account in (("staff with all four", self.all_four), ("plain staff", self.plain_staff)):
            self.client.force_login(account)
            for page in ("staff_management", "categories", "audit_log"):
                with self.subTest(account=who, page=page):
                    self.assertEqual(self.client.get(reverse(f"arabela_admin:{page}")).status_code, 302)
            with self.subTest(account=who, action="add a category"):
                response = self.post(reverse("arabela_admin:gown_category_create"), {"name": "Prom Dress", "audience": "women"})
                self.assertEqual(response.status_code, 403)
            with self.subTest(account=who, action="tag colors"):
                self.assertEqual(self.post(reverse("arabela_admin:gown_tag_colors_update"), {"colors": {}}).status_code, 403)
        self.client.force_login(self.owner)
        for page in ("staff_management", "categories", "audit_log"):
            self.assertEqual(self.client.get(reverse(f"arabela_admin:{page}")).status_code, 200)


class ButtonTests(RoleBase):
    def pages(self, account):
        """The HTML of the four pages where a guarded button lives, as `account` sees them."""
        self.client.force_login(account)
        reservation, [item, _second] = self.booking(gowns=2, number_from=next(self.serial))      # two gowns: only then is there a per-gown "Cancel gown"
        ReceiptRecord.objects.create(reservation=reservation, photo_url="https://files.example/receipt_photos/btn.jpg", uploaded_by=self.legacy_manager,
                                     uploaded_at=timezone.now() - timedelta(days=3))
        get = lambda name: self.client.get(reverse(f"arabela_admin:{name}")).content.decode()
        return {"catalog": get("gown_catalog"), "active": get("active_reservations"), "clients": get("clients"),
                "records": get("reservation_records"), "reservation": reservation, "item": item}

    def buttons_in(self, pages):
        reservation = pages["reservation"]
        return {
            "delete_gowns": [('@click="deleteGown()"' in pages["catalog"]), ('@click="bulkDelete()"' in pages["catalog"])],
            "cancel_bookings": [f'data-url="{reverse("arabela_admin:reservation_cancel", args=[reservation.id])}"' in pages["active"],
                                f'data-url="{reverse("arabela_admin:reservation_cancel_gowns", args=[reservation.id])}"' in pages["active"]],
            "flag_customers": ["pendingFlagAction = 'flag'" in pages["clients"], "pendingFlagAction = 'unflag'" in pages["clients"],
                               "unlockCustomer(viewingCustomer.userId, viewingCustomer.name)" in pages["clients"]],
            # the delete-receipt confirm is always in the page, and shown per photo from its `canDelete` flag
            "delete_receipts": [any(f'"canDelete": {flag}' in pages["records"] or f'"canDelete":{flag}' in pages["records"] for flag in ("true",))],
        }

    def test_a_staff_account_without_permissions_sees_none_of_the_guarded_buttons(self):
        for who, account in self.refused_accounts.items():
            with self.subTest(account=who):
                shown = {key: flags for key, flags in self.buttons_in(self.pages(account)).items() if any(flags)}
                self.assertEqual(shown, {})

    def test_each_permission_shows_exactly_its_own_buttons(self):
        for owned, account in self.only.items():
            with self.subTest(owned=owned):
                shown = self.buttons_in(self.pages(account))
                for key, flags in shown.items():
                    if key == "delete_receipts":
                        continue            # per photo: see the next test
                    self.assertEqual(all(flags), key == owned, (key, flags))
                    self.assertEqual(any(flags), key == owned, (key, flags))

    def test_the_owner_and_an_all_permissions_staff_see_every_button(self):
        for who in ("owner by role", "superuser owner", "staff with all four", "old Manager row"):
            with self.subTest(account=who):
                shown = self.buttons_in(self.pages(self.allowed_accounts[who]))
                self.assertEqual([key for key, flags in shown.items() if not all(flags)], [])

    def test_the_receipt_photos_carry_a_can_delete_flag_for_this_person(self):
        for who, account, expected in (("plain staff", self.plain_staff, False), ("receipt permission", self.only["delete_receipts"], True),
                                       ("owner", self.owner, True)):
            with self.subTest(account=who):
                self.client.force_login(account)
                reservation, _ = self.booking(number_from=next(self.serial))
                ReceiptRecord.objects.create(reservation=reservation, photo_url="https://files.example/receipt_photos/flag.jpg",
                                             uploaded_by=self.legacy_manager, uploaded_at=timezone.now() - timedelta(days=3))
                html = self.client.get(reverse("arabela_admin:reservation_records")).content.decode()
                self.assertEqual('"canDelete": true' in html or '"canDelete":true' in html, expected)

    def test_a_staff_account_still_sees_the_everyday_buttons(self):
        pages = self.pages(self.plain_staff)
        self.assertIn("Mark Picked Up", pages["active"])
        self.assertIn("Remind", pages["active"])
        self.assertIn("Upload receipt", pages["records"])
        self.assertIn("Mark as checked now", pages["catalog"])
        self.assertIn("bulkStatus('Available')", pages["catalog"])
        self.assertIn("markChecked(selectedGownIds)", pages["catalog"])
        self.assertIn("View Reservation History", pages["clients"])
        self.assertIn("Change pick-up date", pages["active"])

    def test_the_single_gown_window_has_no_empty_footer_when_the_remove_button_is_hidden(self):
        footer = '<div class="mt-6 flex items-center border-t border-gray-100 px-2 pt-6 dark:border-gray-800">'
        self.client.force_login(self.plain_staff)
        html = self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()
        self.assertNotIn(footer, html)
        self.assertNotIn("Remove Gown&hellip;", html)
        self.client.force_login(self.only["delete_gowns"])
        html = self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()
        at = html.index(footer)
        self.assertIn("Remove Gown&hellip;", html[at:at + 1200])
        self.assertNotIn(">Close</button>", html[at:at + 1200])

    def test_the_template_flags_are_the_same_rule_the_server_uses(self):
        everyone = {**self.refused_accounts, **self.allowed_accounts, **{f"only {k}": v for k, v in self.only.items()}}
        for who, account in everyone.items():
            self.client.force_login(account)
            flags = self.client.get(reverse("arabela_admin:dashboard")).context["admin_perms"]
            self.assertEqual(set(flags), set(PERMISSION_KEYS))
            for key in PERMISSION_KEYS:
                with self.subTest(account=who, permission=key):
                    self.assertEqual(flags[key], can(account, key))
        # and the server really does what the flag says, for the delete-gown action
        for who, account in everyone.items():
            with self.subTest(account=who, action="delete a gown"):
                self.client.force_login(account)
                flag = self.client.get(reverse("arabela_admin:dashboard")).context["admin_perms"]["delete_gowns"]
                send, _changed = self.scenario("delete_a_gown")
                self.assertEqual(flag, send().status_code == 200)


class RuleTests(RoleBase):
    def test_the_rule_for_every_kind_of_account(self):
        for key in PERMISSION_KEYS:
            with self.subTest(permission=key):
                self.assertTrue(can(self.owner, key))
                self.assertTrue(can(self.owner_by_role, key))
                self.assertTrue(can(self.legacy_manager, key))
                self.assertTrue(can(self.all_four, key))
                self.assertFalse(can(self.plain_staff, key))
                self.assertFalse(can(self.bare, key))
                self.assertFalse(can(self.customer, key))
                self.assertFalse(can(None, key))
                self.assertFalse(can(AnonymousUser(), key))
        self.assertTrue(can(self.only["flag_customers"], "flag_customers"))
        self.assertFalse(can(self.only["flag_customers"], "delete_gowns"))

    def test_parsing_and_joining_permissions(self):
        self.assertEqual(parse_permissions("cancel_bookings, delete_gowns,nonsense"), ["delete_gowns", "cancel_bookings"])      # registry order, junk dropped
        self.assertEqual(parse_permissions(["flag_customers", "flag_customers"]), ["flag_customers"])
        self.assertEqual(parse_permissions(None), [])
        self.assertEqual(parse_permissions(""), [])
        self.assertEqual(join_permissions(["delete_receipts", "delete_gowns"]), "delete_gowns,delete_receipts")

    def test_the_staff_name_rule_is_shared(self):
        self.assertEqual(staff_display_name(self.legacy_manager), "Maria Cruz")
        self.assertEqual(staff_display_name(self.bare), "rp_bare")          # no name entered: the username
        from arabela_admin.views import _staff_display_name
        self.assertEqual(_staff_display_name(self.legacy_manager), staff_display_name(self.legacy_manager))

    def test_the_migration_turns_every_old_manager_into_staff_with_all_four(self):
        module = importlib.import_module("accounts.migrations.0019_managers_become_staff")
        old = make_staff("rp_old_manager", role=UserProfile.Role.MANAGER)
        UserProfile.objects.filter(user=self.legacy_manager).update(role=UserProfile.Role.MANAGER)
        module.managers_become_staff(django_apps, None)
        for account in (old, self.legacy_manager):
            profile = UserProfile.objects.get(user=account)
            self.assertEqual(profile.role, UserProfile.Role.STAFF)
            self.assertEqual(parse_permissions(profile.permissions), list(PERMISSION_KEYS))
        self.assertEqual(UserProfile.objects.get(user=self.plain_staff).permissions, "")                 # nobody else is touched
        self.assertEqual(UserProfile.objects.get(user=self.owner_by_role).role, UserProfile.Role.OWNER)
        module.managers_become_staff(django_apps, None)                                                  # running it twice changes nothing more
        self.assertEqual(UserProfile.objects.filter(role=UserProfile.Role.MANAGER).count(), 0)


class StaffManagementTests(RoleBase):
    def create(self, **extra):
        body = {"name": "Nina Newhire", "username": "nina.newhire", "password": "a-very-long-pass-1", "permissions": []}
        body.update(extra)
        return self.post(reverse("arabela_admin:staff_create"), body)

    def test_the_page_has_checkboxes_not_a_role_dropdown(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("arabela_admin:staff_management")).content.decode()
        self.assertNotIn('<option value="Manager">', html)
        self.assertNotIn("Manager", html)
        self.assertIn("What this staff can do", html)
        for key in PERMISSION_KEYS:
            self.assertIn(f'"key": "{key}"', html)
        self.assertIn("Daily work only", html)
        self.assertIn("Senior staff", html)
        from django.utils.html import escape
        from accounts.roles import PERMISSIONS
        for key, info in PERMISSIONS.items():                                   # each box is a plain server-rendered row with its own "?" bubble, in Add and in Edit
            self.assertEqual(html.count(f'<div class="ht-tip-pop">{escape(info["help"])}</div>'), 2, key)
            self.assertIn(f'id="permnew-{key}"', html)
            self.assertIn(f'id="permedit-{key}"', html)

    def test_the_owner_creates_an_account_with_exactly_the_ticked_permissions(self):
        self.client.force_login(self.owner)
        response = self.create(permissions=["cancel_bookings", "flag_customers"])
        self.assertEqual(response.status_code, 200, response.content)
        account = response.json()["account"]
        self.assertEqual(account["role"], "Staff")
        self.assertEqual(account["permissions"], ["cancel_bookings", "flag_customers"])
        profile = UserProfile.objects.get(user__username="nina.newhire")
        self.assertEqual((profile.role, profile.permissions), (UserProfile.Role.STAFF, "cancel_bookings,flag_customers"))
        self.assertTrue(can(profile.user, "cancel_bookings"))
        self.assertFalse(can(profile.user, "delete_gowns"))

    def test_an_account_with_no_boxes_ticked_does_the_daily_work_only(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.create().json()["account"]["permissions"], [])
        self.assertEqual(granted_permissions(User.objects.get(username="nina.newhire")), frozenset())

    def test_a_role_in_the_request_is_ignored_and_nobody_can_become_a_manager(self):
        self.client.force_login(self.owner)
        response = self.create(role="Manager")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(UserProfile.objects.get(user__username="nina.newhire").role, UserProfile.Role.STAFF)
        self.assertFalse(can(User.objects.get(username="nina.newhire"), "delete_gowns"))

    def test_an_unknown_permission_is_refused_not_ignored(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.create(permissions=["delete_gowns", "become_owner"]).status_code, 400)
        self.assertEqual(self.create(permissions="delete_gowns").status_code, 400)
        self.assertFalse(User.objects.filter(username="nina.newhire").exists())

    def test_the_owner_edits_the_permissions_and_it_takes_effect_at_once(self):
        self.client.force_login(self.owner)
        url = reverse("arabela_admin:staff_update", args=[self.plain_staff.pk])
        response = self.post(url, {"name": "Sam Staff", "permissions": ["delete_gowns", "delete_receipts"], "password": ""})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["account"]["permissions"], ["delete_gowns", "delete_receipts"])
        self.assertTrue(can(User.objects.get(pk=self.plain_staff.pk), "delete_gowns"))
        response = self.post(url, {"name": "Sam Staff", "permissions": [], "password": ""})
        self.assertEqual(response.json()["account"]["permissions"], [])
        self.assertFalse(can(User.objects.get(pk=self.plain_staff.pk), "delete_gowns"))

    def test_only_the_owner_can_change_permissions_even_a_staff_with_all_four(self):
        before = UserProfile.objects.get(user=self.plain_staff).permissions
        for who in (self.all_four, self.plain_staff, self.legacy_manager):
            self.client.force_login(who)
            for url, body in ((reverse("arabela_admin:staff_update", args=[self.plain_staff.pk]),
                               {"name": "Sam Staff", "permissions": list(PERMISSION_KEYS), "password": ""}),
                              (reverse("arabela_admin:staff_create"), {"name": "X Y", "username": "x.yz.1", "password": "a-very-long-pass-1", "permissions": []})):
                with self.subTest(account=who.username, url=url):
                    self.assertEqual(self.post(url, body).status_code, 403)
        self.assertEqual(UserProfile.objects.get(user=self.plain_staff).permissions, before)
        self.assertFalse(User.objects.filter(username="x.yz.1").exists())

    def test_saving_an_old_manager_row_converts_it_to_staff_with_the_ticked_boxes(self):
        self.client.force_login(self.owner)
        self.assertEqual(UserProfile.objects.get(user=self.legacy_manager).role, UserProfile.Role.MANAGER)
        listed = [row for row in self.client.get(reverse("arabela_admin:staff_management")).context["staff_accounts"]
                  if row["id"] == self.legacy_manager.pk][0]
        self.assertEqual((listed["role"], listed["permissions"]), ("Staff", list(PERMISSION_KEYS)))        # reads as Staff with all four until saved
        response = self.post(reverse("arabela_admin:staff_update", args=[self.legacy_manager.pk]),
                             {"name": "Maria Cruz", "permissions": ["flag_customers"], "password": ""})
        self.assertEqual(response.status_code, 200, response.content)
        profile = UserProfile.objects.get(user=self.legacy_manager)
        self.assertEqual((profile.role, profile.permissions), (UserProfile.Role.STAFF, "flag_customers"))
        self.assertFalse(can(self.legacy_manager, "delete_gowns"))

    def test_the_list_shows_what_each_account_can_do(self):
        self.client.force_login(self.owner)
        rows = {row["username"]: row for row in self.client.get(reverse("arabela_admin:staff_management")).context["staff_accounts"]}
        self.assertEqual(rows["rp_staff"]["permissions"], [])
        self.assertEqual(rows["rp_only_flag_customers"]["permissions"], ["flag_customers"])
        self.assertEqual(rows["rp_all"]["permissions"], list(PERMISSION_KEYS))
        self.assertTrue(all(row["role"] == "Staff" for row in rows.values()))

    def test_deactivating_and_deleting_still_work(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.post(reverse("arabela_admin:staff_toggle_status", args=[self.all_four.pk])).json()["is_active"], False)
        self.assertEqual(self.post(reverse("arabela_admin:staff_delete", args=[self.plain_staff.pk])).status_code, 200)
        self.assertFalse(User.objects.filter(pk=self.plain_staff.pk).exists())


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
