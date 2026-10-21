"""Five follow-ups from the full-system test of 2026-10-21:
  1. a customer cancelling their own booking frees the 5 rest days after it (as rejecting and the shop's Cancel already did);
  2. the dashboard's Active Rentals tile counts only bookings still in progress, and Active Reservations labels finished ones;
  3. approving or rejecting a booking rings the customer's bell;
  4. the owner can't save a Facebook address that isn't a normal web link;
  5. Staff Management no longer carries the leftover customer pop-up that threw JavaScript errors."""
import json
from datetime import timedelta

from django.core import mail
from django.test import override_settings
from django.urls import reverse

from accounts.models import CustomerMessage
from arabela_admin.test_cancel_booking import EMAILS_ON, Base
from gowns.models import GownUnavailability, SiteSettings
from reservations.models import Reservation, ReservationItem


class CustomerCancelFreesTheRestDays(Base):
    def test_cancelling_your_own_booking_removes_its_automatic_rest_blocks(self):
        reservation, items = self.booking(days_late=-5, gowns=2, status=Reservation.Status.PENDING, verified=False)
        self.assertEqual(GownUnavailability.objects.filter(auto_for_item__in=items).count(), 2)
        manual = GownUnavailability.objects.create(                      # staff's own block on the same gown must stay
            gown=items[0].gown, start_date=self.today + timedelta(days=60), end_date=self.today + timedelta(days=62),
            reason=GownUnavailability.Reason.CLEANING)
        self.client.force_login(self.customer)
        response = self.client.post(reverse("gowns:reservation_item_cancel", args=[items[0].pk]))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(GownUnavailability.objects.filter(auto_for_item__in=items).exists())
        self.assertTrue(GownUnavailability.objects.filter(pk=manual.pk).exists())
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CANCELLED)

    def test_the_freed_dates_can_be_booked_again(self):
        from gowns.views import _UNIT_ASSIGNED, _UNIT_UNAVAILABLE, _find_available_unit
        reservation, [item] = self.booking(days_late=-5, status=Reservation.Status.PENDING, verified=False)
        day_after_return = item.return_date + timedelta(days=1)
        def outcome():
            return _find_available_unit(item.gown.name, day_after_return, day_after_return + timedelta(days=4),
                                        gown_slug=item.gown.slug, size="M")[0]
        self.assertEqual(outcome(), _UNIT_UNAVAILABLE)                    # resting
        self.client.force_login(self.customer)
        self.client.post(reverse("gowns:reservation_item_cancel", args=[item.pk]))
        self.assertEqual(outcome(), _UNIT_ASSIGNED)                       # free the moment it is cancelled


class ActiveRentalsTile(Base):
    def tile(self):
        return self.client.get(reverse("arabela_admin:dashboard")).context["active_rentals"]

    def test_only_bookings_with_a_gown_still_to_come_out_or_come_back_are_counted(self):
        self.booking(days_late=-5, number_from=1)                                                     # waiting for pick-up
        self.booking(days_late=3, number_from=2, stage=ReservationItem.Stage.RESERVED)                # gown is out
        self.booking(days_late=10, number_from=3, stage=ReservationItem.Stage.OVERDUE)                # late
        self.booking(days_late=20, number_from=4, stage=ReservationItem.Stage.RETURNED)               # finished
        self.booking(days_late=5, number_from=5, stage=ReservationItem.Stage.CANCELLED)               # a cancelled gown
        self.booking(days_late=-5, number_from=6, status=Reservation.Status.PENDING, verified=False)  # not approved yet
        self.booking(days_late=-5, number_from=7, status=Reservation.Status.CANCELLED)                # cancelled booking
        self.assertEqual(self.tile(), 3)

    def test_a_booking_counts_once_and_stays_while_any_one_gown_is_still_out(self):
        reservation, items = self.booking(days_late=10, gowns=3, number_from=10, stage=ReservationItem.Stage.RESERVED)
        self.assertEqual(self.tile(), 1)                                                              # three gowns, one booking
        ReservationItem.objects.filter(pk=items[0].pk).update(stage=ReservationItem.Stage.RETURNED)
        self.assertEqual(self.tile(), 1)                                                              # partly returned = still active
        ReservationItem.objects.filter(pk__in=[items[1].pk, items[2].pk]).update(stage=ReservationItem.Stage.RETURNED)
        self.assertEqual(self.tile(), 0)                                                              # last one back = done

    def test_active_reservations_labels_a_finished_booking_completed_not_confirmed(self):
        reservation, _ = self.booking(days_late=20, number_from=20, stage=ReservationItem.Stage.RETURNED)
        live, _ = self.booking(days_late=3, number_from=21, stage=ReservationItem.Stage.RESERVED)
        html = self.client.get(reverse("arabela_admin:active_reservations")).content.decode()
        self.assertIn(">Completed</p>", html)
        self.assertEqual(html.count(">Completed</p>"), 1)
        self.assertIn(">Confirmed</p>", html)                                                         # the live one keeps its badge


