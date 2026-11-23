"""The Gown Inventory figures (dashboard tile + Gown Catalog strip): every gown is in exactly ONE box, so
Available + Reserved + Blocked + Out-of-Stock always add up to Total Gowns.

Before this rule the shop saw Total 650 with Available 615, Reserved 34 and Blocked 5: the Out-of-Stock gown was in no box at all and the
5 blocked gowns were ALSO counted as Available (or Reserved), so the boxes did not add up to the total on either page.
"""
import itertools
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from arabela_admin import gown_inventory
from arabela_admin.tests import _make_gown
from gowns.models import Gown, GownUnavailability

User = get_user_model()
A, R, O = Gown.Status.AVAILABLE, Gown.Status.RESERVED, Gown.Status.OUT_OF_STOCK


class TheRuleTests(SimpleTestCase):
    def test_the_shops_own_numbers_now_add_up(self):
        # what the live dashboard showed: 650 gowns, 615 + 34 by status (so exactly one Out-of-Stock), and 5 blocked gowns that are Available
        statuses = [(i, A) for i in range(615)] + [(1000 + i, R) for i in range(34)] + [(2000, O)]
        figures = gown_inventory.split(statuses, blocked_ids=range(5))
        self.assertEqual(figures, {"total": 650, "available": 610, "reserved": 34, "blocked": 5, "out_of_stock": 1})
        self.assertEqual(figures["available"] + figures["reserved"] + figures["blocked"] + figures["out_of_stock"], figures["total"])

    def test_every_combination_adds_up_to_the_total(self):
        for combo in itertools.product((A, R, O, "Something Old"), repeat=5):
            for blocked in itertools.product((False, True), repeat=5):
                statuses = list(enumerate(combo))
                figures = gown_inventory.split(statuses, [i for i, b in enumerate(blocked) if b])
                with self.subTest(combo=combo, blocked=blocked):
                    self.assertEqual(figures["total"], 5)
                    self.assertEqual(figures["available"] + figures["reserved"] + figures["blocked"] + figures["out_of_stock"], 5)

    def test_a_reserved_gown_that_is_also_blocked_is_counted_as_reserved_only(self):
        self.assertEqual(gown_inventory.split([(1, R)], [1]), {"total": 1, "available": 0, "reserved": 1, "blocked": 0, "out_of_stock": 0})

    def test_an_out_of_stock_gown_that_is_also_blocked_is_counted_as_out_of_stock_only(self):
        self.assertEqual(gown_inventory.split([(1, O)], [1]), {"total": 1, "available": 0, "reserved": 0, "blocked": 0, "out_of_stock": 1})

    def test_an_available_gown_that_is_blocked_is_blocked_not_available(self):
        self.assertEqual(gown_inventory.split([(1, A), (2, A)], [1]), {"total": 2, "available": 1, "reserved": 0, "blocked": 1, "out_of_stock": 0})

    def test_two_blocks_on_one_gown_are_still_one_blocked_gown(self):
        self.assertEqual(gown_inventory.split([(1, A)], [1, 1, 1])["blocked"], 1)

    def test_a_status_nobody_expects_is_out_of_stock_so_no_gown_goes_missing(self):
        self.assertEqual(gown_inventory.split([(1, "In-Cleaning")], [])["out_of_stock"], 1)

    def test_no_gowns_is_all_zeros(self):
        self.assertEqual(gown_inventory.split([], []), {"total": 0, "available": 0, "reserved": 0, "blocked": 0, "out_of_stock": 0})

    def test_it_is_pure_arithmetic_no_database_and_no_models(self):
        source = (Path(settings.BASE_DIR) / "arabela_admin" / "gown_inventory.py").read_text(encoding="utf8")
        self.assertNotIn("import", source.split('"""', 2)[2])
        self.assertNotIn(".objects", source)

    def test_both_pages_use_this_one_rule(self):
        views = (Path(settings.BASE_DIR) / "arabela_admin" / "views.py").read_text(encoding="utf8")
        self.assertEqual(views.count("gown_inventory.split("), 2)             # the dashboard and Gown Catalog, nothing else counts the boxes


class ThePagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="inv_fig_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)
        self.today = timezone.localdate()

    def block(self, gown, start=-1, end=1):
        return GownUnavailability.objects.create(
            gown=gown, start_date=self.today + timedelta(days=start), end_date=self.today + timedelta(days=end),
            reason=GownUnavailability.Reason.CLEANING)

    def both(self):
        dashboard = self.client.get(reverse("arabela_admin:dashboard")).context
        catalog = self.client.get(reverse("arabela_admin:gown_catalog")).context
        return dashboard, catalog

    def test_both_pages_add_up_and_agree_with_a_mix_of_every_kind(self):
        for n in (1, 2, 3):
            _make_gown(n, status=A)
        for n in (4, 5):
            _make_gown(n, status=R)
        _make_gown(6, status=O)
        self.block(_make_gown(7, status=A))                    # blocked today, Available by status
        self.block(_make_gown(8, status=R))                    # blocked today AND Reserved: counted once, as Reserved
        self.block(_make_gown(9, status=A), 4, 6)              # a block in the future only: still Available today
        dashboard, catalog = self.both()
        self.assertEqual(
            (dashboard["gown_total"], dashboard["gown_available"], dashboard["gown_reserved"], dashboard["gown_blocked"], dashboard["gown_out_of_stock"]),
            (9, 4, 3, 1, 1))
        self.assertEqual(
            (catalog["total_gowns_count"], catalog["available_count"], catalog["reserved_count"], catalog["blocked_count"], catalog["out_of_stock_count"]),
            (9, 4, 3, 1, 1))
        self.assertEqual(dashboard["gown_available"] + dashboard["gown_reserved"] + dashboard["gown_blocked"] + dashboard["gown_out_of_stock"], dashboard["gown_total"])

    def test_the_release_list_still_shows_every_blocked_gown_even_one_counted_as_reserved(self):
        _make_gown(1, status=A)
        self.block(_make_gown(2, status=R))
        self.block(_make_gown(3, status=A))
        _, catalog = self.both()
        self.assertEqual(catalog["blocked_count"], 1)
        self.assertEqual(len(catalog["blocked_gowns_today"]), 2)           # staff can still find and release both

    def test_the_catalog_strip_shows_the_new_box_with_its_number(self):
        _make_gown(1, status=A)
        _make_gown(2, status=O)
        _make_gown(3, status=O)
        html = self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()
        strip = html[html.index('class="gc-stats"'):html.index('class="gc-toolbar"')] if 'class="gc-toolbar"' in html else html[html.index('class="gc-stats"'):]
        self.assertIn("Out-of-Stock", strip)
        self.assertIn('<p class="gc-stat-num">2</p>', strip)

    def test_with_nothing_out_of_stock_both_pages_say_so(self):
        _make_gown(1)
        html = self.client.get(reverse("arabela_admin:dashboard")).content.decode()
        self.assertIn('title="None out of stock"', html)
        catalog = self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()
        self.assertIn("None out of stock", catalog)

    def test_the_dashboard_tile_has_five_figures_and_the_layout_makes_room_for_them(self):
        _make_gown(1)
        html = self.client.get(reverse("arabela_admin:dashboard")).content.decode()
        self.assertIn(".ov-strip--gowns { grid-template-columns: minmax(11rem, 1.25fr) repeat(5, minmax(0, 1fr));", html)
        self.assertIn(".ov-dot--oos", html)
        catalog = self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()
        self.assertIn("grid-template-columns: repeat(5, minmax(0, 1fr))", catalog)
