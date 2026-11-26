"""The admin panel's readable text: ONE dial (html[data-text-size]) that every admin page applies before it draws, the "Aa" switch in the header, and a floor under every
font size the pages write for themselves. Added because the panel's text was 11-14px and the readers it is shown to are over 45."""
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from accounts.models import UserProfile

User = get_user_model()
BASE = Path(settings.BASE_DIR)
ADMIN = BASE / "templates" / "arabela_admin"
CSS = BASE / "static" / "arabela_admin" / "style.css"
# not part of the dial: a customer's e-mail mock-up, the sign-in page, the printed receipt
NOT_THE_PANEL = {"email-preview.html", "admin-login.html", "receipt_print.html"}
FLOOR_REM = 0.8125                   # 13px, before the dial: nothing readable is written smaller


class EveryAdminPageTests(TestCase):
    PAGES = ("dashboard", "active_reservations", "new_booking", "gown_catalog", "reservation_records", "payments", "id_checks", "pending_approval", "payment_verification",
             "security_deposits", "clients", "rental_history", "gown_monitoring", "rental_schedule", "staff_management", "audit_log", "categories")

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("ts_owner", "ts.owner@gmail.com", "x")
        UserProfile.objects.update_or_create(user=cls.owner, defaults={"role": UserProfile.Role.OWNER})

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.owner)

    def urls(self):
        out = {name: reverse(f"arabela_admin:{name}") for name in self.PAGES}
        out["profile"] = reverse("arabela_admin:page", kwargs={"page": "profile"})
        return out

    def test_every_page_applies_the_saved_size_before_anything_is_drawn(self):
        for name, url in self.urls().items():
            with self.subTest(page=name):
                html = self.client.get(url).content.decode()
                boot = html.index("arabelaTextSize")                              # the one-line script after <body>
                self.assertLess(html.index("<body"), boot)
                self.assertLess(boot, html.index("<aside"), "the size must be set before the menu is drawn, or the page flashes at another size")
                self.assertIn("h.setAttribute('data-text-size',s)", html)
                self.assertIn("s='large'", html)                                   # Large is the default for everybody

    def test_every_page_has_the_switch_in_the_header(self):
        for name, url in self.urls().items():
            with self.subTest(page=name):
                html = self.client.get(url).content.decode()
                self.assertEqual(html.count('id="tsz-switch"'), 1)
                self.assertIn('aria-label="Text size"', html)
                for words in ("Normal", "Large", "Extra large", "Recommended", "Saved on this computer."):
                    self.assertIn(words, html)
                for call in ("arabelaSetTextSize('normal')", "arabelaSetTextSize('large')", "arabelaSetTextSize('xl')"):
                    self.assertIn(call, html)
                # it sits in the header, before the bell, so it is next to the dark-mode button
                self.assertLess(html.index('id="tsz-switch"'), html.index('id="arabela-notifier"'))

    def test_the_switch_works_without_the_page_framework_in_the_script_that_saves_the_choice(self):
        html = self.client.get(self.urls()["dashboard"]).content.decode()
        self.assertIn("localStorage.setItem('arabelaTextSize', size)", html)
        self.assertIn("if (size !== 'normal' && size !== 'large' && size !== 'xl') { size = 'large'; }", html)       # anything else means Large

    def test_the_sign_in_page_keeps_its_own_size(self):
        self.assertNotIn("arabelaTextSize", (ADMIN / "admin-login.html").read_text(encoding="utf-8"))


