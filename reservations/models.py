import json
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.formats import date_format

from accounts.roles import staff_display_name


def _peso_text(amount):
    """₱5,500 (and ₱5,500.50 when there are centavos) -- how the admin's payment screens write an amount."""
    return f"₱{amount:,.0f}" if amount == int(amount) else f"₱{amount:,.2f}"


def _group_reference(digits):
    """5046933479702 -> '5046 933 479702', the way GCash prints a reference (anything that is not 13 digits is shown as it is)."""
    digits = digits or ""
    return f"{digits[:4]} {digits[4:7]} {digits[7:]}" if len(digits) == 13 and digits.isdigit() else digits


class Reservation(models.Model):
    """A single customer reservation request. The dates live on the child
    ReservationItem rows (the customer picks a rental period per gown on the
    product page). One request can hold several gowns, mirroring the admin
    mockups' 'N Reservations' grouping."""

    class Status(models.TextChoices):
        PENDING = 'Pending', 'Pending'          # submitted, awaiting admin review
        CONFIRMED = 'Confirmed', 'Confirmed'    # approved, awaiting pick-up
        ACTIVE = 'Active', 'Active'             # currently rented out
        RETURNED = 'Returned', 'Returned'
        OVERDUE = 'Overdue', 'Overdue'
        REJECTED = 'Rejected', 'Rejected'
        CANCELLED = 'Cancelled', 'Cancelled'

    class PaymentMethod(models.TextChoices):
        GCASH = 'GCash', 'GCash'
        CASH = 'Cash', 'Cash'
        MAYA = 'Maya', 'Maya'                    # paid online through PayMongo (reservations/online_payment.py)
        QRPH = 'QR Ph', 'QR Ph'

    class Channel(models.TextChoices):
        """How the booking reached the shop. Online = the customer booked on the website; Walk-in = recorded by staff in the
        admin's New Booking page (arabela_admin/new_booking.py) for a customer who came to the shop -- so every booking, however
        it arrived, holds its gown on the same calendar. Facebook and Phone are no longer offered there; they stay only so a
        booking recorded earlier keeps its label."""
        ONLINE = 'Online', 'Online'
        WALK_IN = 'Walk-in', 'Walk-in'
        FACEBOOK = 'Facebook', 'Facebook'
        PHONE = 'Phone', 'Phone call'

    reference_code = models.CharField(max_length=20, unique=True, blank=True)
    # Empty for a booking staff recorded for a customer with no website account (a walk-in, usually): the customer is then
    # only the name and phone below. Every place that writes to "the customer" (messages, emails, reminders, cancellation
    # limits) skips such a booking -- staff call the phone number instead.
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='reservations',
        null=True, blank=True,
    )
    customer_name = models.CharField(max_length=120)
    # Contact snapshot captured on the reservation form at submission time (blank for
    # reservations made before this field existed -- always display with a fallback).
    phone = models.CharField(max_length=20, blank=True)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=10, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    payment_method = models.CharField(max_length=10, choices=PaymentMethod.choices, blank=True)
    payment_proof_url = models.URLField(blank=True)
    # The GCash receipt's own reference number (digits only), typed by the customer at checkout.
    # Optional. Lets staff find the payment in the shop's GCash history, and lets the admin
    # pages warn when the same number turns up on two bookings (one receipt reused).
    # db_default: an older deployment that doesn't know this column can still create bookings.
    gcash_reference = models.CharField(max_length=20, blank=True, default='', db_default='', db_index=True)
    rental_subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    security_deposit = models.DecimalField(max_digits=10, decimal_places=2, default=2000)
    # The late-return fee for EACH gown, per day late, this booking was made with (the owner's setting then, gowns/late_fee.py): like the
    # deposit, changing the setting later does not touch a booking that already exists. Used by reservations/ledger.py.
    late_fee_per_day = models.PositiveIntegerField(default=200, db_default=200)
    # A booking made since amounts OVER the deposit are paid right away: booked extra days that cost more than the deposit are paid when booking,
    # a date change that goes over it is paid before it happens (reservations/date_requests.py), and a cancelled booking gets that extra back.
    # False = an older booking, which keeps paying such amounts at the shop. db_default: see channel.
    pays_over_deposit = models.BooleanField(default=False, db_default=False)
    # What was due over the security deposit at the moment of booking (paid together with the deposit or the whole booking).
    upfront_extra = models.DecimalField(max_digits=10, decimal_places=2, default=0, db_default=0)
    # Set when a booking is cancelled under the refund rule: the shop keeps the security deposit and gives back everything else the customer paid
    # (rental and extra days), from the Payments page. False = not cancelled, or cancelled before the rule (it stays as it was settled).
    refunds_rental = models.BooleanField(default=False, db_default=False)
    # Only the OWNER can write it, on such a cancelled booking: why the deposit goes back too (the cancel was the shop's fault). Empty = the deposit is kept.
    deposit_refund_reason = models.CharField(max_length=200, blank=True, default='', db_default='')
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    # Set once staff releases the deposit back to the customer (a separate, later step
    # from marking the gown itself returned -- see reservation_return_deposit_view).
    deposit_returned_at = models.DateTimeField(null=True, blank=True)
    # How the booking came in (see Channel). db_default: an older deployment that doesn't know this column can still create
    # (online) bookings while the database is already migrated.
    channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.ONLINE, db_default=Channel.ONLINE)
    # The staff member or owner who recorded a booking in the admin (blank for an online booking). SET_NULL: removing a
    # staff account later must never take a booking with it.
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    # The shop's own number for this booking on paper (its notebook or receipt book), when it has one. Optional; never the
    # same on two bookings (see Meta.constraints), so one paper record can't be reused for a second booking.
    notebook_number = models.CharField(max_length=30, blank=True, default='', db_default='')
    # The ID check that stood behind this booking (reservations/identity.py): the customer's verified ID (online), or the check made for this walk-in.
    # SET_NULL: deleting an ID check (the wipe script, an account being deleted) must never take a booking with it.
    identity_check = models.ForeignKey('IdentityCheck', on_delete=models.SET_NULL, null=True, blank=True, related_name='reservations')

    # ---- What the receipt check found (gowns/receipt_check.py) and the owner's own look at her GCash ----------------------------
    # Every one of these has a database default, so an older deployment that doesn't know the columns yet can still create bookings
    # while the database is already migrated.
    #
    # A fingerprint of the cleaned picture, so the same screenshot can never be used for two bookings.
    proof_hash = models.CharField(max_length=64, blank=True, default='', db_default='', db_index=True)
    # amount | minute | recipient number of the receipt: two bookings with the same one are suspicious (but not refused, because two
    # real customers can pay the same amount in the same minute).
    proof_fingerprint = models.CharField(max_length=120, blank=True, default='', db_default='', db_index=True)
    # '' = not read (a walk-in, or a booking made before the check existed), 'pass' = every check was fine, 'needs_check' = staff
    # decide (the reasons are in proof_notes).
    proof_check = models.CharField(max_length=12, blank=True, default='', db_default='')
    proof_notes = models.TextField(blank=True, default='', db_default='')        # one line per thing the check noticed
    proof_reading = models.TextField(blank=True, default='', db_default='')      # what the reader found, as JSON text
    proof_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    proof_paid_at = models.DateTimeField(null=True, blank=True)
    proof_recipient = models.CharField(max_length=40, blank=True, default='', db_default='')
    proof_old_number = models.BooleanField(default=False, db_default=False)     # paid to the shop's PREVIOUS GCash number
    # The owner's own check in her GCash app. The GCash account is hers alone, so staff cannot see whether the money arrived.
    # '' = nothing to check (a walk-in), 'pending' = waiting for her, 'received' / 'not_found' = her answer.
    owner_check = models.CharField(max_length=12, blank=True, default='', db_default='', db_index=True)
    owner_checked_at = models.DateTimeField(null=True, blank=True)
    owner_checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    owner_reminder_sent_at = models.DateTimeField(null=True, blank=True)
    # Whose GCash the customer paid: 'owner' (her own account: staff cannot approve until she confirms it), 'manager' (the shop's: staff approve as
    # usual) or '' (a walk-in, or a booking made before the shop had two accounts: never held back). Worked out from the receipt's recipient.
    proof_account = models.CharField(max_length=10, blank=True, default='', db_default='')
    # What the customer TYPED from their own GCash app (gowns/typed_payment.py): the 13-digit reference and the amount they paid, and how those
    # compared with what the receipt reader found in the picture. proof_match: '' = nothing was typed (a walk-in, or a booking made before the
    # boxes existed), 'match', or 'reference' / 'amount' / 'both' = the typed value differed from the receipt even after the customer looked
    # again (the staff and the owner see it). db_default for the same reason as every column above.
    proof_typed_reference = models.CharField(max_length=20, blank=True, default='', db_default='', db_index=True)
    proof_typed_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    proof_match = models.CharField(max_length=12, blank=True, default='', db_default='')

    class ProofCheck(models.TextChoices):
        PASS = 'pass', 'Pre-checked'
        NEEDS_CHECK = 'needs_check', 'Needs check'

    class ProofMatch(models.TextChoices):
        MATCH = 'match', 'Typed numbers match the receipt'
        REFERENCE = 'reference', 'Typed reference differs from the receipt'
        AMOUNT = 'amount', 'Typed amount differs from the receipt'
        BOTH = 'both', 'Typed reference and amount differ from the receipt'

    class ProofAccount(models.TextChoices):
        OWNER = 'owner', 'Owner’s GCash'
        MANAGER = 'manager', 'Manager’s GCash'

    class OwnerCheck(models.TextChoices):
        PENDING = 'pending', 'Not checked by the owner yet'
        RECEIVED = 'received', 'Payment confirmed by the owner'
        NOT_FOUND = 'not_found', 'Owner: payment not found'

    @property
    def gcash_duplicate_codes(self):
        """Reference codes of OTHER bookings that carry the same GCash reference number, as a
        comma-separated string ('' when there are none or no number was given). One cheap
        indexed query, and only for the few bookings that have a number."""
        numbers = {number for number in (self.gcash_reference, self.proof_typed_reference) if number}
        if not numbers:
            return ''
        others = (
            Reservation.objects.filter(Q(gcash_reference__in=numbers) | Q(proof_typed_reference__in=numbers))
            .exclude(pk=self.pk).order_by('created_at').values_list('reference_code', flat=True)
        )
        return ', '.join(others)

    @property
    def reference_info(self):
        """What the proof windows show about this payment's GCash reference number, or None when none is known (a walk-in, or a booking made
        before the receipt check). The number shown is the one the customer typed (the receipt's own, when nothing was typed); when the typed
        value and the receipt differ, both are given so the staff and the owner can see what was typed and what the picture reads."""
        typed, read = self.proof_typed_reference, self.gcash_reference
        shown = typed or read
        if not shown:
            return None
        differs = self.proof_match in (self.ProofMatch.REFERENCE, self.ProofMatch.BOTH)
        amount_differs = self.proof_match in (self.ProofMatch.AMOUNT, self.ProofMatch.BOTH)
        paid = ''
        old = False
        if self.proof_paid_at:
            local = timezone.localtime(self.proof_paid_at)
            paid = f"{local:%b} {local.day}, {local:%Y}, {local.hour % 12 or 12}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"
            # A receipt already more than a day old WHEN the booking was made: the date that exposes an old (but real) payment.
            old = bool(self.created_at and self.created_at - self.proof_paid_at > timedelta(hours=24))
        flagged = self.proof_check == self.ProofCheck.NEEDS_CHECK
        return {
            'reference': _group_reference(shown),
            'digits': shown,                                    # what the Copy button copies: the plain number the GCash app searches for
            'typed': bool(typed),
            'match': self.proof_match,
            'referenceDiffers': differs,
            'amountDiffers': amount_differs,
            'receiptReference': _group_reference(read) if differs and read else '',
            'typedAmount': _peso_text(self.proof_typed_amount) if self.proof_typed_amount is not None else '',
            'receiptAmount': _peso_text(self.proof_amount) if amount_differs and self.proof_amount is not None else '',
            'paidAt': paid,                                     # the date and time printed on the receipt, as the reader found it
            'paidOld': old,
            'check': self.proof_check,
            # Why the receipt check sent this booking to the staff (its own words), so the owner sees it next to the number.
            'notes': [line for line in (self.proof_notes or '').split('\n') if line.strip()][:2] if flagged else [],
        }

    @property
    def reference_info_json(self):
        """reference_info as JSON text for a page's script ('null' when there is none); escape it with escapejs."""
        return json.dumps(self.reference_info)

    @property
    def proof_search_text(self):
        """The words the admin lists' search boxes match for this booking's payment: every reference number it carries, as plain digits AND
        grouped the way GCash prints it, so the owner can paste either from her GCash app."""
        words = []
        for number in (self.proof_typed_reference, self.gcash_reference):
            if number and number not in words:
                words.append(number)
                grouped = _group_reference(number)
                if grouped != number:
                    words.append(grouped)
        return ' '.join(words)

    @property
    def deposit_returned_on(self):
        """The day the security deposit counts as returned, or None while it is still held.

        There is no "return deposit" step: the shop settles the deposit in person, so it
        follows the gowns -- returned the moment every gown on the booking is. Worked out
        here rather than stored, so it can never disagree with the gowns. A deposit that
        was already marked returned by hand (deposit_returned_at, from before this was
        automatic) keeps its recorded date."""
        if self.deposit_returned_at:
            return timezone.localtime(self.deposit_returned_at).date()
        # A gown cancelled before pick-up never went out, so it can't hold the deposit up: what counts is every gown
        # that was really rented.
        items = [i for i in self.items.all() if i.stage != ReservationItem.Stage.CANCELLED]
        if not items or any(i.stage != ReservationItem.Stage.RETURNED for i in items):
            return None
        return max(i.returned_on or timezone.localtime(i.updated_at).date() for i in items)

    @property
    def deposit_is_returned(self):
        return self.deposit_returned_on is not None
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['notebook_number'], condition=~models.Q(notebook_number=''),
                name='reservation_notebook_number_unique',
            ),
        ]

    def __str__(self):
        return f'{self.reference_code} — {self.customer_name}'

    @property
    def has_account(self):
        """Whether a website account is behind this booking (messages and emails can reach the customer)."""
        return self.customer_id is not None

    @property
    def is_staff_recorded(self):
        """Recorded by staff in the admin (a walk-in) rather than booked on the website."""
        return self.channel != self.Channel.ONLINE

    def receipts_cover_payment(self, receipts=None):
        """Whether the shop's own receipts (ReceiptRecord) cover both the rental and the deposit: one receipt for both, or
        one for each. A receipt nobody labelled says nothing either way. `receipts` lets a page pass its prefetched rows."""
        labels = {r.covers for r in (self.receipt_records.all() if receipts is None else receipts)}
        if ReceiptRecord.Covers.BOTH in labels:
            return True
        return ReceiptRecord.Covers.RENTAL in labels and ReceiptRecord.Covers.DEPOSIT in labels

    def next_receipt_covers(self, receipts=None):
        """What the NEXT receipt the shop attaches to this booking is for -- decided here, never asked, so staff can't label it
        wrongly. An online booking's receipt is always the full one (it lists the rental and the deposit). A walk-in that only
        paid the deposit is still waiting for its rental receipt; once both are there (or one full receipt), a further one is
        simply another copy of the full payment. Nothing attached yet: the full receipt."""
        covers = ReceiptRecord.Covers
        if not self.is_staff_recorded:
            return covers.BOTH
        labels = {r.covers for r in (self.receipt_records.all() if receipts is None else receipts)}
        if covers.BOTH in labels or {covers.RENTAL, covers.DEPOSIT} <= labels:
            return covers.BOTH
        if covers.DEPOSIT in labels:
            return covers.RENTAL
        if covers.RENTAL in labels:
            return covers.DEPOSIT
        return covers.BOTH

    def receipts_complete(self, receipts=None):
        """True when this booking already has ALL the shop receipts it needs, so no further photo can be attached (a wrong photo is fixed
        with Replace instead). An online booking needs one (the full receipt); a walk-in needs one full receipt, or the deposit receipt
        plus the rental receipt. A receipt nobody labelled completes nothing for a walk-in (see next_receipt_covers)."""
        covers = ReceiptRecord.Covers
        rows = list(self.receipt_records.all() if receipts is None else receipts)
        if not self.is_staff_recorded:
            return bool(rows)
        labels = {r.covers for r in rows}
        return covers.BOTH in labels or {covers.RENTAL, covers.DEPOSIT} <= labels

    def receipt_proofs(self, deposit_only=False):
        """The shop's own receipt photos (ReceiptRecord) as plain dicts for the admin's proof popups. A booking recorded at the
        counter has no GCash screenshot from the customer, so these photos ARE its proof of payment. `deposit_only` keeps the
        receipts that cover the security deposit ("Deposit" and the full one); a receipt nobody labelled says nothing
        either way for a walk-in (see receipts_cover_payment), so it is left out there -- an online booking's unlabelled
        receipt is the shop's full receipt, like every online one now. Oldest first, the order they were handed over.
        "by" is the staff member who attached it ("Removed account" once that account is gone), as All Bookings shows it;
        the pages that call this load receipt_records with select_related("uploaded_by") so it costs no extra query per receipt."""
        staff_recorded = self.is_staff_recorded
        rows = []
        for rec in sorted(self.receipt_records.all(), key=lambda r: (r.uploaded_at, r.id)):
            name = ReceiptRecord.name_for(rec.covers, staff_recorded)
            if deposit_only and name not in (ReceiptRecord.Covers.DEPOSIT, ReceiptRecord.PAID_IN_FULL):
                continue
            rows.append({
                "url": rec.photo_url,
                "label": f"{name} receipt" if name else "Receipt (not labelled)",
                "when": date_format(timezone.localtime(rec.uploaded_at), "M j, Y · g:i A"),
                "by": staff_display_name(rec.uploaded_by) if rec.uploaded_by_id else "Removed account",
            })
        return rows

    @property
    def payment_breakdown(self):
        """What was actually paid, and the rest of the booking's price, for the admin's payment screens -- or None when this
        booking's amounts can't be split (then every screen keeps showing the single total, as it always did).

        The total (e.g. P7,500) is the gown's rental price (P5,500) plus the security deposit (P2,000), but an online
        customer only pays the deposit on the website, by GCash: that is the amount staff verify against the screenshot, so
        that is the headline, with the gown's price shown beside it. A walk-in pays at the shop: the shop's receipts say whether
        that was everything or only the deposit (see next_receipt_covers). Nothing here is saved; it is worked out from the
        booking's own amounts, so it can never disagree with them."""
        deposit, rental, total = self.security_deposit, self.rental_subtotal, self.total_amount
        if not (deposit > 0 and rental > 0 and rental + deposit == total):
            return None
        # Booked extra days over the deposit, paid with the booking (0 for most): its own line, counted in what was paid and in the total.
        over = self.upfront_extra if self.pays_over_deposit else 0

        def lines(deposit_note, rental_note):
            out = [{"label": "Security deposit", "note": deposit_note, "amount": _peso_text(deposit)}]
            if over:
                out.append({"label": "Extra days over the deposit", "note": deposit_note, "amount": _peso_text(over)})
            return out + [{"label": "Gown rental", "note": rental_note, "amount": _peso_text(rental)}]

        both = f"Gown {_peso_text(rental)} + Deposit {_peso_text(deposit)}" + (f" + Extra days {_peso_text(over)}" if over else "")
        if self.is_staff_recorded:
            covers = ReceiptRecord.Covers
            labels = {r.covers for r in self.receipt_records.all()}
            # What the shop RECORDED (the Payments page): the money received at the counter says what was paid. Old bookings, made when a
            # shop receipt photo said it instead, keep reading their photos' labels.
            received = sum((p.amount - p.extra_part for p in self.payments.all()
                            if not p.is_void and p.direction == Payment.Direction.IN
                            and p.purpose in (Payment.Purpose.DEPOSIT, Payment.Purpose.FULL, Payment.Purpose.BALANCE)), 0)
            if received >= total or covers.BOTH in labels or {covers.RENTAL, covers.DEPOSIT} <= labels:
                paid, paid_label, deposit_note, rental_note = total + over, "Paid at the shop", "paid at the shop", "paid at the shop"
                sub = both
            elif received >= deposit or covers.DEPOSIT in labels:
                paid, paid_label, deposit_note, rental_note = deposit + over, "Deposit paid at the shop", "paid at the shop", "still to pay"
                sub = f"Gown {_peso_text(rental)} still to pay · Total {_peso_text(total + over)}"
            else:
                return None
        else:
            # Paid on PayMongo's checkout page: the recorded payments say what was paid (the deposit alone, or everything), not the old assumption that
            # an online customer only ever pays the deposit.
            online = [p for p in self.payments.all() if p.provider in ('paymongo', 'xendit') and p.direction == Payment.Direction.IN and not p.is_void]
            if online:
                how = (online[0].get_method_display() or '').replace(' (online payment)', '')
                core = (Payment.Purpose.DEPOSIT, Payment.Purpose.FULL, Payment.Purpose.BALANCE)
                received = sum((p.amount - p.extra_part for p in self.payments.all()
                                if not p.is_void and p.direction == Payment.Direction.IN and p.purpose in core), 0)
                if received >= total:
                    everything_online = any(p.purpose == Payment.Purpose.FULL for p in online)
                    paid_label = f"Paid in full online ({how})" if everything_online else "Paid in full (deposit online, rental at the shop)"
                    deposit_note = f"paid online ({how})"
                    rental_note = f"paid online ({how})" if everything_online else "paid at the shop"
                    paid, sub = total + over, both
                else:
                    paid, paid_label, deposit_note, rental_note = deposit + over, f"Deposit paid online ({how})", f"paid online ({how})", "to pay at the shop"
                    sub = f"Gown {_peso_text(rental)} still to pay · Total {_peso_text(total + over)}"
                return {
                    "paidLabel": paid_label, "headline": _peso_text(paid), "sub": sub,
                    "lines": lines(deposit_note, rental_note),
                    "total": _peso_text(total + over),
                }
            by = " by GCash" if self.payment_method == self.PaymentMethod.GCASH else ""
            paid, paid_label, deposit_note = deposit + over, f"Paid online{by}", f"paid online{by}"
            if self.status == self.Status.REJECTED:
                # The shop did not accept that payment: it was only sent, not paid.
                paid_label, deposit_note = f"Sent online{by} (rejected)", f"sent online{by}, rejected"
            # A receipt the shop attached to an online booking is its full one (rental + deposit): the gown was paid at the shop.
            rental_note = "paid at the shop" if self.receipt_records.all() else "to pay at the shop"
            sub = f"Gown {_peso_text(rental)} · Total {_peso_text(total + over)}"
        return {
            "paidLabel": paid_label,
            "headline": _peso_text(paid),
            "sub": sub,
            "lines": lines(deposit_note, rental_note),
            "total": _peso_text(total + over),
        }

    @property
    def payment_breakdown_json(self):
        """payment_breakdown as JSON text for a page's script ('null' when there is none); escape it with escapejs."""
        return json.dumps(self.payment_breakdown)

    @property
    def receipts_json(self):
        """Every receipt photo, as JSON text for a template to hand to the page's script (escape it with escapejs)."""
        return json.dumps(self.receipt_proofs())

    @property
    def deposit_receipts_json(self):
        """Only the receipts that cover the security deposit, as JSON text (the Security Deposits page)."""
        return json.dumps(self.receipt_proofs(deposit_only=True))

    @property
    def display_customer_name(self):
        """The name to show admin: prefers the customer's CURRENT profile name over
        `customer_name` (a frozen snapshot of whatever they were called at submission
        time). Without this, renaming your account in Profile leaves every past
        reservation showing the old name in admin, while Client List (which always
        reads the live profile) shows the new one -- looking like the customer
        vanished. Falls back to the snapshot only if the live profile has no name set."""
        profile = getattr(self.customer, 'profile', None)
        if profile and profile.display_name:
            return profile.display_name
        return self.customer_name

    @property
    def booked_as_name(self):
        """The name this reservation was actually submitted under, but only returned
        when it differs from the customer's current display name -- lets templates
        show 'Booked as: X' without duplicating the comparison logic five times."""
        frozen = (self.customer_name or '').strip()
        live = (self.display_customer_name or '').strip()
        if frozen and frozen.casefold() != live.casefold():
            return frozen
        return ''

    @property
    def payment_state(self):
        """One label for 'where is this customer's deposit at?', derived from the
        fields that already exist so it can never drift out of sync.

        There is deliberately no `is_paid` column: the admin's single Approve
        action is what turns Pending into Confirmed, so 'verified' IS the status.
        Returns '' for cancelled reservations, where a payment badge is noise."""
        if self.status == self.Status.CANCELLED:
            return ''
        if self.status == self.Status.REJECTED:
            return 'Payment Rejected'
        if self.status == self.Status.PENDING:
            return 'Payment Under Review' if self.payment_proof_url else 'Not Paid'
        return 'Payment Verified'

    @property
    def owner_check_open(self):
        """True while this booking waits for the owner to look in her GCash (a rejected, released or cancelled booking no longer does)."""
        return self.owner_check == self.OwnerCheck.PENDING and self.status not in (self.Status.REJECTED, self.Status.CANCELLED)

    @property
    def waiting_for_owner(self):
        """True while the staff cannot approve this booking yet: the customer paid the OWNER's GCash and she has not confirmed the money arrived
        (or the payment went to the manager's GCash but that account is out of service, so it falls back to her). Rejecting is never held back.
        A list view can set `_waiting_for_owner` on each row once, so this does not ask the database for every row (reservations/owner_check.py)."""
        cached = getattr(self, '_waiting_for_owner', None)
        if cached is not None:
            return cached
        from reservations import owner_check
        return owner_check.blocks_approval(self)

    @property
    def paid_to_manager(self):
        return self.proof_account == self.ProofAccount.MANAGER

    @property
    def proof_badges(self):
        """Small chips for the staff lists: what the receipt check found and what the owner found in her GCash. [(kind, text), ...] where kind is
        ok / warn / bad / muted. Empty for a booking with no online GCash proof (a walk-in, or one made before the check existed)."""
        badges = []
        if self.proof_check == self.ProofCheck.PASS:
            badges.append(('ok', 'Pre-checked'))
        elif self.proof_check == self.ProofCheck.NEEDS_CHECK:
            badges.append(('warn', 'Needs check'))
        if self.proof_old_number:
            badges.append(('warn', 'Old GCash number'))
        if self.owner_check == self.OwnerCheck.RECEIVED:
            badges.append(('ok', 'Payment confirmed in the manager\u2019s GCash \u2713' if self.paid_to_manager else 'Payment confirmed by owner \u2713'))
        elif self.owner_check == self.OwnerCheck.NOT_FOUND:
            badges.append(('bad', 'Payment not found in the GCash \u2717' if self.paid_to_manager else 'Owner: payment not found \u2717'))
        elif self.owner_check_open:
            if self.waiting_for_owner:
                badges.append(('warn', 'Waiting for the owner to confirm'))
            elif self.paid_to_manager:
                badges.append(('muted', 'Paid to the manager\u2019s GCash'))
            else:
                badges.append(('muted', 'Not checked by owner yet'))
        return badges

    @property
    def proof_window(self):
        """What the proof window shows about the receipt check and the owner's check (None for a booking that has neither)."""
        if not (self.proof_check or self.owner_check):
            return None
        paid = ''
        if self.proof_paid_at:
            local = timezone.localtime(self.proof_paid_at)
            paid = f"{local:%b} {local.day}, {local:%Y}, {local.hour % 12 or 12}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"
        number = self.proof_recipient
        if "?" in number:
            number = number.replace("?", "•")             # GCash hid some digits on the receipt (09?????5992): show them as dots, the way it printed them
        elif len(number) == 11:
            number = f"{number[:4]} {number[4:7]} {number[7:]}"
        return {
            'check': self.proof_check,
            'checkLabel': dict(self.ProofCheck.choices).get(self.proof_check, ''),
            'notes': [line for line in (self.proof_notes or '').split('\n') if line.strip()],
            'ownerCheck': self.owner_check,
            'ownerOpen': self.owner_check_open,
            'ownerLabel': dict(self.OwnerCheck.choices).get(self.owner_check, ''),
            'account': self.proof_account,
            'accountLabel': dict(self.ProofAccount.choices).get(self.proof_account, ''),
            'waiting': bool(self.owner_check_open and self.waiting_for_owner),
            'amount': _peso_text(self.proof_amount) if self.proof_amount is not None else '',
            'paidAt': paid,
            'recipient': number,
            'oldNumber': self.proof_old_number,
        }

    @property
    def proof_window_json(self):
        """proof_window as JSON text for a page's script ('null' when there is none); escape it with escapejs."""
        return json.dumps(self.proof_window)

    @property
    def can_upload_proof(self):
        """Whether the customer may still attach proof of payment. Only while the
        reservation is awaiting review AND nothing was uploaded before -- staff
        verify against the original upload, so it must not be replaceable."""
        return self.status == self.Status.PENDING and not self.payment_proof_url

    @classmethod
    def next_reference_code(cls):
        """RSV-{year}-{sequence:04d}, sequence restarting each year.

        Delegates the actual number to ReservationSequence, which hands out each
        integer under a row lock -- see that model's docstring for why "read the
        last reservation, add one" (this method's old implementation) is not safe
        enough here. gowns.models.Gown.next_tracking_number had the identical shape
        of race for Gown.gown_id and now uses the same fix (gowns.models.GownSequence).
        """
        year = timezone.now().year
        n = ReservationSequence.next_value_for(year)
        return f'RSV-{year}-{n:04d}'

    def save(self, *args, **kwargs):
        if self.reference_code:
            # Every later save (approve/reject, deposit release, staff editing dates on
            # a sibling item, ...) already has a code from creation -- nothing below
            # this line ever runs for those, exactly as before this fix.
            super().save(*args, **kwargs)
            return

        # ReservationSequence.next_value_for() already guarantees each caller gets a
        # distinct number -- no two requests can ever be handed the same one, at any
        # level of concurrency (see that model's docstring). This retry is pure
        # defense in depth for a scenario that shouldn't be reachable through normal
        # use at all: a reference_code collision from data that didn't go through
        # this method (a hand-edited row, a bad fixture, a restored backup). The
        # inner transaction.atomic() is what makes even that retry safe: this save
        # already runs inside reservation_submit's outer atomic() block, and in
        # Postgres one failed INSERT poisons the entire transaction until it rolls
        # back -- wrapping just this save opens a SAVEPOINT instead, so a collision
        # unwinds only to here (the same reasoning ReservationStatusEvent.record()
        # already relies on).
        for _attempt in range(3):
            self.reference_code = self.next_reference_code()
            try:
                with transaction.atomic():
                    super().save(*args, **kwargs)
                return
            except IntegrityError:
                continue

        raise IntegrityError(
            "Could not generate a unique reservation reference code. Please try "
            "submitting again, and let the shop know if this keeps happening."
        )


