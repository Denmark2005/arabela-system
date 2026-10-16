"""Saved addresses live on the customer's ACCOUNT (accounts.SavedAddress), not in one browser.

They used to be kept in localStorage only, so a default address never followed a customer to another phone and vanished when the browser's
data was cleared. The profile page now saves them through three endpoints (save, delete, import), every answer carries the customer's whole
list, and the checkout fills its boxes from the default one. These tests cover the rules the server enforces: whose addresses a customer can
touch, the field checks, one default at most, the limit, and the one-time copy of the addresses a browser already had."""
import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import SavedAddress, UserProfile

User = get_user_model()
GOOD = {"addressLine": "12 Rizal St", "city": "Antipolo City", "postalCode": "1870", "phone": "09171234567"}


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ana = User.objects.create_user("addr_ana", password="x", email="ana@gmail.com")
        cls.ben = User.objects.create_user("addr_ben", password="x", email="ben@gmail.com")
        UserProfile.objects.update_or_create(user=cls.ana, defaults={"display_name": "Ana Cruz"})

    def setUp(self):
        self.client.force_login(self.ana)

    def post(self, name, body=None, args=(), client=None, raw=None):
        client = client or self.client
        return client.post(reverse(f"gowns:{name}", args=args), data=raw if raw is not None else json.dumps(body or {}),
                           content_type="application/json")

    def save(self, **fields):
        return self.post("profile_address_save", {**GOOD, **fields})

    def mine(self, user=None):
        return list(SavedAddress.objects.filter(user=user or self.ana))


