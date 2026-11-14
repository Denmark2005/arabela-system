from django.conf import settings
from django.db import models


class UserProfile(models.Model):
    class Role(models.TextChoices):
        # Admin-panel access tier. Owner = full access (the shop owner / superuser). Staff = the daily work,
        # plus whichever of the four sensitive actions the owner allowed this account (see `permissions`).
        # Neither Staff nor anyone else but the owner can open Staff Management, the Audit Log, Categories or the
        # shop settings. The rule lives in accounts/roles.py (can).
        # MANAGER is no longer offered anywhere: it only exists so an old row that has not been converted yet keeps
        # working (it counts as Staff with all four permissions), until the data migration turns it into Staff.
        # Irrelevant for ordinary customers (they never reach the admin panel), who keep
        # the default STAFF value harmlessly since they have no is_staff flag.
        OWNER = 'Owner', 'Owner'
        MANAGER = 'Manager', 'Manager'
        STAFF = 'Staff', 'Staff'

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.STAFF)
    # The sensitive actions the owner allowed this STAFF account, as the keys in accounts.roles.PERMISSIONS joined by commas
    # ("delete_gowns,cancel_bookings"). Empty = the daily work only. db_default: the site that is still running while the
    # database is migrated inserts profiles without this column.
    permissions = models.CharField(max_length=200, blank=True, default='', db_default='')
    profile_picture_url = models.URLField(blank=True)
    # Where the circular avatar crop is centered within the uploaded photo, as a
    # CSS object-position percentage pair -- lets someone drag an off-center photo
    # into place instead of being stuck with whatever the automatic center-crop cuts off.
    avatar_position_x = models.PositiveSmallIntegerField(default=50)
    avatar_position_y = models.PositiveSmallIntegerField(default=50)
    age = models.PositiveSmallIntegerField(null=True, blank=True)
    display_name = models.CharField(max_length=120, blank=True)
    terms_accepted_at = models.DateTimeField(null=True, blank=True)
    is_flagged = models.BooleanField(default=False)
    reservations_last_seen_at = models.DateTimeField(null=True, blank=True)
    # How many times this customer held a selection and walked away without
    # submitting -- either pressing Cancel on the countdown or letting it lapse.
    # Stored because a hold lives only in the session and leaves no other trace,
    # unlike cancelled reservations which are counted from the Reservation rows.
    hold_abandon_count = models.PositiveIntegerField(default=0)
    # Graduated response to repeated cancellations/abandoned holds, below the
    # permanent is_flagged tier. cancel_lockout_until is the live enforcement
    # value (checked by reservation_hold_start); the two "sent" flags make each
    # tier fire exactly once per account rather than re-arming every time the
    # customer cancels again past that count. See accounts.services for the
    # thresholds and accounts.services.sync_cancellation_flag for where these
    # are set.
    cancel_lockout_until = models.DateTimeField(null=True, blank=True)
    cancel_tier1_lockout_sent = models.BooleanField(default=False)
    cancel_tier2_lockout_sent = models.BooleanField(default=False)
    # Set when too many payment proofs were refused in a short time (accounts.services.record_rejected_proof). While it is on, the account
    # cannot start a new reservation; the owner or a manager lifts it from the Client List (which also restarts the count).
    proof_blocked = models.BooleanField(default=False, db_default=False)
    proof_block_reset_at = models.DateTimeField(null=True, blank=True)
    # A mirror of the customer's shopping cart, so it survives switching devices
    # or clearing browser data -- the cart itself still lives in localStorage for
    # instant reads/writes (see base.html's setDrawerCart); this is a best-effort
    # background copy written on every change and read back on login elsewhere,
    # never the primary source of truth for the device someone is actively using.
    cart_snapshot = models.JSONField(default=list, blank=True)

    @classmethod
    def customer_display_name(cls, user):
        """The one name to show for a customer account everywhere in admin: an
        explicit display name, else their real name, else their username.

        Shared by arabela_admin.views.clients_view (the Client List table) and
        the admin_notifications context processor, so the same account is never
        shown under two different labels in two different places -- e.g. a
        flagged customer's own notification must read exactly what Client List
        shows for that row, or clicking it looks like it points at a ghost
        account. Not for staff/manager accounts -- see
        arabela_admin.views._staff_display_name for that separate rule."""
        profile = getattr(user, 'profile', None)
        return (profile.display_name if profile else '') or user.get_full_name() or user.get_username()

    def __str__(self):
        return f'Profile<{self.user.email}>'


