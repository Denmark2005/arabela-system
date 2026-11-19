"""The four limits in front of the Arabela Recommends chat (2026-11-19): per internet address (per minute and per day), per signed-in customer (per
day) and for the whole shop (per day). The chat shares ONE Gemini quota with the receipt check, so a flood of questions must never use up the part of the
quota the receipts need -- and a blocked message never reaches Gemini (so it costs no quota)."""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import rate_limit
from accounts.models import LoginThrottle
from ai_recommendation import views
from ai_recommendation.tests import _candidate_reply, _mock_response

User = get_user_model()
OK = _mock_response(200, _candidate_reply("Here is a gown."))


@override_settings(GEMINI_API_KEY="test-key")
class ChatLimitsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse("ai_recommendation:chat")
        self.addCleanup(cache.clear)

    def ask(self, ip="203.0.113.10", client=None, message="Hi"):
        return (client or self.client).post(self.url, data=json.dumps({"message": message}), content_type="application/json", HTTP_X_FORWARDED_FOR=ip)

    def fill(self, name, failures):
        LoginThrottle.objects.update_or_create(key=f"rl:{name}", defaults={"failures": failures, "last_failure_at": timezone.now()})

    # ---- the numbers ----
    def test_the_numbers_are_the_ones_agreed(self):
        self.assertEqual(views.AI_CHAT_RATE_LIMIT_PER_MINUTE, 5)
        self.assertEqual(views.AI_CHAT_DAILY_LIMIT, 20)
        self.assertEqual(views.AI_CHAT_ACCOUNT_DAILY_LIMIT, 15)
        self.assertEqual(views.AI_CHAT_SHOP_DAILY_LIMIT, 250)

    # ---- per internet address ----
    def test_five_questions_a_minute_are_answered_and_the_sixth_is_asked_to_wait(self):
        with patch.object(views.requests, "post", return_value=OK) as post:
            for n in range(5):
                self.assertEqual(self.ask().status_code, 200, n)
            sixth = self.ask()
        self.assertEqual((sixth.status_code, sixth.json()["error"]), (429, "rate_limited_burst"))
        self.assertEqual(post.call_count, 5)                                        # the sixth never reached Gemini

    def test_twenty_questions_a_day_from_one_address_then_the_daily_message(self):
        self.fill("ai_chat_rl_day:203.0.113.20", views.AI_CHAT_DAILY_LIMIT)
        with patch.object(views.requests, "post", return_value=OK) as post:
            response = self.ask(ip="203.0.113.20")
        self.assertEqual((response.status_code, response.json()["error"]), (429, "rate_limited_daily"))
        self.assertEqual(response.json()["reply"], views.DAILY_LIMIT_REPLY)
        post.assert_not_called()

    def test_another_address_is_not_affected(self):
        self.fill("ai_chat_rl_day:203.0.113.20", views.AI_CHAT_DAILY_LIMIT)
        with patch.object(views.requests, "post", return_value=OK):
            self.assertEqual(self.ask(ip="203.0.113.21").status_code, 200)

    # ---- per signed-in customer ----
    def test_a_customer_may_ask_fifteen_questions_a_day_whatever_address_they_use(self):
        user = User.objects.create_user("chat_customer", password="pw-12345-abc")
        self.client.force_login(user)
        self.fill(f"ai_chat_rl_acct:{user.pk}", views.AI_CHAT_ACCOUNT_DAILY_LIMIT)
        with patch.object(views.requests, "post", return_value=OK) as post:
            response = self.ask(ip="203.0.113.30")                                  # a fresh address: only the account limit can stop it
        self.assertEqual((response.status_code, response.json()["error"]), (429, "rate_limited_account"))
        self.assertEqual(response.json()["reply"], views.ACCOUNT_LIMIT_REPLY)
        self.assertIn("your account", views.ACCOUNT_LIMIT_REPLY)
        post.assert_not_called()

    def test_each_question_of_a_signed_in_customer_counts_on_the_account(self):
        user = User.objects.create_user("chat_customer2", password="pw-12345-abc")
        self.client.force_login(user)
        with patch.object(views.requests, "post", return_value=OK):
            for _ in range(3):
                self.assertEqual(self.ask(ip="203.0.113.31").status_code, 200)
        self.assertEqual(rate_limit.count(f"ai_chat_rl_acct:{user.pk}"), 3)

    def test_one_customers_limit_never_blocks_another(self):
        first = User.objects.create_user("chat_a", password="pw-12345-abc")
        second = User.objects.create_user("chat_b", password="pw-12345-abc")
        self.fill(f"ai_chat_rl_acct:{first.pk}", views.AI_CHAT_ACCOUNT_DAILY_LIMIT)
        self.client.force_login(second)
        with patch.object(views.requests, "post", return_value=OK):
            self.assertEqual(self.ask(ip="203.0.113.32").status_code, 200)

    def test_a_visitor_who_is_not_signed_in_has_no_account_count(self):
        with patch.object(views.requests, "post", return_value=OK):
            self.assertEqual(self.ask(ip="203.0.113.33").status_code, 200)
        self.assertFalse(LoginThrottle.objects.filter(key__startswith="rl:ai_chat_rl_acct:").exists())

    # ---- for the whole shop ----
    def test_after_250_questions_in_a_day_the_chat_rests_for_everybody(self):
        self.fill("ai_chat_rl_shop", views.AI_CHAT_SHOP_DAILY_LIMIT)
        with patch.object(views.requests, "post", return_value=OK) as post:
            response = self.ask(ip="203.0.113.40")
        self.assertEqual((response.status_code, response.json()["error"]), (429, "rate_limited_shop"))
        self.assertEqual(response.json()["reply"], views.SHOP_LIMIT_REPLY)
        self.assertIn("resting until tomorrow", views.SHOP_LIMIT_REPLY)
        post.assert_not_called()

    def test_the_250th_question_is_answered_and_the_251st_is_not(self):
        self.fill("ai_chat_rl_shop", views.AI_CHAT_SHOP_DAILY_LIMIT - 1)
        with patch.object(views.requests, "post", return_value=OK) as post:
            self.assertEqual(self.ask(ip="203.0.113.41").status_code, 200)
            self.assertEqual(self.ask(ip="203.0.113.42").json()["error"], "rate_limited_shop")
        self.assertEqual(post.call_count, 1)

    def test_every_answered_question_counts_toward_the_shops_total(self):
        with patch.object(views.requests, "post", return_value=OK):
            for n in range(4):
                self.ask(ip=f"203.0.113.{50 + n}")
        self.assertEqual(rate_limit.count("ai_chat_rl_shop"), 4)

    def test_a_blocked_visitor_never_eats_the_shops_allowance(self):
        # a visitor blocked by the per-address limits must not count toward the shop's total
        self.fill("ai_chat_rl_day:203.0.113.60", views.AI_CHAT_DAILY_LIMIT)
        with patch.object(views.requests, "post", return_value=OK):
            for _ in range(3):
                self.ask(ip="203.0.113.60")
        self.assertEqual(rate_limit.count("ai_chat_rl_shop"), 0)

    def test_a_customer_blocked_by_their_account_limit_does_not_eat_the_shops_allowance_either(self):
        user = User.objects.create_user("chat_blocked", password="pw-12345-abc")
        self.client.force_login(user)
        self.fill(f"ai_chat_rl_acct:{user.pk}", views.AI_CHAT_ACCOUNT_DAILY_LIMIT)
        with patch.object(views.requests, "post", return_value=OK):
            self.ask(ip="203.0.113.61")
        self.assertEqual(rate_limit.count("ai_chat_rl_shop"), 0)

    def test_the_counts_start_again_when_the_day_has_passed(self):
        LoginThrottle.objects.create(key="rl:ai_chat_rl_shop", failures=views.AI_CHAT_SHOP_DAILY_LIMIT + 5,
                                     last_failure_at=timezone.now() - timezone.timedelta(days=1, minutes=1))
        with patch.object(views.requests, "post", return_value=OK):
            self.assertEqual(self.ask(ip="203.0.113.62").status_code, 200)
        self.assertEqual(rate_limit.count("ai_chat_rl_shop"), 1)

    # ---- the receipt check is not touched by any of it ----
    def test_the_chat_limits_and_the_receipt_limits_are_separate_counters(self):
        self.fill("ai_chat_rl_shop", views.AI_CHAT_SHOP_DAILY_LIMIT)
        self.assertEqual(rate_limit.count("scan:ip:203.0.113.40:d"), 0)
        self.assertEqual(self.ask(ip="203.0.113.40").status_code, 429)
        self.assertEqual(rate_limit.count("scan:ip:203.0.113.40:d"), 0)

    def test_the_limit_messages_are_friendly_and_point_to_facebook(self):
        for text in (views.DAILY_LIMIT_REPLY, views.ACCOUNT_LIMIT_REPLY, views.SHOP_LIMIT_REPLY):
            self.assertIn("Facebook", text)
            self.assertNotIn("error", text.lower())
