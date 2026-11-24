"""The PayMongo connection (reservations/paymongo.py): what it sends, how it reads PayMongo's answers, and the webhook signature.
No test reaches the network: the HTTP call itself is replaced."""
import base64
import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from reservations import paymongo

KEY = "sk_test_abc123"


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self.content = b"x" if (payload is not None or text) else b""
        self.text = text or ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def session_payload(*, paid=False, amount=200000, channel="gcash", status="active", payment_id="pay_1", sid="cs_1"):
    payments = []
    if paid:
        payments = [{"id": payment_id, "type": "payment", "attributes": {"status": "paid", "amount": amount, "source": {"type": channel}, "paid_at": 1700000100}}]
    return {"data": {"id": sid, "type": "checkout_session", "attributes": {
        "status": status, "reference_number": "tok", "metadata": {"a": "b"}, "livemode": False, "payments": payments,
        "checkout_url": "https://checkout.paymongo.com/cs_1_client#pk_test",
    }}}


@override_settings(TESTING=False, PAYMONGO_SECRET_KEY=KEY, PAYMONGO_API_BASE="https://api.paymongo.com", USING_LAPTOP_DB=False)
class SwitchesAndMoneyTests(SimpleTestCase):
    def test_no_key_means_no_paymongo(self):
        with override_settings(PAYMONGO_SECRET_KEY=""):
            self.assertEqual((paymongo.enabled(), paymongo.mode()), (False, ""))

    def test_a_test_key_is_enabled_and_is_test_mode(self):
        self.assertEqual((paymongo.enabled(), paymongo.mode(), paymongo.is_test()), (True, "test", True))

    def test_a_live_key_is_live_mode_but_never_on_a_laptop_copy_of_the_database(self):
        with override_settings(PAYMONGO_SECRET_KEY="sk_live_zzz"):
            self.assertEqual((paymongo.enabled(), paymongo.mode()), (True, "live"))
        with override_settings(PAYMONGO_SECRET_KEY="sk_live_zzz", USING_LAPTOP_DB=True):
            self.assertFalse(paymongo.enabled())

    def test_the_default_methods_are_gcash_maya_and_qr_ph_and_never_cards(self):
        self.assertEqual(paymongo.methods(), ["gcash", "paymaya", "qrph"])
        self.assertNotIn("card", paymongo.methods())

    def test_pesos_and_centavos_are_exact(self):
        self.assertEqual(paymongo.to_centavos(Decimal("2000")), 200000)
        self.assertEqual(paymongo.to_centavos(Decimal("1234.50")), 123450)
        self.assertEqual(paymongo.to_centavos("0.01"), 1)
        self.assertEqual(paymongo.from_centavos(123450), Decimal("1234.50"))
        self.assertEqual(paymongo.from_centavos(200000), Decimal("2000.00"))


@override_settings(TESTING=False, PAYMONGO_SECRET_KEY=KEY, PAYMONGO_API_BASE="https://api.paymongo.com", USING_LAPTOP_DB=False)
class HttpCallTests(SimpleTestCase):
    def call(self, response, *args, **kwargs):
        with patch("reservations.paymongo.requests.request", return_value=response) as mocked:
            result = paymongo._request(*args, **kwargs)
        return result, mocked

    def test_the_secret_key_is_the_basic_auth_user_name_with_an_empty_password(self):
        _, mocked = self.call(FakeResponse(200, {"data": {}}), "GET", "/v1/x")
        headers = mocked.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Basic " + base64.b64encode(f"{KEY}:".encode()).decode())
        self.assertEqual(mocked.call_args.args[:2], ("GET", "https://api.paymongo.com/v1/x"))
        self.assertEqual(mocked.call_args.kwargs["timeout"], 15)

    def test_a_refusal_carries_paymongos_own_words_and_never_the_key(self):
        response = FakeResponse(400, {"errors": [{"code": "parameter_invalid", "detail": "payment_method_types is invalid"}]})
        with self.assertRaises(paymongo.PaymongoError) as caught:
            self.call(response, "POST", "/v2/checkout_sessions", {"data": {}})
        self.assertIn("payment_method_types is invalid", str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.status, 400)

    def test_a_busy_paymongo_is_retryable_and_so_is_a_dropped_connection(self):
        with self.assertRaises(paymongo.PaymongoError) as busy:
            self.call(FakeResponse(503, {}), "GET", "/v1/x")
        self.assertTrue(busy.exception.retryable)
        with patch("reservations.paymongo.requests.request", side_effect=paymongo.requests.ConnectionError("boom")):
            with self.assertRaises(paymongo.PaymongoError) as dropped:
                paymongo._request("GET", "/v1/x")
        self.assertTrue(dropped.exception.retryable)
        self.assertNotIn(KEY, str(dropped.exception))

    def test_an_answer_that_is_not_json_is_an_error_not_a_crash(self):
        with self.assertRaises(paymongo.PaymongoError):
            self.call(FakeResponse(200, None, text="<html>"), "GET", "/v1/x")

    def test_the_test_suite_can_never_reach_paymongo_by_accident(self):
        with override_settings(TESTING=True):
            with patch("reservations.paymongo.requests.request") as mocked:
                with self.assertRaises(paymongo.PaymongoError):
                    paymongo._request("GET", "/v1/x")
            mocked.assert_not_called()

    def test_without_a_key_nothing_is_sent(self):
        with override_settings(PAYMONGO_SECRET_KEY=""):
            with patch("reservations.paymongo.requests.request") as mocked:
                with self.assertRaises(paymongo.PaymongoError):
                    paymongo._request("GET", "/v1/x")
            mocked.assert_not_called()


