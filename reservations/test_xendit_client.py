"""reservations/xendit.py on its own: how it builds each request, reads each answer (the shapes Xendit's test mode really sent on 2026-11-24), and checks a webhook.
No test reaches the network: `_request` is patched, or `requests.request` for the one test of the HTTP call itself."""
import json
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from reservations import xendit

KEY = "xnd_development_abcdefghijklmnopqrstuvwxyz0123456789"
TOKEN = "callback_token_unit"


def session_json(status="ACTIVE", **extra):
    base = {"payment_session_id": "ps-6b05777a9b048d20d115549d", "status": status, "reference_id": "tok-123", "currency": "PHP", "amount": 100, "country": "PH",
            "mode": "PAYMENT_LINK", "session_type": "PAY", "metadata": {"attempt": "7"},
            "payment_link_url": "https://checkout-staging.xendit.co/session/session-b0ff4ecc297a2a08af1f96ea8c05b2c1"}
    base.update(extra)
    return base


def payment_json(status="SUCCEEDED", **extra):
    base = {"payment_id": "py-d9232620-8af4-4cdb-8794-73f14e604bc4", "status": status, "channel_code": "GCASH", "request_amount": 100,
            "captures": [{"capture_id": "cptr-1", "capture_timestamp": "2026-11-24T11:41:17.366Z", "capture_amount": 100}] if status == "SUCCEEDED" else []}
    base.update(extra)
    return base


@override_settings(TESTING=False, XENDIT_SECRET_KEY=KEY, XENDIT_API_BASE="https://api.xendit.co", USING_LAPTOP_DB=False)
class SwitchAndMoneyTests(SimpleTestCase):
    def test_the_mode_is_read_from_the_key(self):
        self.assertEqual((xendit.mode(), xendit.is_test(), xendit.enabled()), ("test", True, True))
        with override_settings(XENDIT_SECRET_KEY="xnd_production_abc"):
            self.assertEqual((xendit.mode(), xendit.is_test(), xendit.enabled()), ("live", False, True))
            with override_settings(USING_LAPTOP_DB=True):
                self.assertFalse(xendit.enabled())
        with override_settings(XENDIT_SECRET_KEY=""):
            self.assertEqual((xendit.mode(), xendit.enabled()), ("", False))

    def test_amounts_are_pesos_as_a_number(self):
        self.assertEqual([xendit.to_amount(a) for a in ("2000", Decimal("5500.00"), "150.5", Decimal("1234.567"))], [2000, 5500, 150.5, 1234.57])
        self.assertEqual(xendit.from_amount(100), Decimal("100.00"))
        self.assertEqual(xendit.from_amount("garbage"), Decimal("0.00"))

    def test_the_channels_are_the_three_wallets_unless_configured(self):
        self.assertEqual(xendit.methods(), ["GCASH", "PAYMAYA", "QRPH"])
        with override_settings(XENDIT_CHANNELS=["gcash"]):
            self.assertEqual(xendit.methods(), ["GCASH"])


