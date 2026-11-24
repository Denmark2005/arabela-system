"""The practice payment gateway (reservations/demo_gateway.py) and the guards that keep it away from the real shop.

The one promise: a practice payment can NEVER make a booking in the live database. So the shop accepts a practice gateway address only when it is a
local address AND the shop is the practice shop, a laptop copy of the database, the test suite, or has been told so on purpose; and the practice
routes (/demo/...) do not exist anywhere else."""
import base64
import json
import urllib.error
import urllib.request

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import NoReverseMatch, reverse

from reservations import demo_gateway, paymongo

KEY = "sk_test_demo"
LOCAL = "http://127.0.0.1:8801"


class GuardTests(SimpleTestCase):
    def allowed(self, **settings_):
        base = {"PAYMONGO_API_BASE": LOCAL, "DEMO_MODE": False, "USING_LAPTOP_DB": False, "TESTING": False, "PAYMONGO_ALLOW_LOCAL_GATEWAY": False}
        base.update(settings_)
        with override_settings(**base):
            return paymongo.api_base_allowed()

    def test_the_real_paymongo_address_is_always_fine(self):
        for flags in ({}, {"DEMO_MODE": True}, {"USING_LAPTOP_DB": True}):
            self.assertTrue(self.allowed(PAYMONGO_API_BASE="https://api.paymongo.com", **flags))

    def test_a_local_gateway_on_the_live_shop_is_refused(self):
        self.assertFalse(self.allowed())                                                  # not the practice shop, not a laptop copy, not a test, not asked for

    def test_a_local_gateway_is_accepted_only_where_it_cannot_book_into_the_live_database(self):
        self.assertTrue(self.allowed(DEMO_MODE=True))
        self.assertTrue(self.allowed(USING_LAPTOP_DB=True))
        self.assertTrue(self.allowed(TESTING=True))
        self.assertTrue(self.allowed(PAYMONGO_ALLOW_LOCAL_GATEWAY=True))

    def test_an_address_that_is_not_this_computer_is_never_accepted(self):
        for base in ("http://evil.example", "http://evil.example:8801", "https://127.0.0.1:8801", "http://127.0.0.1.evil.example", "http://127.0.0.1:8801/path",
                     "http://192.168.1.5:8801", "ftp://127.0.0.1", "http://localhost.evil.example"):
            with self.subTest(base=base):
                self.assertFalse(self.allowed(PAYMONGO_API_BASE=base, DEMO_MODE=True, USING_LAPTOP_DB=True, TESTING=True, PAYMONGO_ALLOW_LOCAL_GATEWAY=True))

    def test_localhost_and_127_with_or_without_a_port_are_local(self):
        for base in ("http://127.0.0.1", "http://127.0.0.1:8801", "http://localhost", "http://localhost:9000"):
            with self.subTest(base=base):
                self.assertTrue(self.allowed(PAYMONGO_API_BASE=base, DEMO_MODE=True))

    def test_enabled_follows_the_guard(self):
        with override_settings(PAYMONGO_SECRET_KEY=KEY, PAYMONGO_API_BASE=LOCAL, DEMO_MODE=False, USING_LAPTOP_DB=False, TESTING=False, PAYMONGO_ALLOW_LOCAL_GATEWAY=False):
            self.assertFalse(paymongo.enabled())                                          # the shop would silently use the old checkout instead
        with override_settings(PAYMONGO_SECRET_KEY=KEY, PAYMONGO_API_BASE=LOCAL, DEMO_MODE=True):
            self.assertTrue(paymongo.enabled())
            self.assertTrue(paymongo.is_demo())
        with override_settings(PAYMONGO_SECRET_KEY=KEY, PAYMONGO_API_BASE="https://api.paymongo.com"):
            self.assertFalse(paymongo.is_demo())

    def test_a_live_key_is_still_never_used_on_a_laptop_copy_even_with_a_local_gateway(self):
        with override_settings(PAYMONGO_SECRET_KEY="sk_live_x", PAYMONGO_API_BASE=LOCAL, USING_LAPTOP_DB=True):
            self.assertFalse(paymongo.enabled())

    def test_the_gateway_only_listens_on_this_computer(self):
        for host in ("0.0.0.0", "192.168.1.5", "example.com", ""):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    demo_gateway.make_server(host, 0, KEY)