@override_settings(TESTING=False, PAYMONGO_SECRET_KEY=KEY, PAYMONGO_API_BASE="https://api.paymongo.com", USING_LAPTOP_DB=False)
class CheckoutSessionTests(SimpleTestCase):
    def create(self, payload=None, **kwargs):
        args = dict(amount=Decimal("2000"), name="Security deposit", description="Deposit for RSV", reference_number="tok123",
                    success_url="https://shop.test/ok/", cancel_url="https://shop.test/no/", metadata={"attempt": 7, "who": "Ana"},
                    customer_name="Ana Cruz", customer_email="ana@example.com")
        args.update(kwargs)
        with patch("reservations.paymongo._request", return_value=session_payload() if payload is None else payload) as mocked:
            return paymongo.create_checkout_session(**args), mocked

    def test_the_session_asks_for_exactly_the_amount_in_centavos_with_the_three_methods(self):
        session, mocked = self.create()
        method, path, body = mocked.call_args.args
        self.assertEqual((method, path), ("POST", "/v2/checkout_sessions"))
        attributes = body["data"]["attributes"]
        self.assertEqual(attributes["line_items"], [{"name": "Security deposit", "amount": 200000, "currency": "PHP", "quantity": 1, "description": "Deposit for RSV"}])
        self.assertEqual(attributes["payment_method_types"], ["gcash", "paymaya", "qrph"])
        self.assertEqual((attributes["success_url"], attributes["cancel_url"], attributes["reference_number"]), ("https://shop.test/ok/", "https://shop.test/no/", "tok123"))
        self.assertEqual(attributes["metadata"], {"attempt": "7", "who": "Ana"})         # PayMongo accepts string values only
        self.assertNotIn("pass_on_fees", attributes)                                      # the shop absorbs the fee
        self.assertFalse(attributes["send_email_receipt"])
        self.assertEqual(attributes["billing"], {"name": "Ana Cruz", "email": "ana@example.com"})
        self.assertEqual((session.id, session.checkout_url), ("cs_1", "https://checkout.paymongo.com/cs_1_client#pk_test"))

    def test_a_bad_email_is_left_out_of_billing_instead_of_failing_the_payment(self):
        _, mocked = self.create(customer_email="not an email")
        self.assertEqual(mocked.call_args.args[2]["data"]["attributes"]["billing"], {"name": "Ana Cruz"})
        _, mocked = self.create(customer_email="", customer_name="")
        self.assertNotIn("billing", mocked.call_args.args[2]["data"]["attributes"])

    def test_an_amount_under_one_peso_is_refused_before_anything_is_sent(self):
        with patch("reservations.paymongo._request") as mocked:
            with self.assertRaises(paymongo.PaymongoError):
                paymongo.create_checkout_session(amount=Decimal("0.5"), name="x", description="x", reference_number="t", success_url="https://a/", cancel_url="https://a/")
        mocked.assert_not_called()

    def test_a_payment_page_that_is_not_https_is_refused(self):
        bad = session_payload()
        bad["data"]["attributes"]["checkout_url"] = "http://evil.example/pay"
        with self.assertRaises(paymongo.PaymongoError):
            self.create(bad)
        bad["data"]["attributes"]["checkout_url"] = "javascript:alert(1)"
        with self.assertRaises(paymongo.PaymongoError):
            self.create(bad)

    def test_http_is_accepted_only_for_the_local_stand_in_the_browser_tests_use(self):
        local = session_payload()
        local["data"]["attributes"]["checkout_url"] = "http://127.0.0.1:8801/pay/cs_1"
        with self.assertRaises(paymongo.PaymongoError):
            self.create(local)                                                            # a real PayMongo base never allows plain http
        with override_settings(PAYMONGO_API_BASE="http://127.0.0.1:8801"):
            session, _ = self.create(local)
        self.assertEqual(session.checkout_url, "http://127.0.0.1:8801/pay/cs_1")

    def test_an_answer_without_a_session_id_is_an_error(self):
        with self.assertRaises(paymongo.PaymongoError):
            self.create({"data": {"id": "nope", "attributes": {"checkout_url": "https://checkout.paymongo.com/x"}}})
        with self.assertRaises(paymongo.PaymongoError):
            self.create({})


