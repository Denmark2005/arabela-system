"""ID verification, the admin side (arabela_admin/identity_views.py, the walk-in rule in arabela_admin/new_booking.py and reservations/staff_booking.py, the owner's switch).
The owner alone sees the ID Checks page and the pictures; staff only run the walk-in ID row. Didit is FakeDidit (reservations/test_identity.py), Xendit is FakeXendit."""
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditLog, CustomerMessage, UserProfile
from gowns.models import Gown, SiteSettings
from gowns.test_xendit_checkout import FakeXendit, SETTINGS as XENDIT_SETTINGS
from reservations import didit, identity, identity_photos
from reservations.models import IdentityCheck, IdentityPhoto, PaymentAttempt, Reservation, ReservationItem
from reservations.test_identity import FakeDidit, SETTINGS as DIDIT_SETTINGS, set_switch

User = get_user_model()
S = IdentityCheck.Status
PHONE = "09171234567"


@override_settings(**{**XENDIT_SETTINGS, **DIDIT_SETTINGS})
class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("ida_owner", "ida.owner@gmail.com", "x")
        cls.owner2 = User.objects.create_superuser("ida_owner2", "ida.owner2@gmail.com", "x")
        cls.staff = User.objects.create_user("ida_staff", password="x", is_staff=True, first_name="Maria", last_name="Cruz")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.STAFF})
        cls.ana = User.objects.create_user("ida_ana", "ida.ana@gmail.com", "x", first_name="Ana")
        UserProfile.objects.update_or_create(user=cls.ana, defaults={"display_name": "Ana Cruz"})
        cls.customer = User.objects.create_user("ida_customer", "ida.customer@gmail.com", "x")
        cls.gown = Gown.objects.create(gown_id="Wedding Gown-IDA-001", name="Ida Gown One", category=Gown.Category.WEDDING_GOWN, color_name="White", color_code="WH",
                                       size=Gown.Size.MEDIUM, rental_price=Decimal("3000"), status=Gown.Status.AVAILABLE)
        cls.gown2 = Gown.objects.create(gown_id="Wedding Gown-IDA-002", name="Ida Gown Two", category=Gown.Category.WEDDING_GOWN, color_name="Ivory", color_code="IV",
                                        size=Gown.Size.MEDIUM, rental_price=Decimal("2500"), status=Gown.Status.AVAILABLE)

    def setUp(self):
        self.fake = FakeDidit()
        self.xendit = FakeXendit()
        for target, kwargs in (("reservations.didit._request", {"side_effect": self.fake}), ("reservations.didit.download_picture", {"side_effect": self.fake.download}),
                               ("reservations.xendit._request", {"side_effect": self.xendit})):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.event = timezone.localdate() + timedelta(days=20)
        self.owner_client = self.login(self.owner)
        self.staff_client = self.login(self.staff)

    def login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def callback_for(self, token):
        return f"https://shop.example/{token}/"

    def verified_online(self, user=None):
        user = user or self.ana
        started = identity.start_for_customer(user=user, id_type="philid", consent=True, callback_for=self.callback_for)
        check = IdentityCheck.objects.get(token=started["token"])
        self.fake.finish(check.session_id, "Approved", name="ANA MARIE CRUZ")
        IdentityCheck.objects.filter(pk=check.pk).update(last_checked_at=None)
        return identity.refresh(check.pk, source="poll")

    def in_review(self, user=None):
        user = user or self.ana
        started = identity.start_for_customer(user=user, id_type="philid", consent=True, callback_for=self.callback_for)
        check = IdentityCheck.objects.get(token=started["token"])
        self.fake.finish(check.session_id, "In Review", warnings=[{"risk": "LOW_FACE_MATCH_SIMILARITY", "log_type": "warning", "short_description": "x"}])
        IdentityCheck.objects.filter(pk=check.pk).update(last_checked_at=None)
        return identity.refresh(check.pk, source="poll")

    def counter(self, **kwargs):
        kwargs.setdefault("name", "Lorna Reyes")
        kwargs.setdefault("phone", PHONE)
        kwargs.setdefault("id_type", "drivers_license")
        return identity.counter_check(staff=self.staff, **kwargs)

    def post_json(self, client, url, body=None):
        return client.post(url, data=json.dumps(body if body is not None else {}), content_type="application/json")


