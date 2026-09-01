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
