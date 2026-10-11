"""One reminder per booking (reservations/reminders.py send_reminder_group): several gowns of one booking share one message
and one email, an overdue notice is brought up to date instead of piling up, and once everything is back the old notice
reads "Returned" instead of a red warning. The shop's proof (one timeline entry per gown) is unchanged."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import CustomerMessage
from reservations import reminders
from reservations.models import Reservation, ReservationItem

User = get_user_model()
EMAILS_ON = dict(CUSTOMER_EMAILS_ENABLED=True, CUSTOMER_EMAILS_BACKGROUND=False, SITE_BASE_URL="https://arabela.example.org",
                 DEFAULT_FROM_EMAIL="Arabela <shop@gmail.com>")


class GroupedReminderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="group_rem", password="x", email="group@gmail.com")

    def setUp(self):
        self.today = timezone.localdate()
        self.reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Group Cust", status=Reservation.Status.ACTIVE,
            rental_subtotal=Decimal("10000"), security_deposit=Decimal("4000"), total_amount=Decimal("14000"))

    def gown(self, name, *, stage=ReservationItem.Stage.RESERVED, return_offset=1, reservation=None):
        return ReservationItem.objects.create(
            reservation=reservation or self.reservation, gown_name=name, stage=stage,
            rental_date=self.today - timedelta(days=5), event_date=self.today - timedelta(days=3),
            return_date=self.today + timedelta(days=return_offset), overdue_date=self.today + timedelta(days=return_offset))

    def messages(self):
        return list(CustomerMessage.objects.filter(recipient=self.customer).order_by("created_at", "id"))

    # ---- one message per booking -------------------------------------------------------------------------------------
    def test_two_gowns_due_back_tomorrow_get_one_message_naming_both(self):
        self.gown("Wedding Gown 9")
        self.gown("Wedding Gown 11")
        self.assertEqual(reminders.send_due_reminders(self.today), 1)
        [message] = self.messages()
        self.assertEqual(message.category, CustomerMessage.Category.RESERVATION_REMINDER)
        self.assertIn("Wedding Gown 9 and Wedding Gown 11 are due back tomorrow", message.body)
        self.assertTrue(message.body.endswith(f"Reference: {self.reservation.reference_code}."))
        label = reminders.EVENT_LABELS[reminders.RETURN_TOMORROW]
        self.assertEqual(self.reservation.status_events.filter(label=label).count(), 2)   # the proof stays per gown
        self.assertEqual(reminders.send_due_reminders(self.today), 0)                      # and nothing goes out twice

    def test_three_gowns_and_two_units_of_one_gown_read_naturally(self):
        self.gown("Bridesmaid Dress 3")
        self.gown("Bridesmaid Dress 3")
        self.gown("Suit 1")
        reminders.send_due_reminders(self.today)
        self.assertIn("Bridesmaid Dress 3 (x2) and Suit 1 are due back tomorrow", self.messages()[0].body)

    def test_pick_up_tomorrow_for_two_gowns_is_one_message(self):
        for name in ("Evening Gown 4", "Wedding Gown 17"):
            ReservationItem.objects.create(
                reservation=self.reservation, gown_name=name, stage=ReservationItem.Stage.PICKUP,
                rental_date=self.today + timedelta(days=1), event_date=self.today + timedelta(days=2),
                return_date=self.today + timedelta(days=4), overdue_date=self.today + timedelta(days=4))
        reminders.send_due_reminders(self.today)
        [message] = self.messages()
        self.assertIn("your rental of Evening Gown 4 and Wedding Gown 17 starts tomorrow", message.body)
        self.assertIn("pick up your gowns", message.body)

    def test_different_bookings_still_get_their_own_messages(self):
        other = Reservation.objects.create(customer=self.customer, customer_name="Group Cust", status=Reservation.Status.ACTIVE)
        self.gown("Wedding Gown 9")
        self.gown("Suit 2", reservation=other)
        self.assertEqual(reminders.send_due_reminders(self.today), 2)
        self.assertEqual(len(self.messages()), 2)

    def test_a_single_gown_reads_exactly_as_before(self):
        item = self.gown("Ivory Ballgown")
        reminders.send_due_reminders(self.today)
        self.assertEqual(self.messages()[0].body, reminders._body_for(reminders.RETURN_TOMORROW, item, self.today))

    # ---- overdue: one notice, brought up to date ---------------------------------------------------------------------
    def test_two_overdue_gowns_share_one_notice_that_is_updated_not_repeated(self):
        self.gown("Wedding Gown 9", stage=ReservationItem.Stage.OVERDUE, return_offset=-1)
        self.gown("Wedding Gown 11", stage=ReservationItem.Stage.OVERDUE, return_offset=-1)
        self.assertEqual(reminders.send_due_reminders(self.today), 1)
        [first] = self.messages()
        self.assertEqual(first.category, CustomerMessage.Category.RETURN_OVERDUE)
        self.assertIn("Wedding Gown 9 and Wedding Gown 11 were due back on", first.body)
        self.assertIn("are now 1 day overdue", first.body)
        CustomerMessage.objects.filter(pk=first.pk).update(is_read=True)

        later = self.today + timedelta(days=reminders.OVERDUE_REPEAT_DAYS)
        self.assertEqual(reminders.send_due_reminders(later), 1)
        [updated] = self.messages()                                          # still ONE message
        self.assertEqual(updated.pk, first.pk)
        self.assertIn(f"are now {1 + reminders.OVERDUE_REPEAT_DAYS} days overdue", updated.body)
        self.assertFalse(updated.is_read)                                    # the bell rings again
        self.assertGreater(updated.created_at, first.created_at)             # and it moves back to the top
        label = reminders.EVENT_LABELS[reminders.OVERDUE]
        self.assertEqual(self.reservation.status_events.filter(label=label).count(), 4)   # 2 gowns x 2 reminders

    def test_overdue_gowns_due_back_on_different_days_each_get_their_own_date(self):
        self.gown("Wedding Gown 9", stage=ReservationItem.Stage.OVERDUE, return_offset=-4)
        self.gown("Wedding Gown 11", stage=ReservationItem.Stage.OVERDUE, return_offset=-2)
        reminders.send_due_reminders(self.today)
        body = self.messages()[0].body
        self.assertIn("These gowns are overdue: Wedding Gown 9 (due back", body)
        self.assertIn("now 4 days overdue) and Wedding Gown 11 (due back", body)
        self.assertIn("now 2 days overdue)", body)

    def test_a_gown_that_turns_overdue_later_joins_the_same_notice(self):
        self.gown("Wedding Gown 9", stage=ReservationItem.Stage.OVERDUE, return_offset=-1)
        late = self.gown("Wedding Gown 11", stage=ReservationItem.Stage.RETURN, return_offset=1)
        reminders.send_due_reminders(self.today)                             # overdue for 9 + return-tomorrow for 11
        self.assertEqual(len(self.messages()), 2)
        ReservationItem.objects.filter(pk=late.pk).update(stage=ReservationItem.Stage.OVERDUE)
        reminders.send_due_reminders(self.today + timedelta(days=2))         # 11 is now 1 day overdue
        overdue = [m for m in self.messages() if m.category == CustomerMessage.Category.RETURN_OVERDUE]
        self.assertEqual(len(overdue), 1)
        self.assertIn("Wedding Gown 9", overdue[0].body)
        self.assertIn("Wedding Gown 11", overdue[0].body)

    def test_if_the_customer_deleted_the_notice_a_new_one_is_written(self):
        self.gown("Wedding Gown 9", stage=ReservationItem.Stage.OVERDUE, return_offset=-1)
        reminders.send_due_reminders(self.today)
        CustomerMessage.objects.filter(recipient=self.customer).delete()
        reminders.send_due_reminders(self.today + timedelta(days=reminders.OVERDUE_REPEAT_DAYS))
        [again] = self.messages()
        self.assertIn("4 days overdue", again.body)

    def test_another_bookings_notice_is_never_touched(self):
        other = Reservation.objects.create(customer=self.customer, customer_name="Group Cust", status=Reservation.Status.ACTIVE)
        self.gown("Suit 2", stage=ReservationItem.Stage.OVERDUE, return_offset=-1, reservation=other)
        reminders.send_due_reminders(self.today)
        self.gown("Wedding Gown 9", stage=ReservationItem.Stage.OVERDUE, return_offset=-1)
        reminders.send_due_reminders(self.today + timedelta(days=1))
        bodies = sorted(m.body for m in self.messages())
        self.assertEqual(len(bodies), 2)
        self.assertTrue(any("Suit 2" in b and other.reference_code in b for b in bodies))
        self.assertTrue(any("Wedding Gown 9" in b and self.reservation.reference_code in b for b in bodies))

    @override_settings(**EMAILS_ON)
    def test_every_update_of_the_notice_is_emailed_once_per_booking(self):
        self.gown("Wedding Gown 9", stage=ReservationItem.Stage.OVERDUE, return_offset=-1)
        self.gown("Wedding Gown 11", stage=ReservationItem.Stage.OVERDUE, return_offset=-1)
        with self.captureOnCommitCallbacks(execute=True):
            reminders.send_due_reminders(self.today)
        self.assertEqual(len(mail.outbox), 1)                                # one email, not one per gown
        with self.captureOnCommitCallbacks(execute=True):
            reminders.send_due_reminders(self.today + timedelta(days=reminders.OVERDUE_REPEAT_DAYS))
        self.assertEqual(len(mail.outbox), 2)                                # the update is emailed too
        self.assertEqual(mail.outbox[1].subject, "Your Arabela gown is overdue")
        self.assertIn("4 days overdue", mail.outbox[1].body)

    # ---- once everything is back -------------------------------------------------------------------------------------
    def test_a_fully_returned_booking_closes_its_overdue_notices(self):
        first = self.gown("Wedding Gown 9", stage=ReservationItem.Stage.OVERDUE, return_offset=-1)
        second = self.gown("Wedding Gown 11", stage=ReservationItem.Stage.OVERDUE, return_offset=-1)
        reminders.send_due_reminders(self.today)
        messages = self.messages()
        self.assertEqual(reminders.closed_overdue_notices(self.customer, messages), {})
        ReservationItem.objects.filter(pk=first.pk).update(stage=ReservationItem.Stage.RETURNED)
        self.assertEqual(reminders.closed_overdue_notices(self.customer, messages), {}, "one gown is still out")
        ReservationItem.objects.filter(pk=second.pk).update(stage=ReservationItem.Stage.RETURNED)
        self.assertEqual(reminders.closed_overdue_notices(self.customer, messages),
                         {messages[0].id: (2, self.reservation.reference_code)})

    def test_another_customers_booking_code_never_closes_a_notice(self):
        stranger = User.objects.create_user(username="group_rem_other", password="x")
        theirs = Reservation.objects.create(customer=stranger, customer_name="Other", status=Reservation.Status.ACTIVE)
        self.gown("Their Gown", stage=ReservationItem.Stage.RETURNED, reservation=theirs)
        fake = CustomerMessage.objects.create(recipient=self.customer, category=CustomerMessage.Category.RETURN_OVERDUE,
                                              body=f"Gown is overdue. Reference: {theirs.reference_code}.")
        self.assertEqual(reminders.closed_overdue_notices(self.customer, [fake]), {})