# ---------------------------------------------------------------- the "Valid ID" chip on a booking
class BookingChipTests(Base):
    def booking(self, check=None, name="Ana Cruz"):
        today = timezone.localdate()
        r = Reservation.objects.create(customer=self.ana, customer_name=name, phone=PHONE, status="Confirmed", identity_check=check, rental_subtotal=Decimal("3000"),
                                       security_deposit=Decimal("2000"), total_amount=Decimal("5000"), reviewed_at=timezone.now())
        ReservationItem.objects.create(reservation=r, gown=self.gown, gown_name=self.gown.name, size="Medium", rental_price=Decimal("3000"), rental_date=today + timedelta(days=3),
                                       event_date=today + timedelta(days=5), return_date=today + timedelta(days=7), original_return_date=today + timedelta(days=7),
                                       stage=ReservationItem.Stage.PICKUP)
        return r

    def records(self, client):
        import re
        page = client.get(reverse("arabela_admin:reservation_records")).content.decode()
        raw = re.search(r'<script id="reservation-records-data" type="application/json">(.*?)</script>', page, re.S).group(1)
        return {row["id"]: row for row in json.loads(raw)}, page

    def test_active_reservations_shows_what_stands_behind_each_booking(self):
        none = self.booking(name="Nora None")
        good = self.booking(self.verified_online(), name="Vera Verified")
        page = self.owner_client.get(reverse("arabela_admin:active_reservations")).content.decode()
        self.assertIn("Valid ID: No ID on file", page)
        self.assertIn("Valid ID: ID verified", page)
        link = reverse("arabela_admin:id_checks") + f"?show=all&open={good.identity_check_id}"
        self.assertIn(f'href="{link.replace("&", "&amp;")}"', page)                      # the owner's chip opens that check
        self.assertEqual(page.count('<a class="ar-idchip'), 1)                              # a booking with no ID has nothing to open
        self.assertEqual(page.count('<span class="ar-idchip ar-idchip--none"'), 1)

    def test_staff_see_the_chip_but_never_a_way_into_the_id_pictures(self):
        self.booking(self.verified_online(), name="Vera Verified")
        page = self.staff_client.get(reverse("arabela_admin:active_reservations")).content.decode()
        self.assertIn("Valid ID: ID verified", page)
        self.assertNotIn(reverse("arabela_admin:id_checks"), page)

    def test_the_chip_follows_the_check_through_every_state(self):
        for status, words in ((S.REVIEW, "ID needs review"), (S.DECLINED, "ID declined")):
            check = self.verified_online()
            IdentityCheck.objects.filter(pk=check.pk).update(status=status)
            self.booking(IdentityCheck.objects.get(pk=check.pk), name=f"Chip {status}")
            page = self.owner_client.get(reverse("arabela_admin:active_reservations")).content.decode()
            self.assertIn(f"Valid ID: {words}", page)
            Reservation.objects.all().delete()
            IdentityCheck.objects.all().delete()

    def test_a_counter_check_reads_as_verified(self):
        self.booking(self.counter(), name="Lorna Reyes")
        self.assertIn("Valid ID: ID checked at the counter", self.owner_client.get(reverse("arabela_admin:active_reservations")).content.decode())

    def test_reservation_records_carry_the_chip_and_only_the_owner_gets_the_link(self):
        good = self.booking(self.verified_online(), name="Vera Verified")
        none = self.booking(name="Nora None")
        rows, _ = self.records(self.owner_client)
        self.assertEqual((rows[good.pk]["idBadge"]["kind"], rows[good.pk]["idBadge"]["words"]), ("ok", "ID verified"))
        self.assertEqual(rows[good.pk]["idBadge"]["link"], reverse("arabela_admin:id_checks") + f"?show=all&open={good.identity_check_id}")
        self.assertEqual(rows[none.pk]["idBadge"], {"kind": "none", "words": "No ID on file", "link": ""})
        staff_rows, _ = self.records(self.staff_client)
        self.assertEqual(staff_rows[good.pk]["idBadge"], {"kind": "ok", "words": "ID verified", "link": ""})

    def test_the_lists_stay_cheap_however_many_bookings_there_are(self):
        # the chip reads the booking's own link, so ten more bookings do not add ten more questions
        for i in range(2):
            self.booking(self.counter(name=f"Lorna Reyes {i}"), name=f"Lorna Reyes {i}")
        self.owner_client.get(reverse("arabela_admin:active_reservations"))                 # warm any one-time caches
        with CaptureQueriesContext(connection) as few:
            self.owner_client.get(reverse("arabela_admin:active_reservations"))
        for i in range(2, 8):
            self.booking(self.counter(name=f"Lorna Reyes {i}"), name=f"Lorna Reyes {i}")
        with CaptureQueriesContext(connection) as many:
            self.owner_client.get(reverse("arabela_admin:active_reservations"))

        def asked_about_checks(captured):
            return len([q for q in captured.captured_queries if "reservations_identitycheck" in q["sql"]])

        self.assertEqual(asked_about_checks(many), asked_about_checks(few))                   # the same number of questions about ID checks, six more bookings or not


# ---------------------------------------------------------------- the owner's bell
class BellTests(Base):
    def items(self, user):
        from arabela_admin import notifications
        return [i for i in notifications.build_items(user) if i["key"].startswith("idcheck:")]

    def test_a_check_waiting_for_review_rings_the_owners_bell_and_only_the_owners(self):
        check = self.in_review()
        (item,) = self.items(self.owner)
        self.assertEqual(item["key"], f"idcheck:{check.pk}")
        self.assertEqual((item["kind"], item["group"], item["level"], item["module"]), ("customer", "customer", "warning", "Customers"))
        self.assertEqual(item["title"].strip(), "Ana Cruz has an ID waiting for your review")
        self.assertEqual(item["url"], reverse("arabela_admin:id_checks") + f"?open={check.pk}")
        self.assertEqual(self.items(self.staff), [])                                      # staff cannot decide it, so it is not shown to them
        self.assertEqual(self.items(self.owner2)[0]["key"], item["key"])                    # any real owner sees it

    def test_it_goes_away_by_itself_once_the_owner_has_answered(self):
        check = self.in_review()
        self.assertEqual(len(self.items(self.owner)), 1)
        identity.owner_decide(check.pk, approve=True, user=self.owner)
        self.assertEqual(self.items(self.owner), [])

    def test_checks_that_need_nothing_do_not_ring(self):
        self.verified_online()
        self.counter()
        self.assertEqual(self.items(self.owner), [])

    def test_the_bell_on_a_real_page_names_the_customer_and_opens_the_check(self):
        check = self.in_review()
        page = self.owner_client.get(reverse("arabela_admin:dashboard")).content.decode()
        self.assertIn("Ana Cruz", page)
        self.assertIn("has an ID waiting for your review", page)
        self.assertIn(reverse("arabela_admin:id_checks") + f"?open={check.pk}", page)
        staff_page = self.staff_client.get(reverse("arabela_admin:dashboard")).content.decode()
        self.assertNotIn("has an ID waiting for your review", staff_page)

    def test_at_most_ten_are_listed_and_they_are_the_ten_that_have_waited_longest(self):
        made = []
        for index in range(12):
            user = User.objects.create_user(f"ida_bell_{index}", f"ida.bell{index}@gmail.com", "x")
            made.append(self.in_review(user))
        listed = self.items(self.owner)
        self.assertEqual({i["key"] for i in listed}, {f"idcheck:{c.pk}" for c in made[:10]})       # the queue works from the oldest (the bell then shows newest first)


