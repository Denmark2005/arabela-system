"""Size and fitting, as customers read it. A gown cannot be reserved without a size any more, so no page says "leave it as TBD". The Terms,
How It Works and the AI helper say instead: try the gown on at the shop, the shop adjusts one that is too loose or too
big, and that can be done up to one week before the pick-up date, depending on whether the gown is available that week."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts import terms
from ai_recommendation import views as ai


class SizeAndFittingTextTests(TestCase):
    def page(self, name):
        response = self.client.get(reverse(name))
        self.assertEqual(response.status_code, 200, name)
        return response.content.decode()

    def test_the_terms_page_says_how_and_when_a_gown_is_fitted_and_adjusted(self):
        html = self.page("gowns:terms_and_conditions")
        self.assertIn("Size and Fitting", html)
        self.assertIn("If you are not sure of your size, choose the size you think fits you best.", html)
        self.assertIn("Alterations: if the gown is too loose or too big for you, our staff can adjust it to fit your size.", html)
        self.assertIn("Fitting and alterations can be done at the shop, within shop hours, up to one (1) week before your pickup date.", html)
        self.assertIn("Whether it can be done depends on whether the gown is available during that week", html)
        self.assertIn("please check with our staff first", html)
        self.assertNotIn("not available on the event day", html)
        self.assertIn("Arabela Gown Rental is not liable for size issues if the correct size was not communicated prior to or during pickup.", html)

    def test_no_customer_page_offers_a_tbd_size_any_more(self):
        for name in ("gowns:terms_and_conditions", "gowns:how_it_works", "gowns:faqs"):
            html = self.page(name)
            self.assertNotIn("TBD", html, name)
            self.assertNotIn("To Be Determined", html, name)

    def test_the_terms_pop_up_shows_the_same_wording(self):
        user = get_user_model().objects.create_user(username="fit-customer", password="pw-12345-abc")
        self.client.force_login(user)
        html = self.page("gowns:selection")
        self.assertTrue(terms.needs_to_accept(user))
        self.assertIn("Alterations: if the gown is too loose or too big for you, our staff can adjust it to fit your size.", html)
        self.assertIn("up to one (1) week before your pickup date", html)
        self.assertNotIn("To Be Determined", html)

    def test_how_it_works_says_when_the_shop_adjusts_a_gown(self):
        html = self.page("gowns:how_it_works")
        self.assertIn("you can try the gown on at the shop", html)
        self.assertIn("adjust it to your size", html)
        self.assertIn("This can be done up to one week before your pick-up date, depending on whether the gown is available that week.", html)
        self.assertNotIn("not on the event day", html)

    def test_the_ai_helper_gives_the_same_rule(self):
        prompt = ai._system_prompt()
        self.assertNotIn("TBD", prompt)
        self.assertIn("the shop can adjust it to their size -- this can be done up to one week before the pick-up date, depending on whether the gown is available that week", prompt)
        self.assertNotIn("not on the event day", prompt)
