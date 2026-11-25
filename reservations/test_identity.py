"""ID verification (reservations/didit.py, identity.py, identity_photos.py): the connector, the rules, starting a check, reading the result back, the owner's decision,
walk-in checks, the pictures and the cleanup. Didit is replaced by FakeDidit (nothing reaches the network) and the pictures go to a temporary folder."""
import io
import json
import shutil
import tempfile
import time as time_module
from datetime import date, timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from PIL import Image

from accounts.models import CustomerMessage, UserProfile
from gowns.models import SiteSettings
from reservations import didit, identity, identity_photos
from reservations.models import IdentityCheck, IdentityPhoto, Reservation, ReservationItem

User = get_user_model()
S = IdentityCheck.Status
TMP = tempfile.mkdtemp(prefix="arabela-id-test-")
STORAGES = {**settings.STORAGES, "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}}
SETTINGS = dict(DIDIT_API_KEY="sk_test_unit", DIDIT_WORKFLOW_ID="wf-unit-0001", DIDIT_WEBHOOK_SECRET="whsec_unit", STORAGES=STORAGES, MEDIA_ROOT=TMP, USING_LAPTOP_DB=False)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def jpeg_bytes(size=(800, 500), color="white", exif=False):
    out = io.BytesIO()
    picture = Image.new("RGB", size, color)
    if exif:
        data = Image.Exif()
        data[0x010F] = "Camera Maker"             # Make
        data[0x0112] = 6                          # Orientation: rotated
        picture.save(out, format="JPEG", exif=data)
    else:
        picture.save(out, format="JPEG")
    return out.getvalue()


def set_switch(on):
    """The owner's switch (the settings row is created on first read, so it is made first)."""
    shop = SiteSettings.load()
    shop.require_id_before_payment = on
    shop.save(update_fields=["require_id_before_payment", "updated_at"])


class FakeDidit:
    """What Didit does, in memory: sessions (one unfinished per vendor_data), decisions with picture links, and the picture downloads."""

    def __init__(self):
        self.sessions, self.calls, self.fail, self.bad_pictures, self.n = {}, [], {}, set(), 0

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        name = "create_session" if (method == "POST" and path == "/v3/session/") else ("read_decision" if (method == "GET" and path.endswith("/decision/")) else "other")
        if name in self.fail:
            raise self.fail[name]
        if name == "create_session":
            for sid, s in self.sessions.items():
                if s["vendor_data"] == body["vendor_data"] and s["status"] in ("Not Started", "In Progress"):
                    return self._created(sid)
            self.n += 1
            sid = f"00000000-0000-4000-8000-{self.n:012d}"
            self.sessions[sid] = {"vendor_data": body["vendor_data"], "status": "Not Started", "callback": body["callback"], "report": None, "warnings": [], "images": True}
            return self._created(sid)
        if name == "read_decision":
            return self._decision(path.split("/")[3])
        raise AssertionError(f"unexpected Didit call {method} {path}")

    def _created(self, sid):
        s = self.sessions[sid]
        return {"session_id": sid, "session_token": sid[-6:], "url": f"https://verify.didit.me/session/{sid[-12:]}", "expires_at": (timezone.now() + timedelta(days=7)).isoformat(),
                "status": s["status"], "vendor_data": s["vendor_data"], "callback": s["callback"], "workflow_id": "wf-unit-0001"}

    def _decision(self, sid):
        s = self.sessions[sid]
        out = {"session_id": sid, "status": s["status"], "vendor_data": s["vendor_data"], "id_verifications": None, "decision_trace": {"status_warnings": s["warnings"]}}
        if s["report"]:
            report = dict(s["report"])
            if s["images"]:
                report["front_image"] = f"https://media.didit.me/{sid}/front.jpg?sig=abc"
                report["back_image"] = f"https://media.didit.me/{sid}/back.jpg?sig=abc"
            out["id_verifications"] = [report]
        return out

    def finish(self, sid, status, *, document_type="Identity Card", name="ANA MARIE CRUZ", warnings=None, images=True, country="PHL"):
        s = self.sessions[sid]
        s["status"], s["images"] = status, images
        s["report"] = {"status": status, "document_type": document_type, "issuing_state": country, "full_name": name, "warnings": []}
        s["warnings"] = warnings or []

    def download(self, url):
        if any(bad in url for bad in self.bad_pictures):
            raise didit.DiditError("A picture link answered 500.", status=500, retryable=True)
        return jpeg_bytes()

    def last_session(self):
        return list(self.sessions)[-1]


@override_settings(**SETTINGS)
class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ana = User.objects.create_user("id_ana", "id.ana@gmail.com", "x", first_name="Ana")
        UserProfile.objects.update_or_create(user=cls.ana, defaults={"display_name": "Ana Cruz"})
        cls.ben = User.objects.create_user("id_ben", "id.ben@gmail.com", "x", first_name="Ben")
        cls.staff = User.objects.create_user("id_staff", password="x", is_staff=True, first_name="Maria", last_name="Cruz")
        cls.owner = User.objects.create_superuser("id_owner", "id.owner@gmail.com", "x")

    def setUp(self):
        self.fake = FakeDidit()
        for target, kwargs in (("reservations.didit._request", {"side_effect": self.fake}), ("reservations.didit.download_picture", {"side_effect": self.fake.download})):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.callbacks = []

    def callback_for(self, token):
        self.callbacks.append(token)
        return f"https://shop.example/identity/return/{token}/"

    def start(self, user=None, **kwargs):
        kwargs.setdefault("id_type", "philid")
        kwargs.setdefault("consent", True)
        return identity.start_for_customer(user=user or self.ana, callback_for=self.callback_for, **kwargs)

    def poll(self, check):
        IdentityCheck.objects.filter(pk=check.pk).update(last_checked_at=None)
        return identity.refresh(check.pk, source="poll")

    def verified(self, user=None, **finish):
        """A customer who went through the whole thing: the check, approved."""
        started = self.start(user)
        check = IdentityCheck.objects.get(token=started["token"])
        self.fake.finish(check.session_id, "Approved", **finish)
        return self.poll(check)


# ---------------------------------------------------------------- the connector
class ConnectorTests(Base):
    def test_a_sandbox_key_is_test_mode_and_a_missing_key_switches_everything_off(self):
        self.assertEqual((didit.mode(), didit.is_test(), didit.enabled()), ("test", True, True))
        with override_settings(DIDIT_API_KEY=""):
            self.assertEqual((didit.mode(), didit.enabled()), ("", False))
        with override_settings(DIDIT_WORKFLOW_ID=""):
            self.assertFalse(didit.enabled())
        with override_settings(DIDIT_API_KEY="sk_live_x"):
            self.assertEqual((didit.mode(), didit.enabled()), ("live", True))
        with override_settings(DIDIT_API_KEY="sk_live_x", USING_LAPTOP_DB=True):
            self.assertFalse(didit.enabled())                                       # a live key is never used against a laptop copy of the database

    def test_creating_a_session_sends_only_opaque_references_and_returns_a_secure_page(self):
        session = didit.create_session(vendor_data="c7", callback="https://shop.example/back/", metadata={"check": 1})
        sent = self.fake.calls[0][2]
        self.assertEqual((sent["workflow_id"], sent["vendor_data"], sent["callback"], sent["metadata"]), ("wf-unit-0001", "c7", "https://shop.example/back/", {"check": 1}))
        self.assertNotIn("sandbox_scenario", sent)
        self.assertTrue(session.url.startswith("https://verify.didit.me/session/"))
        self.assertIsNotNone(session.expires_at)

    def test_a_sandbox_outcome_can_be_chosen_up_front_but_only_in_test_mode(self):
        didit.create_session(vendor_data="c1", callback="https://x/", scenario="decline_document_expired")
        self.assertEqual(self.fake.calls[-1][2]["sandbox_scenario"], "decline_document_expired")
        with override_settings(DIDIT_API_KEY="sk_live_x"):
            didit.create_session(vendor_data="c2", callback="https://x/", scenario="approve")
        self.assertNotIn("sandbox_scenario", self.fake.calls[-1][2])

    def test_a_verification_page_that_is_not_a_secure_didit_address_is_refused(self):
        for url in ("http://verify.didit.me/session/x", "https://evil.example/session/x", "https://didit.me.evil.example/x", ""):
            with self.subTest(url=url), patch("reservations.didit._request", return_value={"session_id": "00000000-0000-4000-8000-000000000999", "url": url}):
                with self.assertRaises(didit.DiditError):
                    didit.create_session(vendor_data="c1", callback="https://x/")

    def test_a_session_without_an_id_is_refused(self):
        with patch("reservations.didit._request", return_value={"url": "https://verify.didit.me/session/x"}), self.assertRaises(didit.DiditError):
            didit.create_session(vendor_data="c1", callback="https://x/")

    def test_a_decision_is_read_into_plain_fields_and_the_reasons_are_plain_words(self):
        payload = {"session_id": "s1", "status": "declined", "vendor_data": "c1", "decision_trace": {"status_warnings": [
            {"risk": "LOW_FACE_MATCH_SIMILARITY", "log_type": "error", "short_description": "Low similarity", "additional_data": {"id_number": "SECRET"}},
            {"risk": "SOMETHING_NEW", "log_type": "warning", "short_description": "Odd <b>thing</b> happened"},
            {"risk": "FYI", "log_type": "information", "short_description": "Just so you know"}]},
            "id_verifications": [{"document_type": "Passport", "issuing_state": "phl", "full_name": "ANA CRUZ", "front_image": "https://m.didit.me/f.jpg", "back_image": None,
                                  "document_number": "P1234567"}]}
        decision = didit.parse_decision(payload)
        self.assertEqual((decision.status, decision.document_type, decision.document_country, decision.name), ("Declined", "Passport", "PHL", "ANA CRUZ"))
        self.assertEqual(decision.reasons, ["The selfie does not match the picture on the ID.", "Odd bthingb happened"])           # markup characters are dropped
        self.assertEqual((decision.front_url, decision.back_url), ("https://m.didit.me/f.jpg", ""))
        self.assertNotIn("SECRET", json.dumps(decision.reasons))                   # nothing from additional_data ever becomes a reason
        self.assertNotIn("P1234567", json.dumps(decision.reasons))

    def test_the_warnings_of_a_real_sandbox_decline_read_as_one_plain_sentence(self):
        # The shape a REAL Didit sandbox session gave (a virtual camera was used): one error and two "information" notes.
        payload = {"session_id": "s1", "status": "Declined", "decision_trace": {"status_warnings": [
            {"risk": "INJECTION_ATTACK_DETECTED", "log_type": "error", "short_description": "Injection attack detected"}]},
            "id_verifications": [{"status": "Declined", "document_type": "Passport", "issuing_state": "BRA", "warnings": [
                {"risk": "INJECTION_ATTACK_DETECTED", "log_type": "error", "short_description": "Injection attack detected"},
                {"risk": "VIRTUAL_CAMERA_DETECTED", "log_type": "information", "short_description": "Virtual camera detected"},
                {"risk": "FRAME_INJECTION_SUSPECTED", "log_type": "information", "short_description": "Frame injection suspected"}]}]}
        self.assertEqual(didit.parse_decision(payload).reasons, ["The camera check failed. Please try again with the phone's own camera."])

    def test_statuses_are_matched_without_regard_to_case_and_waiting_ones_are_waiting(self):
        for raw, wanted in (("APPROVED", "Approved"), ("in review", "In Review"), ("not started", "Not Started"), ("Abandoned", "Abandoned")):
            self.assertEqual(didit.parse_decision({"status": raw}).status, wanted)
        self.assertTrue(didit.parse_decision({"status": "In Progress"}).is_waiting)
        self.assertFalse(didit.parse_decision({"status": "Approved"}).is_waiting)

    def test_reading_a_decision_refuses_a_session_id_that_could_change_the_address(self):
        for bad in ("", "../x", "a/b", "x" * 100, "ab"):
            with self.subTest(bad=bad), self.assertRaises(didit.DiditError):
                didit.retrieve_decision(bad)

    def test_the_test_suite_can_never_reach_didit(self):
        patch.stopall()
        with override_settings(TESTING=True), self.assertRaises(didit.DiditError):
            didit._request("GET", "/v3/session/x/decision/")


class WebhookTests(Base):
    def event(self, **extra):
        return {"event_id": "e1", "webhook_type": "status.updated", "environment": "sandbox", "session_id": "s-1", "status": "Approved", "vendor_data": "c1", "decision": {"x": 1.0}, **extra}

    def request(self, body_dict=None, *, secret="whsec_unit", stamp=None, headers=None, raw=None):
        body = raw if raw is not None else json.dumps(body_dict or self.event()).encode()
        data = body_dict or self.event()
        extra = {"HTTP_X_TIMESTAMP": str(int(time_module.time()) if stamp is None else stamp)}
        if secret is not None:
            extra["HTTP_X_SIGNATURE_V2"] = didit.sign(data, secret)
        for key, value in (headers or {}).items():
            extra[key] = value
        return RequestFactory().post("/didit/webhook/", data=body, content_type="application/json", **extra)

    def test_a_genuine_webhook_is_accepted(self):
        self.assertEqual(didit.event_session_id(didit.verify_webhook(self.request())), "s-1")

    def test_the_signature_is_over_the_sorted_compact_json_with_whole_floats_as_integers(self):
        self.assertEqual(didit.canonical_json({"b": 1.0, "a": [2.0, "é"]}), '{"a":[2,"é"],"b":1}')

    def test_a_webhook_signed_with_another_secret_is_refused(self):
        self.assertIsNone(didit.verify_webhook(self.request(secret="not-the-secret")))

    def test_a_webhook_with_a_changed_body_is_refused(self):
        request = self.request()
        tampered = RequestFactory().post("/didit/webhook/", data=json.dumps(self.event(status="Declined")).encode(), content_type="application/json",
                                         HTTP_X_TIMESTAMP=str(int(time_module.time())), HTTP_X_SIGNATURE_V2=request.headers["X-Signature-V2"])
        self.assertIsNone(didit.verify_webhook(tampered))

    def test_an_old_or_missing_or_odd_timestamp_is_refused(self):
        self.assertIsNone(didit.verify_webhook(self.request(stamp=int(time_module.time()) - 3600)))
        self.assertIsNone(didit.verify_webhook(self.request(stamp=int(time_module.time()) + 3600)))
        request = self.request()
        del request.META["HTTP_X_TIMESTAMP"]
        self.assertIsNone(didit.verify_webhook(request))
        self.assertIsNone(didit.verify_webhook(self.request(headers={"HTTP_X_TIMESTAMP": "soon"})))

    def test_with_no_secret_set_every_webhook_is_refused(self):
        with override_settings(DIDIT_WEBHOOK_SECRET=""):
            self.assertIsNone(didit.verify_webhook(self.request()))

    def test_no_signature_or_garbage_is_refused(self):
        self.assertIsNone(didit.verify_webhook(self.request(secret=None)))
        self.assertIsNone(didit.verify_webhook(self.request(headers={"HTTP_X_SIGNATURE_V2": "zz"})))
        self.assertIsNone(didit.verify_webhook(RequestFactory().post("/x", data=b"not json", content_type="application/json", HTTP_X_TIMESTAMP=str(int(time_module.time())))))
        self.assertIsNone(didit.verify_webhook(RequestFactory().post("/x", data=b"[1,2]", content_type="application/json", HTTP_X_TIMESTAMP=str(int(time_module.time())))))

    def test_the_raw_body_signature_is_accepted_too_and_the_previous_secret_signature_during_a_rotation(self):
        import hashlib
        import hmac
        body = json.dumps(self.event()).encode()
        raw_sig = hmac.new(b"whsec_unit", body, hashlib.sha256).hexdigest()
        request = RequestFactory().post("/x", data=body, content_type="application/json", HTTP_X_TIMESTAMP=str(int(time_module.time())), HTTP_X_SIGNATURE=raw_sig)
        self.assertIsNotNone(didit.verify_webhook(request))
        previous = RequestFactory().post("/x", data=body, content_type="application/json", HTTP_X_TIMESTAMP=str(int(time_module.time())),
                                         HTTP_X_SIGNATURE_V2_PREVIOUS=didit.sign(self.event(), "whsec_unit"))
        self.assertIsNotNone(didit.verify_webhook(previous))


class DownloadTests(Base):
    class Response:
        def __init__(self, chunks=(b"abc",), status=200, headers=None, url="https://media.didit.me/x.jpg"):
            self.status_code, self.headers, self.url, self._chunks = status, headers or {"Content-Type": "image/jpeg"}, url, chunks

        def iter_content(self, chunk_size=0):
            return iter(self._chunks)

        def close(self):
            pass

    def get(self, response, url="https://media.didit.me/x.jpg"):
        patch.stopall()                                           # the real download_picture, with the web call replaced
        with patch("reservations.didit.requests.get", return_value=response) as mocked:
            return didit.download_picture(url), mocked

    def test_a_good_picture_is_read_without_sending_the_key(self):
        data, mocked = self.get(self.Response())
        self.assertEqual(data, b"abc")
        self.assertNotIn("headers", mocked.call_args.kwargs)

    def test_a_link_that_is_not_https_is_refused(self):
        patch.stopall()
        for url in ("http://media.didit.me/x.jpg", "ftp://x/y", "", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(didit.DiditError):
                didit.download_picture(url)

    def test_something_that_is_not_a_picture_or_is_too_big_or_empty_or_errors_is_refused(self):
        for response in (self.Response(headers={"Content-Type": "text/html"}), self.Response(chunks=(b"x" * (didit.MAX_PICTURE_BYTES + 1),)), self.Response(chunks=()),
                         self.Response(status=500), self.Response(url="http://insecure/x")):
            with self.subTest(status=response.status_code), self.assertRaises(didit.DiditError):
                self.get(response)

    def test_the_key_is_sent_only_to_didit_itself_and_only_when_it_asks(self):
        patch.stopall()
        first, second = self.Response(status=403), self.Response()
        with patch("reservations.didit.requests.get", side_effect=[first, second]) as mocked:
            self.assertEqual(didit.download_picture("https://media.didit.me/x.jpg"), b"abc")
        self.assertEqual(mocked.call_args.kwargs["headers"], {"x-api-key": "sk_test_unit"})
        with patch("reservations.didit.requests.get", return_value=self.Response(status=403)) as mocked, self.assertRaises(didit.DiditError):
            didit.download_picture("https://some-bucket.example.com/x.jpg")
        self.assertEqual(mocked.call_count, 1)                    # another host: refused, the key is never sent there


# ---------------------------------------------------------------- the rules
class RuleTests(Base):
    def test_the_owners_switch_and_didit_decide_who_must_verify(self):
        self.assertTrue(SiteSettings.load().require_id_before_payment)             # on by default
        self.assertEqual((identity.required_online(), identity.required_walkin()), (True, True))
        with override_settings(DIDIT_API_KEY=""):
            self.assertEqual((identity.required_online(), identity.required_walkin()), (False, True))   # no Didit: nobody can verify online; staff still check at the counter
        set_switch(False)
        self.assertEqual((identity.required_online(), identity.required_walkin()), (False, False))

    def test_the_gate_turns_an_unverified_customer_away_and_lets_a_verified_one_through(self):
        refusal = identity.online_gate(self.ana)
        self.assertEqual((refusal.status, refusal.extra), (403, {"id_required": True}))
        self.verified()
        self.assertIsNone(identity.online_gate(self.ana))
        self.assertIsNotNone(identity.online_gate(self.ben))                          # only the account that verified
        set_switch(False)
        self.assertIsNone(identity.online_gate(self.ben))

    def test_a_verified_id_counts_for_twelve_months_then_the_customer_is_asked_again(self):
        check = self.verified()
        self.assertTrue(check.is_valid)
        self.assertAlmostEqual((check.expires_at - check.verified_at).days, 365, delta=1)
        IdentityCheck.objects.filter(pk=check.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertFalse(identity.is_verified(self.ana))
        self.assertEqual(identity.customer_state(self.ana)["state"], "expired")
        self.assertIsNotNone(identity.online_gate(self.ana))
        again = self.start()                                                          # a fresh check, a fresh Didit page
        self.assertEqual(again["state"], "checking")
        self.assertNotEqual(again["token"], check.token)

    def test_the_customers_state_tells_each_stage_in_plain_words(self):
        self.assertEqual(identity.customer_state(self.ana)["state"], "none")
        started = self.start()
        state = identity.customer_state(self.ana)
        self.assertEqual((state["state"], state["url"]), ("checking", started["url"]))
        check = IdentityCheck.objects.get(token=started["token"])
        self.fake.finish(check.session_id, "Declined", warnings=[{"risk": "DOCUMENT_EXPIRED", "log_type": "error", "short_description": "Expired"}])
        self.poll(check)
        state = identity.customer_state(self.ana)
        self.assertEqual((state["state"], state["reasons"]), ("declined", ["The ID has expired."]))
        self.assertEqual(identity.customer_state(None)["state"], "none")


# ---------------------------------------------------------------- starting a check
class StartTests(Base):
    def test_pressing_verify_opens_one_check_and_one_didit_page(self):
        started = self.start()
        check = IdentityCheck.objects.get()
        self.assertEqual((started["state"], started["url"]), ("checking", check.session_url))
        self.assertEqual((check.source, check.status, check.customer, check.id_type, check.subject_name), ("online", "pending", self.ana, "philid", "Ana Cruz"))
        self.assertIsNotNone(check.consent_at)
        self.assertTrue(check.session_url.startswith("https://verify.didit.me/session/"))
        sent = self.fake.calls[0][2]
        self.assertEqual(sent["vendor_data"], f"c{self.ana.pk}")                      # an opaque reference, never a name or an email
        self.assertNotIn("Ana", json.dumps(sent))
        self.assertEqual(sent["callback"], f"https://shop.example/identity/return/{check.token}/")

    def test_spamming_the_button_is_the_same_check_and_never_a_second_page(self):
        first = self.start()
        for _ in range(6):
            again = self.start()
            self.assertEqual((again["state"], again["token"], again["url"]), ("checking", first["token"], first["url"]))
        self.assertEqual(IdentityCheck.objects.count(), 1)
        self.assertEqual(sum(1 for call in self.fake.calls if call[0] == "POST"), 1)

    def test_choosing_a_different_id_type_on_the_second_press_keeps_the_same_check(self):
        self.start(id_type="philid")
        self.start(id_type="passport")
        self.assertEqual(IdentityCheck.objects.get().id_type, "passport")

    def test_the_agreement_and_a_real_id_type_are_required(self):
        for kwargs in (dict(consent=False), dict(consent=None), dict(consent="yes"), dict(id_type="library_card"), dict(id_type="")):
            with self.subTest(kwargs=kwargs), self.assertRaises(identity.IdentityError) as caught:
                self.start(**kwargs)
            self.assertEqual(caught.exception.status, 400)
        self.assertFalse(IdentityCheck.objects.exists())
        self.assertEqual(self.fake.calls, [])

    def test_it_is_refused_politely_when_didit_is_not_set_up(self):
        with override_settings(DIDIT_API_KEY=""), self.assertRaises(identity.IdentityError) as caught:
            self.start()
        self.assertEqual(caught.exception.status, 503)

    def test_pressing_far_too_often_is_slowed_down(self):
        with patch("reservations.identity.rate_limit.allowed", return_value=False), self.assertRaises(identity.IdentityError) as caught:
            self.start()
        self.assertEqual(caught.exception.status, 429)
        self.assertFalse(IdentityCheck.objects.exists())

    def test_didit_being_down_leaves_nothing_half_made_and_the_next_press_works(self):
        self.fake.fail["create_session"] = didit.DiditError("down", status=503, retryable=True)
        with self.assertRaises(identity.IdentityError) as caught:
            self.start()
        self.assertEqual(caught.exception.status, 502)
        check = IdentityCheck.objects.get()
        self.assertEqual(check.status, S.CANCELLED)                                   # closed, so it cannot block the next press
        del self.fake.fail["create_session"]
        self.assertEqual(self.start()["state"], "checking")
        self.assertEqual(IdentityCheck.objects.filter(status=S.PENDING).count(), 1)

    def test_a_press_while_the_other_one_is_still_opening_is_told_to_wait(self):
        IdentityCheck.objects.create(token="t" * 32, source="online", customer=self.ana, id_type="philid")        # no page yet: another press is opening it
        with self.assertRaises(identity.IdentityError) as caught:
            self.start()
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(IdentityCheck.objects.count(), 1)

    def test_an_already_verified_customer_is_told_so_and_no_new_page_is_opened(self):
        self.verified()
        posts = sum(1 for call in self.fake.calls if call[0] == "POST")
        answer = self.start()
        self.assertEqual(answer["state"], "verified")
        self.assertEqual(sum(1 for call in self.fake.calls if call[0] == "POST"), posts)

    def test_a_customer_who_finished_while_away_is_found_verified_instead_of_starting_again(self):
        started = self.start()
        check = IdentityCheck.objects.get(token=started["token"])
        self.fake.finish(check.session_id, "Approved")
        IdentityCheck.objects.filter(pk=check.pk).update(last_checked_at=None)
        self.assertEqual(self.start()["state"], "verified")
        self.assertEqual(IdentityCheck.objects.count(), 1)

    def test_a_customer_waiting_for_the_owner_is_not_given_a_second_page(self):
        started = self.start()
        check = IdentityCheck.objects.get(token=started["token"])
        self.fake.finish(check.session_id, "In Review")
        IdentityCheck.objects.filter(pk=check.pk).update(last_checked_at=None)
        self.assertEqual(self.start()["state"], "review")
        self.assertEqual(IdentityCheck.objects.count(), 1)

    def test_an_abandoned_page_lets_the_customer_start_a_new_one(self):
        started = self.start()
        check = IdentityCheck.objects.get(token=started["token"])
        self.fake.finish(check.session_id, "Abandoned", images=False)
        IdentityCheck.objects.filter(pk=check.pk).update(last_checked_at=None)
        again = self.start()
        check.refresh_from_db()
        self.assertEqual(check.status, S.EXPIRED)
        self.assertEqual(again["state"], "checking")
        self.assertNotEqual(again["token"], check.token)

    def test_a_declined_customer_may_try_again_with_a_fresh_page(self):
        started = self.start()
        check = IdentityCheck.objects.get(token=started["token"])
        self.fake.finish(check.session_id, "Declined", images=False)
        self.poll(check)
        again = self.start()
        self.assertEqual(again["state"], "checking")
        self.assertNotEqual(again["token"], started["token"])

    def test_a_session_didit_gives_that_another_check_already_holds_is_never_used_twice(self):
        first = self.start()
        IdentityCheck.objects.filter(token=first["token"]).update(status=S.CANCELLED)            # an older check that lost its place
        # Didit still has that session unfinished and hands it back for the same person: it is never saved on two checks, and the older check's page is the one used
        again = self.start()
        self.assertEqual((again["state"], again["token"], again["url"]), ("checking", first["token"], first["url"]))
        self.assertEqual(IdentityCheck.objects.get(token=first["token"]).status, S.PENDING)
        self.assertEqual(IdentityCheck.objects.exclude(session_id="").count(), 1)

    def test_a_check_the_owner_already_decided_never_leaves_the_customer_stuck_on_opening(self):
        first = self.start()
        IdentityCheck.objects.filter(token=first["token"]).update(status=S.DECLINED, decision_source="owner", finished_at=timezone.now())     # decided here, Didit's page still open
        again = self.start()
        self.assertEqual(again["state"], "checking")
        self.assertNotEqual((again["token"], again["url"]), (first["token"], first["url"]))                   # a NEW check with a NEW page
        self.assertEqual(IdentityCheck.objects.get(token=first["token"]).status, S.DECLINED)                  # the answer stays on record
        self.assertEqual(IdentityCheck.objects.get(token=again["token"]).status, S.PENDING)
        self.assertEqual(IdentityCheck.objects.exclude(session_id="").count(), 2)
        self.assertEqual(self.fake.calls[-1][2]["vendor_data"], f"c{self.ana.pk}-{IdentityCheck.objects.get(token=again['token']).pk}")
        self.assertEqual(self.start()["token"], again["token"])                                               # pressing again is still the same new check

    def test_two_customers_never_share_a_check_or_a_page(self):
        one, two = self.start(self.ana), self.start(self.ben)
        self.assertNotEqual((one["token"], one["url"]), (two["token"], two["url"]))
        self.assertEqual(IdentityCheck.objects.count(), 2)


# ---------------------------------------------------------------- reading the result back
class ReadBackTests(Base):
    def test_an_approved_check_is_verified_for_a_year_and_both_pictures_are_kept(self):
        check = self.verified()
        self.assertEqual((check.status, check.decision_source, check.provider_status), (S.APPROVED, "didit", "Approved"))
        self.assertTrue(check.is_valid)
        self.assertEqual((check.name_check, check.type_check), ("match", "match"))
        self.assertEqual(check.photos_state, "stored")
        photos = {p.side: p for p in check.photos.all()}
        self.assertEqual(set(photos), {"front", "back"})
        for photo in photos.values():
            self.assertTrue(default_storage.exists(photo.storage_name))
            self.assertRegex(photo.storage_name, r"^id_photos/[A-Za-z0-9_-]{20,}\.jpg$")        # a long random name: nothing about the person
            image = Image.open(io.BytesIO(identity_photos.read_photo(photo)))
            self.assertEqual(image.format, "JPEG")

    def test_what_is_not_kept(self):
        check = self.verified(name="ANA MARIE CRUZ")
        row = {f.name: getattr(check, f.name) for f in IdentityCheck._meta.fields}
        blob = json.dumps(row, default=str).upper()
        self.assertNotIn("MARIE", blob)                                               # the name on the ID is compared and thrown away
        self.assertFalse(any("selfie" in f.name or "number" in f.name for f in IdentityCheck._meta.fields))

    def test_polling_again_and_again_changes_nothing_and_copies_the_pictures_once(self):
        check = self.verified()
        stored = list(IdentityPhoto.objects.values_list("pk", "storage_name"))
        for _ in range(4):
            self.poll(check)
        identity.refresh(check.pk, source="webhook")
        self.assertEqual(list(IdentityPhoto.objects.values_list("pk", "storage_name")), stored)
        self.assertEqual(IdentityCheck.objects.get(pk=check.pk).status, S.APPROVED)

    def test_two_looks_at_the_same_moment_copy_the_pictures_once(self):
        started = self.start()
        check = IdentityCheck.objects.get(token=started["token"])
        self.fake.finish(check.session_id, "Approved")
        decision = didit.retrieve_decision(check.session_id)
        identity.apply_decision(check.pk, decision)
        identity.apply_decision(check.pk, decision)
        names = list(IdentityPhoto.objects.filter(id_check=check).values_list("storage_name", flat=True))
        self.assertEqual(len(names), 2)
        self.assertEqual(len(set(names)), 2)
        self.assertTrue(all(default_storage.exists(name) for name in names))

    def test_the_page_does_not_ask_didit_more_often_than_every_couple_of_seconds(self):
        started = self.start()
        check = IdentityCheck.objects.get(token=started["token"])
        before = len(self.fake.calls)
        identity.refresh(check.pk, source="poll")
        identity.refresh(check.pk, source="poll")
        identity.refresh(check.pk, source="poll")
        self.assertEqual(len(self.fake.calls) - before, 1)

    def test_a_customer_still_on_the_page_stays_pending_while_didit_is_waiting(self):
        started = self.start()
        check = IdentityCheck.objects.get(token=started["token"])
        for status in ("Not Started", "In Progress", "Awaiting User", "Resubmitted"):
            self.fake.sessions[check.session_id]["status"] = status
            self.assertEqual(self.poll(check).status, S.PENDING)
        self.assertEqual(IdentityCheck.objects.get(pk=check.pk).provider_status, "Resubmitted")

    def test_didit_being_unreachable_leaves_the_check_waiting_not_failed(self):
        started = self.start()
        check = IdentityCheck.objects.get(token=started["token"])
        self.fake.fail["read_decision"] = didit.DiditError("down", status=503, retryable=True)
        self.assertEqual(self.poll(check).status, S.PENDING)
        del self.fake.fail["read_decision"]
        self.fake.finish(check.session_id, "Approved")
        self.assertEqual(self.poll(check).status, S.APPROVED)

    def test_a_declined_check_keeps_its_plain_reasons_and_is_never_verified(self):
        started = self.start()
        check = IdentityCheck.objects.get(token=started["token"])
        self.fake.finish(check.session_id, "Declined", warnings=[{"risk": "LOW_LIVENESS_SCORE", "log_type": "error", "short_description": "x"},
                                                                 {"risk": "DOCUMENT_EXPIRED", "log_type": "warning", "short_description": "y"}])
        check = self.poll(check)
        self.assertEqual((check.status, check.decision_source, check.verified_at, check.expires_at), (S.DECLINED, "didit", None, None))
        self.assertEqual(check.reasons, ["The selfie check did not show a live person.", "The ID has expired."])
        self.assertFalse(check.is_valid)
        self.assertFalse(identity.is_verified(self.ana))

    def test_a_declined_check_with_no_picture_has_nothing_to_keep(self):
        started = self.start()
        check = IdentityCheck.objects.get(token=started["token"])
        self.fake.finish(check.session_id, "Declined", images=False)
        check = self.poll(check)
        self.assertEqual((check.photos_state, check.photos.count()), ("", 0))

    def test_a_check_for_review_waits_for_the_owner_and_the_poll_does_not_move_it(self):
        started = self.start()
        check = IdentityCheck.objects.get(token=started["token"])
        self.fake.finish(check.session_id, "In Review", warnings=[{"risk": "LOW_FACE_MATCH_SIMILARITY", "log_type": "warning", "short_description": "x"}])
        check = self.poll(check)
        self.assertEqual((check.status, check.reasons, check.photos_state), (S.REVIEW, ["The selfie does not match the picture on the ID."], "stored"))
        self.fake.finish(check.session_id, "Approved")
        self.assertEqual(self.poll(check).status, S.REVIEW)                            # a page asking never moves it
        self.assertEqual(identity.refresh(check.pk, source="webhook").status, S.APPROVED)   # Didit telling us (its own reviewer) does

    def test_an_expired_or_abandoned_session_closes_the_check(self):
        for status in ("Expired", "Abandoned", "Kyc Expired"):
            with self.subTest(status=status):
                IdentityCheck.objects.all().delete()
                self.fake.sessions.clear()
                started = self.start()
                check = IdentityCheck.objects.get(token=started["token"])
                self.fake.finish(check.session_id, status, images=False)
                check = self.poll(check)
                self.assertEqual(check.status, S.EXPIRED)
                self.assertIsNotNone(check.finished_at)

    def test_a_different_kind_of_id_or_name_than_the_one_given_is_flagged_not_refused(self):
        check = self.verified(document_type="Passport", name="JUAN DELA PAZ")
        self.assertEqual((check.status, check.type_check, check.name_check), (S.APPROVED, "differs", "differs"))

    def test_the_name_and_type_checks(self):
        self.assertEqual(identity.name_check("Ana Cruz", "ANA MARIE SANTOS CRUZ"), "match")
        self.assertEqual(identity.name_check("Cruz, Ana", "Ana Cruz"), "match")
        self.assertEqual(identity.name_check("José Peña", "JOSE PENA"), "match")
        self.assertEqual(identity.name_check("Ana", "Ana Cruz"), "match")
        self.assertEqual(identity.name_check("Ana Cruz", "Maria Lopez"), "differs")
        self.assertEqual(identity.name_check("", "Ana Cruz"), "")
        self.assertEqual(identity.name_check("Ana Cruz", ""), "")
        self.assertEqual(identity.type_check("passport", "Passport"), "match")
        self.assertEqual(identity.type_check("passport", "Identity Card"), "differs")
        self.assertEqual(identity.type_check("other", "Passport"), "")
        self.assertEqual(identity.type_check("philid", ""), "")


# ---------------------------------------------------------------- the pictures
class PictureTests(Base):
    def test_a_big_picture_is_shrunk_and_its_camera_data_is_gone(self):
        big = jpeg_bytes(size=(4000, 3000), exif=True)
        self.assertGreater(len(Image.open(io.BytesIO(big)).getexif()), 0)
        kept = Image.open(io.BytesIO(identity_photos.shrink(big)))
        self.assertLessEqual(max(kept.size), identity_photos.MAX_SIDE)
        self.assertEqual(len(kept.getexif()), 0)
        self.assertEqual(kept.format, "JPEG")

    def test_something_that_is_not_a_picture_or_is_enormous_is_refused(self):
        for junk in (b"", b"not a picture", b"<html>hi</html>", b"%PDF-1.4"):
            with self.subTest(junk=junk[:10]), self.assertRaises(identity_photos.PhotoError):
                identity_photos.shrink(junk)
        with patch.object(identity_photos, "MAX_PIXELS", 100), self.assertRaises(identity_photos.PhotoError):
            identity_photos.shrink(jpeg_bytes())

    def test_one_picture_failing_keeps_the_other_and_a_later_try_finishes_the_job(self):
        started = self.start()
        check = IdentityCheck.objects.get(token=started["token"])
        self.fake.finish(check.session_id, "Approved")
        self.fake.bad_pictures = {"back.jpg"}
        check = self.poll(check)
        self.assertEqual(check.photos_state, "failed")
        self.assertEqual(list(check.photos.values_list("side", flat=True)), ["front"])
        self.fake.bad_pictures = set()
        self.assertTrue(identity_photos.copy_photos(check.pk))
        check.refresh_from_db()
        self.assertEqual((check.photos_state, check.photos.count()), ("stored", 2))

    def test_a_picture_that_cannot_be_kept_never_stops_the_verdict(self):
        started = self.start()
        check = IdentityCheck.objects.get(token=started["token"])
        self.fake.finish(check.session_id, "Approved")
        with patch("reservations.identity_photos.default_storage.save", side_effect=OSError("disk full")):
            check = self.poll(check)
        self.assertEqual((check.status, check.photos_state, check.photos.count()), (S.APPROVED, "failed", 0))
        self.assertTrue(identity.is_verified(self.ana))

    def test_a_copy_that_was_interrupted_can_be_claimed_again_but_a_running_one_cannot(self):
        check = self.verified()
        IdentityPhoto.objects.filter(id_check=check).delete()
        IdentityCheck.objects.filter(pk=check.pk).update(photos_state="copying")
        self.assertFalse(identity_photos.copy_photos(check.pk))                      # someone else is copying right now
        IdentityCheck.objects.filter(pk=check.pk).update(updated_at=timezone.now() - timedelta(minutes=30))
        self.assertTrue(identity_photos.copy_photos(check.pk))                       # that one died: take over

    def test_deleting_the_pictures_removes_the_files_and_marks_the_check(self):
        check = self.verified()
        names = list(check.photos.values_list("storage_name", flat=True))
        self.assertEqual(identity_photos.delete_photos(check), 2)
        self.assertFalse(any(default_storage.exists(name) for name in names))
        check.refresh_from_db()
        self.assertEqual((check.photos_state, check.photos.count()), ("deleted", 0))
        self.assertTrue(check.is_valid)                                               # the result stays; only the pictures go

    def test_deleting_an_account_deletes_its_checks_and_their_picture_files(self):
        check = self.verified()
        names = list(check.photos.values_list("storage_name", flat=True))
        self.ana.delete()
        self.assertFalse(IdentityCheck.objects.exists())
        self.assertFalse(any(default_storage.exists(name) for name in names))

    def test_a_viewing_link_is_for_one_picture_one_person_and_five_minutes(self):
        check = self.verified()
        photo = check.photos.first()
        token = identity_photos.view_token(self.owner.pk, photo.pk)
        self.assertEqual(identity_photos.read_view_token(token), (photo.pk, self.owner.pk))
        self.assertIsNone(identity_photos.read_view_token(token + "x"))
        self.assertIsNone(identity_photos.read_view_token(""))
        self.assertIsNone(identity_photos.read_view_token(None))
        with patch("django.core.signing.time.time", return_value=time_module.time() + 400):
            self.assertIsNone(identity_photos.read_view_token(token))


# ---------------------------------------------------------------- the owner decides
class OwnerDecisionTests(Base):
    def review(self):
        started = self.start()
        check = IdentityCheck.objects.get(token=started["token"])
        self.fake.finish(check.session_id, "In Review")
        return self.poll(check)

    def test_the_owner_approves_and_the_customer_is_verified_and_told(self):
        check = self.review()
        done = identity.owner_decide(check.pk, approve=True, user=self.owner, note="  Looked fine   in person ")
        self.assertEqual((done.status, done.decision_source, done.decided_by, done.note), (S.APPROVED, "owner", self.owner, "Looked fine in person"))
        self.assertTrue(done.is_valid)
        self.assertTrue(identity.is_verified(self.ana))
        self.assertIn("verified", CustomerMessage.objects.get(recipient=self.ana).body)

    def test_the_owner_declines_and_the_customer_is_told_to_try_again(self):
        check = self.review()
        done = identity.owner_decide(check.pk, approve=False, user=self.owner)
        self.assertEqual((done.status, done.verified_at), (S.DECLINED, None))
        self.assertFalse(identity.is_verified(self.ana))
        self.assertIn("could not verify", CustomerMessage.objects.get(recipient=self.ana).body)

    def test_only_a_check_waiting_for_review_can_be_decided_and_only_once(self):
        check = self.review()
        identity.owner_decide(check.pk, approve=True, user=self.owner)
        with self.assertRaises(identity.IdentityError) as caught:
            identity.owner_decide(check.pk, approve=False, user=self.owner)              # a double click
        self.assertEqual(caught.exception.status, 409)
        self.assertTrue(IdentityCheck.objects.get(pk=check.pk).is_valid)
        pending = self.verified(self.ben)
        with self.assertRaises(identity.IdentityError):
            identity.owner_decide(pending.pk, approve=False, user=self.owner)            # already approved by Didit
        with self.assertRaises(identity.IdentityError) as missing:
            identity.owner_decide(999999, approve=True, user=self.owner)
        self.assertEqual(missing.exception.status, 404)

    def test_what_didit_says_later_never_overrides_the_owner(self):
        check = self.review()
        identity.owner_decide(check.pk, approve=False, user=self.owner)
        self.fake.finish(check.session_id, "Approved")
        identity.refresh(check.pk, source="webhook")
        identity.apply_decision(check.pk, didit.retrieve_decision(check.session_id))
        self.assertEqual(IdentityCheck.objects.get(pk=check.pk).status, S.DECLINED)

    def test_a_walk_ins_check_has_no_one_to_message(self):
        check = IdentityCheck.objects.create(token="w" * 32, source="walkin", status=S.REVIEW, staff=self.staff, subject_name="Lorna Reyes", subject_phone="09171234567")
        identity.owner_decide(check.pk, approve=True, user=self.owner)
        self.assertFalse(CustomerMessage.objects.exists())


# ---------------------------------------------------------------- walk-in checks
class WalkInTests(Base):
    PHONE = "0917 123 4567"

    def counter(self, **kwargs):
        kwargs.setdefault("name", "Lorna Reyes")
        kwargs.setdefault("phone", self.PHONE)
        kwargs.setdefault("id_type", "drivers_license")
        return identity.counter_check(staff=self.staff, **kwargs)

    def test_checking_the_original_id_at_the_counter_is_recorded_with_who_and_when(self):
        check = self.counter(note="Checked the licence, expires 2029")
        self.assertEqual((check.source, check.status, check.decision_source, check.staff, check.decided_by, check.customer), ("counter", "approved", "counter", self.staff, self.staff, None))
        self.assertEqual((check.subject_name, check.subject_phone, check.id_type, check.note), ("Lorna Reyes", "09171234567", "drivers_license", "Checked the licence, expires 2029"))
        self.assertTrue(check.is_valid)
        self.assertEqual((check.photos_state, check.photos.count()), ("", 0))            # a counter check has no picture
        self.assertEqual(check.badge, ("ok", "ID checked at the counter"))

    def test_a_counter_check_needs_a_real_type_a_name_and_a_mobile_number(self):
        for kwargs in (dict(id_type="library_card"), dict(name=""), dict(phone=""), dict(phone="123")):
            with self.subTest(kwargs=kwargs), self.assertRaises(identity.IdentityError):
                self.counter(**kwargs)
        self.assertFalse(IdentityCheck.objects.exists())

    def test_the_qr_code_opens_a_page_for_the_customers_own_phone(self):
        answer = identity.start_walkin(staff=self.staff, name="Lorna Reyes", phone=self.PHONE, id_type="philid", callback_for=self.callback_for)
        check = IdentityCheck.objects.get()
        self.assertEqual((answer["state"], answer["url"]), ("checking", check.session_url))
        self.assertEqual((check.source, check.staff, check.customer, check.subject_phone, check.status), ("walkin", self.staff, None, "09171234567", "pending"))
        self.assertEqual(self.fake.calls[0][2]["vendor_data"], f"w{check.pk}")

    def test_pressing_scan_again_for_the_same_customer_is_the_same_qr_code(self):
        first = identity.start_walkin(staff=self.staff, name="Lorna Reyes", phone=self.PHONE, id_type="philid", callback_for=self.callback_for)
        again = identity.start_walkin(staff=self.staff, name="Lorna Reyes", phone="09171234567", id_type="passport", callback_for=self.callback_for)
        self.assertEqual((again["token"], again["url"]), (first["token"], first["url"]))
        self.assertEqual((IdentityCheck.objects.count(), IdentityCheck.objects.get().id_type), (1, "passport"))
        other = identity.start_walkin(staff=self.staff, name="Cora Diaz", phone="09170000000", id_type="philid", callback_for=self.callback_for)
        self.assertNotEqual(other["token"], first["token"])

    def test_the_qr_code_needs_the_customers_name_and_number_and_didit(self):
        with self.assertRaises(identity.IdentityError):
            identity.start_walkin(staff=self.staff, name="", phone=self.PHONE, id_type="philid", callback_for=self.callback_for)
        with self.assertRaises(identity.IdentityError):
            identity.start_walkin(staff=self.staff, name="Lorna", phone="12", id_type="philid", callback_for=self.callback_for)
        with override_settings(DIDIT_API_KEY=""), self.assertRaises(identity.IdentityError) as caught:
            identity.start_walkin(staff=self.staff, name="Lorna Reyes", phone=self.PHONE, id_type="philid", callback_for=self.callback_for)
        self.assertEqual(caught.exception.status, 503)

    def test_a_walk_in_scan_that_finishes_is_a_verified_check_for_that_phone(self):
        answer = identity.start_walkin(staff=self.staff, name="Lorna Reyes", phone=self.PHONE, id_type="philid", callback_for=self.callback_for)
        check = IdentityCheck.objects.get(token=answer["token"])
        self.fake.finish(check.session_id, "Approved", name="LORNA REYES")
        check = self.poll(check)
        self.assertEqual((check.status, check.name_check, check.photos_state), (S.APPROVED, "match", "stored"))
        self.assertEqual(identity.usable_walkin_check(check.token, phone=self.PHONE), check)

    def test_a_check_can_be_used_only_for_the_customer_it_was_made_for_once_and_soon(self):
        check = self.counter()
        self.assertEqual(identity.usable_walkin_check(check.token, phone="09171234567"), check)
        self.assertEqual(identity.usable_walkin_check(check.token, phone="+63 917 123 4567"), check)       # the same number written another way
        with self.assertRaises(identity.IdentityError) as other:
            identity.usable_walkin_check(check.token, phone="09179999999")
        self.assertEqual(other.exception.extra, {"id_required": True})
        with self.assertRaises(identity.IdentityError):
            identity.usable_walkin_check("nonsense", phone="09171234567")
        with self.assertRaises(identity.IdentityError):
            identity.usable_walkin_check(check.token, phone="09171234567", now=timezone.now() + timedelta(hours=13))
        reservation = Reservation.objects.create(customer=None, customer_name="Lorna", phone="09171234567", channel="Walk-in", status="Confirmed", identity_check=check)
        with self.assertRaises(identity.IdentityError) as used:
            identity.usable_walkin_check(check.token, phone="09171234567")
        self.assertIn("already used", used.exception.message)
        reservation.delete()

    def test_a_check_that_is_not_verified_cannot_be_used(self):
        answer = identity.start_walkin(staff=self.staff, name="Lorna Reyes", phone=self.PHONE, id_type="philid", callback_for=self.callback_for)
        for status in (S.PENDING, S.REVIEW, S.DECLINED, S.EXPIRED, S.CANCELLED):
            IdentityCheck.objects.filter(token=answer["token"]).update(status=status)
            with self.subTest(status=status), self.assertRaises(identity.IdentityError):
                identity.usable_walkin_check(answer["token"], phone=self.PHONE)
        IdentityCheck.objects.filter(token=answer["token"]).update(status=S.APPROVED, expires_at=timezone.now() - timedelta(days=1))
        with self.assertRaises(identity.IdentityError):
            identity.usable_walkin_check(answer["token"], phone=self.PHONE)

    def test_a_website_checks_token_is_not_a_walk_in_check(self):
        online = self.verified()
        with self.assertRaises(identity.IdentityError):
            identity.usable_walkin_check(online.token, phone="09171234567")

    def test_a_walk_in_booking_needs_an_id_check_unless_the_owner_turned_the_rule_off(self):
        with self.assertRaises(identity.IdentityError) as caught:
            identity.walkin_requirement(account=None, phone=self.PHONE, token="")
        self.assertEqual((caught.exception.status, caught.exception.extra), (400, {"id_required": True}))
        check = self.counter()
        self.assertEqual(identity.walkin_requirement(account=None, phone=self.PHONE, token=check.token), check)
        with self.assertRaises(identity.IdentityError):
            identity.walkin_requirement(account=None, phone="09179999999", token=check.token)                # made for another number
        set_switch(False)
        self.assertIsNone(identity.walkin_requirement(account=None, phone=self.PHONE, token=""))
        self.assertIsNone(identity.walkin_requirement(account=None, phone="09179999999", token=check.token))   # a wrong one is just ignored when nothing is required
        self.assertEqual(identity.walkin_requirement(account=None, phone=self.PHONE, token=check.token), check)

    def test_a_linked_website_account_that_is_verified_satisfies_the_rule(self):
        online = self.verified()
        self.assertEqual(identity.walkin_requirement(account=self.ana, phone=self.PHONE, token=""), online)
        with self.assertRaises(identity.IdentityError):
            identity.walkin_requirement(account=self.ben, phone=self.PHONE, token="")                          # an account that has not verified does not


# ---------------------------------------------------------------- the cleanup
class CleanupTests(Base):
    def booking(self, check, *, status="Returned", returned=None, return_date=None):
        reservation = Reservation.objects.create(customer=self.ana, customer_name="Ana Cruz", phone="09171234567", status=status, identity_check=check)
        today = timezone.localdate()
        ReservationItem.objects.create(reservation=reservation, gown_name="Gown", rental_date=today - timedelta(days=50), return_date=return_date or today - timedelta(days=45),
                                       returned_on=returned, rental_price=1000)
        return reservation

    def test_the_pictures_go_thirty_days_after_the_last_return_and_not_before(self):
        check = self.verified()
        today = timezone.localdate()
        self.booking(check, returned=today - timedelta(days=29))
        self.assertEqual(identity.cleanup_photos()[0], 0)
        self.assertEqual(check.photos.count(), 2)
        Reservation.objects.all().delete()
        self.booking(check, returned=today - timedelta(days=31))
        self.assertEqual(identity.cleanup_photos()[0], 1)
        check.refresh_from_db()
        self.assertEqual((check.photos_state, check.photos.count()), ("deleted", 0))
        self.assertTrue(check.is_valid)

    def test_the_last_of_several_returns_counts(self):
        check = self.verified()
        today = timezone.localdate()
        self.booking(check, returned=today - timedelta(days=60))
        self.booking(check, returned=today - timedelta(days=5))
        self.assertEqual(identity.cleanup_photos()[0], 0)

    def test_a_booking_still_open_keeps_the_pictures_however_old_the_check_is(self):
        check = self.verified()
        IdentityCheck.objects.filter(pk=check.pk).update(verified_at=timezone.now() - timedelta(days=400))
        self.booking(check, status="Active", returned=None)
        self.assertEqual(identity.cleanup_photos()[0], 0)
        self.assertEqual(check.photos.count(), 2)

    def test_a_cancelled_booking_counts_from_its_return_date(self):
        check = self.verified()
        self.booking(check, status="Cancelled", return_date=timezone.localdate() - timedelta(days=40))
        self.assertEqual(identity.cleanup_photos()[0], 1)

    def test_a_check_with_no_booking_loses_its_pictures_thirty_days_after_it_was_verified(self):
        check = self.verified()
        self.assertEqual(identity.cleanup_photos()[0], 0)
        IdentityCheck.objects.filter(pk=check.pk).update(verified_at=timezone.now() - timedelta(days=31))
        self.assertEqual(identity.cleanup_photos()[0], 1)

    def test_running_it_twice_or_on_a_check_with_no_pictures_is_harmless(self):
        check = self.verified()
        IdentityCheck.objects.filter(pk=check.pk).update(verified_at=timezone.now() - timedelta(days=31))
        self.assertEqual(identity.cleanup_photos()[0], 1)
        self.assertEqual(identity.cleanup_photos(), (0, 0))

    def test_a_failed_copy_is_tried_again_by_the_cleanup(self):
        started = self.start()
        check = IdentityCheck.objects.get(token=started["token"])
        self.fake.finish(check.session_id, "Approved")
        self.fake.bad_pictures = {"front.jpg", "back.jpg"}
        check = self.poll(check)
        self.assertEqual(check.photos_state, "failed")
        self.fake.bad_pictures = set()
        self.assertEqual(identity.cleanup_photos(), (0, 1))
        self.assertEqual(IdentityCheck.objects.get(pk=check.pk).photos_state, "stored")

    def test_the_cleanup_that_rides_on_staff_visits_runs_at_most_once_an_hour_and_never_raises(self):
        from django.core.cache import cache
        cache.delete("arabela:id-photos:last-run")
        self.assertEqual(identity.run_cleanup_if_due(), (0, 0))
        self.assertIsNone(identity.run_cleanup_if_due())
        cache.delete("arabela:id-photos:last-run")
        with patch("reservations.identity.cleanup_photos", side_effect=RuntimeError("boom")):
            self.assertIsNone(identity.run_cleanup_if_due())
        cache.delete("arabela:id-photos:last-run")

    def test_the_cleanup_command_counts_with_dry_run_and_deletes_without_it(self):
        from io import StringIO
        from django.core.management import call_command
        check = self.verified()
        IdentityCheck.objects.filter(pk=check.pk).update(verified_at=timezone.now() - timedelta(days=31))
        out = StringIO()
        call_command("cleanup_id_photos", "--dry-run", stdout=out)
        self.assertIn("1 ID check(s) have pictures that are due", out.getvalue())
        self.assertEqual(check.photos.count(), 2)                                           # nothing was deleted
        out = StringIO()
        call_command("cleanup_id_photos", stdout=out)
        self.assertIn("Deleted the pictures of 1 ID check(s)", out.getvalue())
        self.assertEqual((IdentityCheck.objects.get(pk=check.pk).photos_state, check.photos.count()), ("deleted", 0))
        out = StringIO()
        call_command("cleanup_id_photos", stdout=out)                                       # again: harmless
        self.assertIn("Deleted the pictures of 0 ID check(s)", out.getvalue())


class BadgeTests(Base):
    def test_the_chip_on_a_booking_says_what_stands_behind_it(self):
        booking = Reservation.objects.create(customer=self.ana, customer_name="Ana", phone="09171234567", status="Confirmed")
        self.assertEqual((identity.badge(booking)["kind"], identity.badge(booking)["words"]), ("none", "No ID on file"))
        check = self.verified()
        booking.identity_check = check
        self.assertEqual((identity.badge(booking)["kind"], identity.badge(booking)["words"]), ("ok", "ID verified"))
        for status, kind, words in ((S.REVIEW, "warn", "ID needs review"), (S.DECLINED, "bad", "ID declined"), (S.PENDING, "wait", "ID being checked"), (S.EXPIRED, "bad", "ID not finished")):
            check.status = status
            self.assertEqual(check.badge, (kind, words))
        check.status, check.expires_at = S.APPROVED, timezone.now() - timedelta(days=1)
        self.assertEqual(check.badge, ("warn", "ID expired"))
