import json
from unittest.mock import MagicMock, patch

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from ai_recommendation import views


def _mock_response(status_code=200, json_data=None, raise_for_json=False):
    resp = MagicMock()
    resp.status_code = status_code
    if raise_for_json:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = json_data or {}
    return resp


def _candidate_reply(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


class ChatViewTests(TestCase):
    """`ai_recommendation.views.chat` -- the AI stylist chat endpoint. Every test here
    mocks the actual outbound `requests.post` call: this must NEVER make a real
    network request to Google's Gemini API during a test run (costs real quota,
    non-deterministic, and the tests would be worthless if they depended on a live
    external service being up)."""

    def setUp(self):
        cache.clear()  # the rate limiter below is stateful; start every test clean
        self.url = reverse("ai_recommendation:chat")

    def _post(self, message="What gowns do you have?", history=None, **overrides):
        payload = {"message": message}
        if history is not None:
            payload["history"] = history
        payload.update(overrides)
        return self.client.post(self.url, data=json.dumps(payload), content_type="application/json")

    @override_settings(GEMINI_API_KEY="")
    def test_no_api_key_configured_returns_fallback_without_calling_the_api(self):
        with patch.object(views.requests, "post") as mock_post:
            response = self._post()
        mock_post.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reply"], views.FALLBACK_REPLY)
        self.assertEqual(response.json()["error"], "not_configured")

    @override_settings(GEMINI_API_KEY="test-key")
    def test_empty_message_is_rejected_without_calling_the_api(self):
        with patch.object(views.requests, "post") as mock_post:
            response = self._post(message="   ")
        mock_post.assert_not_called()
        self.assertEqual(response.status_code, 400)

    @override_settings(GEMINI_API_KEY="test-key")
    def test_get_request_is_rejected(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    @override_settings(GEMINI_API_KEY="test-key")
    def test_successful_reply_is_returned_to_the_customer(self):
        with patch.object(views.requests, "post", return_value=_mock_response(
            200, _candidate_reply("Try Wedding Gown Five -- P2,400!")
        )) as mock_post:
            response = self._post(message="Recommend something for a wedding")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reply"], "Try Wedding Gown Five -- P2,400!")
        mock_post.assert_called_once()

    @override_settings(GEMINI_API_KEY="test-key")
    def test_message_over_the_length_limit_is_truncated_before_sending(self):
        long_message = "a" * 1000
        with patch.object(views.requests, "post", return_value=_mock_response(200, _candidate_reply("ok"))) as mock_post:
            self._post(message=long_message)
        sent_body = mock_post.call_args.kwargs["json"]
        sent_text = sent_body["contents"][-1]["parts"][0]["text"]
        self.assertEqual(len(sent_text), views.MAX_MESSAGE_LENGTH)

    @override_settings(GEMINI_API_KEY="test-key")
    def test_timeout_returns_the_timeout_reply(self):
        with patch.object(views.requests, "post", side_effect=requests.Timeout):
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reply"], views.TIMEOUT_REPLY)
        self.assertEqual(response.json()["error"], "timeout")

    @override_settings(GEMINI_API_KEY="test-key")
    def test_network_error_returns_the_fallback_reply(self):
        with patch.object(views.requests, "post", side_effect=requests.ConnectionError):
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reply"], views.FALLBACK_REPLY)
        self.assertEqual(response.json()["error"], "network")

    @override_settings(GEMINI_API_KEY="test-key")
    def test_rate_limit_429_returns_the_busy_reply(self):
        with patch.object(views.requests, "post", return_value=_mock_response(429)):
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reply"], views.BUSY_REPLY)
        self.assertEqual(response.json()["error"], "rate_limited")

    @override_settings(GEMINI_API_KEY="test-key")
    def test_non_200_non_429_status_returns_the_fallback_reply(self):
        with patch.object(views.requests, "post", return_value=_mock_response(500)):
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reply"], views.FALLBACK_REPLY)
        self.assertEqual(response.json()["error"], "http_500")

    @override_settings(GEMINI_API_KEY="test-key")
    def test_response_with_no_extractable_text_returns_fallback_with_finish_reason(self):
        with patch.object(views.requests, "post", return_value=_mock_response(
            200, {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]}
        )):
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reply"], views.FALLBACK_REPLY)
        self.assertEqual(response.json()["error"], "empty_MAX_TOKENS")

    @override_settings(GEMINI_API_KEY="test-key")
    def test_malformed_json_body_is_rejected(self):
        response = self.client.post(self.url, data="not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)


class SystemPromptTests(TestCase):
    """`_system_prompt` builds the AI's instructions from real SiteSettings + the real
    collection registry -- it must never silently fall back to placeholder contact
    info once the shop's real details are configured."""

    def test_system_prompt_includes_configured_shop_contact_info(self):
        from gowns.models import SiteSettings
        settings_obj = SiteSettings.load()
        settings_obj.phone = "0917-000-1234"
        settings_obj.save(update_fields=["phone"])
        prompt = views._system_prompt()
        self.assertIn("0917-000-1234", prompt)

    def test_system_prompt_lists_all_eleven_collections(self):
        prompt = views._system_prompt()
        for label in ["Wedding Gown", "Ball Gown", "Belo", "Dresses"]:
            self.assertIn(label, prompt)


class ClientIPTests(TestCase):
    """`_client_ip` -- must read the LAST X-Forwarded-For entry (the one Render's own
    edge proxy appended itself), never the first. Trusting the first entry would let
    any visitor fake a different IP just by sending their own X-Forwarded-For header,
    defeating the rate limit below entirely."""

    def test_no_forwarded_header_falls_back_to_remote_addr(self):
        request = MagicMock()
        request.META = {"REMOTE_ADDR": "10.0.0.5"}
        self.assertEqual(views._client_ip(request), "10.0.0.5")

    def test_single_forwarded_ip_is_used(self):
        request = MagicMock()
        request.META = {"HTTP_X_FORWARDED_FOR": "203.0.113.7", "REMOTE_ADDR": "10.0.0.1"}
        self.assertEqual(views._client_ip(request), "203.0.113.7")

    def test_takes_the_last_ip_not_the_first_to_resist_client_spoofing(self):
        request = MagicMock()
        request.META = {
            "HTTP_X_FORWARDED_FOR": "9.9.9.9, 203.0.113.7",
            "REMOTE_ADDR": "10.0.0.1",
        }
        self.assertEqual(views._client_ip(request), "203.0.113.7")


class ChatRateLimitTests(TestCase):
    """The self-imposed rate limit in front of Gemini -- must stop a burst from a
    single visitor cold, without ever punishing a different visitor sharing the
    server, and a blocked request must never reach Gemini (so it never costs quota)."""

    def setUp(self):
        cache.clear()
        self.url = reverse("ai_recommendation:chat")

    def tearDown(self):
        cache.clear()

    def _post(self, ip="203.0.113.50", message="Hi"):
        return self.client.post(
            self.url,
            data=json.dumps({"message": message}),
            content_type="application/json",
            HTTP_X_FORWARDED_FOR=ip,
        )

    @override_settings(GEMINI_API_KEY="test-key")
    def test_requests_up_to_the_per_minute_limit_all_go_through(self):
        with patch.object(views.requests, "post", return_value=_mock_response(200, _candidate_reply("ok"))) as mock_post:
            for _ in range(views.AI_CHAT_RATE_LIMIT_PER_MINUTE):
                response = self._post()
                self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_post.call_count, views.AI_CHAT_RATE_LIMIT_PER_MINUTE)

    @override_settings(GEMINI_API_KEY="test-key")
    def test_the_next_request_past_the_per_minute_limit_is_blocked(self):
        with patch.object(views.requests, "post", return_value=_mock_response(200, _candidate_reply("ok"))) as mock_post:
            for _ in range(views.AI_CHAT_RATE_LIMIT_PER_MINUTE):
                self._post()
            response = self._post()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["reply"], views.BUSY_REPLY)
        self.assertEqual(response.json()["error"], "rate_limited_burst")
        # the blocked request must never have reached Gemini
        self.assertEqual(mock_post.call_count, views.AI_CHAT_RATE_LIMIT_PER_MINUTE)

    @override_settings(GEMINI_API_KEY="test-key")
    def test_a_different_visitor_is_never_affected_by_someone_elses_burst(self):
        with patch.object(views.requests, "post", return_value=_mock_response(200, _candidate_reply("ok"))):
            for _ in range(views.AI_CHAT_RATE_LIMIT_PER_MINUTE):
                self._post(ip="203.0.113.50")
            blocked = self._post(ip="203.0.113.50")
            other_visitor = self._post(ip="198.51.100.9")
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(other_visitor.status_code, 200)

    @override_settings(GEMINI_API_KEY="test-key")
    def test_spoofed_first_hop_does_not_evade_the_limit(self):
        """A visitor prepending their own fake IP can't dodge the limit -- Render still
        appends the real one last, and the view must key off that last entry."""
        with patch.object(views.requests, "post", return_value=_mock_response(200, _candidate_reply("ok"))):
            for i in range(views.AI_CHAT_RATE_LIMIT_PER_MINUTE):
                self.client.post(
                    self.url, data=json.dumps({"message": "hi"}), content_type="application/json",
                    HTTP_X_FORWARDED_FOR=f"9.9.9.{i}, 203.0.113.50",
                )
            response = self.client.post(
                self.url, data=json.dumps({"message": "hi"}), content_type="application/json",
                HTTP_X_FORWARDED_FOR="1.1.1.1, 203.0.113.50",
            )
        self.assertEqual(response.status_code, 429)

    @override_settings(GEMINI_API_KEY="test-key")
    def test_daily_limit_blocks_even_while_under_the_per_minute_cap(self):
        ip = "203.0.113.77"
        cache.set(f"ai_chat_rl_day:{ip}", views.AI_CHAT_DAILY_LIMIT, views.AI_CHAT_DAILY_WINDOW_SECONDS)
        with patch.object(views.requests, "post", return_value=_mock_response(200, _candidate_reply("ok"))) as mock_post:
            response = self._post(ip=ip)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"], "rate_limited_daily")
        mock_post.assert_not_called()

    @override_settings(GEMINI_API_KEY="test-key")
    def test_a_request_blocked_by_the_per_minute_limit_does_not_also_count_against_the_daily_limit(self):
        ip = "203.0.113.88"
        with patch.object(views.requests, "post", return_value=_mock_response(200, _candidate_reply("ok"))):
            for _ in range(views.AI_CHAT_RATE_LIMIT_PER_MINUTE + 3):
                self._post(ip=ip)
        self.assertEqual(cache.get(f"ai_chat_rl_day:{ip}"), views.AI_CHAT_RATE_LIMIT_PER_MINUTE)