class ReservationSequence(models.Model):
    """One row per year, holding the next reservation number to hand out for it.

    Why this exists instead of "read the highest existing reference_code, add one"
    (the old approach, and what Gown.next_tracking_number used to do too before it got
    the same fix): that approach reads, then separately writes, with nothing stopping
    two concurrent requests from both reading the same "last" value before either has
    written -- a real race, confirmed in this project by racing 8 concurrent checkouts
    against live Postgres. A bounded retry-on-collision (this model's own first draft)
    narrows the failure window but does not close it: past some number of simultaneous
    requests, retries run out and a customer sees a crash anyway.

    `next_value_for()` closes it completely with `select_for_update()` -- a real
    Postgres row lock. A second transaction asking for the same year's row does not
    race the first: it simply waits its turn until the first commits, then reads the
    value the first one left behind. There is no number of simultaneous requests that
    can defeat this; they just queue up, which is exactly what should happen when two
    people want consecutive numbers.

    The one thing a row lock cannot protect is a row that does not exist yet (nothing
    to lock for the very first reservation of a new year) -- `get_or_create` covers
    exactly that gap: it is written to catch the unique-constraint violation from two
    requests both trying to create year 2027's row for the first time, and re-fetch
    the winner's row instead of erroring, which is a standard, well-tested part of
    Django itself, not something this project is trusting to chance.
    """

    year = models.PositiveIntegerField(unique=True)
    next_value = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f'{self.year}: next is {self.next_value}'

    @classmethod
    def next_value_for(cls, year):
        with transaction.atomic():
            cls.objects.get_or_create(year=year)
            # Locks THIS row until this transaction commits -- any other request
            # asking for the same year blocks here rather than racing.
            row = cls.objects.select_for_update().get(year=year)
            value = row.next_value
            row.next_value = value + 1
            row.save(update_fields=['next_value'])
        return value