# ---------------------------------------------------------------- the page
class PageTests(Base):
    def test_only_the_owner_opens_the_page(self):
        url = reverse("arabela_admin:id_checks")
        self.assertEqual(self.owner_client.get(url).status_code, 200)
        staff = self.staff_client.get(url)
        self.assertEqual((staff.status_code, staff["Location"]), (302, reverse("arabela_admin:dashboard")))
        anonymous = Client().get(url)
        self.assertEqual((anonymous.status_code, anonymous["Location"]), (302, reverse("arabela_admin:admin_login")))
        self.assertEqual(self.login(self.customer).get(url).status_code, 302)

    def test_it_lists_what_needs_a_decision_first_and_everything_on_the_other_tab(self):
        waiting = self.in_review()
        done = self.counter()
        page = self.owner_client.get(reverse("arabela_admin:id_checks")).content.decode()
        self.assertIn("Ana Cruz", page)
        self.assertNotIn("Lorna Reyes", page)                                            # the counter check needs nothing
        self.assertIn(f'data-open="{waiting.pk}"', page)
        self.assertIn(">Review<", page)
        everything = self.owner_client.get(reverse("arabela_admin:id_checks") + "?show=all").content.decode()
        self.assertIn("Lorna Reyes", everything)
        self.assertIn(f'data-open="{done.pk}"', everything)
        self.assertIn("ID checked at the counter", everything)

    def test_it_can_be_searched_by_name_number_email_and_booking(self):
        self.counter()
        self.verified_online()
        base = reverse("arabela_admin:id_checks") + "?show=all&q="
        self.assertIn("Lorna Reyes", self.owner_client.get(base + "Lorna").content.decode())
        self.assertNotIn("Lorna Reyes", self.owner_client.get(base + "Ana").content.decode())
        self.assertIn("Lorna Reyes", self.owner_client.get(base + "0917123").content.decode())
        self.assertIn("Ana Cruz", self.owner_client.get(base + "ida.ana@").content.decode())
        self.assertIn("No ID check matches", self.owner_client.get(base + "zzzz").content.decode())

    def test_the_page_says_what_the_rule_and_didit_are_doing(self):
        page = self.owner_client.get(reverse("arabela_admin:id_checks")).content.decode()
        self.assertIn("required</strong> before a customer pays online", page)
        self.assertIn("test mode", page)
        set_switch(False)
        with override_settings(DIDIT_API_KEY=""):
            page = self.owner_client.get(reverse("arabela_admin:id_checks")).content.decode()
        self.assertIn("not required</strong>", page)
        self.assertIn("ID verification is not set up", page)

    def test_the_long_note_is_one_short_line_with_the_rest_behind_a_question_mark(self):
        import re
        page = self.owner_client.get(reverse("arabela_admin:id_checks")).content.decode()
        note = re.search(r'<div class="idc-note">(.*?)</div></details>\s*</div>', page, re.S).group(1)
        popup = re.search(r'<div class="ht-tip-pop">(.*)$', note, re.S).group(1)
        visible = re.sub(r"<[^>]+>", " ", note.replace(popup, ""))                       # what is printed on the page all the time
        self.assertLess(len(" ".join(visible.split())), 120)
        self.assertIn("Only you can see this page and the ID pictures.", visible)
        self.assertIn("ID required", visible)                                              # the rule and Didit's state stay visible as two short words
        for words in ("A verified ID counts for 365 days", "deleted 30 days after the customer's last return", "the selfie is never kept"):
            self.assertIn(words, popup)                                                    # the long explanation opens on demand
            self.assertNotIn(words, visible)

    def test_the_empty_pages_explain_themselves(self):
        self.assertIn("No ID check needs your review", self.owner_client.get(reverse("arabela_admin:id_checks")).content.decode())
        self.assertIn("No ID checks yet", self.owner_client.get(reverse("arabela_admin:id_checks") + "?show=all").content.decode())

    def test_the_page_runs_the_daily_cleanup(self):
        with patch("reservations.identity.run_cleanup_if_due") as cleanup:
            self.owner_client.get(reverse("arabela_admin:id_checks"))
        cleanup.assert_called_once()

    def test_the_dashboard_visit_runs_the_daily_cleanup_and_a_failing_cleanup_never_hides_the_dashboard(self):
        with patch("reservations.identity.run_cleanup_if_due") as cleanup:
            self.assertEqual(self.owner_client.get(reverse("arabela_admin:dashboard")).status_code, 200)
        cleanup.assert_called_once()
        with patch("reservations.identity.run_cleanup_if_due", side_effect=RuntimeError("boom")):
            self.assertEqual(self.owner_client.get(reverse("arabela_admin:dashboard")).status_code, 200)

    def test_a_row_names_the_things_worth_a_look(self):
        check = self.verified_online()
        IdentityCheck.objects.filter(pk=check.pk).update(name_check="differs", type_check="differs")
        with override_settings(DIDIT_API_KEY="sk_live_unit"):                                  # a real key: the name and the kind of ID are real, so they are compared
            page = self.owner_client.get(reverse("arabela_admin:id_checks") + "?show=all").content.decode()
        self.assertIn("The name on the ID differs from the name given", page)
        self.assertIn("The ID scanned is not the kind the customer chose", page)

    def test_in_test_mode_there_are_no_false_warnings_because_didits_sample_data_is_not_compared(self):
        # Seen on the live site: a real Philippine National ID scanned in Didit's sandbox came back as a Brazilian passport with a made-up name.
        check = self.verified_online()
        IdentityCheck.objects.filter(pk=check.pk).update(name_check="differs", type_check="differs")
        page = self.owner_client.get(reverse("arabela_admin:id_checks") + "?show=all").content.decode()          # the tests' key is a sandbox key
        self.assertNotIn("differs from the name given", page)
        self.assertNotIn("not the kind the customer chose", page)
        body = self.owner_client.get(reverse("arabela_admin:id_check_open", args=[check.pk])).json()
        self.assertEqual((body["nameWords"], body["typeWords"]), ("", ""))
        self.assertIn("test mode sends sample", body["compareNote"])
        with override_settings(DIDIT_API_KEY="sk_live_unit"):
            real = self.owner_client.get(reverse("arabela_admin:id_check_open", args=[check.pk])).json()
        self.assertEqual((real["nameWords"], real["typeWords"], real["compareNote"]), ("Differs from the name given", "Not the kind the customer chose", ""))

    def test_the_sidebar_link_is_the_owners_alone(self):
        link = reverse("arabela_admin:id_checks")
        self.assertIn(link, self.owner_client.get(reverse("arabela_admin:new_booking")).content.decode())
        self.assertIn(link, self.owner_client.get(reverse("arabela_admin:gown_board")).content.decode())
        self.assertNotIn(link, self.staff_client.get(reverse("arabela_admin:new_booking")).content.decode())
        self.assertNotIn(link, self.staff_client.get(reverse("arabela_admin:gown_board")).content.decode())

    def test_the_review_count_and_the_booking_chip_links_open_the_check(self):
        waiting = self.in_review()
        page = self.owner_client.get(reverse("arabela_admin:id_checks") + f"?open={waiting.pk}").content.decode()
        self.assertIn(f'data-open-id="{waiting.pk}"', page)
        self.assertIn("Needs your review <b>1</b>", page)


