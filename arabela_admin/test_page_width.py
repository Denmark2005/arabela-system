"""Every admin page is the same width: the page box (the area next to the menu) uses .adm-page, ONE rule in static/arabela_admin/style.css that fills the width
it is given up to 2560px. A page that brings back its own width (the theme's 1536px max-w-(--breakpoint-2xl), the old 2000px .ar-wide) fails here, so the
pages can never drift apart again."""
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounts.models import UserProfile

BASE = Path(settings.BASE_DIR)
ADMIN = BASE / "templates" / "arabela_admin"
CSS = BASE / "static" / "arabela_admin" / "style.css"
# The only admin templates without a page box: the sign-in page, the e-mail preview and the printed receipt (each has its own size).
NO_PAGE_BOX = {"admin-login.html", "email-preview.html", "receipt_print.html"}
BOX = re.compile(r'<div class="([^"]*\badm-page\b[^"]*)">')


class PageWidthTests(SimpleTestCase):
    def pages(self):
        return sorted(p for p in ADMIN.glob("*.html") if p.name not in NO_PAGE_BOX)

    def test_every_admin_page_uses_the_one_page_width(self):
        pages = self.pages()
        self.assertGreaterEqual(len(pages), 18)
        for page in pages:
            with self.subTest(page=page.name):
                html = page.read_text(encoding="utf-8")
                boxes = BOX.findall(html)
                self.assertEqual(len(boxes), 1, "one page box per page")
                self.assertEqual(set(boxes[0].split()) - {"adm-page"}, {"mx-auto", "p-4", "md:p-6"})        # centred, with the same padding everywhere
                self.assertNotIn("max-w-(--breakpoint-2xl)", html)
                self.assertNotIn("ar-wide", html)

    def test_the_width_is_set_once_in_the_admin_stylesheet(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertEqual(css.count(".adm-page {"), 1)
        self.assertIn(".adm-page { max-width: 2560px; }", css)

    def test_pages_without_a_page_box_keep_their_own_size(self):
        for name in NO_PAGE_BOX:
            with self.subTest(page=name):
                self.assertNotIn("adm-page", (ADMIN / name).read_text(encoding="utf-8"))


class RenderedPageWidthTests(TestCase):
    def test_the_pages_staff_open_most_carry_the_page_width(self):
        owner = get_user_model().objects.create_superuser("pw_owner", "pw.owner@gmail.com", "x")
        UserProfile.objects.update_or_create(user=owner, defaults={"role": UserProfile.Role.OWNER})
        self.client.force_login(owner)
        for name in ("arabela_admin:active_reservations", "arabela_admin:payments", "arabela_admin:gown_catalog"):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertIn('class="mx-auto adm-page p-4 md:p-6"', response.content.decode())