class ReservationItem(models.Model):
    """One gown within a reservation. Stores a denormalized snapshot so the record
    is self-contained even though the public catalog isn't DB-backed yet; the
    optional gown FK links to a real inventory row when a name match is found."""

    class Stage(models.TextChoices):
        """The gown's current real-world condition, set directly by staff from the
        Rental Schedule calendar -- matches the 4 states on the calendar legend.
        RETURNED is a separate terminal state: once set, the booking drops off the
        calendar entirely (handled in arabela_admin.views._calendar_events)."""
        PICKUP = 'Pick-up', 'Pick-up'
        RESERVED = 'Reserved', 'Reserved'
        RETURN = 'Return', 'Return'
        OVERDUE = 'Overdue', 'Overdue'
        RETURNED = 'Returned', 'Returned'
        # Staff cancelled just this gown (it was never picked up); the rest of the booking carries on. Like RETURNED
        # it is final -- the gown holds nothing any more -- but it never went out, so it is NOT a rental that was
        # completed. See reservations.cancellation.cancel_gowns_by_staff.
        CANCELLED = 'Cancelled', 'Cancelled'

    # Stages in which a gown holds nothing any more: it is back, or its booking line was cancelled. Every "is this gown
    # still tied up?" question (availability, the calendar, conflicts, the delete guard) leaves these out.
    FINISHED_STAGES = (Stage.RETURNED, Stage.CANCELLED)

    class ReturnCondition(models.TextChoices):
        """Set by staff on the Mark Returned step (post-checkin, before the deposit is
        released). Values mirror gowns.Gown.Condition so they map 1:1 onto the gown's
        own condition field without a cross-app import."""
        GOOD = 'Good', 'Good'
        FAIR = 'Fair', 'Fair'
        NEEDS_REPAIR = 'Needs Repair', 'Needs Repair'

    reservation = models.ForeignKey(
        Reservation, on_delete=models.CASCADE, related_name='items'
    )
    gown = models.ForeignKey(
        'gowns.Gown', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reservation_items'
    )
    gown_name = models.CharField(max_length=150)
    gown_slug = models.CharField(max_length=150, blank=True)
    # The gown's picture as it was when this line was booked. The link above is cleared when the gown is deleted from the catalog (a reset, a
    # removal), and a booking line only keeps the gown's NAME, so without this its page had no picture of what was reserved.
    gown_photo_url = models.URLField(max_length=500, blank=True, default='', db_default='')
    size = models.CharField(max_length=20, blank=True)
    rental_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    rental_date = models.DateField()   # start of the rental window (default: 2 days before event)
    event_date = models.DateField(null=True, blank=True)  # the customer's event/reserved day
    return_date = models.DateField()   # end of the rental window (default: 2 days after event)
    overdue_date = models.DateField(null=True, blank=True)  # the day the Overdue status marks
    # The shop's first-ever scheduled window, set once at submission and never touched again --
    # not even by a later staff reschedule (see reservation_item_reschedule_view), which is free
    # to adjust rental_date/return_date above for legitimate reasons (a correction, a genuine
    # change of plan) without erasing what was originally promised. This is what "2 days before/
    # after the event" is compared against when a customer picks up early or returns late, so
    # staff always have a record of the original alongside whatever actually happened
    # (picked_up_on/returned_on below) -- without the system doing that Php 200/day math itself.
    original_rental_date = models.DateField(null=True, blank=True)
    original_return_date = models.DateField(null=True, blank=True)
    # The FREE days of a booking made since the customer chooses all three dates (reservations/ledger.py): two days before the event the
    # customer first booked, and two days after it. Each booked day outside them costs the booking's late_fee_per_day, taken from the
    # security deposit. Set once at booking and never moved (not even when staff change the event date). Both empty = a booking made before
    # this rule, which keeps the old money rules (only days after the original return date were ever charged).
    free_pickup_date = models.DateField(null=True, blank=True)
    free_return_date = models.DateField(null=True, blank=True)
    # The return day the customer chose when it is BEFORE free_return_date (1 day after the event). Returning any day up to free_return_date is
    # free, so the gown stays booked until then and return_date holds that day; this only remembers what the customer said they planned.
    planned_return_date = models.DateField(null=True, blank=True)
    # Every saved change of the return date by staff: [{"from": ISO, "to": ISO, "by": name, "at": ISO datetime}]. From the third one the
    # Audit Log line is marked important, so the owner sees who keeps moving it (arabela_admin/audit_describe.py). Nullable so an older
    # deployment sharing the database can still insert rows.
    return_changes = models.JSONField(default=list, blank=True, null=True)
    # The Rental Schedule shows ONE status at a time, chosen by staff (item.stage). Each
    # status maps to its own dates: Pick-up = rental_date..event_date-1, Reserved = the
    # event day, Return = event_date+1..return_date, Overdue = the overdue_date. All four
    # dates are editable by staff (event_date defaults to rental_date + 2, overdue_date to
    # return_date). Ranges are derived in arabela_admin.views._stage_segment.
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.PICKUP)
    # The day the gown actually left/came back, as staff record it -- may differ from
    # rental_date/return_date (see original_rental_date/original_return_date above).
    # Auto-stamped to today the first time reservation_item_reschedule_view moves the
    # stage off Pick-up (or reservation_item_mark_returned_view runs), but also directly
    # editable by staff afterward (reservation_item_set_actual_date_view) in case the
    # paperwork was done a day later than the real handoff.
    picked_up_on = models.DateField(null=True, blank=True)
    returned_on = models.DateField(null=True, blank=True)
    return_condition = models.CharField(
        max_length=20, choices=ReturnCondition.choices, blank=True
    )
    # "Ready" on Gown Monitoring (arabela_admin/monitoring.py): staff prepared this gown for its pick-up. It only counts while the pick-up day is still the
    # day it was prepared for (ready_for), so moving the pick-up date makes it "To prepare" again by itself. ready_told_for remembers the pick-up day the
    # customer was already told about, so taking the mark back and pressing Ready again never sends the same news twice.
    ready_at = models.DateTimeField(null=True, blank=True)
    ready_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    ready_for = models.DateField(null=True, blank=True)
    ready_told_for = models.DateField(null=True, blank=True)
    # A gown the shop cancelled while the booking carried on (under the refund rule): its rental is no longer counted, and rental already paid for it
    # is given back. False = not cancelled, or cancelled before the rule (its rental stays counted).
    rental_dropped = models.BooleanField(default=False, db_default=False)
    # Bumped on every save (creation + every staff stage/date edit) -- lets the
    # customer's "new activity" badge tell freshly-changed items apart from ones
    # they've already seen, without a separate read/unread table.
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['rental_date']

    def __str__(self):
        return f'{self.gown_name} ({self.rental_date} → {self.return_date})'

    @property
    def effective_event_date(self):
        """The customer's event day. Bookings made since the customer chooses the event, pick-up and return save it (event_date). An
        older booking never saved it at checkout, so until staff set one it's worked out from the return date: checkout always booked
        return = event + 2, while pick-up got pinned to today for a booking made less than 2 days out -- so "pick-up + 2" (the old
        guess) lands on the wrong day for those, and "return - 2" is right for every such booking. Never earlier than the pick-up
        date, so the four dates stay in order even if staff have since shrunk the window."""
        if self.event_date:
            return self.event_date
        return max(self.rental_date, self.return_date - timedelta(days=2))

    @property
    def uses_free_days(self):
        """A booking made under the free-days rule (free_pickup_date / free_return_date): every booked day outside them is charged."""
        return self.free_pickup_date is not None and self.free_return_date is not None

    @property
    def extra_days_count(self):
        """A free-days booking's booked days outside its free days (reservations/ledger.booked_extra_days), 0 for an older booking."""
        from reservations import ledger
        return sum(ledger.booked_extra_days(self))

    @property
    def extra_days_fee(self):
        """What those days take out of the security deposit, at the booking's fee per day (a Decimal, 0 when none)."""
        return Decimal(self.extra_days_count * int(self.reservation.late_fee_per_day or 0))

    @property
    def shown_return_date(self):
        """The return day to show the CUSTOMER: the day they chose (planned_return_date, 1 day after the event) when it is earlier than the
        free return day the gown is kept until, else the return date. Once staff move the dates so that day no longer fits (a return that is not
        the free return day, or an event or pick-up after it), the return date is the one shown."""
        planned = self.planned_return_date
        if (planned and planned < self.return_date and self.return_date == self.free_return_date
                and planned >= self.rental_date and planned > self.effective_event_date):
            return planned
        return self.return_date

    @property
    def effective_return_date(self):
        """The date this item's rental actually ended, or is scheduled to end.
        Mirrors effective_event_date's shape: returned_on is the ground truth once
        the gown is physically back; until then return_date is the plan."""
        return self.returned_on or self.return_date

    @property
    def is_ready_for_pickup(self):
        """Staff marked this gown ready for the pick-up it is still waiting for (the shop's pages and the customer's own page ask this one question)."""
        return self.stage == self.Stage.PICKUP and self.ready_at is not None and self.ready_for == self.rental_date

    @property
    def can_customer_cancel(self):
        """Whether the customer can self-cancel from this item. Cancelling is a
        whole-Reservation action (mirrors reservation_approve/reject_view, which act
        on the whole reservation, not one item), so eligibility requires every sibling
        item in the same reservation to still be un-picked-up -- otherwise cancelling
        would interrupt an already-active rental for a bundled gown. Checking
        reservation.status too (not just stage) matters because a Rejected
        reservation's item still sits at the default stage='Pick-up' forever."""
        if self.stage != self.Stage.PICKUP:
            return False
        if self.reservation.status not in (
            Reservation.Status.PENDING, Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE,
        ):
            return False
        # A sibling the shop already cancelled never went out, so it doesn't stop the customer cancelling the rest.
        return not self.reservation.items.exclude(stage__in=[self.Stage.PICKUP, self.Stage.CANCELLED]).exists()

    @property
    def gown_held_by_another_active_item(self):
        """Whether the physical gown this item points at is still legitimately needed
        Reserved on account of a DIFFERENT booking -- checked before anything downgrades
        Gown.status back to Available on THIS item's account (mark returned, customer
        cancel). The same physical gown can be booked again for a later, non-overlapping
        date range, so a second ReservationItem pointing at it is normal; finishing THIS
        one must never clobber the status a still-active sibling booking depends on.
        Excludes finished items (Returned, or cancelled by the shop) and Rejected/Cancelled reservations. Also excludes
        PENDING ones: Gown.status only becomes Reserved when a booking is APPROVED
        (reservation_approve_view), so a booking still awaiting approval never made the
        gown Reserved and must not keep it Reserved either -- before this, one stale
        Pending booking (dates long past, never approved or rejected) left a returned
        gown stuck on Reserved in the catalog forever. Approving it later flips the gown
        to Reserved again, so nothing is lost. (Availability on the website is a
        separate check, _find_available_unit, which still counts Pending bookings.)"""
        if not self.gown_id:
            return False
        return (
            ReservationItem.objects.filter(gown_id=self.gown_id)
            .exclude(id=self.id)
            .exclude(stage__in=self.FINISHED_STAGES)
            .exclude(reservation__status__in=[
                Reservation.Status.PENDING, Reservation.Status.REJECTED, Reservation.Status.CANCELLED,
            ])
            .exists()
        )