# ---------------------------------------------------------------- opening a check and seeing the pictures
class OpenAndPictureTests(Base):
    def test_opening_a_check_gives_the_facts_and_short_lived_links_and_is_written_to_the_audit_log(self):
        check = self.verified_online()
        with override_settings(DIDIT_API_KEY="sk_live_unit"):                                  # a real key: the name and the kind of ID are real, so they are compared
            response = self.owner_client.get(reverse("arabela_admin:id_check_open", args=[check.pk]))
        body = response.json()
        self.assertEqual((response.status_code, body["success"], body["who"], body["words"], body["idType"]), (200, True, "Ana Cruz", "ID verified", "PhilID (National ID)"))
        self.assertEqual([p["side"] for p in body["photosList"]], ["Front", "Back"])                       # the front first, then the back
        self.assertEqual((body["nameWords"], body["typeWords"], body["canDelete"], body["canDecide"]), ("Matches the name given", "The kind the customer chose", True, False))
        for photo in body["photosList"]:
            self.assertRegex(photo["url"], r"^/admin-panel/api/id-checks/photo/.+/$")
            self.assertNotIn("cloudinary", photo["url"])                                   # the browser is never given the storage address
        row = AuditLog.objects.get(action="id.view")
        self.assertEqual((row.actor, row.category, row.sensitive, row.target_label), (self.owner, "Customers", True, "Ana Cruz"))
        self.assertIn("opened the ID of Ana Cruz", row.summary)
        self.assertNotIn("MARIE", json.dumps(row.details) + row.summary)

    def test_every_opening_is_a_line_and_a_missing_check_is_not(self):
        check = self.counter()
        for _ in range(3):
            self.owner_client.get(reverse("arabela_admin:id_check_open", args=[check.pk]))
        self.assertEqual(AuditLog.objects.filter(action="id.view").count(), 3)
        self.assertEqual(self.owner_client.get(reverse("arabela_admin:id_check_open", args=[999999])).status_code, 404)
        self.assertEqual(AuditLog.objects.filter(action="id.view").count(), 3)

    def test_staff_and_strangers_cannot_open_a_check(self):
        check = self.verified_online()
        url = reverse("arabela_admin:id_check_open", args=[check.pk])
        staff = self.staff_client.get(url)
        self.assertEqual((staff.status_code, staff.json()["error"]), (403, "Only the owner can do that."))
        self.assertEqual(Client().get(url).status_code, 401)
        self.assertEqual(self.login(self.customer).get(url).status_code, 401)
        self.assertEqual(AuditLog.objects.get(action="id.view").outcome, "refused")        # the refusal is written too
        self.assertEqual(self.staff_client.post(url).status_code, 405)

    def test_a_counter_check_opens_with_no_pictures_and_says_who_checked_it(self):
        check = self.counter(note="Checked the licence")
        body = self.owner_client.get(reverse("arabela_admin:id_check_open", args=[check.pk])).json()
        self.assertEqual((body["photosList"], body["canDelete"], body["decidedBy"], body["note"]), ([], False, "Maria Cruz", "Checked the licence"))
        self.assertEqual(body["words"], "ID checked at the counter")

    def test_the_owner_opens_a_picture_through_our_own_address_and_it_is_never_cached(self):
        check = self.verified_online()
        url = self.owner_client.get(reverse("arabela_admin:id_check_open", args=[check.pk])).json()["photosList"][0]["url"]
        response = self.owner_client.get(url)
        self.assertEqual((response.status_code, response["Content-Type"]), (200, "image/jpeg"))
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("noindex", response["X-Robots-Tag"])
        self.assertTrue(response.content.startswith(b"\xff\xd8"))

    def test_a_picture_link_works_only_for_the_owner_it_was_made_for(self):
        check = self.verified_online()
        url = self.owner_client.get(reverse("arabela_admin:id_check_open", args=[check.pk])).json()["photosList"][0]["url"]
        self.assertEqual(self.login(self.owner2).get(url).status_code, 403)             # another owner with the first one's link
        self.assertEqual(self.staff_client.get(url).status_code, 403)
        self.assertEqual(Client().get(url).status_code, 403)
        self.assertEqual(self.login(self.customer).get(url).status_code, 403)

    def test_a_link_for_a_staff_member_is_useless_and_so_is_a_made_up_one(self):
        photo = self.verified_online().photos.first()
        staff_link = reverse("arabela_admin:id_photo", args=[identity_photos.view_token(self.staff.pk, photo.pk)])
        self.assertEqual(self.staff_client.get(staff_link).status_code, 403)
        self.assertEqual(self.owner_client.get(staff_link).status_code, 403)
        for bad in ("nonsense", "x" * 80, "a:b:c"):
            self.assertEqual(self.owner_client.get(reverse("arabela_admin:id_photo", args=[bad])).status_code, 403)

    def test_a_link_stops_working_after_five_minutes(self):
        photo = self.verified_online().photos.first()
        url = reverse("arabela_admin:id_photo", args=[identity_photos.view_token(self.owner.pk, photo.pk)])
        self.assertEqual(self.owner_client.get(url).status_code, 200)
        with patch("django.core.signing.time.time", return_value=timezone.now().timestamp() + 400):
            self.assertEqual(self.owner_client.get(url).status_code, 403)

    def test_a_picture_that_was_deleted_or_lost_is_a_plain_404(self):
        photo = self.verified_online().photos.first()
        url = reverse("arabela_admin:id_photo", args=[identity_photos.view_token(self.owner.pk, photo.pk)])
        default_storage.delete(photo.storage_name)                                     # the file vanished from the storage
        self.assertEqual(self.owner_client.get(url).status_code, 404)
        photo.delete()
        self.assertEqual(self.owner_client.get(url).status_code, 404)

    def test_only_get_is_allowed_for_a_picture(self):
        photo = self.verified_online().photos.first()
        url = reverse("arabela_admin:id_photo", args=[identity_photos.view_token(self.owner.pk, photo.pk)])
        self.assertEqual(self.owner_client.post(url).status_code, 405)


