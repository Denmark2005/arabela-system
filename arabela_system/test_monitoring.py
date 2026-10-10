"""Error alerts and the uptime check: /healthz/ answers for a monitor without a login and tells strangers nothing; the alert service is OFF
unless SENTRY_DSN is set; when on it sends only the error (no people, no form contents), skips noise, and can never stop the site."""
import io
import sys
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from arabela_system import monitoring

DSN = "https://publickey@o123.ingest.sentry.io/456"


class HealthPageTests(TestCase):
    def test_it_answers_ok_for_anyone_with_no_login(self):
        response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(reverse("healthz"), "/healthz/")

    def test_it_asks_the_database_one_tiny_question(self):
        with self.assertNumQueries(1):
            self.client.get("/healthz/")

    def test_it_is_never_kept_by_a_browser_or_a_cache(self):
        control = self.client.get("/healthz/")["Cache-Control"]
        for part in ("no-cache", "no-store", "max-age=0"):
            self.assertIn(part, control)

    def test_a_monitor_can_use_head_but_nothing_can_post_to_it(self):
        self.assertEqual(self.client.head("/healthz/").status_code, 200)
        self.assertEqual(self.client.post("/healthz/").status_code, 405)

    def test_it_says_whether_error_alerts_are_on_and_nothing_else(self):
        self.assertEqual(self.client.get("/healthz/").json(), {"status": "ok", "error_alerts": "off"})
        with patch("arabela_system.monitoring.is_on", return_value=True):
            self.assertEqual(self.client.get("/healthz/").json(), {"status": "ok", "error_alerts": "on"})

    def test_when_the_database_does_not_answer_it_says_503_without_any_details(self):
        broken = MagicMock()
        broken.cursor.side_effect = OperationalError("password authentication failed for user secret_user at secret.host")
        with patch("arabela_system.health.connection", broken), patch("arabela_system.health.time.sleep"):
            with self.assertLogs("arabela.health", level="WARNING"):
                response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "error"})
        self.assertNotIn("secret", response.content.decode())

    def test_one_busy_moment_is_not_reported_as_down(self):
        real_cursor = MagicMock()
        flaky = MagicMock()
        flaky.cursor.side_effect = [OperationalError("pool full"), real_cursor]
        with patch("arabela_system.health.connection", flaky), patch("arabela_system.health.time.sleep"):
            response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 200)

    def test_it_is_plain_json_not_a_page(self):
        response = self.client.get("/healthz/")
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertNotIn(b"<html", response.content.lower())


class AlertsAreOffUnlessConfiguredTests(SimpleTestCase):
    def test_without_the_setting_nothing_starts_and_nothing_is_called(self):
        with patch("sentry_sdk.init") as init:
            for environ in ({}, {"SENTRY_DSN": ""}, {"SENTRY_DSN": "   "}):
                with self.subTest(environ=environ):
                    self.assertFalse(monitoring.init_from_environment(environ))
        init.assert_not_called()

    def test_the_test_run_itself_has_alerts_off(self):
        self.assertFalse(monitoring.is_on())


class AlertsWhenConfiguredTests(SimpleTestCase):
    def start(self, extra=None):
        environ = {"SENTRY_DSN": DSN, **(extra or {})}
        with patch("sentry_sdk.init") as init:
            self.assertTrue(monitoring.init_from_environment(environ))
        init.assert_called_once()
        return init.call_args.kwargs

    def test_it_starts_with_the_address_and_the_django_integration(self):
        from sentry_sdk.integrations.django import DjangoIntegration
        options = self.start()
        self.assertEqual(options["dsn"], DSN)
        self.assertTrue(any(isinstance(i, DjangoIntegration) for i in options["integrations"]))

    def test_it_sends_no_people_and_no_form_contents(self):
        options = self.start()
        self.assertIs(options["send_default_pii"], False)             # no names, emails or IP addresses
        self.assertEqual(options["max_request_body_size"], "never")   # never what was typed into a form

    def test_it_only_sends_errors_not_speed_measurements(self):
        self.assertEqual(self.start()["traces_sample_rate"], 0.0)

    def test_each_alert_says_which_version_of_the_code_it_came_from(self):
        self.assertEqual(self.start({"RENDER_GIT_COMMIT": "6ad3cbf0"})["release"], "6ad3cbf0")
        self.assertIsNone(self.start()["release"])

    def test_the_environment_defaults_to_production_and_can_be_named(self):
        self.assertEqual(self.start()["environment"], "production")
        self.assertEqual(self.start({"SENTRY_ENVIRONMENT": "staging"})["environment"], "staging")

    def test_noise_is_dropped_but_real_errors_are_kept(self):
        from django.core.exceptions import DisallowedHost
        before_send = self.start()["before_send"]
        event = {"message": "x"}
        self.assertIsNone(before_send(event, {"exc_info": (DisallowedHost, DisallowedHost("bad host"), None)}))
        self.assertIsNone(before_send(event, {"exc_info": (BrokenPipeError, BrokenPipeError(), None)}))
        self.assertEqual(before_send(event, {"exc_info": (ValueError, ValueError("real bug"), None)}), event)
        self.assertEqual(before_send(event, {}), event)

    def test_if_the_package_is_missing_the_site_still_runs_and_says_alerts_are_off(self):
        with patch.dict(sys.modules, {"sentry_sdk": None}):
            with self.assertLogs("arabela.monitoring", level="WARNING") as logged:
                self.assertFalse(monitoring.init_from_environment({"SENTRY_DSN": DSN}))
        self.assertIn("error alerts are OFF", logged.output[0])


class TestAlertCommandTests(SimpleTestCase):
    def test_it_refuses_clearly_when_alerts_are_off(self):
        with self.assertRaisesMessage(CommandError, "Error alerts are OFF"):
            call_command("send_test_alert", stdout=io.StringIO())

    def test_it_sends_one_error_level_message_and_waits_for_it_to_leave(self):
        out = io.StringIO()
        with patch("arabela_system.monitoring.is_on", return_value=True), \
                patch("sentry_sdk.capture_message", return_value="abc123") as capture, patch("sentry_sdk.flush") as flush:
            call_command("send_test_alert", stdout=out)
        capture.assert_called_once()
        self.assertEqual(capture.call_args.kwargs["level"], "error")
        self.assertIn("error alerts work", capture.call_args.args[0])
        flush.assert_called_once()
        self.assertIn("Test alert sent (id abc123)", out.getvalue())
