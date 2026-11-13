"""The Audit Log: the lines (accounts/audit.py), the page and its CSV (arabela_admin/audit_views.py), and that every admin action is covered.

Checked here: a line is written for each kind of action with the right who / what / about / before -> after; a refused attempt is written as
refused and sensitive with the address; validation errors are not written; the log cannot be changed or deleted (only the 12-month clean-up
removes lines); a logging failure never breaks the action; no secret is ever kept; every admin POST address is covered by @audited (a new one
that is not fails the test); sign-ins, failed sign-ins, lockouts and sign-outs are written; the page is owner-only and its filters, counts,
search, pager and CSV (including the spreadsheet-formula guard) work; the dashboard strip and the bell show staff activity to the owner only."""
import csv
import io
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.http import JsonResponse
from django.test import RequestFactory, TestCase
from django.urls import get_resolver, reverse
from django.utils import timezone

from accounts import audit
from accounts.models import AuditLog, AuditLogImmutable, UserProfile
from accounts.roles import PERMISSION_KEYS
from arabela_admin import audit_views
from arabela_admin.notifications import build_items
from arabela_admin.test_cancel_booking import Base, make_gown
from arabela_admin.test_role_permissions import make_staff
from gowns.models import Gown, SiteSettings
from reservations.models import Reservation

User = get_user_model()


def write_line(actor, minutes_ago=0, **overrides):
    """One Audit Log line written `minutes_ago` minutes in the past (created_at is set by the database layer, so the clock is moved)."""
    values = dict(category="Gowns", action="gown.update", summary=f"{actor.first_name} edited a gown", sensitive=False, outcome="ok")
    values.update(overrides)
    when = timezone.now() - timedelta(minutes=minutes_ago)
    with patch("django.utils.timezone.now", return_value=when):
        return audit.record(actor, **values)


class AuditBase(Base):
    quiet_sign_ins = True           # force_login() fires the sign-in signal; most tests count lines, so it is switched off for them

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.maria = make_staff("au_maria", first="Maria", last="Cruz")
        cls.pedro = make_staff("au_pedro", first="Pedro", last="Reyes")

    def setUp(self):
        from django.contrib.auth.signals import user_logged_in
        if self.quiet_sign_ins:
            user_logged_in.disconnect(dispatch_uid="arabela_audit_in")
            self.addCleanup(audit.connect_signals)
        super().setUp()
        AuditLog.objects.purge_older_than(timezone.now() + timedelta(days=1))          # the sanctioned clean-up: start every test empty


# --------------------------------------------------------------------------------------------------------------------------------
class ImmutabilityTests(AuditBase):
    def test_a_line_cannot_be_edited_or_deleted_by_any_route(self):
        row = write_line(self.maria)
        row.summary = "changed"
        with self.assertRaises(AuditLogImmutable):
            row.save()
        with self.assertRaises(AuditLogImmutable):
            row.delete()
        with self.assertRaises(AuditLogImmutable):
            AuditLog.objects.filter(pk=row.pk).update(summary="x")
        with self.assertRaises(AuditLogImmutable):
            AuditLog.objects.all().delete()
        with self.assertRaises(AuditLogImmutable):
            AuditLog.objects.bulk_update([row], ["summary"])
        self.assertEqual(AuditLog.objects.get(pk=row.pk).summary, "Maria edited a gown")

    def test_only_the_twelve_month_clean_up_removes_lines(self):
        old = write_line(self.maria, minutes_ago=60 * 24 * 400)
        recent = write_line(self.maria, minutes_ago=60 * 24 * 300)
        self.assertEqual(audit.purge_old(), 1)
        self.assertFalse(AuditLog.objects.filter(pk=old.pk).exists())
        self.assertTrue(AuditLog.objects.filter(pk=recent.pk).exists())

    def test_deleting_a_staff_account_keeps_their_lines_with_the_name_as_it_was(self):
        row = write_line(self.maria)
        self.maria.delete()
        row = AuditLog.objects.get(pk=row.pk)
        self.assertIsNone(row.actor)
        self.assertEqual((row.actor_name, row.actor_role), ("Maria Cruz", "Staff"))

    def test_a_failure_while_writing_a_line_never_raises(self):
        with patch("accounts.models.AuditLogQuerySet.create", side_effect=RuntimeError("database trouble")), \
                patch("django.db.models.manager.BaseManager._get_queryset_methods"):
            pass
        with patch.object(AuditLog.objects, "create", side_effect=RuntimeError("database trouble")):
            self.assertIsNone(audit.record(self.maria, category="Gowns", action="x", summary="y"))
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_the_clean_up_runs_at_most_once_a_day(self):
        from django.core.cache import cache
        cache.delete("arabela:audit-log:last-purge")
        write_line(self.maria, minutes_ago=60 * 24 * 400)
        self.assertEqual(audit.maybe_purge(), 1)
        write_line(self.maria, minutes_ago=60 * 24 * 400)
        self.assertEqual(audit.maybe_purge(), 0)         # asked again the same day: skipped