# ---------------------------------------------------------------- the owner decides
class DecisionTests(Base):
    def decide(self, check, approve, client=None, **extra):
        return self.post_json(client or self.owner_client, reverse("arabela_admin:id_check_decide", args=[check.pk]), {"approve": approve, **extra})

    def test_the_owner_approves_a_check_and_it_is_written_down_and_the_customer_told(self):
        check = self.in_review()
        response = self.decide(check, True, note="Looked fine in person")
        self.assertEqual((response.status_code, response.json()), (200, {"success": True, "approved": True, "status": "approved"}))
        check.refresh_from_db()
        self.assertEqual((check.status, check.decision_source, check.decided_by, check.note), (S.APPROVED, "owner", self.owner, "Looked fine in person"))
        self.assertTrue(identity.is_verified(self.ana))
        row = AuditLog.objects.get(action="id.decide")
        self.assertEqual((row.actor, row.sensitive, row.target_label), (self.owner, True, "Ana Cruz"))
        self.assertIn("approved the ID of Ana Cruz", row.summary)
        self.assertEqual(CustomerMessage.objects.filter(recipient=self.ana).count(), 1)

    def test_the_owner_declines_a_check(self):
        check = self.in_review()
        self.assertEqual(self.decide(check, False).json()["approved"], False)
        check.refresh_from_db()
        self.assertEqual(check.status, S.DECLINED)
        self.assertIn("declined the ID of Ana Cruz", AuditLog.objects.get(action="id.decide").summary)

    def test_staff_cannot_decide_and_nothing_changes(self):
        check = self.in_review()
        response = self.decide(check, True, client=self.staff_client)
        self.assertEqual((response.status_code, response.json()["error"]), (403, "Only the owner can do that."))
        self.assertEqual(IdentityCheck.objects.get(pk=check.pk).status, S.REVIEW)
        self.assertEqual(self.decide(check, True, client=Client()).status_code, 401)
        self.assertEqual(AuditLog.objects.get(action="id.decide").outcome, "refused")

    def test_a_bad_request_is_refused(self):
        check = self.in_review()
        url = reverse("arabela_admin:id_check_decide", args=[check.pk])
        for body in ({}, {"approve": "yes"}, {"approve": 1}, {"approve": None}):
            self.assertEqual(self.post_json(self.owner_client, url, body).status_code, 400)
        self.assertEqual(self.owner_client.post(url, data=b"not json", content_type="application/json").status_code, 400)
        self.assertEqual(self.owner_client.get(url).status_code, 405)
        self.assertEqual(IdentityCheck.objects.get(pk=check.pk).status, S.REVIEW)

    def test_a_double_click_decides_once(self):
        check = self.in_review()
        self.assertEqual(self.decide(check, True).status_code, 200)
        second = self.decide(check, False)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(IdentityCheck.objects.get(pk=check.pk).status, S.APPROVED)
        self.assertEqual(AuditLog.objects.filter(action="id.decide").count(), 1)

    def test_only_a_check_waiting_for_review_can_be_decided(self):
        approved = self.verified_online(self.customer)
        self.assertEqual(self.decide(approved, False).status_code, 409)
        self.assertEqual(self.post_json(self.owner_client, reverse("arabela_admin:id_check_decide", args=[999999]), {"approve": True}).status_code, 404)


# ---------------------------------------------------------------- deleting and copying pictures
class PictureManagementTests(Base):
    def test_the_owner_deletes_the_pictures_and_the_files_go(self):
        check = self.verified_online()
        names = list(check.photos.values_list("storage_name", flat=True))
        response = self.post_json(self.owner_client, reverse("arabela_admin:id_check_delete_photos", args=[check.pk]))
        self.assertEqual((response.status_code, response.json()), (200, {"success": True, "deleted": 2}))
        self.assertFalse(any(default_storage.exists(name) for name in names))
        check.refresh_from_db()
        self.assertEqual(check.photos_state, "deleted")
        self.assertTrue(check.is_valid)
        self.assertIn("deleted the pictures of the ID of Ana Cruz", AuditLog.objects.get(action="id.delete_photos").summary)
        again = self.post_json(self.owner_client, reverse("arabela_admin:id_check_delete_photos", args=[check.pk]))
        self.assertEqual(again.json()["deleted"], 0)                                    # nothing left: harmless
        opened = self.owner_client.get(reverse("arabela_admin:id_check_open", args=[check.pk])).json()
        self.assertEqual((opened["photosList"], opened["photosWords"], opened["photos"], opened["canDelete"]), ([], "Deleted", "deleted", False))    # the window explains it

    def test_staff_cannot_delete_pictures(self):
        check = self.verified_online()
        response = self.post_json(self.staff_client, reverse("arabela_admin:id_check_delete_photos", args=[check.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(check.photos.count(), 2)
        self.assertEqual(self.post_json(self.owner_client, reverse("arabela_admin:id_check_delete_photos", args=[999999])).status_code, 404)

    def test_a_failed_copy_can_be_tried_again_by_the_owner(self):
        started = identity.start_for_customer(user=self.ana, id_type="philid", consent=True, callback_for=self.callback_for)
        check = IdentityCheck.objects.get(token=started["token"])
        self.fake.finish(check.session_id, "Approved")
        self.fake.bad_pictures = {"front.jpg", "back.jpg"}
        IdentityCheck.objects.filter(pk=check.pk).update(last_checked_at=None)
        check = identity.refresh(check.pk, source="poll")
        self.assertEqual(check.photos_state, "failed")
        opened = self.owner_client.get(reverse("arabela_admin:id_check_open", args=[check.pk])).json()
        self.assertEqual((opened["canRetry"], opened["photosWords"]), (True, "The pictures could not be copied"))
        self.fake.bad_pictures = set()
        response = self.post_json(self.owner_client, reverse("arabela_admin:id_check_retry_photos", args=[check.pk]))
        self.assertEqual((response.status_code, response.json()["kept"], response.json()["photos"]), (200, True, "stored"))
        self.assertEqual(check.photos.count(), 2)
        self.assertEqual(AuditLog.objects.filter(action="id.retry_photos").count(), 1)

    def test_pictures_that_are_fine_or_deleted_cannot_be_copied_again(self):
        check = self.verified_online()
        url = reverse("arabela_admin:id_check_retry_photos", args=[check.pk])
        self.assertEqual(self.post_json(self.owner_client, url).status_code, 409)
        self.assertEqual(self.post_json(self.staff_client, url).status_code, 403)
        self.assertEqual(self.post_json(self.owner_client, reverse("arabela_admin:id_check_retry_photos", args=[999999])).status_code, 404)


# ---------------------------------------------------------------- the owner's switch
class SwitchTests(Base):
    def put(self, client, body):
        return self.post_json(client, reverse("arabela_admin:id_setting_update"), body)

    def test_the_owner_turns_the_rule_off_and_on_and_it_is_written_with_before_and_after(self):
        self.assertTrue(identity.switch_on())
        self.assertEqual(self.put(self.owner_client, {"require": False}).json(), {"success": True, "require": False})
        self.assertFalse(identity.switch_on())
        self.assertFalse(identity.required_online())
        self.assertEqual(self.put(self.owner_client, {"require": True}).json()["require"], True)
        rows = list(AuditLog.objects.filter(action="settings.require_id").order_by("id"))
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0].sensitive, rows[0].category), (True, "Settings"))
        self.assertIn("turned OFF", rows[0].summary)
        self.assertEqual(rows[0].details["Valid ID required"], [True, False])
        self.assertIn("turned ON", rows[1].summary)

    def test_staff_cannot_change_it(self):
        response = self.put(self.staff_client, {"require": False})
        self.assertEqual((response.status_code, response.json()["error"]), (403, "Only the owner can do that."))
        self.assertTrue(identity.switch_on())
        self.assertEqual(self.put(Client(), {"require": False}).status_code, 401)
        self.assertEqual(AuditLog.objects.get(action="settings.require_id").outcome, "refused")

    def test_a_bad_request_is_refused(self):
        for body in ({}, {"require": "no"}, {"require": 0}, {"require": None}, []):
            self.assertEqual(self.put(self.owner_client, body).status_code, 400)
        self.assertEqual(self.owner_client.post(reverse("arabela_admin:id_setting_update"), data=b"x", content_type="application/json").status_code, 400)
        self.assertEqual(self.owner_client.get(reverse("arabela_admin:id_setting_update")).status_code, 405)
        self.assertTrue(identity.switch_on())

    def test_the_edit_profile_page_shows_the_owner_the_card_and_hides_it_from_staff(self):
        profile = reverse("arabela_admin:page", kwargs={"page": "profile"})
        page = self.owner_client.get(profile).content.decode()
        self.assertIn('x-data="idRuleCard()"', page)
        self.assertIn('data-on="1"', page)
        self.assertIn("test mode", page)
        set_switch(False)
        self.assertIn('data-on="0"', self.owner_client.get(profile).content.decode())
        self.assertNotIn('x-data="idRuleCard()"', self.staff_client.get(profile).content.decode())


