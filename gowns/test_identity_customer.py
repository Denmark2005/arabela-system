"""ID verification, the customer's side (gowns/identity_views.py, the gate in gowns/online_checkout.py and gowns/views.py, the Valid ID box on the checkout and profile pages).
Didit is the in-memory FakeDidit of reservations/test_identity.py and Xendit the FakeXendit of gowns/test_xendit_checkout.py: nothing reaches the network."""
import json
import time
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from accounts.test_support import agreed
from gowns.models import Gown, SiteSettings
from gowns.test_xendit_checkout import FakeXendit, SETTINGS as XENDIT_SETTINGS
from reservations import didit, identity
from reservations.models import IdentityCheck, PaymentAttempt, Reservation
from reservations.test_identity import FakeDidit, SETTINGS as DIDIT_SETTINGS

User = get_user_model()
S = IdentityCheck.Status


def set_switch(on):
    """The owner's switch (the settings row is created on first read, so it is made first)."""
    shop = SiteSettings.load()
    shop.require_id_before_payment = on
    shop.save(update_fields=["require_id_before_payment", "updated_at"])


@override_settings(**{**XENDIT_SETTINGS, **DIDIT_SETTINGS})
class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ana = User.objects.create_user("idc_ana", "idc.ana@gmail.com", "x", first_name="Ana")
        UserProfile.objects.update_or_create(user=cls.ana, defaults={"display_name": "Ana Cruz"})
        cls.ben = User.objects.create_user("idc_ben", "idc.ben@gmail.com", "x", first_name="Ben")
        UserProfile.objects.update_or_create(user=cls.ben, defaults={"display_name": "Ben Reyes"})

    def setUp(self):
        self.fake = FakeDidit()
        for target, kwargs in (("reservations.didit._request", {"side_effect": self.fake}), ("reservations.didit.download_picture", {"side_effect": self.fake.download})):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = Client()
        self.client.force_login(self.ana)
        agreed(self.ana)

    def post_start(self, data=None, client=None):
        body = {"id_type": "philid", "consent": True} if data is None else data
        return (client or self.client).post(reverse("gowns:identity_start"), data=json.dumps(body), content_type="application/json")

    def started(self, client=None):
        response = self.post_start(client=client)
        self.assertEqual(response.status_code, 200, response.content)
        return IdentityCheck.objects.get(token=response.json()["token"])

    def finish(self, check, status="Approved", **kwargs):
        self.fake.finish(check.session_id, status, **kwargs)
        IdentityCheck.objects.filter(pk=check.pk).update(last_checked_at=None)

    def webhook(self, event=None, *, secret="whsec_unit", stamp=None, client=None):
        event = event or {"event_id": "e1", "webhook_type": "status.updated", "session_id": "x", "status": "Approved"}
        return (client or Client()).post(
            reverse("gowns:didit_webhook"), data=json.dumps(event), content_type="application/json",
            HTTP_X_TIMESTAMP=str(int(time.time()) if stamp is None else stamp), HTTP_X_SIGNATURE_V2=didit.sign(event, secret))


