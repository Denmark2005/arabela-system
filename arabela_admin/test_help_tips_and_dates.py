"""Two things every admin page now shares (both live in partials pulled in by the header partial, partials/notifications.html):

  * the small round "?" (partials/help_tip_styles.html): long explanation text moved behind it, on every page that had some, so a page no longer prints a paragraph of
    tutorial under a title. Looks and works like New Booking's "?".
  * the date box's calendar icon (partials/date_picker.html): every admin date box shows an icon, a click on the icon opens the calendar, a click on the date itself
    leaves it free for typing. The Check boxes also stop past dates and absurd years.

What is checked: the shared pieces are on every page exactly once, every "?" is built the same way and never sits inside a button or a label, each long sentence now
lives only inside a "?" (and is still there, not lost), the date box rules, and the Check box's limits. Real clicks (a bubble opens, stays on screen, closes; the icon
opens the calendar) are in the browser check."""
import re
from html.parser import HTMLParser
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from arabela_admin.test_active_dropdown import ActiveDropdownBase, P

User = get_user_model()
ADMIN = Path(settings.BASE_DIR) / "templates" / "arabela_admin"


def without_tips(html):
    return re.sub(r'<div class="ht-tip-pop">.*?</div>', "", html, flags=re.S)


def tips_text(html):
    return " ".join(re.findall(r'<div class="ht-tip-pop">(.*?)</div>', html, flags=re.S))


class _Tips(HTMLParser):
    """Every "?" is <details class="ht-tip"><summary aria-label="About ..." title="About ...">?</summary><div class="ht-tip-pop">text</div></details>, and none sits
    inside a <button> or a <label>."""

    def __init__(self):
        super().__init__()
        self.stack, self.errors, self.tips = [], [], 0
        self._in_tip = False
        self._in_summary = False
        self._summary = {}
        self._pop = None
        self._text = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("button", "label"):
            self.stack.append(tag)
        if tag == "details" and "ht-tip" in (a.get("class") or "").split():
            self.tips += 1
            self._in_tip = True
            if self.stack:
                self.errors.append(f"a ? inside <{self.stack[-1]}> near line {self.getpos()[0]}")
        if tag == "summary" and self._in_tip:
            label, title = a.get("aria-label") or "", a.get("title") or ""
            if not label.startswith("About ") or label != title:
                self.errors.append(f"summary label/title wrong near line {self.getpos()[0]}: {label!r} / {title!r}")
            self._in_summary = True
            self._text = ""
        if tag == "div" and self._in_tip and (a.get("class") or "") == "ht-tip-pop":
            self._pop = ""

    def handle_data(self, data):
        if self._in_summary:
            self._text += data
        if self._pop is not None:
            self._pop += data

    def handle_endtag(self, tag):
        if tag == "summary" and self._in_summary:
            if self._text.strip() != "?":
                self.errors.append(f"summary text is not '?' near line {self.getpos()[0]}")
            self._in_summary = False
        if tag == "div" and self._pop is not None:
            if len(self._pop.strip()) < 15:
                self.errors.append(f"an empty bubble near line {self.getpos()[0]}")
            self._pop = None
        if tag == "details":
            self._in_tip = False
        if tag in self.stack:
            while self.stack and self.stack[-1] != tag:
                self.stack.pop()
            self.stack.pop()


class EveryTipIsBuiltTheSameWayTests(SimpleTestCase):
    def test_every_question_mark_in_every_admin_template(self):
        total = 0
        for path in sorted(list(ADMIN.glob("*.html")) + list((ADMIN / "partials").glob("*.html"))):
            parser = _Tips()
            parser.feed(path.read_text(encoding="utf8"))
            with self.subTest(template=path.name):
                self.assertEqual(parser.errors, [])
            total += parser.tips
        self.assertGreaterEqual(total, 27)                                                   # the catalog alone has 12

    def test_the_gown_catalog_the_calendar_and_the_profile_pages_have_their_share(self):
        counts = {}
        for name in ("gown-catalog.html", "calendar.html", "profile.html", "account-settings.html", "email-preview.html", "payment-verification.html", "categories.html",
                     "staff-management.html", "reservation-records.html"):
            counts[name] = len(re.findall(r'<details class="ht-tip"', (ADMIN / name).read_text(encoding="utf8")))
        self.assertGreaterEqual(counts["gown-catalog.html"], 12)
        self.assertEqual({k: v for k, v in counts.items() if k != "gown-catalog.html"}, {
            "calendar.html": 2, "profile.html": 3, "account-settings.html": 2, "email-preview.html": 3, "payment-verification.html": 1, "categories.html": 1,
            "staff-management.html": 1, "reservation-records.html": 1})


