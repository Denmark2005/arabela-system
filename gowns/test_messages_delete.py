"""The customer's Messages page: delete one message or Clear all (with Undo on the page), only their own and only ones
already read, and an overdue notice whose gowns are all back reads "Returned"."""
import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomerMessage
from reservations.models import Reservation, ReservationItem

User = get_user_model()


class MessagesDeleteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="msg_delete", password="x")
        cls.other = User.objects.create_user(username="msg_delete_other", password="x")

    def setUp(self):
        self.client.force_login(self.customer)

    def message(self, user=None, read=True, category=CustomerMessage.Category.GENERAL, body="Hello from Arabela."):
        return CustomerMessage.objects.create(recipient=user or self.customer, category=category, body=body, is_read=read)

    def delete(self, ids):
        return self.client.post(reverse("gowns:messages_delete"), data=json.dumps({"ids": ids}), content_type="application/json")

    def test_a_customer_deletes_their_own_read_messages(self):
        first, second, kept = self.message(), self.message(), self.message()
        response = self.delete([first.id, second.id])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True, "deleted": 2})
        self.assertEqual(list(CustomerMessage.objects.filter(recipient=self.customer)), [kept])

    def test_someone_elses_message_and_an_unread_one_are_never_deleted(self):
        theirs = self.message(user=self.other)
        unseen = self.message(read=False)                     # arrived after the page was opened
        self.assertEqual(self.delete([theirs.id, unseen.id]).json()["deleted"], 0)
        self.assertTrue(CustomerMessage.objects.filter(pk=theirs.pk).exists())
        self.assertTrue(CustomerMessage.objects.filter(pk=unseen.pk).exists())

    def test_bad_requests_are_refused_cleanly(self):
        url = reverse("gowns:messages_delete")
        self.assertEqual(self.delete([]).status_code, 400)
        self.assertEqual(self.delete(["abc"]).status_code, 400)
        self.assertEqual(self.delete(list(range(1, 502))).status_code, 400)
        self.assertEqual(self.client.post(url, data="not json", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post(url, data=json.dumps(["x"]), content_type="application/json").status_code, 400)
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_a_signed_out_visitor_gets_a_clear_answer_and_nothing_is_deleted(self):
        kept = self.message()
        self.client.logout()
        response = self.delete([kept.id])
        self.assertEqual(response.status_code, 401)
        self.assertTrue(CustomerMessage.objects.filter(pk=kept.pk).exists())

    def test_the_page_offers_delete_clear_all_and_undo(self):
        self.message()
        response = self.client.get(reverse("gowns:messages"))
        self.assertContains(response, 'id="messages-clear-all"')
        self.assertContains(response, "js-delete-message")
        self.assertContains(response, 'id="messages-undo-bar"')
        self.assertContains(response, f'data-delete-url="{reverse("gowns:messages_delete")}"')

    def test_with_no_messages_the_empty_note_shows_and_clear_all_is_hidden(self):
        html = self.client.get(reverse("gowns:messages")).content.decode()
        self.assertIn('id="messages-clear-all" class="mb-1 shrink-0', html)
        self.assertRegex(html, r'id="messages-clear-all" class="[^"]*messages-hidden')
        self.assertNotRegex(html, r'id="messages-empty" class="[^"]*messages-hidden')

    def test_an_overdue_notice_reads_returned_once_every_gown_is_back(self):
        today = timezone.localdate()
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Msg Cust", status=Reservation.Status.ACTIVE)
        item = ReservationItem.objects.create(
            reservation=reservation, gown_name="Wedding Gown 9", stage=ReservationItem.Stage.OVERDUE,
            rental_date=today - timedelta(days=8), event_date=today - timedelta(days=6),
            return_date=today - timedelta(days=4), overdue_date=today - timedelta(days=4))
        body = f"Wedding Gown 9 was due back and is now 4 days overdue. Reference: {reservation.reference_code}."
        self.message(category=CustomerMessage.Category.RETURN_OVERDUE, body=body)

        still_out = self.client.get(reverse("gowns:messages"))
        self.assertContains(still_out, "now 4 days overdue")
        self.assertNotContains(still_out, "this overdue notice is closed")

        ReservationItem.objects.filter(pk=item.pk).update(stage=ReservationItem.Stage.RETURNED)
        back = self.client.get(reverse("gowns:messages"))
        self.assertContains(back, "your gown has been returned, so this overdue notice is closed")
        self.assertContains(back, f"Reference: {reservation.reference_code}.")
        self.assertNotContains(back, "now 4 days overdue")