class StartEndpointTests(Base):
    def test_pressing_verify_gives_the_page_to_send_the_browser_to(self):
        response = self.post_start()
        body = response.json()
        self.assertEqual((response.status_code, body["success"], body["state"]), (200, True, "checking"))
        check = IdentityCheck.objects.get()
        self.assertEqual(body["url"], check.session_url)
        self.assertTrue(body["url"].startswith("https://verify.didit.me/session/"))
        self.assertEqual(self.fake.calls[0][2]["callback"].endswith(reverse("gowns:identity_return", args=[check.token])), True)

    def test_spamming_the_button_is_the_same_check(self):
        tokens = {self.post_start().json()["token"] for _ in range(8)}
        self.assertEqual(len(tokens), 1)
        self.assertEqual((IdentityCheck.objects.count(), sum(1 for call in self.fake.calls if call[0] == "POST")), (1, 1))

    def test_a_bad_request_is_turned_away_with_plain_words(self):
        for body in ({"id_type": "philid", "consent": False}, {"id_type": "philid"}, {"id_type": "nope", "consent": True}, {"consent": True}, {"id_type": "philid", "consent": "true"}):
            with self.subTest(body=body):
                response = self.post_start(body)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["success"])
                self.assertTrue(response.json()["error"])
        self.assertFalse(IdentityCheck.objects.exists())

    def test_garbage_bodies_and_other_methods_are_refused(self):
        for raw in (b"not json", b"[1, 2]", b'"text"', b""):
            with self.subTest(raw=raw):
                response = self.client.post(reverse("gowns:identity_start"), data=raw, content_type="application/json")
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get(reverse("gowns:identity_start")).status_code, 405)
        self.assertEqual(self.client.put(reverse("gowns:identity_start")).status_code, 405)

    def test_a_signed_out_visitor_gets_a_message_the_page_can_show(self):
        response = self.post_start(client=Client())
        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.json()["signed_out"])
        self.assertFalse(IdentityCheck.objects.exists())

    def test_the_request_needs_the_sites_csrf_token(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.ana)
        response = strict.post(reverse("gowns:identity_start"), data=json.dumps({"id_type": "philid", "consent": True}), content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(IdentityCheck.objects.exists())

    def test_didit_being_down_is_a_clear_message_and_nothing_is_left_open(self):
        self.fake.fail["create_session"] = didit.DiditError("down", status=503, retryable=True)
        response = self.post_start()
        self.assertEqual((response.status_code, response.json()["success"]), (502, False))
        self.assertIn("Nothing was saved", response.json()["error"])
        del self.fake.fail["create_session"]
        self.assertEqual(self.post_start().status_code, 200)

    def test_when_didit_is_not_set_up_the_button_says_so(self):
        with override_settings(DIDIT_API_KEY=""):
            response = self.post_start()
        self.assertEqual(response.status_code, 503)

    def test_a_customer_only_ever_starts_a_check_for_their_own_account(self):
        mine = self.started()
        other = Client()
        other.force_login(self.ben)
        theirs = self.started(client=other)
        self.assertNotEqual(mine.token, theirs.token)
        self.assertEqual((mine.customer, theirs.customer), (self.ana, self.ben))


class StateAndReturnTests(Base):
    def test_the_state_follows_the_customer_through_the_whole_check(self):
        url = reverse("gowns:identity_state")
        self.assertEqual(self.client.get(url).json()["state"], "none")
        check = self.started()
        self.assertEqual(self.client.get(url).json()["state"], "checking")
        self.finish(check)
        state = self.client.get(url).json()
        self.assertEqual((state["state"], state["success"]), ("verified", True))
        self.assertTrue(state["until"])

    def test_the_state_asks_for_a_signed_in_customer_and_is_never_cached(self):
        self.assertEqual(Client().get(reverse("gowns:identity_state")).status_code, 401)
        self.assertIn("no-cache", self.client.get(reverse("gowns:identity_state"))["Cache-Control"])

    def test_the_return_page_is_the_owners_alone(self):
        check = self.started()
        url = reverse("gowns:identity_return", args=[check.token])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, reverse("gowns:identity_status", args=[check.token]))
        other = Client()
        other.force_login(self.ben)
        self.assertEqual(other.get(url).status_code, 404)
        self.assertEqual(other.get(reverse("gowns:identity_status", args=[check.token])).status_code, 404)
        anonymous = Client().get(url)
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("login", anonymous["Location"])
        self.assertEqual(self.client.get(reverse("gowns:identity_return", args=["nonsense"])).status_code, 404)

    def test_a_walk_ins_check_has_no_customer_page(self):
        walkin = IdentityCheck.objects.create(token="w" * 32, source="walkin", subject_name="Lorna", subject_phone="09171234567")
        self.assertEqual(self.client.get(reverse("gowns:identity_return", args=[walkin.token])).status_code, 404)
        self.assertEqual(self.client.get(reverse("gowns:identity_status", args=[walkin.token])).status_code, 404)

    def test_the_status_says_each_stage_and_asks_a_signed_in_customer(self):
        check = self.started()
        url = reverse("gowns:identity_status", args=[check.token])
        self.assertEqual(self.client.get(url).json()["state"], "checking")
        self.finish(check, "Declined", warnings=[{"risk": "DOCUMENT_EXPIRED", "log_type": "error", "short_description": "x"}], images=False)
        body = self.client.get(url).json()
        self.assertEqual((body["state"], body["reasons"]), ("declined", ["The ID has expired."]))
        self.assertEqual(Client().get(url).status_code, 401)

    def test_an_approved_status_carries_how_long_it_counts(self):
        check = self.started()
        self.finish(check)
        body = self.client.get(reverse("gowns:identity_status", args=[check.token])).json()
        self.assertEqual(body["state"], "approved")
        self.assertRegex(body["until"], r"^[A-Z][a-z]{2} \d{1,2}, \d{4}$")

    def test_a_review_waits_and_the_poll_leaves_it_alone(self):
        check = self.started()
        self.finish(check, "In Review")
        self.assertEqual(self.client.get(reverse("gowns:identity_status", args=[check.token])).json()["state"], "review")
        self.finish(check, "Approved")
        self.assertEqual(self.client.get(reverse("gowns:identity_status", args=[check.token])).json()["state"], "review")

    def test_a_closed_check_is_ended(self):
        check = self.started()
        IdentityCheck.objects.filter(pk=check.pk).update(status=S.EXPIRED)
        self.assertEqual(self.client.get(reverse("gowns:identity_status", args=[check.token])).json()["state"], "ended")


