"""The refund rule (reservations/ledger.py): a cancelled booking keeps the security deposit and everything else paid goes back (the owner may
give the deposit back too); a single cancelled gown's rental is no longer counted and goes back if paid; only the owner or staff with
"Return money to customers" can hand money back."""
import json
from decimal import Decimal as D

from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditLog, CustomerMessage, UserProfile
from arabela_admin import dashboard_money, notifications
from gowns.test_over_deposit import OverBase
from reservations import cancellation, ledger, payments
from reservations.models import Payment, Reservation, ReservationItem


class RefundBase(OverBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.owner = User.objects.create_user("rr_owner", password="x", is_staff=True, first_name="Olive")
        UserProfile.objects.update_or_create(user=cls.owner, defaults={"role": UserProfile.Role.OWNER})
        cls.cashier = User.objects.create_user("rr_cashier", password="x", is_staff=True, first_name="Cathy")
        UserProfile.objects.update_or_create(user=cls.cashier, defaults={"role": UserProfile.Role.STAFF, "permissions": "return_money"})

    def client_of(self, user):
        client = Client()
        client.force_login(user)
        return client

    def full(self, *, gowns=1, channel="GCASH"):
        """An online booking paid in full."""
        lines = [self.dated()] if gowns == 1 else [self.dated(), self.dated(self.gown2)]
        attempt, _ = self.started(items=lines, option="full")
        self.assertEqual(self.pay_and_confirm(attempt, channel=channel)["state"], "booked")
        attempt.refresh_from_db()
        return Reservation.objects.get(pk=attempt.reservation_id)

    def customer_cancels(self, reservation):
        response = self.client.post(reverse("gowns:reservation_item_cancel", args=[reservation.items.order_by("id").first().pk]))
        self.assertEqual(response.status_code, 200, response.content)

    def give_back(self, reservation, amount, method="Cash", *, who=None, **extra):
        body = {"method": method, "amount": str(amount), "request_id": f"rr-{method}-{amount}-{Payment.objects.count()}", **extra}
        with override_settings(PAYMONGO_AUTO_REFUND=True):
            return self.post("deposit_return", reservation.pk, body=body, client=self.client_of(who or self.cashier))

    def detail(self, reservation, who=None):
        with override_settings(PAYMONGO_AUTO_REFUND=True):
            return self.client_of(who or self.owner).get(reverse("arabela_admin:payment_detail", args=[reservation.pk])).json()


class CancelledBookingRefundTests(RefundBase):
    def test_everything_but_the_deposit_comes_back_to_the_account_that_paid(self):
        reservation = self.full()
        rental, deposit = reservation.rental_subtotal, reservation.security_deposit
        page = self.client.get(reverse("gowns:reservation_item_detail", args=[reservation.items.get().pk])).content.decode()
        self.assertIn(f"Your PHP {deposit:,.2f} security deposit is not refunded.", page)
        self.assertIn(f"{rental:,.2f} you paid besides it will be returned to you", page)
        self.assertIn(f"{rental:,.2f} you paid besides it", self.client.get(reverse("gowns:orders")).content.decode())
        self.customer_cancels(reservation)
        reservation.refresh_from_db()
        s = self.statement(reservation)
        self.assertEqual((reservation.refunds_rental, s.state, s.refund_available, s.rental_to_return, s.kept, s.balance_due),
                         (True, ledger.REFUND, rental, rental, deposit, D("0")))
        self.assertIn(f"{ledger.peso(rental)} you paid besides it will be returned", CustomerMessage.objects.latest("id").body)
        shown = self.detail(reservation)
        self.assertEqual([(m["label"], m["note"]) for m in shown["money"]],
                         [("Gown rental paid", "To give back"), ("Security deposit", "Kept by the shop")])
        self.assertTrue(shown["canReturn"] and shown["canReturnMoney"])
        self.assertEqual((shown["onlineRefund"]["available"], shown["onlineRefund"]["label"]), (True, "GCash"), shown["onlineRefund"])
        listed = self.admin.get(reverse("arabela_admin:payments")).content.decode()
        self.assertIn(f"Deposit kept (cancelled), give back {ledger.peso(rental)}", listed)
        mine = self.client.get(reverse("gowns:reservation_item_detail", args=[reservation.items.get().pk])).content.decode()
        for words in ("Money coming back to you", "Coming back to you", "Kept by the shop (not refunded)", "Still coming back to you"):
            self.assertIn(words, mine)
        self.assertEqual(dashboard_money.money_between()["income"], rental)
        # sent back through Xendit, to the payment that brought it
        response = self.give_back(reservation, rental, "PayMongo")
        self.assertEqual(response.status_code, 200, response.content)
        refund = list(self.fake.refunds.values())[0]
        paid = Payment.objects.get(reservation=reservation, direction="In")
        self.assertEqual((D(str(refund["amount"])), refund["payment_id"]), (rental, paid.provider_ref))
        out = Payment.objects.get(reservation=reservation, direction="Out")
        self.assertEqual((out.title, out.get_purpose_display(), out.rental_part, out.amount), ("REFUND", "Refund", rental, rental))
        s = self.statement(reservation)
        self.assertEqual((s.state, s.refund_available, s.state_label), (ledger.CANCELLED, D("0"), "Cancelled - deposit kept"))
        money = dashboard_money.money_between()
        self.assertEqual((money["income"], money["returned"], money["rental_back"]), (D("0"), D("0"), rental))
        receipt = self.client.get(reverse("gowns:receipt_view", args=[out.pk])).content.decode()
        for words in ("REFUND", "BOOKING CANCELLED", "Gown rental paid", "Kept by the shop", "RETURNED NOW"):
            self.assertIn(words, receipt)
        self.assertIn("of your cancelled reservation", CustomerMessage.objects.latest("id").body)
        self.assertTrue(AuditLog.objects.filter(action="payment.return_deposit").exists())
        self.assertEqual(self.give_back(reservation, "1").status_code, 409)

    def test_only_the_owner_or_allowed_staff_can_hand_money_back(self):
        reservation = self.full()
        self.customer_cancels(reservation)
        for method in ("Cash", "PayMongo", "GCash"):
            refused = self.give_back(reservation, "100", method, who=self.staff, reference="1234567890123")
            self.assertEqual(refused.status_code, 403, method)
            self.assertIn("return money to customers", refused.json()["error"])
        self.assertFalse(Payment.objects.filter(direction="Out").exists())
        self.assertFalse(self.detail(reservation, self.staff)["canReturnMoney"])
        self.assertEqual(self.give_back(reservation, "100", who=self.owner).status_code, 200)
        self.assertEqual(self.give_back(reservation, "100", who=self.cashier).status_code, 200)

    def test_cash_or_the_shops_gcash_with_its_reference_number(self):
        reservation = self.full()
        self.customer_cancels(reservation)
        rental = reservation.rental_subtotal
        self.assertEqual(self.give_back(reservation, "500", "GCash", reference="").status_code, 400)
        self.assertEqual(self.give_back(reservation, "500", "GCash", reference="1234567890123").status_code, 200)
        self.assertEqual(self.give_back(reservation, rental - D("500")).status_code, 200)
        rows = Payment.objects.filter(direction="Out").order_by("id")
        self.assertEqual([(r.method, r.reference, r.rental_part) for r in rows],
                         [(Payment.Method.GCASH, "1234567890123", D("500")), (Payment.Method.CASH, "", rental - D("500"))])
        self.assertEqual(self.statement(reservation).refund_available, D("0"))

    def test_the_owner_may_give_the_deposit_back_too(self):
        reservation = self.full()
        self.customer_cancels(reservation)
        total = reservation.rental_subtotal + reservation.security_deposit
        url_body = {"give": True, "reason": "The gown was damaged at the shop"}
        for who in (self.staff, self.cashier):
            self.assertEqual(self.post("deposit_exception", reservation.pk, body=url_body, client=self.client_of(who)).status_code, 403)
        owner = self.client_of(self.owner)
        self.assertEqual(self.post("deposit_exception", reservation.pk, body={"give": True, "reason": ""}, client=owner).status_code, 400)
        self.assertEqual(self.post("deposit_exception", reservation.pk, body=url_body, client=owner).status_code, 200)
        s = self.statement(reservation)
        self.assertEqual((s.refund_available, s.kept, s.deposit_back_too), (total, D("0"), True))
        self.assertIn("also return your", CustomerMessage.objects.latest("id").body)
        self.assertEqual(self.post("deposit_exception", reservation.pk, body={"give": False, "reason": ""}, client=owner).status_code, 200)
        self.assertEqual(self.statement(reservation).refund_available, reservation.rental_subtotal)
        self.post("deposit_exception", reservation.pk, body=url_body, client=owner)
        response = self.give_back(reservation, total, "PayMongo")
        self.assertEqual(response.status_code, 200, response.content)
        s = self.statement(reservation)
        self.assertEqual((s.refund_available, s.state_label), (D("0"), "Cancelled"))
        self.assertEqual(self.post("deposit_exception", reservation.pk, body={"give": False, "reason": ""}, client=owner).status_code, 409)
        self.assertTrue(AuditLog.objects.filter(action="payment.deposit_exception").exists())

    def test_the_shop_cancelling_a_walk_in_paid_in_cash(self):
        reservation = Reservation.objects.create(
            channel=Reservation.Channel.WALK_IN, status=Reservation.Status.CONFIRMED, customer_name="Walk In", reviewed_at=timezone.now(),
            rental_subtotal=D("3000"), security_deposit=D("2000"), total_amount=D("5000"))
        item = ReservationItem.objects.create(reservation=reservation, gown=self.gown, gown_name=self.gown.name, rental_price=D("3000"),
                                              rental_date=self.day(-2), return_date=self.day(2))
        payments.record_payment(reservation_id=reservation.pk, purpose=Payment.Purpose.FULL, amount=D("5000"), method=Payment.Method.CASH,
                                user=self.staff)
        cancellation.cancel_by_staff(reservation.pk, staff_name="Olive", reason_key="customer_asked", note="",
                                     typed_code=reservation.reference_code)
        s = self.statement(reservation)
        self.assertEqual((s.refund_available, s.kept, s.state), (D("3000"), D("2000"), ledger.REFUND))
        choice = self.detail(reservation)["onlineRefund"]
        self.assertEqual(choice["available"], False)
        self.assertIn("not paid online", choice["reason"])
        self.assertEqual(self.give_back(reservation, "3001").status_code, 400)
        self.assertEqual(self.give_back(reservation, "3000").status_code, 200)
        self.assertEqual(item.pk, reservation.items.get().pk)

    def test_a_maya_payment_goes_back_in_part_only_from_the_next_day(self):
        reservation = self.full(channel="PAYMAYA")
        self.customer_cancels(reservation)
        choice = self.detail(reservation)["onlineRefund"]
        self.assertEqual(choice["available"], False)
        self.assertIn("day after", choice["reason"])
        Payment.objects.filter(reservation=reservation).update(paid_at=timezone.now() - timezone.timedelta(days=1, hours=1))
        self.assertTrue(self.detail(reservation)["onlineRefund"]["available"])

    def test_an_older_cancelled_booking_stays_as_it_was(self):
        reservation = self.full()
        Reservation.objects.filter(pk=reservation.pk).update(status=Reservation.Status.CANCELLED, pays_over_deposit=False)
        s = self.statement(reservation)
        self.assertEqual((s.refund_available, s.state, s.refunds_rental), (D("0"), ledger.CANCELLED, False))
        self.assertEqual(self.give_back(reservation, "100").status_code, 409)
        notes = [m["note"] for m in self.detail(reservation)["money"]]
        self.assertIn("Cancelled before refunds: handled at the shop", notes)


class OneGownCancelledTests(RefundBase):
    def cancel_one(self, reservation, which=1):
        item = list(reservation.items.order_by("id"))[which]
        with self.captureOnCommitCallbacks(execute=True):
            cancellation.cancel_gowns_by_staff(reservation.pk, [item.pk], staff_name="Olive", reason_key="no_show", note="")
        return ReservationItem.objects.get(pk=item.pk)

    def test_paid_in_full_the_cancelled_gowns_rental_goes_back_and_the_rest_carries_on(self):
        reservation = self.full(gowns=2)
        total_rental, deposit = reservation.rental_subtotal, reservation.security_deposit
        item = self.cancel_one(reservation)
        price = item.rental_price
        s = self.statement(reservation)
        self.assertEqual((item.rental_dropped, s.rental, s.rental_dropped, s.rental_to_return, s.refund_available, s.balance_due, s.state),
                         (True, total_rental - price, price, price, price, D("0"), ledger.REFUND))
        self.assertEqual(s.deposit_paid, deposit)
        self.assertIn(f"The {ledger.peso(price)} rental you paid for it will be returned", CustomerMessage.objects.latest("id").body)
        self.assertEqual(dashboard_money.money_between()["income"], total_rental)
        bell = [i for i in notifications.build_items(self.owner) if "rental of a cancelled gown" in str(i)]
        self.assertEqual(len(bell), 1, bell)
        response = self.give_back(reservation, price + D("1"))
        self.assertEqual(response.status_code, 409)
        self.assertIn("rental of the cancelled gown", response.json()["error"])
        response = self.give_back(reservation, price, "PayMongo")
        self.assertEqual(response.status_code, 200, response.content)
        out = Payment.objects.get(reservation=reservation, direction="Out")
        self.assertEqual((out.title, out.rental_part, out.amount), ("REFUND", price, price))
        self.assertTrue(out.snapshot["return"]["rental_only"])
        self.assertIn("rental you paid for", CustomerMessage.objects.latest("id").body)
        s = self.statement(reservation)
        self.assertEqual((s.refund_available, s.rental_to_return, s.deposit_given, s.state), (D("0"), D("0"), D("0"), ledger.PAID))
        self.assertEqual((s.paid_net, s.total), (total_rental - price + deposit, total_rental - price + deposit))   # "Paid so far" matches
        self.assertEqual(dashboard_money.money_between()["income"], total_rental - price)
        page = self.client_of(self.owner).get(reverse("arabela_admin:security_deposits"))
        shown = next(r.dep for r in page.context["reservations"] if r.pk == reservation.pk)
        self.assertEqual((shown["state"], shown["handed_back"], shown["paid"]), ("Held", D("0"), deposit))   # returned rental is not the deposit
        # the remaining gown comes back: the whole deposit is returned, as before
        ReservationItem.objects.filter(reservation=reservation, rental_dropped=False).update(
            stage=ReservationItem.Stage.RETURNED, picked_up_on=self.today, returned_on=self.today)
        s = self.statement(reservation)
        self.assertEqual((s.refund_available, s.deposit_available), (deposit, deposit))
        response = self.give_back(reservation, deposit)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Payment.objects.filter(direction="Out").latest("id").title, "DEPOSIT RETURN")
        self.assertEqual((self.statement(reservation).refund_available, self.statement(reservation).state), (D("0"), ledger.SETTLED))

    def test_returned_gown_and_cancelled_rental_in_one_payment(self):
        reservation = self.full(gowns=2)
        item = self.cancel_one(reservation)
        ReservationItem.objects.filter(reservation=reservation, rental_dropped=False).update(
            stage=ReservationItem.Stage.RETURNED, picked_up_on=self.today, returned_on=self.today)
        both = item.rental_price + reservation.security_deposit
        self.assertEqual(self.statement(reservation).refund_available, both)
        response = self.give_back(reservation, both)
        self.assertEqual(response.status_code, 200, response.content)
        out = Payment.objects.get(reservation=reservation, direction="Out")
        self.assertEqual((out.title, out.rental_part, out.snapshot["return"]["rental_part"]),
                         ("DEPOSIT RETURN", item.rental_price, f"{item.rental_price:.2f}"))
        receipt = self.client.get(reverse("gowns:receipt_view", args=[out.pk])).content.decode()
        for words in ("Rental of cancelled gown", "RETURNED NOW"):
            self.assertIn(words, receipt)

    def test_deposit_only_paid_the_balance_just_goes_down(self):
        reservation = self.booked([self.dated(), self.dated(self.gown2)])
        before = self.statement(reservation)
        item = self.cancel_one(reservation)
        after = self.statement(reservation)
        self.assertEqual((after.balance_due, after.rental_to_return, after.refund_available),
                         (before.balance_due - item.rental_price, D("0"), D("0")))
        self.assertIn(f"You no longer pay its {ledger.peso(item.rental_price)} rental", CustomerMessage.objects.latest("id").body)

    def test_the_booking_page_and_the_cancel_window_say_what_goes_back(self):
        reservation = self.full(gowns=2)
        second = list(reservation.items.order_by("id"))[1]
        active = self.client_of(self.owner).get(reverse("arabela_admin:active_reservations")).content.decode()
        self.assertIn(f'data-mode="gown" data-ids="{second.pk}" data-back="{second.rental_price:,.2f}"', active)
        self.assertIn(f'data-back="{reservation.rental_subtotal:,.2f}"', active)
        self.assertNotIn("no refund is given", active)
        item = self.cancel_one(reservation)
        page = self.client.get(reverse("gowns:reservation_item_detail", args=[item.pk])).content.decode()
        self.assertIn("Rental coming back to you", page)

    def test_the_last_gown_cancelled_cancels_the_booking_and_keeps_only_the_deposit(self):
        reservation = self.full(gowns=2)
        self.cancel_one(reservation, 1)
        self.give_back(reservation, reservation.items.order_by("id").last().rental_price)
        with self.captureOnCommitCallbacks(execute=True):
            cancellation.cancel_gowns_by_staff(reservation.pk, [reservation.items.order_by("id").first().pk], staff_name="Olive",
                                               reason_key="no_show", note="")
        reservation.refresh_from_db()
        s = self.statement(reservation)
        first_price = reservation.items.order_by("id").first().rental_price
        self.assertEqual((reservation.status, reservation.refunds_rental, s.refund_available, s.kept),
                         (Reservation.Status.CANCELLED, True, first_price, reservation.security_deposit))


class WordingTests(RefundBase):
    def test_the_pages_describe_the_payment_in_use(self):
        for name in ("gowns:terms_and_conditions", "gowns:faqs", "gowns:how_it_works"):
            page = self.client.get(reverse(name)).content.decode()
            for old in ("via GCash only", "GCash via website only", "GCash or cash. This", "before the admin confirms it", "Admin Verification"):
                self.assertNotIn(old, page, f"{name}: {old}")
        terms_page = self.client.get(reverse("gowns:terms_and_conditions")).content.decode()
        for words in ("GCash, Maya or QR Ph", "at any time before pick-up", "everything else you paid", "If only one gown"):
            self.assertIn(words, terms_page)
        with override_settings(XENDIT_SECRET_KEY="", PAYMONGO_SECRET_KEY="", PAYMENT_PROVIDER=""):
            old = self.client.get(reverse("gowns:terms_and_conditions")).content.decode()
        self.assertIn("via GCash only", old)
