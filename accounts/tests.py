from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialApp

from accounts.models import UserProfile

User = get_user_model()


def _ensure_google_social_app():
    """login.html/signup.html/verification.html all render a "Sign in with Google"
    link via allauth's {% provider_login_url %}, which looks up a SocialApp row for
    the current Site. The real dev database has one added manually (outside any
    migration); a fresh test database starts with none, so every page render would
    otherwise fail with SocialApp.DoesNotExist. The actual client id/secret are
    irrelevant here -- no test in this file completes a real Google OAuth round trip,
    the page just needs the row to exist to render the link at all."""
    site = Site.objects.get_current()
    app, _ = SocialApp.objects.get_or_create(
        provider="google", name="Google",
        defaults={"client_id": "test-client-id", "secret": "test-secret"},
    )
    app.sites.add(site)
    return app

# Signup/verification send a real email via Gmail SMTP by default (see
# arabela_system/settings.py). Every test in this file must run against Django's
# in-memory backend instead -- this must NEVER touch the real Gmail account.
_TEST_EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


@override_settings(EMAIL_BACKEND=_TEST_EMAIL_BACKEND)
class SignupTests(TestCase):
    """`signup_view` -- account creation, its field rules, and that a new account is
    correctly left unverified pending the emailed code (never auto-verified)."""

    @classmethod
    def setUpTestData(cls):
        _ensure_google_social_app()

    def setUp(self):
        mail.outbox = []

    def _signup(self, **overrides):
        data = dict(
            first_name="Test", last_name="Signup", email="test.signup@gmail.com",
            password="Password123", accept_terms="on",
        )
        data.update(overrides)
        return self.client.post(reverse("accounts:signup"), data=data)

    def test_valid_signup_creates_an_unverified_user_and_sends_one_email(self):
        response = self._signup()
        self.assertRedirects(response, reverse("accounts:verify_email_pending"))
        user = User.objects.get(email="test.signup@gmail.com")
        self.assertFalse(
            EmailAddress.objects.filter(user=user, email=user.email, verified=True).exists()
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(user.email, mail.outbox[0].to)

    def test_non_gmail_address_is_rejected(self):
        response = self._signup(email="test@yahoo.com")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Gmail")
        self.assertFalse(User.objects.filter(email="test@yahoo.com").exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_weak_password_is_rejected(self):
        response = self._signup(email="weak.pw@gmail.com", password="short")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="weak.pw@gmail.com").exists())

    def test_password_without_a_letter_is_rejected(self):
        response = self._signup(email="numonly@gmail.com", password="12345678")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="numonly@gmail.com").exists())

    def test_missing_terms_acceptance_is_rejected(self):
        response = self._signup(email="noterms@gmail.com", accept_terms="")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="noterms@gmail.com").exists())

    def test_signing_up_again_with_an_already_verified_email_is_rejected(self):
        existing = User.objects.create_user(username="already", email="already@gmail.com", password="Password123")
        EmailAddress.objects.create(user=existing, email="already@gmail.com", verified=True, primary=True)
        response = self._signup(email="already@gmail.com")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already registered")

    def test_signing_up_again_with_an_unverified_email_resends_the_code(self):
        existing = User.objects.create_user(username="pending", email="pending@gmail.com", password="Password123")
        EmailAddress.objects.create(user=existing, email="pending@gmail.com", verified=False, primary=True)
        response = self._signup(email="pending@gmail.com")
        self.assertRedirects(response, f"{reverse('accounts:verify_email_pending')}?email=pending@gmail.com&resent=1")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(User.objects.filter(email="pending@gmail.com").count(), 1)  # no duplicate account


