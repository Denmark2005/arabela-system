from django.conf import settings
from django.db import models


class UserProfile(models.Model):
    class Role(models.TextChoices):
        # Admin-panel access tier. Owner = full access (the shop owner / superuser);
        # Manager & Staff are the restricted tier -- everything except Staff Management.
        # Irrelevant for ordinary customers (they never reach the admin panel), who keep
        # the default STAFF value harmlessly since they have no is_staff flag.
        OWNER = 'Owner', 'Owner'
        MANAGER = 'Manager', 'Manager'
        STAFF = 'Staff', 'Staff'

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.STAFF)
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
    """Failed admin sign-in attempts, per username -- kept in the DATABASE (see accounts/login_throttle.py).

    They used to live in the server's memory, which the host wipes every time the app restarts or sleeps and which a
    second server process would not share, so the "5 wrong attempts = locked for 15 minutes" rule kept forgetting."""

    key = models.CharField(max_length=120, unique=True)
    failures = models.PositiveIntegerField(default=0)
    last_failure_at = models.DateTimeField(db_index=True)

    def __str__(self):
        return f'{self.key}: {self.failures} failed'


class OwnerRecovery(models.Model):
    """The shop owner's CONFIRMED recovery email -- the one address a password-reset link may be sent to (accounts/owner_recovery.py).

    Deliberately not the profile's Email box: that box can be typed into with no proof, so it must never decide who can reset the admin
    password. A row exists only after a code mailed to the address was typed back, so a mistyped or made-up address is never stored.
    Only the owner can have one; staff and customers never get a reset link."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='owner_recovery')
    email = models.EmailField(max_length=254)
    verified_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.user_id}: {self.email}'


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