class WalkInPageTests(Base):
    def test_the_page_a_walk_ins_phone_lands_on_needs_no_sign_in_and_shows_nothing(self):
        check = IdentityCheck.objects.create(token="walk" + "x" * 28, source="walkin", subject_name="Lorna Reyes", subject_phone="09171234567", id_type="philid")
        page = Client().get(reverse("gowns:identity_walkin_done", args=[check.token]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Thank you")
        for secret in ("Lorna", "0917", "philid", check.token, "Verified", "declined"):
            self.assertNotContains(page, secret)

    def test_only_a_walk_ins_token_opens_it(self):
        online = self.started()
        self.assertEqual(Client().get(reverse("gowns:identity_walkin_done", args=[online.token])).status_code, 404)
        self.assertEqual(Client().get(reverse("gowns:identity_walkin_done", args=["x" * 32])).status_code, 404)


class WebhookTests(Base):
    def test_a_signed_webhook_makes_the_server_read_the_result_back(self):
        check = self.started()
        self.finish(check)
        response = self.webhook({"event_id": "e1", "webhook_type": "status.updated", "session_id": check.session_id, "status": "Approved"})
        self.assertEqual((response.status_code, response.content), (200, b"ok"))
        check.refresh_from_db()
        self.assertEqual((check.status, check.photos_state), (S.APPROVED, "stored"))
        self.assertIsNotNone(check.webhook_at)

    def test_a_webhook_cannot_approve_anyone_by_what_it_says(self):
        check = self.started()                                                # Didit's own record still says "Not Started"
        response = self.webhook({"event_id": "e1", "session_id": check.session_id, "status": "Approved", "decision": {"id_verifications": [{"full_name": "Ana Cruz"}]}})
        self.assertEqual(response.status_code, 503)                           # it claims Approved, Didit's own record does not say so: asked to try again
        check.refresh_from_db()
        self.assertEqual(check.status, S.PENDING)                             # the server read Didit back: nothing was approved
        self.assertFalse(identity.is_verified(self.ana))

    def test_a_webhook_with_a_bad_or_old_or_missing_signature_is_refused(self):
        check = self.started()
        event = {"event_id": "e1", "session_id": check.session_id, "status": "Approved"}
        self.assertEqual(self.webhook(event, secret="wrong").status_code, 400)
        self.assertEqual(self.webhook(event, stamp=int(time.time()) - 3600).status_code, 400)
        unsigned = Client().post(reverse("gowns:didit_webhook"), data=json.dumps(event), content_type="application/json", HTTP_X_TIMESTAMP=str(int(time.time())))
        self.assertEqual(unsigned.status_code, 400)
        with override_settings(DIDIT_WEBHOOK_SECRET=""):
            self.assertEqual(self.webhook(event).status_code, 400)            # with no secret set, every webhook is refused
        check.refresh_from_db()
        self.assertEqual(check.status, S.PENDING)
        self.assertIsNone(check.webhook_at)

    def test_a_webhook_for_a_session_we_do_not_know_is_answered_ok_so_it_is_not_retried(self):
        response = self.webhook({"event_id": "e1", "session_id": "00000000-0000-4000-8000-000000009999", "status": "Approved"})
        self.assertEqual((response.status_code, response.content), (200, b"ignored"))
        self.assertEqual(self.webhook({"event_id": "e2", "status": "Approved"}).status_code, 200)

    def test_when_we_cannot_confirm_what_it_says_didit_is_asked_to_try_again(self):
        check = self.started()
        self.fake.fail["read_decision"] = didit.DiditError("down", status=503, retryable=True)
        response = self.webhook({"event_id": "e1", "session_id": check.session_id, "status": "Approved"})
        self.assertEqual(response.status_code, 503)
        del self.fake.fail["read_decision"]
        self.finish(check)
        self.assertEqual(self.webhook({"event_id": "e1", "session_id": check.session_id, "status": "Approved"}).status_code, 200)
        self.assertEqual(IdentityCheck.objects.get(pk=check.pk).status, S.APPROVED)

    def test_the_same_webhook_twice_is_harmless(self):
        check = self.started()
        self.finish(check)
        event = {"event_id": "e1", "session_id": check.session_id, "status": "Approved"}
        self.assertEqual(self.webhook(event).status_code, 200)
        self.assertEqual(self.webhook(event).status_code, 200)
        self.assertEqual(IdentityCheck.objects.get(pk=check.pk).photos.count(), 2)

    def test_only_post_is_accepted_and_no_sign_in_is_needed(self):
        self.assertEqual(Client().get(reverse("gowns:didit_webhook")).status_code, 405)
        self.assertEqual(self.webhook(client=Client()).status_code, 200)       # an unknown session, but signed: answered without any sign-in

    def test_a_webhook_for_a_walk_ins_check_updates_it_too(self):
        walkin = identity.start_walkin(staff=User.objects.create_user("idc_staff", password="x", is_staff=True), name="Lorna Reyes", phone="09171234567", id_type="philid",
                                       callback_for=lambda token: f"https://shop.example/{token}/")
        check = IdentityCheck.objects.get(token=walkin["token"])
        self.fake.finish(check.session_id, "Approved", name="LORNA REYES")
        self.assertEqual(self.webhook({"event_id": "e1", "session_id": check.session_id, "status": "Approved"}).status_code, 200)
        self.assertEqual(IdentityCheck.objects.get(pk=check.pk).status, S.APPROVED)


class PagesTests(Base):
    def test_the_checkout_shows_the_id_box_while_the_owner_requires_it(self):
        page = self.client.get(reverse("gowns:reservation"))
        self.assertContains(page, 'id="idv-box"')
        self.assertContains(page, "Valid ID")
        self.assertContains(page, reverse("gowns:identity_start"))
        self.assertContains(page, "window.arabelaIdBox")

    def test_the_checkout_has_no_id_box_when_the_owner_turned_the_rule_off_or_didit_is_off(self):
        set_switch(False)
        self.assertNotContains(self.client.get(reverse("gowns:reservation")), 'id="idv-box"')
        set_switch(True)
        with override_settings(DIDIT_API_KEY=""):
            self.assertNotContains(self.client.get(reverse("gowns:reservation")), 'id="idv-box"')

    def test_the_profile_shows_the_card_while_didit_is_set_up_even_if_the_rule_is_off(self):
        self.assertContains(self.client.get(reverse("gowns:profile")), 'id="idv-box"')
        set_switch(False)
        self.assertContains(self.client.get(reverse("gowns:profile")), 'id="idv-box"')
        with override_settings(DIDIT_API_KEY=""):
            self.assertNotContains(self.client.get(reverse("gowns:profile")), 'id="idv-box"')

    def test_the_boxs_data_is_plain_json_and_carries_no_one_elses_information(self):
        check = self.started()
        page = self.client.get(reverse("gowns:reservation")).content.decode()
        start = page.index('<script id="idv-data" type="application/json">') + len('<script id="idv-data" type="application/json">')
        data = json.loads(page[start:page.index("</script>", start)])
        self.assertEqual((data["state"], data["url"], data["required"]), ("checking", check.session_url, True))
        self.assertEqual(sorted(t["value"] for t in data["types"]), sorted(IdentityCheck.IdType.values))
        self.assertNotIn(check.token, json.dumps(data))
        self.assertNotIn("idc.ana@gmail.com", json.dumps(data))

    def test_a_verified_customers_box_says_so(self):
        check = self.started()
        self.finish(check)
        identity.refresh(check.pk, source="poll")
        page = self.client.get(reverse("gowns:reservation")).content.decode()
        self.assertIn('"state": "verified"', page)

    def test_the_return_page_wording_and_the_solid_green_check(self):
        check = self.started()
        page = self.client.get(reverse("gowns:identity_return", args=[check.token]))
        for text in ("Checking your ID.", "Your ID is verified.", "The shop will take a look.", "We could not verify your ID.", "Continue to checkout", "#22c55e"):
            self.assertContains(page, text)


@override_settings(**{**XENDIT_SETTINGS, **DIDIT_SETTINGS})
class GateTests(Base):
    """The rule itself: an unverified customer cannot pay online, whatever the page does."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.gown = Gown.objects.create(gown_id="Wedding Gown-IDC-001", name="Id Gown One", category=Gown.Category.WEDDING_GOWN, color_name="White", color_code="WH",
                                       size=Gown.Size.MEDIUM, rental_price="3500", status=Gown.Status.AVAILABLE)

    def setUp(self):
        super().setUp()
        self.xendit = FakeXendit()
        patcher = patch("reservations.xendit._request", side_effect=self.xendit)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.event = timezone.localdate() + timedelta(days=20)

    def pay(self, client=None, **extra):
        item = {"gown_name": self.gown.name, "gown_slug": self.gown.slug, "size": "Medium", "rental_date": (self.event - timedelta(days=2)).isoformat(),
                "return_date": (self.event + timedelta(days=2)).isoformat()}
        data = {"items": json.dumps([item]), "pay_option": "deposit", "first_name": "Ana", "last_name": "Cruz", "phone": "09171234567", "address": "1 Test St",
                "city": "Antipolo", "postal_code": "1870", "expected_deposit": "2000", "request_id": "idc-req-0000001"}
        data.update(extra)
        return (client or self.client).post(reverse("gowns:pay_start"), data=data)

    def verify(self, user=None):
        client = self.client
        if user is not None:
            client = Client()
            client.force_login(user)
            agreed(user)
        check = self.started(client=client)
        self.finish(check)
        return identity.refresh(check.pk, source="poll")

    def test_an_unverified_customer_is_turned_away_before_anything_is_opened(self):
        response = self.pay()
        body = response.json()
        self.assertEqual((response.status_code, body["success"], body["id_required"]), (403, False, True))
        self.assertIn("verify your valid ID", body["error"])
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertEqual(self.xendit.count("create_session"), 0)

    def test_a_verified_customer_goes_through_and_the_booking_remembers_the_check(self):
        check = self.verify()
        response = self.pay()
        self.assertEqual(response.status_code, 200, response.content)
        attempt = PaymentAttempt.objects.get()
        self.assertEqual(attempt.payload["identity_check"], check.pk)
        self.xendit.paid(attempt.session_id)
        PaymentAttempt.objects.filter(pk=attempt.pk).update(last_checked_at=None)
        self.client.get(reverse("gowns:payment_status", args=[attempt.token]))
        reservation = Reservation.objects.get()
        self.assertEqual(reservation.identity_check, check)
        self.assertEqual(identity.badge(reservation)["words"], "ID verified")

    def test_someone_elses_verified_id_does_not_let_this_customer_pay(self):
        self.verify(self.ben)
        self.assertEqual(self.pay().status_code, 403)

    def test_an_id_that_ran_out_must_be_verified_again(self):
        check = self.verify()
        IdentityCheck.objects.filter(pk=check.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.pay().status_code, 403)

    def test_an_id_waiting_for_the_owner_or_declined_does_not_count(self):
        check = self.started()
        self.finish(check, "In Review")
        identity.refresh(check.pk, source="poll")
        self.assertEqual(self.pay().status_code, 403)
        IdentityCheck.objects.filter(pk=check.pk).update(status=S.DECLINED)
        self.assertEqual(self.pay().status_code, 403)

    def test_the_owner_turning_the_rule_off_lets_everyone_pay_and_nothing_is_linked(self):
        set_switch(False)
        self.assertEqual(self.pay().status_code, 200)
        self.assertIsNone(PaymentAttempt.objects.get().payload["identity_check"])

    def test_without_didit_nobody_can_verify_so_nobody_is_stopped(self):
        with override_settings(DIDIT_API_KEY=""):
            self.assertEqual(self.pay().status_code, 200)

    def test_the_rule_is_checked_after_the_terms_and_before_the_rest(self):
        from accounts import terms
        with patch.object(terms, "needs_to_accept", return_value=True):
            self.assertTrue(self.pay().json().get("terms_required"))
        response = self.pay(items="not json")
        self.assertEqual((response.status_code, response.json()["id_required"]), (403, True))             # refused for the ID, before the bag is even read

    def test_a_verified_customer_who_pays_twice_with_the_same_press_still_gets_one_payment_page(self):
        self.verify()
        first, second = self.pay(), self.pay()
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(first.json()["token"], second.json()["token"])

    def test_the_older_checkout_is_held_to_the_same_rule(self):
        with override_settings(XENDIT_SECRET_KEY="", PAYMENT_PROVIDER="", PAYMONGO_SECRET_KEY=""):
            response = self.client.post(reverse("gowns:reservation_submit"), data={"items": "[]"})
        self.assertEqual((response.status_code, response.json()["id_required"]), (403, True))

    def test_a_walk_in_is_not_affected_by_an_online_customers_rule(self):
        self.assertTrue(identity.required_online())
        self.assertIsNotNone(identity.online_gate(self.ana))
