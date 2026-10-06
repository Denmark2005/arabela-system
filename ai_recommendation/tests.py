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

    def test_system_prompt_lists_the_real_collections(self):
        prompt = views._system_prompt()
        for label in ["Wedding Gown", "Ball Gown", "Suit", "Dresses"]:
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


def _make_gown(n, **overrides):
    from decimal import Decimal
    from gowns.models import Gown
    defaults = dict(
        gown_id=f"AITEST-{n:04d}", name=f"AI Test Gown {n}", category=Gown.Category.WEDDING_GOWN,
        color_name="White", color_code="WH", size=Gown.Size.MEDIUM, rental_price=Decimal("20000.00"),
        status=Gown.Status.AVAILABLE,
    )
    defaults.update(overrides)
    return Gown.objects.create(**defaults)


class LiveCatalogPromptTests(TestCase):
    """The AI is grounded in the database as it is RIGHT NOW: real gowns, real prices, each one's own
    product page, only the categories that currently exist -- never the old placeholder catalog."""

    def test_the_old_placeholder_catalog_is_gone_from_the_prompt(self):
        prompt = views._system_prompt()
        for stale in ("Ball Gown One", "Wedding Gown Three", "11 collections", "P1,600", "One=P1,600", "currently Reserved"):
            self.assertNotIn(stale, prompt)

    def test_a_real_gown_is_listed_with_its_real_price_and_its_own_product_page(self):
        gown = _make_gown(1, name="Marquee Gown", rental_price="20000")
        prompt = views._system_prompt()
        self.assertIn(f"Marquee Gown | ₱20,000 | [Marquee Gown](/collections/wedding/products/{gown.slug}/)", prompt)
        self.assertIn("[Wedding Gown](/collections/wedding/)", prompt)  # and the category's browse link

    def test_the_prompt_follows_the_database_between_two_messages(self):
        gown = _make_gown(1, name="Marquee Gown", rental_price="20000")
        self.assertIn("Marquee Gown | ₱20,000", views._system_prompt())
        gown.rental_price = 18500
        gown.save()
        self.assertIn("Marquee Gown | ₱18,500", views._system_prompt())
        self.assertNotIn("₱20,000", views._system_prompt())
        gown.delete()
        self.assertNotIn("Marquee Gown", views._system_prompt())
        late = _make_gown(2, name="Fresh Arrival")
        self.assertIn("Fresh Arrival", views._system_prompt())
        late.status = "Out-of-Stock"
        late.save()
        self.assertNotIn("Fresh Arrival", views._system_prompt())

    def test_several_units_of_one_gown_are_listed_once_at_the_lowest_price(self):
        _make_gown(1, name="Twin Gown", rental_price="25000")
        _make_gown(2, name="Twin Gown", rental_price="21000")
        lines = [line for line in views._system_prompt().splitlines() if line.strip().startswith("- Twin Gown")]
        self.assertEqual(len(lines), 1)
        self.assertIn("₱21,000", lines[0])

    def test_a_removed_builtin_category_disappears_and_comes_back(self):
        from gowns.models import HiddenCategory
        self.assertIn("Barong (men's collection)", views._system_prompt())
        HiddenCategory.objects.create(name="Barong")
        prompt = views._system_prompt()
        self.assertNotIn("Barong", prompt)
        self.assertNotIn("/collections/barong/", prompt)
        HiddenCategory.objects.all().delete()
        self.assertIn("Barong (men's collection)", views._system_prompt())

    def test_an_owner_added_category_is_listed_with_the_audience_the_owner_chose(self):
        from gowns.models import CustomCategory
        CustomCategory.objects.create(name="Tuxedo", slug="tuxedo", audience="men")
        CustomCategory.objects.create(name="Debut Gown", slug="debut-gown", audience="women")
        prompt = views._system_prompt()
        self.assertIn("- Tuxedo (men's collection) -- browse all: [Tuxedo](/collections/tuxedo/)", prompt)
        self.assertIn("- Debut Gown (women's collection) -- browse all: [Debut Gown](/collections/debut-gown/)", prompt)

    def test_built_in_categories_carry_their_audience(self):
        prompt = views._system_prompt()
        self.assertIn("- Suit (men's collection)", prompt)
        self.assertIn("- Wedding Gown (women's collection)", prompt)

    def test_an_empty_collection_is_marked_so_the_ai_does_not_recommend_from_it(self):
        self.assertIn("[Suit](/collections/suit/) -- NO GOWNS LISTED YET", views._system_prompt())
        _make_gown(1, category="Suit", name="Gentleman Suit")
        prompt = views._system_prompt()
        self.assertNotIn("[Suit](/collections/suit/) -- NO GOWNS LISTED YET", prompt)
        self.assertIn("Gentleman Suit", prompt)

    def test_a_very_large_collection_is_capped_with_a_pointer_to_the_browse_link(self):
        for n in range(1, 5):
            _make_gown(n, name=f"Cap Gown {n}")
        with patch.object(views, "MAX_GOWNS_PER_CATEGORY", 2):
            prompt = views._system_prompt()
        self.assertIn("...and 2 more", prompt)
        self.assertEqual(sum(1 for line in prompt.splitlines() if "Cap Gown" in line), 2)

    def test_the_prompt_tells_the_ai_to_ask_for_women_or_men_first(self):
        prompt = views._system_prompt()
        self.assertIn("women's gown or a men's outfit", prompt)
        self.assertIn("NEVER put a website address in front of them", prompt)

    def test_no_collections_at_all_still_builds_a_prompt(self):
        from gowns.models import Gown, HiddenCategory
        for name in Gown.Category.values:
            HiddenCategory.objects.create(name=name)
        text, allowed = views._live_catalog()
        self.assertIn("no collections on the website right now", text)
        self.assertEqual(allowed, {"/collections/", "/collections/all/"})
        self.assertIn("LIVE CATALOG", views._system_prompt())

    def test_chat_sends_the_live_catalog_with_every_message(self):
        cache.clear()
        _make_gown(1, name="Marquee Gown")
        url = reverse("ai_recommendation:chat")
        with override_settings(GEMINI_API_KEY="test-key"), patch.object(
            views.requests, "post", return_value=_mock_response(200, _candidate_reply("ok"))
        ) as mock_post:
            self.client.post(url, data=json.dumps({"message": "hi"}), content_type="application/json")
            _make_gown(2, name="Brand New Gown")
            self.client.post(url, data=json.dumps({"message": "hi again"}), content_type="application/json")
        first = mock_post.call_args_list[0].kwargs["json"]["systemInstruction"]["parts"][0]["text"]
        second = mock_post.call_args_list[1].kwargs["json"]["systemInstruction"]["parts"][0]["text"]
        self.assertIn("Marquee Gown", first)
        self.assertNotIn("Brand New Gown", first)
        self.assertIn("Brand New Gown", second)