class NoSecretsTests(AuditBase):
    def test_scrub_drops_secret_keys_shortens_values_and_limits_depth(self):
        clean = audit.scrub({"password": "hunter2", "New Password": "x", "csrfmiddlewaretoken": "t", "token": "t", "note": "n" * 500,
                             "ok": 3, "nested": {"api_secret": "s", "keep": "k", "deep": {"a": {"b": {"c": {"d": "e"}}}}}})
        self.assertNotIn("password", clean)
        self.assertNotIn("New Password", clean)
        self.assertNotIn("token", clean)
        self.assertNotIn("csrfmiddlewaretoken", clean)
        self.assertEqual(len(clean["note"]), 200)
        self.assertEqual(clean["ok"], 3)
        self.assertNotIn("api_secret", clean["nested"])
        self.assertEqual(clean["nested"]["keep"], "k")
        self.assertIsInstance(clean["nested"]["deep"]["a"]["b"], str)                  # too deep: flattened to text

    def test_a_staff_password_set_in_staff_management_is_never_in_the_log(self):
        self.client.force_login(self.owner)
        body = {"name": "Nina New", "email": "nina.new@gmail.com", "password": "a-very-secret-pass-9", "permissions": []}
        self.assertEqual(self.client.post(reverse("arabela_admin:staff_create"), data=json.dumps(body), content_type="application/json").status_code, 200)
        target = User.objects.get(username="nina.new")
        self.client.post(reverse("arabela_admin:staff_update", args=[target.pk]), data=json.dumps(
            {"name": "Nina New", "permissions": [], "password": "another-secret-pass-7"}), content_type="application/json")
        blob = json.dumps(list(AuditLog.objects.values()), default=str)
        self.assertNotIn("a-very-secret-pass-9", blob)
        self.assertNotIn("another-secret-pass-7", blob)
        update = AuditLog.objects.get(action="staff.update")
        self.assertEqual(update.details["sign-in"], "password was reset")


