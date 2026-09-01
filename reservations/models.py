from django.conf import settings
from django.db import models
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
        """RSV-{year}-{sequence:04d}, sequence restarting each year. Mirrors the
        Gown.next_tracking_number pattern used in the inventory module."""
        year = timezone.now().year
        prefix = f'RSV-{year}-'
        last = (cls.objects.filter(reference_code__startswith=prefix)
                            .order_by('-id').first())
        if not last:
            return f'{prefix}0001'
        try:
            nxt = int(last.reference_code.split('-')[2]) + 1
        except (IndexError, ValueError):
            nxt = cls.objects.filter(reference_code__startswith=prefix).count() + 1
        return f'{prefix}{nxt:04d}'

    def save(self, *args, **kwargs):
        if not self.reference_code:
            self.reference_code = self.next_reference_code()
        super().save(*args, **kwargs)


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
    # The Rental Schedule shows ONE status at a time, chosen by staff (item.stage). Each
    # status maps to its own dates: Pick-up = rental_date..event_date-1, Reserved = the
    # event day, Return = event_date+1..return_date, Overdue = the overdue_date. All four
    # dates are editable by staff (event_date defaults to rental_date + 2, overdue_date to
    # return_date). Ranges are derived in arabela_admin.views._stage_segment.
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.PICKUP)
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