class SavedAddressModelTests(Base):
    def test_the_default_comes_first_then_the_order_they_were_added(self):
        first = SavedAddress.objects.create(user=self.ana, address_line="A", city="c", postal_code="1111", phone="09111111111")
        second = SavedAddress.objects.create(user=self.ana, address_line="B", city="c", postal_code="1111", phone="09111111111", is_default=True)
        third = SavedAddress.objects.create(user=self.ana, address_line="C", city="c", postal_code="1111", phone="09111111111")
        self.assertEqual(self.mine(), [second, first, third])

    def test_the_database_itself_refuses_a_second_default(self):
        SavedAddress.objects.create(user=self.ana, address_line="A", city="c", postal_code="1111", phone="09111111111", is_default=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SavedAddress.objects.create(user=self.ana, address_line="B", city="c", postal_code="1111", phone="09111111111", is_default=True)

    def test_every_customer_has_their_own_default(self):
        for user in (self.ana, self.ben):
            SavedAddress.objects.create(user=user, address_line="A", city="c", postal_code="1111", phone="09111111111", is_default=True)
        self.assertEqual(SavedAddress.objects.filter(is_default=True).count(), 2)

    def test_deleting_an_account_removes_its_addresses(self):
        SavedAddress.objects.create(user=self.ben, address_line="A", city="c", postal_code="1111", phone="09111111111")
        self.ben.delete()
        self.assertFalse(SavedAddress.objects.filter(address_line="A").exists())


class WhoMayUseItTests(Base):
    def test_only_a_signed_in_customer_can_use_the_endpoints(self):
        mine = SavedAddress.objects.create(user=self.ana, address_line="A", city="c", postal_code="1111", phone="09111111111")
        anonymous = Client()
        for name, args, body in (("profile_address_save", (), GOOD), ("profile_address_delete", (mine.pk,), {}),
                                 ("profile_address_import", (), {"addresses": [GOOD]})):
            response = self.post(name, body, args=args, client=anonymous)
            self.assertEqual(response.status_code, 401, name)
            self.assertFalse(response.json()["success"])
        self.assertEqual(self.mine(), [mine])                                         # nothing changed

    def test_they_only_answer_to_post(self):
        mine = SavedAddress.objects.create(user=self.ana, address_line="A", city="c", postal_code="1111", phone="09111111111")
        for name, args in (("profile_address_save", ()), ("profile_address_delete", (mine.pk,)), ("profile_address_import", ())):
            self.assertEqual(self.client.get(reverse(f"gowns:{name}", args=args)).status_code, 405, name)

    def test_a_customer_cannot_change_or_delete_someone_elses_address(self):
        theirs = SavedAddress.objects.create(user=self.ben, address_line="Ben's", city="c", postal_code="1111", phone="09111111111")
        response = self.save(id=theirs.pk, addressLine="Hacked")
        self.assertEqual(response.status_code, 404)
        self.post("profile_address_delete", args=(theirs.pk,))
        theirs.refresh_from_db()
        self.assertEqual(theirs.address_line, "Ben's")                                # untouched, and still there

    def test_the_list_a_customer_gets_back_is_only_their_own(self):
        SavedAddress.objects.create(user=self.ben, address_line="Ben's", city="c", postal_code="1111", phone="09111111111")
        response = self.save()
        self.assertEqual([a["addressLine"] for a in response.json()["addresses"]], ["12 Rizal St"])


class SavingTests(Base):
    def test_adding_an_address_saves_it_trimmed_and_returns_the_whole_list(self):
        response = self.save(addressLine="  12 Rizal St  ", city=" Antipolo City ")
        self.assertEqual(response.status_code, 200, response.content)
        [address] = self.mine()
        self.assertEqual((address.address_line, address.city, address.postal_code, address.phone, address.is_default),
                         ("12 Rizal St", "Antipolo City", "1870", "09171234567", False))
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["addresses"], [{"id": address.pk, "addressLine": "12 Rizal St", "city": "Antipolo City",
                                              "postalCode": "1870", "phone": "09171234567", "isDefault": False}])

    def test_every_box_is_required_with_the_pages_own_words(self):
        for field in ("addressLine", "city", "postalCode", "phone"):
            response = self.save(**{field: "   "})
            self.assertEqual(response.status_code, 400, field)
            self.assertEqual(response.json()["errors"], {field: "This field is required."})
        self.assertEqual(self.mine(), [])

    def test_the_postal_code_is_four_digits_and_the_phone_eleven(self):
        for postal in ("123", "12345", "12a4", "١٢٣٤", "12 34"):
            self.assertEqual(self.save(postalCode=postal).json()["errors"], {"postalCode": "Zip code is not valid"}, postal)
        for phone in ("0917123456", "091712345678", "0917-1234567", "٠٩١٧١٢٣٤٥٦٧"):
            self.assertEqual(self.save(phone=phone).json()["errors"], {"phone": "Number invalid"}, phone)
        self.assertEqual(self.mine(), [])

    def test_everything_wrong_at_once_is_reported_at_once(self):
        response = self.post("profile_address_save", {"addressLine": "", "city": "", "postalCode": "1", "phone": "2"})
        self.assertEqual(set(response.json()["errors"]), {"addressLine", "city", "postalCode", "phone"})

    def test_too_long_text_is_refused_not_cut(self):
        self.assertEqual(self.save(addressLine="x" * 256).json()["errors"], {"addressLine": "Address is too long."})
        self.assertEqual(self.save(city="x" * 101).json()["errors"], {"city": "City is too long."})
        self.assertEqual(self.save(addressLine="x" * 255).status_code, 200)

    def test_wrong_kinds_of_values_are_refused_not_a_crash(self):
        for body in ({"addressLine": 5, "city": ["x"], "postalCode": None, "phone": {}}, [], "text"):
            response = self.post("profile_address_save", raw=json.dumps(body))
            self.assertEqual(response.status_code, 400, body)
        for raw in ("not json", ""):
            self.assertEqual(self.post("profile_address_save", raw=raw).status_code, 400, raw)
        self.assertEqual(self.mine(), [])

    def test_the_id_must_be_a_real_number(self):
        for bad in ("5", True, 1.5, [1], {}):
            self.assertEqual(self.save(id=bad).status_code, 400, bad)
        self.assertEqual(self.save(id=99999).status_code, 404)

    def test_changing_an_address_changes_that_one(self):
        first = self.mine_after(self.save(addressLine="First"))
        self.save(addressLine="Second")
        self.assertEqual(self.save(id=first.pk, addressLine="First, updated", phone="09999999999").status_code, 200)
        first.refresh_from_db()
        self.assertEqual((first.address_line, first.phone), ("First, updated", "09999999999"))
        self.assertEqual(len(self.mine()), 2)

    def mine_after(self, response):
        return SavedAddress.objects.get(pk=response.json()["addresses"][0]["id"])