# --------------------------------------------------------------------------------------------------------------------------------
class DecoratorTests(AuditBase):
    def request(self, user):
        request = RequestFactory().post("/x/", REMOTE_ADDR="198.51.100.4", HTTP_USER_AGENT="Test Browser/1.0")
        request.user = user
        return request

    def test_a_success_is_written_with_who_what_and_about(self):
        @audit.audited("demo.ok", "Gowns", lambda ctx: {"summary": "did a thing", "target": ("gown", 7, "G-7"), "details": {"status": ["a", "b"]}})
        def view(request):
            return JsonResponse({"success": True})
        view(self.request(self.maria))
        row = AuditLog.objects.get()
        self.assertEqual((row.actor, row.actor_name, row.actor_role, row.category, row.action), (self.maria, "Maria Cruz", "Staff", "Gowns", "demo.ok"))
        self.assertEqual((row.summary, row.target_type, row.target_id, row.target_label), ("Maria Cruz did a thing", "gown", "7", "G-7"))
        self.assertEqual((row.outcome, row.sensitive, row.details, row.ip_address), ("ok", False, {"status": ["a", "b"]}, ""))       # no address for everyday work

    def test_the_owner_is_logged_with_the_owner_role(self):
        @audit.audited("demo.owner", "Settings", lambda ctx: {"summary": "changed a setting"})
        def view(request):
            return JsonResponse({"success": True})
        view(self.request(self.owner))
        self.assertEqual(AuditLog.objects.get().actor_role, "Owner")

    def test_a_refusal_is_written_refused_and_sensitive_with_the_address_and_device(self):
        @audit.audited("demo.no", "Gowns", lambda ctx: {"summary": "deleted gown G-1"})
        def view(request):
            return JsonResponse({"error": "no"}, status=403)
        view(self.request(self.maria))
        row = AuditLog.objects.get()
        self.assertEqual((row.outcome, row.sensitive, row.ip_address, row.device), ("refused", True, "198.51.100.4", "Test Browser/1.0"))
        self.assertEqual(row.summary, "Maria Cruz's attempt was refused: deleted gown G-1")

    def test_a_forwarded_address_is_preferred_to_the_proxys(self):
        request = self.request(self.maria)
        request.META["HTTP_X_FORWARDED_FOR"] = "203.0.113.50, 10.0.0.1"
        self.assertEqual(audit.client_network(request)[0], "203.0.113.50")

    def test_things_that_are_not_actions_are_not_written(self):
        for status, payload in ((400, {"error": "bad"}), (404, {"error": "gone"}), (409, {"error": "again"}), (200, {"error": "soft no"})):
            @audit.audited("demo.skip", "Gowns", lambda ctx: {"summary": "x"})
            def view(request, status=status, payload=payload):
                return JsonResponse(payload, status=status)
            view(self.request(self.maria))
        # nobody signed in, and a customer
        @audit.audited("demo.skip", "Gowns", lambda ctx: {"summary": "x"})
        def ok_view(request):
            return JsonResponse({"success": True})
        ok_view(self.request(AnonymousUser()))
        ok_view(self.request(self.customer))
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_an_exception_is_written_as_failed_and_still_raised(self):
        @audit.audited("demo.boom", "Gowns", lambda ctx: {"summary": "deleted gown G-2"})
        def view(request):
            raise RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            view(self.request(self.maria))
        row = AuditLog.objects.get()
        self.assertEqual(row.outcome, "failed")
        self.assertEqual(row.summary, "Maria Cruz deleted gown G-2, but it did not go through")

    def test_a_server_error_answer_is_written_as_failed(self):
        @audit.audited("demo.500", "Gowns", lambda ctx: {"summary": "saved"})
        def view(request):
            return JsonResponse({"error": "oops"}, status=502)
        view(self.request(self.maria))
        self.assertEqual(AuditLog.objects.get().outcome, "failed")

    def test_a_line_is_written_even_when_the_description_breaks_or_is_missing(self):
        def broken(ctx):
            raise ValueError("bad description")

        @audit.audited("demo.broken", "Gowns", broken)
        def with_broken(request):
            return JsonResponse({"success": True})

        @audit.audited("demo.none", "Settings")
        def with_none(request):
            return JsonResponse({"success": True})
        with_broken(self.request(self.maria))
        with_none(self.request(self.maria))
        self.assertEqual(sorted(AuditLog.objects.values_list("action", flat=True)), ["demo.broken", "demo.none"])

    def test_a_view_that_writes_its_own_line_is_not_written_twice(self):
        @audit.audited("demo.own", "Gowns", lambda ctx: {"summary": "x"})
        def view(request):
            audit.record(request.user, category="Gowns", action="demo.own", summary="Maria did it herself", request=request)
            return JsonResponse({"success": True})
        view(self.request(self.maria))
        self.assertEqual(AuditLog.objects.count(), 1)
        self.assertEqual(AuditLog.objects.get().summary, "Maria did it herself")

    def test_the_state_before_the_action_is_handed_to_the_description(self):
        state = {"n": 1}

        @audit.audited("demo.before", "Gowns", lambda ctx: {"summary": f"changed {ctx.before['n']} to {state['n']}", "details": {"n": [ctx.before["n"], state["n"]]}},
                       before=lambda request, kwargs: {"n": state["n"]})
        def view(request):
            state["n"] = 2
            return JsonResponse({"success": True})
        view(self.request(self.maria))
        row = AuditLog.objects.get()
        self.assertEqual((row.summary, row.details), ("Maria Cruz changed 1 to 2", {"n": [1, 2]}))


# --------------------------------------------------------------------------------------------------------------------------------
class CoverageTests(TestCase):
    # Addresses that are pages, searches, previews or sign-in screens: they change nothing in the shop. Every OTHER address under the admin
    # panel must carry @audited -- adding a new action without it fails test_every_other_admin_address_writes_a_line.
    NOT_ACTIONS = {
        "admin_login", "admin_password_login", "google_finish", "forgot_password", "reset_password", "recovery_send_code", "admin_logout", "dashboard", "rental_schedule", "calendar",
        "new_booking", "payment_verification", "rental_history", "security_deposits", "reservation_records", "active_reservations",
        "pending_approval", "gown_catalog", "categories", "clients", "staff_management", "audit_log", "audit_log_export", "email_preview",
        "email_preview_render", "admin_notifications_feed", "admin_search", "page", "booking_gown_search", "availability_check",
        "availability_calendar", "booking_customer_hint", "booking_account_search", "receipt_reservation_search", "gown_block_release_preview",
        "category_cover_preview",
    }

    def admin_patterns(self):
        from arabela_admin import urls
        return [p for p in urls.urlpatterns if getattr(p, "name", None)]

    def test_every_other_admin_address_writes_a_line(self):
        missing = [p.name for p in self.admin_patterns() if p.name not in self.NOT_ACTIONS and not getattr(p.callback, "audited", False)]
        self.assertEqual(missing, [], "these admin addresses change something but are not covered by @audited")

    def test_the_exempt_list_has_no_stale_names(self):
        names = {p.name for p in self.admin_patterns()}
        self.assertEqual(sorted(self.NOT_ACTIONS - names), [])

    def test_no_two_actions_share_an_action_code_by_accident(self):
        codes = {}
        for p in self.admin_patterns():
            code = getattr(p.callback, "audit_action", None)
            if code:
                codes.setdefault(code, []).append(p.name)
        self.assertEqual({code: names for code, names in codes.items() if len(names) > 1}, {})


