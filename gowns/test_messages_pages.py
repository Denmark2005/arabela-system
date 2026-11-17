"""The customer's Messages page shows 10 messages a page (Previous / Next, "Page 2 of 4"), marks as read only the messages it shows, and is
easier to read (16px message text, 13px labels, 14px dates)."""
import json
import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomerMessage
from reservations.models import Reservation, ReservationItem

User = get_user_model()
URL = reverse("gowns:messages")


class PagesBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="msg_pages", password="x")
        cls.other = User.objects.create_user(username="msg_pages_other", password="x")

    def setUp(self):
        self.client.force_login(self.customer)

    def make(self, count, *, read=False, user=None, prefix="Note"):
        """`count` messages, oldest first, each a minute newer than the one before ("Note 1" is the oldest)."""
        start = timezone.now() - timedelta(hours=3)
        rows = []
        for n in range(1, count + 1):
            row = CustomerMessage.objects.create(recipient=user or self.customer, category=CustomerMessage.Category.GENERAL,
                                                 body=f"{prefix} {n} body", is_read=read)
            CustomerMessage.objects.filter(pk=row.pk).update(created_at=start + timedelta(minutes=n))
            rows.append(row)
        return rows

    def shown(self, response):
        """The message bodies on the page, in the order they are drawn."""
        return re.findall(r"(Note \d+) body", response.content.decode())


class TenAPageTests(PagesBase):
    def test_no_messages_means_no_pager_and_the_empty_note(self):
        html = self.client.get(URL).content.decode()
        self.assertNotIn('id="messages-pager"', html)
        self.assertNotRegex(html, r'id="messages-empty" class="[^"]*messages-hidden')
        self.assertIn(">Clear all</button>", html)

    def test_ten_messages_fit_one_page_with_no_pager(self):
        self.make(10)
        response = self.client.get(URL)
        self.assertEqual(len(self.shown(response)), 10)
        self.assertNotContains(response, 'id="messages-pager"')
        self.assertContains(response, ">Clear all</button>")
        self.assertContains(response, 'data-paged="false"')

    def test_the_eleventh_message_starts_a_second_page(self):
        self.make(11)
        first = self.client.get(URL)
        self.assertEqual(self.shown(first), [f"Note {n}" for n in range(11, 1, -1)])                 # the 10 newest, newest first
        second = self.client.get(URL, {"page": 2})
        self.assertEqual(self.shown(second), ["Note 1"])

    def test_the_pager_says_where_you_are_and_links_to_the_neighbours(self):
        self.make(25)
        html = self.client.get(URL).content.decode()
        self.assertEqual(html.count('id="messages-pager"'), 1)
        self.assertIn("Showing 1&ndash;10 of 25 messages", html)
        self.assertIn("Page 1 of 3", html)
        self.assertRegex(html, r'<a class="messages-pager__btn" id="messages-next" rel="next" href="\?page=2">')
        self.assertRegex(html, r'<span class="messages-pager__btn" id="messages-prev" aria-disabled="true">')      # nothing before page 1
        middle = self.client.get(URL, {"page": 2}).content.decode()
        self.assertIn("Showing 11&ndash;20 of 25 messages", middle)
        self.assertIn("Page 2 of 3", middle)
        self.assertIn('href="?page=1"', middle)
        self.assertIn('href="?page=3"', middle)
        last = self.client.get(URL, {"page": 3}).content.decode()
        self.assertIn("Showing 21&ndash;25 of 25 messages", last)
        self.assertRegex(last, r'<span class="messages-pager__btn" id="messages-next" aria-disabled="true">')       # nothing after the last page
        self.assertIn('data-paged="true"', last)
        self.assertIn(">Clear this page</button>", last)

    def test_every_message_is_on_exactly_one_page(self):
        self.make(25)
        seen = []
        for number in (1, 2, 3):
            seen += self.shown(self.client.get(URL, {"page": number}))
        self.assertEqual(sorted(seen), sorted(f"Note {n}" for n in range(1, 26)))
        self.assertEqual(len(set(seen)), 25)

    def test_a_page_number_that_makes_no_sense_never_breaks_the_page(self):
        self.make(25)
        for odd in ("abc", "0", "-4", "", "2.5x"):
            with self.subTest(page=odd):
                response = self.client.get(URL, {"page": odd})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.shown(response)[0], "Note 25")                        # not a number, zero, negative: page 1
        self.assertEqual(self.shown(self.client.get(URL, {"page": "99"})), [f"Note {n}" for n in range(5, 0, -1)])    # too big: the last page

    def test_someone_elses_messages_never_appear_or_count(self):
        self.make(3)
        self.make(30, user=self.other, prefix="Other")
        response = self.client.get(URL)
        self.assertEqual(len(self.shown(response)), 3)
        self.assertNotContains(response, "Other 1 body")
        self.assertNotContains(response, 'id="messages-pager"')

    def test_a_signed_out_visitor_is_sent_to_sign_in(self):
        self.client.logout()
        self.assertEqual(self.client.get(URL).status_code, 302)