class OneDefaultTests(Base):
    def defaults(self):
        return [a.address_line for a in self.mine() if a.is_default]

    def test_making_one_the_default_takes_it_from_the_old_default(self):
        self.save(addressLine="First", isDefault=True)
        self.assertEqual(self.defaults(), ["First"])
        self.save(addressLine="Second", isDefault=True)
        self.assertEqual(self.defaults(), ["Second"])                                 # exactly one, and it is the new one

    def test_editing_an_old_address_into_the_default_moves_the_default(self):
        self.save(addressLine="First", isDefault=True)
        second = SavedAddress.objects.get(pk=self.save(addressLine="Second").json()["addresses"][-1]["id"])
        self.assertEqual(self.save(id=second.pk, addressLine="Second", isDefault=True).status_code, 200)
        self.assertEqual(self.defaults(), ["Second"])

    def test_unticking_the_default_leaves_no_default_and_the_list_still_leads_with_the_oldest(self):
        first = SavedAddress.objects.get(pk=self.save(addressLine="First", isDefault=True).json()["addresses"][0]["id"])
        self.save(addressLine="Second")
        self.save(id=first.pk, addressLine="First", isDefault=False)
        self.assertEqual(self.defaults(), [])
        self.assertEqual([a.address_line for a in self.mine()], ["First", "Second"])

    def test_the_default_list_order_is_what_the_page_gets(self):
        self.save(addressLine="First")
        response = self.save(addressLine="Second", isDefault=True)
        self.assertEqual([a["addressLine"] for a in response.json()["addresses"]], ["Second", "First"])
        self.assertEqual([a["isDefault"] for a in response.json()["addresses"]], [True, False])

    def test_a_default_that_is_not_exactly_true_is_not_a_default(self):
        for value in ("true", 1, "yes"):
            self.save(addressLine=f"x{value}", isDefault=value)
        self.assertEqual(self.defaults(), [])

    def test_one_customers_default_never_touches_anothers(self):
        SavedAddress.objects.create(user=self.ben, address_line="Ben's", city="c", postal_code="1111", phone="09111111111", is_default=True)
        self.save(addressLine="Ana's", isDefault=True)
        self.assertEqual(self.defaults(), ["Ana's"])
        self.assertTrue(SavedAddress.objects.get(address_line="Ben's").is_default)


class LimitAndDeleteTests(Base):
    def test_a_customer_can_save_five_and_the_sixth_is_refused(self):
        for n in range(SavedAddress.MAX_PER_CUSTOMER):
            self.assertEqual(self.save(addressLine=f"Address {n}").status_code, 200)
        response = self.save(addressLine="One too many")
        self.assertEqual(response.status_code, 400)
        self.assertIn("up to 5 addresses", response.json()["error"])
        self.assertEqual(len(self.mine()), 5)

    def test_a_customer_at_the_limit_can_still_edit_and_then_add_after_deleting(self):
        for n in range(5):
            self.save(addressLine=f"Address {n}")
        first = self.mine()[0]
        self.assertEqual(self.save(id=first.pk, addressLine="Edited").status_code, 200)
        self.post("profile_address_delete", args=(first.pk,))
        self.assertEqual(self.save(addressLine="Room again").status_code, 200)
        self.assertEqual(len(self.mine()), 5)

    def test_the_limit_is_per_customer(self):
        for n in range(5):
            SavedAddress.objects.create(user=self.ben, address_line=f"B{n}", city="c", postal_code="1111", phone="09111111111")
        self.assertEqual(self.save().status_code, 200)

    def test_deleting_removes_it_and_a_second_tap_is_harmless(self):
        address = SavedAddress.objects.get(pk=self.save().json()["addresses"][0]["id"])
        first = self.post("profile_address_delete", args=(address.pk,))
        self.assertEqual((first.status_code, first.json()["addresses"]), (200, []))
        second = self.post("profile_address_delete", args=(address.pk,))
        self.assertEqual((second.status_code, second.json()["success"]), (200, True))
        self.assertEqual(self.mine(), [])