@override_settings(EMAIL_BACKEND=_TEST_EMAIL_BACKEND)
class EmailVerificationTests(TestCase):
    """`verify_email_code_view` -- the actual security check that turns a pending
    signup into a usable account. Must accept only the exact code just emailed."""

    @classmethod
    def setUpTestData(cls):
        _ensure_google_social_app()

    def _signed_up_session(self, email="verify.me@gmail.com"):
        self.client.post(reverse("accounts:signup"), data=dict(
            first_name="Verify", last_name="Me", email=email,
            password="Password123", accept_terms="on",
        ))
        return self.client.session.get("verification_code")

    def test_correct_code_verifies_the_email_and_redirects_to_login(self):
        code = self._signed_up_session()
        response = self.client.post(reverse("accounts:verify_email_code"), data={"code": code})
        self.assertRedirects(response, reverse("accounts:login"))
        user = User.objects.get(email="verify.me@gmail.com")
        self.assertTrue(EmailAddress.objects.filter(user=user, verified=True).exists())

    def test_wrong_code_is_rejected_and_does_not_verify(self):
        self._signed_up_session(email="wrongcode@gmail.com")
        response = self.client.post(reverse("accounts:verify_email_code"), data={"code": "000000"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "invalid")
        user = User.objects.get(email="wrongcode@gmail.com")
        self.assertFalse(EmailAddress.objects.filter(user=user, verified=True).exists())

    def test_empty_code_is_rejected(self):
        self._signed_up_session(email="emptycode@gmail.com")
        response = self.client.post(reverse("accounts:verify_email_code"), data={"code": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please enter")


class LoginTests(TestCase):
    """`login_view` -- every reason a login attempt should fail, and that a genuinely
    correct, fully-verified login succeeds."""

    @classmethod
    def setUpTestData(cls):
        _ensure_google_social_app()
        cls.password = "Password123"
        cls.user = User.objects.create_user(username="logintest", email="login.test@gmail.com", password=cls.password)
        EmailAddress.objects.create(user=cls.user, email=cls.user.email, verified=True, primary=True)

    def setUp(self):
        cache.clear()  # the lockout below is stateful; start every test clean

    def _login(self, **overrides):
        data = dict(email="login.test@gmail.com", password=self.password)
        data.update(overrides)
        return self.client.post(reverse("accounts:login"), data=data)

    def test_correct_credentials_log_in_successfully(self):
        response = self._login()
        self.assertRedirects(response, reverse("gowns:homepage"))
        # Confirm the session actually carries an authenticated user on the next request.
        session_check = self.client.get(reverse("gowns:homepage"))
        self.assertTrue(session_check.wsgi_request.user.is_authenticated)

    def test_wrong_password_is_rejected(self):
        response = self._login(password="wrongpassword")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid email or password")

    def test_nonexistent_email_is_rejected(self):
        response = self._login(email="nobody.here@gmail.com")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No account found")

    def test_unverified_email_cannot_log_in(self):
        unverified = User.objects.create_user(username="unverified", email="unverified@gmail.com", password="Password123")
        EmailAddress.objects.create(user=unverified, email=unverified.email, verified=False, primary=True)
        response = self._login(email="unverified@gmail.com", password="Password123")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "verify your email")

    def test_google_only_account_cannot_password_login(self):
        google_user = User.objects.create_user(username="googleuser", email="google.user@gmail.com")
        google_user.set_unusable_password()
        google_user.save()
        EmailAddress.objects.create(user=google_user, email=google_user.email, verified=True, primary=True)
        response = self._login(email="google.user@gmail.com", password="anything123")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Google Sign In")

    def test_invalid_email_format_is_rejected(self):
        response = self._login(email="not-an-email")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "valid email")


class CustomerLoginLockoutTests(TestCase):
    """`login_view`'s brute-force protection -- mirrors the admin login's existing
    lockout exactly: 5 wrong passwords for the same email locks it out for 15
    minutes. Only real wrong-password attempts against a valid, usable, verified
    account count -- a mistyped email, a Google-only account, or an unverified
    account never reach authenticate(), so none of those consume the budget."""

    @classmethod
    def setUpTestData(cls):
        _ensure_google_social_app()
        cls.password = "Password123"
        cls.user = User.objects.create_user(username="locktest", email="lock.test@gmail.com", password=cls.password)
        EmailAddress.objects.create(user=cls.user, email=cls.user.email, verified=True, primary=True)

    def setUp(self):
        cache.clear()

    def _login(self, **overrides):
        data = dict(email="lock.test@gmail.com", password=self.password)
        data.update(overrides)
        return self.client.post(reverse("accounts:login"), data=data)

    def test_the_sixth_wrong_attempt_is_locked_out_even_with_the_wrong_password(self):
        for _ in range(5):
            self._login(password="wrongpassword")
        response = self._login(password="wrongpassword")
        self.assertContains(response, "Too many failed login attempts")

    def test_a_locked_out_email_is_rejected_even_with_the_correct_password(self):
        """The whole point of a lockout: once tripped, even the real password is
        refused until the timer runs out -- otherwise it would not stop a script
        that eventually guesses right."""
        for _ in range(5):
            self._login(password="wrongpassword")
        response = self._login(password=self.password)
        self.assertContains(response, "Too many failed login attempts")
        session_check = self.client.get(reverse("gowns:homepage"))
        self.assertTrue(session_check.wsgi_request.user.is_anonymous)

    def test_remaining_attempts_count_down_correctly(self):
        response = self._login(password="wrongpassword")
        self.assertContains(response, "4 attempts remaining")
        response = self._login(password="wrongpassword")
        self.assertContains(response, "3 attempts remaining")

    def test_a_successful_login_before_the_limit_clears_the_counter(self):
        for _ in range(3):
            self._login(password="wrongpassword")
        response = self._login(password=self.password)
        self.assertRedirects(response, reverse("gowns:homepage"))
        # the counter reset -- 3 more wrong attempts afterward must not be an instant lockout
        self.client.post(reverse("accounts:logout"))
        for _ in range(3):
            response = self._login(password="wrongpassword")
        self.assertNotContains(response, "Too many failed login attempts")

    def test_a_different_email_is_never_affected_by_someone_elses_lockout(self):
        other = User.objects.create_user(username="othertest", email="other.test@gmail.com", password="Password123")
        EmailAddress.objects.create(user=other, email=other.email, verified=True, primary=True)
        for _ in range(5):
            self._login(password="wrongpassword")
        response = self._login(email="other.test@gmail.com", password="Password123")
        self.assertRedirects(response, reverse("gowns:homepage"))

    def test_a_nonexistent_email_never_counts_toward_lockout(self):
        for _ in range(10):
            self._login(email="nobody.here@gmail.com", password="whatever")
        response = self._login(password=self.password)
        self.assertRedirects(response, reverse("gowns:homepage"))

    def test_a_google_only_account_never_counts_toward_lockout(self):
        google_user = User.objects.create_user(username="lockgoogle", email="lock.google@gmail.com")
        google_user.set_unusable_password()
        google_user.save()
        EmailAddress.objects.create(user=google_user, email=google_user.email, verified=True, primary=True)
        for _ in range(10):
            self._login(email="lock.google@gmail.com", password="anything123")
        response = self._login(password=self.password)
        self.assertRedirects(response, reverse("gowns:homepage"))

    def test_an_unverified_account_never_counts_toward_lockout(self):
        unverified = User.objects.create_user(username="lockunverified", email="lock.unverified@gmail.com", password="Password123")
        EmailAddress.objects.create(user=unverified, email=unverified.email, verified=False, primary=True)
        for _ in range(10):
            self._login(email="lock.unverified@gmail.com", password="Password123")
        response = self._login(password=self.password)
        self.assertRedirects(response, reverse("gowns:homepage"))


class LogoutTests(TestCase):
    def test_logout_clears_the_session(self):
        user = User.objects.create_user(username="logouttest", email="logout.test@gmail.com", password="Password123")
        self.client.force_login(user)
        self.client.post(reverse("accounts:logout"))
        response = self.client.get(reverse("gowns:homepage"))
        self.assertTrue(response.wsgi_request.user.is_anonymous)


class CancellationAutoFlagTests(TestCase):
    """`accounts/services.py` -- the auto-flag policy the AI chatbot's own grounding
    prompt describes to customers: 15 cancelled/abandoned attempts flags an account
    for staff review, automatically, with no way for the customer to self-clear it."""

    def setUp(self):
        self.user = User.objects.create_user(username="flag_policy_test", password="x")

    def test_only_cancelled_reservations_count_not_rejected(self):
        from accounts.services import count_cancellations
        from reservations.models import Reservation
        Reservation.objects.create(customer=self.user, customer_name="A", status=Reservation.Status.CANCELLED)
        Reservation.objects.create(customer=self.user, customer_name="B", status=Reservation.Status.REJECTED)
        Reservation.objects.create(customer=self.user, customer_name="C", status=Reservation.Status.PENDING)
        self.assertEqual(count_cancellations(self.user), 1)

    def test_attempt_count_combines_cancellations_and_abandoned_holds(self):
        from accounts.services import count_cancellation_attempts
        from reservations.models import Reservation
        Reservation.objects.create(customer=self.user, customer_name="A", status=Reservation.Status.CANCELLED)
        Reservation.objects.create(customer=self.user, customer_name="B", status=Reservation.Status.CANCELLED)
        UserProfile.objects.create(user=self.user, hold_abandon_count=3)
        self.assertEqual(count_cancellation_attempts(self.user), 5)

    def test_below_every_threshold_does_not_flag_or_lock_the_account(self):
        # Below even the first (5-attempt) tier, sync_cancellation_flag must not
        # touch UserProfile at all -- so a real, pre-existing profile (as a
        # signed-up customer already has) is what this test needs, not one
        # created by the function under test.
        from accounts.services import sync_cancellation_flag
        from reservations.models import Reservation
        UserProfile.objects.create(user=self.user)
        for _ in range(4):
            Reservation.objects.create(customer=self.user, customer_name="X", status=Reservation.Status.CANCELLED)
        sync_cancellation_flag(self.user)
        profile = UserProfile.objects.get(user=self.user)
        self.assertFalse(profile.is_flagged)
        self.assertIsNone(profile.cancel_lockout_until)

    def test_five_attempts_triggers_a_30_minute_lockout_and_sends_a_message(self):
        from accounts.services import (
            sync_cancellation_flag,
            CANCELLATION_LOCKOUT_TIER_1,
            CANCELLATION_LOCKOUT_TIER_1_MINUTES,
        )
        from accounts.models import CustomerMessage
        from reservations.models import Reservation
        for _ in range(CANCELLATION_LOCKOUT_TIER_1):
            Reservation.objects.create(customer=self.user, customer_name="X", status=Reservation.Status.CANCELLED)
        count = sync_cancellation_flag(self.user)
        self.assertEqual(count, CANCELLATION_LOCKOUT_TIER_1)
        profile = UserProfile.objects.get(user=self.user)
        self.assertFalse(profile.is_flagged)
        self.assertIsNotNone(profile.cancel_lockout_until)
        remaining_minutes = (profile.cancel_lockout_until - timezone.now()).total_seconds() / 60
        self.assertAlmostEqual(remaining_minutes, CANCELLATION_LOCKOUT_TIER_1_MINUTES, delta=1)
        self.assertTrue(
            CustomerMessage.objects.filter(recipient=self.user, category=CustomerMessage.Category.CANCELLATION_LOCKOUT).exists()
        )

    def test_ten_attempts_triggers_a_2_hour_lockout(self):
        from accounts.services import (
            sync_cancellation_flag,
            CANCELLATION_LOCKOUT_TIER_2,
            CANCELLATION_LOCKOUT_TIER_2_HOURS,
        )
        from reservations.models import Reservation
        for _ in range(CANCELLATION_LOCKOUT_TIER_2):
            Reservation.objects.create(customer=self.user, customer_name="X", status=Reservation.Status.CANCELLED)
        sync_cancellation_flag(self.user)
        profile = UserProfile.objects.get(user=self.user)
        self.assertFalse(profile.is_flagged)
        remaining_hours = (profile.cancel_lockout_until - timezone.now()).total_seconds() / 3600
        self.assertAlmostEqual(remaining_hours, CANCELLATION_LOCKOUT_TIER_2_HOURS, delta=0.02)

    def test_each_lockout_tier_only_fires_once(self):
        # Cancelling a 6th, 7th, 8th... time must not keep re-extending the
        # 30-minute lockout that already fired at 5 -- otherwise an account
        # could never climb past tier 1 towards the 10/15 checkpoints.
        from accounts.services import sync_cancellation_flag, CANCELLATION_LOCKOUT_TIER_1
        from accounts.models import CustomerMessage
        from reservations.models import Reservation
        for _ in range(CANCELLATION_LOCKOUT_TIER_1):
            Reservation.objects.create(customer=self.user, customer_name="X", status=Reservation.Status.CANCELLED)
        sync_cancellation_flag(self.user)
        first_deadline = UserProfile.objects.get(user=self.user).cancel_lockout_until

        Reservation.objects.create(customer=self.user, customer_name="X", status=Reservation.Status.CANCELLED)
        sync_cancellation_flag(self.user)
        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual(profile.cancel_lockout_until, first_deadline)
        self.assertEqual(
            CustomerMessage.objects.filter(recipient=self.user, category=CustomerMessage.Category.CANCELLATION_LOCKOUT).count(), 1
        )

    def test_reaching_the_threshold_flags_the_account_and_sends_a_message(self):
        from accounts.services import sync_cancellation_flag, CANCELLATION_FLAG_THRESHOLD
        from accounts.models import CustomerMessage
        from reservations.models import Reservation
        for _ in range(CANCELLATION_FLAG_THRESHOLD):
            Reservation.objects.create(customer=self.user, customer_name="X", status=Reservation.Status.CANCELLED)
        count = sync_cancellation_flag(self.user)
        self.assertEqual(count, CANCELLATION_FLAG_THRESHOLD)
        self.assertTrue(UserProfile.objects.get(user=self.user).is_flagged)
        self.assertTrue(
            CustomerMessage.objects.filter(recipient=self.user, category=CustomerMessage.Category.ACCOUNT_FLAGGED).exists()
        )

    def test_already_flagged_account_is_not_re_flagged_or_re_messaged(self):
        from accounts.services import sync_cancellation_flag, CANCELLATION_FLAG_THRESHOLD
        from accounts.models import CustomerMessage
        from reservations.models import Reservation
        UserProfile.objects.create(user=self.user, is_flagged=True)
        for _ in range(CANCELLATION_FLAG_THRESHOLD):
            Reservation.objects.create(customer=self.user, customer_name="X", status=Reservation.Status.CANCELLED)
        sync_cancellation_flag(self.user)
        self.assertEqual(
            CustomerMessage.objects.filter(recipient=self.user, category=CustomerMessage.Category.ACCOUNT_FLAGGED).count(), 0
        )

    def test_record_abandoned_hold_increments_the_counter_and_rechecks_the_flag(self):
        from accounts.services import record_abandoned_hold
        UserProfile.objects.create(user=self.user, hold_abandon_count=0)
        record_abandoned_hold(self.user)
        self.assertEqual(UserProfile.objects.get(user=self.user).hold_abandon_count, 1)

    def test_record_abandoned_hold_can_trip_the_flag_on_its_own(self):
        from accounts.services import record_abandoned_hold, CANCELLATION_FLAG_THRESHOLD
        UserProfile.objects.create(user=self.user, hold_abandon_count=CANCELLATION_FLAG_THRESHOLD - 1)
        record_abandoned_hold(self.user)
        self.assertTrue(UserProfile.objects.get(user=self.user).is_flagged)
