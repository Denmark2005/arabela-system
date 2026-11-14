"""Gmail is one mailbox however it is spelled: dots, a "+tag" and googlemail.com all reach the same Google account.

The admin Gmail checks used to compare the exact text, so rosastaffer@gmail.com and ros.astaffer@gmail.com counted as two different people:
the same person could be added twice, a Gmail that a customer already uses could slip in as a staff account, and -- the real problem -- the
owner typing one spelling while Google sends back another meant the staff member could never sign in. Matching is now by mailbox
(accounts.admin_google.canonical_email / same_mailbox). The owner's own sign-in Gmail can no longer be given to a staff account."""
import json

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse

from accounts import admin_google as ag
from accounts.models import UserProfile
from arabela_admin.test_google_admin_signin import OWNER_GMAIL, Base, google_answer, run_adapter
from arabela_system.middleware import admin_cookie_name
from django.conf import settings
from importlib import import_module

User = get_user_model()


class CanonicalEmailTests(Base):
    def test_gmail_spellings_collapse_to_one_mailbox(self):
        for spelling in ("rosa.staffer@gmail.com", "rosastaffer@gmail.com", "Rosa.Staffer+shop@Gmail.com", "rosastaffer@googlemail.com", "r.o.s.a.s.t.a.f.f.e.r+x+y@googlemail.com"):
            with self.subTest(spelling=spelling):
                self.assertEqual(ag.canonical_email(spelling), "rosastaffer@gmail.com")

    def test_other_addresses_are_only_themselves(self):
        self.assertEqual(ag.canonical_email("Rosa.Staffer@Yahoo.com"), "rosa.staffer@yahoo.com")           # dots matter at other providers
        self.assertEqual(ag.canonical_email("rosa+shop@company.ph"), "rosa+shop@company.ph")
        self.assertEqual(ag.canonical_email(""), "@")

    def test_the_filter_matches_the_same_mailbox_and_nothing_else(self):
        User.objects.create_user("mb_same", "rosastaffer@gmail.com", None)
        User.objects.create_user("mb_other1", "rosastaffer2@gmail.com", None)
        User.objects.create_user("mb_other2", "xrosastaffer@gmail.com", None)
        User.objects.create_user("mb_other3", "rosastaffer@yahoo.com", None)
        User.objects.create_user("mb_other4", "rosa.staffer.x@gmail.com", None)
        User.objects.create_user("mb_special", "a_b-c@gmail.com", None)
        found = lambda email: set(User.objects.filter(ag.same_mailbox(email)).values_list("username", flat=True))
        for spelling in ("ros.astaffer@gmail.com", "rosastaffer+shop@gmail.com", "ROSASTAFFER@GOOGLEMAIL.COM"):
            with self.subTest(spelling=spelling):
                self.assertEqual(found(spelling), {"mb_same"})
        self.assertEqual(found("rosastaffer@yahoo.com"), {"mb_other3"})                    # a non-Gmail address matches only itself
        self.assertEqual(found("a_b-c@gmail.com"), {"mb_special"})                          # characters that mean something in a pattern are plain text
        self.assertEqual(found("a.b+c@gmail.com"), set())


class NoTwoSpellingsOfOneGmailTests(Base):
    def test_a_second_spelling_of_a_staff_gmail_is_refused(self):
        for spelling in ("m.aria.staff@gmail.com", "mariastaff+shop@gmail.com", "mariastaff@googlemail.com", "Maria.Staff@GMAIL.com"):
            with self.subTest(spelling=spelling):
                self.assertEqual(ag.gmail_problem(ag.normalize_email(spelling)), "That Gmail already belongs to another admin account.")

    def test_a_spelling_of_a_customer_gmail_is_refused_too(self):
        User.objects.create_user("mb_cust", "custy.person@gmail.com", None)
        self.assertEqual(ag.gmail_problem("cust.y.person@gmail.com"), "That Gmail is already used by a customer account. Please use a different Gmail.")

    def test_a_staff_account_can_still_keep_its_own_gmail_when_edited(self):
        self.assertEqual(ag.gmail_problem("maria.staff@gmail.com", exclude_pk=self.staff.pk), "")
        self.assertEqual(ag.gmail_problem("m.aria.staff@gmail.com", exclude_pk=self.staff.pk), "")      # a different spelling of her own Gmail is fine for her

    def test_the_add_account_form_says_so_in_plain_words(self):
        client = Client()
        client.force_login(self.owner)
        response = client.post(reverse("arabela_admin:staff_create"), data=json.dumps({"name": "Maria Again", "email": "m.aria.staff@gmail.com"}), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "That Gmail already belongs to another admin account.")
        self.assertFalse(User.objects.filter(email="m.aria.staff@gmail.com").exists())            # nothing was created


