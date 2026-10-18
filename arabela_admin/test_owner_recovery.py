"""Owner password recovery: the closed doors, the recovery email (code), the Forgot password page, the reset link, the emails, and the pages
that changed (sign-in page, Account Settings, Staff Management eye icons).

The point of most of these tests is what must NOT happen: no reset for staff or customers, no answer that tells a stranger whether a
username exists, no recovery email without the code, no link that outlives its 30 minutes, its single use, or the password it was issued for."""
import json
import re
import smtplib
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone

from accounts.tests import _ensure_google_social_app
from accounts import customer_emails as ce
from accounts import login_throttle, owner_recovery, rate_limit
from accounts.models import OwnerRecovery, UserProfile
from arabela_system.urls import not_found

User = get_user_model()

OWNER_PASSWORD = "Str0ng-Owner-Pass!"
NEW_PASSWORD = "river-candle-orange-42"
PROFILE_EMAIL = "shop.contact@gmail.com"
RECOVERY_EMAIL = "owner.recovery@gmail.com"
SETTINGS = dict(
    CUSTOMER_EMAILS_BACKGROUND=False, SITE_BASE_URL="https://arabela.example.org",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],      # fast hashing: these tests make many accounts
)

SEND = reverse("arabela_admin:recovery_send_code")
VERIFY = reverse("arabela_admin:recovery_verify_code")
FORGOT = reverse("arabela_admin:forgot_password")
LOGIN = reverse("arabela_admin:admin_login")
SENT_LOCATION = FORGOT + "?sent=1"


