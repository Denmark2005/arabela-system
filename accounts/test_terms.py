"""Customers read and agree to the Terms and Conditions before they rent: the agreement record (accounts/terms.py), the accept endpoint,
the server-side checks on the countdown and on submitting a reservation, who sees the pop-up, and that the pop-up and the Terms page share one text.

The server keeps the proof-of-payment save mocked like every other checkout test: this environment's storage can be real Cloudinary."""
import json
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts import terms
from accounts.models import UserProfile
from accounts.tests import _ensure_google_social_app
from gowns.models import Gown
from gowns.sample_images import TINY_JPEG
from reservations.models import Reservation

User = get_user_model()
ROOT = Path(settings.BASE_DIR)


def customer(name="terms_cust"):
    return User.objects.create_user(username=name, password="x")


def agree(user, days_ago=0):
    """Mark a customer as having agreed `days_ago` days ago (today by default)."""
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.terms_accepted_at = timezone.now() - timedelta(days=days_ago)
    profile.save(update_fields=["terms_accepted_at"])
    return user


class AgreementRecordTests(TestCase):
    def test_only_a_signed_in_customer_has_to_agree(self):
        from django.contrib.auth.models import AnonymousUser
        staff = User.objects.create_user(username="t_staff", password="x", is_staff=True)
        owner = User.objects.create_superuser(username="t_owner", password="x", email="o@example.com")
        inactive = User.objects.create_user(username="t_off", password="x", is_active=False)
        for who in (AnonymousUser(), staff, owner, inactive):
            with self.subTest(who=str(who)):
                self.assertFalse(terms.applies_to(who))
                self.assertFalse(terms.needs_to_accept(who))
        self.assertTrue(terms.applies_to(customer()))

    def test_a_customer_who_never_agreed_has_to(self):
        user = customer()
        self.assertTrue(terms.needs_to_accept(user))                       # no profile row yet
        UserProfile.objects.get_or_create(user=user)
        self.assertTrue(terms.needs_to_accept(user))                       # a profile, but no agreement date

    def test_agreeing_before_the_terms_were_last_updated_does_not_count(self):
        user = customer()
        days_since_update = (timezone.localdate() - terms.TERMS_UPDATED_ON).days
        agree(user, days_ago=days_since_update + 3)
        self.assertTrue(terms.needs_to_accept(user))

    def test_agreeing_on_or_after_the_update_counts(self):
        user = agree(customer())
        self.assertFalse(terms.needs_to_accept(user))

    def test_moving_the_update_date_makes_everyone_agree_again(self):
        user = agree(customer())
        self.assertFalse(terms.needs_to_accept(user))
        with patch.object(terms, "TERMS_UPDATED_ON", timezone.localdate() + timedelta(days=1)):
            self.assertTrue(terms.needs_to_accept(user))

    def test_recording_creates_the_profile_and_keeps_the_first_moment(self):
        user = customer()
        first = terms.record_acceptance(user)
        self.assertEqual(UserProfile.objects.get(user=user).terms_accepted_at, first)
        self.assertEqual(terms.record_acceptance(user), first)             # saying it again changes nothing
        self.assertEqual(UserProfile.objects.get(user=user).terms_accepted_at, first)

    def test_the_version_label_is_a_readable_date(self):
        self.assertEqual(terms.label(), "Nov 8, 2026")


class AcceptEndpointTests(TestCase):
    url = None

    @classmethod
    def setUpTestData(cls):
        cls.url = reverse("accounts:accept_terms")
        cls.customer = customer("accept_cust")
        cls.staff = User.objects.create_user(username="accept_staff", password="x", is_staff=True)

    def test_a_visitor_is_sent_to_sign_in(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_it_only_answers_post(self):
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_a_customer_agreeing_is_recorded_with_the_time(self):
        self.client.force_login(self.customer)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["version"], terms.label())
        profile = UserProfile.objects.get(user=self.customer)
        self.assertEqual(profile.terms_accepted_at.isoformat(), body["accepted_at"])
        self.assertFalse(terms.needs_to_accept(self.customer))

    def test_agreeing_twice_keeps_the_first_time(self):
        self.client.force_login(self.customer)
        first = self.client.post(self.url).json()["accepted_at"]
        second = self.client.post(self.url).json()["accepted_at"]
        self.assertEqual(first, second)

    def test_a_staff_sign_in_is_refused_and_nothing_is_recorded(self):
        self.client.force_login(self.staff)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(UserProfile.objects.filter(user=self.staff, terms_accepted_at__isnull=False).exists())