class SharedPiecesAreOnEveryPageTests(SimpleTestCase):
    def test_the_header_partial_pulls_in_both_once(self):
        header = (ADMIN / "partials" / "notifications.html").read_text(encoding="utf8")
        self.assertEqual(header.count('{% include "arabela_admin/partials/help_tip_styles.html" %}'), 1)
        self.assertEqual(header.count('{% include "arabela_admin/partials/date_picker.html" %}'), 1)

    def test_every_admin_page_with_the_header_gets_them_without_including_them_itself(self):
        for path in sorted(ADMIN.glob("*.html")):
            text = path.read_text(encoding="utf8")
            with self.subTest(template=path.name):
                if 'partials/notifications.html' in text:
                    self.assertNotIn("partials/help_tip_styles.html", text)
                    self.assertNotIn("partials/date_picker.html", text)

    def test_the_customer_emails_page_has_no_header_so_it_includes_the_bubble_itself(self):
        text = (ADMIN / "email-preview.html").read_text(encoding="utf8")
        self.assertNotIn("partials/notifications.html", text)
        self.assertEqual(text.count('{% include "arabela_admin/partials/help_tip_styles.html" %}'), 1)

    def test_the_bubble_script_runs_once_and_closes_on_outside_click_and_escape(self):
        text = (ADMIN / "partials" / "help_tip_styles.html").read_text(encoding="utf8")
        self.assertIn("if (window.__arabelaHelpTips) { return; }", text)
        self.assertIn("details.ht-tip[open]", text)
        self.assertIn("if (!tip.contains(event.target)) { tip.removeAttribute('open'); }", text)
        self.assertIn("event.stopPropagation();", text)                                      # Escape closes only the "?", not the window behind it
        self.assertIn("}, true);", text)                                                      # in the capture phase, so it runs before the window's own Escape
        self.assertIn("pop.style.transform = 'translateX('", text)                            # slid back in when it would leave the screen
        self.assertIn("pop.style.bottom = 'calc(100% + .4rem)';", text)                       # opened above when there is no room below
        self.assertNotIn("Alpine", text[text.index("<script>"):])                              # plain script: it works on pages that are not Alpine pages

    def test_the_bubble_looks_like_new_bookings(self):
        text = (ADMIN / "partials" / "help_tip_styles.html").read_text(encoding="utf8")
        booking = (ADMIN / "new-booking.html").read_text(encoding="utf8")
        for rule in (".nb-tip > summary { list-style: none; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; width: 1.1rem; height: 1.1rem;",
                     "border-radius: 999px; color: #667085; font-size: .68rem; font-weight: 700; line-height: 1;"):
            self.assertIn(rule, booking)
        for rule in (".ht-tip > summary { list-style: none; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; width: 1.1rem; height: 1.1rem;",
                     "border-radius: 999px; color: #667085; font-size: .68rem; font-weight: 700; line-height: 1;", "background: #101828; color: #f2f4f7;", ".dark .ht-tip-pop { background: #1d2939;"):
            self.assertIn(rule, text)
        self.assertIn(".nb-tip", booking)                                                      # New Booking's own "?" is left as it was