def post_json(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


class RecoveryBase(TestCase):
    """An owner (with a profile email), a staff member and a customer who happens to share the owner's profile Gmail."""

    def setUp(self):
        self.owner = User.objects.create_superuser("admin", PROFILE_EMAIL, OWNER_PASSWORD, first_name="Honie")
        UserProfile.objects.update_or_create(user=self.owner, defaults={"role": UserProfile.Role.OWNER})
        self.staff = User.objects.create_user("mark", "mark.staff@gmail.com", "Staff-Pass-123!", is_staff=True)
        UserProfile.objects.update_or_create(user=self.staff, defaults={"role": UserProfile.Role.STAFF})
        self.customer = User.objects.create_user("custgoogle", PROFILE_EMAIL)        # a Google-only customer on the same Gmail
        self.client_owner = Client()
        self.client_owner.force_login(self.owner)

    def give_recovery(self, address=RECOVERY_EMAIL, user=None):
        return OwnerRecovery.objects.create(user=user or self.owner, email=address, verified_at=timezone.now())

    def code_from_outbox(self, index=-1):
        found = re.search(r"CONFIRMATION CODE: (\d{6})", mail.outbox[index].body)
        self.assertIsNotNone(found, mail.outbox[index].body)
        return found.group(1)

    def link_from_outbox(self, index=-1):
        found = re.search(r"https://arabela\.example\.org(/admin-panel/reset-password/\S+/)", mail.outbox[index].body)
        self.assertIsNotNone(found, mail.outbox[index].body)
        return found.group(1)


# ---- 1. the two hidden doors are closed, Google sign-in is not touched -------------------------------------------------------
class ClosedDoorsTests(TestCase):
    def test_allauth_password_and_email_pages_are_gone(self):
        for path in ("/accounts/password/reset/", "/accounts/password/reset/done/", "/accounts/password/reset/key/1-abc-def/",
                     "/accounts/password/reset/key/done/", "/accounts/password/change/", "/accounts/password/set/",
                     "/accounts/email/", "/accounts/confirm-email/", "/accounts/confirm-email/abc123/", "/accounts/reauthenticate/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_the_reset_form_cannot_be_posted_to_either(self):
        owner = User.objects.create_user("owner2", "o2@gmail.com", "x")
        response = self.client.post("/accounts/password/reset/", {"email": owner.email})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(mail.outbox), 0)

    def test_djangos_own_admin_sign_in_is_gone(self):
        for path in ("/admin/", "/admin/login/", "/admin/login/?next=/admin/", "/admin/auth/user/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.post("/admin/login/", {"username": "x", "password": "y"}).status_code, 404)

    def test_the_real_admin_panel_is_not_caught_by_those_rules(self):
        self.assertEqual(self.client.get(LOGIN).status_code, 200)
        self.assertEqual(self.client.get("/admin-panel/").status_code, 302)          # to its own sign-in page, not a 404

    def test_google_sign_in_and_its_error_pages_still_route_to_allauth(self):
        for path in ("/accounts/google/login/", "/accounts/google/login/callback/", "/accounts/3rdparty/login/cancelled/",
                     "/accounts/3rdparty/login/error/", "/accounts/3rdparty/signup/", "/accounts/3rdparty/"):
            with self.subTest(path=path):
                self.assertIsNot(resolve(path).func, not_found)
        self.assertEqual(resolve("/accounts/google/login/").url_name, "google_login")

    def test_the_customer_sign_in_page_still_works(self):
        _ensure_google_social_app()
        response = self.client.get("/accounts/login/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Continue with Google")


# ---- 2. setting the recovery email: current password + a code mailed to the NEW address ------------------------------------
@override_settings(**SETTINGS)
class SendCodeTests(RecoveryBase):
    def send(self, email=RECOVERY_EMAIL, password=OWNER_PASSWORD, client=None):
        return post_json(client or self.client_owner, SEND, {"email": email, "current_password": password})

    def test_only_the_owner_can_ask_for_a_code(self):
        staff_client = Client()
        staff_client.force_login(self.staff)
        self.assertEqual(self.send(client=staff_client).status_code, 403)
        self.assertEqual(self.send(client=Client()).status_code, 302)                 # not signed in: sent to the sign-in page
        customer_client = Client()
        customer_client.force_login(self.customer)
        self.assertEqual(self.send(client=customer_client).status_code, 302)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_wrong_current_password_sends_nothing(self):
        response = self.send(password="not-my-password")
        self.assertEqual(response.status_code, 400)
        self.assertIn("current password is incorrect", response.json()["error"])
        self.assertEqual(len(mail.outbox), 0)

    def test_wrong_password_guessing_is_limited_like_the_sign_in_page(self):
        for _ in range(login_throttle.ATTEMPT_LIMIT):
            self.assertEqual(self.send(password="guess").status_code, 400)
        locked = self.send(password="guess")
        self.assertEqual(locked.status_code, 429)
        self.assertIn("Too many wrong passwords", locked.json()["error"])
        self.assertEqual(self.send(password=OWNER_PASSWORD).status_code, 429)         # even the right one waits
        self.assertEqual(len(mail.outbox), 0)
        # ...but it is its own count: the sign-in page itself is not locked by it
        self.assertEqual(login_throttle.seconds_locked(login_throttle.username_key("admin")), 0)

    def test_a_right_password_clears_the_count(self):
        for _ in range(login_throttle.ATTEMPT_LIMIT - 1):
            self.send(password="guess")
        self.assertEqual(self.send().status_code, 200)
        later = owner_recovery._now_ts() + owner_recovery.SEND_COOLDOWN_SECONDS + 1
        with mock.patch.object(owner_recovery, "_now_ts", return_value=later):
            for _ in range(login_throttle.ATTEMPT_LIMIT - 1):
                self.assertEqual(self.send(password="guess").status_code, 400)           # a fresh count of 4, not 8

    def test_addresses_that_cannot_work_are_refused(self):
        for bad in ("", "no-at-sign", "a@b", "x@example.com", "x@shop.test", "x@mail.invalid", "a b@gmail.com", "x" * 250 + "@gmail.com", None):
            with self.subTest(email=bad):
                response = self.send(email=bad)
                self.assertEqual(response.status_code, 400)
                self.assertIn("real email address", response.json()["error"])
        self.assertEqual(len(mail.outbox), 0)

    def test_the_code_goes_to_the_new_address_only_and_nothing_is_saved_yet(self):
        response = self.send(email="  Owner.Recovery@Gmail.com ")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"success": True, "email_masked": "o***@gmail.com"})
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [RECOVERY_EMAIL])                                 # lower-cased, trimmed
        self.assertEqual(message.subject, "Your Arabela recovery email code")
        code = self.code_from_outbox()
        self.assertRegex(code, r"^\d{6}$")
        html = message.alternatives[0][0]
        self.assertIn(code, html)
        self.assertNotIn(OWNER_PASSWORD, message.body + html)
        self.assertFalse(OwnerRecovery.objects.exists())                               # not saved until the code comes back

    def test_the_pending_code_is_kept_only_as_a_keyed_hash(self):
        self.send()
        code = self.code_from_outbox()
        pending = self.client_owner.session[owner_recovery.SESSION_KEY]
        self.assertNotIn(code, json.dumps(pending))
        self.assertEqual(len(pending["hash"]), 64)
        self.assertEqual(pending["email"], RECOVERY_EMAIL)

    def test_the_old_recovery_email_stays_until_the_new_one_is_confirmed(self):
        self.give_recovery("old.address@gmail.com")
        self.send()
        self.assertEqual(OwnerRecovery.objects.get(user=self.owner).email, "old.address@gmail.com")

    def test_the_same_address_again_is_refused(self):
        self.give_recovery()
        response = self.send()
        self.assertEqual(response.status_code, 400)
        self.assertIn("already your recovery email", response.json()["error"])
        self.assertEqual(len(mail.outbox), 0)

    def test_two_clicks_in_a_row_send_one_code(self):
        self.assertEqual(self.send().status_code, 200)
        second = self.send()
        self.assertEqual(second.status_code, 429)
        self.assertIn("wait", second.json()["error"])
        self.assertEqual(len(mail.outbox), 1)

    def test_a_new_code_can_be_asked_for_after_the_wait_and_replaces_the_old_one(self):
        self.send()
        first = self.code_from_outbox()
        later = owner_recovery._now_ts() + owner_recovery.SEND_COOLDOWN_SECONDS + 1
        with mock.patch.object(owner_recovery, "_now_ts", return_value=later):
            self.assertEqual(self.send().status_code, 200)
            second = self.code_from_outbox()
            self.assertEqual(len(mail.outbox), 2)
            if first != second:
                self.assertEqual(post_json(self.client_owner, VERIFY, {"code": first}).status_code, 400)   # the old code is dead
            self.assertEqual(post_json(self.client_owner, VERIFY, {"code": second}).status_code, 200)

    def test_at_most_five_codes_an_hour(self):
        start = owner_recovery._now_ts()
        for n in range(owner_recovery.SENDS_PER_HOUR):
            with mock.patch.object(owner_recovery, "_now_ts", return_value=start + n * 100):
                self.assertEqual(self.send().status_code, 200, n)
        with mock.patch.object(owner_recovery, "_now_ts", return_value=start + 900):
            response = self.send()
        self.assertEqual(response.status_code, 429)
        self.assertIn("Too many codes", response.json()["error"])
        self.assertEqual(len(mail.outbox), owner_recovery.SENDS_PER_HOUR)

    def test_when_the_email_cannot_be_sent_nothing_changes_and_the_owner_is_told(self):
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=smtplib.SMTPRecipientsRefused({})), \
                mock.patch.object(owner_recovery, "_sleep"):
            response = self.send()
        self.assertEqual(response.status_code, 502)
        self.assertIn("could not be sent", response.json()["error"])
        self.assertNotIn(owner_recovery.SESSION_KEY, self.client_owner.session)
        self.assertFalse(OwnerRecovery.objects.exists())

    def test_it_says_so_when_the_database_table_does_not_exist_yet(self):
        with mock.patch.object(OwnerRecovery.objects, "filter", side_effect=DatabaseError("no such table")):
            response = self.send()
        self.assertEqual(response.status_code, 503)
        self.assertIn("database update", response.json()["error"])
        self.assertEqual(len(mail.outbox), 0)

    def test_it_is_a_post_only_endpoint(self):
        self.assertEqual(self.client_owner.get(SEND).status_code, 405)
        self.assertEqual(self.client_owner.post(SEND, data="not json", content_type="application/json").status_code, 400)
        self.assertEqual(post_json(self.client_owner, SEND, ["a list"]).status_code, 400)