# ---------------------------------------------------------------- the walk-in ID row
class WalkInEndpointTests(Base):
    def start(self, client=None, **extra):
        data = {"name": "Lorna Reyes", "phone": "0917 123 4567", "id_type": "philid"}
        data.update(extra)
        return (client or self.staff_client).post(reverse("arabela_admin:booking_id_start"), data=data)

    def test_staff_start_a_scan_and_get_a_qr_code_for_the_customers_phone(self):
        response = self.start()
        body = response.json()
        self.assertEqual((response.status_code, body["success"], body["state"], body["phone"]), (200, True, "checking", PHONE))
        self.assertTrue(body["url"].startswith("https://verify.didit.me/session/"))
        self.assertTrue(body["qr"].startswith("data:image/png;base64,"))
        check = IdentityCheck.objects.get()
        self.assertEqual((check.source, check.staff, check.subject_name, check.id_type), ("walkin", self.staff, "Lorna Reyes", "philid"))
        self.assertTrue(body["statusUrl"].endswith(f"/{check.token}/status/"))
        self.assertIn("opened an ID scan (QR code) for Lorna Reyes", AuditLog.objects.get(action="id.walkin_start").summary)

    def test_the_same_customer_pressed_again_is_the_same_qr_code(self):
        first, second = self.start().json(), self.start().json()
        self.assertEqual((first["token"], first["url"]), (second["token"], second["url"]))
        self.assertEqual(IdentityCheck.objects.count(), 1)

    def test_it_needs_the_customer_and_a_real_id_type(self):
        for extra in (dict(name=""), dict(phone=""), dict(phone="123"), dict(id_type="library_card"), dict(id_type="")):
            self.assertEqual(self.start(**extra).status_code, 400)
        self.assertFalse(IdentityCheck.objects.exists())

    def test_only_signed_in_staff_can_start_one(self):
        self.assertEqual(self.start(client=Client()).status_code, 401)
        self.assertEqual(self.start(client=self.login(self.customer)).status_code, 401)
        self.assertEqual(self.staff_client.get(reverse("arabela_admin:booking_id_start")).status_code, 405)
        self.assertFalse(IdentityCheck.objects.exists())

    def test_without_didit_the_scan_says_to_use_the_counter(self):
        with override_settings(DIDIT_API_KEY=""):
            response = self.start()
        self.assertEqual(response.status_code, 503)
        self.assertIn("Checked at the counter", response.json()["error"])

    def test_the_screen_asks_and_sees_the_result_when_the_customer_finishes(self):
        body = self.start().json()
        url = body["statusUrl"]
        self.assertEqual(self.staff_client.get(url).json()["state"], "checking")
        check = IdentityCheck.objects.get(token=body["token"])
        self.fake.finish(check.session_id, "Approved", name="LORNA REYES")
        IdentityCheck.objects.filter(pk=check.pk).update(last_checked_at=None)
        after = self.staff_client.get(url).json()
        self.assertEqual((after["state"], after["token"], after["phone"]), ("approved", check.token, PHONE))
        self.assertTrue(after["until"])
        self.assertEqual(Client().get(url).status_code, 401)

    def test_a_declined_scan_says_so_with_the_reasons(self):
        body = self.start().json()
        check = IdentityCheck.objects.get(token=body["token"])
        self.fake.finish(check.session_id, "Declined", warnings=[{"risk": "DOCUMENT_EXPIRED", "log_type": "error", "short_description": "x"}], images=False)
        IdentityCheck.objects.filter(pk=check.pk).update(last_checked_at=None)
        after = self.staff_client.get(body["statusUrl"]).json()
        self.assertEqual((after["state"], after["reasons"]), ("declined", ["The ID has expired."]))

    def test_the_status_is_only_for_a_walk_ins_own_check(self):
        online = self.verified_online()
        self.assertEqual(self.staff_client.get(reverse("arabela_admin:booking_id_status", args=[online.token])).status_code, 404)
        self.assertEqual(self.staff_client.get(reverse("arabela_admin:booking_id_status", args=["nonsense"])).status_code, 404)

    def test_the_counter_check_needs_the_tick_and_records_who_and_when(self):
        url = reverse("arabela_admin:booking_id_counter")
        data = {"name": "Lorna Reyes", "phone": PHONE, "id_type": "drivers_license", "note": "Original licence, expires 2029"}
        self.assertEqual(self.staff_client.post(url, data=data).status_code, 400)                       # no tick
        self.assertEqual(self.staff_client.post(url, data={**data, "confirmed": "0"}).status_code, 400)
        response = self.staff_client.post(url, data={**data, "confirmed": "1"})
        body = response.json()
        self.assertEqual((response.status_code, body["state"], body["counter"], body["staffName"], body["phone"]), (200, "approved", True, "Maria Cruz", PHONE))
        check = IdentityCheck.objects.get(token=body["token"])
        self.assertEqual((check.source, check.status, check.decided_by, check.note), ("counter", "approved", self.staff, "Original licence, expires 2029"))
        self.assertIn("recorded that the original ID of Lorna Reyes was checked at the counter", AuditLog.objects.get(action="id.counter_check").summary)

    def test_the_counter_check_needs_the_customer_and_a_staff_session(self):
        url = reverse("arabela_admin:booking_id_counter")
        good = {"name": "Lorna Reyes", "phone": PHONE, "id_type": "philid", "confirmed": "1"}
        for extra in (dict(name=""), dict(phone="12"), dict(id_type="nope")):
            self.assertEqual(self.staff_client.post(url, data={**good, **extra}).status_code, 400)
        self.assertEqual(Client().post(url, data=good).status_code, 401)
        self.assertEqual(self.login(self.customer).post(url, data=good).status_code, 401)
        self.assertFalse(IdentityCheck.objects.exists())

    def test_staff_can_close_a_qr_code_that_is_still_open_and_not_one_that_finished(self):
        body = self.start().json()
        url = body["cancelUrl"]
        self.assertEqual(self.staff_client.post(url).json()["state"], "ended")
        self.assertEqual(IdentityCheck.objects.get(token=body["token"]).status, S.CANCELLED)
        done = self.counter()
        self.assertEqual(self.staff_client.post(reverse("arabela_admin:booking_id_cancel", args=[done.token])).status_code, 404)        # a counter check is not a QR code
        self.assertEqual(AuditLog.objects.filter(action="id.walkin_cancel").count(), 1)
        self.assertEqual(Client().post(url).status_code, 401)

    def test_a_cancelled_qr_code_is_not_reopened_by_what_the_customer_does_later(self):
        body = self.start().json()
        self.staff_client.post(body["cancelUrl"])
        check = IdentityCheck.objects.get(token=body["token"])
        self.fake.finish(check.session_id, "Approved")
        IdentityCheck.objects.filter(pk=check.pk).update(last_checked_at=None)
        self.assertEqual(identity.refresh(check.pk, source="poll").status, S.CANCELLED)
        with self.assertRaises(identity.IdentityError):
            identity.usable_walkin_check(check.token, phone=PHONE)

    def test_the_account_search_says_whether_an_account_is_already_verified(self):
        self.verified_online()
        results = self.staff_client.get(reverse("arabela_admin:booking_account_search"), {"q": "Ana"}).json()["results"]
        self.assertEqual((results[0]["name"], results[0]["idVerified"]), ("Ana Cruz", True))
        self.assertTrue(results[0]["idUntil"])
        other = self.staff_client.get(reverse("arabela_admin:booking_account_search"), {"q": "ida_customer"}).json()["results"] or self.staff_client.get(
            reverse("arabela_admin:booking_account_search"), {"q": "ida.customer"}).json()["results"]
        self.assertEqual(other[0]["idVerified"], False)