class CustomerMessage(models.Model):
    """A message from shop staff to a customer, shown on the customer's Messages page.
    Currently only created by the Client List flag/unflag action; the category set
    is kept generic so other real admin actions can create these later without a
    model change."""

    class Category(models.TextChoices):
        ACCOUNT_FLAGGED = 'Account Flagged', 'Account Flagged'
        ACCOUNT_UNFLAGGED = 'Account Unflagged', 'Account Unflagged'
        # Written by the automatic due-date sweep (reservations/reminders.py). Split
        # into two so the Messages page can style an overdue warning differently from
        # a friendly heads-up -- the customer should be able to tell them apart at a
        # glance without reading the body.
        RESERVATION_REMINDER = 'Reservation Reminder', 'Reservation Reminder'
        RETURN_OVERDUE = 'Return Overdue', 'Return Overdue'
        # Written by accounts.services.sync_cancellation_flag when a customer crosses
        # the 5- or 10-attempt cancellation/abandoned-hold checkpoint.
        CANCELLATION_LOCKOUT = 'Cancellation Lockout', 'Cancellation Lockout'
        GENERAL = 'General', 'General'

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='customer_messages'
    )
    category = models.CharField(max_length=30, choices=Category.choices, default=Category.GENERAL)
    body = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.category} -> {self.recipient.get_username()}'


class LoginThrottle(models.Model):
    """Counters kept in the DATABASE: the rate limits in accounts/rate_limit.py (key "rl:...") and, in older rows, the admin sign-in
    lockout of the removed username + password page (key "user:..."; the admin panel signs in with Google only now).

    They used to live in the server's memory, which the host wipes every time the app restarts or sleeps and which a
    second server process would not share, so the counts kept forgetting."""

    key = models.CharField(max_length=120, unique=True)
    failures = models.PositiveIntegerField(default=0)
    last_failure_at = models.DateTimeField(db_index=True)

    def __str__(self):
        return f'{self.key}: {self.failures} failed'


class CustomerEmailLog(models.Model):
    """One row for every email the site tried to send to a customer -- the owner's "Email history" (Customer emails page).

    It answers "did this customer get their email?" after the site has restarted or slept, which the in-memory list could
    not. Written by accounts/customer_emails.py after each attempt; a failure to write a row never stops an email.
    Rows older than about six months are removed as new ones are written. The address is stored in full (the page is owner-only);
    the server logs keep masking it."""

    class Outcome(models.TextChoices):
        SENT = 'sent', 'Sent'
        FAILED = 'failed', 'Failed'
        SKIPPED = 'skipped', 'Not sent'

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    kind = models.CharField(max_length=30)                      # received, approved, reminder ... or "test"
    reference_code = models.CharField(max_length=30, blank=True)   # RSV-2026-0058, when the email is about one booking
    to_address = models.CharField(max_length=254, blank=True)
    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    reason = models.CharField(max_length=400, blank=True)       # why it failed / was not sent

    class Meta:
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f'{self.kind} -> {self.to_address or "(no address)"}: {self.outcome}'


class SavedAddress(models.Model):
    """One of a customer's saved addresses (Profile -> Addresses).

    These used to live only in the browser (localStorage), so a default address never followed a customer to another phone or survived
    clearing the browser. They are kept on the account now; the checkout fills its boxes from the default one (else the first).

    At most one address per customer is the default -- the database itself refuses a second one (the partial unique constraint below), so
    two taps or two devices at once can never leave two defaults. The limits and the field checks (4-digit postal code, 11-digit phone)
    are applied in gowns.views (_clean_saved_address), the same ones the profile page has always applied in the browser."""

    MAX_PER_CUSTOMER = 5

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='saved_addresses')
    address_line = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    postal_code = models.CharField(max_length=10)
    phone = models.CharField(max_length=20)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # The default first, then in the order they were added.
        ordering = ['-is_default', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['user'], condition=models.Q(is_default=True), name='one_default_address_per_customer',
            ),
        ]

    def __str__(self):
        return f'{self.address_line}, {self.city} ({"default" if self.is_default else "saved"})'


