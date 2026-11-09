"""The redesigned New Booking page: a compact layout, a gown TABLE where one row at a time opens into its editor, a gown dropdown that lists every
gown (so staff who forgot the name can browse) with its own search and category filter, the long explanations behind small "?" buttons,
icon buttons, and the website account kept optional behind an icon.

The booking rules themselves are untouched and keep their own tests (test_new_booking.py). What is checked here: the server side of the gown
dropdown (browse / category / paging), and that the page's markup keeps its promises."""
import json
import re
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from arabela_admin.test_new_booking import Base, jpeg, make_gown
from gowns.models import Gown
from reservations.models import Reservation

SEARCH = reverse("arabela_admin:booking_gown_search")


def gown(n, category, name, status=Gown.Status.AVAILABLE):
    return Gown.objects.create(gown_id=f"{category}-T{n:03d}", name=name, category=category, color_name="Red", color_code="RD",
                               size=Gown.Size.MEDIUM, rental_price=Decimal("1500"), status=status)


class GownDropdownServerTests(Base):
    """GET /api/bookings/gowns/: with text it searches (as before); with NO text it browses every gown, paged, optionally in one category."""

    def setUp(self):
        super().setUp()
        self.categories = list(Gown.Category.values)[:3]
        self.made = []
        for n in range(1, 41):
            self.made.append(gown(n, self.categories[n % 3], f"Dropdown Gown {n:02d}"))

    def get(self, **params):
        response = self.client.get(SEARCH, params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_no_text_browses_every_gown_and_reports_whether_there_is_more(self):
        first = self.get(limit=15)
        self.assertEqual(len(first["results"]), 15)
        self.assertTrue(first["hasMore"])
        seen = []
        offset = 0
        while True:
            page = self.get(offset=offset, limit=15)
            seen += [g["id"] for g in page["results"]]
            offset += len(page["results"])
            if not page["hasMore"]:
                break
        self.assertEqual(len(seen), 40)
        self.assertEqual(len(set(seen)), 40)                                              # no gown twice, none missed
        self.assertEqual(set(seen), {g.id for g in self.made})

    def test_browsing_is_grouped_by_category_then_name(self):
        results = self.get(limit=50)["results"]
        keys = [(g["category"], g["name"], g["gownId"]) for g in results]
        self.assertEqual(keys, sorted(keys))

    def test_the_default_is_still_15_like_the_pop_up_that_relies_on_it(self):
        data = self.get(q="Dropdown")
        self.assertEqual(len(data["results"]), 15)
        self.assertTrue(data["hasMore"])

    def test_one_request_can_never_ask_for_more_than_50(self):
        self.assertEqual(len(self.get(limit=500)["results"]), 40)
        for n in range(41, 61):
            gown(n, self.categories[0], f"Dropdown Gown {n}")
        self.assertEqual(len(self.get(limit=500)["results"]), 50)

    def test_nonsense_numbers_fall_back_instead_of_failing(self):
        self.assertEqual(len(self.get(offset="abc", limit="x")["results"]), 15)
        self.assertEqual(self.get(offset=-5, limit=3)["results"], self.get(offset=0, limit=3)["results"])
        self.assertEqual(len(self.get(limit=0)["results"]), 1)

    def test_an_offset_past_the_end_is_an_empty_list_not_an_error(self):
        data = self.get(offset=10000)
        self.assertEqual((data["results"], data["hasMore"]), ([], False))

    def test_a_category_narrows_it_and_an_unknown_one_finds_nothing(self):
        wanted = self.categories[1]
        data = self.get(category=wanted, limit=50)
        self.assertTrue(data["results"])
        self.assertEqual({g["category"] for g in data["results"]}, {wanted})
        self.assertEqual(len(data["results"]), Gown.objects.filter(category=wanted).count())
        self.assertEqual(self.get(category="No Such Category")["results"], [])

    def test_text_and_category_work_together(self):
        wanted = self.categories[0]
        data = self.get(q="Gown 1", category=wanted, limit=50)
        self.assertTrue(data["results"])
        self.assertTrue(all(g["category"] == wanted and "Gown 1" in g["name"] for g in data["results"]))

    def test_searching_by_text_still_finds_by_id_tag_number_name_and_puts_available_first(self):
        gown(99, self.categories[0], "Zed Special", status=Gown.Status.OUT_OF_STOCK)
        gown(98, self.categories[0], "Zed Special Two")
        names = [g["name"] for g in self.get(q="Zed")["results"]]
        self.assertEqual(names, ["Zed Special Two", "Zed Special"])                       # Out-of-Stock goes below the ones that can be booked
        self.assertEqual([g["name"] for g in self.get(q="T098")["results"]], ["Zed Special Two"])
        self.assertEqual([g["name"] for g in self.get(q="99")["results"]], ["Zed Special"])

    def test_out_of_stock_gowns_are_listed_but_marked(self):
        stuck = gown(77, self.categories[2], "Not Bookable", status=Gown.Status.OUT_OF_STOCK)
        card = next(g for g in self.get(q="Not Bookable")["results"] if g["id"] == stuck.id)
        self.assertTrue(card["outOfStock"])

    def test_the_query_count_does_not_grow_with_the_number_of_gowns(self):
        self.get(limit=1)                                                                 # the first request also does one-time setup (the shop settings row)
        with CaptureQueriesContext(connection) as few:
            self.get(limit=3)
        with CaptureQueriesContext(connection) as many:
            self.get(limit=40)
        self.assertEqual(len(few), len(many))

    def test_only_signed_in_staff_may_browse(self):
        self.client.logout()
        self.assertEqual(self.client.get(SEARCH).status_code, 401)
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(SEARCH).status_code, 401)


