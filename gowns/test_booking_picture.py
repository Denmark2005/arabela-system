"""The picture on a booking's page is the picture of the gown that was RESERVED.

A booking line only kept a link to its gown and the gown's name. When the gown was deleted from the catalog (a reset, a removal) the link was
cleared and the page showed a stock gown picture from the website template: a gown that was never reserved (RSV-2026-0011 "Kid Suit Three").
Now:
  * the picture is saved on the booking when it is made (checkout and the walk-in New Booking);
  * the page shows the gown's own photo while the gown exists, else the saved picture, else an honest "Photo not available" card -- never the
    stock picture and never another gown's;
  * the migration gave every existing booking its picture: from the gown that is still there, or -- only when it is certain -- from the
    removal log.
"""
from datetime import timedelta
from decimal import Decimal
from importlib import import_module

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from arabela_admin.test_new_booking import Base as WalkInBase, make_gown as walk_in_gown
from gowns.models import Gown, GownRemoval
from gowns.test_receipt_checkout import ReceiptCheckBase, good_reading
from gowns.views import _FALLBACK_IMG
from reservations.models import Reservation, ReservationItem

User = get_user_model()
PHOTO = "https://res.cloudinary.com/demo/image/upload/v1/gown_photos/live.jpg"
SAVED = "https://res.cloudinary.com/demo/image/upload/v1/gown_photos/saved.jpg"
OLD = "https://res.cloudinary.com/demo/image/upload/v1/gown_photos/old.jpg"
STOCK_HOST = "lh3.googleusercontent.com"


def make_gown(number, photo=PHOTO, name="Picture Gown"):
    return Gown.objects.create(
        gown_id=f"Wedding Gown-PC-{number:03d}", name=f"{name} {number}", category=Gown.Category.WEDDING_GOWN, color_name="White",
        color_code="WH", size=Gown.Size.MEDIUM, rental_price=Decimal("3000"), status=Gown.Status.AVAILABLE, photo_url=photo)


class PageBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user("pic_customer", "pic.customer@gmail.com", "x")

    def booking(self, gown=None, *, name="Picture Gown 1", saved="", **kwargs):
        today = timezone.localdate()
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Pic Customer", status=Reservation.Status.CONFIRMED)
        return ReservationItem.objects.create(
            reservation=reservation, gown=gown, gown_name=name, gown_photo_url=saved, rental_date=today + timedelta(days=5),
            return_date=today + timedelta(days=9), **kwargs)

    def page(self, item):
        self.client.force_login(self.customer)
        response = self.client.get(reverse("gowns:reservation_item_detail", args=[item.pk]))
        self.assertEqual(response.status_code, 200)
        return response.context, response.content.decode()


class ThePageShowsTheGownThatWasReservedTests(PageBase):
    def test_a_gown_still_in_the_catalog_shows_its_own_photo(self):
        gown = make_gown(1)
        _, html = self.page(self.booking(gown, name=gown.name, saved=SAVED))
        self.assertIn("gown_photos/live.jpg", html)
        self.assertNotIn("gown_photos/saved.jpg", html)
        self.assertNotIn('id="gown-photo-missing"', html)

    def test_a_deleted_gown_shows_the_picture_saved_on_the_booking(self):
        _, html = self.page(self.booking(None, saved=SAVED))
        self.assertIn("gown_photos/saved.jpg", html)
        self.assertNotIn('id="gown-photo-missing"', html)

    def test_a_gown_that_lost_its_photo_falls_back_to_the_saved_one(self):
        gown = make_gown(2, photo="")
        _, html = self.page(self.booking(gown, name=gown.name, saved=SAVED))
        self.assertIn("gown_photos/saved.jpg", html)

    def test_a_deleted_gown_with_no_saved_picture_says_so_and_never_shows_the_stock_gown(self):
        context, html = self.page(self.booking(None, name="Kid Suit Three"))
        self.assertEqual(context["gown_photo"], "")
        self.assertIn('id="gown-photo-missing"', html)
        self.assertIn("Photo not available", html)
        self.assertIn("This gown is no longer in our catalog", html)
        self.assertNotIn(STOCK_HOST, html)
        self.assertNotIn(_FALLBACK_IMG[:60], html)

    def test_the_gown_name_and_dates_stay_when_the_picture_is_gone(self):
        _, html = self.page(self.booking(None, name="Kid Suit Three"))
        self.assertIn("Kid Suit Three", html)

    def test_deleting_the_gown_keeps_the_picture_on_the_booking(self):
        gown = make_gown(3)
        item = self.booking(gown, name=gown.name, saved=gown.photo_url)
        gown.delete()
        item.refresh_from_db()
        self.assertIsNone(item.gown_id)
        _, html = self.page(item)
        self.assertIn("gown_photos/live.jpg", html)

    def test_two_bookings_of_different_deleted_gowns_never_share_a_picture(self):
        first = self.booking(None, name="Gown A", saved=SAVED)
        second = self.booking(None, name="Gown B", saved=OLD)
        self.assertIn("saved.jpg", self.page(first)[1])
        self.assertNotIn("old.jpg", self.page(first)[1])
        self.assertIn("old.jpg", self.page(second)[1])

    def test_no_unrendered_template_code_leaks_into_the_missing_picture_card(self):
        _, html = self.page(self.booking(None))
        for leak in ("{%", "{{", "{#"):
            self.assertNotIn(leak, html)