class ReservationStatusEvent(models.Model):
    """One row per real thing that happened to a reservation, in order, forever.

    Reservation.status / ReservationItem.stage only ever hold the CURRENT state, and
    the two timestamps that do exist (reviewed_at, deposit_returned_at) are each
    overwritten on the next save -- so before this model there was no way to answer
    "when was this approved?" or "what happened to this booking, in what order?".
    Rows are only ever appended, never edited, which is what makes the customer's
    order timeline (and any later dispute: "you approved it on the 5th") trustworthy.

    item is null for things that happened to the whole reservation (submitted,
    approved, rejected, cancelled, deposit returned) and set for things that happened
    to one specific gown in it (marked returned, status moved on the schedule), so a
    single item's timeline is its own events plus the reservation-wide ones.

    label is written at the moment it happens rather than derived later, so a wording
    change to Status/Stage never silently rewrites history that customers already saw.
    """

    class Actor(models.TextChoices):
        CUSTOMER = 'Customer', 'Customer'
        STAFF = 'Staff', 'Staff'
        SYSTEM = 'System', 'System'

    reservation = models.ForeignKey(
        Reservation, on_delete=models.CASCADE, related_name='status_events'
    )
    item = models.ForeignKey(
        ReservationItem, on_delete=models.CASCADE, related_name='status_events',
        null=True, blank=True,
    )
    label = models.CharField(max_length=200)
    # Optional second line under the label (rejection reason, return condition) --
    # kept separate so the timeline can style it as secondary text.
    detail = models.CharField(max_length=300, blank=True)
    actor = models.CharField(max_length=20, choices=Actor.choices, default=Actor.SYSTEM)
    occurred_at = models.DateTimeField(default=timezone.now)
    # False only for events reconstructed by the backfill migration from pre-existing
    # date-only columns (picked_up_on / returned_on), where the real time of day was
    # never recorded. Those render as a date with no time rather than showing an
    # invented "8:00 AM" -- a timeline people may rely on must never make a time up.
    time_known = models.BooleanField(default=True)
    # A note for the shop's own records that the customer never sees -- e.g. a gown
    # checked in needing repair. Shown on every admin timeline, but left out of the
    # customer's own order page (reservations.timeline.for_item).
    staff_only = models.BooleanField(default=False)

    class Meta:
        ordering = ['occurred_at', 'id']
        indexes = [models.Index(fields=['reservation', 'occurred_at'])]

    def __str__(self):
        return f'{self.reservation.reference_code}: {self.label} @ {self.occurred_at}'

    @classmethod
    def record(cls, reservation, label, *, item=None, detail='', actor=Actor.SYSTEM, staff_only=False, quiet=False):
        """The one way events get written. `quiet`: the customer was already told about this by another notice, so this line sends no email of its
        own (accounts.customer_emails reads event.quiet; nothing is stored). Deliberately swallows its own errors: a
        timeline is a record OF the action, never a reason the action itself fails --
        an approval must still go through even if writing its history row somehow
        can't.

        The inner atomic() is what makes that swallow safe. Several callers already run
        inside a transaction; a failed INSERT there marks the whole transaction broken,
        so merely catching the exception would turn a missing history row into a
        TransactionManagementError on the caller's very next query. The savepoint keeps
        the failure contained to this INSERT and leaves the caller's work intact."""
        try:
            with transaction.atomic():
                event = cls(reservation=reservation, item=item, label=label, detail=(detail or '')[:300], actor=actor, staff_only=staff_only)
                event.quiet = quiet
                event.save(force_insert=True)
                return event
        except Exception:
            return None