class ReadingASessionTests(SimpleTestCase):
    def test_an_unpaid_session_is_not_paid(self):
        state = paymongo.parse_session(session_payload())
        self.assertEqual((state.paid, state.payment_id, state.status, state.reference_number), (False, "", "active", "tok"))

    def test_the_one_paid_payment_gives_its_amount_method_and_id(self):
        state = paymongo.parse_session(session_payload(paid=True, amount=550000, channel="paymaya", payment_id="pay_77"))
        self.assertEqual((state.paid, state.payment_id, state.amount, state.channel, state.paid_at), (True, "pay_77", Decimal("5500.00"), "paymaya", 1700000100))
        self.assertEqual(state.metadata, {"a": "b"})

    def test_only_a_payment_whose_status_is_paid_counts(self):
        payload = session_payload()
        payload["data"]["attributes"]["payments"] = [
            {"id": "pay_a", "attributes": {"status": "failed", "amount": 200000, "source": {"type": "gcash"}}},
            {"id": "pay_b", "attributes": {"status": "awaiting_next_action", "amount": 200000}},
            "garbage",
        ]
        self.assertFalse(paymongo.parse_session(payload).paid)

    def test_a_session_resource_inside_a_webhook_event_reads_the_same_way(self):
        resource = session_payload(paid=True)["data"]
        self.assertTrue(paymongo.parse_session(resource).paid)


@override_settings(TESTING=False, PAYMONGO_SECRET_KEY=KEY, PAYMONGO_API_BASE="https://api.paymongo.com", USING_LAPTOP_DB=False)
class RetrieveExpireRefundTests(SimpleTestCase):
    def test_a_404_on_the_first_read_address_tries_the_other_documented_one(self):
        calls = []

        def fake(method, path, body=None):
            calls.append(path)
            if path.startswith("/v1/"):
                raise paymongo.PaymongoError("not found", status=404)
            return session_payload(paid=True)

        with patch("reservations.paymongo._request", side_effect=fake):
            state = paymongo.retrieve_checkout_session("cs_1")
        self.assertEqual(calls, ["/v1/checkout_sessions/cs_1", "/v2/checkout_sessions/cs_1"])
        self.assertTrue(state.paid)

    def test_any_other_error_is_not_retried_on_another_address(self):
        with patch("reservations.paymongo._request", side_effect=paymongo.PaymongoError("busy", status=503, retryable=True)) as mocked:
            with self.assertRaises(paymongo.PaymongoError):
                paymongo.retrieve_checkout_session("cs_1")
        self.assertEqual(mocked.call_count, 1)

    def test_expiring_says_what_happened(self):
        with patch("reservations.paymongo._request", return_value={}):
            self.assertEqual(paymongo.expire_checkout_session("cs_1"), "expired")
        cases = {"The session is already expired": "already_expired", "The session already has a paid payment": "has_payment",
                 "a payment that is still pending or processing": "has_payment", "something else": "error"}
        for message, expected in cases.items():
            with self.subTest(message=message):
                with patch("reservations.paymongo._request", side_effect=paymongo.PaymongoError(message, status=400)):
                    self.assertEqual(paymongo.expire_checkout_session("cs_1"), expected)
        with patch("reservations.paymongo._request", side_effect=paymongo.PaymongoError("down", retryable=True)):
            self.assertEqual(paymongo.expire_checkout_session("cs_1"), "error")

    def test_a_refund_is_asked_for_in_centavos_with_a_reason_both_lists_allow(self):
        answer = {"data": {"id": "ref_1", "type": "refund", "attributes": {"status": "pending", "amount": 150000, "payment_id": "pay_1"}}}
        with patch("reservations.paymongo._request", return_value=answer) as mocked:
            refund = paymongo.create_refund(payment_id="pay_1", amount=Decimal("1500"), note="x" * 400)
        method, path, body = mocked.call_args.args
        self.assertEqual((method, path), ("POST", "/v1/refunds"))
        attributes = body["data"]["attributes"]
        self.assertEqual((attributes["payment_id"], attributes["amount"], attributes["reason"]), ("pay_1", 150000, "others"))
        self.assertEqual(len(attributes["notes"]), 255)
        self.assertEqual((refund.id, refund.status, refund.amount, refund.payment_id), ("ref_1", "pending", Decimal("1500.00"), "pay_1"))

    def test_a_refund_under_one_peso_or_without_an_id_is_an_error(self):
        with self.assertRaises(paymongo.PaymongoError):
            paymongo.create_refund(payment_id="pay_1", amount=Decimal("0.50"))
        with patch("reservations.paymongo._request", return_value={"data": {"attributes": {"status": "pending"}}}):
            with self.assertRaises(paymongo.PaymongoError):
                paymongo.create_refund(payment_id="pay_1", amount=Decimal("100"))