class AuditLogImmutable(Exception):
    """Raised when something tries to change or delete an Audit Log row. The log is append-only."""


class AuditLogQuerySet(models.QuerySet):
    """update() and delete() are refused, so no view, shell snippet or admin action can rewrite what staff did. The one way rows
    leave is purge_older_than(), the 12-month clean-up."""

    def update(self, **kwargs):
        raise AuditLogImmutable('The Audit Log cannot be changed.')

    def delete(self):
        raise AuditLogImmutable('The Audit Log cannot be deleted.')

    def bulk_update(self, objs, fields, batch_size=None):
        raise AuditLogImmutable('The Audit Log cannot be changed.')

    def purge_older_than(self, cutoff):
        """Delete every row written before `cutoff` (a datetime). Returns how many went."""
        old = self.filter(created_at__lt=cutoff)
        count = old.count()
        if count:
            super(AuditLogQuerySet, old).delete()
        return count


class AuditLog(models.Model):
    """One line for everything a signed-in admin account did (and every sign-in, failed sign-in and refused attempt): the owner's Audit Log page.

    Append-only: nothing edits or deletes a row except the 12-month clean-up. Never holds a password, a token or a picture. The actor is
    kept twice -- the account link (cleared if the account is deleted) and the name/role as they were that day -- so the line still reads
    right after the account is renamed or removed. IP address and device are kept only for sign-in events and refused sensitive attempts."""

    class Category(models.TextChoices):
        BOOKINGS = 'Bookings', 'Bookings'
        PAYMENTS = 'Payments', 'Payments'
        GOWNS = 'Gowns', 'Gowns'
        CUSTOMERS = 'Customers', 'Customers'
        STAFF = 'Staff', 'Staff'
        SETTINGS = 'Settings', 'Settings'
        SIGN_IN = 'Sign-in', 'Sign-in'

    class Outcome(models.TextChoices):
        OK = 'ok', 'Done'
        REFUSED = 'refused', 'Refused'
        FAILED = 'failed', 'Failed'

    created_at = models.DateTimeField(auto_now_add=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    actor_name = models.CharField(max_length=150, blank=True)
    actor_role = models.CharField(max_length=20, blank=True)           # 'Owner' / 'Staff' as it was then
    category = models.CharField(max_length=20, choices=Category.choices)
    action = models.CharField(max_length=60)                           # a code such as 'booking.approve'
    summary = models.CharField(max_length=300)                         # the readable sentence
    target_type = models.CharField(max_length=30, blank=True)          # 'booking', 'gown', 'customer', 'staff', 'receipt', ...
    target_id = models.CharField(max_length=40, blank=True)
    target_label = models.CharField(max_length=160, blank=True)
    details = models.JSONField(default=dict, blank=True)               # before -> after, the reason, counts
    sensitive = models.BooleanField(default=False)
    outcome = models.CharField(max_length=10, choices=Outcome.choices, default=Outcome.OK)
    ip_address = models.CharField(max_length=45, blank=True)
    device = models.CharField(max_length=120, blank=True)

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['created_at'], name='auditlog_created_idx'),
            models.Index(fields=['actor', 'created_at'], name='auditlog_actor_created_idx'),
            models.Index(fields=['category', 'created_at'], name='auditlog_cat_created_idx'),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AuditLogImmutable('The Audit Log cannot be changed.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditLogImmutable('The Audit Log cannot be deleted.')

    def __str__(self):
        return f'{self.created_at:%Y-%m-%d %H:%M} {self.actor_name}: {self.summary}'
