"""New Booking: walk-in bookings recorded by staff (arabela_admin/new_booking.py, reservations/staff_booking.py),
the shared "Check availability" answer (gowns.availability.check_unit), and how such a booking -- often with no website account --
then behaves everywhere else: Pending Approval, Active Reservations, Reservation Records, the reminders, the staff bell, the
customer emails, cancelling, and the customer website's calendar and checkout."""
import json
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomerEmailLog, CustomerMessage, UserProfile
from arabela_admin.new_booking import _calendar_why
from arabela_admin.notifications import build_items
from gowns import availability
from gowns.models import COOLDOWN_DAYS, Gown, GownUnavailability
from reservations import reminders, staff_booking
from reservations.models import ReceiptRecord, Reservation, ReservationItem, ReservationStatusEvent

User = get_user_model()
EMAILS_ON = dict(CUSTOMER_EMAILS_ENABLED=True, CUSTOMER_EMAILS_BACKGROUND=False)
_JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"


def jpeg(name="receipt.jpg", salt=b""):
    """A tiny JPEG; `salt` makes the bytes (and so the file fingerprint) different from any other test photo."""
    return SimpleUploadedFile(name, _JPEG_HEAD + salt + b"\xff\xd9", content_type="image/jpeg")


def make_gown(number, *, name="Walkin Gown", size=Gown.Size.MEDIUM, status=Gown.Status.AVAILABLE, price="3000"):
    return Gown.objects.create(
        gown_id=f"Wedding Gown-WH-{number:03d}", name=name, category=Gown.Category.WEDDING_GOWN, color_name="White",
        color_code="WH", size=size, rental_price=Decimal(price), status=status)


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("nb_staff", password="x", is_staff=True, first_name="Maria", last_name="Cruz")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"role": UserProfile.Role.STAFF})
        cls.owner = User.objects.create_superuser("nb_owner", "nb.owner@gmail.com", "x")
        cls.customer = User.objects.create_user("nb_customer", email="nb.customer@gmail.com", password="x")
        UserProfile.objects.update_or_create(user=cls.customer, defaults={"display_name": "Online Customer"})

    def setUp(self):
        self.today = timezone.localdate()
        self.event = self.today + timedelta(days=20)
        self.client.force_login(self.staff)
        saver = patch("arabela_admin.new_booking._save_receipt_photo", side_effect=lambda f: f"https://example.test/r/{f.name}")
        saver.start()
        self.addCleanup(saver.stop)
        saver2 = patch("arabela_admin.views._save_receipt_photo", side_effect=lambda f: f"https://example.test/r/{f.name}")
        saver2.start()
        self.addCleanup(saver2.stop)

    def post_booking(self, lines, *, payment="full", receipt=b"a", name="Lorna Reyes",
                     phone="0917 123 4567", channel="Walk-in", account=None, notebook="", note="", user=None):
        """`payment` is the form's choice ("full" = Paid in full, "deposit" = Deposit only); `receipt` is the photo's salt
        (None = no photo attached)."""
        if user:
            self.client.force_login(user)
        data = {
            "channel": channel, "name": name, "phone": phone, "address": "Antipolo", "notebook_number": notebook,
            "account": account.id if account else "", "lines": json.dumps(lines), "payment": payment, "note": note,
        }
        if receipt is not None:
            data["receipt_1"] = jpeg("receipt1.jpg", receipt)
        return self.client.post(reverse("arabela_admin:booking_create"), data=data)

    def legacy_pending_walk_in(self, number, receipts=()):
        """A walk-in recorded before "Not paid yet" went away: Pending, holding its gown, no receipt (or the given ones)."""
        gown = make_gown(number)
        reservation = Reservation.objects.create(
            customer=None, customer_name="Old Walkin", phone="09171234567", channel=Reservation.Channel.WALK_IN,
            recorded_by=self.staff, status=Reservation.Status.PENDING, rental_subtotal=Decimal("3000"),
            security_deposit=Decimal("2000"), total_amount=Decimal("5000"))
        ReservationItem.objects.create(
            reservation=reservation, gown=gown, gown_name=gown.name, size=gown.size, rental_price=gown.rental_price,
            rental_date=self.event - timedelta(days=2), return_date=self.event + timedelta(days=2))
        for name, covers in receipts:
            ReceiptRecord.objects.create(reservation=reservation, photo_url=f"https://example.test/r/{name}", covers=covers)
        return reservation, gown

    def upload(self, reservation, salt, *, covers=None, name="upload.jpg"):
        """The Reservation Records / Attach receipt upload; `covers` is sent only when given (the Records page never sends it)."""
        data = {"reference_code": reservation.reference_code, "photo": jpeg(name, salt)}
        if covers is not None:
            data["covers"] = covers
        return self.client.post(reverse("arabela_admin:receipt_upload"), data=data)

    def line(self, gown, event=None, takes_now=False, picked_up_on=""):
        return {"gown": gown.id, "event": (event or self.event).isoformat(), "takes_now": takes_now,
                "picked_up_on": picked_up_on}

    def online_booking(self, gown, pickup, ret, *, status=Reservation.Status.CONFIRMED, stage=ReservationItem.Stage.PICKUP):
        reservation = Reservation.objects.create(customer=self.customer, customer_name="Online Customer", status=status)
        item = ReservationItem.objects.create(
            reservation=reservation, gown=gown, gown_name=gown.name, size=gown.size, rental_date=pickup, return_date=ret,
            stage=stage)
        return reservation, item


# ======================================================================= the availability answer
class CheckUnitTests(Base):
    def test_a_free_gown_gets_the_same_plan_as_checkout(self):
        gown = make_gown(1)
        result = availability.check_unit(gown, self.event)
        self.assertTrue(result["ok"])
        self.assertEqual(result["pickup"], self.event - timedelta(days=2))
        self.assertEqual(result["return_date"], self.event + timedelta(days=2))
        self.assertEqual(result["rest_until"], self.event + timedelta(days=2 + COOLDOWN_DAYS))

    def test_a_booking_on_the_dates_is_named_with_its_number(self):
        gown = make_gown(2)
        reservation, _ = self.online_booking(gown, self.event - timedelta(days=1), self.event + timedelta(days=1))
        result = availability.check_unit(gown, self.event)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "booked")
        self.assertIn(reservation.reference_code, result["conflicts"][0]["label"])
        self.assertIn("Not available", availability.check_message(gown, result))

    def test_a_pending_booking_holds_the_gown_too(self):
        gown = make_gown(3)
        self.online_booking(gown, self.event, self.event + timedelta(days=2), status=Reservation.Status.PENDING)
        result = availability.check_unit(gown, self.event)
        self.assertFalse(result["ok"])
        self.assertIn("awaiting payment approval", result["conflicts"][0]["label"])

    def test_rest_days_running_into_the_next_booking_are_refused(self):
        gown = make_gown(4)
        self.online_booking(gown, self.event + timedelta(days=5), self.event + timedelta(days=9))
        result = availability.check_unit(gown, self.event)
        self.assertEqual(result["reason"], "rest")

    def test_a_staff_block_and_out_of_stock_are_refused(self):
        gown = make_gown(5)
        GownUnavailability.objects.create(gown=gown, start_date=self.event, end_date=self.event,
                                          reason=GownUnavailability.Reason.REPAIR)
        result = availability.check_unit(gown, self.event)
        self.assertEqual(result["reason"], "booked")
        self.assertIn("Blocked for repair", result["conflicts"][0]["label"])
        gone = make_gown(6, status=Gown.Status.OUT_OF_STOCK)
        self.assertEqual(availability.check_unit(gone, self.event)["reason"], availability.OUT_OF_STOCK)

    def test_past_dates_and_the_five_pm_rule(self):
        gown = make_gown(7)
        self.assertEqual(availability.check_unit(gown, self.today - timedelta(days=1))["reason"], availability.PAST)
        evening = timezone.localtime().replace(hour=18, minute=0)
        self.assertEqual(availability.check_unit(gown, self.today, now=evening)["reason"], availability.CLOSED)
        # ...but a customer standing at the counter can take it now, whatever the hour.
        taken = availability.check_unit(gown, self.today, picked_up_on=self.today, now=evening)
        self.assertTrue(taken["ok"])
        self.assertEqual(taken["pickup"], self.today)

    def test_taking_it_now_checks_every_day_from_today(self):
        gown = make_gown(8)
        self.online_booking(gown, self.today + timedelta(days=3), self.today + timedelta(days=5))
        # Planned, the event in 20 days is fine; taken today the gown would be out across that booking.
        self.assertTrue(availability.check_unit(gown, self.event)["ok"])
        self.assertEqual(availability.check_unit(gown, self.event, picked_up_on=self.today)["reason"], "booked")

    def test_only_the_owner_may_record_an_earlier_pick_up(self):
        gown = make_gown(9)
        earlier = self.today - timedelta(days=3)
        self.assertEqual(availability.check_unit(gown, self.today, picked_up_on=earlier)["reason"], availability.PAST)
        self.assertTrue(availability.check_unit(
            gown, self.today, picked_up_on=earlier, backdate_days=availability.OWNER_BACKDATE_DAYS)["ok"])
        self.assertEqual(availability.check_unit(gown, self.event, picked_up_on=self.today + timedelta(days=1))["reason"],
                         availability.PICKUP_IN_FUTURE)

    def test_the_next_free_date_is_after_the_booking_and_its_rest(self):
        gown = make_gown(10)
        _, item = self.online_booking(gown, self.event - timedelta(days=2), self.event + timedelta(days=2))
        GownUnavailability.objects.create(gown=gown, start_date=item.return_date + timedelta(days=1),
                                          end_date=item.return_date + timedelta(days=COOLDOWN_DAYS),
                                          reason=GownUnavailability.Reason.COOLDOWN, auto_for_item=item)
        nxt = availability.next_free_event(gown, self.event)
        self.assertEqual(nxt, self.event + timedelta(days=2 + COOLDOWN_DAYS + 1))
        self.assertTrue(availability.check_unit(gown, nxt)["ok"])