class ReadingTheMessagesTests(PagesBase):
    def unread(self):
        return CustomerMessage.objects.filter(recipient=self.customer, is_read=False).count()

    def test_only_the_messages_on_the_page_are_marked_read(self):
        self.make(25)
        self.client.get(URL)
        self.assertEqual(self.unread(), 15)                                  # the 10 shown are read, the other 15 are still waiting
        self.client.get(URL, {"page": 2})
        self.assertEqual(self.unread(), 5)
        self.client.get(URL, {"page": 3})
        self.assertEqual(self.unread(), 0)

    def test_the_new_tag_is_only_on_the_messages_that_were_unread_when_shown(self):
        rows = self.make(12)
        CustomerMessage.objects.filter(pk=rows[-1].pk).update(is_read=True)                  # the newest was already read
        html = self.client.get(URL).content.decode()
        self.assertEqual(html.count('class="new-message-tag'), 9)             # 10 shown, 1 of them already read
        again = self.client.get(URL).content.decode()
        self.assertEqual(again.count('class="new-message-tag'), 0)            # they were marked read by the first visit

    def test_the_bell_count_stays_true_while_other_pages_are_unopened(self):
        self.make(25)
        self.client.get(URL)
        feed = self.client.get(reverse("gowns:notification_badges")).json()
        self.assertEqual(feed["messages"], 15)

    def test_a_returned_overdue_notice_on_a_later_page_still_reads_returned(self):
        today = timezone.localdate()
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Msg Cust", status=Reservation.Status.ACTIVE)
        ReservationItem.objects.create(
            reservation=reservation, gown_name="Wedding Gown 9", stage=ReservationItem.Stage.RETURNED, rental_date=today - timedelta(days=8),
            event_date=today - timedelta(days=6), return_date=today - timedelta(days=4), overdue_date=today - timedelta(days=4))
        old = CustomerMessage.objects.create(recipient=self.customer, category=CustomerMessage.Category.RETURN_OVERDUE, is_read=True,
                                             body=f"Wedding Gown 9 was due back and is now 4 days overdue. Reference: {reservation.reference_code}.")
        CustomerMessage.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=30))
        self.make(10)                                                       # ten newer messages push the notice onto page 2
        self.assertNotContains(self.client.get(URL), "overdue")
        second = self.client.get(URL, {"page": 2})
        self.assertContains(second, "so this overdue notice is closed")
        self.assertNotContains(second, "now 4 days overdue")


