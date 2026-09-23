from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.utils import timezone


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

    reference_code = models.CharField(max_length=20, unique=True, blank=True)
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='reservations'
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
    rental_subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    security_deposit = models.DecimalField(max_digits=10, decimal_places=2, default=2000)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    # Set once staff releases the deposit back to the customer (a separate, later step
    # from marking the gown itself returned -- see reservation_return_deposit_view).
    deposit_returned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.reference_code} — {self.customer_name}'

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
    # Bumped on every save (creation + every staff stage/date edit) -- lets the
    # customer's "new activity" badge tell freshly-changed items apart from ones
    # they've already seen, without a separate read/unread table.
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['rental_date']

    def __str__(self):
        return f'{self.gown_name} ({self.rental_date} → {self.return_date})'

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
        return not self.reservation.items.exclude(stage=self.Stage.PICKUP).exists()


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

    class Meta:
        ordering = ['occurred_at', 'id']
        indexes = [models.Index(fields=['reservation', 'occurred_at'])]

    def __str__(self):
        return f'{self.reservation.reference_code}: {self.label} @ {self.occurred_at}'

    @classmethod
    def record(cls, reservation, label, *, item=None, detail='', actor=Actor.SYSTEM):
        """The one way events get written. Deliberately swallows its own errors: a
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
                return cls.objects.create(
                    reservation=reservation, item=item, label=label,
                    detail=(detail or '')[:300], actor=actor,
                )
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

    reservation = models.ForeignKey(
        Reservation, on_delete=models.CASCADE, related_name='receipt_records'
    )
    photo_url = models.URLField()
    # SET_NULL, not CASCADE: a staff account being removed later must never take a
    # financial record down with it -- the receipt itself still matters.
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f'Receipt for {self.reservation.reference_code} ({self.uploaded_at:%Y-%m-%d})'