class ApproveAndRejectRingTheBell(Base):
    def pending(self):
        reservation, items = self.booking(days_late=-5, gowns=2, status=Reservation.Status.PENDING, verified=False)
        return reservation, items

    def messages(self):
        return list(CustomerMessage.objects.filter(recipient=self.customer))

    def test_approving_adds_one_unread_message_naming_the_booking_and_gowns(self):
        reservation, items = self.pending()
        response = self.client.post(reverse("arabela_admin:reservation_approve", args=[reservation.pk]))
        self.assertEqual(response.status_code, 200, response.content)
        [message] = self.messages()
        self.assertEqual(message.category, CustomerMessage.Category.GENERAL)
        self.assertFalse(message.is_read)
        self.assertIn(reservation.reference_code, message.body)
        self.assertIn("approved", message.body)
        self.assertIn(items[0].gown_name, message.body)
        self.assertIn(items[1].gown_name, message.body)

    def test_rejecting_adds_a_message_with_the_reason(self):
        reservation, _ = self.pending()
        response = self.client.post(reverse("arabela_admin:reservation_reject", args=[reservation.pk]),
                                    data=json.dumps({"reason": "The payment screenshot is blurry."}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        [message] = self.messages()
        self.assertIn("not approved", message.body)
        self.assertIn("The payment screenshot is blurry.", message.body)
        self.assertIn(reservation.reference_code, message.body)

    def test_a_second_click_does_not_add_a_second_message(self):
        reservation, _ = self.pending()
        url = reverse("arabela_admin:reservation_approve", args=[reservation.pk])
        self.client.post(url)
        self.assertEqual(self.client.post(url).status_code, 409)
        self.assertEqual(len(self.messages()), 1)

    @override_settings(**EMAILS_ON)
    def test_the_customers_bell_badge_counts_it_and_the_customer_gets_exactly_one_email(self):
        from gowns.context_processors import unread_messages_total
        reservation, _ = self.pending()
        self.assertEqual(unread_messages_total(self.customer), 0)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("arabela_admin:reservation_approve", args=[reservation.pk]))
        self.assertEqual(unread_messages_total(self.customer), 1)
        self.assertEqual(len(mail.outbox), 1)                                                          # the approval email only: the bell entry sends none


class FacebookAddressIsChecked(Base):
    def save(self, user, **fields):
        self.client.force_login(user)
        return self.client.post(reverse("arabela_admin:save_profile"), data=json.dumps(fields), content_type="application/json")

    def test_a_javascript_address_is_refused_and_nothing_else_is_saved(self):
        before = SiteSettings.load().facebook_url
        response = self.save(self.owner, firstName="Changed", facebook="javascript:alert(1)")
        self.assertEqual(response.status_code, 400)
        self.assertIn("https://", response.json()["error"])
        self.assertEqual(SiteSettings.load().facebook_url, before)
        self.owner.refresh_from_db()
        self.assertNotEqual(self.owner.first_name, "Changed")                                         # the whole save was refused

    def test_other_non_web_addresses_are_refused(self):
        for bad in ("data:text/html,hi", "ftp://facebook.com/arabela", "facebook.com/arabela", "just words", "https://exa mple.com",
                    "https://www.facebook.com/" + "a" * 200):
            self.assertEqual(self.save(self.owner, facebook=bad).status_code, 400, bad)

    def test_a_normal_link_blank_and_surrounding_spaces_are_accepted(self):
        self.assertEqual(self.save(self.owner, facebook="  https://www.facebook.com/arabela.gowns  ").status_code, 200)
        self.assertEqual(SiteSettings.load().facebook_url, "https://www.facebook.com/arabela.gowns")
        self.assertEqual(self.save(self.owner, facebook="").status_code, 200)
        self.assertEqual(SiteSettings.load().facebook_url, "")

    def test_a_staff_member_still_cannot_change_it_at_all(self):
        before = SiteSettings.load().facebook_url
        response = self.save(self.staff, firstName="Maria", facebook="https://www.facebook.com/hacked")
        self.assertEqual(response.status_code, 200)                                                    # their own name saves...
        self.assertEqual(SiteSettings.load().facebook_url, before)                                     # ...the shop's address does not


class StaffManagementHasNoLeftoverPopup(Base):
    def test_the_page_no_longer_refers_to_the_customer_popup_that_does_not_exist_there(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("arabela_admin:staff_management")).content.decode()
        self.assertNotIn("isViewCustomerModal", html)
        self.assertNotIn("viewingCustomer", html)
        self.assertIn("isEditStaffModal", html)                                                         # the real dialogs are still there