class ImportTests(Base):
    LEGACY = [
        {"addressLine": "Old One", "city": "C1", "postalCode": "1111", "phone": "09111111111", "isDefault": False},
        {"addressLine": "Old Two", "city": "C2", "postalCode": "2222", "phone": "09222222222", "isDefault": True},
    ]

    def run_import(self, addresses, client=None):
        return self.post("profile_address_import", {"addresses": addresses}, client=client)

    def test_an_account_with_none_takes_over_what_the_browser_had(self):
        response = self.run_import(self.LEGACY)
        body = response.json()
        self.assertEqual((response.status_code, body["imported"], body["skipped"]), (200, 2, 0))
        self.assertEqual([(a["addressLine"], a["isDefault"]) for a in body["addresses"]], [("Old Two", True), ("Old One", False)])

    def test_it_never_copies_into_an_account_that_already_has_addresses(self):
        self.save(addressLine="Already here")
        body = self.run_import(self.LEGACY).json()
        self.assertEqual((body["imported"], len(body["addresses"])), (0, 1))
        self.assertEqual([a.address_line for a in self.mine()], ["Already here"])

    def test_calling_it_twice_cannot_duplicate_anything(self):
        self.run_import(self.LEGACY)
        self.run_import(self.LEGACY)
        self.assertEqual(len(self.mine()), 2)

    def test_two_devices_importing_at_once_end_with_one_copy(self):
        other_phone = Client()
        other_phone.force_login(self.ana)
        self.run_import(self.LEGACY)
        self.run_import(self.LEGACY, client=other_phone)
        self.assertEqual(len(self.mine()), 2)

    def test_only_one_default_survives_and_exact_repeats_are_dropped(self):
        both_default = [dict(self.LEGACY[0], isDefault=True), dict(self.LEGACY[1], isDefault=True), dict(self.LEGACY[1], isDefault=True)]
        body = self.run_import(both_default).json()
        self.assertEqual((body["imported"], body["skipped"]), (2, 1))
        self.assertEqual([a.address_line for a in self.mine() if a.is_default], ["Old One"])   # the first one marked

    def test_addresses_saved_under_looser_old_rules_are_not_lost(self):
        sloppy = [{"addressLine": "Sloppy St", "city": "", "postalCode": "111", "phone": "123", "isDefault": True}]
        self.assertEqual(self.run_import(sloppy).json()["imported"], 1)               # a strict check would have thrown it away
        [address] = self.mine()
        self.assertEqual((address.city, address.postal_code, address.phone, address.is_default), ("", "111", "123", True))

    def test_an_entry_with_no_address_at_all_is_skipped_and_long_text_is_cut_to_fit(self):
        body = self.run_import([{"city": "Nowhere"}, "junk", None, dict(GOOD, addressLine="y" * 400)]).json()
        self.assertEqual((body["imported"], body["skipped"]), (1, 3))
        self.assertEqual(len(self.mine()[0].address_line), 255)

    def test_more_than_the_limit_keeps_the_first_five_and_says_so(self):
        many = [dict(GOOD, addressLine=f"Address {n}") for n in range(8)]
        body = self.run_import(many).json()
        self.assertEqual((body["imported"], body["skipped"], len(self.mine())), (5, 3, 5))

    def test_a_bad_request_is_refused(self):
        for raw in ('{"addresses": "x"}', '{"nothing": 1}', "[]", "nope"):
            self.assertEqual(self.post("profile_address_import", raw=raw).status_code, 400, raw)
        too_many = {"addresses": [GOOD] * 51}
        self.assertEqual(self.post("profile_address_import", too_many).status_code, 400)
        self.assertEqual(self.mine(), [])