class ReminderRun(models.Model):
    """Singleton (always pk=1) recording the last day the due-date reminder sweep ran.

    This project has no scheduler -- no cron, no Celery, nothing that runs on its own
    clock -- so the sweep is triggered by staff opening the admin dashboard, and this
    row is what stops it running again on every page load for the rest of the day.

    `last_run_on` is a DATE, not a timestamp, on purpose: the question being asked is
    "has the shop's reminder pass happened TODAY?", and a date compares cleanly against
    timezone.localdate() without any window arithmetic. The timestamp beside it is for
    humans wondering when it actually fired.
    """

    last_run_on = models.DateField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    # Purely observational -- how many reminders the most recent pass sent. Staff never
    # see this today; it exists so a "why did nobody get reminded?" question has an
    # answer that doesn't require re-running anything.
    last_sent_count = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f'Reminder sweep (last run {self.last_run_on or "never"})'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class ReceiptRecord(models.Model):
    """A manual receipt -- a physical receipt the shop itself issued (printed, written,
    or otherwise produced), photographed and attached here by staff after the fact.

    Deliberately the OPPOSITE direction from Reservation.payment_proof_url: that field
    is the CUSTOMER's own GCash screenshot, uploaded automatically at checkout, proving
    the customer paid. This is the shop's OWN record of the transaction, for its own
    bookkeeping -- staff produce it, staff attach it, and it exists independently of
    whatever the customer uploaded (or didn't; a reservation can have a manual receipt
    with no online proof at all, or the reverse).

    One reservation can end up with more than one of these over time (a redo, a second
    physical receipt for a partial payment) -- nothing here forces exactly one, matching
    how the feature's own "Replace Photo" action already treats a mistake in ONE receipt
    as something to fix in place, not a reason to enforce a stricter one-per-booking rule.
    """

    class Covers(models.TextChoices):
        """What a receipt is for. A booking paid at the counter has one receipt for both, or one for each."""
        RENTAL = 'Rental', 'Rental'
        DEPOSIT = 'Deposit', 'Deposit'
        BOTH = 'Rental + deposit', 'Rental + deposit'

    # What staff READ for the full receipt. The stored word stays 'Rental + deposit' (older receipts keep working and nothing in
    # the database changes); only the name shown on the screens is this one.
    PAID_IN_FULL = 'Paid in full'

    @classmethod
    def name_for(cls, covers, staff_recorded=True):
        """The name of a receipt's label as staff read it: 'Paid in full' for the stored 'Rental + deposit'. A receipt nobody
        labelled is the shop's full receipt when the booking came in online (it always is now) and says nothing ('') for a
        walk-in."""
        if covers == cls.Covers.BOTH:
            return cls.PAID_IN_FULL
        if covers:
            return covers
        return '' if staff_recorded else cls.PAID_IN_FULL

    reservation = models.ForeignKey(
        Reservation, on_delete=models.CASCADE, related_name='receipt_records'
    )
    photo_url = models.URLField()
    # Blank for receipts attached before this label existed (and when staff leave it unsaid).
    covers = models.CharField(max_length=20, choices=Covers.choices, blank=True, default='', db_default='')
    # SHA-256 of the photo file exactly as it was uploaded: the same file can't be attached to a second booking, so one
    # receipt photo can't be passed off as the payment for two. Blank for receipts from before this check.
    file_hash = models.CharField(max_length=64, blank=True, default='', db_default='', db_index=True)
    # SET_NULL, not CASCADE: a staff account being removed later must never take a
    # financial record down with it -- the receipt itself still matters.
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-uploaded_at']

    def undo_window_open(self, user, now=None):
        """True when `user` attached this photo themselves and did so less than RECEIPT_UNDO_MINUTES (15) ago: they may take it back
        without the "Delete receipt photos" permission -- a photo put on the wrong booking is removed at once. Replacing the picture
        later does not extend the window (uploaded_at stays the moment the record was created)."""
        from accounts.roles import RECEIPT_UNDO_MINUTES
        if user is None or not getattr(user, 'pk', None) or self.uploaded_by_id != user.pk:
            return False
        return (now or timezone.now()) - self.uploaded_at <= timedelta(minutes=RECEIPT_UNDO_MINUTES)

    def __str__(self):
        return f'Receipt for {self.reservation.reference_code} ({self.uploaded_at:%Y-%m-%d})'