@override_settings(ADMIN_OWNER_GMAIL=OWNER_GMAIL)
class OwnerGmailStaysWithTheOwnerTests(Base):
    def test_the_owner_gmail_cannot_be_given_to_a_staff_account(self):
        client = Client()
        client.force_login(self.owner)
        for spelling in (OWNER_GMAIL, "owner.shop@gmail.com".replace("owner.shop", "o.wner.shop"), "ownershop+staff@googlemail.com"):
            with self.subTest(spelling=spelling):
                response = client.post(reverse("arabela_admin:staff_create"), data=json.dumps({"name": "Not Owner", "email": spelling}), content_type="application/json")
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("owner's own sign-in Gmail", response.json()["error"])
        self.assertFalse(User.objects.filter(first_name="Not").exists())

    def test_nor_by_editing_an_existing_staff_account(self):
        client = Client()
        client.force_login(self.owner)
        response = client.post(reverse("arabela_admin:staff_update", args=[self.staff.pk]), data=json.dumps({"name": "Maria Staff", "email": "ownershop@gmail.com"}), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(User.objects.get(pk=self.staff.pk).email, "maria.staff@gmail.com")

    def test_the_owner_herself_may_change_her_sign_in_to_that_gmail(self):
        self.assertEqual(ag.gmail_problem(OWNER_GMAIL, exclude_pk=self.owner.pk, allow_owner_gmail=True), "")

    def test_the_owner_is_still_attached_by_any_spelling_of_her_gmail(self):
        response = run_adapter(google_answer("ownershop@gmail.com"))                       # Google sends the plain spelling
        self.assertTrue(response.url.startswith(ag.finish_path() + "?t="), response.url)
        self.assertEqual(User.objects.get(pk=self.owner.pk).email, "ownershop@gmail.com")

    def test_the_change_form_knows_a_spelling_of_her_current_gmail_is_the_same(self):
        User.objects.filter(pk=self.owner.pk).update(email="owner.shop@gmail.com")
        client = Client()
        client.force_login(self.owner)
        response = client.post(reverse("arabela_admin:owner_gmail_change"), data=json.dumps({"email": "o.wner.shop@gmail.com"}), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "That is already your sign-in Gmail.")


def signed_in_as(client):
    """The id of the account signed in to the ADMIN area (it has its own session cookie)."""
    key = client.cookies[admin_cookie_name()].value
    return int(import_module(settings.SESSION_ENGINE).SessionStore(key)["_auth_user_id"])


class SignInByMailboxTests(Base):
    def make_staff(self, gmail, name="Dot Staff", **flags):
        user = User.objects.create_user(name.replace(" ", "_").lower(), gmail, None, is_staff=True, **flags)
        UserProfile.objects.create(user=user, role=UserProfile.Role.STAFF)
        return user

    def test_a_staff_gmail_typed_with_dots_still_signs_in_when_google_sends_the_plain_spelling(self):
        staff = self.make_staff("ros.astaffer@gmail.com")
        client = Client()
        self.finish(client, run_adapter(google_answer("rosastaffer@gmail.com")))
        self.assertEqual(signed_in_as(client), staff.pk)
        self.assertEqual(client.get(reverse("arabela_admin:pending_approval")).status_code, 200)

    def test_every_spelling_google_may_send_opens_the_same_account(self):
        staff = self.make_staff("rosastaffer@gmail.com")
        for sent in ("rosastaffer@gmail.com", "rosa.staffer@gmail.com", "rosastaffer+x@googlemail.com"):
            with self.subTest(sent=sent):
                client = Client()
                self.finish(client, run_adapter(google_answer(sent)))
                self.assertEqual(signed_in_as(client), staff.pk)

    def test_an_exact_match_wins_over_a_look_alike(self):
        loose = self.make_staff("ros.astaffer@gmail.com", name="Loose One")
        exact = self.make_staff("rosastaffer@gmail.com", name="Exact One")
        user, why = ag.resolve_admin_user("rosastaffer@gmail.com")
        self.assertEqual((user.pk, why), (exact.pk, ""))
        user, why = ag.resolve_admin_user("ros.astaffer@gmail.com")
        self.assertEqual((user.pk, why), (loose.pk, ""))

    def test_a_non_gmail_address_still_needs_the_exact_address(self):
        self.make_staff("pat.person@company.ph", name="Pat Person")
        self.assertEqual(ag.resolve_admin_user("pat.person@company.ph")[1], "")
        self.assertEqual(ag.resolve_admin_user("patperson@company.ph"), (None, "not_allowed"))
        self.assertEqual(ag.resolve_admin_user("PAT.PERSON@Company.PH")[1], "")

    def test_a_stranger_with_a_look_alike_is_still_refused(self):
        self.make_staff("rosastaffer@gmail.com")
        for sent in ("rosastaffer2@gmail.com", "rosa.staffer.x@gmail.com", "xrosastaffer@gmail.com", "rosastaffer@yahoo.com"):
            with self.subTest(sent=sent):
                self.assertEqual(ag.resolve_admin_user(sent), (None, "not_allowed"))

    def test_a_switched_off_account_is_still_reported_as_off_by_any_spelling(self):
        self.make_staff("rosastaffer@gmail.com", is_active=False)
        self.assertEqual(ag.resolve_admin_user("ros.astaffer@gmail.com"), (None, "inactive"))

    def test_a_customer_with_a_look_alike_gmail_never_opens_the_admin_panel(self):
        User.objects.create_user("mb_cust2", "custy.person@gmail.com", None)
        self.assertEqual(ag.resolve_admin_user("custyperson@gmail.com"), (None, "not_allowed"))
