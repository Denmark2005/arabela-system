from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialLogin

from accounts.adapters import ArabelaSocialAccountAdapter
from accounts.models import UserProfile

User = get_user_model()


def _ensure_google_social_app():
    """login.html renders a "Continue with Google" link via allauth's
    {% provider_login_url %}, which looks up a SocialApp row for the current Site.
    The real dev database has one added manually (outside any migration); a fresh
    test database starts with none, so every page render would otherwise fail with
    SocialApp.DoesNotExist. The actual client id/secret are irrelevant here -- no
    test in this file completes a real Google OAuth round trip, the page just needs
    the row to exist to render the link at all."""
    site = Site.objects.get_current()
    app, _ = SocialApp.objects.get_or_create(
        provider="google", name="Google",
        defaults={"client_id": "test-client-id", "secret": "test-secret"},
    )
    app.sites.add(site)
    return app


class LoginPageTests(TestCase):
    """`login_view` -- the single Google-only entry point shared by accounts:login
    and accounts:signup, now that manual password signup/OTP verification are gone."""

    @classmethod
    def setUpTestData(cls):
        _ensure_google_social_app()

    def test_login_page_renders_the_google_button(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Continue with Google")

    def test_signup_url_renders_the_same_page(self):
        response = self.client.get(reverse("accounts:signup"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Continue with Google")

    def test_an_already_authenticated_visitor_is_redirected_away(self):
        user = User.objects.create_user(username="alreadyin", email="already.in@gmail.com")
        self.client.force_login(user)
        response = self.client.get(reverse("accounts:login"))
        self.assertRedirects(response, reverse("gowns:homepage"))

    def test_next_is_threaded_into_the_google_login_link(self):
        response = self.client.get(reverse("accounts:login"), {"next": "/gowns/orders/"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "next=%2Fgowns%2Forders%2F")

    def test_an_unsafe_next_url_is_dropped(self):
        response = self.client.get(reverse("accounts:login"), {"next": "https://evil.example.com/"})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "evil.example.com")


class GoogleAccountLinkingTests(TestCase):
    """The custom social adapter (accounts/adapters.py) -- now that manual signup
    is gone, this is the only thing standing between an old account created under
    it and a permanently orphaned duplicate. A customer who signed up manually
    before this change (verified or not -- some never finished the OTP step
    that no longer exists) must land back in that SAME account when they use
    "Continue with Google" with the same address, never a second one."""

    @classmethod
    def setUpTestData(cls):
        # sociallogin.connect() resolves the "google" SocialApp for the current
        # Site as part of actually saving the link -- not just for rendering the
        # button like other tests in this file, this one exercises the real save.
        _ensure_google_social_app()

    def _google_login_for(self, email):
        """Builds the same SocialLogin shape allauth constructs mid-OAuth-callback
        for a Google account that has never signed in here before -- an unsaved
        User (pk=None keeps `is_existing` False, exactly like a real first-time
        social login) plus an unsaved SocialAccount for that provider."""
        return SocialLogin(
            user=User(email=email),
            account=SocialAccount(provider="google", uid="1234567890"),
        )

    def test_links_to_an_existing_unverified_account_instead_of_duplicating_it(self):
        existing = User.objects.create_user(
            username="oldmanual", email="old.manual@gmail.com", password="Whatever123",
        )
        EmailAddress.objects.create(user=existing, email="old.manual@gmail.com", verified=False, primary=True)

        sociallogin = self._google_login_for("old.manual@gmail.com")
        self.assertFalse(sociallogin.is_existing)

        request = self.client.get(reverse("gowns:homepage")).wsgi_request
        ArabelaSocialAccountAdapter().pre_social_login(request, sociallogin)

        self.assertEqual(sociallogin.user.pk, existing.pk)
        self.assertEqual(User.objects.filter(email__iexact="old.manual@gmail.com").count(), 1)
        self.assertTrue(SocialAccount.objects.filter(user=existing, provider="google").exists())

    def test_links_to_an_existing_verified_account_too(self):
        existing = User.objects.create_user(
            username="oldverified", email="old.verified@gmail.com", password="Whatever123",
        )
        EmailAddress.objects.create(user=existing, email="old.verified@gmail.com", verified=True, primary=True)

        sociallogin = self._google_login_for("old.verified@gmail.com")
        request = self.client.get(reverse("gowns:homepage")).wsgi_request
        ArabelaSocialAccountAdapter().pre_social_login(request, sociallogin)

        self.assertEqual(sociallogin.user.pk, existing.pk)
        self.assertEqual(User.objects.filter(email__iexact="old.verified@gmail.com").count(), 1)

    def test_a_brand_new_email_is_left_alone_for_normal_signup(self):
        sociallogin = self._google_login_for("nobody.yet@gmail.com")
        request = self.client.get(reverse("gowns:homepage")).wsgi_request
        ArabelaSocialAccountAdapter().pre_social_login(request, sociallogin)

        # Untouched -- still the original unsaved instance, no existing account found to connect to.
        self.assertIsNone(sociallogin.user.pk)
        self.assertEqual(User.objects.filter(email__iexact="nobody.yet@gmail.com").count(), 0)


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

    def test_a_lock_and_a_flag_only_ever_touch_that_one_account(self):
        # Reported as "when one customer is locked, every account is locked". It isn't: every write is
        # keyed to the one account that crossed the line. Kept as a guard so it can never become true.
        from accounts.models import CustomerMessage
        from accounts.services import (
            CANCELLATION_FLAG_THRESHOLD, CANCELLATION_LOCKOUT_TIER_1, cancel_lockout_notice, record_abandoned_hold,
        )
        bystander = User.objects.create_user(username="flag_policy_bystander", password="x")
        UserProfile.objects.create(user=bystander)
        UserProfile.objects.create(user=self.user, hold_abandon_count=CANCELLATION_LOCKOUT_TIER_1 - 1)
        record_abandoned_hold(self.user)
        self.assertGreater(cancel_lockout_notice(self.user)[0], 0)
        UserProfile.objects.filter(user=self.user).update(hold_abandon_count=CANCELLATION_FLAG_THRESHOLD - 1)
        record_abandoned_hold(self.user)
        self.assertTrue(UserProfile.objects.get(user=self.user).is_flagged)

        untouched = UserProfile.objects.get(user=bystander)
        self.assertEqual((untouched.is_flagged, untouched.cancel_lockout_until, untouched.hold_abandon_count), (False, None, 0))
        self.assertFalse(untouched.cancel_tier1_lockout_sent)
        self.assertEqual(cancel_lockout_notice(bystander), (0, ""))
        self.assertFalse(CustomerMessage.objects.filter(recipient=bystander).exists())


class CancelLockoutNoticeTests(TestCase):
    """`cancel_lockout_notice` -- the one wording Proceed, the checkout page and Confirm Rental all show a
    locked customer: why, for how long, and until when (it used to say only "try again in N minutes")."""

    def setUp(self):
        self.user = User.objects.create_user(username="lockout_notice_test", password="x")

    def _lock(self, attempts):
        from accounts.services import sync_cancellation_flag
        UserProfile.objects.update_or_create(user=self.user, defaults={"hold_abandon_count": attempts})
        sync_cancellation_flag(self.user)

    def test_no_lock_means_no_message(self):
        from accounts.services import cancel_lockout_notice
        self.assertEqual(cancel_lockout_notice(self.user), (0, ""))  # no profile at all
        UserProfile.objects.create(user=self.user)
        self.assertEqual(cancel_lockout_notice(self.user), (0, ""))

    def test_the_30_minute_lock_says_why_how_long_and_until_when(self):
        from accounts.services import cancel_lockout_notice
        self._lock(5)
        seconds, message = cancel_lockout_notice(self.user)
        self.assertTrue(1790 <= seconds <= 1800, seconds)
        self.assertIn("You've reached 5 cancelled or abandoned reservations", message)
        self.assertIn("paused for 30 minutes", message)
        self.assertRegex(message, r"until \d{1,2}:\d{2} [AP]M \(about 30 minutes left\)")
        self.assertIn("You can still browse the gowns", message)

    def test_the_2_hour_lock_says_2_hours(self):
        from accounts.services import cancel_lockout_notice
        self._lock(10)
        message = cancel_lockout_notice(self.user)[1]
        self.assertIn("You've reached 10 cancelled or abandoned reservations", message)
        self.assertIn("paused for 2 hours", message)
        self.assertIn("(about 2 hours left)", message)

    def test_a_lock_ending_on_another_day_names_the_day(self):
        from datetime import timedelta
        from accounts.services import cancel_lockout_notice
        self._lock(5)
        UserProfile.objects.filter(user=self.user).update(cancel_lockout_until=timezone.now() + timedelta(hours=25))
        self.assertRegex(cancel_lockout_notice(self.user)[1], r"until [A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} [AP]M")

    def test_a_lock_that_has_run_out_is_gone(self):
        from datetime import timedelta
        from accounts.services import cancel_lockout_notice
        self._lock(5)
        UserProfile.objects.filter(user=self.user).update(cancel_lockout_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(cancel_lockout_notice(self.user), (0, ""))