class ProofRejection(models.Model):
    """One refused payment proof, kept so an account that keeps sending bad proofs can be flagged (accounts.services.record_rejected_proof).

    Written for every way a proof gets refused: the receipt check at upload (no booking is made then, so `reservation` is empty), the
    staff rejecting a booking, and the owner finding no such payment in her GCash."""

    class Source(models.TextChoices):
        UPLOAD = 'upload', 'Receipt check'
        STAFF = 'staff', 'Staff'
        OWNER = 'owner', 'Owner'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='proof_rejections')
    reservation = models.ForeignKey(Reservation, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    source = models.CharField(max_length=10, choices=Source.choices)
    reason = models.CharField(max_length=200, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']



class CancelRequest(models.Model):
    """A staff member's request that the OWNER cancel a booking (or some of its gowns): the way a staff account without the "Cancel
    bookings" permission handles a no-show or a customer who phoned to cancel. The booking stays exactly as it is until the owner
    answers; approving runs the normal cancellation (reservations/cancellation.py), declining leaves the booking alone and tells
    staff why. Rules and the steps: reservations/cancel_requests.py."""

    class Status(models.TextChoices):
        PENDING = 'Pending', 'Waiting'
        APPROVED = 'Approved', 'Approved'
        DECLINED = 'Declined', 'Declined'
        WITHDRAWN = 'Withdrawn', 'Withdrawn'
        CLOSED = 'Closed', 'No longer possible'      # the booking changed before the owner answered (picked up, already cancelled, ...)

    reservation = models.ForeignKey(Reservation, on_delete=models.CASCADE, related_name='cancel_requests')
    # The booking lines to cancel, as ids joined by commas. Empty = the whole booking.
    item_ids = models.CharField(max_length=400, blank=True, default='')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    requested_by_name = models.CharField(max_length=150)
    reason_key = models.CharField(max_length=20)            # a key of reservations.cancellation.REASONS
    note = models.CharField(max_length=150, blank=True, default='')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    decided_by_name = models.CharField(max_length=150, blank=True, default='')
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=200, blank=True, default='')
    reminder_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at', 'id']
        constraints = [
            # One open request per booking: two staff (or a double tap) can never leave two waiting.
            models.UniqueConstraint(fields=['reservation'], condition=models.Q(status='Pending'), name='one_pending_cancel_request_per_booking'),
        ]

    @property
    def item_id_list(self):
        return [int(part) for part in self.item_ids.split(',') if part.strip().isdigit()]

    @property
    def is_whole_booking(self):
        return not self.item_ids

    def __str__(self):
        return f'Cancel request for {self.reservation.reference_code} ({self.status})'


class ReceiptSequence(models.Model):
    """One row per year, holding the next Payment Receipt number to hand out for it (PR-2026-000123).

    The same idea as ReservationSequence: the next number is read under a row lock (select_for_update), so two staff saving a payment
    at the very same moment queue up and get consecutive numbers -- never the same one."""

    year = models.PositiveIntegerField(unique=True)
    next_value = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f'{self.year}: next is {self.next_value}'

    @classmethod
    def next_number(cls, year):
        """'PR-2026-000123'. Must be called inside the transaction that saves the Payment (so a rolled-back payment hands the number back)."""
        with transaction.atomic():
            cls.objects.get_or_create(year=year)
            row = cls.objects.select_for_update().get(year=year)
            value = row.next_value
            row.next_value = value + 1
            row.save(update_fields=['next_value'])
        return f'PR-{year}-{value:06d}'


class Payment(models.Model):
    """One movement of money on a booking, recorded by staff (or, later, by the online payment service): money received from the customer,
    or money handed back to them. The booking's balance, its late fee and the deposit it can refund are all WORKED OUT from these rows
    (reservations/ledger.py) -- nobody types a balance.

    A saved payment is never edited and never deleted. A mistake is VOIDED with a reason (the row stays, marked void, with who and when),
    and its receipt prints as VOID. `snapshot` holds what the receipt says as it was the moment it was issued, so a reprint is always the
    same paper. Rows with is_legacy=True were made by the one-time backfill from what older bookings already carried (an online deposit, a
    shop receipt photo): they have no receipt number."""

    class Direction(models.TextChoices):
        IN = 'In', 'Received'
        OUT = 'Out', 'Returned to the customer'

    class Purpose(models.TextChoices):
        DEPOSIT = 'Deposit', 'Deposit'
        FULL = 'Paid in full', 'Paid in full'
        BALANCE = 'Balance', 'Balance'
        EXTRA = 'Extra', 'Late fee / extra charge'
        DEPOSIT_RETURN = 'Deposit return', 'Deposit return'

    class Method(models.TextChoices):
        CASH = 'Cash', 'Cash'
        GCASH = 'GCash', 'GCash'
        ONLINE_GCASH = 'GCash online', 'GCash (online)'          # an online booking's deposit, from the old screenshot flow
        # Paid on the online payment service's own page (PayMongo or Xendit, whichever the shop used: Payment.provider says which). The stored values keep
        # the name PayMongo (they are in the database already); the words people read do not name a service.
        PM_GCASH = 'PayMongo GCash', 'GCash (online payment)'
        PM_MAYA = 'PayMongo Maya', 'Maya (online payment)'
        PM_QRPH = 'PayMongo QR Ph', 'QR Ph (online payment)'
        PM_OTHER = 'PayMongo other', 'Online payment'            # a way of paying the shop did not offer (never expected; recorded honestly)
        OLD = 'Before Payments', 'Recorded before the Payments page'

    class Status(models.TextChoices):
        RECORDED = 'Recorded', 'Recorded'
        VOID = 'Void', 'Void'

    reservation = models.ForeignKey(Reservation, on_delete=models.CASCADE, related_name='payments')
    direction = models.CharField(max_length=3, choices=Direction.choices, default=Direction.IN)
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    method = models.CharField(max_length=20, choices=Method.choices, default=Method.CASH)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    # The part of `amount` that paid booked extra days over the deposit, received together with the deposit (or the whole booking). It counts
    # as an extra charge, never as deposit or rental (reservations/ledger.py). 0 for every other payment.
    extra_part = models.DecimalField(max_digits=10, decimal_places=2, default=0, db_default=0)
    # Money handed back (direction Out): the part of it that is gown rental (a cancelled booking or gown). It comes off the Rental Income.
    rental_part = models.DecimalField(max_digits=10, decimal_places=2, default=0, db_default=0)
    # The GCash reference number (digits only) when the method is GCash; empty for cash.
    reference = models.CharField(max_length=20, blank=True, default='')
    # NULL (not '') for a legacy row, so the unique rule below allows any number of them.
    receipt_number = models.CharField(max_length=20, unique=True, null=True, blank=True)
    paid_at = models.DateTimeField(default=timezone.now, db_index=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    recorded_by_name = models.CharField(max_length=150, blank=True, default='')
    note = models.CharField(max_length=200, blank=True, default='')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RECORDED, db_index=True)
    void_reason = models.CharField(max_length=200, blank=True, default='')
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    voided_by_name = models.CharField(max_length=150, blank=True, default='')
    voided_at = models.DateTimeField(null=True, blank=True)
    snapshot = models.JSONField(default=dict, blank=True)
    # A token the page makes once per "Save": a double click or a retried request that carries the same token returns the payment already
    # saved instead of recording a second one.
    request_id = models.CharField(max_length=40, unique=True, null=True, blank=True)
    is_legacy = models.BooleanField(default=False)
    first_printed_at = models.DateTimeField(null=True, blank=True)
    reprint_count = models.PositiveIntegerField(default=0)
    customer_notified_at = models.DateTimeField(null=True, blank=True)
    # The online payment service that moved this money ('paymongo') and its own reference (the pay_... of a payment, the ref_... of a refund).
    # Empty for money handled at the shop.
    provider = models.CharField(max_length=20, blank=True, default='')
    provider_ref = models.CharField(max_length=60, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['paid_at', 'id']
        indexes = [models.Index(fields=['reference'])]

    @property
    def is_void(self):
        return self.status == self.Status.VOID

    @property
    def is_money_in(self):
        return self.direction == self.Direction.IN

    @property
    def is_over_return(self):
        """A REFUND rather than a deposit handed back: money given back to a CANCELLED booking, or only the rental of a cancelled gown."""
        back = (self.snapshot or {}).get('return') or {}
        return self.purpose == self.Purpose.DEPOSIT_RETURN and bool(back.get('cancelled') or back.get('rental_only'))

    @property
    def title(self):
        """The receipt's heading."""
        if self.purpose != self.Purpose.DEPOSIT_RETURN:
            return 'PAYMENT RECEIPT'
        return 'REFUND' if self.is_over_return else 'DEPOSIT RETURN'

    def get_purpose_display(self):
        """The words people read for the purpose. EXTRA is whatever the security deposit did not cover (an overdue fee, a date change fee, a
        deduction): "Extra charge". The stored choice keeps its old label, so the database needs no change."""
        if self.purpose == self.Purpose.EXTRA:
            return 'Extra charge'
        if self.is_over_return:
            return 'Refund'
        return dict(self.Purpose.choices).get(self.purpose, self.purpose)

    def __str__(self):
        return f'{self.receipt_number or "legacy"} {self.direction} {self.amount} for {self.reservation.reference_code}'


class Deduction(models.Model):
    """An amount the shop takes off a customer's security deposit besides the late fee (damage, a stain, a missing item), with the reason.
    It makes the deposit the customer gets back smaller (reservations/ledger.py). Voided, never deleted, like a Payment."""

    class Status(models.TextChoices):
        RECORDED = 'Recorded', 'Recorded'
        VOID = 'Void', 'Void'

    reservation = models.ForeignKey(Reservation, on_delete=models.CASCADE, related_name='deductions')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.CharField(max_length=200)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    recorded_by_name = models.CharField(max_length=150, blank=True, default='')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RECORDED, db_index=True)
    void_reason = models.CharField(max_length=200, blank=True, default='')
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    voided_by_name = models.CharField(max_length=150, blank=True, default='')
    voided_at = models.DateTimeField(null=True, blank=True)
    request_id = models.CharField(max_length=40, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['created_at', 'id']

    @property
    def is_void(self):
        return self.status == self.Status.VOID

    def __str__(self):
        return f'Deduction {self.amount} on {self.reservation.reference_code}: {self.reason}'


class DateChangeRequest(models.Model):
    """A date change that costs more than the security deposit covers: the shop asks the customer to pay the part over it FIRST and holds the new
    days for that gown; once the money is in, the dates change by themselves (reservations/date_requests.py). A request that is not paid in
    time, cancelled or no longer needed changes nothing. Only that module changes these rows."""

    class Status(models.TextChoices):
        WAITING = 'waiting', 'Waiting for payment'
        APPLIED = 'applied', 'Paid, dates changed'
        EXPIRED = 'expired', 'Not paid in time'
        CANCELLED = 'cancelled', 'Cancelled by the shop'
        REPLACED = 'replaced', 'Replaced by a newer request'
        CLOSED = 'closed', 'No longer needed'

    reservation = models.ForeignKey(Reservation, on_delete=models.CASCADE, related_name='date_requests')
    item = models.ForeignKey(ReservationItem, on_delete=models.CASCADE, related_name='date_requests')
    gown = models.ForeignKey('gowns.Gown', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    # What the staff member asked to change (empty = that date stays as it is).
    want_pickup = models.DateField(null=True, blank=True)
    want_return = models.DateField(null=True, blank=True)
    want_event = models.DateField(null=True, blank=True)
    # The dates when it was asked (the request is dropped when they changed meanwhile).
    from_pickup = models.DateField()
    from_return = models.DateField()
    # The days this gown is held for while the request waits: from the new pick-up to the new return and the cleaning days after it.
    hold_start = models.DateField()
    hold_end = models.DateField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.WAITING, db_index=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    requested_by_name = models.CharField(max_length=150, blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=300, blank=True, default='')
    payment = models.ForeignKey('Payment', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['reservation'], condition=models.Q(status='waiting'), name='datechange_one_waiting_per_booking'),
        ]

    @property
    def is_open(self):
        """Still waiting for the money and inside its time."""
        return self.status == self.Status.WAITING and self.expires_at > timezone.now()

    def __str__(self):
        return f'{self.reservation_id}: {self.amount} {self.get_status_display()}'


class PaymentAttempt(models.Model):
    """One try to pay online through PayMongo, from the moment the customer presses Pay to the moment it ends.

    The BOOKING DOES NOT EXIST while an attempt is waiting: the checkout page sends the whole selection (gowns, dates, the prices the customer
    saw, their details) to the server, which checks everything exactly as the old checkout did, keeps it here in `payload`, and opens a PayMongo
    checkout for the exact amount. Only when PayMongo confirms the money does reservations/online_payment.py make the booking (confirmed at
    once, with its Payment Receipt). If the gown was taken while the customer paid, the whole payment is refunded instead.

    So nothing half-made ever shows in Pending Approval, the calendar or the account's booking limits, and an abandoned checkout leaves only
    this row (and a PayMongo session the sweep closes)."""

    class Status(models.TextChoices):
        CREATED = 'created', 'Waiting for the customer to pay'
        PAID = 'paid', 'Paid, making the booking'                    # PayMongo confirmed the money; the booking is being made (or will be tried again)
        BOOKED = 'booked', 'Paid, booking made'
        REFUNDED = 'refunded', 'Paid, booking not possible, money sent back'
        FLAGGED = 'flagged', 'Paid, needs a person to look'
        HANDLED = 'handled', 'A person sorted it out'              # staff took care of a flagged payment by hand (refunded in PayMongo's dashboard, booked by hand, ...)
        EXPIRED = 'expired', 'Not paid, closed'
        CANCELLED = 'cancelled', 'The customer went back without paying'

    class Purpose(models.TextChoices):
        DEPOSIT = 'deposit', 'Deposit only'
        FULL = 'full', 'Paid in full'
        BALANCE = 'balance', 'Rental balance'             # only for source "shop": a booking that already exists
        EXTRA = 'extra', 'Extra charge'                   # fees or deductions beyond the deposit, on a booking that already exists

    class Source(models.TextChoices):
        ONLINE = 'online', 'Online checkout'              # the customer, on their own account (gowns/online_checkout.py start_payment)
        WALKIN = 'walkin', 'Walk-in at the shop'          # staff, in New Booking: the customer pays on their phone from a QR code (gowns/walkin_payment.py)
        SHOP = 'shop', 'Existing booking at the shop'     # staff, in Payments: the customer pays what an existing booking owes from a QR code
                                                          # (gowns/counter_payment.py); `reservation` is set from the start

    token = models.CharField(max_length=43, unique=True)          # the secret the success and cancel addresses carry (never PayMongo's own query)
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='payment_attempts')
    source = models.CharField(max_length=6, choices=Source.choices, default=Source.ONLINE)
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='walkin_payment_attempts')   # who started a walk-in one
    customer_name = models.CharField(max_length=120, blank=True, default='')
    purpose = models.CharField(max_length=10, choices=Purpose.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2)                     # exactly what the customer is asked to pay
    payload = models.JSONField(default=dict, blank=True)                              # the booking to make once it is paid (items, prices, details)
    request_id = models.CharField(max_length=40, blank=True, default='')              # one per press of Pay: pressing twice gives the same attempt
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.CREATED, db_index=True)
    session_id = models.CharField(max_length=60, blank=True, default='')              # PayMongo's cs_...
    checkout_url = models.URLField(max_length=500, blank=True, default='')
    livemode = models.BooleanField(default=False)                                      # False = a test payment: no real money moved
    channel = models.CharField(max_length=20, blank=True, default='')                 # how the customer paid, as PayMongo names it (gcash, paymaya, qrph)
    provider_payment_id = models.CharField(max_length=60, blank=True, default='')     # PayMongo's pay_... of the paid payment
    paid_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    reservation = models.ForeignKey(Reservation, on_delete=models.SET_NULL, null=True, blank=True, related_name='payment_attempts')
    # A payment for a date change request (reservations/date_requests.py): made by the customer on their booking page, or from the shop's QR code.
    date_change = models.ForeignKey(DateChangeRequest, on_delete=models.SET_NULL, null=True, blank=True, related_name='attempts')
    refund_id = models.CharField(max_length=60, blank=True, default='')                # when the money was sent back (the booking could not be made)
    refund_status = models.CharField(max_length=12, blank=True, default='')
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    note = models.CharField(max_length=300, blank=True, default='')                   # why it needs a look / why it was refunded (staff read this)
    webhook_at = models.DateTimeField(null=True, blank=True)                           # the last time PayMongo's webhook told us about it
    last_checked_at = models.DateTimeField(null=True, blank=True)                      # the last time we asked PayMongo (so polling cannot hammer it)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField()                                                # when we close the session (the checkout's own 20 minutes)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['customer', 'request_id'], condition=~models.Q(request_id=''), name='paymentattempt_one_per_press'),
            models.UniqueConstraint(fields=['staff', 'request_id'], condition=~models.Q(request_id=''), name='paymentattempt_walkin_one_per_press'),
            models.UniqueConstraint(fields=['session_id'], condition=~models.Q(session_id=''), name='paymentattempt_session_unique'),
            models.UniqueConstraint(fields=['provider_payment_id'], condition=~models.Q(provider_payment_id=''), name='paymentattempt_payment_unique'),
        ]

    @property
    def is_final(self):
        return self.status not in (self.Status.CREATED, self.Status.PAID)

    @property
    def refund_can_be_asked(self):
        """True while the money can still be sent back from here: paid, no booking, and PayMongo was never asked for the refund (or refused it)."""
        return (self.status in (self.Status.PAID, self.Status.FLAGGED, self.Status.REFUNDED) and bool(self.provider_payment_id)
                and not self.refund_id and self.refund_status in ('', 'failed'))

    UNMADE_AFTER = timedelta(minutes=5)         # a paid attempt whose booking is still not made after this long needs a person

    @classmethod
    def attention_filter(cls, now=None):
        """The rows that need a person, as a query filter (the same rule as `needs_a_look`, which a test keeps in step)."""
        now = now or timezone.now()
        return (Q(status=cls.Status.FLAGGED) | Q(status=cls.Status.REFUNDED, refund_status='failed')
                | Q(status=cls.Status.PAID, paid_at__lt=now - cls.UNMADE_AFTER))

    @property
    def needs_a_look(self):
        """True while a person has to do something: paid money no booking stands behind (flagged, or a booking that has stayed unmade for
        minutes), or a refund that did not go through."""
        if self.status == self.Status.FLAGGED or (self.status == self.Status.REFUNDED and self.refund_status == 'failed'):
            return True
        return self.status == self.Status.PAID and bool(self.paid_at) and timezone.now() - self.paid_at > self.UNMADE_AFTER

    def __str__(self):
        return f'{self.token[:8]} {self.get_status_display()} {self.amount}'