class ServerChecksTests(TestCase):
    """The pop-up is the friendly way in; these are the checks that stop anything that skips it."""

    @classmethod
    def setUpTestData(cls):
        cls.gown = Gown.objects.create(gown_id="TERMS-0001", name="Terms Test Gown", category=Gown.Category.GUEST_GOWN, color_name="Red",
                                       color_code="RD", size=Gown.Size.MEDIUM, rental_price=Decimal("4500.00"), status=Gown.Status.AVAILABLE)

    def setUp(self):
        self.user = customer("gate_cust")
        self.client.force_login(self.user)

    def submit(self):
        items = [{"gown_name": self.gown.name, "gown_slug": self.gown.slug, "size": "Medium", "rental_date": "2027-04-10", "return_date": "2027-04-13"}]
        with patch("gowns.views._save_proof_file", return_value="https://example.test/proof.jpg"):
            return self.client.post(reverse("gowns:reservation_submit"), data={
                "items": json.dumps(items), "first_name": "Test", "last_name": "Buyer", "phone": "09171234567", "address": "123 Test St",
                "city": "Test City", "postal_code": "1000", "payment_method": "GCash",
                "proof_of_payment": SimpleUploadedFile("proof.jpg", TINY_JPEG, content_type="image/jpeg")})

    def test_the_countdown_does_not_start_before_the_terms_are_agreed(self):
        response = self.client.post(reverse("gowns:reservation_hold_start"))
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertTrue(body["terms_required"])
        self.assertIn("Terms and Conditions", body["error"])

    def test_the_countdown_starts_once_they_have_agreed(self):
        agree(self.user)
        response = self.client.post(reverse("gowns:reservation_hold_start"))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["success"])
        self.assertGreater(response.json()["seconds_remaining"], 0)

    def test_a_reservation_cannot_be_submitted_before_the_terms_are_agreed(self):
        response = self.submit()
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.json()["terms_required"])
        self.assertFalse(Reservation.objects.filter(customer=self.user).exists())

    def test_a_reservation_goes_through_after_agreeing_and_notes_which_terms(self):
        agree(self.user)
        response = self.submit()
        self.assertEqual(response.status_code, 200, response.content)
        reservation = Reservation.objects.get(customer=self.user)
        note = reservation.status_events.get(label="Terms and Conditions accepted")
        self.assertTrue(note.staff_only)                            # for the shop's own records, not shown to the customer
        self.assertIn(terms.label(), note.detail)

    def test_an_agreement_from_before_the_terms_changed_is_not_enough(self):
        agree(self.user, days_ago=(timezone.localdate() - terms.TERMS_UPDATED_ON).days + 2)
        self.assertEqual(self.submit().status_code, 403)

    def test_staff_are_not_asked(self):
        staff = User.objects.create_user(username="gate_staff", password="x", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.post(reverse("gowns:reservation_hold_start")).status_code, 200)


class WhoSeesThePopupTests(TestCase):
    page = None

    @classmethod
    def setUpTestData(cls):
        cls.page = reverse("gowns:homepage")

    def html(self):
        return self.client.get(self.page).content.decode()

    def test_a_customer_who_has_not_agreed_gets_the_popup_and_the_flag(self):
        self.client.force_login(customer("seer"))
        html = self.html()
        self.assertIn('data-terms-needed="true"', html)
        self.assertIn('id="ara-terms"', html)
        self.assertIn("Before you rent", html)
        self.assertIn("The 5 things that matter most", html)
        self.assertIn(reverse("accounts:accept_terms"), html)
        self.assertIn(f"Version of {terms.label()}", html)

    def test_the_popup_has_the_damage_and_lost_gown_wording(self):
        self.client.force_login(customer("seer2"))
        html = re.sub(r"\s+", " ", self.html())
        self.assertIn("the cost of the repair will be deducted from your security deposit", html)
        self.assertIn("If the gown is lost, the amount to be deducted depends on the gown and will be agreed with you at the shop", html)

    def test_a_customer_who_agreed_gets_neither(self):
        self.client.force_login(agree(customer("agreed")))
        html = self.html()
        self.assertNotIn("data-terms-needed", html)
        self.assertNotIn('id="ara-terms"', html)

    def test_a_visitor_gets_neither(self):
        html = self.html()
        self.assertNotIn("data-terms-needed", html)
        self.assertNotIn('id="ara-terms"', html)

    def test_staff_looking_at_the_customer_site_get_neither(self):
        self.client.force_login(User.objects.create_user(username="seer_staff", password="x", is_staff=True))
        html = self.html()
        self.assertNotIn("data-terms-needed", html)
        self.assertNotIn('id="ara-terms"', html)

    def test_the_admin_area_never_asks(self):
        owner = User.objects.create_superuser(username="seer_owner", password="x", email="so@example.com")
        self.client.force_login(owner)
        self.assertNotIn('id="ara-terms"', self.client.get(reverse("arabela_admin:dashboard")).content.decode())

    def test_the_sign_in_page_says_what_will_happen(self):
        _ensure_google_social_app()
        html = re.sub(r"\s+", " ", self.client.get(reverse("accounts:login")).content.decode())
        self.assertIn("you will be asked to read and accept Arabela's", html)
        self.assertNotIn("By continuing, you agree to", html)