class TheDialTests(SimpleTestCase):
    css = CSS.read_text(encoding="utf-8")

    def test_the_three_sizes_exist_and_large_is_what_the_pages_default_to(self):
        for size, percent in (("normal", "100%"), ("large", "112.5%"), ("xl", "125%")):
            with self.subTest(size=size):
                self.assertRegex(self.css, r'html\[data-text-size="%s"\]\s*\{\s*font-size:\s*%s;' % (size, re.escape(percent)))

    def test_the_themes_px_sizes_follow_the_dial(self):
        block = self.css[self.css.index("html[data-text-size] {"):]
        block = block[:block.index("}")]
        for token in ("--text-theme-xs", "--text-theme-sm", "--text-theme-xl", "--text-title-sm", "--text-title-md", "--text-title-2xl", "--text-xs"):
            with self.subTest(token=token):
                self.assertRegex(block, token + r":\s*[\d.]+rem;")                  # rem, never px
        self.assertIn("--text-theme-xs: 0.8125rem;", block)                         # the smallest theme size is lifted to 13px

    def test_the_menu_is_never_narrower_than_its_old_width_and_grows_for_its_labels(self):
        for size, width in (("normal", "18.125rem"), ("large", "max(290px, 17rem)"), ("xl", "max(290px, 17.5rem)")):
            with self.subTest(size=size):
                self.assertRegex(self.css, r'html\[data-text-size="%s"\] \.sidebar:not\(\.lg\\:w-\\\[90px\\\]\)[^{]*\{ width: %s; \}' % (size, re.escape(width)))

    def test_the_bigger_sizes_ease_back_on_narrower_screens(self):
        # (the pages' wide table layouts were drawn for 16px text; every admin page was measured at 1024-1920px wide before these steps were chosen)
        steps = {1599: ('html[data-text-size="xl"]', "118.75%"), 1439: ('html[data-text-size="xl"]', "112.5%"),
                 1365: ('html[data-text-size="large"], html[data-text-size="xl"]', "106.25%"), 1279: ('html[data-text-size="large"], html[data-text-size="xl"]', "100%"),
                 699: ('html[data-text-size="large"], html[data-text-size="xl"]', "106.25%")}
        for width, (selector, percent) in steps.items():
            with self.subTest(width=width):
                self.assertIn("@media (max-width: %dpx) { %s { font-size: %s; } }" % (width, selector, percent), self.css)
        # narrower screens never get a BIGGER size than wider ones: the rules come after the three sizes themselves
        self.assertLess(self.css.index('html[data-text-size="xl"] { font-size: 125%; }'), self.css.index("@media (max-width: 1599px)"))

    def test_the_size_is_not_pushed_on_pages_that_do_not_carry_the_attribute(self):
        # every rule of the block is scoped by the attribute, so the sign-in pages, printed receipts and the customer website keep their size
        block = self.css[self.css.index("Readable admin text"):]
        for selector in re.findall(r"^([^\s/@}{][^{]*)\{", block, re.M):
            with self.subTest(selector=selector.strip()):
                self.assertIn("data-text-size", selector)


class NothingIsWrittenTooSmallTests(SimpleTestCase):
    """The panel's own pages write their font sizes themselves (hundreds of rules). Written in px they would not follow the dial, and below 13px they are hard to read."""

    def files(self):
        names = sorted(ADMIN.glob("*.html")) + sorted((ADMIN / "partials").glob("*.html"))
        return [p for p in names if p.name not in NOT_THE_PANEL]

    def test_every_font_size_follows_the_dial_and_none_is_below_the_floor(self):
        declared = 0
        for path in self.files():
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"font-size:\s*([\d.]+)(px|rem|em|%)", text):
                declared += 1
                value, unit = float(match.group(1)), match.group(2)
                with self.subTest(file=path.name, rule=match.group(0)):
                    self.assertNotEqual(unit, "px", "px does not follow the text-size dial: write it in rem")
                    if unit == "rem":
                        self.assertGreaterEqual(value, FLOOR_REM, "nothing readable is smaller than 13px (0.8125rem)")
        self.assertGreater(declared, 300)                                           # the scan really read the pages

    def test_line_heights_follow_the_dial_too(self):
        for path in self.files():
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertNotRegex(text, r"line-height:\s*[\d.]+px")

    def test_the_scripts_that_draw_parts_of_the_page_follow_the_dial(self):
        for name in ("global-search.js", "pager.js", "notifications-live.js", "live-regions.js"):
            text = (BASE / "static" / "arabela_admin" / name).read_text(encoding="utf-8")
            with self.subTest(script=name):
                self.assertNotRegex(text, r"font-size:\s*[\d.]+px")