class AvailabilityEndpointTests(Base):
    def test_free_and_taken_answers_with_alternatives(self):
        gown = make_gown(11)
        sibling = make_gown(12, size=Gown.Size.LARGE)
        url = reverse("arabela_admin:availability_check")
        free = self.client.get(url, {"gown": gown.id, "event": self.event.isoformat()}).json()
        self.assertTrue(free["ok"])
        self.assertEqual(free["pickup"], (self.event - timedelta(days=2)).isoformat())
        self.online_booking(gown, self.event - timedelta(days=2), self.event + timedelta(days=2))
        taken = self.client.get(url, {"gown": gown.id, "event": self.event.isoformat()}).json()
        self.assertFalse(taken["ok"])
        self.assertTrue(taken["nextFree"])
        self.assertEqual([g["id"] for g in taken["alternatives"]], [sibling.id])

    def test_staff_cannot_ask_about_an_earlier_pick_up_but_the_owner_can(self):
        gown = make_gown(13)
        url = reverse("arabela_admin:availability_check")
        params = {"gown": gown.id, "event": self.today.isoformat(), "takes_now": "1",
                  "picked_up_on": (self.today - timedelta(days=2)).isoformat()}
        self.assertEqual(self.client.get(url, params).json()["pickup"], self.today.isoformat())   # staff: today
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url, params).json()["pickup"], params["picked_up_on"])

    def test_gown_search_finds_by_id_tag_number_and_name(self):
        gown = make_gown(14, name="Ivory Lace Gown")
        url = reverse("arabela_admin:booking_gown_search")
        for query in ("WH-014", "14", "ivory lace"):
            ids = [g["id"] for g in self.client.get(url, {"q": query}).json()["results"]]
            self.assertIn(gown.id, ids, query)

    def test_strangers_and_customers_get_nothing(self):
        gown = make_gown(15)
        self.client.logout()
        for name in ("availability_check", "booking_gown_search", "booking_customer_hint", "booking_account_search"):
            self.assertEqual(self.client.get(reverse(f"arabela_admin:{name}"), {"gown": gown.id, "q": "x"}).status_code, 401)
        self.assertEqual(self.client.post(reverse("arabela_admin:booking_create")).status_code, 401)
        self.client.force_login(self.customer)
        self.assertEqual(self.client.post(reverse("arabela_admin:booking_create")).status_code, 401)
        page = self.client.get(reverse("arabela_admin:new_booking"))
        self.assertEqual(page.status_code, 302)