@override_settings(TESTING=False, XENDIT_SECRET_KEY=KEY, XENDIT_API_BASE="https://api.xendit.co", USING_LAPTOP_DB=False)
class CreateSessionTests(SimpleTestCase):
    def create(self, answer=None, **kw):
        args = dict(amount=Decimal("2000"), name="Security deposit", description="Arabela Gown Rental: Gown", reference_number="tok-123",
                    success_url="https://shop.example/ok", cancel_url="https://shop.example/no", metadata={"attempt": 7, "purpose": "deposit"})
        args.update(kw)
        with patch("reservations.xendit._request", return_value=session_json() if answer is None else answer) as mocked:
            result = xendit.create_checkout_session(**args)
        return result, mocked

    def test_the_request_is_exactly_what_xendit_documents(self):
        session, mocked = self.create(customer_name="Ana Cruz", customer_email="ana@example.com", expires_minutes=20)
        (method, path, body), _ = mocked.call_args
        self.assertEqual((method, path), ("POST", "/sessions"))
        self.assertEqual({k: body[k] for k in ("reference_id", "session_type", "mode", "amount", "currency", "country", "allowed_payment_channels")},
                         {"reference_id": "tok-123", "session_type": "PAY", "mode": "PAYMENT_LINK", "amount": 2000, "currency": "PHP", "country": "PH",
                          "allowed_payment_channels": ["GCASH", "PAYMAYA", "QRPH"]})
        self.assertEqual((body["success_return_url"], body["cancel_return_url"]), ("https://shop.example/ok", "https://shop.example/no"))
        self.assertEqual(body["metadata"], {"attempt": "7", "purpose": "deposit"})
        self.assertIn("Security deposit", body["description"])
        self.assertNotIn("customer", body)
        self.assertNotIn("Ana", json.dumps(body))                                   # the customer's name and email stay with the shop
        self.assertNotIn("ana@example.com", json.dumps(body))
        self.assertEqual((session.id, session.checkout_url), ("ps-6b05777a9b048d20d115549d", session_json()["payment_link_url"]))

    def test_the_expiry_is_sent_as_utc_and_never_below_ten_minutes(self):
        _, mocked = self.create(expires_minutes=20)
        stamp = mocked.call_args.args[2]["expires_at"]
        self.assertRegex(stamp, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.000Z$")
        delta = (datetime.fromisoformat(stamp.replace("Z", "+00:00")) - datetime.now(dt_timezone.utc)).total_seconds() / 60
        self.assertTrue(19 < delta <= 20.5, delta)
        _, short = self.create(expires_minutes=2)
        delta = (datetime.fromisoformat(short.call_args.args[2]["expires_at"].replace("Z", "+00:00")) - datetime.now(dt_timezone.utc)).total_seconds() / 60
        self.assertTrue(9 < delta <= 10.5, delta)
        _, none = self.create()
        self.assertNotIn("expires_at", none.call_args.args[2])

    def test_a_decimal_amount_and_a_tiny_amount(self):
        _, mocked = self.create(amount=Decimal("150.50"))
        self.assertEqual(mocked.call_args.args[2]["amount"], 150.5)
        with self.assertRaises(xendit.XenditError):
            self.create(amount=Decimal("0.50"))

    def test_an_answer_without_a_session_id_or_with_an_unsafe_page_is_refused(self):
        with self.assertRaises(xendit.XenditError):
            self.create(answer={"status": "ACTIVE"})
        with self.assertRaises(xendit.XenditError):
            self.create(answer=session_json(payment_link_url="http://insecure.example/pay"))
        with self.assertRaises(xendit.XenditError):
            self.create(answer=session_json(payment_link_url=""))

    def test_long_text_is_cut_to_what_xendit_accepts(self):
        _, mocked = self.create(description="x" * 5000, metadata={"k" * 100: "v" * 900})
        body = mocked.call_args.args[2]
        self.assertLessEqual(len(body["description"]), 1000)
        self.assertEqual([(len(k), len(v)) for k, v in body["metadata"].items()], [(40, 500)])


@override_settings(TESTING=False, XENDIT_SECRET_KEY=KEY, XENDIT_API_BASE="https://api.xendit.co", USING_LAPTOP_DB=False)
class ReadSessionTests(SimpleTestCase):
    def read(self, session, payment=None):
        def fake(method, path, body=None, headers=None):
            if path.startswith("/sessions/"):
                return session
            if path.startswith("/v3/payments/"):
                self.assertEqual(headers, {"api-version": "2024-11-11"})
                return payment
            raise AssertionError(path)
        with patch("reservations.xendit._request", side_effect=fake) as mocked:
            return xendit.retrieve_checkout_session("ps-6b05777a9b048d20d115549d"), mocked

    def test_an_active_session_is_waiting_and_asks_nothing_more(self):
        state, mocked = self.read(session_json("ACTIVE"))
        self.assertEqual((state.paid, state.status, state.id, state.reference_number), (False, "active", "ps-6b05777a9b048d20d115549d", "tok-123"))
        self.assertEqual(mocked.call_count, 1)

    def test_a_paid_session_gives_the_channel_the_amount_and_the_time_from_the_payment(self):
        state, mocked = self.read(session_json("COMPLETED", payment_id="py-d9232620-8af4-4cdb-8794-73f14e604bc4"), payment_json())
        self.assertEqual((state.paid, state.payment_id, state.amount, state.channel), (True, "py-d9232620-8af4-4cdb-8794-73f14e604bc4", Decimal("100.00"), "gcash"))
        self.assertEqual(state.paid_at, int(datetime(2026, 11, 24, 11, 41, 17, tzinfo=dt_timezone.utc).timestamp()))
        self.assertEqual(state.metadata, {"attempt": "7"})
        self.assertEqual(mocked.call_count, 2)

    def test_the_three_channels_come_back_in_the_names_the_shop_uses(self):
        for code, name in (("GCASH", "gcash"), ("PAYMAYA", "paymaya"), ("QRPH", "qrph")):
            state, _ = self.read(session_json("COMPLETED", payment_id="py-1"), payment_json(channel_code=code))
            self.assertEqual(state.channel, name)
            self.assertIn(name, xendit.CHANNEL_LABELS)

    def test_a_completed_session_whose_payment_did_not_succeed_is_not_paid(self):
        for status in ("FAILED", "PENDING", "EXPIRED", "CANCELED"):
            state, _ = self.read(session_json("COMPLETED", payment_id="py-1"), payment_json(status))
            self.assertFalse(state.paid, status)

    def test_a_completed_session_with_no_payment_id_is_not_paid(self):
        state, mocked = self.read(session_json("COMPLETED"))
        self.assertFalse(state.paid)
        self.assertEqual(mocked.call_count, 1)

    def test_expired_and_cancelled_sessions_are_both_expired_to_the_shop(self):
        for status in ("EXPIRED", "CANCELED"):
            state, _ = self.read(session_json(status))
            self.assertEqual((state.paid, state.status), (False, "expired"))

    def test_several_captures_are_added_up(self):
        payment = payment_json(captures=[{"capture_timestamp": "2026-11-24T11:41:17.366Z", "capture_amount": 60}, {"capture_timestamp": "2026-11-24T11:45:00.000Z", "capture_amount": 40}])
        state, _ = self.read(session_json("COMPLETED", payment_id="py-1"), payment)
        self.assertEqual(state.amount, Decimal("100.00"))

    def test_nonsense_never_crashes_it(self):
        for junk in ({}, {"status": None}, {"payment_session_id": None, "status": 5}):
            state, _ = self.read(junk)
            self.assertFalse(state.paid)


@override_settings(TESTING=False, XENDIT_SECRET_KEY=KEY, XENDIT_API_BASE="https://api.xendit.co", USING_LAPTOP_DB=False)
class CloseSessionTests(SimpleTestCase):
    def close(self, **routes):
        def fake(method, path, body=None, headers=None):
            key = f"{method} {path.split('/')[-1] if method == 'POST' else 'get'}"
            answer = routes[key]
            if isinstance(answer, Exception):
                raise answer
            return answer
        with patch("reservations.xendit._request", side_effect=fake):
            return xendit.expire_checkout_session("ps-6b05777a9b048d20d115549d")

    def test_an_active_session_is_cancelled(self):
        self.assertEqual(self.close(**{"GET get": session_json("ACTIVE"), "POST cancel": session_json("CANCELED")}), "expired")

    def test_a_paid_session_is_left_alone(self):
        self.assertEqual(self.close(**{"GET get": session_json("COMPLETED", payment_id="py-1")}), "has_payment")

    def test_an_already_closed_session_says_so_without_asking_again(self):
        for status in ("EXPIRED", "CANCELED"):
            self.assertEqual(self.close(**{"GET get": session_json(status)}), "already_expired")

    def test_a_session_paid_in_the_moment_between_the_two_calls_is_not_lost(self):
        refused = xendit.XenditError("The session is already completed", status=400)
        self.assertEqual(self.close(**{"GET get": session_json("ACTIVE"), "POST cancel": refused}), "has_payment")

    def test_any_other_trouble_is_an_error_to_try_again_never_a_close(self):
        self.assertEqual(self.close(**{"GET get": xendit.XenditError("down", retryable=True)}), "error")
        self.assertEqual(self.close(**{"GET get": session_json("ACTIVE"), "POST cancel": xendit.XenditError("boom", status=500)}), "error")


@override_settings(TESTING=False, XENDIT_SECRET_KEY=KEY, XENDIT_API_BASE="https://api.xendit.co", USING_LAPTOP_DB=False)
class RefundTests(SimpleTestCase):
    def test_the_request_has_a_new_reference_every_time_and_the_version_header(self):
        answer = {"refund_id": "rfd-a88c4317", "status": "PENDING", "amount": 1, "payment_id": "py-1", "channel_code": "GCASH"}
        with patch("reservations.xendit._request", return_value=answer) as mocked:
            first = xendit.create_refund(payment_id="py-1", amount=Decimal("1500"), note="Security deposit return for RSV-1")
            xendit.create_refund(payment_id="py-1", amount=Decimal("1500"))
        (method, path, body), kw = mocked.call_args_list[0]
        self.assertEqual((method, path, kw["headers"]), ("POST", "/v3/refunds", {"api-version": "2024-11-11"}))
        self.assertEqual((body["payment_id"], body["amount"], body["reason"], body["metadata"]), ("py-1", 1500, "OTHERS", {"note": "Security deposit return for RSV-1"}))
        self.assertTrue(body["reference_id"].startswith("rf-"))
        self.assertNotEqual(body["reference_id"], mocked.call_args_list[1].args[2]["reference_id"])
        self.assertEqual((first.id, first.status, first.payment_id), ("rfd-a88c4317", "pending", "py-1"))

    def test_too_small_and_no_id_are_refused(self):
        with self.assertRaises(xendit.XenditError):
            xendit.create_refund(payment_id="py-1", amount=Decimal("0.40"))
        with patch("reservations.xendit._request", return_value={"status": "PENDING"}), self.assertRaises(xendit.XenditError):
            xendit.create_refund(payment_id="py-1", amount=Decimal("100"))

    def test_a_refund_is_read_back_by_its_id(self):
        with patch("reservations.xendit._request", return_value={"refund_id": "rfd-1", "status": "SUCCEEDED", "amount": 1500, "payment_id": "py-1"}) as mocked:
            refund = xendit.retrieve_refund("rfd-1")
        self.assertEqual(mocked.call_args.args[:2], ("GET", "/v3/refunds/rfd-1"))
        self.assertEqual((refund.status, refund.amount), ("succeeded", Decimal("1500.00")))

    def test_parse_refund_takes_the_refund_itself_or_an_event_wrapper(self):
        itself = xendit.parse_refund({"refund_id": "rfd-1", "status": "FAILED", "amount": 10, "payment_id": "py-1"})
        wrapped = xendit.parse_refund({"data": {"refund_id": "rfd-1", "status": "FAILED", "amount": 10, "payment_id": "py-1"}})
        self.assertEqual((itself.id, itself.status), (wrapped.id, wrapped.status))
        self.assertEqual(xendit.parse_refund({}).id, "")


class WebhookTests(SimpleTestCase):
    BODY = json.dumps({"event": "payment_session.completed", "business_id": "b", "data": {"payment_session_id": "ps-1", "status": "COMPLETED"}}).encode()

    def test_only_the_shops_own_token_makes_an_event_believable(self):
        self.assertEqual(xendit.verify_webhook(self.BODY, TOKEN, TOKEN)["event"], "payment_session.completed")
        self.assertEqual(xendit.verify_webhook(self.BODY, f"  {TOKEN} ", TOKEN)["event"], "payment_session.completed")
        for header in ("", None, "wrong", TOKEN + "x", TOKEN[:-1], TOKEN.upper()):
            self.assertIsNone(xendit.verify_webhook(self.BODY, header, TOKEN), header)
        self.assertIsNone(xendit.verify_webhook(self.BODY, TOKEN, ""))                      # a shop with no token set believes nothing
        self.assertIsNone(xendit.verify_webhook(self.BODY, "", ""))

    def test_a_body_that_is_not_an_event_is_refused_even_with_the_right_token(self):
        for body in (b"not json", b"[1, 2]", b"\xff\xfe", "text"):
            self.assertIsNone(xendit.verify_webhook(body, TOKEN, TOKEN))

    def test_events_are_named_the_way_the_shops_webhook_view_expects(self):
        paid = {"event": "payment_session.completed", "data": {"payment_session_id": "ps-1"}}
        self.assertEqual((xendit.event_type(paid), xendit.event_resource(paid)["id"]), ("checkout_session.payment.paid", "ps-1"))
        ok = {"event": "refund.succeeded", "data": {"refund_id": "rfd-1", "payment_id": "py-1", "amount": 5}}
        bad = {"event": "refund.failed", "data": {"refund_id": "rfd-2", "status": "FAILED"}}
        self.assertEqual((xendit.event_type(ok), xendit.event_resource(ok)["id"], xendit.event_resource(ok)["status"]), ("payment.refund.updated", "rfd-1", "succeeded"))
        self.assertEqual((xendit.event_type(bad), xendit.event_resource(bad)["status"]), ("payment.refund.updated", "FAILED"))
        self.assertEqual(xendit.event_type({"event": "payment_session.expired"}), "payment_session.expired")
        self.assertEqual((xendit.event_type({}), xendit.event_resource({})), ("", {}))

    def test_the_session_and_the_refund_are_found_under_either_name_xendit_uses(self):
        # The sample Xendit's dashboard sends ("Test and save") names the session "id"; its documentation names it "payment_session_id".
        sample = {"event": "payment_session.completed", "business_id": "b", "data": {"id": "ps-579c8d61f23fa4ca35e52da4", "amount": 100000}}
        self.assertEqual(xendit.event_resource(sample)["id"], "ps-579c8d61f23fa4ca35e52da4")
        refund = {"event": "refund.succeeded", "data": {"id": "rfd-7", "payment_id": "py-1", "amount": 5}}
        resource = xendit.event_resource(refund)
        self.assertEqual((resource["id"], resource["refund_id"], xendit.parse_refund(resource).id), ("rfd-7", "rfd-7", "rfd-7"))


class HttpCallTests(SimpleTestCase):
    def response(self, status=200, body=None, content=True):
        r = MagicMock()
        r.status_code = status
        r.content = b"x" if content else b""
        r.json.return_value = {} if body is None else body
        return r

    @override_settings(TESTING=False, XENDIT_SECRET_KEY=KEY, XENDIT_API_BASE="https://api.xendit.co", XENDIT_TIMEOUT=7)
    def test_the_key_is_the_basic_auth_user_name_with_an_empty_password(self):
        with patch("reservations.xendit.requests.request", return_value=self.response(body={"ok": 1})) as mocked:
            self.assertEqual(xendit._request("GET", "/sessions/ps-1", headers={"api-version": "2024-11-11"}), {"ok": 1})
        args, kwargs = mocked.call_args
        self.assertEqual(args, ("GET", "https://api.xendit.co/sessions/ps-1"))
        self.assertEqual((kwargs["auth"], kwargs["timeout"]), ((KEY, ""), 7))
        self.assertEqual(kwargs["headers"]["api-version"], "2024-11-11")
        self.assertNotIn(KEY, json.dumps({k: v for k, v in kwargs["headers"].items()}))      # never in a header

    @override_settings(TESTING=False, XENDIT_SECRET_KEY=KEY)
    def test_errors_are_short_safe_sentences_and_say_whether_to_try_again(self):
        with patch("reservations.xendit.requests.request", return_value=self.response(403, {"message": "The API key is forbidden", "error_code": "REQUEST_FORBIDDEN_ERROR"})):
            with self.assertRaises(xendit.XenditError) as caught:
                xendit._request("POST", "/sessions", {})
        self.assertEqual((str(caught.exception), caught.exception.status, caught.exception.retryable), ("The API key is forbidden", 403, False))
        self.assertNotIn(KEY, str(caught.exception))
        for status, retry in ((429, True), (500, True), (503, True), (400, False), (404, False)):
            with patch("reservations.xendit.requests.request", return_value=self.response(status, {})):
                with self.assertRaises(xendit.XenditError) as caught:
                    xendit._request("GET", "/x")
            self.assertEqual(caught.exception.retryable, retry, status)

    @override_settings(TESTING=False, XENDIT_SECRET_KEY=KEY)
    def test_a_dropped_connection_and_a_garbage_answer_are_retryable(self):
        with patch("reservations.xendit.requests.request", side_effect=xendit.requests.ConnectionError("boom")):
            with self.assertRaises(xendit.XenditError) as caught:
                xendit._request("GET", "/x")
        self.assertTrue(caught.exception.retryable)
        bad = self.response()
        bad.json.side_effect = ValueError("no json")
        with patch("reservations.xendit.requests.request", return_value=bad), self.assertRaises(xendit.XenditError):
            xendit._request("GET", "/x")
        with patch("reservations.xendit.requests.request", return_value=self.response(content=False)):
            self.assertEqual(xendit._request("GET", "/x"), {})

    @override_settings(TESTING=True, XENDIT_SECRET_KEY=KEY)
    def test_the_test_suite_can_never_reach_xendit(self):
        with patch("reservations.xendit.requests.request") as mocked, self.assertRaises(xendit.XenditError):
            xendit._request("GET", "/x")
        mocked.assert_not_called()

    @override_settings(TESTING=False, XENDIT_SECRET_KEY="")
    def test_no_key_means_no_call(self):
        with patch("reservations.xendit.requests.request") as mocked, self.assertRaises(xendit.XenditError):
            xendit._request("GET", "/x")
        mocked.assert_not_called()