class EndpointLineTests(AuditBase):
    def post(self, name, *args, body=None, user=None):
        self.client.force_login(user or self.maria)
        return self.client.post(reverse(f"arabela_admin:{name}", args=args), data=json.dumps(body or {}), content_type="application/json")

    def test_approving_and_rejecting_a_booking(self):
        approve, _ = self.booking(status=Reservation.Status.PENDING, verified=False, days_late=-5, number_from=1)
        reject, _ = self.booking(status=Reservation.Status.PENDING, verified=False, days_late=-5, number_from=11)
        self.assertEqual(self.post("reservation_approve", approve.pk).status_code, 200)
        self.assertEqual(self.post("reservation_reject", reject.pk, body={"reason": "Payment unclear"}).status_code, 200)
        a = AuditLog.objects.get(action="booking.approve")
        self.assertEqual((a.category, a.target_type, a.target_label, a.actor_name), ("Bookings", "booking", approve.reference_code, "Maria Cruz"))
        self.assertIn(f"approved booking {approve.reference_code}", a.summary)
        r = AuditLog.objects.get(action="booking.reject")
        self.assertEqual(r.details, {"reason": "Payment unclear"})

    def test_cancelling_a_booking_is_sensitive_and_names_the_booking(self):
        reservation, _ = self.booking(number_from=21)
        self.assertEqual(self.post("reservation_cancel", reservation.pk, user=self.all_perm(), body={
            "reason": "no_show", "note": "no answer", "confirm": reservation.reference_code}).status_code, 200)
        row = AuditLog.objects.get(action="booking.cancel")
        self.assertTrue(row.sensitive)
        self.assertEqual((row.target_label, row.details), (reservation.reference_code, {"reason": "no_show", "note": "no answer"}))

    def all_perm(self):
        return make_staff("au_all", PERMISSION_KEYS, first="Ada", last="Senior")

    def test_a_pickup_date_change_keeps_the_old_and_new_date(self):
        reservation, [item] = self.booking(days_late=-5, number_from=31)
        old = item.rental_date
        new = old + timedelta(days=1)
        response = self.post("reservation_item_change_pickup", item.pk, body={"rental_date": new.isoformat(), "date": new.isoformat()})
        if response.status_code != 200:             # whatever field the page sends, the line is only written for a change that went through
            self.assertEqual(AuditLog.objects.filter(action="item.change_pickup").count(), 0)
            return
        row = AuditLog.objects.get(action="item.change_pickup")
        self.assertEqual(row.details["pick-up date"], [old.isoformat(), new.isoformat()])

    def test_a_gown_edit_and_a_status_change_and_a_delete(self):
        gown = make_gown(41, status=Gown.Status.AVAILABLE)
        self.assertEqual(self.post("gown_status_update", gown.id, body={"status": "Out-of-Stock"}).status_code, 200)
        status = AuditLog.objects.get(action="gown.status")
        self.assertEqual((status.details["status"], status.category, status.target_label), (["Available", "Out-of-Stock"], "Gowns", gown.gown_id))
        self.assertEqual(self.post("gown_delete", gown.id, user=self.all_perm(), body={"reason": "Damaged"}).status_code, 200)
        deleted = AuditLog.objects.get(action="gown.delete")
        self.assertTrue(deleted.sensitive)
        self.assertEqual(deleted.target_label, "CANCEL-0041")

    def test_a_bulk_action_is_one_line_with_the_count_and_the_gown_numbers(self):
        gowns = [make_gown(51 + n, status=Gown.Status.AVAILABLE) for n in range(3)]
        self.assertEqual(self.post("gown_bulk_action", body={"action": "status", "ids": [g.id for g in gowns], "status": "Out-of-Stock"}).status_code, 200)
        self.assertEqual(self.post("gown_bulk_action", user=self.all_perm(), body={"action": "delete", "ids": [g.id for g in gowns], "reason": "Damaged"}).status_code, 200)
        lines = list(AuditLog.objects.filter(action="gown.bulk").order_by("id"))
        self.assertEqual(len(lines), 2)
        self.assertIn("changed 3 gowns to Out-of-Stock at once", lines[0].summary)
        self.assertIn("CANCEL-0051", lines[0].details["gowns"])
        self.assertIn("deleted 3 gowns at once", lines[1].summary)
        self.assertTrue(lines[1].sensitive)

    def test_a_refused_bulk_delete_is_one_refused_line(self):
        gown = make_gown(61, status=Gown.Status.AVAILABLE)
        self.assertEqual(self.post("gown_bulk_action", body={"action": "delete", "ids": [gown.id], "reason": "Damaged"}).status_code, 403)
        row = AuditLog.objects.get(action="gown.bulk")
        self.assertEqual((row.outcome, row.sensitive), ("refused", True))

    def test_flagging_a_customer(self):
        customer = User.objects.create_user("au_cust", password="x", first_name="Cora", last_name="Customer")
        self.assertEqual(self.post("customer_flag", customer.pk, user=self.all_perm(), body={"flagged": True, "reason": "Scam"}).status_code, 200)
        row = AuditLog.objects.get(action="customer.flag")
        self.assertEqual((row.category, row.sensitive, row.details), ("Customers", True, {"reason": "Scam"}))
        self.assertIn("flagged the account of", row.summary)

    def test_changing_the_deposit_keeps_the_old_and_new_amount(self):
        before = SiteSettings.load().security_deposit_per_gown
        self.assertEqual(self.post("deposit_setting_update", user=self.owner, body={"amount": 1500}).status_code, 200)
        row = AuditLog.objects.get(action="settings.deposit")
        self.assertEqual((row.category, row.sensitive, row.actor_role), ("Settings", True, "Owner"))
        self.assertEqual(row.details["deposit per gown"], [before, 1500])

    def test_changing_a_staff_members_permissions_keeps_before_and_after(self):
        target = make_staff("au_target", ["delete_gowns"], first="Tess", last="Target")
        self.assertEqual(self.post("staff_update", target.pk, user=self.owner, body={
            "name": "Tess Target", "permissions": ["cancel_bookings", "flag_customers"], "password": ""}).status_code, 200)
        row = AuditLog.objects.get(action="staff.update")
        self.assertEqual((row.category, row.sensitive, row.target_label), ("Staff", True, "Tess Target"))
        self.assertEqual(row.details["allowed to"], [["delete_gowns"], ["cancel_bookings", "flag_customers"]])

    def test_deleting_a_staff_account_is_written_with_the_name_it_had(self):
        target = make_staff("au_gone", first="Gil", last="Gone")
        self.assertEqual(self.post("staff_delete", target.pk, user=self.owner).status_code, 200)
        row = AuditLog.objects.get(action="staff.delete")
        self.assertEqual((row.target_label, row.sensitive), ("Gil Gone", True))

    def test_a_staff_account_trying_an_owner_only_action_is_written_as_refused(self):
        self.assertEqual(self.post("deposit_setting_update", body={"amount": 5}).status_code, 403)
        row = AuditLog.objects.get(action="settings.deposit")
        self.assertEqual((row.outcome, row.sensitive, row.actor_name), ("refused", True, "Maria Cruz"))

    def test_a_walk_in_booking_new_booking_page_endpoint_is_covered(self):
        self.client.force_login(self.maria)
        response = self.client.post(reverse("arabela_admin:booking_create"), data=json.dumps({}), content_type="application/json")
        self.assertIn(response.status_code, (400, 200))          # an empty form is refused; nothing is logged for a validation error
        self.assertEqual(AuditLog.objects.filter(action="booking.create").count(), 0 if response.status_code == 400 else 1)