class DateBoxTests(SimpleTestCase):
    css = (ADMIN / "partials" / "date_picker.html").read_text(encoding="utf8")

    def test_every_date_box_gets_the_icon_in_both_themes(self):
        self.assertIn('input[type="date"] {', self.css)
        self.assertIn("background-image: url(\"data:image/svg+xml,", self.css)
        self.assertIn("%23667085", self.css)                                                   # the light icon
        self.assertIn(".dark input[type=\"date\"] {", self.css)
        self.assertIn("%2398a2b3", self.css)                                                   # the lighter icon on a dark box
        self.assertIn("color-scheme: dark;", self.css)
        for rule in ("background-repeat: no-repeat !important;", "background-position: right .8rem center !important;", "padding-right: 2.6rem !important;"):
            self.assertIn(rule, self.css)

    def test_only_the_icon_opens_the_calendar_so_a_date_can_still_be_typed(self):
        self.assertIn("var ICON_ZONE = 46;", self.css)
        self.assertIn("if (box.getBoundingClientRect().right - event.clientX > ICON_ZONE) { return; }", self.css)
        self.assertIn("try { box.showPicker(); } catch (ignore)", self.css)                    # an old browser must never get an error
        self.assertIn("box.disabled || box.readOnly", self.css)
        self.assertIn("if (window.__arabelaDatePicker) { return; }", self.css)

    def test_no_page_has_its_own_per_box_calendar_script_any_more(self):
        for path in sorted(list(ADMIN.glob("*.html")) + list((ADMIN / "partials").glob("*.html"))):
            if path.name == "date_picker.html":
                continue
            with self.subTest(template=path.name):
                self.assertNotIn("showPicker", path.read_text(encoding="utf8"))

    def test_no_date_box_is_read_only(self):
        for path in sorted(ADMIN.glob("*.html")):
            for tag in re.findall(r'<input[^>]*type="date"[^>]*>', path.read_text(encoding="utf8"), flags=re.S):
                with self.subTest(template=path.name):
                    self.assertNotIn("readonly", tag)

    def test_the_check_boxes_stop_past_dates_and_absurd_years(self):
        text = (ADMIN / "partials" / "availability_check.html").read_text(encoding="utf8")
        for needle in ("function limit(input) {", "input.max = isoOf(new Date(now.getFullYear() + 3, now.getMonth(), now.getDate()));", "function problem(input) {",
                       "That date has already passed. Choose today or a later date.", "That date is too far ahead. Choose a date within the next 3 years.",
                       "limit(dateEl);", "window.arabelaAvailability = { check: check, render: render, inline: inline, clear: clear, limit: limit };"):
            with self.subTest(needle=needle[:50]):
                self.assertIn(needle, text)
        self.assertEqual(text.count("var wrong = problem("), 2)                                 # asked in both places: the window and the gown window's box

    def test_the_check_a_date_window_keeps_its_title_name_and_has_a_question_mark(self):
        text = (ADMIN / "partials" / "availability_check.html").read_text(encoding="utf8")
        self.assertIn('<h2 class="ca-title"><span id="ca-title">Check a date</span><details class="ht-tip">', text)
        self.assertIn('aria-labelledby="ca-title"', text)
        self.assertNotIn("ca-lead", text)
        self.assertIn("Nothing is saved. Use it for a Facebook or phone question too.", tips_text(text))