# ---- 3. confirming the code ---------------------------------------------------------------------------------------------------
@override_settings(**SETTINGS)
class VerifyCodeTests(RecoveryBase):
    def start(self, address=RECOVERY_EMAIL):
        response = post_json(self.client_owner, SEND, {"email": address, "current_password": OWNER_PASSWORD})
        self.assertEqual(response.status_code, 200, response.content)
        return self.code_from_outbox()

    def verify(self, code, client=None):
        return post_json(client or self.client_owner, VERIFY, {"code": code})

    def test_the_right_code_saves_the_recovery_email(self):
        code = self.start()
        response = self.verify(code)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True, "email_masked": "o***@gmail.com"})
        row = OwnerRecovery.objects.get(user=self.owner)
        self.assertEqual(row.email, RECOVERY_EMAIL)
        self.assertLess(abs((timezone.now() - row.verified_at).total_seconds()), 60)
        self.assertNotIn(owner_recovery.SESSION_KEY, self.client_owner.session)
        self.assertEqual(len(mail.outbox), 1)                                          # the code email only: nobody to warn the first time

    def test_a_code_typed_with_spaces_or_dashes_still_works(self):
        code = self.start()
        self.assertEqual(self.verify(f"{code[:3]} - {code[3:]}").status_code, 200)

    def test_a_wrong_code_is_refused_and_counted(self):
        code = self.start()
        response = self.verify("000000" if code != "000000" else "111111")
        self.assertEqual(response.status_code, 400)
        self.assertIn("4 tries left", response.json()["error"])
        self.assertFalse(OwnerRecovery.objects.exists())

    def test_five_wrong_codes_throw_the_code_away(self):
        code = self.start()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(owner_recovery.CODE_MAX_TRIES):
            self.assertEqual(self.verify(wrong).status_code, 400)
        self.assertNotIn(owner_recovery.SESSION_KEY, self.client_owner.session)
        response = self.verify(code)                                                    # even the true code is dead now
        self.assertEqual(response.status_code, 400)
        self.assertIn("Ask for a new code", response.json()["error"])
        self.assertFalse(OwnerRecovery.objects.exists())

    def test_without_asking_for_a_code_first_there_is_nothing_to_confirm(self):
        response = self.verify("123456")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Ask for a new code first", response.json()["error"])

    def test_an_old_code_expires(self):
        code = self.start()
        later = owner_recovery._now_ts() + owner_recovery.CODE_TTL_SECONDS + 5
        with mock.patch.object(owner_recovery, "_now_ts", return_value=later):
            response = self.verify(code)
        self.assertEqual(response.status_code, 400)
        self.assertIn("expired", response.json()["error"])
        self.assertFalse(OwnerRecovery.objects.exists())

    def test_a_code_still_works_just_before_it_expires(self):
        code = self.start()
        almost = owner_recovery._now_ts() + owner_recovery.CODE_TTL_SECONDS - 5
        with mock.patch.object(owner_recovery, "_now_ts", return_value=almost):
            self.assertEqual(self.verify(code).status_code, 200)

    def test_a_code_is_tied_to_its_own_address(self):
        code = self.start()
        session = self.client_owner.session
        session[owner_recovery.SESSION_KEY]["email"] = "someone.else@gmail.com"        # as if the pending address were swapped
        session.save()
        self.assertEqual(self.verify(code).status_code, 400)
        self.assertFalse(OwnerRecovery.objects.exists())

    def test_replacing_a_confirmed_address_warns_the_old_one(self):
        self.give_recovery("old.address@gmail.com")
        code = self.start()
        self.assertEqual(self.verify(code).status_code, 200)
        self.assertEqual(OwnerRecovery.objects.get(user=self.owner).email, RECOVERY_EMAIL)
        self.assertEqual(len(mail.outbox), 2)
        notice = mail.outbox[1]
        self.assertEqual(notice.to, ["old.address@gmail.com"])
        self.assertEqual(notice.subject, "Your Arabela recovery email was changed")
        self.assertIn("o***@gmail.com", notice.body)
        self.assertNotIn(RECOVERY_EMAIL, notice.body)                                  # the new address is shown masked only

    def test_staff_cannot_confirm_anything(self):
        code = self.start()
        staff_client = Client()
        staff_client.force_login(self.staff)
        self.assertEqual(self.verify(code, client=staff_client).status_code, 403)
        self.assertEqual(self.verify(code, client=Client()).status_code, 302)

    def test_another_browser_cannot_use_the_owners_pending_code(self):
        code = self.start()
        second_session = Client()
        second_session.force_login(self.owner)                                         # same owner, a different session: nothing pending there
        self.assertEqual(self.verify(code, client=second_session).status_code, 400)

    def test_verify_attempts_are_limited_per_hour(self):
        self.start()
        with mock.patch.object(owner_recovery, "CODE_MAX_TRIES", 1000):
            for _ in range(20):
                self.verify("000000")
            response = self.verify("000000")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Too many tries", response.json()["error"])