class PageContextTests(Base):
    def html(self, user=None):
        if user:
            self.client.force_login(user)
        response = self.client.get(reverse("arabela_admin:new_booking"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_category_filter_lists_only_categories_that_really_have_gowns(self):
        gown(1, Gown.Category.WEDDING_GOWN, "Has A Gown")
        gown(2, Gown.Category.GUEST_GOWN, "Has A Gown Too")
        html = self.html()
        listed = json.loads(re.search(r'<script id="nb-categories" type="application/json">(.*?)</script>', html, re.S).group(1))
        self.assertEqual(listed, sorted({Gown.Category.WEDDING_GOWN, Gown.Category.GUEST_GOWN}))

    def test_the_page_still_renders_with_no_gowns_at_all(self):
        self.assertIn('id="nb-categories"', self.html())


class BookingLogicIsUntouchedTests(Base):
    """The form no longer sends a notebook number, and a website account is optional: the booking must save exactly as before."""

    def post_without(self, *dropped, lines=None, account=None):
        data = {"channel": "Walk-in", "name": "Redesign Walk-in", "phone": "09171234567", "address": "Antipolo", "payment": "full", "note": "",
                "notebook_number": "", "account": account.id if account else "", "lines": json.dumps(lines), "receipt_1": jpeg("r.jpg", b"redesign")}
        for key in dropped:
            data.pop(key)
        return self.client.post(reverse("arabela_admin:booking_create"), data=data)

    def test_a_booking_saves_with_no_notebook_and_no_account_fields_at_all(self):
        g = make_gown(501)
        response = self.post_without("notebook_number", "account", lines=[self.line(g)])
        self.assertEqual(response.status_code, 200, response.content)
        reservation = Reservation.objects.get(reference_code=response.json()["reference_code"])
        self.assertEqual((reservation.notebook_number, reservation.customer_id), ("", None))
        self.assertEqual((reservation.status, reservation.channel), (Reservation.Status.CONFIRMED, Reservation.Channel.WALK_IN))
        self.assertEqual(reservation.items.count(), 1)

    def test_a_website_account_can_still_be_linked(self):
        g = make_gown(502)
        response = self.post_without("notebook_number", lines=[self.line(g)], account=self.customer)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Reservation.objects.get(reference_code=response.json()["reference_code"]).customer, self.customer)

    def test_two_gowns_on_different_dates_still_save_together(self):
        a, b = make_gown(503), make_gown(504)
        response = self.post_without("notebook_number", "account", lines=[self.line(a), self.line(b, self.event.replace(day=min(self.event.day, 20)))])
        self.assertIn(response.status_code, (200, 409), response.content)              # both are free, or the 2nd date collides with the cooldown: never a crash


class PageMarkupPromisesTests(Base):
    """The markup is the part a tester judges, so what the redesign promised is pinned down here."""

    def setUp(self):
        super().setUp()
        make_gown(601)
        self.html = self.client.get(reverse("arabela_admin:new_booking")).content.decode()
        start = self.html.index('<form x-show="!saved"')
        self.form = self.html[start:self.html.index("</form>", start)]

    def test_the_notebook_number_is_gone_from_the_page_and_the_page_never_sends_it(self):
        self.assertNotIn("nb-notebook", self.html)
        self.assertNotIn("Notebook", self.html)
        self.assertNotIn("notebook_number", self.html)

    def test_the_website_account_is_still_there_but_closed_behind_an_icon(self):
        self.assertIn('aria-label="Link a website account (optional)"', self.form)
        self.assertIn('<template x-if="!account &amp;&amp; linkOpen">', self.form)          # the search only exists once the icon is pressed
        self.assertIn('x-model="accountQuery"', self.form)
        self.assertIn("form.append('account'", self.html)
        self.assertIn('aria-label="Unlink the website account"', self.form)

    def test_the_long_explanations_live_only_inside_question_mark_popovers(self):
        for text in ("The day the customer wears it", "Tick this when the customer pays and leaves", "Only if the customer already has an account on the website",
                     "One photo of the shop's receipt", "The gown the customer chose. Open the list", "Paid in full:</strong> the rental and the deposit were paid."):
            with self.subTest(text=text):
                self.assertEqual(self.form.count(text), 1)
                self.assertIn("nb-tip-pop", self.form[self.form.index(text) - 90:self.form.index(text)])
        for gone in ("Find the gown the customer chose, by its ID, tag number or name", "Most walk-ins have no account. Leave this empty",
                     "Only if the shop also wrote it on paper"):
            self.assertNotIn(gone, self.html)

    def test_no_question_mark_sits_inside_a_label(self):
        """A button or summary inside a <label> hijacks the label's control: pressing the ? next to "takes the gown now" would tick the box."""
        for label in re.findall(r"<label\b[^>]*>.*?</label>", self.form, flags=re.S):
            self.assertNotIn("nb-tip", label)
            self.assertNotIn("<summary", label)

    def test_every_button_in_the_form_has_a_type_so_none_can_submit_it_by_accident(self):
        untyped = re.findall(r"<button(?![^>]*\btype=)[^>]*>", self.form)
        self.assertEqual(untyped, [])
        self.assertEqual(len(re.findall(r'<button[^>]*type="submit"', self.form)), 1)         # only Save booking submits

    def test_every_icon_button_has_a_tooltip_and_a_screen_reader_label(self):
        icon_buttons = re.findall(r'<button\b[^>]*class="nb-ib[^"]*"[^>]*>', self.form, flags=re.S)
        self.assertGreaterEqual(len(icon_buttons), 10)
        for tag in icon_buttons:
            with self.subTest(tag=tag[:120]):
                self.assertRegex(tag, r"(?<![\w-])(:?)title=")
                self.assertRegex(tag, r"(:?)aria-label=")

    def test_the_gown_actions_are_the_icons_that_were_asked_for(self):
        for label in ('aria-label="Add another gown"', ':aria-label="\'Edit gown \' + (index + 1)"', ':aria-label="\'Remove gown \' + (index + 1)"',
                      'aria-label="Done"', 'aria-label="Take a photo of the receipt"', 'aria-label="Choose the receipt photo"',
                      'aria-label="Remove this photo"'):
            with self.subTest(label=label):
                self.assertIn(label, self.form)

    def test_save_booking_and_cancel_keep_an_icon_and_their_word(self):
        tail = self.form[self.form.index('<div class="nb-actions" style="margin-bottom: 1.5rem;">'):]
        self.assertEqual(len(re.findall(r"<svg", tail)), 2)
        self.assertIn("Cancel", tail)
        self.assertIn("Save booking", tail)

    def test_the_gown_dropdown_has_its_search_and_category_filter_and_loads_more(self):
        for needle in ('aria-label="Search gowns"', 'aria-label="Filter by category"', 'All categories', 'Show more gowns', 'role="listbox"',
                       "@mousemove=\"line.pk.active = i\"", '@keydown.arrow-down.prevent="moveActive(line, 1)"', '@keydown.enter.prevent="pickActive(line)"'):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.form)
        self.assertNotIn("@mouseenter", self.form)                 # a resting mouse pointer must not steal the keyboard highlight

    def test_only_one_editor_can_be_open_and_rows_are_short(self):
        self.assertIn('<template x-if="editKey !== line.key">', self.form)
        self.assertIn('<template x-if="editKey === line.key">', self.form)
        self.assertEqual(self.form.count('class="nb-edit"'), 1)                  # a single editor template, shown for the one open row

    def test_the_owner_only_pick_up_day_is_still_owner_only(self):
        self.assertNotIn("Picked up on", self.html)
        self.client.force_login(self.owner)
        owner_html = self.client.get(reverse("arabela_admin:new_booking")).content.decode()
        self.assertIn("Picked up on", owner_html)