class IdentityCheck(models.Model):
    """One check of a customer's valid government ID (reservations/identity.py).

    There are three ways a check comes about:
      * ONLINE: a customer verifies on their account. Didit (the verification service) hosts the page where they scan the ID and take a selfie; its verdict is
        read back by the server (never taken from the browser or from a webhook's body alone).
      * WALKIN: staff, in New Booking, show a QR code; the customer verifies on their own phone the same way.
      * COUNTER: staff looked at the original ID at the counter and say so (their name and the time are kept; there is no photo).
    What is kept: the result, the ID type, when it was verified and when it runs out (12 months), a few plain reasons if it failed, and -- for the owner only --
    the FRONT and BACK photos of the ID (IdentityPhoto), deleted 30 days after the customer's last return. NEVER kept: the selfie, the ID number as text."""

    class Source(models.TextChoices):
        ONLINE = 'online', 'Online customer'
        WALKIN = 'walkin', 'Walk-in (QR code)'
        COUNTER = 'counter', 'Checked at the counter'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Checking'                  # the customer has not finished on the verification page
        APPROVED = 'approved', 'Verified'
        REVIEW = 'review', 'Needs the owner\'s review'    # the service could not decide by itself
        DECLINED = 'declined', 'Declined'
        EXPIRED = 'expired', 'Not finished'              # the customer never finished (the page closed after 7 days)
        CANCELLED = 'cancelled', 'Cancelled'             # staff closed a walk-in QR code

    class IdType(models.TextChoices):
        PHILID = 'philid', 'PhilID (National ID)'
        DRIVERS = 'drivers_license', "Driver's license"
        PASSPORT = 'passport', 'Passport'
        UMID = 'umid', 'UMID'
        SSS = 'sss', 'SSS ID'
        PRC = 'prc', 'PRC ID'
        VOTERS = 'voters_id', "Voter's ID"
        POSTAL = 'postal', 'Postal ID'
        OTHER = 'other', 'Other valid government ID'

    class DecisionSource(models.TextChoices):
        DIDIT = 'didit', 'The verification service'
        OWNER = 'owner', 'The owner'
        COUNTER = 'counter', 'Staff at the counter'

    class Photos(models.TextChoices):
        NONE = '', 'None'
        PENDING = 'pending', 'To be copied'
        COPYING = 'copying', 'Being copied'
        STORED = 'stored', 'Kept'
        FAILED = 'failed', 'Could not be copied'
        DELETED = 'deleted', 'Deleted'

    token = models.CharField(max_length=43, unique=True)            # the secret the customer's return address carries
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.ONLINE)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    # The website account that was verified (online). CASCADE: deleting an account deletes its ID checks, and with them the photos (IdentityPhoto's
    # post_delete removes the files). A walk-in's check has no account.
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name='identity_checks')
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')       # who started / confirmed it at the shop
    subject_name = models.CharField(max_length=120, blank=True, default='')
    subject_phone = models.CharField(max_length=20, blank=True, default='')    # digits only: a walk-in's check is only good for a booking with this phone
    id_type = models.CharField(max_length=16, choices=IdType.choices, default=IdType.OTHER)
    note = models.CharField(max_length=200, blank=True, default='')            # the staff member's note at the counter / the owner's note on a decision
    consent_at = models.DateTimeField(null=True, blank=True)                   # when the customer agreed to the ID being checked and kept
    session_id = models.CharField(max_length=60, blank=True, default='')       # the verification service's session
    session_url = models.URLField(max_length=500, blank=True, default='')      # the page the customer opens (kept secret: whoever has it can finish the session)
    session_expires_at = models.DateTimeField(null=True, blank=True)
    provider_status = models.CharField(max_length=30, blank=True, default='')  # what the service said last ("Approved", "In Review", ...)
    decision_source = models.CharField(max_length=8, choices=DecisionSource.choices, blank=True, default='')
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    reasons = models.JSONField(default=list, blank=True)                       # short plain reasons it was not approved (never ID data)
    name_check = models.CharField(max_length=8, blank=True, default='')        # '' | match | differs: the name on the ID against the name given
    type_check = models.CharField(max_length=8, blank=True, default='')        # '' | match | differs: the ID type chosen against the one scanned
    verified_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)                   # when a verified check stops counting (12 months after it)
    photos_state = models.CharField(max_length=8, choices=Photos.choices, blank=True, default='')
    photos_tries = models.PositiveSmallIntegerField(default=0)
    last_checked_at = models.DateTimeField(null=True, blank=True)              # the last time the service was asked (so polling cannot hammer it)
    webhook_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            # One unfinished online check per account: pressing "Verify my ID" twice (or from two tabs) is the same check, never a second one.
            models.UniqueConstraint(fields=['customer'], condition=Q(status='pending', source='online'), name='identitycheck_one_pending_per_customer'),
            models.UniqueConstraint(fields=['session_id'], condition=~Q(session_id=''), name='identitycheck_session_unique'),
        ]

    def __str__(self):
        return f'{self.token[:8]} {self.get_status_display()} ({self.get_source_display()})'

    @property
    def is_valid(self):
        """True while this check counts: approved and not yet past its 12 months."""
        return self.status == self.Status.APPROVED and bool(self.expires_at) and self.expires_at > timezone.now()

    @property
    def is_open(self):
        """Still waiting for the customer or for a decision."""
        return self.status in (self.Status.PENDING, self.Status.REVIEW)

    @property
    def badge(self):
        """(kind, words) for the small ID chip staff see: kind is ok | warn | bad | wait."""
        if self.status == self.Status.APPROVED:
            if not self.is_valid:
                return 'warn', 'ID expired'
            if self.decision_source == self.DecisionSource.COUNTER:
                return 'ok', 'ID checked at the counter'
            return 'ok', 'ID verified'
        if self.status == self.Status.REVIEW:
            return 'warn', 'ID needs review'
        if self.status == self.Status.DECLINED:
            return 'bad', 'ID declined'
        if self.status == self.Status.PENDING:
            return 'wait', 'ID being checked'
        return 'bad', 'ID not finished'


