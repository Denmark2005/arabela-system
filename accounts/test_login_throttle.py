"""The admin sign-in lockout: 5 wrong attempts for a username lock it for 15 minutes -- and the count is kept in the
database, so a restart of the app (which wipes its memory) can no longer make it "reset"."""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import DatabaseError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts import login_throttle
from accounts.models import LoginThrottle

User = get_user_model()
LOGIN = reverse("arabela_admin:admin_login")


class LoginLockoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User.objects.create_user(username="lock_user", password="the-right-password-1", is_staff=True)

    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        patcher = patch("accounts.login_throttle._now", side_effect=lambda: self.now)
        self.addCleanup(patcher.stop)
        patcher.start()

    def attempt(self, username="lock_user", password="wrong-password"):
        return self.client.post(LOGIN, {"username": username, "password": password})

    def says(self, response, text):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, text)

    # ---- the bug: the count must survive the app's memory being wiped ----------------------------------------------------
    def test_the_count_survives_a_restart_and_a_page_refresh(self):
        self.says(self.attempt(), "4 attempts remaining")
        self.says(self.attempt(), "3 attempts remaining")
        cache.clear()                                   # what a restart of the app does to its memory
        self.client.get(LOGIN)                          # ...and the person refreshes the page
        self.says(self.attempt(), "2 attempts remaining")   # NOT "4": nothing was forgotten

    def test_five_wrong_attempts_lock_even_if_the_memory_is_wiped_between_every_one(self):
        for _ in range(5):
            self.attempt()
            cache.clear()
        self.says(self.attempt("lock_user", "the-right-password-1"), "Too many failed login attempts")   # even the right password

    def test_the_count_is_a_row_in_the_database(self):
        self.attempt()
        self.attempt()
        self.assertEqual(LoginThrottle.objects.get(key="user:lock_user").failures, 2)

    # ---- the rule itself, unchanged -------------------------------------------------------------------------------------------
    def test_the_warnings_count_down_and_the_fifth_attempt_locks(self):
        for left in (4, 3, 2):
            self.says(self.attempt(), f"{left} attempts remaining")
        self.says(self.attempt(), "1 attempt remaining")
        self.says(self.attempt(), "Too many failed login attempts. Please try again in 15 minutes.")

    def test_a_lockout_lasts_fifteen_minutes_and_says_how_long_is_left(self):
        for _ in range(5):
            self.attempt()
        self.says(self.attempt("lock_user", "the-right-password-1"), "try again in 15 minutes")
        self.now += timedelta(minutes=10)
        self.says(self.attempt("lock_user", "the-right-password-1"), "try again in 5 minutes")
        self.now += timedelta(minutes=4, seconds=30)
        self.says(self.attempt("lock_user", "the-right-password-1"), "try again in 1 minute.")
        self.now += timedelta(seconds=31)               # 15 minutes after the fifth wrong attempt
        self.assertEqual(self.attempt("lock_user", "the-right-password-1").status_code, 302)

    def test_attempts_made_while_locked_do_not_extend_the_lockout(self):
        for _ in range(5):
            self.attempt()
        self.now += timedelta(minutes=14)
        self.attempt()                                  # a wrong try while locked
        self.now += timedelta(minutes=1, seconds=1)
        self.assertEqual(self.attempt("lock_user", "the-right-password-1").status_code, 302)

    def test_old_wrong_attempts_are_forgotten_after_fifteen_minutes(self):
        for _ in range(3):
            self.attempt()
        self.now += timedelta(minutes=16)
        self.says(self.attempt(), "4 attempts remaining")

    def test_a_correct_sign_in_clears_the_count(self):
        for _ in range(3):
            self.attempt()
        self.assertEqual(self.attempt("lock_user", "the-right-password-1").status_code, 302)
        self.assertFalse(LoginThrottle.objects.filter(key="user:lock_user").exists())
        self.client.logout()
        self.says(self.attempt(), "4 attempts remaining")

    def test_each_username_is_counted_on_its_own_ignoring_capitals(self):
        User.objects.create_user(username="other_user", password="another-password-1", is_staff=True)
        for _ in range(5):
            self.attempt("LOCK_user")                   # capitals do not matter...
        self.says(self.attempt("lock_user", "the-right-password-1"), "Too many failed login attempts")
        self.assertEqual(self.attempt("other_user", "another-password-1").status_code, 302)   # ...but other people are not affected

    def test_a_username_that_does_not_exist_is_counted_and_locked_too(self):
        for _ in range(5):
            self.attempt("nobody_here")
        self.says(self.attempt("nobody_here"), "Too many failed login attempts")

    # ---- safety nets -------------------------------------------------------------------------------------------------------------
    def test_before_the_database_update_the_same_rule_runs_on_the_cache_and_sign_in_never_breaks(self):
        broken = MagicMock()
        broken.objects.filter.side_effect = DatabaseError("relation does not exist")
        broken.objects.select_for_update.side_effect = DatabaseError("relation does not exist")
        with patch("accounts.login_throttle.LoginThrottle", broken):
            self.says(self.attempt(), "4 attempts remaining")
            for _ in range(4):
                self.attempt()
            self.says(self.attempt("lock_user", "the-right-password-1"), "Too many failed login attempts")
            self.now += timedelta(minutes=16)
            self.assertEqual(self.attempt("lock_user", "the-right-password-1").status_code, 302)

    def test_rows_older_than_a_day_are_tidied_away(self):
        old = LoginThrottle.objects.create(key="user:ancient", failures=5, last_failure_at=self.now - timedelta(days=2))
        recent = LoginThrottle.objects.create(key="user:recent", failures=2, last_failure_at=self.now - timedelta(hours=1))
        with patch("accounts.login_throttle.random.random", return_value=0.0):
            login_throttle.record_failure("user:someone")
        self.assertFalse(LoginThrottle.objects.filter(pk=old.pk).exists())
        self.assertTrue(LoginThrottle.objects.filter(pk=recent.pk).exists())

    def test_a_huge_made_up_username_cannot_break_the_table(self):
        self.says(self.attempt("x" * 5000), "4 attempts remaining")
        self.assertLessEqual(len(LoginThrottle.objects.get().key), 120)