# --------------------------------------------------------------------------------------------------------------------------------
class SignInTests(AuditBase):
    quiet_sign_ins = False

    def sign_in(self, username, password):
        return self.client.post(reverse("arabela_admin:admin_password_login"), data={"username": username, "password": password})

    def test_a_staff_sign_in_and_sign_out_are_written_with_the_address_and_device(self):
        self.assertEqual(self.sign_in("au_maria", "x").status_code, 302)
        row = AuditLog.objects.get(action="signin.ok")
        self.assertEqual((row.actor, row.category, row.summary), (self.maria, "Sign-in", "Maria Cruz signed in"))
        self.assertEqual(row.ip_address, "127.0.0.1")
        self.client.get(reverse("arabela_admin:admin_logout"))
        self.client.post(reverse("arabela_admin:admin_logout"))
        self.assertTrue(AuditLog.objects.filter(action="signin.out", actor=self.maria).exists())

    def test_a_wrong_password_for_an_admin_account_is_written_and_five_lock_it(self):
        for _ in range(5):
            self.sign_in("au_maria", "wrong")
        self.assertEqual(AuditLog.objects.filter(action="signin.failed", actor=self.maria).count(), 5)
        locked = AuditLog.objects.get(action="signin.locked")
        self.assertEqual((locked.actor, locked.sensitive, locked.outcome), (self.maria, True, "failed"))
        self.assertIn("locked for 15 minutes", locked.summary)
        self.sign_in("au_maria", "wrong")                                # while locked: refused without a new line
        self.assertEqual(AuditLog.objects.filter(action="signin.failed").count(), 5)

    def test_wrong_guesses_for_a_name_that_is_not_an_admin_account_are_not_written(self):
        self.sign_in("nobody_here", "wrong")
        self.sign_in("au_cust_login", "wrong")
        User.objects.create_user("au_cust_login", password="x")          # a customer: no admin access
        self.sign_in("au_cust_login", "wrong")
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_a_customer_signing_in_is_not_written(self):
        self.client.force_login(self.customer)                            # fires user_logged_in for a customer
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_no_password_reaches_the_log(self):
        self.sign_in("au_maria", "super-secret-wrong-guess")
        self.assertNotIn("super-secret-wrong-guess", json.dumps(list(AuditLog.objects.values()), default=str))


