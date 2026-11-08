"""The shop's public phone number, Facebook link and address are the OWNER's to change -- nobody else.

`save_profile_view` has always thrown those fields away for a non-owner, but the profile page still showed every admin an editable "Phone"
box (it is the SHOP's number, the one customers see in the footer and on the Contact page, not the staff member's own phone), so a staff
member could type in it, press Save, and see it "work" on their screen. The page now hides the shop phone / link / address from everyone but
the owner, and decides that with the very same check the server uses (`_is_owner`)."""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import UserProfile
from gowns.models import SiteSettings

User = get_user_model()
PROFILE_PAGE = reverse("arabela_admin:page", kwargs={"page": "profile"})
SAVE = reverse("arabela_admin:save_profile")

SHOP = dict(phone="0917 123 4567", facebook_url="https://www.facebook.com/arabela", shop_street="1 Gown Street", shop_city="Cebu City",
            shop_country="Philippines", shop_postal_code="6000")
TRY_TO_CHANGE = dict(phone="0999 000 0000", facebook="https://www.facebook.com/not-the-shop", street="99 Other Road", cityState="Elsewhere",
                     country="Nowhere", postalCode="1234")


def make_admin(username, role=None, **flags):
    user = User.objects.create_user(username, f"{username}@example.com", "Pass-12345!", is_staff=True, **flags)
    if role is not None:
        UserProfile.objects.update_or_create(user=user, defaults={"role": role})
    return user


class ShopSettingsAreOwnerOnlyBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser("so_super", "so_super@example.com", "Pass-12345!")
        cls.owner_role = make_admin("so_owner_role", UserProfile.Role.OWNER)
        cls.manager = make_admin("so_manager", UserProfile.Role.MANAGER)
        cls.staff = make_admin("so_staff", UserProfile.Role.STAFF)
        cls.no_profile = make_admin("so_noprofile")                                   # an admin account that has no profile row at all
        shop = SiteSettings.load()
        for name, value in SHOP.items():
            setattr(shop, name, value)
        shop.save()

    def shop(self):
        shop = SiteSettings.load()
        return {name: getattr(shop, name) for name in SHOP}

    def save(self, user, **fields):
        self.client.force_login(user)
        return self.client.post(SAVE, data=json.dumps(fields), content_type="application/json")

    def page(self, user):
        self.client.force_login(user)
        response = self.client.get(PROFILE_PAGE)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class ServerRefusesEveryoneButTheOwnerTests(ShopSettingsAreOwnerOnlyBase):
    def test_staff_manager_and_an_admin_without_a_profile_cannot_change_any_of_it(self):
        for user in (self.staff, self.manager, self.no_profile):
            with self.subTest(user=user.username):
                response = self.save(user, firstName="Mine", **TRY_TO_CHANGE)
                self.assertEqual(response.status_code, 200)                           # their own details still save...
                user.refresh_from_db()
                self.assertEqual(user.first_name, "Mine")
                self.assertEqual(self.shop(), SHOP)                                   # ...the shop's phone, link and address do not

    def test_a_phone_alone_from_a_staff_account_changes_nothing(self):
        self.assertEqual(self.save(self.staff, phone="0999 000 0000").status_code, 200)
        self.assertEqual(SiteSettings.load().phone, SHOP["phone"])

    def test_a_customer_or_a_signed_out_visitor_cannot_save_at_all(self):
        customer = User.objects.create_user("so_customer", "so_customer@example.com", "Pass-12345!")
        self.client.force_login(customer)
        self.assertEqual(self.client.post(SAVE, data=json.dumps(TRY_TO_CHANGE), content_type="application/json").status_code, 401)
        self.client.logout()
        self.assertEqual(self.client.post(SAVE, data=json.dumps(TRY_TO_CHANGE), content_type="application/json").status_code, 401)
        self.assertEqual(self.shop(), SHOP)

    def test_the_owner_still_can(self):
        for user in (self.superuser, self.owner_role):
            with self.subTest(user=user.username):
                response = self.save(user, phone="0999 000 0000", facebook="https://www.facebook.com/new-page", street="99 Other Road")
                self.assertEqual(response.status_code, 200, response.content)
                shop = SiteSettings.load()
                self.assertEqual((shop.phone, shop.facebook_url, shop.shop_street), ("0999 000 0000", "https://www.facebook.com/new-page", "99 Other Road"))
                for name, value in SHOP.items():
                    setattr(shop, name, value)                                        # put it back for the next account
                shop.save()


class ProfilePageShowsShopSettingsOnlyToTheOwnerTests(ShopSettingsAreOwnerOnlyBase):
    OWN_DETAILS = ('x-model="firstName"', 'x-model="lastName"', 'x-model="email"', 'x-model="bio"')

    def test_staff_and_manager_see_their_own_details_but_no_shop_phone_link_or_address(self):
        for user in (self.staff, self.manager):
            with self.subTest(user=user.username):
                html = self.page(user)
                for own in self.OWN_DETAILS:
                    self.assertIn(own, html)                                          # name / email / bio can still be edited
                self.assertNotIn('x-model="phone"', html)                             # the Phone box in Edit Personal Information
                self.assertNotIn('x-text="phone"', html)                              # the Phone line on the profile card
                self.assertNotIn('x-model="facebook"', html)                          # the Facebook link box
                self.assertNotIn('x-show="isProfileAddressModal"', html)              # the shop address dialog
                self.assertNotIn("Edit Social Links", html)

    def test_what_staff_press_save_on_never_even_carries_the_shop_phone_or_link(self):
        body = self.page(self.staff)
        body = body[body.index("saveProfileInfo() {"):]
        body = body[:body.index("})\n        })")]
        self.assertIn("firstName: this.firstName", body)
        self.assertIn("bio: this.bio", body)
        self.assertNotIn("phone", body)
        self.assertNotIn("facebook", body)

    def test_the_email_box_takes_the_whole_row_when_the_phone_box_is_gone(self):
        html = self.page(self.staff)
        email = html[:html.index('x-model="email"')]
        self.assertIn('<div class="col-span-2">', email[email.rindex("<div"):])
        self.assertNotIn("lg:col-span-1", email[email.rindex("<div"):email.rindex("<input")])

    def test_the_owner_sees_and_can_save_all_of_it(self):
        for user in (self.superuser, self.owner_role):
            with self.subTest(user=user.username):
                html = self.page(user)
                for own in self.OWN_DETAILS + ('x-model="phone"', 'x-text="phone"', 'x-model="facebook"', 'x-show="isProfileAddressModal"'):
                    self.assertIn(own, html)
                body = html[html.index("saveProfileInfo() {"):]
                body = body[:body.index("})\n        })")]
                self.assertIn("phone: this.phone", body)
                self.assertIn("facebook: this.facebook", body)
                email = html[:html.index('x-model="email"')]
                self.assertIn('<div class="col-span-2 lg:col-span-1">', email[email.rindex("<div"):])

    def test_the_shop_number_is_not_typeable_by_staff_even_in_the_page_source(self):
        """The number is public (footer, Contact page), so it may sit in the page's starting data; what matters is that no box can change it."""
        html = self.page(self.staff)
        self.assertNotIn('x-model="phone"', html)
        self.assertNotIn("this.phone", html[html.index("saveProfileInfo() {"):html.index("saveProfileAddress() {")])