# ---- 4. "Forgot password?" ------------------------------------------------------------------------------------------------------
@override_settings(**SETTINGS)
class ForgotPasswordTests(RecoveryBase):
    def ask(self, username, client=None, **extra):
        return (client or Client()).post(FORGOT, {"username": username}, **extra)

    def test_the_page_is_public_and_asks_for_a_username(self):
        response = Client().get(FORGOT)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="username"')
        self.assertNotContains(response, 'name="email"')
        self.assertNotContains(response, "reset link is on its way")
        self.assertContains(Client().get(FORGOT + "?sent=1"), "reset link is on its way")
        self.assertIn("no-store", Client().get(FORGOT)["Cache-Control"])

    def test_the_owner_with_a_recovery_email_gets_one_link_at_that_address(self):
        self.give_recovery()
        response = self.ask("admin")
        self.assertRedirects(response, SENT_LOCATION, fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [RECOVERY_EMAIL])                                 # never the profile address
        self.assertEqual(message.subject, "Reset your Arabela admin password")
        self.assertIn("https://arabela.example.org/admin-panel/reset-password/", message.body)
        self.assertIn(self.link_from_outbox(), message.alternatives[0][0])             # the button carries the same link

    def test_every_other_case_gets_the_very_same_answer_and_no_email(self):
        self.give_recovery()
        # a staff member WITH a recovery row (made by hand), the shared-Gmail customer, nobody, blank, a long string
        self.give_recovery("mark.recovery@gmail.com", user=self.staff)
        answers = set()
        for name in ("mark", "custgoogle", "nobody-here", "", "x" * 500):
            response = self.ask(name)
            answers.add((response.status_code, response.get("Location")))
        self.assertEqual(answers, {(302, SENT_LOCATION)})
        self.assertEqual(len(mail.outbox), 0)

    def test_the_owner_without_a_recovery_email_gets_nothing_but_the_same_answer(self):
        response = self.ask("admin")
        self.assertRedirects(response, SENT_LOCATION, fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 0)

    def test_the_answer_is_identical_for_a_real_and_a_made_up_username(self):
        self.give_recovery()
        real = self.ask("admin")
        made_up = self.ask("zzz-nobody")
        self.assertEqual((real.status_code, real["Location"]), (made_up.status_code, made_up["Location"]))
        self.assertEqual(real.content, made_up.content)

    def test_the_username_is_not_case_sensitive(self):
        self.give_recovery()
        self.ask("  ADMIN ")
        self.assertEqual(len(mail.outbox), 1)

    def test_an_email_address_typed_in_the_username_box_finds_nobody(self):
        self.give_recovery()
        self.ask(PROFILE_EMAIL)
        self.ask(RECOVERY_EMAIL)
        self.assertEqual(len(mail.outbox), 0)

    def test_an_inactive_owner_gets_nothing(self):
        self.give_recovery()
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        self.ask("admin")
        self.assertEqual(len(mail.outbox), 0)

    def test_a_staff_account_never_gets_a_link_even_with_a_recovery_row(self):
        self.give_recovery("mark.recovery@gmail.com", user=self.staff)
        self.ask("mark")
        self.assertEqual(len(mail.outbox), 0)

    def test_at_most_three_links_per_username_per_hour_and_the_page_still_answers_the_same(self):
        self.give_recovery()
        for _ in range(5):
            self.assertRedirects(self.ask("admin"), SENT_LOCATION, fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 3)

    def test_at_most_ten_requests_per_connection_per_hour(self):
        self.give_recovery()
        for n in range(10):
            self.ask(f"someone-{n}")
        self.ask("admin")                                                              # the 11th request from this connection
        self.assertEqual(len(mail.outbox), 0)

    def test_the_connection_limit_counts_the_forwarded_address_render_appends(self):
        self.give_recovery()
        for n in range(10):
            self.ask(f"someone-{n}", HTTP_X_FORWARDED_FOR="1.1.1.1, 203.0.113.9")
        self.ask("admin", HTTP_X_FORWARDED_FOR="9.9.9.9, 203.0.113.9")                 # same real connection, a different claimed first entry
        self.assertEqual(len(mail.outbox), 0)
        self.ask("admin", HTTP_X_FORWARDED_FOR="9.9.9.9, 198.51.100.7")                # a different connection is fine
        self.assertEqual(len(mail.outbox), 1)

    def test_changing_the_profile_email_does_not_change_where_the_link_goes(self):
        self.give_recovery()
        self.client_owner.post(reverse("arabela_admin:save_profile"), data=json.dumps({"email": "attacker@gmail.com"}),
                               content_type="application/json")
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.email, "attacker@gmail.com")
        self.ask("admin")
        self.assertEqual(mail.outbox[0].to, [RECOVERY_EMAIL])

    def test_the_form_is_protected_against_cross_site_posts(self):
        self.give_recovery()
        response = Client(enforce_csrf_checks=True).post(FORGOT, {"username": "admin"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_missing_database_table_does_not_break_the_page(self):
        with mock.patch.object(OwnerRecovery.objects, "filter", side_effect=DatabaseError("no such table")):
            response = self.ask("admin")
        self.assertRedirects(response, SENT_LOCATION, fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_failing_mail_server_does_not_change_the_answer(self):
        self.give_recovery()
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=smtplib.SMTPServerDisconnected("down")), \
                mock.patch.object(owner_recovery, "_sleep"):
            response = self.ask("admin")
        self.assertRedirects(response, SENT_LOCATION, fetch_redirect_response=False)

    def test_background_sending_answers_without_waiting_for_the_mail(self):
        self.give_recovery()
        started = []

        class FakeThread:
            def __init__(self, target, args=(), **kwargs):
                started.append((target, args))

            def start(self):
                pass

        with override_settings(CUSTOMER_EMAILS_BACKGROUND=True), mock.patch.object(owner_recovery.threading, "Thread", FakeThread):
            response = self.ask("admin")
        self.assertRedirects(response, SENT_LOCATION, fetch_redirect_response=False)
        self.assertEqual(len(started), 1)                                              # handed to a thread, not sent in the request
        self.assertEqual(len(mail.outbox), 0)


# ---- 5. the reset link --------------------------------------------------------------------------------------------------------
@override_settings(**SETTINGS)
class ResetPasswordTests(RecoveryBase):
    def setUp(self):
        super().setUp()
        self.give_recovery()

    def new_link(self):
        Client().post(FORGOT, {"username": "admin"})
        return self.link_from_outbox()

    def post_new(self, path, first=NEW_PASSWORD, second=None, client=None):
        return (client or Client()).post(path, {"password1": first, "password2": first if second is None else second})

    def assertInvalidPage(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This link can")
        self.assertNotContains(response, 'name="password1"')

    def test_a_good_link_shows_the_form_without_leaking_itself(self):
        response = Client().get(self.new_link())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose a new password")
        self.assertContains(response, 'name="password1"')
        # nothing goes to other sites, but the page's own form still sends its Origin (a "no-referrer" policy makes browsers send
        # "Origin: null" on the POST, which Django's CSRF check refuses -- the password could never be saved)
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertContains(response, '<meta name="referrer" content="same-origin" />')
        self.assertNotContains(response, "no-referrer")
        self.assertIn("no-store", response["Cache-Control"])

    def test_the_form_post_works_with_the_origin_header_a_real_browser_sends(self):
        path = self.new_link()
        strict = Client(enforce_csrf_checks=True)
        page = strict.get(path)
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
        response = strict.post(path, {"csrfmiddlewaretoken": token, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
                               HTTP_ORIGIN="http://testserver")
        self.assertEqual(response.status_code, 302, response.content[:300])
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password(NEW_PASSWORD))

    def test_a_good_link_sets_the_password_and_everything_that_should_follow(self):
        other_device = Client()
        other_device.force_login(self.owner)        # signed in BEFORE the link is made: signing in afterwards would (rightly) kill the link
        self.assertEqual(other_device.get("/admin-panel/").status_code, 200)
        path = self.new_link()
        key = login_throttle.username_key("admin")
        for _ in range(login_throttle.ATTEMPT_LIMIT):
            login_throttle.record_failure(key)
        self.assertGreater(login_throttle.seconds_locked(key), 0)                      # the owner is locked out of the sign-in page...

        response = self.post_new(path)
        self.assertRedirects(response, LOGIN + "?reset=done", fetch_redirect_response=False)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password(NEW_PASSWORD))
        self.assertFalse(self.owner.check_password(OWNER_PASSWORD))
        self.assertEqual(login_throttle.seconds_locked(key), 0)                        # ...and the reset lifts that lock
        self.assertEqual(other_device.get("/admin-panel/").status_code, 302)           # the other device is signed out
        notice = mail.outbox[-1]
        self.assertEqual((notice.to, notice.subject), ([RECOVERY_EMAIL], "Your Arabela admin password was changed"))
        self.assertNotIn(NEW_PASSWORD, notice.body)
        # she can now really sign in with it
        signed_in = Client()
        self.assertEqual(signed_in.post(LOGIN, {"username": "admin", "password": NEW_PASSWORD}).status_code, 302)
        self.assertEqual(signed_in.get("/admin-panel/").status_code, 200)

    def test_the_sign_in_page_says_the_password_was_changed(self):
        self.assertContains(Client().get(LOGIN + "?reset=done"), "Your password was changed")
        self.assertNotContains(Client().get(LOGIN), "Your password was changed")
        self.assertContains(Client().get(LOGIN), reverse("arabela_admin:forgot_password"))

    def test_a_link_works_only_once(self):
        path = self.new_link()
        self.assertEqual(self.post_new(path).status_code, 302)
        self.assertInvalidPage(Client().get(path))
        self.assertInvalidPage(self.post_new(path, first="another-fresh-passphrase-9"))
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password(NEW_PASSWORD))

    def test_a_link_lasts_thirty_minutes(self):
        path = self.new_link()
        generator = owner_recovery.token_generator
        with mock.patch.object(generator, "_now", return_value=generator._now() + timedelta(minutes=29)):
            self.assertContains(Client().get(path), 'name="password1"')
        with mock.patch.object(generator, "_now", return_value=generator._now() + timedelta(minutes=31)):
            self.assertInvalidPage(Client().get(path))
            self.assertInvalidPage(self.post_new(path))
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password(OWNER_PASSWORD))

    def test_a_tampered_or_made_up_link_is_refused(self):
        path = self.new_link()
        prefix, uid, token, _ = path.rsplit("/", 3)
        flipped = token[:-1] + ("a" if token[-1] != "a" else "b")
        for bad in (f"{prefix}/{uid}/{flipped}/", f"{prefix}/{uid}/{token.split('-')[0]}-{'0' * 20}/", f"{prefix}/MA/{token}/",
                    f"{prefix}/!!!!/{token}/", f"{prefix}/{uid}/abc/", f"{prefix}/{uid}/-/"):
            with self.subTest(link=bad):
                self.assertInvalidPage(Client().get(bad))

    def test_the_link_dies_when_the_owner_signs_in_normally(self):
        User.objects.filter(pk=self.owner.pk).update(last_login=timezone.now() - timedelta(minutes=5))   # (Django stamps to the second)
        path = self.new_link()
        self.assertEqual(Client().post(LOGIN, {"username": "admin", "password": OWNER_PASSWORD}).status_code, 302)
        self.assertInvalidPage(Client().get(path))

    def test_the_link_dies_when_the_password_is_changed_another_way(self):
        path = self.new_link()
        self.owner.set_password("changed-elsewhere-77")
        self.owner.save()
        self.assertInvalidPage(Client().get(path))

    def test_the_link_dies_when_the_recovery_email_changes(self):
        path = self.new_link()
        OwnerRecovery.objects.filter(user=self.owner).update(email="brand.new@gmail.com")
        self.assertInvalidPage(Client().get(path))

    def test_a_newer_link_does_not_have_to_kill_the_older_one_but_each_works_once(self):
        first = self.new_link()
        second = self.new_link()
        self.assertEqual(self.post_new(second).status_code, 302)
        self.assertInvalidPage(Client().get(first))                                    # the password changed, so the older one is dead too

    def test_a_staff_account_cannot_use_a_link_even_if_one_is_made_for_it(self):
        self.give_recovery("mark.recovery@gmail.com", user=self.staff)
        path = reverse("arabela_admin:reset_password", kwargs={
            "uidb64": owner_recovery.urlsafe_base64_encode(owner_recovery.force_bytes(self.staff.pk)),
            "token": owner_recovery.token_generator.make_token(self.staff)})
        self.assertInvalidPage(Client().get(path))
        self.assertInvalidPage(self.post_new(path))
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password("Staff-Pass-123!"))

    def test_a_link_for_the_owner_needs_a_recovery_email_to_still_exist(self):
        path = self.new_link()
        OwnerRecovery.objects.all().delete()
        self.assertInvalidPage(Client().get(path))

    def test_a_weak_new_password_is_refused_and_the_old_one_stays(self):
        path = self.new_link()
        for first, second, expected in (
            ("short9chr", None, "at least 10 characters"),
            (NEW_PASSWORD, "something-else-entirely", "do not match"),
            ("12345678901234", None, "entirely numeric"),
            ("qwertyuiop", None, "too common"),
            ("", "", "type a new password"),
            (OWNER_PASSWORD, None, "current password"),
            ("a" * 129, None, "128 characters or fewer"),
        ):
            with self.subTest(first=first[:12]):
                response = self.post_new(path, first=first, second=second)
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, expected, status_code=400)
                self.assertContains(response, 'name="password1"', status_code=400)       # the form is still there to try again
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password(OWNER_PASSWORD))
        self.assertEqual(self.post_new(path).status_code, 302)                            # and the link still works after those mistakes

    def test_a_password_too_close_to_the_account_details_is_refused(self):
        path = self.new_link()
        response = self.post_new(path, first=PROFILE_EMAIL)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "similar", status_code=400)

    def test_opening_the_page_too_often_from_one_connection_is_stopped(self):
        path = self.new_link()
        with mock.patch.object(owner_recovery, "RESET_PAGE_PER_IP", (2, 3600)):
            client = Client()
            self.assertContains(client.get(path), 'name="password1"')
            self.assertContains(client.get(path), 'name="password1"')
            self.assertInvalidPage(client.get(path))

    def test_the_reset_form_is_protected_against_cross_site_posts(self):
        path = self.new_link()
        response = Client(enforce_csrf_checks=True).post(path, {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(response.status_code, 403)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password(OWNER_PASSWORD))

    def test_only_get_and_post_are_accepted(self):
        path = self.new_link()
        self.assertEqual(Client().put(path).status_code, 405)
        self.assertEqual(Client().delete(path).status_code, 405)


# ---- 6. the emails ------------------------------------------------------------------------------------------------------------
@override_settings(**SETTINGS)
class EmailTests(TestCase):
    def test_each_owner_email_is_built_in_the_shops_layout(self):
        link = "https://arabela.example.org/admin-panel/reset-password/Mg/abc-def/"
        cases = {
            owner_recovery.KIND_CODE: dict(code="482913"),
            owner_recovery.KIND_RESET: dict(link=link),
            owner_recovery.KIND_PASSWORD_CHANGED: {},
            owner_recovery.KIND_RECOVERY_CHANGED: dict(masked="n***@gmail.com"),
        }
        for kind, extra in cases.items():
            with self.subTest(kind=kind):
                built = owner_recovery.build_email(kind, first_name="Honie", **extra)
                self.assertTrue(built.subject)
                self.assertIn("Hi Honie,", built.text)
                self.assertIn("Hi Honie,", built.html)
                self.assertNotIn("{{", built.html + built.text)
                self.assertNotIn("{%", built.html + built.text)
                self.assertTrue(built.inline_logo or not ce._logo_bytes())
                self.assertIn("This is an automatic message", built.html)

    def test_the_code_email_shows_the_code_boxed_and_in_plain_text(self):
        built = owner_recovery.build_email(owner_recovery.KIND_CODE, first_name="Honie", code="482913")
        self.assertIn("482913", built.html)
        self.assertIn("letter-spacing:8px", built.html)
        self.assertIn("YOUR CONFIRMATION CODE: 482913", built.text)
        self.assertIn("Account Settings", built.html)
        self.assertIn("https://arabela.example.org/admin-panel/account-settings.html", built.html)

    def test_the_reset_email_button_is_the_link(self):
        link = "https://arabela.example.org/admin-panel/reset-password/Mg/abc-def/"
        built = owner_recovery.build_email(owner_recovery.KIND_RESET, first_name="Honie", link=link)
        self.assertIn(f'href="{link}"', built.html)
        self.assertIn(link, built.text)
        self.assertIn("30 minutes", built.text)

    def test_a_name_with_html_in_it_is_escaped(self):
        built = owner_recovery.build_email(owner_recovery.KIND_CODE, first_name="<b>x</b>", code="123456")
        self.assertNotIn("<b>x</b>", built.html)

    def test_the_existing_customer_emails_are_unchanged(self):
        built = ce.build_email(ce.APPROVED, ce.sample_data(ce.APPROVED), base_url="https://arabela.example.org", shop={})
        self.assertNotIn("letter-spacing:8px", built.html)
        self.assertNotIn("CONFIRMATION CODE", built.text)
        self.assertIn("https://arabela.example.org/reservations/", built.html)

    def test_an_unknown_kind_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            owner_recovery.build_email("nope")


# ---- 7. the pages that changed ------------------------------------------------------------------------------------------------
@override_settings(**SETTINGS)
class PageTests(RecoveryBase):
    SETTINGS_URL = "/admin-panel/account-settings.html"

    def test_the_owner_sees_the_recovery_card_with_only_a_masked_address(self):
        self.give_recovery()
        response = self.client_owner.get(self.SETTINGS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recovery Email")
        self.assertContains(response, "o***@gmail.com")
        self.assertNotContains(response, RECOVERY_EMAIL)
        self.assertContains(response, reverse("arabela_admin:recovery_send_code"))
        self.assertContains(response, reverse("arabela_admin:recovery_verify_code"))

    def test_before_a_recovery_email_exists_the_card_invites_the_owner_to_add_one(self):
        response = self.client_owner.get(self.SETTINGS_URL)
        self.assertContains(response, "No recovery email yet")
        self.assertContains(response, "recoveryMasked: ''")

    def test_staff_do_not_get_the_card(self):
        staff_client = Client()
        staff_client.force_login(self.staff)
        response = staff_client.get(self.SETTINGS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="recovery-email"')

    def test_the_page_still_opens_when_the_database_table_is_missing(self):
        with mock.patch.object(OwnerRecovery.objects, "filter", side_effect=DatabaseError("no such table")):
            response = self.client_owner.get(self.SETTINGS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "database update has not been applied")
        self.assertContains(response, "Change Password")                                # the rest of the page is untouched

    def test_the_existing_account_settings_forms_are_still_there(self):
        response = self.client_owner.get(self.SETTINGS_URL)
        for needle in ('x-model="currentPassword"', 'x-model="newPassword"', "Change Username", "Show passwords", "Previous sign-in"):
            self.assertContains(response, needle)

    def test_staff_management_password_boxes_have_a_working_eye(self):
        response = self.client_owner.get(reverse("arabela_admin:staff_management"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertEqual(html.count(":type=\"showPw ? 'text' : 'password'\""), 2)         # Add Account and Edit Account
        self.assertEqual(html.count('type="password"'), 0)                                # no password box is left without it
        self.assertIn('x-model="newAccountPassword"', html)
        self.assertIn('x-model="editPassword"', html)
        self.assertIn("::-ms-reveal", html)
        self.assertEqual(html.count(":aria-label=\"showPw ? 'Hide password' : 'Show password'\""), 2)

    def test_the_forgot_and_reset_pages_have_no_dependency_on_a_cdn(self):
        self.give_recovery()
        Client().post(FORGOT, {"username": "admin"})
        for html in (Client().get(FORGOT).content.decode(), Client().get(self.link_from_outbox()).content.decode()):
            self.assertNotIn("cdn.jsdelivr", html)
            self.assertNotIn("alpinejs", html)