class IdentityPhoto(models.Model):
    """The FRONT or BACK picture of an ID, copied from the verification service when the check finished. Only the owner can open it (arabela_admin/
    identity_views.py); the file is deleted 30 days after the customer's last return, when the owner presses Delete, or when the account goes."""

    class Side(models.TextChoices):
        FRONT = 'front', 'Front'
        BACK = 'back', 'Back'

    id_check = models.ForeignKey(IdentityCheck, on_delete=models.CASCADE, related_name='photos')
    side = models.CharField(max_length=5, choices=Side.choices)
    storage_name = models.CharField(max_length=300)           # the name in the file storage (a long random name; never shown to anyone)
    size_bytes = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id_check_id', 'side']
        constraints = [models.UniqueConstraint(fields=['id_check', 'side'], name='identityphoto_one_per_side')]

    def __str__(self):
        return f'{self.get_side_display()} of check {self.id_check_id}'


from django.db.models.signals import post_delete       # noqa: E402
from django.dispatch import receiver                    # noqa: E402


@receiver(post_delete, sender=IdentityPhoto)
def _delete_photo_file(sender, instance, **kwargs):
    """The picture goes when its row goes (a row deleted by the cleanup, the owner's button, the wipe script, or an account being deleted)."""
    if not instance.storage_name:
        return
    try:
        from django.core.files.storage import default_storage
        default_storage.delete(instance.storage_name)
    except Exception:       # noqa: BLE001 -- a file that cannot be removed must never stop the row being removed; the cleanup reports leftovers
        import logging
        logging.getLogger(__name__).warning("An ID photo file could not be deleted (check %s)", instance.id_check_id)