class PagesTests(Base):
    def setUp(self):
        super().setUp()
        SavedAddress.objects.create(user=self.ana, address_line="12 Rizal St", city="Antipolo", postal_code="1870",
                                    phone="09171234567", is_default=True)

    def data_in(self, html):
        start = html.index('<script id="saved-addresses-data" type="application/json">') + len('<script id="saved-addresses-data" type="application/json">')
        return json.loads(html[start:html.index("</script>", start)])

    def test_the_profile_page_carries_the_accounts_addresses(self):
        html = self.client.get(reverse("gowns:profile")).content.decode()
        [entry] = self.data_in(html)
        self.assertEqual((entry["addressLine"], entry["isDefault"]), ("12 Rizal St", True))

    def test_the_checkout_page_carries_them_too(self):
        html = self.client.get(reverse("gowns:reservation")).content.decode()
        [entry] = self.data_in(html)
        self.assertEqual((entry["postalCode"], entry["phone"]), ("1870", "09171234567"))

    def test_another_phone_signed_into_the_same_account_sees_the_same_addresses(self):
        other_phone = Client()
        other_phone.force_login(self.ana)
        for page in ("gowns:profile", "gowns:reservation"):
            [entry] = self.data_in(other_phone.get(reverse(page)).content.decode())
            self.assertEqual(entry["addressLine"], "12 Rizal St", page)

    def test_someone_else_never_sees_them(self):
        stranger = Client()
        stranger.force_login(self.ben)
        for page in ("gowns:profile", "gowns:reservation"):
            self.assertEqual(self.data_in(stranger.get(reverse(page)).content.decode()), [], page)
        self.assertEqual(self.data_in(Client().get(reverse("gowns:reservation")).content.decode()), [])   # a visitor not signed in

    def test_an_address_with_html_in_it_cannot_run_as_a_script(self):
        SavedAddress.objects.create(user=self.ana, address_line="<script>alert(1)</script>", city="c", postal_code="1111", phone="09111111111")
        for page in ("gowns:profile", "gowns:reservation"):
            html = self.client.get(reverse(page)).content.decode()
            self.assertNotIn("<script>alert(1)</script>", html, page)
            self.assertIn("<script>alert(1)</script>", [a["addressLine"] for a in self.data_in(html)], page)   # still intact as data


class WiringTests(Base):
    def read(self, rel):
        return (Path(settings.BASE_DIR) / rel).read_text(encoding="utf-8")

    def test_the_profile_page_saves_through_the_server_and_no_longer_only_in_the_browser(self):
        source = self.read("templates/profile.htmls")
        for text in ("postJson(ADDRESS_SAVE_URL, payload)", "ADDRESS_DELETE_URL.replace(", "postJson(ADDRESS_IMPORT_URL", "function escapeHtml(",
                     "if (addressRequestPending) return;"):
            self.assertIn(text, source)
        self.assertNotIn("localStorage.setItem(LEGACY_ADDRESS_STORAGE_KEY", source)       # the browser copy is only ever read (and removed)
        self.assertNotIn("data-index", source)
        self.assertIn("{% url 'gowns:profile_address_save' %}", source)

    def test_the_old_addresses_are_copied_over_once_and_never_again(self):
        source = self.read("templates/profile.htmls")
        self.assertIn('localStorage.setItem(LEGACY_IMPORTED_FLAG, "1")', source)
        self.assertIn("if (alreadyTried) return;", source)
        self.assertIn("if (!result.data.skipped) localStorage.removeItem(LEGACY_ADDRESS_STORAGE_KEY);", source)

    def test_the_checkout_reads_the_account_first_and_the_browsers_old_copy_only_until_it_was_imported(self):
        source = self.read("templates/reservation.html")
        sent = source.index("document.getElementById('saved-addresses-data')")
        flag = source.index("localStorage.getItem(PROFILE_ADDRESS_IMPORTED_FLAG)")
        legacy = source.index("localStorage.getItem(PROFILE_ADDRESS_STORAGE_KEY)")
        self.assertLess(sent, flag)
        self.assertLess(flag, legacy)