class TheCheckoutSavesThePictureTests(ReceiptCheckBase):
    def test_a_customers_booking_keeps_the_picture_of_the_booked_gown(self):
        Gown.objects.filter(pk=self.gowns[0].pk).update(photo_url=PHOTO)
        self.reads(good_reading())
        item = self.booked(self.checkout("a")).items.get()
        self.assertEqual(item.gown_photo_url, PHOTO)

    def test_a_gown_with_no_photo_saves_nothing_and_the_booking_still_works(self):
        self.reads(good_reading())
        item = self.booked(self.checkout("b")).items.get()
        self.assertEqual(item.gown_photo_url, "")

    def test_the_saved_picture_survives_the_gown_being_deleted_and_the_page_still_shows_it(self):
        Gown.objects.filter(pk=self.gowns[0].pk).update(photo_url=PHOTO)
        self.reads(good_reading())
        item = self.booked(self.checkout("a")).items.get()
        Gown.objects.filter(pk=self.gowns[0].pk).delete()
        client = Client()
        client.force_login(self.customers["a"])
        html = client.get(reverse("gowns:reservation_item_detail", args=[item.pk])).content.decode()
        self.assertIn("gown_photos/live.jpg", html)
        self.assertNotIn('id="gown-photo-missing"', html)


class WalkInSavesThePictureTests(WalkInBase):
    def test_a_walk_in_booking_keeps_the_picture_of_the_booked_gown(self):
        gown = walk_in_gown(41)
        Gown.objects.filter(pk=gown.pk).update(photo_url=PHOTO)
        response = self.post_booking([self.line(gown)])
        self.assertEqual(response.status_code, 200, response.content)
        item = ReservationItem.objects.get(reservation__reference_code=response.json()["reference_code"])
        self.assertEqual(item.gown_photo_url, PHOTO)

    def test_a_gown_with_no_photo_saves_nothing_and_the_booking_still_works(self):
        gown = walk_in_gown(42)
        response = self.post_booking([self.line(gown)])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(ReservationItem.objects.get(reservation__reference_code=response.json()["reference_code"]).gown_photo_url, "")


# ---- the migration ---------------------------------------------------------------------------------------------------------------------
class TheMigrationGaveExistingBookingsTheirPictureTests(PageBase):
    @staticmethod
    def run_migration():
        import_module("reservations.migrations.0028_reservationitem_gown_photo_url").keep_the_pictures(apps, None)

    def removal(self, name, photo=OLD, removed_at=None, number=1):
        return GownRemoval.objects.create(gown_id=f"Wedding Gown-RM-{number:03d}", name=name, photo_url=photo, reason=GownRemoval.Reason.OTHER,
                                          removed_at=removed_at)

    def test_a_booking_whose_gown_is_still_there_gets_that_gowns_picture(self):
        gown = make_gown(10)
        item = self.booking(gown, name=gown.name)
        self.run_migration()
        item.refresh_from_db()
        self.assertEqual(item.gown_photo_url, PHOTO)

    def test_a_picture_already_saved_is_never_overwritten(self):
        gown = make_gown(11)
        item = self.booking(gown, name=gown.name, saved=SAVED)
        self.run_migration()
        item.refresh_from_db()
        self.assertEqual(item.gown_photo_url, SAVED)

    def test_a_deleted_gown_gets_its_picture_from_the_removal_log_when_it_is_certain(self):
        item = self.booking(None, name="Wedding Gown 18")
        self.removal("Wedding Gown 18", removed_at=timezone.now() + timedelta(days=3))
        self.run_migration()
        item.refresh_from_db()
        self.assertEqual(item.gown_photo_url, OLD)

    def test_the_name_may_differ_in_capitals(self):
        item = self.booking(None, name="wedding gown 18")
        self.removal("Wedding Gown 18", removed_at=timezone.now() + timedelta(days=3))
        self.run_migration()
        item.refresh_from_db()
        self.assertEqual(item.gown_photo_url, OLD)

    def test_two_removals_with_the_same_name_are_not_certain_so_nothing_is_guessed(self):
        item = self.booking(None, name="Wedding Gown 18")
        self.removal("Wedding Gown 18", removed_at=timezone.now() + timedelta(days=3), number=1)
        self.removal("Wedding Gown 18", photo=SAVED, removed_at=timezone.now() + timedelta(days=4), number=2)
        self.run_migration()
        item.refresh_from_db()
        self.assertEqual(item.gown_photo_url, "")

    def test_a_removal_made_before_the_booking_cannot_be_the_gown_that_was_booked(self):
        item = self.booking(None, name="Wedding Gown 18")
        self.removal("Wedding Gown 18", removed_at=timezone.now() - timedelta(days=30))
        self.run_migration()
        item.refresh_from_db()
        self.assertEqual(item.gown_photo_url, "")

    def test_a_removal_with_no_photo_gives_nothing(self):
        item = self.booking(None, name="Wedding Gown 18")
        self.removal("Wedding Gown 18", photo="", removed_at=timezone.now() + timedelta(days=3))
        self.run_migration()
        item.refresh_from_db()
        self.assertEqual(item.gown_photo_url, "")

    def test_a_deleted_gown_nobody_logged_stays_without_a_picture(self):
        item = self.booking(None, name="Kid Suit Three")
        self.run_migration()
        item.refresh_from_db()
        self.assertEqual(item.gown_photo_url, "")

    def test_a_removal_with_an_unknown_date_still_counts_when_it_is_the_only_one(self):
        item = self.booking(None, name="Wedding Gown 19")
        self.removal("Wedding Gown 19", removed_at=None)
        self.run_migration()
        item.refresh_from_db()
        self.assertEqual(item.gown_photo_url, OLD)

    def test_running_it_twice_changes_nothing_more(self):
        gown = make_gown(12)
        item = self.booking(gown, name=gown.name)
        self.run_migration()
        self.run_migration()
        item.refresh_from_db()
        self.assertEqual(item.gown_photo_url, PHOTO)