class PracticeRoutesDoNotExistOnTheRealShopTests(TestCase):
    def test_the_demo_pages_and_sign_in_links_are_404_and_have_no_name(self):
        for path in ("/demo/", "/demo/signin/customer/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        for name in ("demo_start", "demo_signin_customer", "demo_signin_owner"):
            with self.assertRaises(NoReverseMatch):
                reverse(name)

    def test_nobody_gets_signed_in_by_visiting_them(self):
        self.client.get("/demo/signin/customer/")
        self.client.get("/admin-panel/demo-signin/")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertNotIn("_auth_user_id", self.client.cookies)

    def test_the_demo_view_itself_refuses_when_demo_mode_is_off(self):
        from django.http import Http404
        from django.test import RequestFactory
        from arabela_system import demo
        request = RequestFactory().get("/demo/")
        for view in (demo.start_page, demo.signin_customer, demo.signin_owner):
            with self.assertRaises(Http404):
                view(request)


class FakeGatewayHttpTests(SimpleTestCase):
    """The practice gateway answers the way PayMongo's documentation says (a real server on a free port, this computer only)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = demo_gateway.start_in_background("127.0.0.1", 0, KEY)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    def call(self, method, path, body=None, key=KEY, raw=None, follow=False):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{key}:".encode()).decode()
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(*([] if follow else [NoRedirect]))
        try:
            with opener.open(request, timeout=10) as response:
                return response.status, response.read().decode(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode(), dict(error.headers)

    def make_session(self, amount=200000):
        status, text, _ = self.call("POST", "/v2/checkout_sessions", {"data": {"attributes": {
            "line_items": [{"name": "Security deposit", "amount": amount, "currency": "PHP", "quantity": 1}], "payment_method_types": ["gcash", "paymaya", "qrph"],
            "success_url": "http://127.0.0.1:8000/ok/", "cancel_url": "http://127.0.0.1:8000/no/", "reference_number": "tok", "metadata": {"a": "b"}}}})
        self.assertEqual(status, 200, text)
        return json.loads(text)["data"]["id"]

    def test_the_wrong_key_or_none_is_refused(self):
        self.assertEqual(self.call("POST", "/v2/checkout_sessions", {"data": {}}, key="sk_test_other")[0], 401)
        self.assertEqual(self.call("POST", "/v2/checkout_sessions", {"data": {}}, key=None)[0], 401)
        self.assertEqual(self.call("GET", "/v1/checkout_sessions/cs_x", key="nope")[0], 401)

    def test_a_session_is_created_read_paid_and_closed_like_paymongo_says(self):
        sid = self.make_session()
        self.assertTrue(sid.startswith("cs_"))
        status, text, _ = self.call("GET", f"/v1/checkout_sessions/{sid}")
        data = json.loads(text)["data"]
        self.assertEqual((status, data["attributes"]["status"], data["attributes"]["payments"]), (200, "active", []))
        status, text, headers = self.call("POST", f"/pay/{sid}/authorize", raw=b"channel=paymaya")
        self.assertEqual((status, headers.get("Location")), (302, "http://127.0.0.1:8000/ok/"))
        payments = json.loads(self.call("GET", f"/v1/checkout_sessions/{sid}")[1])["data"]["attributes"]["payments"]
        self.assertEqual(len(payments), 1)
        self.assertEqual((payments[0]["attributes"]["status"], payments[0]["attributes"]["amount"], payments[0]["attributes"]["source"]["type"]), ("paid", 200000, "paymaya"))
        status, text, _ = self.call("POST", f"/v1/checkout_sessions/{sid}/expire")
        self.assertEqual(status, 400)
        self.assertIn("already has a paid payment", text)

    def test_an_unpaid_session_can_be_closed_once_and_then_cannot_be_paid(self):
        sid = self.make_session()
        self.assertEqual(self.call("POST", f"/v1/checkout_sessions/{sid}/expire")[0], 200)
        status, text, _ = self.call("POST", f"/v1/checkout_sessions/{sid}/expire")
        self.assertEqual(status, 400)
        self.assertIn("already expired", text)
        self.assertEqual(self.call("POST", f"/pay/{sid}/authorize", raw=b"channel=gcash")[0], 200)                    # a page saying it is closed, no redirect
        self.assertEqual(json.loads(self.call("GET", f"/v1/checkout_sessions/{sid}")[1])["data"]["attributes"]["payments"], [])

    def test_a_payment_can_only_be_made_once(self):
        sid = self.make_session()
        self.call("POST", f"/pay/{sid}/authorize", raw=b"channel=gcash")
        self.call("POST", f"/pay/{sid}/authorize", raw=b"channel=qrph")
        payments = json.loads(self.call("GET", f"/v1/checkout_sessions/{sid}")[1])["data"]["attributes"]["payments"]
        self.assertEqual([p["attributes"]["source"]["type"] for p in payments], ["gcash"])

    def test_an_unknown_channel_falls_back_and_failing_charges_nothing(self):
        sid = self.make_session()
        self.call("POST", f"/pay/{sid}/fail", raw=b"")
        self.assertEqual(json.loads(self.call("GET", f"/v1/checkout_sessions/{sid}")[1])["data"]["attributes"]["payments"], [])
        self.call("POST", f"/pay/{sid}/authorize", raw=b"channel=<script>")
        payments = json.loads(self.call("GET", f"/v1/checkout_sessions/{sid}")[1])["data"]["attributes"]["payments"]
        self.assertEqual(payments[0]["attributes"]["source"]["type"], "gcash")

    def test_the_payment_page_says_it_is_a_practice_and_shows_the_exact_amount_and_escapes_everything(self):
        sid = self.make_session(amount=550000)
        status, page, _ = self.call("GET", f"/pay/{sid}", key=None)
        self.assertEqual(status, 200)
        for needed in ("DEMO PAYMENT PAGE", "not PayMongo", "PHP 5,500.00", "GCash", "Maya", "QR Ph", 'id="dp-authorize"', 'id="dp-fail"'):
            self.assertIn(needed, page)
        self.assertEqual(self.call("GET", "/pay/cs_nope", key=None)[0], 404)

    def test_a_session_without_its_return_addresses_is_refused(self):
        status, text, _ = self.call("POST", "/v2/checkout_sessions", {"data": {"attributes": {"line_items": [{"name": "x", "amount": 100}]}}})
        self.assertEqual(status, 400)
        self.assertEqual(self.call("POST", "/v2/checkout_sessions", raw=b"not json")[0], 400)

    def test_a_refund_is_recorded_and_can_be_read(self):
        status, text, _ = self.call("POST", "/v1/refunds", {"data": {"attributes": {"payment_id": "pay_1", "amount": 150000, "reason": "others", "notes": "n"}}})
        refund = json.loads(text)["data"]
        self.assertEqual((status, refund["attributes"]["status"], refund["attributes"]["amount"]), (200, "pending", 150000))
        self.assertEqual(json.loads(self.call("GET", f"/v1/refunds/{refund['id']}")[1])["data"]["attributes"]["status"], "succeeded")

    def test_the_shops_own_client_works_against_it_end_to_end(self):
        """The shop's real connection code (reservations/paymongo.py) against this gateway: create, read paid, expire, refund."""
        with override_settings(TESTING=False, PAYMONGO_SECRET_KEY=KEY, PAYMONGO_API_BASE=self.base, DEMO_MODE=True):
            session = paymongo.create_checkout_session(amount=2000, name="Security deposit", description="d", reference_number="tok9",
                                                       success_url="http://127.0.0.1:8000/ok/", cancel_url="http://127.0.0.1:8000/no/", metadata={"attempt": 1})
            self.assertTrue(session.checkout_url.startswith(self.base + "/pay/cs_"))
            state = paymongo.retrieve_checkout_session(session.id)
            self.assertEqual((state.paid, state.status, state.reference_number), (False, "active", "tok9"))
            self.call("POST", f"/pay/{session.id}/authorize", raw=b"channel=qrph")
            state = paymongo.retrieve_checkout_session(session.id)
            self.assertEqual((state.paid, state.amount, state.channel), (True, paymongo.from_centavos(200000), "qrph"))
            self.assertEqual(paymongo.expire_checkout_session(session.id), "has_payment")
            refund = paymongo.create_refund(payment_id=state.payment_id, amount=1500, note="x")
            self.assertEqual((refund.status, refund.amount), ("pending", paymongo.from_centavos(150000)))
            other = paymongo.create_checkout_session(amount=100, name="n", description="d", reference_number="tok10", success_url="http://127.0.0.1:8000/ok/", cancel_url="http://127.0.0.1:8000/no/")
            self.assertEqual(paymongo.expire_checkout_session(other.id), "expired")
            self.assertEqual(paymongo.expire_checkout_session(other.id), "already_expired")
