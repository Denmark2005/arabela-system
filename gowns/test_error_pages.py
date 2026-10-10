"""Branded error pages (templates/404.html, templates/500.html): a missing page keeps the site's menu and offers a way back; a crash
shows a calm page that needs nothing that could itself be broken."""
from django.contrib.auth import get_user_model
from django.template import loader
from django.test import TestCase, override_settings
from django.urls import path


def _crash(request):
    raise RuntimeError("deliberate test crash")


urlpatterns = [path("boom/", _crash)]


class NotFoundPageTests(TestCase):
    def test_a_missing_customer_page_is_a_branded_404_with_the_normal_menu(self):
        response = self.client.get("/this-page-does-not-exist/")
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "404.html")
        self.assertTemplateUsed(response, "base.html")
        self.assertContains(response, "Page not found.", status_code=404)
        self.assertContains(response, 'href="/collections/all/"', status_code=404)
        self.assertNotContains(response, "Back to the dashboard", status_code=404)

    def test_a_gown_that_no_longer_exists_gets_the_same_page(self):
        response = self.client.get("/collections/wedding/products/no-such-gown/")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Page not found.", status_code=404)

    def test_a_missing_admin_page_points_back_to_the_dashboard(self):
        response = self.client.get("/admin-panel/no-such-page/")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "This admin page does not exist.", status_code=404)
        self.assertContains(response, 'href="/admin-panel/"', status_code=404)

    def test_it_works_for_a_signed_in_customer_and_for_staff(self):
        users = get_user_model()
        customer = users.objects.create_user(username="err_customer", password="x" * 12)
        staff = users.objects.create_user(username="err_staff", password="x" * 12, is_staff=True)
        owner = users.objects.create_superuser(username="err_owner", password="x" * 12)
        for user in (customer, staff, owner):
            self.client.force_login(user)
            for url in ("/this-page-does-not-exist/", "/admin-panel/no-such-page/"):
                with self.subTest(user=user.username, url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 404)
                    self.assertContains(response, "Page not found.", status_code=404)


class ServerErrorPageTests(TestCase):
    def test_the_page_renders_with_no_context_at_all_just_as_django_renders_it(self):
        html = loader.get_template("500.html").render()
        self.assertIn("Something went wrong.", html)
        self.assertIn('href="/reservations/"', html)
        self.assertNotIn("{%", html)

    @override_settings(ROOT_URLCONF="gowns.test_error_pages")
    def test_a_crashing_page_shows_it_with_status_500(self):
        self.client.raise_request_exception = False
        with self.assertLogs("django.request", level="ERROR"):
            response = self.client.get("/boom/")
        self.assertEqual(response.status_code, 500)
        self.assertContains(response, "Something went wrong.", status_code=500)