# ======================================================================= saving a booking
class RecordBookingTests(Base):
    def test_a_paid_walk_in_with_no_account_is_saved_like_an_online_booking(self):
        gown = make_gown(20)
        response = self.post_booking([self.line(gown)])
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        reservation = Reservation.objects.get(reference_code=data["reference_code"])
        self.assertIsNone(reservation.customer)
        self.assertEqual(reservation.customer_name, "Lorna Reyes")
        self.assertEqual(reservation.phone, "09171234567")
        self.assertEqual(reservation.channel, Reservation.Channel.WALK_IN)
        self.assertEqual(reservation.recorded_by, self.staff)
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
        self.assertIsNotNone(reservation.reviewed_at)
        self.assertEqual(reservation.payment_method, "")          # how a walk-in paid is the shop's own business
        self.assertEqual(reservation.rental_subtotal, Decimal("3000"))
        self.assertEqual(reservation.security_deposit, Decimal("2000"))
        self.assertEqual(reservation.total_amount, Decimal("5000"))
        item = reservation.items.get()
        self.assertEqual((item.gown, item.stage), (gown, ReservationItem.Stage.PICKUP))
        self.assertEqual(item.rental_date, self.event - timedelta(days=2))
        self.assertEqual(item.return_date, self.event + timedelta(days=2))
        self.assertEqual(item.original_rental_date, item.rental_date)
        block = GownUnavailability.objects.get(auto_for_item=item)
        self.assertEqual((block.start_date, block.end_date),
                         (item.return_date + timedelta(days=1), item.return_date + timedelta(days=COOLDOWN_DAYS)))
        receipt = reservation.receipt_records.get()
        self.assertEqual((receipt.covers, receipt.uploaded_by), (ReceiptRecord.Covers.BOTH, self.staff))
        self.assertEqual(len(receipt.file_hash), 64)
        gown.refresh_from_db()
        self.assertEqual(gown.status, Gown.Status.RESERVED)
        labels = list(reservation.status_events.values_list("label", flat=True))
        self.assertIn(staff_booking.RECORDED_LABEL, labels)
        self.assertIn(staff_booking.APPROVED_LABEL, labels)
        note = reservation.status_events.get(label=staff_booking.STAFF_NOTE_LABEL)
        self.assertTrue(note.staff_only)
        self.assertIn("Maria Cruz", note.detail)
        self.assertIn("Paid in full.", note.detail)
        self.assertNotIn("Cash", note.detail)
        self.assertEqual(data["where"], "Active Reservations")
        self.assertFalse(data["depositOnly"])

    def test_the_booking_number_continues_the_same_series(self):
        online = Reservation.objects.create(customer=self.customer, customer_name="Online Customer")
        walk_in = Reservation.objects.get(reference_code=self.post_booking([self.line(make_gown(21))]).json()["reference_code"])
        after = Reservation.objects.create(customer=self.customer, customer_name="Online Customer")
        numbers = [int(r.reference_code.rsplit("-", 1)[1]) for r in (online, walk_in, after)]
        self.assertEqual(numbers, [numbers[0], numbers[0] + 1, numbers[0] + 2])
        self.assertEqual(walk_in.reference_code[:9], online.reference_code[:9])

    def test_deposit_only_is_confirmed_at_once_and_still_owes_its_rental_receipt(self):
        gown = make_gown(22)
        response = self.post_booking([self.line(gown)], payment="deposit", receipt=b"dep")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        reservation = Reservation.objects.get(reference_code=data["reference_code"])
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
        self.assertIsNotNone(reservation.reviewed_at)
        self.assertEqual((data["where"], data["depositOnly"]), ("Active Reservations", True))
        self.assertEqual(reservation.payment_method, "")
        self.assertEqual(reservation.total_amount, Decimal("5000"))      # the booking itself is the same either way
        receipt = reservation.receipt_records.get()
        self.assertEqual(receipt.covers, ReceiptRecord.Covers.DEPOSIT)    # the Payment choice labels it; staff are never asked
        gown.refresh_from_db()
        self.assertEqual(gown.status, Gown.Status.RESERVED)
        self.assertFalse(availability.check_unit(gown, self.event)["ok"])         # holds the gown like any booking
        note = reservation.status_events.get(label=staff_booking.STAFF_NOTE_LABEL)
        self.assertIn("Deposit paid; the rental is still to pay.", note.detail)
        self.assertEqual(reservation.status_events.get(label=staff_booking.APPROVED_LABEL).detail,
                         "Deposit paid at the shop. Your booking is confirmed.")
        self.assertFalse(reservation.receipts_cover_payment())
        self.assertEqual(reservation.next_receipt_covers(), ReceiptRecord.Covers.RENTAL)

    def test_paid_in_full_is_labelled_by_the_choice_and_owes_nothing(self):
        response = self.post_booking([self.line(make_gown(19))], payment="full", receipt=b"full")
        reservation = Reservation.objects.get(reference_code=response.json()["reference_code"])
        self.assertEqual(reservation.receipt_records.get().covers, ReceiptRecord.Covers.BOTH)
        self.assertTrue(reservation.receipts_cover_payment())
        records = self.client.get(reverse("arabela_admin:reservation_records"))
        record = next(r for r in records.context["records"] if r["reference"] == reservation.reference_code)
        self.assertFalse(record["paymentIncomplete"])

    def test_customer_takes_it_now(self):
        gown = make_gown(23)
        response = self.post_booking([self.line(gown, event=self.today + timedelta(days=4), takes_now=True)])
        self.assertEqual(response.status_code, 200, response.content)
        item = ReservationItem.objects.get(gown=gown)
        self.assertEqual(item.stage, ReservationItem.Stage.RESERVED)
        self.assertEqual((item.rental_date, item.picked_up_on), (self.today, self.today))
        self.assertTrue(item.reservation.status_events.filter(label=f"{gown.name} picked up", item=item).exists())

    def test_the_payment_rules(self):
        gown = make_gown(24)
        cases = [
            ("no payment chosen", dict(lines=[self.line(gown)], payment=""), "Paid in full"),
            ("an old Not paid yet", dict(lines=[self.line(gown)], payment="0"), "Deposit only"),
            ("a made-up choice", dict(lines=[self.line(gown)], payment="half"), "Deposit only"),
            ("taken now with only the deposit", dict(lines=[self.line(gown, takes_now=True)], payment="deposit"), "once the rental is paid"),
            ("paid in full with no receipt", dict(lines=[self.line(gown)], receipt=None), "receipt"),
            ("deposit only with no receipt", dict(lines=[self.line(gown)], payment="deposit", receipt=None), "receipt"),
        ]
        for label, kwargs, words in cases:
            response = self.post_booking(**kwargs)
            self.assertEqual(response.status_code, 400, label)
            self.assertIn(words, response.json()["error"], label)
        self.assertFalse(Reservation.objects.exists())
        self.assertFalse(ReceiptRecord.objects.exists())

    def test_the_old_form_fields_are_gone_and_a_second_photo_is_not_taken(self):
        gown = make_gown(18)
        data = {"name": "Lorna Reyes", "phone": "0917 123 4567", "lines": json.dumps([self.line(gown)]), "payment": "full",
                "paid": "0", "payment_method": "GCash", "receipt_1": jpeg("one.jpg", b"o1"), "receipt_1_covers": "Rental",
                "receipt_2": jpeg("two.jpg", b"o2"), "receipt_2_covers": "Deposit"}
        response = self.client.post(reverse("arabela_admin:booking_create"), data=data)
        self.assertEqual(response.status_code, 200, response.content)
        reservation = Reservation.objects.get(reference_code=response.json()["reference_code"])
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)         # `paid=0` means nothing any more
        self.assertEqual(reservation.payment_method, "")                              # nor does a payment method
        receipt = reservation.receipt_records.get()                                    # only the first photo is read
        self.assertEqual((receipt.photo_url, receipt.covers), ("https://example.test/r/one.jpg", ReceiptRecord.Covers.BOTH))
        self.assertEqual(staff_booking.MAX_RECEIPTS, 1)

    def test_a_gown_already_taken_is_refused_and_nothing_is_saved(self):
        gown = make_gown(25)
        online, _ = self.online_booking(gown, self.event - timedelta(days=1), self.event + timedelta(days=1))
        before = (Reservation.objects.count(), ReceiptRecord.objects.count(), GownUnavailability.objects.count())
        response = self.post_booking([self.line(gown)])
        self.assertEqual(response.status_code, 409)
        self.assertIn(online.reference_code, response.json()["error"])
        self.assertEqual((Reservation.objects.count(), ReceiptRecord.objects.count(), GownUnavailability.objects.count()), before)
        # The refused try used no booking number: the next booking still gets the very next one.
        nxt = Reservation.objects.create(customer=self.customer, customer_name="x")
        self.assertEqual(int(nxt.reference_code.rsplit("-", 1)[1]), int(online.reference_code.rsplit("-", 1)[1]) + 1)

    def test_one_booking_with_two_gowns_and_the_same_gown_twice(self):
        first, second = make_gown(26), make_gown(27)
        ok = self.post_booking([self.line(first), self.line(second)])
        self.assertEqual(ok.status_code, 200, ok.content)
        reservation = Reservation.objects.get(reference_code=ok.json()["reference_code"])
        self.assertEqual(reservation.items.count(), 2)
        self.assertEqual(reservation.security_deposit, Decimal("4000"))
        third = make_gown(28)
        twice = self.post_booking([self.line(third), self.line(third)], receipt=b"t")
        self.assertEqual(twice.status_code, 400)
        self.assertIn("twice", twice.json()["error"])

    def test_the_customer_fields_are_checked(self):
        gown = make_gown(29)
        for kwargs, words in ((dict(name=""), "name"), (dict(name="12345"), "real name"), (dict(phone="12345"), "mobile number"),
                              (dict(channel="Online"), "Only walk-in"), (dict(channel="Facebook"), "Only walk-in"),
                              (dict(channel="Phone"), "Only walk-in"), (dict(notebook="bad*no"), "notebook")):
            response = self.post_booking([self.line(gown)], **kwargs)
            self.assertEqual(response.status_code, 400, kwargs)
            self.assertIn(words, response.json()["error"], kwargs)
        good = self.post_booking([self.line(gown)], phone="+63 917 765 4321", notebook=" 0457 ")
        self.assertEqual(good.status_code, 200, good.content)
        reservation = Reservation.objects.get(reference_code=good.json()["reference_code"])
        self.assertEqual((reservation.phone, reservation.channel, reservation.notebook_number),
                         ("09177654321", Reservation.Channel.WALK_IN, "0457"))

    def test_a_notebook_number_and_a_receipt_photo_can_be_used_only_once(self):
        first = self.post_booking([self.line(make_gown(30))], notebook="A-12", receipt=b"same")
        self.assertEqual(first.status_code, 200, first.content)
        again = self.post_booking([self.line(make_gown(31))], notebook="a-12", receipt=b"other")
        self.assertEqual(again.status_code, 409)
        self.assertIn(first.json()["reference_code"], again.json()["error"])
        photo = self.post_booking([self.line(make_gown(32))], receipt=b"same")
        self.assertEqual(photo.status_code, 409)
        self.assertIn("already attached", photo.json()["error"])

    def test_only_the_owner_can_record_an_earlier_pick_up(self):
        gown = make_gown(34)
        earlier = (self.today - timedelta(days=2)).isoformat()
        staff = self.post_booking([self.line(gown, event=self.today + timedelta(days=1), takes_now=True, picked_up_on=earlier)])
        self.assertEqual(staff.status_code, 400)
        self.assertIn("Only the owner", staff.json()["error"])
        owner = self.post_booking([self.line(gown, event=self.today + timedelta(days=1), takes_now=True, picked_up_on=earlier)],
                                  user=self.owner)
        self.assertEqual(owner.status_code, 200, owner.content)
        self.assertEqual(ReservationItem.objects.get(gown=gown).picked_up_on.isoformat(), earlier)

    def test_linking_a_website_account(self):
        gown = make_gown(35)
        response = self.post_booking([self.line(gown)], account=self.customer)
        self.assertEqual(response.status_code, 200, response.content)
        reservation = Reservation.objects.get(reference_code=response.json()["reference_code"])
        self.assertEqual(reservation.customer, self.customer)
        self.client.force_login(self.customer)
        page = self.client.get(reverse("gowns:orders"))
        self.assertIn(reservation, list(page.context["reservations"]))      # on the customer's own Reservations page
        self.assertContains(page, gown.name)
        staff_account = self.post_booking([self.line(make_gown(36))], account=self.staff, receipt=b"s", user=self.staff)
        self.assertEqual(staff_account.status_code, 400)

    def test_the_customer_hint_and_account_search(self):
        self.post_booking([self.line(make_gown(37))], name="Lorna Reyes", phone="09171234567")
        hint = self.client.get(reverse("arabela_admin:booking_customer_hint"), {"name": "", "phone": "0917-123-4567"}).json()
        self.assertEqual(len(hint["results"]), 1)
        accounts = self.client.get(reverse("arabela_admin:booking_account_search"), {"q": "online"}).json()["results"]
        self.assertEqual([a["id"] for a in accounts], [self.customer.id])
        self.assertFalse(self.client.get(reverse("arabela_admin:booking_account_search"), {"q": "maria"}).json()["results"])