class ClearingTests(PagesBase):
    def delete(self, ids):
        return self.client.post(reverse("gowns:messages_delete"), data=json.dumps({"ids": ids}), content_type="application/json")

    def ids_on(self, response):
        return [int(i) for i in re.findall(r'data-message-id="(\d+)"', response.content.decode())]

    def test_clearing_the_page_that_was_shown_lets_the_next_messages_fill_it(self):
        self.make(25)
        ids = self.ids_on(self.client.get(URL))
        self.assertEqual(len(ids), 10)
        self.assertEqual(self.delete(ids).json(), {"success": True, "deleted": 10})
        again = self.client.get(URL)
        self.assertEqual(self.shown(again), [f"Note {n}" for n in range(15, 5, -1)])
        self.assertIn("Page 1 of 2", again.content.decode())

    def test_clearing_the_last_page_steps_back_to_the_page_before(self):
        self.make(13)
        self.client.get(URL, {"page": 1})
        self.delete(self.ids_on(self.client.get(URL, {"page": 2})))
        back = self.client.get(URL, {"page": 2})                              # the address still says page 2: it is the last page now
        self.assertEqual(len(self.shown(back)), 10)
        self.assertNotContains(back, 'id="messages-pager"')

    def test_a_message_that_was_never_shown_cannot_be_cleared(self):
        self.make(25)
        self.client.get(URL)                                                  # page 1 shown: only these are read
        hidden = list(CustomerMessage.objects.filter(recipient=self.customer, is_read=False).values_list("id", flat=True))
        self.assertEqual(self.delete(hidden).json()["deleted"], 0)
        self.assertEqual(CustomerMessage.objects.filter(recipient=self.customer).count(), 25)

    def test_the_page_reloads_after_a_delete_only_when_it_has_pages(self):
        self.make(11)
        html = self.client.get(URL).content.decode()
        self.assertIn("window.location.reload()", html)
        self.assertIn('list.dataset.paged === "true"', html)


class ReadableTypeTests(PagesBase):
    def test_the_message_text_dates_and_labels_are_bigger_than_before(self):
        self.make(2)
        html = self.client.get(URL).content.decode()
        self.assertIn(".message-body { font-size: 1rem;", html)                # 16px (it was 14px)
        self.assertIn(".message-label { font-size: 0.8125rem;", html)         # 13px bold (it was 12px)
        self.assertIn(".message-date { font-size: 0.875rem;", html)           # 14px (it was 12px)
        self.assertEqual(html.count('class="message-body max-w-xl"'), 2)
        self.assertEqual(html.count("message-date md:text-right"), 2)
        self.assertNotIn('class="max-w-xl text-sm leading-relaxed text-secondary"', html)      # the old 14px paragraph is gone

    def test_the_text_is_darker_than_the_old_faint_grey(self):
        html = self.client.get(URL).content.decode()
        self.assertIn("color: #2f2e2c", html)                                  # message text
        self.assertIn("color: #55534e", html)                                  # dates
        self.make(1)
        rows = self.client.get(URL).content.decode().split('id="messages-list"')[1].split("</main>")[0]
        self.assertNotIn("text-secondary/40", rows)                            # the old faint date / trash grey

    def test_the_trash_button_is_a_comfortable_tap_target(self):
        self.make(1)
        self.assertContains(self.client.get(URL), "js-delete-message -mr-2 -mt-1.5 flex h-10 w-10")

    def test_no_unrendered_template_code_leaks_into_the_page(self):
        self.make(12)
        for number in (1, 2):
            html = self.client.get(URL, {"page": number}).content.decode()
            for leak in ("{%", "{{", "{#"):
                self.assertNotIn(leak, html)


class QueryCountTests(PagesBase):
    def test_a_page_costs_the_same_number_of_queries_however_many_messages_there_are(self):
        self.make(5)
        with CaptureQueriesContext(connection) as few:
            self.client.get(URL)
        CustomerMessage.objects.filter(recipient=self.customer).update(is_read=True)
        self.make(60, prefix="Extra")
        with CaptureQueriesContext(connection) as many:
            self.client.get(URL)
        self.assertLessEqual(len(many), len(few) + 2)