# ---------------------------------------------------------------- the rule itself: a walk-in booking needs the customer's ID checked
class WalkInRuleTests(Base):
    def line(self, gown=None):
        return {"gown": (gown or self.gown).id, "event": self.event.isoformat(), "takes_now": False, "picked_up_on": ""}

    def book(self, *, id_check=None, account=None, phone="0917 123 4567", gown=None, client=None, **extra):
        gown = gown or self.gown
        data = {"channel": "Walk-in", "name": "Lorna Reyes", "phone": phone, "address": "Antipolo", "account": account.id if account else "", "lines": json.dumps([self.line(gown)]),
                "payment": "full", "method": "Cash", "amount": str(gown.rental_price + Decimal("2000")), "request_id": "ida-req-%s" % gown.id}
        if id_check is not None:
            data["id_check"] = id_check if isinstance(id_check, str) else id_check.token
        data.update(extra)
        return (client or self.staff_client).post(reverse("arabela_admin:booking_create"), data=data)

    def test_a_booking_without_an_id_check_is_refused_with_words_for_staff(self):
        response = self.book()
        body = response.json()
        self.assertEqual((response.status_code, body["id_required"]), (400, True))
        self.assertIn("Check the customer's valid ID first", body["error"])
        self.assertFalse(Reservation.objects.exists())

    def test_a_check_at_the_counter_lets_it_through_and_the_booking_remembers_it(self):
        check = self.counter()
        response = self.book(id_check=check)
        self.assertEqual(response.status_code, 200, response.content)
        reservation = Reservation.objects.get()
        self.assertEqual(reservation.identity_check, check)
        self.assertEqual(identity.badge(reservation)["words"], "ID checked at the counter")

    def test_a_scan_that_finished_lets_it_through_too(self):
        body = self.staff_client.post(reverse("arabela_admin:booking_id_start"), data={"name": "Lorna Reyes", "phone": PHONE, "id_type": "philid"}).json()
        check = IdentityCheck.objects.get(token=body["token"])
        self.fake.finish(check.session_id, "Approved", name="LORNA REYES")
        IdentityCheck.objects.filter(pk=check.pk).update(last_checked_at=None)
        identity.refresh(check.pk, source="poll")
        self.assertEqual(self.book(id_check=check).status_code, 200)
        self.assertEqual(Reservation.objects.get().identity_check, check)

    def test_a_scan_still_waiting_declined_or_in_review_does_not_count(self):
        body = self.staff_client.post(reverse("arabela_admin:booking_id_start"), data={"name": "Lorna Reyes", "phone": PHONE, "id_type": "philid"}).json()
        check = IdentityCheck.objects.get(token=body["token"])
        for status in (S.PENDING, S.REVIEW, S.DECLINED, S.EXPIRED, S.CANCELLED):
            IdentityCheck.objects.filter(pk=check.pk).update(status=status)
            response = self.book(id_check=check)
            self.assertEqual((response.status_code, response.json()["id_required"]), (409, True), status)
        self.assertFalse(Reservation.objects.exists())

    def test_a_check_made_for_another_number_is_refused(self):
        check = self.counter()
        response = self.book(id_check=check, phone="0918 000 0000")
        self.assertEqual((response.status_code, response.json()["id_required"]), (409, True))
        self.assertIn("different mobile number", response.json()["error"])

    def test_one_check_backs_one_booking(self):
        check = self.counter()
        self.assertEqual(self.book(id_check=check).status_code, 200)
        second = self.book(id_check=check, gown=self.gown2)
        self.assertEqual(second.status_code, 409)
        self.assertIn("already used", second.json()["error"])
        self.assertEqual(Reservation.objects.count(), 1)

    def test_two_staff_pressing_save_with_the_same_check_make_one_booking(self):
        check = self.counter()
        with patch("arabela_admin.new_booking.identity.walkin_requirement", return_value=check):
            # the pre-check can be fooled by two simultaneous requests; the booking's own locked check is the one that stops the second
            self.assertEqual(self.book(id_check=check).status_code, 200)
            second = self.book(id_check=check, gown=self.gown2)
            self.assertEqual(second.status_code, 409)
            self.assertIn("already used", second.json()["error"])
        self.assertEqual(Reservation.objects.count(), 1)

    def test_a_check_that_is_too_old_is_refused(self):
        check = self.counter()
        IdentityCheck.objects.filter(pk=check.pk).update(created_at=timezone.now() - timedelta(hours=13))
        self.assertEqual(self.book(id_check=check).status_code, 409)

    def test_a_made_up_token_is_refused(self):
        self.assertEqual(self.book(id_check="nonsense").status_code, 409)

    def test_an_online_customers_token_cannot_be_used_for_a_walk_in(self):
        online = self.verified_online()
        self.assertEqual(self.book(id_check=online).status_code, 409)

    def test_a_verified_website_account_linked_to_the_booking_is_enough(self):
        online = self.verified_online()
        response = self.book(account=self.ana)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Reservation.objects.get().identity_check, online)

    def test_a_website_account_that_has_not_verified_is_not_enough(self):
        response = self.book(account=self.customer)
        self.assertEqual((response.status_code, response.json()["id_required"]), (400, True))

    def test_when_the_owner_turns_the_rule_off_a_booking_needs_no_check_and_a_good_one_is_still_kept(self):
        set_switch(False)
        self.assertEqual(self.book().status_code, 200)
        self.assertIsNone(Reservation.objects.get().identity_check)
        check = self.counter(name="Cora Diaz", phone="09170000000")
        self.assertEqual(self.book(id_check=check, phone="09170000000", gown=self.gown2).status_code, 200)
        self.assertEqual(Reservation.objects.get(phone="09170000000").identity_check, check)

    def test_the_same_rule_holds_for_the_online_payment_qr(self):
        url = reverse("arabela_admin:booking_pay_start")
        data = {"channel": "Walk-in", "name": "Lorna Reyes", "phone": PHONE, "address": "Antipolo", "lines": json.dumps([self.line()]), "payment": "full", "request_id": "ida-qr-0000001",
                "expected_amount": "5000"}
        refused = self.staff_client.post(url, data=data)
        self.assertEqual((refused.status_code, refused.json()["id_required"]), (400, True))
        self.assertFalse(PaymentAttempt.objects.exists())
        check = self.counter()
        opened = self.staff_client.post(url, data={**data, "id_check": check.token})
        self.assertEqual(opened.status_code, 200, opened.content)
        attempt = PaymentAttempt.objects.get()
        self.assertEqual(attempt.payload["identity_check"], check.pk)
        self.xendit.paid(attempt.session_id)
        PaymentAttempt.objects.filter(pk=attempt.pk).update(last_checked_at=None)
        self.staff_client.get(reverse("arabela_admin:booking_pay_status", args=[attempt.token]))
        reservation = Reservation.objects.get()
        self.assertEqual(reservation.identity_check, check)

    def test_a_paid_online_booking_is_never_refused_over_its_id_link(self):
        check = self.counter()
        url = reverse("arabela_admin:booking_pay_start")
        data = {"channel": "Walk-in", "name": "Lorna Reyes", "phone": PHONE, "address": "Antipolo", "lines": json.dumps([self.line()]), "payment": "full", "request_id": "ida-qr-0000002",
                "expected_amount": "5000", "id_check": check.token}
        attempt = PaymentAttempt.objects.get(token=self.staff_client.post(url, data=data).json()["token"])
        # the same check is used for a cash booking before the customer finishes paying
        self.assertEqual(self.book(id_check=check, gown=self.gown2).status_code, 200)
        self.xendit.paid(attempt.session_id)
        PaymentAttempt.objects.filter(pk=attempt.pk).update(last_checked_at=None)
        self.staff_client.get(reverse("arabela_admin:booking_pay_status", args=[attempt.token]))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.BOOKED)                      # the money was paid: the booking is made
        self.assertIsNone(Reservation.objects.get(pk=attempt.reservation_id).identity_check)

    def test_the_new_booking_page_knows_whether_the_rule_is_on(self):
        page = self.staff_client.get(reverse("arabela_admin:new_booking")).content.decode()
        self.assertIn("var ID_REQUIRED = true;", page)
        set_switch(False)
        self.assertIn("var ID_REQUIRED = false;", self.staff_client.get(reverse("arabela_admin:new_booking")).content.decode())

    def test_the_ids_do_not_leak_into_the_booking_lists_for_staff(self):
        check = self.counter()
        self.book(id_check=check)
        listing = self.staff_client.get(reverse("arabela_admin:reservation_records")).content.decode()
        self.assertNotIn(check.token, listing)
        self.assertNotIn("id_photos/", listing)