class ReplyLinkSanitizerTests(TestCase):
    """A reply may only link to pages that exist. Invented pages and the Facebook address glued in
    front of a path -- the bug seen in production -- are removed before the customer sees them."""

    ALLOWED = {
        "/collections/", "/collections/all/", "/collections/wedding/", "/collections/wedding/products/gown-a/",
    }
    HOST = "arabela.example.com"
    FB = "https://www.facebook.com/share/1EHAS1iemQ"

    def clean(self, text):
        return views._sanitize_reply(text, self.ALLOWED, self.HOST)

    def test_a_real_product_link_is_kept(self):
        text = "Try [Gown A](/collections/wedding/products/gown-a/) today."
        self.assertEqual(self.clean(text), text)

    def test_an_invented_product_link_loses_its_link_but_keeps_its_words(self):
        out = self.clean("Try [Ball Gown One](/collections/ball-gown/products/ball-gown-one/) today.")
        self.assertEqual(out, "Try Ball Gown One today.")

    def test_the_facebook_address_glued_in_front_of_a_path_is_removed(self):
        out = self.clean(f"See [the collection]({self.FB}/collections/ball-gown/).")
        self.assertEqual(out, "See the collection.")
        bare = self.clean(f"Browse here: {self.FB}/collections/ball-gown/ for more.")
        self.assertNotIn("facebook", bare)
        self.assertIn("/collections/all/", bare)

    def test_a_plain_facebook_contact_link_is_left_alone(self):
        text = f"Message us: {self.FB} or [our Facebook]({self.FB})."
        self.assertEqual(self.clean(text), text)

    def test_a_real_path_written_bare_or_without_trailing_slash_is_kept_and_tidied(self):
        self.assertEqual(self.clean("Browse /collections/wedding/ now."), "Browse /collections/wedding/ now.")
        self.assertEqual(self.clean("Browse /collections/wedding now"), "Browse /collections/wedding/ now")
        self.assertEqual(self.clean("See /collections/wedding/."), "See /collections/wedding/.")
        self.assertEqual(self.clean("See **/collections/wedding/**"), "See **/collections/wedding/**")

    def test_an_absolute_link_to_this_site_becomes_a_site_path(self):
        out = self.clean(f"[Wedding](https://{self.HOST}/collections/wedding/) and https://{self.HOST}/collections/wedding/products/gown-a/")
        self.assertEqual(out, "[Wedding](/collections/wedding/) and /collections/wedding/products/gown-a/")

    def test_a_collection_page_on_another_website_is_removed(self):
        out = self.clean("[Wedding](https://evil.example.org/collections/wedding/)")
        self.assertEqual(out, "Wedding")

    def test_a_bare_invented_path_points_to_the_whole_catalog_instead(self):
        self.assertEqual(self.clean("Look at /collections/nope/ please"), "Look at /collections/all/ please")

    def test_text_without_links_is_untouched(self):
        text = "Our pick-up hours are 1pm-5pm, **daily**. Price: ₱2,000!"
        self.assertEqual(self.clean(text), text)

    def test_chat_cleans_the_reply_before_it_reaches_the_customer(self):
        cache.clear()
        gown = _make_gown(1, name="Marquee Gown")
        bad = "Try [Ball Gown One](/collections/ball-gown/products/ball-gown-one/) or [Marquee Gown](/collections/wedding/products/%s/)." % gown.slug
        with override_settings(GEMINI_API_KEY="test-key"), patch.object(
            views.requests, "post", return_value=_mock_response(200, _candidate_reply(bad))
        ):
            response = self.client.post(
                reverse("ai_recommendation:chat"), data=json.dumps({"message": "hi"}), content_type="application/json",
            )
        reply = response.json()["reply"]
        self.assertNotIn("ball-gown-one", reply)
        self.assertIn("Try Ball Gown One or", reply)
        self.assertIn(f"[Marquee Gown](/collections/wedding/products/{gown.slug}/)", reply)
