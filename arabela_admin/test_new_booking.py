"""New Booking: walk-in / Facebook / phone bookings recorded by staff (arabela_admin/new_booking.py, reservations/staff_booking.py),
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
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomerEmailLog, CustomerMessage, UserProfile
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

    def post_booking(self, lines, *, paid=True, receipts=(("Rental + deposit", b"a"),), method="Cash", name="Lorna Reyes",
                     phone="0917 123 4567", channel="Walk-in", account=None, notebook="", note="", user=None):
        if user:
            self.client.force_login(user)
        data = {
            "channel": channel, "name": name, "phone": phone, "address": "Antipolo", "notebook_number": notebook,
            "account": account.id if account else "", "lines": json.dumps(lines), "paid": "1" if paid else "0",
            "payment_method": method if paid else "", "note": note,
        }
        for index, (covers, salt) in enumerate(receipts, start=1):
            data[f"receipt_{index}"] = jpeg(f"receipt{index}.jpg", salt)
            data[f"receipt_{index}_covers"] = covers
        return self.client.post(reverse("arabela_admin:booking_create"), data=data)

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
        self.assertEqual(reservation.payment_method, "Cash")
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
        self.assertEqual(data["where"], "Active Reservations")

    def test_the_booking_number_continues_the_same_series(self):
        online = Reservation.objects.create(customer=self.customer, customer_name="Online Customer")
        walk_in = Reservation.objects.get(reference_code=self.post_booking([self.line(make_gown(21))]).json()["reference_code"])
        after = Reservation.objects.create(customer=self.customer, customer_name="Online Customer")
        numbers = [int(r.reference_code.rsplit("-", 1)[1]) for r in (online, walk_in, after)]
        self.assertEqual(numbers, [numbers[0], numbers[0] + 1, numbers[0] + 2])
        self.assertEqual(walk_in.reference_code[:9], online.reference_code[:9])

    def test_not_paid_yet_waits_in_pending_and_still_holds_the_gown(self):
        gown = make_gown(22)
        response = self.post_booking([self.line(gown)], paid=False, receipts=())
        self.assertEqual(response.status_code, 200, response.content)
        reservation = Reservation.objects.get(reference_code=response.json()["reference_code"])
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertIsNone(reservation.reviewed_at)
        self.assertEqual(response.json()["where"], "Pending Approval")
        gown.refresh_from_db()
        self.assertEqual(gown.status, Gown.Status.AVAILABLE)      # Reserved only once approved, exactly like online
        self.assertFalse(availability.check_unit(gown, self.event)["ok"])

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
            ("taken now but not paid", dict(lines=[self.line(gown, takes_now=True)], paid=False, receipts=()), "once it is paid"),
            ("paid with no receipt", dict(lines=[self.line(gown)], receipts=()), "receipt"),
            ("paid, receipt for the rental only", dict(lines=[self.line(gown)], receipts=(("Rental", b"r"),)), "both"),
            ("paid with no method", dict(lines=[self.line(gown)], method=""), "Cash or GCash"),
            ("receipt without a label", dict(lines=[self.line(gown)], receipts=(("", b"x"),)), "Choose what each receipt"),
        ]
        for label, kwargs, words in cases:
            response = self.post_booking(**{k: v for k, v in kwargs.items() if k != "lines"}, lines=kwargs["lines"])
            self.assertEqual(response.status_code, 400, label)
            self.assertIn(words, response.json()["error"], label)
        self.assertFalse(Reservation.objects.exists())
        both = self.post_booking([self.line(gown)], receipts=(("Rental", b"r1"), ("Deposit", b"d1")))
        self.assertEqual(both.status_code, 200, both.content)
        self.assertEqual(ReceiptRecord.objects.count(), 2)

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
        twice = self.post_booking([self.line(third), self.line(third)], receipts=(("Rental + deposit", b"t"),))
        self.assertEqual(twice.status_code, 400)
        self.assertIn("twice", twice.json()["error"])

    def test_the_customer_fields_are_checked(self):
        gown = make_gown(29)
        for kwargs, words in ((dict(name=""), "name"), (dict(name="12345"), "real name"), (dict(phone="12345"), "mobile number"),
                              (dict(channel="Online"), "Walk-in, Facebook, or Phone"), (dict(notebook="bad*no"), "notebook")):
            response = self.post_booking([self.line(gown)], **kwargs)
            self.assertEqual(response.status_code, 400, kwargs)
            self.assertIn(words, response.json()["error"], kwargs)
        good = self.post_booking([self.line(gown)], phone="+63 917 765 4321", channel="Facebook", notebook=" 0457 ")
        self.assertEqual(good.status_code, 200, good.content)
        reservation = Reservation.objects.get(reference_code=good.json()["reference_code"])
        self.assertEqual((reservation.phone, reservation.channel, reservation.notebook_number),
                         ("09177654321", Reservation.Channel.FACEBOOK, "0457"))

    def test_a_notebook_number_and_a_receipt_photo_can_be_used_only_once(self):
        first = self.post_booking([self.line(make_gown(30))], notebook="A-12", receipts=(("Rental + deposit", b"same"),))
        self.assertEqual(first.status_code, 200, first.content)
        again = self.post_booking([self.line(make_gown(31))], notebook="a-12", receipts=(("Rental + deposit", b"other"),))
        self.assertEqual(again.status_code, 409)
        self.assertIn(first.json()["reference_code"], again.json()["error"])
        photo = self.post_booking([self.line(make_gown(32))], receipts=(("Rental + deposit", b"same"),))
        self.assertEqual(photo.status_code, 409)
        self.assertIn("already attached", photo.json()["error"])
        two_same = self.post_booking([self.line(make_gown(33))], receipts=(("Rental", b"twin"), ("Deposit", b"twin")))
        self.assertEqual(two_same.status_code, 400)

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
        staff_account = self.post_booking([self.line(make_gown(36))], account=self.staff, receipts=(("Rental + deposit", b"s"),),
                                          user=self.staff)
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
    def walk_in(self, *, paid=True, event=None, takes_now=False, salt=b"w"):
        gown = make_gown(40 + Reservation.objects.count())
        response = self.post_booking([self.line(gown, event=event, takes_now=takes_now)], paid=paid,
                                     receipts=(("Rental + deposit", salt),) if paid else ())
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

    def test_pending_approval_then_approve_with_no_account(self):
        reservation, gown = self.walk_in(paid=False)
        page = self.client.get(reverse("arabela_admin:pending_approval")).content.decode()
        self.assertIn(reservation.reference_code, page)
        self.assertIn("Attach receipt", page)
        self.assertIn("Not paid yet", page)
        upload = self.client.post(reverse("arabela_admin:receipt_upload"), data={
            "reference_code": reservation.reference_code, "photo": jpeg("paid.jpg", b"paid"), "covers": "Rental + deposit"})
        self.assertEqual(upload.status_code, 200, upload.content)
        approve = self.client.post(reverse("arabela_admin:reservation_approve", args=[reservation.id]))
        self.assertEqual(approve.status_code, 200, approve.content)
        reservation.refresh_from_db()
        gown.refresh_from_db()
        self.assertEqual((reservation.status, gown.status), (Reservation.Status.CONFIRMED, Gown.Status.RESERVED))
        self.assertFalse(CustomerMessage.objects.exists())

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

    def test_payment_incomplete_when_the_receipts_dont_cover_both(self):
        reservation, _ = self.walk_in(paid=False)
        self.client.post(reverse("arabela_admin:reservation_approve", args=[reservation.id]))
        records = self.client.get(reverse("arabela_admin:reservation_records"))
        record = next(r for r in records.context["records"] if r["reference"] == reservation.reference_code)
        self.assertTrue(record["paymentIncomplete"])

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
        pending, _ = self.walk_in(paid=False, salt=b"p")
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
            response = self.post_booking([self.line(gown)], account=self.customer, receipts=(("Rental + deposit", b"le"),))
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