def sign(body, secret, *, t="1700000000", field="te"):
    digest = hmac.new(secret.encode(), t.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={t},te={digest if field == 'te' else ''},li={digest if field == 'li' else ''}"


class WebhookSignatureTests(SimpleTestCase):
    SECRET = "whsk_secret"

    def body(self, *, livemode=False, kind="checkout_session.payment.paid"):
        return json.dumps({"data": {"id": "evt_1", "type": "event", "attributes": {
            "type": kind, "livemode": livemode, "data": {"id": "cs_1", "type": "checkout_session", "attributes": {"status": "active"}}}}}).encode()

    def test_a_genuine_test_event_passes_and_its_parts_can_be_read(self):
        body = self.body()
        event = paymongo.verify_webhook(body, sign(body, self.SECRET), self.SECRET)
        self.assertIsNotNone(event)
        self.assertEqual(paymongo.event_type(event), "checkout_session.payment.paid")
        self.assertEqual(paymongo.event_resource(event)["id"], "cs_1")
        self.assertFalse(paymongo.event_livemode(event))

    def test_a_genuine_live_event_is_checked_against_the_live_signature(self):
        body = self.body(livemode=True)
        self.assertIsNotNone(paymongo.verify_webhook(body, sign(body, self.SECRET, field="li"), self.SECRET))

    def test_a_test_signature_can_never_pass_for_a_live_event_nor_the_other_way_round(self):
        live = self.body(livemode=True)
        self.assertIsNone(paymongo.verify_webhook(live, sign(live, self.SECRET, field="te"), self.SECRET))
        test = self.body(livemode=False)
        self.assertIsNone(paymongo.verify_webhook(test, sign(test, self.SECRET, field="li"), self.SECRET))

    def test_a_changed_body_a_wrong_secret_or_a_changed_time_all_fail(self):
        body = self.body()
        header = sign(body, self.SECRET)
        self.assertIsNone(paymongo.verify_webhook(body.replace(b"cs_1", b"cs_2"), header, self.SECRET))
        self.assertIsNone(paymongo.verify_webhook(body, header, "another_secret"))
        self.assertIsNone(paymongo.verify_webhook(body, header.replace("t=1700000000", "t=1700000001"), self.SECRET))

    def test_a_missing_or_broken_header_or_secret_fails_closed(self):
        body = self.body()
        for header, secret in (("", self.SECRET), (None, self.SECRET), ("garbage", self.SECRET), ("t=1,te=,li=", self.SECRET), (sign(body, self.SECRET), "")):
            with self.subTest(header=header, secret=secret):
                self.assertIsNone(paymongo.verify_webhook(body, header, secret))

    def test_a_body_that_is_not_json_fails_even_with_a_matching_signature(self):
        body = b"not json at all"
        self.assertIsNone(paymongo.verify_webhook(body, sign(body, self.SECRET), self.SECRET))

    def test_the_signature_is_of_the_exact_bytes_received(self):
        body = self.body()
        reformatted = json.dumps(json.loads(body), indent=2).encode()
        self.assertIsNone(paymongo.verify_webhook(reformatted, sign(body, self.SECRET), self.SECRET))