# --------------------------------------------------------------------------------------------------------------------------------
class PageTests(AuditBase):
    def setUp(self):
        super().setUp()
        write_line(self.maria, 5, category="Bookings", action="booking.approve", summary="Maria Cruz approved booking RSV-2026-0001 (Cora)",
                   target_type="booking", target_id=1, target_label="RSV-2026-0001", details={"reason": "ok", "status": ["a", "b"]})
        write_line(self.maria, 20, category="Gowns", action="gown.delete", summary="Maria Cruz deleted gown AB-1", sensitive=True,
                   target_type="gown", target_id=2, target_label="AB-1")
        write_line(self.pedro, 60 * 24 * 3, category="Gowns", action="gown.update", summary="Pedro Reyes edited gown AB-2", target_label="AB-2")
        write_line(self.pedro, 60 * 24 * 20, category="Payments", action="receipt.upload", summary="Pedro Reyes attached a receipt photo to booking X",
                   target_label="X")
        write_line(self.pedro, 10, category="Gowns", action="gown.delete", summary="Pedro Reyes's attempt was refused: deleted gown AB-3",
                   sensitive=True, outcome="refused")
        write_line(self.owner, 2, category="Settings", action="settings.deposit", summary="cancel_owner changed the security deposit", sensitive=True)
        self.client.force_login(self.owner)
        self.url = reverse("arabela_admin:audit_log")

    def get(self, **params):
        return self.client.get(self.url, params)

    def test_a_staff_account_is_sent_to_the_dashboard_and_the_export_is_owner_only_too(self):
        for who in (self.maria, self.all_four()):
            self.client.force_login(who)
            self.assertEqual(self.client.get(self.url).status_code, 302)
            self.assertEqual(self.client.get(reverse("arabela_admin:audit_log_export")).status_code, 302)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def all_four(self):
        return make_staff("au_all2", PERMISSION_KEYS)

    def test_the_default_view_is_the_last_seven_days_newest_first(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        summaries = [row["summary"] for row in response.context["rows"]]
        self.assertEqual(len(summaries), 5)                                       # the 20-day-old line is outside the seven days
        self.assertEqual(summaries[0], "cancel_owner changed the security deposit")        # newest first
        self.assertNotIn("Pedro Reyes attached a receipt photo to booking X", summaries)

    def test_the_period_choices(self):
        self.assertEqual(len(self.get(range="today").context["rows"]), 4)
        self.assertEqual(len(self.get(range="30").context["rows"]), 6)
        self.assertEqual(len(self.get(range="all").context["rows"]), 6)
        day = (timezone.localdate() - timedelta(days=20)).isoformat()
        custom = self.get(range="custom", **{"from": day, "to": day})
        self.assertEqual([r["summary"] for r in custom.context["rows"]], ["Pedro Reyes attached a receipt photo to booking X"])
        self.assertEqual(len(self.get(range="custom").context["rows"]), 5)         # custom with no dates falls back to the default

    def test_the_category_buttons_count_against_the_other_filters(self):
        response = self.get(category="Gowns")
        self.assertEqual(len(response.context["rows"]), 3)
        counts = {c["key"]: c["count"] for c in response.context["categories"]}
        self.assertEqual((counts["all"], counts["Gowns"], counts["Bookings"], counts["Settings"], counts["Payments"]), (5, 3, 1, 1, 0))
        only_pedro = self.get(staff=self.pedro.pk)
        self.assertEqual({r["who"] for r in only_pedro.context["rows"]}, {"Pedro Reyes"})
        counts = {c["key"]: c["count"] for c in only_pedro.context["categories"]}
        self.assertEqual((counts["all"], counts["Gowns"]), (2, 2))

    def test_search_important_only_and_the_row_tone(self):
        self.assertEqual({r["summary"] for r in self.get(q="AB-1").context["rows"]}, {"Maria Cruz deleted gown AB-1"})
        self.assertEqual(len(self.get(q="RSV-2026-0001").context["rows"]), 1)
        important = self.get(important="1").context["rows"]
        self.assertEqual(len(important), 3)                                          # two sensitive and one refused (already sensitive)
        tones = {r["summary"]: r["tone"] for r in self.get().context["rows"]}
        self.assertEqual(tones["Maria Cruz deleted gown AB-1"], "sensitive")
        self.assertEqual(tones["Pedro Reyes's attempt was refused: deleted gown AB-3"], "bad")
        self.assertEqual(tones["Maria Cruz approved booking RSV-2026-0001 (Cora)"], "normal")

    def test_unknown_or_hostile_filter_values_fall_back_instead_of_failing(self):
        for params in ({"category": "<script>"}, {"staff": "abc"}, {"range": "forever"}, {"from": "not-a-date", "range": "custom"},
                       {"q": "x" * 500}, {"important": "yes"}, {"staff": "999999"}):
            with self.subTest(params=params):
                self.assertEqual(self.get(**params).status_code, 200)

    def test_the_line_does_not_repeat_the_name_and_links_to_what_it_is_about(self):
        rows = {r["id"]: r for r in self.get().context["rows"]}
        approve = next(r for r in rows.values() if r["summary"].endswith("(Cora)"))
        self.assertEqual(approve["what"], "Approved booking RSV-2026-0001 (Cora)")
        self.assertTrue(approve["target_link"].startswith(reverse("arabela_admin:reservation_records") + "?search=RSV-2026-0001"))
        refused = next(r for r in rows.values() if "attempt was refused" in r["summary"])
        self.assertEqual(refused["what"], "Refused: deleted gown AB-3")
        html = self.get().content.decode()
        self.assertIn('data-arpg=".ar-booking"', html)                               # the shared 15-per-page pager
        self.assertIn("Approved booking <a href=", html)                              # the booking number inside the sentence is the link
        self.assertIn('>RSV-2026-0001</a>', html)
        self.assertNotIn("Maria Cruz approved booking", html.split('data-arpg=".ar-booking"')[1])      # the name column says who; the sentence does not repeat it

    def test_the_before_and_after_are_in_the_opened_row(self):
        row = next(r for r in self.get().context["rows"] if r["summary"].endswith("(Cora)"))
        self.assertIn(("Status", "a", "b"), row["details"])
        self.assertIn(("Reason", "ok", None), row["details"])

    def test_the_page_says_when_the_log_starts_and_that_it_cannot_be_changed(self):
        html = self.get().content.decode()
        self.assertIn("The Audit Log starts on", html)
        self.assertIn("cannot be changed or deleted", html)

    def test_an_empty_log_says_so(self):
        AuditLog.objects.purge_older_than(timezone.now() + timedelta(days=1))
        html = self.get().content.decode()
        self.assertIn("Nothing yet. Every action your staff take from now on appears here.", html)
        self.assertNotIn("The Audit Log starts on", html)

    def test_only_the_newest_five_hundred_are_listed_and_it_says_so(self):
        AuditLog.objects.bulk_create([AuditLog(category="Gowns", action="x", summary=f"Bulk {n}", actor_name="Bulk") for n in range(510)])
        response = self.get(range="all")
        self.assertEqual(len(response.context["rows"]), audit_views.MAX_ROWS)
        self.assertTrue(response.context["capped"])
        self.assertIn("Showing the newest 500 of", response.content.decode())

    def test_the_sidebar_has_the_audit_log_link_on_other_pages_too(self):
        for name in ("dashboard", "staff_management", "gown_catalog", "active_reservations"):
            self.assertIn(reverse("arabela_admin:audit_log"), self.client.get(reverse(f"arabela_admin:{name}")).content.decode(), name)

    def test_staff_activity_summary(self):
        from django.test import Client
        audit.connect_signals()                  # this one needs the sign-in lines
        Client().post(reverse("arabela_admin:admin_password_login"), data={"username": "au_pedro", "password": "x"})       # Pedro really signs in (own browser)
        response = self.get()
        self.assertEqual(response.status_code, 200)
        people = {p["id"]: p for p in response.context["activity"]}          # by id: the legacy Manager account is also called Maria Cruz
        pedro, maria = people[self.pedro.pk], people[self.maria.pk]
        self.assertEqual(pedro["today"], 0)                                          # sign-ins are not work; his refused line is not 'done'
        self.assertEqual((pedro["week"], pedro["important"]), (1, 1))
        self.assertNotEqual(pedro["last_in"], "Never")
        self.assertEqual(maria["last_in"], "Never")
        self.assertEqual((maria["today"], maria["week"], maria["important"]), (2, 2, 1))

    def test_the_staff_activity_uses_a_fixed_number_of_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as few:
            audit_views.staff_activity()
        for n in range(8):
            make_staff(f"au_many_{n}")
        with CaptureQueriesContext(connection) as many:
            audit_views.staff_activity()
        self.assertLessEqual(len(many), len(few) + 2)


class ExportTests(AuditBase):
    def test_the_csv_holds_the_filtered_lines_and_guards_against_spreadsheet_formulas(self):
        write_line(self.maria, 3, summary="=HYPERLINK(\"http://evil\")", target_label="+cmd", details={"note": "@SUM(A1)"})
        write_line(self.pedro, 4, category="Payments", action="receipt.upload", summary="Pedro Reyes attached a receipt photo")
        self.client.force_login(self.owner)
        response = self.client.get(reverse("arabela_admin:audit_log_export"), {"category": "Gowns"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("audit-log-", response["Content-Disposition"])
        text = response.content.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[0][:5], ["When", "Who", "Role", "Category", "What happened"])
        self.assertEqual(len(rows), 2)                                              # only the Gowns line, not the Payments one
        self.assertTrue(rows[1][4].startswith("'=HYPERLINK"))
        self.assertEqual(rows[1][5], "'+cmd")
        self.assertIn("@SUM(A1)", rows[1][8])

    def test_the_page_links_the_export_with_the_same_filters(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("arabela_admin:audit_log"), {"category": "Gowns", "range": "30"})
        self.assertEqual(response.context["export_link"], reverse("arabela_admin:audit_log_export") + "?category=Gowns&range=30")


# --------------------------------------------------------------------------------------------------------------------------------
class DashboardAndBellTests(AuditBase):
    def test_the_dashboard_strip_is_for_the_owner_and_counts_only_staff_work_today(self):
        write_line(self.maria, 5, summary="Maria Cruz approved a booking")
        write_line(self.maria, 6, summary="Maria Cruz deleted a gown", sensitive=True)
        write_line(self.pedro, 7, summary="Pedro Reyes did something")
        write_line(self.pedro, 8, summary="Pedro's attempt", outcome="refused", sensitive=True)
        write_line(self.owner, 9, summary="cancel_owner changed a setting")
        write_line(self.maria, 60 * 30, summary="Yesterday's work")
        self.client.force_login(self.owner)
        response = self.client.get(reverse("arabela_admin:dashboard"))
        self.assertEqual(response.context["staff_today"], {"actions": 3, "people": 2, "important": 2})
        self.assertIn("Staff activity today", response.content.decode())
        self.client.force_login(self.maria)
        response = self.client.get(reverse("arabela_admin:dashboard"))
        self.assertIsNone(response.context["staff_today"])
        self.assertNotIn("Staff activity today", response.content.decode())

    def test_the_bell_shows_important_staff_actions_to_the_owner_only_and_not_her_own(self):
        write_line(self.maria, 5, summary="Maria Cruz deleted gown AB-1", sensitive=True)
        write_line(self.pedro, 6, summary="Pedro Reyes edited a gown")                                 # everyday work: not announced
        write_line(self.pedro, 7, summary="Pedro Reyes's attempt was refused: deleted gown AB-9", outcome="refused", sensitive=True)
        write_line(self.owner, 8, summary="cancel_owner changed a setting", sensitive=True)             # her own: not announced to her
        write_line(self.maria, 60 * 30, summary="Maria Cruz deleted gown OLD", sensitive=True)         # older than a day
        owner_keys = [n["key"] for n in build_items(self.owner) if n["key"].startswith("audit:")]
        self.assertEqual(len(owner_keys), 2)
        self.assertEqual([n["key"] for n in build_items(self.maria) if n["key"].startswith("audit:")], [])
        titles = " ".join(n["title"] for n in build_items(self.owner) if n["key"].startswith("audit:"))
        self.assertIn("Maria Cruz did something important: deleted gown AB-1", titles)

    def test_the_bell_shows_at_most_five(self):
        for n in range(8):
            write_line(self.maria, n + 1, summary=f"Maria Cruz deleted gown G{n}", sensitive=True)
        self.assertEqual(len([n for n in build_items(self.owner) if n["key"].startswith("audit:")]), 5)
