"""Rate limits: scripts that hammer reservation submit, the checkout hold, cart saving or search get a polite "slow down", while a
normal customer never meets a limit. Counts live in the database, so an app restart cannot reset them."""
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import DatabaseError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts import rate_limit
from accounts.models import LoginThrottle

User = get_user_model()


class LimiterTests(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        patcher = patch("accounts.rate_limit._now", side_effect=lambda: self.now)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_it_counts_up_inside_the_window_and_starts_again_after_it(self):
        self.assertEqual([rate_limit.hit("t", 60) for _ in range(3)], [1, 2, 3])
        self.now += timedelta(seconds=61)
        self.assertEqual(rate_limit.hit("t", 60), 1)

    def test_the_window_starts_at_the_first_action_and_later_ones_do_not_stretch_it(self):
        rate_limit.hit("t", 60)
        self.now += timedelta(seconds=50)
        rate_limit.hit("t", 60)
        self.now += timedelta(seconds=11)          # 61s after the FIRST action
        self.assertEqual(rate_limit.hit("t", 60), 1)

    def test_allowed_is_true_up_to_the_limit_then_false(self):
        self.assertEqual([rate_limit.allowed("t", 2, 60) for _ in range(3)], [True, True, False])

    def test_different_names_are_counted_separately(self):
        rate_limit.hit("a", 60)
        rate_limit.hit("a", 60)
        self.assertEqual(rate_limit.hit("b", 60), 1)

    def test_the_count_is_in_the_database_and_survives_a_restart(self):
        rate_limit.hit("t", 60)
        cache.clear()                              # what a restart does to the app's memory
        self.assertEqual(rate_limit.hit("t", 60), 2)
        self.assertEqual(LoginThrottle.objects.get(key="rl:t").failures, 2)

    def test_it_never_touches_the_admin_sign_in_lockout(self):
        LoginThrottle.objects.create(key="user:admin", failures=3, last_failure_at=self.now)
        rate_limit.hit("admin", 60)
        self.assertEqual(LoginThrottle.objects.get(key="user:admin").failures, 3)

    def test_if_the_table_cannot_be_reached_the_same_rule_runs_on_the_cache(self):
        broken = MagicMock()
        broken.objects.select_for_update.side_effect = DatabaseError("no table")
        broken.objects.filter.side_effect = DatabaseError("no table")
        with patch("accounts.rate_limit.LoginThrottle", broken):
            self.assertEqual([rate_limit.allowed("t", 2, 60) for _ in range(3)], [True, True, False])

    def test_old_rows_are_tidied_but_live_ones_and_lockouts_are_kept(self):
        old = LoginThrottle.objects.create(key="rl:old", failures=9, last_failure_at=self.now - timedelta(days=2))
        live = LoginThrottle.objects.create(key="rl:live", failures=1, last_failure_at=self.now - timedelta(hours=23))
        lock = LoginThrottle.objects.create(key="user:x", failures=5, last_failure_at=self.now - timedelta(days=2))
        with patch("accounts.rate_limit.random.random", return_value=0.0):
            rate_limit.hit("someone", 60)
        self.assertFalse(LoginThrottle.objects.filter(pk=old.pk).exists())
        self.assertTrue(LoginThrottle.objects.filter(pk=live.pk).exists())
        self.assertTrue(LoginThrottle.objects.filter(pk=lock.pk).exists())     # the sign-in lockout keeps its own tidying

    def test_a_huge_name_cannot_break_the_table(self):
        rate_limit.hit("x" * 5000, 60)
        self.assertLessEqual(len(LoginThrottle.objects.get(key__startswith="rl:").key), 120)

    def test_the_visitor_ip_is_the_last_forwarded_entry_so_it_cannot_be_faked(self):
        factory = RequestFactory()
        self.assertEqual(rate_limit.client_ip(factory.get("/", HTTP_X_FORWARDED_FOR="1.1.1.1, 203.0.113.9")), "203.0.113.9")
        self.assertEqual(rate_limit.client_ip(factory.get("/", REMOTE_ADDR="10.0.0.2")), "10.0.0.2")


class EndpointLimitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="rl_customer", password="x" * 12)
        cls.other = User.objects.create_user(username="rl_other", password="x" * 12)

    def setUp(self):
        cache.clear()
        self.client.force_login(self.customer)

    def slowed(self, response):
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"], rate_limit.SLOW_DOWN)
        self.assertTrue(response.json()["rate_limited"])

    # ---- reservation submit ------------------------------------------------------------------------------------------------
    def submit(self):
        return self.client.post(reverse("gowns:reservation_submit"), {"items": "[]"})   # empty bag: a quick, harmless answer

    @patch("gowns.views._SUBMIT_LIMIT", (3, 600))
    def test_reservation_submit_slows_down_a_script_but_not_a_customer_or_anyone_else(self):
        for _ in range(3):
            self.assertEqual(self.submit().status_code, 400)          # normal answers ("your bag is empty")
        self.slowed(self.submit())
        self.client.force_login(self.other)                           # a different customer is not affected
        self.assertEqual(self.submit().status_code, 400)

    def test_the_real_submit_limit_is_far_above_normal_use(self):
        from gowns import views
        self.assertGreaterEqual(views._SUBMIT_LIMIT[0], 10)
        self.assertEqual(self.submit().status_code, 400)              # one try is just a normal answer

    # ---- checkout hold ----------------------------------------------------------------------------------------------------
    @patch("gowns.views._HOLD_LIMIT", (2, 600))
    def test_starting_the_checkout_hold_is_limited_and_says_success_false(self):
        url = reverse("gowns:reservation_hold_start")
        for _ in range(2):
            self.assertTrue(self.client.post(url, "{}", content_type="application/json").json()["success"])
        response = self.client.post(url, "{}", content_type="application/json")
        self.slowed(response)
        self.assertFalse(response.json()["success"])                 # the page shows "Please wait before trying again"

    # ---- cart save --------------------------------------------------------------------------------------------------------
    @patch("gowns.views._CART_SAVE_LIMIT", (2, 60))
    def test_cart_saving_is_limited(self):
        url = reverse("gowns:cart_save")
        body = json.dumps({"cart": []})
        for _ in range(2):
            self.assertEqual(self.client.post(url, body, content_type="application/json").status_code, 200)
        self.slowed(self.client.post(url, body, content_type="application/json"))

    # ---- search ------------------------------------------------------------------------------------------------------------
    @patch("gowns.views._SEARCH_LIMIT", (2, 60))
    def test_search_is_limited_per_connection_with_a_friendly_page(self):
        self.client.logout()
        for _ in range(2):
            self.assertEqual(self.client.get("/search/", {"q": "we"}, HTTP_X_FORWARDED_FOR="203.0.113.5").status_code, 200)
        response = self.client.get("/search/", {"q": "we"}, HTTP_X_FORWARDED_FOR="203.0.113.5")
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "searching very quickly", status_code=429)
        self.assertContains(response, "browse all gowns", status_code=429)
        self.assertNotContains(response, "No gowns match", status_code=429)
        other = self.client.get("/search/", {"q": "we"}, HTTP_X_FORWARDED_FOR="198.51.100.8")
        self.assertEqual(other.status_code, 200)                      # another visitor searches normally

    def test_an_empty_search_still_just_goes_to_the_all_page_without_counting(self):
        self.client.get("/search/", {"q": ""})
        self.assertEqual(rate_limit.count("search:127.0.0.1"), 0)
