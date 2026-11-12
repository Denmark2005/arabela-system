"""The shop's GCash number over time: a receipt is judged against the number that was the shop's WHEN IT WAS PAID.

The owner may change the number (and QR) at any moment. A customer who opened the QR page, paid the old number and uploaded the receipt a few
minutes later must pass; a payment to a number the shop abandoned long ago must not. The owner picks, when she changes the number, how long the
old one keeps counting: not at all, 24 hours or 48 hours (the default)."""
import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from gowns import gcash_numbers
from gowns.models import GcashNumberHistory, SiteSettings

User = get_user_model()
OLD = "09171234567"
NEW = "09488745992"
SAVE = reverse("arabela_admin:gcash_details_update")


def numbers_at(moment):
    """{number: is_old} -- the order the numbers come back in does not matter."""
    return dict(gcash_numbers.accepted_numbers_at(moment))


def which(moment):
    return set(numbers_at(moment))


class NormalizeTests(TestCase):
    def test_every_common_way_of_writing_a_number(self):
        for raw in ("09488745992", "0948 874 5992", "+63 948 874 5992", "639488745992", "0948-874-5992", "9488745992", "948 874 5992"):
            self.assertEqual(gcash_numbers.normalize(raw), NEW, raw)
        for raw in ("", None, "12345", "0848 874 5992", "+1 555 123 4567", "0948 874 599"):
            self.assertEqual(gcash_numbers.normalize(raw), "", raw)

    def test_the_grace_is_zero_24_or_48_and_anything_else_is_the_default(self):
        self.assertEqual([gcash_numbers.clean_grace(v) for v in (0, "24", 48, "0")], [0, 24, 48, 0])
        for odd in (7, "abc", None, -1, 72, ""):
            self.assertEqual(gcash_numbers.clean_grace(odd), 48, odd)


class HistoryTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        shop = SiteSettings.load()
        shop.gcash_number = NEW
        shop.save()

    def test_a_shop_that_never_changed_its_number_accepts_the_saved_one_always(self):
        self.assertEqual(numbers_at(self.now), {NEW: False})
        self.assertEqual(numbers_at(self.now - timedelta(days=400)), {NEW: False})
        self.assertEqual(gcash_numbers.known_numbers(), {NEW})

    def test_a_shop_with_no_number_accepts_nothing(self):
        shop = SiteSettings.load()
        shop.gcash_number = ""
        shop.save()
        self.assertEqual(numbers_at(self.now), {})

    def test_changing_the_number_keeps_the_old_one_for_the_chosen_grace(self):
        gcash_numbers.record_change(OLD, NEW, 48, now=self.now)
        self.assertEqual(numbers_at(self.now + timedelta(minutes=1)), {OLD: True, NEW: False})
        self.assertEqual(numbers_at(self.now + timedelta(hours=47)), {OLD: True, NEW: False})
        self.assertEqual(numbers_at(self.now + timedelta(hours=48, minutes=5)), {OLD: True, NEW: False})      # 10 minutes of overlap
        self.assertEqual(numbers_at(self.now + timedelta(hours=48, minutes=11)), {NEW: False})

    def test_24_hours_and_not_at_all(self):
        gcash_numbers.record_change(OLD, NEW, 24, now=self.now)
        self.assertEqual(which(self.now + timedelta(hours=23)), {OLD, NEW})
        self.assertEqual(which(self.now + timedelta(hours=25)), {NEW})

    def test_not_at_all_still_forgives_ten_minutes_of_clock_difference(self):
        gcash_numbers.record_change(OLD, NEW, 0, now=self.now)
        self.assertEqual(which(self.now + timedelta(minutes=9)), {OLD, NEW})
        self.assertEqual(which(self.now + timedelta(minutes=11)), {NEW})

    def test_a_payment_made_before_the_change_is_judged_by_the_number_of_that_time(self):
        gcash_numbers.record_change(OLD, NEW, 0, now=self.now)
        self.assertEqual(which(self.now - timedelta(hours=3)), {OLD})                         # the new number did not exist yet
        self.assertEqual(which(self.now - timedelta(minutes=5)), {OLD, NEW})                  # ten-minute overlap before it too

    def test_the_old_number_is_flagged_as_old_and_the_new_one_is_not(self):
        gcash_numbers.record_change(OLD, NEW, 48, now=self.now)
        flags = numbers_at(self.now + timedelta(hours=1))
        self.assertTrue(flags[OLD])
        self.assertFalse(flags[NEW])

    def test_every_number_the_shop_ever_had_is_known(self):
        gcash_numbers.record_change(OLD, NEW, 0, now=self.now)
        self.assertEqual(gcash_numbers.known_numbers(), {OLD, NEW})

    def test_saving_the_same_number_again_writes_nothing(self):
        gcash_numbers.record_change(NEW, "0948 874 5992", 48, now=self.now)
        self.assertEqual(GcashNumberHistory.objects.count(), 0)

    def test_changing_twice_keeps_both_old_numbers_each_with_its_own_grace(self):
        third = "09999999999"
        gcash_numbers.record_change(OLD, NEW, 0, now=self.now - timedelta(days=5))
        shop = SiteSettings.load()
        shop.gcash_number = third
        shop.save()
        gcash_numbers.record_change(NEW, third, 48, now=self.now)
        later = self.now + timedelta(hours=1)
        self.assertEqual(which(later), {NEW, third})                  # OLD's grace (0) ended long ago
        self.assertEqual(gcash_numbers.known_numbers(), {OLD, NEW, third})

    def test_a_number_saved_without_history_still_counts(self):
        # e.g. the number was changed straight in the database: history has only an old row, the settings hold the new number.
        GcashNumberHistory.objects.create(number=OLD, valid_from=None, valid_until=self.now - timedelta(days=9), grace_hours=0)
        self.assertEqual(which(self.now), {NEW})


class TheOwnersSaveButtonTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("gh_owner", "gh_owner@example.com", "Pass-12345!")
        cls.staff = User.objects.create_user("gh_staff", "gh_staff@example.com", "Pass-12345!", is_staff=True)
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.STAFF})

    def save(self, user, **data):
        self.client.force_login(user)
        return self.client.post(SAVE, data=json.dumps(data), content_type="application/json")

    def test_changing_the_number_writes_the_history_with_the_chosen_grace(self):
        self.assertEqual(self.save(self.owner, name="Arabela", number=OLD).status_code, 200)
        response = self.save(self.owner, name="Arabela", number=NEW, old_number_grace_hours=24)
        self.assertEqual(response.status_code, 200, response.content)
        old_row = GcashNumberHistory.objects.get(number=OLD)
        self.assertEqual(old_row.grace_hours, 24)
        self.assertIsNotNone(old_row.valid_until)
        self.assertIsNone(GcashNumberHistory.objects.get(number=NEW).valid_until)

    def test_a_missing_or_odd_grace_means_48_hours(self):
        self.save(self.owner, name="Arabela", number=OLD)
        self.save(self.owner, name="Arabela", number=NEW)
        self.assertEqual(GcashNumberHistory.objects.get(number=OLD).grace_hours, 48)
        self.save(self.owner, name="Arabela", number=OLD, old_number_grace_hours=999)
        self.assertEqual(GcashNumberHistory.objects.filter(number=NEW).first().grace_hours, 48)

    def test_changing_only_the_name_writes_no_history(self):
        self.save(self.owner, name="Arabela", number=NEW)
        before = GcashNumberHistory.objects.count()
        self.save(self.owner, name="Arabela Gowns", number=NEW)
        self.assertEqual(GcashNumberHistory.objects.count(), before)

    def test_staff_cannot_change_the_number_and_so_cannot_touch_the_history(self):
        response = self.save(self.staff, name="Mine", number=NEW)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(GcashNumberHistory.objects.count(), 0)
        self.assertEqual(SiteSettings.load().gcash_number, "")

    def test_a_bad_number_is_refused_and_the_history_is_untouched(self):
        self.save(self.owner, name="Arabela", number=OLD)
        response = self.save(self.owner, name="Arabela", number="12345")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SiteSettings.load().gcash_number, OLD)
        self.assertEqual(GcashNumberHistory.objects.count(), 1)

    def test_the_profile_page_asks_how_long_the_old_number_counts(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("arabela_admin:page", kwargs={"page": "profile"})).content.decode()
        self.assertIn("Keep accepting receipts paid to the old number", html)
        self.assertIn("for 48 hours (recommended)", html)