class OneTextTests(TestCase):
    """The Terms page and the pop-up are the same text, so they can never disagree."""

    def headings(self, html):
        return re.findall(r'<h2 class="font-label[^>]*>([^<]+)</h2>', html)

    def test_the_page_and_the_popup_show_the_same_sections_in_the_same_order(self):
        page = self.client.get(reverse("gowns:terms_and_conditions")).content.decode()
        page = page[page.index('id="terms-page"'):page.index("</section>")]
        self.client.force_login(customer("same_text"))
        popup = self.client.get(reverse("gowns:homepage")).content.decode()
        popup = popup[popup.index('id="ara-terms-scroll"'):popup.index('class="ara-terms__end"')]
        self.assertEqual(self.headings(page), self.headings(popup))
        self.assertGreaterEqual(len(self.headings(page)), 9)

    def test_the_page_keeps_its_reveal_animation_and_the_popup_does_not_hide_anything(self):
        page = self.client.get(reverse("gowns:terms_and_conditions")).content.decode()
        self.assertEqual(page.count('<article class="terms-reveal'), 10)         # all 10 sections fade in on the page, exactly as before
        self.assertIn('class="text-center mb-14 terms-reveal"', page)            # and so does the heading
        self.client.force_login(customer("same_text2"))
        popup = self.client.get(reverse("gowns:homepage")).content.decode()
        popup = popup[popup.index('id="ara-terms"'):]
        self.assertNotIn("terms-reveal", popup[:popup.index("</script>")])   # faded-out text would be unreadable in the pop-up

    def test_the_full_retail_price_rule_is_gone_everywhere(self):
        page = self.client.get(reverse("gowns:terms_and_conditions")).content.decode()
        self.assertNotIn("full retail price", page.lower())
        self.assertNotIn("billed to your account", page.lower())
        self.assertNotIn("full retail", (ROOT / "ai_recommendation" / "views.py").read_text(encoding="utf8").lower())


class WiringTests(SimpleTestCase):
    """The browser side, read from the files (no database)."""

    def read(self, *parts):
        return (ROOT.joinpath(*parts)).read_text(encoding="utf8")

    def test_the_popup_unlocks_only_at_the_end_and_cannot_be_dismissed_by_escape_or_the_background(self):
        js = self.read("templates", "partials", "terms_gate.html")
        self.assertIn("var END_SLACK = 24;", js)
        self.assertIn("if (atEnd()) unlock();", js)
        self.assertIn("check.disabled = false;", js)
        self.assertNotIn("'Escape'", js)
        self.assertNotIn("backdrop').addEventListener", js)
        self.assertIn('<input type="checkbox" id="ara-terms-check" disabled>', js)
        self.assertIn('id="ara-terms-agree-btn" disabled', js)

    def test_the_agree_box_cannot_be_reached_by_keyboard_before_the_end(self):
        css = self.read("templates", "partials", "terms_gate.html")
        locked_rule = css[css.index(".ara-terms__agree {"):css.index(".ara-terms.is-unlocked .ara-terms__agree {")]
        self.assertIn("visibility: hidden", locked_rule)

    def test_proceed_asks_for_the_agreement_before_the_countdown_starts(self):
        js = self.read("static", "js", "reservation-flow.js")
        wrapper = js[js.index("window.beginReservationHold = function"):]
        self.assertLess(wrapper.index("askForTerms()"), wrapper.index("beginHold(storageKey, payload, true)"))
        self.assertIn("if (!needsTerms) return beginHold(storageKey, payload, false);", wrapper)
        self.assertIn("result.terms_required && !askedForTerms", js)

    def test_confirm_rental_asks_first_and_handles_a_server_refusal(self):
        html = self.read("templates", "reservation.html")
        self.assertIn("document.body.dataset.termsNeeded === 'true' && typeof window.arabelaRequireTerms === 'function'", html)
        self.assertIn("err.data.terms_required", html)

    def test_base_includes_the_popup_only_for_a_customer_who_still_has_to_agree(self):
        base = self.read("templates", "base.html")
        self.assertIn('{% if terms_needed %}{% include "partials/terms_gate.html" %}{% endif %}', base)
        self.assertIn('{% if terms_needed %}\n      data-terms-needed="true"{% endif %}', base.replace("\r\n", "\n"))