class LongTextIsOnlyInsideAQuestionMarkTests(ActiveDropdownBase):
    """Each long sentence that used to be printed under a title is now only inside a "?" -- and is still there."""

    def get(self, name, **kwargs):
        response = self.client.get(reverse(name, **kwargs) if "." not in name else name)
        self.assertEqual(response.status_code, 200, name)
        return response.content.decode().replace("\r\n", "\n")

    def check(self, html, sentences):
        shown = without_tips(html)
        inside = tips_text(html)
        for sentence in sentences:
            with self.subTest(sentence=sentence[:50]):
                self.assertNotIn(sentence, shown)
                self.assertIn(sentence, inside)

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_superuser("tips_owner", password="x", email="tips@example.com")
        self.client.force_login(self.owner)

    def test_payment_verification(self):
        self.book("Verified Today", "09170000001", [(3, 6, P, {})])
        self.check(self.get("arabela_admin:payment_verification"), ["Double-check these are correctly scheduled before you move on."])

    def test_the_colour_key_notes_are_gone_from_active_reservations_and_reservation_records(self):
        self.book("Key", "09170000002", [(3, 6, P, {})])
        for name, gone in (("arabela_admin:active_reservations", "The small circle before a customer's name"), ("arabela_admin:reservation_records", "The circle before a customer's name")):
            with self.subTest(page=name):
                html = self.get(name)
                self.assertNotIn(gone, html)
                self.assertNotIn("ar-leg-note\">", html)
        records = self.get("arabela_admin:reservation_records")
        self.assertIn("What the circles mean", records)                                        # the key itself stays
        self.check(records, ["PNG, JPG or WebP. Large phone photos are resized automatically."])
        self.assertNotIn("No manual receipt attached yet", records)
        self.assertIn("No receipt attached yet.", records)

    def test_the_rental_schedule(self):
        html = self.get("arabela_admin:calendar")
        self.check(html, ["Blocked days are gowns held back for cleaning or repair, not customer bookings", "This calendar is view-only. To change a date or mark a pick-up or return, use Active Reservations."])
        self.assertIn(reverse("arabela_admin:gown_catalog"), tips_text(html))                   # the link to Gown Catalog is kept inside the bubble

    def test_categories_staff_management_and_the_two_profile_pages(self):
        self.check(self.get("arabela_admin:categories"), ["One tall photo of the whole gown on a plain white background."])
        self.check(self.get("arabela_admin:staff_management"), ["Staff do the daily work. A Manager can also delete gowns, cancel bookings, flag customers, and delete receipt photos."])
        staff = self.get("arabela_admin:staff_management")
        self.assertIn("Leave the password blank to keep it unchanged.", staff)
        self.assertNotIn("Update the account details.", staff)
        self.check(self.client.get("/admin-panel/profile.html").content.decode(), [
            "Shown to customers when they pay the security deposit. The QR is always shown whole, never cropped.",
            "Customers can be emailed when a reservation is received, approved or rejected", "This is ARABELA's public Facebook page, shown in the site footer and Contact page."])
        self.check(self.client.get("/admin-panel/account-settings.html").content.decode(), [
            "If you forget your password, the sign-in page can email you a reset link here.", "Every other device you are signed in on will be signed out."])

    def test_the_customer_emails_page(self):
        html = self.get("arabela_admin:email_preview")
        self.check(html, ["Every email uses sample details (not a real customer).", "Use this to check that an email really reaches a customer's Gmail"])
        self.assertIn('<script>try{if(JSON.parse(localStorage.getItem(\'darkMode\'))===true)', html)
        self.assertIn(".ht-tip-pop {", html)                                                  # the bubble's styles are on this page too

    def test_the_notifications_window_says_it_in_one_short_line(self):
        html = render_to_string("arabela_admin/partials/notif_modal_body.html", {"admin_notification_count": 3})
        self.assertIn("Most urgent first. Click one to open the page where you act on it.", html)
        self.assertNotIn("Everything waiting on you right now", html)

    def test_the_gown_catalogs_sentences(self):
        self.check(self.get("arabela_admin:gown_catalog"), [
            "Every category has one tag color. Put a tag or ribbon in that color", "A new kind of gown or outfit, like", "The original ones can be brought back by adding a category with the same name.",
            "Damaged or otherwise unavailable? Use", "Days this gown can't go out even though nobody booked it", "Name a gown by its color. It fills in from the Color you pick below"])


class EveryPageStillRendersTests(ActiveDropdownBase):
    def test_each_admin_page_has_the_shared_pieces_exactly_once(self):
        self.book("Pieces", "09170000010", [(3, 6, P, {})])
        owner = User.objects.create_superuser("pieces_owner", password="x", email="pieces@example.com")
        self.client.force_login(owner)
        pages = [reverse(f"arabela_admin:{n}") for n in ("dashboard", "calendar", "gown_catalog", "active_reservations", "pending_approval", "rental_history", "reservation_records",
                                                            "new_booking", "payment_verification", "security_deposits", "clients", "categories", "staff_management")]
        pages += ["/admin-panel/profile.html", "/admin-panel/account-settings.html"]
        for url in pages:
            with self.subTest(page=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertEqual(html.count("window.__arabelaHelpTips = true;"), 1)
                self.assertEqual(html.count("window.__arabelaDatePicker = true;"), 1)
                self.assertEqual(len(re.findall(r'(?m)^\s*input\[type="date"\] \{', html)), 1)
                self.assertEqual(len(re.findall(r'(?m)^\s*\.ht-tip-pop \{', html)), 1)