# ---------------------------------------------------------------- the confirm box
class AdminDialogNameTests(SimpleTestCase):
    def test_no_admin_page_asks_the_customer_sites_dialog_for_anything(self):
        # arabelaConfirm / arabelaAlert / arabelaPrompt only exist on the customer website. The admin panel's own dialog is adminConfirm / adminAlert / adminPrompt
        # (arabela_admin/partials/dialog.html): a page that asks the other one gets no box at all (or the browser's plain pop-up), and a Cancel would go ahead unasked.
        root = Path(settings.BASE_DIR) / "templates" / "arabela_admin"
        offenders = sorted(str(path.relative_to(root)) for path in root.rglob("*.html")
                           if any(name in path.read_text(encoding="utf-8") for name in ("arabelaConfirm", "arabelaAlert", "arabelaPrompt")))
        self.assertEqual(offenders, [])

    def test_the_pages_that_ask_for_a_confirmation_have_the_admin_dialog_on_them(self):
        root = Path(settings.BASE_DIR) / "templates" / "arabela_admin"
        for name in ("id-checks.html", "new-booking.html"):
            text = (root / name).read_text(encoding="utf-8")
            with self.subTest(page=name):
                self.assertIn("adminConfirm", text)
                self.assertIn('{% include "arabela_admin/partials/dialog.html" %}', text)
