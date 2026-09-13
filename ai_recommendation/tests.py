import json
from unittest.mock import MagicMock, patch

import requests
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