# ======================================================================= the rest of the system with a no-account booking
class EverywhereElseTests(Base):
    def walk_in(self, *, payment="full", event=None, takes_now=False, salt=b"w"):
        gown = make_gown(40 + Reservation.objects.count())
        response = self.post_booking([self.line(gown, event=event, takes_now=takes_now)], payment=payment, receipt=salt)
        self.assertEqual(response.status_code, 200, response.content)
        return Reservation.objects.get(reference_code=response.json()["reference_code"]), gown

    def test_the_customer_website_greys_the_date_and_checkout_refuses_it(self):
        reservation, gown = self.walk_in()
        page = self.client.get(reverse("gowns:product_detail", kwargs={"collection": "wedding", "slug": gown.slug}))
        plans = page.context["calendar_data"]["plans"]["M"]
        for day in (self.event, self.event + timedelta(days=2 + COOLDOWN_DAYS)):
            self.assertIn(plans[day.isoformat()], ("booked", "rest"), day)
        self.client.force_login(self.customer)
        items = [{"gown_name": gown.name, "gown_slug": gown.slug, "size": "M",
                  "rental_date": (self.event - timedelta(days=2)).isoformat(), "return_date": (self.event + timedelta(days=2)).isoformat()}]
        with patch("gowns.views._save_proof_file", return_value="https://example.test/proof.jpg"):
            response = self.client.post(reverse("gowns:reservation_submit"), data={
                "items": json.dumps(items), "first_name": "Online", "last_name": "Customer", "phone": "09170000000",
                "address": "x", "city": "x", "postal_code": "1000", "payment_method": "GCash",
                "proof_of_payment": jpeg("proof.jpg", b"proof")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("no longer available", response.json()["error"])

    def test_an_old_not_paid_yet_walk_in_still_works_in_pending_approval(self):
        """Walk-ins recorded before "Not paid yet" went away may still be waiting on live: Attach receipt, then Approve."""
        reservation, gown = self.legacy_pending_walk_in(60)
        page = self.client.get(reverse("arabela_admin:pending_approval")).content.decode()
        self.assertIn(reservation.reference_code, page)
        self.assertIn("Attach receipt", page)
        self.assertIn("Not paid yet", page)
        self.assertIn('<option value="Rental + deposit">Paid in full</option>', page)
        self.assertIn('<option value="Deposit">Deposit only</option>', page)
        upload = self.upload(reservation, b"paid", covers="Rental + deposit")
        self.assertEqual(upload.status_code, 200, upload.content)
        self.assertEqual(upload.json()["receipt"]["covers"], ReceiptRecord.Covers.BOTH)
        approve = self.client.post(reverse("arabela_admin:reservation_approve", args=[reservation.id]))
        self.assertEqual(approve.status_code, 200, approve.content)
        reservation.refresh_from_db()
        gown.refresh_from_db()
        self.assertEqual((reservation.status, gown.status), (Reservation.Status.CONFIRMED, Gown.Status.RESERVED))
        self.assertFalse(CustomerMessage.objects.exists())

    def test_an_old_pending_walk_in_that_only_paid_the_deposit_keeps_that_answer(self):
        reservation, _ = self.legacy_pending_walk_in(61)
        self.assertEqual(self.upload(reservation, b"dep61", covers="Deposit").json()["receipt"]["covers"], ReceiptRecord.Covers.DEPOSIT)
        self.client.post(reverse("arabela_admin:reservation_approve", args=[reservation.id]))
        records = self.client.get(reverse("arabela_admin:reservation_records"))
        record = next(r for r in records.context["records"] if r["reference"] == reservation.reference_code)
        self.assertTrue(record["paymentIncomplete"])                  # approved, but the rental receipt is still to come

    def test_active_reservations_records_and_the_calendar_show_it(self):
        reservation, _ = self.walk_in()
        active = self.client.get(reverse("arabela_admin:active_reservations")).content.decode()
        self.assertIn(reservation.reference_code, active)
        self.assertIn("Walk-in", active)
        self.assertIn('href="tel:09171234567"', active)
        records = self.client.get(reverse("arabela_admin:reservation_records"))
        record = next(r for r in records.context["records"] if r["reference"] == reservation.reference_code)
        self.assertEqual((record["channel"], record["recordedBy"], record["hasAccount"]), ("Walk-in", "Maria Cruz", False))
        self.assertFalse(record["paymentIncomplete"])
        self.assertEqual(record["email"], "")
        calendar = self.client.get(reverse("arabela_admin:rental_schedule"))
        self.assertTrue(any(e.get("extendedProps", e).get("reference") == reservation.reference_code
                            for e in calendar.context["calendar_events"]))
        dashboard = self.client.get(reverse("arabela_admin:dashboard"))
        self.assertEqual(dashboard.context["active_rentals"], 1)

    def test_a_deposit_only_booking_is_payment_incomplete_until_the_rental_receipt_is_uploaded(self):
        reservation, _ = self.walk_in(payment="deposit")

        def record():
            records = self.client.get(reverse("arabela_admin:reservation_records"))
            return next(r for r in records.context["records"] if r["reference"] == reservation.reference_code)

        self.assertTrue(record()["paymentIncomplete"])
        upload = self.upload(reservation, b"rental-later")                 # the Records page sends no label at all
        self.assertEqual(upload.status_code, 200, upload.content)
        self.assertEqual((upload.json()["receipt"]["covers"], upload.json()["receipt"]["coversName"]), ("Rental", "Rental"))
        self.assertFalse(record()["paymentIncomplete"])                     # Deposit + Rental = paid in full
        extra = self.upload(reservation, b"another-copy")                    # anything after that is another copy of the full payment
        self.assertEqual(extra.json()["receipt"]["covers"], ReceiptRecord.Covers.BOTH)
        self.assertFalse(record()["paymentIncomplete"])

    def test_pick_up_return_and_the_deposit_with_no_account(self):
        reservation, gown = self.walk_in(event=self.today, takes_now=True)
        item = reservation.items.get()
        response = self.client.post(reverse("arabela_admin:reservation_item_mark_returned", args=[item.id]),
                                    data=json.dumps({"condition": "Good"}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        item.refresh_from_db()
        self.assertEqual(item.stage, ReservationItem.Stage.RETURNED)
        self.assertTrue(reservation.status_events.filter(label="Security deposit settled").exists())
        self.assertFalse(CustomerMessage.objects.exists())

    def test_mark_picked_up_on_the_day(self):
        # Booked in the morning (before the shop's 5 PM pick-up cut-off), so the pick-up is planned for today.
        morning = timezone.localtime().replace(hour=10, minute=0, second=0, microsecond=0)
        with patch("gowns.availability.now_local", return_value=morning):
            reservation, _ = self.walk_in(event=self.today + timedelta(days=2))
        item = reservation.items.get()
        self.assertEqual(item.rental_date, self.today)
        response = self.client.post(reverse("arabela_admin:reservation_item_mark_picked_up", args=[item.id]))
        self.assertEqual(response.status_code, 200, response.content)

    def test_cancelling_a_no_show_with_no_account(self):
        reservation, gown = self.walk_in()
        self.client.force_login(self.owner)           # cancelling a confirmed booking is for a Manager or the owner
        response = self.client.post(reverse("arabela_admin:reservation_cancel", args=[reservation.id]), data=json.dumps(
            {"reason": "no_show", "note": "", "confirm": reservation.reference_code}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CANCELLED)
        self.assertFalse(GownUnavailability.objects.filter(auto_for_item__reservation=reservation).exists())
        self.assertFalse(CustomerMessage.objects.exists())
        self.assertTrue(availability.check_unit(gown, self.event)["ok"])

    def test_reminders_skip_it_and_remind_says_call(self):
        reservation, _ = self.walk_in(event=self.today + timedelta(days=3))     # pick-up tomorrow
        self.assertEqual(reminders.find_due_reminders(), [])
        item = reservation.items.get()
        response = self.client.post(reverse("arabela_admin:reservation_item_send_reminder", args=[item.id]),
                                    data=json.dumps({"body": "Please pick up tomorrow."}), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("09171234567", response.json()["error"])
        self.assertFalse(CustomerMessage.objects.exists())

    def test_the_staff_bell_says_who_to_call(self):
        reservation, _ = self.walk_in(event=self.today + timedelta(days=3))     # pick-up tomorrow
        pending, _ = self.legacy_pending_walk_in(62)
        items = build_items(self.staff)
        calls = [i for i in items if i["kind"] == "call"]
        self.assertEqual(len(calls), 1)
        self.assertIn("09171234567", calls[0]["actor"])
        self.assertEqual(calls[0]["subject"], reservation.reference_code)
        waiting = next(i for i in items if i["key"] == f"reservation:{pending.reference_code}")
        self.assertIn("walk-in booking waiting for payment", waiting["text"])

    @override_settings(**EMAILS_ON)
    def test_no_email_is_attempted_for_a_booking_with_no_account(self):
        with self.captureOnCommitCallbacks(execute=True):
            reservation, _ = self.walk_in()
        self.assertEqual(mail.outbox, [])
        self.assertFalse(CustomerEmailLog.objects.exists())

    @override_settings(**EMAILS_ON)
    def test_a_linked_account_gets_the_approval_email(self):
        gown = make_gown(90)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post_booking([self.line(gown)], account=self.customer, receipt=b"le")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["nb.customer@gmail.com"])


class PagesTests(Base):
    def test_the_new_booking_page_and_the_sidebar(self):
        page = self.client.get(reverse("arabela_admin:new_booking"))
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertIn('x-data="newBooking()"', html)
        self.assertIn("Customer takes the gown now", html)
        self.assertIn("nb-payment", html)
        self.assertNotIn("Picked up on", html)                     # owner only
        self.client.force_login(self.owner)
        self.assertIn("Picked up on", self.client.get(reverse("arabela_admin:new_booking")).content.decode())
        for name in ("dashboard", "active_reservations", "pending_approval", "gown_catalog", "reservation_records"):
            html = self.client.get(reverse(f"arabela_admin:{name}")).content.decode()
            self.assertIn(reverse("arabela_admin:new_booking"), html, name)

    def test_gown_catalog_has_the_check(self):
        html = self.client.get(reverse("arabela_admin:gown_catalog")).content.decode()
        self.assertIn("Check a date", html)
        self.assertIn("arabelaAvailability.inline", html)


class WalkInOnlyTests(Base):
    def test_the_form_has_no_facebook_or_phone_choice(self):
        html = self.client.get(reverse("arabela_admin:new_booking")).content.decode()
        self.assertNotIn("How did the booking come in", html)
        self.assertNotIn('name="nb-channel"', html)
        self.assertNotIn("Phone call", html)
        self.assertNotIn("a Facebook message", html)
        self.assertIn("walked into the shop", html)

    def test_a_booking_sent_without_a_channel_is_a_walk_in(self):
        gown = make_gown(120)
        data = {"name": "Lorna Reyes", "phone": "0917 123 4567", "address": "Antipolo", "lines": json.dumps([self.line(gown)]),
                "payment": "full", "note": "", "receipt_1": jpeg("only.jpg", b"w1")}
        response = self.client.post(reverse("arabela_admin:booking_create"), data=data)
        self.assertEqual(response.status_code, 200, response.content)
        reservation = Reservation.objects.get(reference_code=response.json()["reference_code"])
        self.assertEqual(reservation.channel, Reservation.Channel.WALK_IN)

    def test_facebook_phone_and_online_bookings_are_refused_and_nothing_is_saved(self):
        gown = make_gown(121)
        for channel in ("Facebook", "Phone", "Online"):
            response = self.post_booking([self.line(gown)], channel=channel, receipt=channel.encode())
            self.assertEqual(response.status_code, 400, channel)
            self.assertIn("Only walk-in", response.json()["error"], channel)
        self.assertFalse(Reservation.objects.exists())
        self.assertFalse(ReceiptRecord.objects.exists())


class ReceiptPhotosInProofPopupsTests(Base):
    """A booking recorded at the counter has no customer GCash screenshot: the shop's receipt photos are its proof."""

    def paid_booking(self, number, payment="full", salt=b"p", **kwargs):
        response = self.post_booking([self.line(make_gown(number))], payment=payment, receipt=salt, **kwargs)
        self.assertEqual(response.status_code, 200, response.content)
        return Reservation.objects.get(reference_code=response.json()["reference_code"])

    def deposit_then_rental(self, number, salt):
        """A deposit-only walk-in (receipt1.jpg = Deposit) whose rental receipt was uploaded later (receipt2.jpg = Rental)."""
        reservation = self.paid_booking(number, payment="deposit", salt=salt)
        later = self.upload(reservation, salt + b"-rental", name="receipt2.jpg")
        self.assertEqual(later.status_code, 200, later.content)
        return reservation

    def test_the_receipts_as_the_popups_read_them(self):
        reservation = self.deposit_then_rental(130, b"p1")
        ReceiptRecord.objects.create(reservation=reservation, photo_url="https://example.test/r/nolabel.jpg", covers="")
        everything = reservation.receipt_proofs()
        self.assertEqual([row["label"] for row in everything], ["Deposit receipt", "Rental receipt", "Receipt (not labelled)"])
        self.assertEqual(everything[0]["url"], "https://example.test/r/receipt1.jpg")
        self.assertIn("·", everything[0]["when"])
        deposit = reservation.receipt_proofs(deposit_only=True)
        self.assertEqual([row["label"] for row in deposit], ["Deposit receipt"])      # a Rental-only or unlabelled receipt says nothing about it
        self.assertEqual(json.loads(reservation.receipts_json), everything)
        self.assertEqual(json.loads(reservation.deposit_receipts_json), deposit)

    def test_one_receipt_for_both_counts_for_the_deposit_and_reads_paid_in_full(self):
        reservation = self.paid_booking(131, salt=b"p3")
        self.assertEqual([row["label"] for row in reservation.receipt_proofs(deposit_only=True)], ["Paid in full receipt"])

    def test_an_online_bookings_unlabelled_receipt_is_the_full_one_but_a_walk_ins_says_nothing(self):
        online, _ = self.online_booking(make_gown(139), self.event - timedelta(days=2), self.event + timedelta(days=2))
        ReceiptRecord.objects.create(reservation=online, photo_url="https://example.test/r/old-online.jpg", covers="")
        self.assertEqual([row["label"] for row in online.receipt_proofs()], ["Paid in full receipt"])
        self.assertEqual([row["label"] for row in online.receipt_proofs(deposit_only=True)], ["Paid in full receipt"])
        walk_in = self.paid_booking(140, salt=b"p11")
        ReceiptRecord.objects.filter(reservation=walk_in).update(covers="")
        walk_in = Reservation.objects.get(id=walk_in.id)
        self.assertEqual([row["label"] for row in walk_in.receipt_proofs()], ["Receipt (not labelled)"])
        self.assertEqual(walk_in.receipt_proofs(deposit_only=True), [])

    def test_a_booking_with_no_receipts_has_none(self):
        reservation, _ = self.legacy_pending_walk_in(132)
        self.assertEqual(reservation.receipt_proofs(), [])
        self.assertEqual(reservation.receipts_json, "[]")
        self.assertEqual(reservation.deposit_receipts_json, "[]")

    def test_security_deposits_shows_only_the_deposit_receipts(self):
        self.deposit_then_rental(133, b"p4")
        html = self.client.get(reverse("arabela_admin:security_deposits")).content.decode()
        self.assertIn("receipt1.jpg", html)                       # the Deposit one
        self.assertNotIn("receipt2.jpg", html)                    # the Rental one is not about the deposit
        self.assertIn("!viewingPayment.proofUrl && !(viewingPayment.receipts || []).length", html)

    def test_security_deposits_shows_nothing_for_a_rental_only_receipt(self):
        reservation = Reservation.objects.create(customer=None, customer_name="Walk In", phone="09171234567",
                                                 channel=Reservation.Channel.WALK_IN, status=Reservation.Status.CONFIRMED)
        ReservationItem.objects.create(reservation=reservation, gown=make_gown(134), gown_name="Walkin Gown", size="Medium",
                                       rental_date=self.event, return_date=self.event + timedelta(days=4))
        ReceiptRecord.objects.create(reservation=reservation, photo_url="https://example.test/r/rentalonly.jpg", covers="Rental")
        html = self.client.get(reverse("arabela_admin:security_deposits")).content.decode()
        self.assertIn(reservation.reference_code.replace("-", "\\u002D"), html)    # the booking is on the page (escapejs writes "-" as -) ...
        self.assertNotIn("rentalonly.jpg", html)                  # ... but its Rental receipt is not offered as deposit proof

    def test_the_other_pages_show_every_receipt(self):
        self.deposit_then_rental(135, b"p7")
        self.legacy_pending_walk_in(136, receipts=(("pending9.jpg", "Rental"),))
        for name, popup_var, expected in (("payment_verification", "viewingPayment", ("receipt1.jpg", "receipt2.jpg")),
                                          ("rental_history", "viewingRental", ("receipt1.jpg", "receipt2.jpg")),
                                          ("dashboard", "receiptPreview", ("pending9.jpg",))):      # the dashboard lists the Pending one
            html = self.client.get(reverse(f"arabela_admin:{name}")).content.decode()
            for piece in expected:
                self.assertIn(piece, html, (name, piece))
            self.assertIn(f"!{popup_var}.proofUrl && !({popup_var}.receipts || []).length", html, name)

    def test_payment_verification_says_a_shop_receipt_is_attached(self):
        self.paid_booking(137, salt=b"p10")
        html = self.client.get(reverse("arabela_admin:payment_verification")).content.decode()
        self.assertIn("Shop receipt attached", html)
        self.assertNotIn("No proof uploaded", html)

    def test_an_online_booking_without_proof_still_says_so(self):
        self.online_booking(make_gown(138), self.event - timedelta(days=2), self.event + timedelta(days=2),
                            status=Reservation.Status.PENDING)
        html = self.client.get(reverse("arabela_admin:payment_verification")).content.decode()
        self.assertIn("No proof uploaded", html)
        self.assertNotIn("Shop receipt attached", html)

    def test_every_receipt_window_fits_the_screen_scrolls_inside_and_opens_at_the_top(self):
        """Several tall receipt photos made the centred window taller than the screen, so its top (the first photo) could not be
        scrolled to. The limit is inside the :style string (Alpine replaces the whole style attribute with it), for both widths."""
        self.deposit_then_rental(180, b"w1")
        for name, flag, var in (("payment_verification", "openReceiptModal", "viewingPayment"),
                                ("security_deposits", "openReceiptModal", "viewingPayment"),
                                ("rental_history", "openProofModal", "viewingRental"),
                                ("dashboard", "openReceiptModal", "receiptPreview")):
            with self.subTest(page=name):
                html = self.client.get(reverse(f"arabela_admin:{name}")).content.decode()
                self.assertEqual(html.count("max-width: 700px; max-height: calc(100vh - 2.5rem); max-height: calc(100dvh - 2.5rem);"), 1)
                self.assertEqual(html.count("max-width: min(1100px, 92vw); max-height: calc(100vh - 2.5rem); max-height: calc(100dvh - 2.5rem);"), 1)
                self.assertIn(f'<div @click.outside="{flag} = false" x-effect="if ({flag}) {{ $nextTick(function () {{ $el.scrollTop = 0; }}); }}"', html)
                self.assertNotIn('style="max-height: calc(100vh - 2.5rem)', html)           # a static style="" would be wiped by :style
                self.assertIn(f"{var}.proofUrl || ({var}.receipts || []).length", html)         # the width rule itself is as before

    def test_the_pages_do_not_ask_the_database_once_per_booking(self):
        numbers = iter(range(200, 400))

        def add(count):
            for _ in range(count):
                number = next(numbers)
                self.paid_booking(number, salt=f"q{number}".encode())

        def queries(name):
            with CaptureQueriesContext(connection) as captured:
                self.assertEqual(self.client.get(reverse(f"arabela_admin:{name}")).status_code, 200)
            return len(captured.captured_queries)

        pages = ("payment_verification", "security_deposits", "rental_history", "dashboard")
        add(3)
        few = {name: queries(name) for name in pages}
        add(4)
        for name in pages:
            self.assertLessEqual(queries(name), few[name] + 1, name)


class EventDateCalendarTests(Base):
    """The month calendar under New Booking's Event date box: every day is judged by check_unit itself."""

    def url(self, gown, month, **extra):
        query = {"gown": gown.id, "month": month}
        query.update(extra)
        return reverse("arabela_admin:availability_calendar") + "?" + "&".join(f"{k}={v}" for k, v in query.items())

    def busy_gown(self, number):
        """A gown with every kind of thing that can be in the way, spread over the next three months."""
        gown = make_gown(number)
        self.online_booking(gown, self.today + timedelta(days=5), self.today + timedelta(days=9))                     # confirmed
        self.online_booking(gown, self.today + timedelta(days=20), self.today + timedelta(days=24),
                            status=Reservation.Status.PENDING)                                                         # awaiting approval
        GownUnavailability.objects.create(gown=gown, start_date=self.today + timedelta(days=30),
                                          end_date=self.today + timedelta(days=32), reason=GownUnavailability.Reason.REPAIR)
        GownUnavailability.objects.create(gown=gown, start_date=self.today + timedelta(days=50),
                                          end_date=self.today + timedelta(days=54), reason=GownUnavailability.Reason.COOLDOWN)
        self.online_booking(gown, self.today + timedelta(days=70), self.today + timedelta(days=74))                   # rest days run into it
        return gown

    def months(self):
        first = self.today.replace(day=1)
        out = []
        for _ in range(4):
            out.append(first.strftime("%Y-%m"))
            first = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        return out

    def assert_same_as_check(self, gown, *, now, takes_now=False, picked_up_on=None, owner=False):
        extra = {}
        if takes_now:
            extra["takes_now"] = 1
            if picked_up_on:
                extra["picked_up_on"] = picked_up_on.isoformat()
        backdate = availability.OWNER_BACKDATE_DAYS if owner else 0
        seen, reasons = 0, set()
        for month in self.months():
            with patch("gowns.availability.now_local", return_value=now):
                response = self.client.get(self.url(gown, month, **extra))
            self.assertEqual(response.status_code, 200, (month, response.content))
            payload = response.json()
            self.assertEqual(payload["today"], now.date().isoformat())
            for iso, day in payload["days"].items():
                expected = availability.check_unit(gown, datetime.strptime(iso, "%Y-%m-%d").date(), now=now, backdate_days=backdate,
                                                   picked_up_on=picked_up_on if takes_now else None)
                self.assertEqual(day["ok"], expected["ok"], (iso, day, expected["reason"]))
                if expected["ok"]:
                    self.assertEqual((day["pickup"], day["returnDate"], day["restUntil"]),
                                     (expected["pickup"].isoformat(), expected["return_date"].isoformat(), expected["rest_until"].isoformat()), iso)
                else:
                    self.assertEqual((day["reason"], day["why"]), (expected["reason"], _calendar_why(expected)), iso)
                reasons.add("free" if expected["ok"] else expected["reason"])
                seen += 1
        self.assertGreaterEqual(seen, 28 * 4)
        return reasons

    def test_every_day_is_the_same_answer_as_the_check_after_a_date_is_chosen(self):
        gown = self.busy_gown(300)
        morning = timezone.localtime().replace(hour=10, minute=0, second=0, microsecond=0)
        reasons = self.assert_same_as_check(gown, now=morning)
        self.assertTrue({"free", "booked", "rest", "past"} <= reasons, reasons)      # the month really has every kind of day in it

    def test_the_same_when_the_customer_takes_the_gown_now(self):
        gown = self.busy_gown(301)
        morning = timezone.localtime().replace(hour=10, minute=0, second=0, microsecond=0)
        reasons = self.assert_same_as_check(gown, now=morning, takes_now=True, picked_up_on=self.today)
        self.assertTrue({"booked", "rest", "past"} <= reasons, reasons)        # leaving today, a booking days away blocks everything after it
        clean = make_gown(310)
        self.assertIn("free", self.assert_same_as_check(clean, now=morning, takes_now=True, picked_up_on=self.today))

    def test_the_same_after_five_pm_and_taking_it_now_then(self):
        gown = self.busy_gown(302)
        evening = timezone.localtime().replace(hour=18, minute=0, second=0, microsecond=0)
        self.assert_same_as_check(gown, now=evening)
        self.assert_same_as_check(gown, now=evening, takes_now=True, picked_up_on=self.today)
        with patch("gowns.availability.now_local", return_value=evening):
            days = self.client.get(self.url(gown, self.today.strftime("%Y-%m"))).json()["days"]
        self.assertEqual(days[self.today.isoformat()]["reason"], availability.CLOSED)
        self.assertIn("5:00 PM", days[self.today.isoformat()]["why"])

    def test_the_owner_may_look_back_and_record_an_earlier_pick_up(self):
        gown = self.busy_gown(303)
        self.client.force_login(self.owner)
        morning = timezone.localtime().replace(hour=10, minute=0, second=0, microsecond=0)
        self.assert_same_as_check(gown, now=morning, takes_now=True, picked_up_on=self.today - timedelta(days=3), owner=True)

    def test_a_staff_member_cannot_ask_for_an_earlier_pick_up_day(self):
        gown = self.busy_gown(304)
        earlier = (self.today - timedelta(days=3)).isoformat()
        month = self.today.strftime("%Y-%m")
        staff_view = self.client.get(self.url(gown, month, takes_now=1, picked_up_on=earlier)).json()["days"]
        today_view = self.client.get(self.url(gown, month, takes_now=1)).json()["days"]
        self.assertEqual(staff_view, today_view)

    def test_what_is_in_the_way_is_named(self):
        gown = make_gown(305)
        reservation, _ = self.online_booking(gown, self.today + timedelta(days=5), self.today + timedelta(days=9))
        day = self.today + timedelta(days=6)
        days = self.client.get(self.url(gown, day.strftime("%Y-%m"))).json()["days"]
        why = days[day.isoformat()]["why"]
        self.assertIn(reservation.reference_code, why)
        self.assertIn("Online Customer", why)

    def test_an_out_of_stock_gown_has_no_free_day(self):
        gown = make_gown(306, status=Gown.Status.OUT_OF_STOCK)
        days = self.client.get(self.url(gown, (self.today + timedelta(days=40)).strftime("%Y-%m"))).json()["days"]
        self.assertTrue(days and all(not d["ok"] and d["reason"] == availability.OUT_OF_STOCK and "Out-of-Stock" in d["why"] for d in days.values()))

    def test_a_booking_made_in_new_booking_shows_on_the_calendar_straight_away(self):
        gown = make_gown(307)
        month = self.event.strftime("%Y-%m")
        self.assertTrue(self.client.get(self.url(gown, month)).json()["days"][self.event.isoformat()]["ok"])
        saved = self.post_booking([self.line(gown)], receipt=b"cal1")
        self.assertEqual(saved.status_code, 200, saved.content)
        day = self.client.get(self.url(gown, month)).json()["days"][self.event.isoformat()]
        self.assertFalse(day["ok"])
        self.assertIn(saved.json()["reference_code"], day["why"])

    def test_who_may_ask_and_what_is_refused(self):
        gown = make_gown(308)
        month = self.today.strftime("%Y-%m")
        self.assertEqual(self.client.get(self.url(gown, month)).status_code, 200)
        self.assertEqual(self.client.get(reverse("arabela_admin:availability_calendar") + f"?gown=999999&month={month}").status_code, 404)
        last_month = (self.today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        for bad in ("", "abc", "2026-13", "2026-00", "2020-01", "2100-01", last_month):
            response = self.client.get(self.url(gown, bad))
            self.assertEqual(response.status_code, 400, bad)
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(self.url(gown, month)).status_code, 401)
        self.client.logout()
        self.assertEqual(self.client.get(self.url(gown, month)).status_code, 401)
        self.client.force_login(self.owner)           # the owner may page back as far as the month 30 days ago
        back = (self.today - timedelta(days=availability.OWNER_BACKDATE_DAYS)).strftime("%Y-%m")
        self.assertEqual(self.client.get(self.url(gown, back)).status_code, 200)

    def test_nothing_is_written_and_the_cost_does_not_grow_with_the_bookings(self):
        gown = make_gown(309)
        month = self.event.strftime("%Y-%m")
        with CaptureQueriesContext(connection) as few:
            self.assertEqual(self.client.get(self.url(gown, month)).status_code, 200)
        for offset in range(3, 40, 3):
            self.online_booking(gown, self.today + timedelta(days=offset), self.today + timedelta(days=offset + 1))
        counts = (Reservation.objects.count(), ReservationItem.objects.count(), GownUnavailability.objects.count())
        with CaptureQueriesContext(connection) as many:
            self.assertEqual(self.client.get(self.url(gown, month)).status_code, 200)
        self.assertLessEqual(len(many.captured_queries), len(few.captured_queries) + 2)
        self.assertEqual(counts, (Reservation.objects.count(), ReservationItem.objects.count(), GownUnavailability.objects.count()))

    def test_the_page_has_the_calendar_and_no_longer_a_typed_date_or_a_check_button(self):
        html = self.client.get(reverse("arabela_admin:new_booking")).content.decode()
        self.assertIn("availability-calendar.js", html)
        self.assertIn(reverse("arabela_admin:availability_calendar"), html)
        self.assertIn('type="text" readonly inputmode="none"', html)
        self.assertNotIn(">Check availability<", html)
        self.assertNotIn('type="date" :min="minEvent(line)"', html)


class PaymentChoiceAndReceiptLabelTests(Base):
    """New Booking's Payment question (Paid in full / Deposit only) and what a receipt uploaded later is for."""

    def test_the_form_asks_only_paid_in_full_or_deposit_only(self):
        html = self.client.get(reverse("arabela_admin:new_booking")).content.decode()
        self.assertIn('name="nb-payment" value="full"', html)
        self.assertIn('name="nb-payment" value="deposit"', html)
        self.assertIn("Paid in full</label>", html)
        self.assertIn("Deposit only</label>", html)
        for gone in ("Not paid yet", "Paid by", 'name="nb-method"', "This receipt is for", "Rental only", "Cash</label>",
                     "GCash</label>", "payment_method"):
            self.assertNotIn(gone, html, gone)
        self.assertIn("form.append('payment', this.payment)", html)
        self.assertEqual(html.count("name=\"nb-payment\""), 2)

    def test_an_online_bookings_receipt_is_always_the_full_one_whatever_is_sent(self):
        online, _ = self.online_booking(make_gown(400), self.event - timedelta(days=2), self.event + timedelta(days=2))
        for salt, sent in ((b"o1", None), (b"o2", "Rental"), (b"o3", "Deposit"), (b"o4", "")):
            response = self.upload(online, salt, covers=sent)
            self.assertEqual(response.status_code, 200, response.content)
            receipt = response.json()["receipt"]
            self.assertEqual((receipt["covers"], receipt["coversName"]), (ReceiptRecord.Covers.BOTH, "Paid in full"), sent)
        self.assertEqual(set(online.receipt_records.values_list("covers", flat=True)), {ReceiptRecord.Covers.BOTH})

    def test_a_walk_ins_receipt_is_what_its_payment_is_missing_whatever_is_sent(self):
        deposit = Reservation.objects.get(reference_code=self.post_booking(
            [self.line(make_gown(401))], payment="deposit", receipt=b"d401").json()["reference_code"])
        first = self.upload(deposit, b"r401", covers="Deposit")            # tampered: still the missing one
        self.assertEqual(first.json()["receipt"]["covers"], ReceiptRecord.Covers.RENTAL)
        full = Reservation.objects.get(reference_code=self.post_booking(
            [self.line(make_gown(402))], payment="full", receipt=b"f402").json()["reference_code"])
        again = self.upload(full, b"x402", covers="Rental")
        self.assertEqual(again.json()["receipt"]["covers"], ReceiptRecord.Covers.BOTH)

    def test_next_receipt_covers_for_every_state(self):
        covers = ReceiptRecord.Covers
        online, _ = self.online_booking(make_gown(403), self.event - timedelta(days=2), self.event + timedelta(days=2))
        ReceiptRecord.objects.create(reservation=online, photo_url="https://example.test/r/a.jpg", covers=covers.RENTAL)
        self.assertEqual(online.next_receipt_covers(), covers.BOTH)              # online: always the full one
        cases = [((), covers.BOTH), ((covers.DEPOSIT,), covers.RENTAL), ((covers.RENTAL,), covers.DEPOSIT),
                 ((covers.BOTH,), covers.BOTH), ((covers.RENTAL, covers.DEPOSIT), covers.BOTH), (("",), covers.BOTH),
                 ((covers.DEPOSIT, ""), covers.RENTAL)]
        for number, (labels, expected) in enumerate(cases, start=410):
            reservation, _ = self.legacy_pending_walk_in(number, receipts=[(f"n{number}-{i}.jpg", label) for i, label in enumerate(labels)])
            self.assertEqual(reservation.next_receipt_covers(), expected, labels)

    def test_the_records_page_has_no_dropdown_and_tells_a_deposit_only_walk_in_what_the_upload_is(self):
        html = self.client.get(reverse("arabela_admin:reservation_records")).content.decode()
        for gone in ("rr-upload-covers", "Not specified", "uploadCovers", "This receipt is for</label>"):
            self.assertNotIn(gone, html, gone)
        self.assertIn("nextReceiptCovers(uploadFor)", html)
        self.assertIn("form.append('photo', this.uploadFile);", html)
        self.assertNotIn("form.append('covers'", html)

    def test_the_names_staff_read(self):
        covers = ReceiptRecord.Covers
        self.assertEqual(ReceiptRecord.name_for(covers.BOTH), "Paid in full")
        self.assertEqual((ReceiptRecord.name_for(covers.RENTAL), ReceiptRecord.name_for(covers.DEPOSIT)), ("Rental", "Deposit"))
        self.assertEqual((ReceiptRecord.name_for("", staff_recorded=False), ReceiptRecord.name_for("", staff_recorded=True)),
                         ("Paid in full", ""))
        self.assertEqual(covers.BOTH.value, "Rental + deposit")                  # what is STORED does not change


class PaymentBreakdownTests(Base):
    """Payment Verification / Dashboard Quick Verify / Security Deposits show what was PAID (an online customer pays only the
    ₱2,000 deposit, by GCash) with the gown's price and the total beside it, instead of only the total."""

    def online(self, number, *, status=Reservation.Status.PENDING, rental="5500", deposit="2000", total="7500", method="GCash"):
        gown = make_gown(number)
        reservation = Reservation.objects.create(
            customer=self.customer, customer_name="Online Customer", status=status, payment_method=method,
            payment_proof_url="https://example.test/proof.jpg", rental_subtotal=Decimal(rental), security_deposit=Decimal(deposit),
            total_amount=Decimal(total))
        ReservationItem.objects.create(reservation=reservation, gown=gown, gown_name=gown.name, size=gown.size,
                                       rental_date=self.event - timedelta(days=2), return_date=self.event + timedelta(days=2))
        return reservation

    def test_an_online_booking_shows_the_deposit_paid_with_the_gown_price_and_the_total(self):
        reservation = self.online(500)
        self.assertEqual(reservation.payment_breakdown, {
            "paidLabel": "Paid online by GCash", "headline": "₱2,000", "sub": "Gown ₱5,500 · Total ₱7,500", "total": "₱7,500",
            "lines": [{"label": "Security deposit", "note": "paid online by GCash", "amount": "₱2,000"},
                      {"label": "Gown rental", "note": "to pay at the shop", "amount": "₱5,500"}]})
        self.assertEqual(json.loads(reservation.payment_breakdown_json), reservation.payment_breakdown)
        # the shop's own (full) receipt later attached says the gown was paid at the shop; the headline stays the online payment
        ReceiptRecord.objects.create(reservation=reservation, photo_url="https://example.test/r/full.jpg", covers="Rental + deposit")
        again = Reservation.objects.get(id=reservation.id).payment_breakdown
        self.assertEqual((again["headline"], again["lines"][1]["note"]), ("₱2,000", "paid at the shop"))

    def test_without_gcash_it_just_says_paid_online(self):
        breakdown = self.online(501, method="").payment_breakdown
        self.assertEqual((breakdown["paidLabel"], breakdown["lines"][0]["note"]), ("Paid online", "paid online"))

    def test_a_rejected_online_payment_is_not_called_paid(self):
        breakdown = self.online(506, status=Reservation.Status.REJECTED).payment_breakdown
        self.assertEqual((breakdown["paidLabel"], breakdown["lines"][0]["note"]), ("Sent online by GCash (rejected)", "sent online by GCash, rejected"))

    def test_amounts_that_do_not_add_up_keep_the_single_total(self):
        for number, kwargs in enumerate((dict(total="9999"), dict(rental="0", total="2000"), dict(deposit="0", total="5500")), start=502):
            reservation = self.online(number, **kwargs)
            self.assertIsNone(reservation.payment_breakdown, kwargs)
            self.assertEqual(reservation.payment_breakdown_json, "null")
        html = self.client.get(reverse("arabela_admin:payment_verification")).content.decode()
        self.assertNotIn("Gown ₱", html)

    def test_a_walk_in_shows_what_the_shops_receipts_say(self):
        full = Reservation.objects.get(reference_code=self.post_booking([self.line(make_gown(503))], payment="full", receipt=b"b1").json()["reference_code"])
        self.assertEqual((full.payment_breakdown["headline"], full.payment_breakdown["sub"], full.payment_breakdown["paidLabel"]),
                         ("₱5,000", "Gown ₱3,000 + Deposit ₱2,000", "Paid at the shop"))
        self.assertEqual([line["note"] for line in full.payment_breakdown["lines"]], ["paid at the shop", "paid at the shop"])
        part = Reservation.objects.get(reference_code=self.post_booking([self.line(make_gown(504))], payment="deposit", receipt=b"b2").json()["reference_code"])
        self.assertEqual((part.payment_breakdown["headline"], part.payment_breakdown["sub"], part.payment_breakdown["paidLabel"]),
                         ("₱2,000", "Gown ₱3,000 still to pay · Total ₱5,000", "Deposit paid at the shop"))
        self.assertEqual([line["note"] for line in part.payment_breakdown["lines"]], ["paid at the shop", "still to pay"])
        self.upload(part, b"b2-rental")                                          # the rental receipt arrives: now it is paid in full
        done = Reservation.objects.get(id=part.id).payment_breakdown
        self.assertEqual((done["headline"], done["paidLabel"]), ("₱5,000", "Paid at the shop"))
        waiting, _ = self.legacy_pending_walk_in(505)                            # nothing paid, no receipt: the single total, as before
        self.assertIsNone(waiting.payment_breakdown)

    def test_payment_verification_shows_the_paid_amount_with_the_breakdown_window(self):
        self.online(510)
        html = self.client.get(reverse("arabela_admin:payment_verification")).content.decode()
        self.assertIn('<p class="font-medium text-gray-800 text-theme-sm dark:text-white/90">₱2,000</p>', html)
        self.assertIn('<p class="text-xs text-gray-400 dark:text-gray-500">Gown ₱5,500 · Total ₱7,500</p>', html)
        self.assertNotIn('<p class="text-gray-500 text-theme-sm dark:text-gray-400">&#8369;7500</p>', html)    # the table cell no longer leads with the total
        self.assertIn("breakdown: JSON.parse(", html)
        self.assertIn("viewingPayment.breakdown ? viewingPayment.breakdown.paidLabel : 'Amount'", html)
        self.assertIn("viewingPayment.breakdown ? viewingPayment.breakdown.headline : viewingPayment.amount", html)
        self.assertIn('class="pbd" x-show="viewingPayment.breakdown"', html)
        self.assertEqual(html.count("Amount breakdown"), 1)

    def test_an_old_booking_with_odd_amounts_still_shows_its_total(self):
        self.online(511, total="9000")
        html = self.client.get(reverse("arabela_admin:payment_verification")).content.decode()
        self.assertIn("&#8369;9000", html)
        self.assertNotIn("Gown ₱", html)

    def test_the_dashboard_card_and_window_and_security_deposits_window_show_it_too(self):
        self.online(512)
        dashboard = self.client.get(reverse("arabela_admin:dashboard")).content.decode()
        self.assertIn('style="color:#D4AF37">₱2,000</p>', dashboard)
        self.assertIn("Gown ₱5,500 · Total ₱7,500", dashboard)
        self.assertEqual(dashboard.count("breakdown: JSON.parse("), 2)
        self.assertIn('class="pbd" x-show="receiptPreview.breakdown"', dashboard)
        self.assertIn("receiptPreview.breakdown ? receiptPreview.breakdown.headline : receiptPreview.amount", dashboard)
        self.online(513, status=Reservation.Status.CONFIRMED)
        deposits = self.client.get(reverse("arabela_admin:security_deposits")).content.decode()
        self.assertIn("breakdown: JSON.parse(", deposits)
        self.assertIn('class="pbd" x-show="viewingPayment.breakdown"', deposits)
        self.assertIn("viewingPayment.breakdown ? viewingPayment.breakdown.headline : viewingPayment.amount", deposits)

    def test_the_pages_do_not_ask_the_database_once_per_booking_with_the_breakdown(self):
        def queries(name):
            with CaptureQueriesContext(connection) as captured:
                self.assertEqual(self.client.get(reverse(f"arabela_admin:{name}")).status_code, 200)
            return len(captured.captured_queries)

        for number in range(520, 523):
            self.online(number)
        few = {name: queries(name) for name in ("payment_verification", "dashboard", "security_deposits")}
        for number in range(523, 528):
            self.online(number)
        for name, count in few.items():
            self.assertLessEqual(queries(name), count + 1, name)
