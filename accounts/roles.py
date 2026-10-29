"""Who may do what in the admin panel -- the one place the Manager tier is decided.

Three admin roles (UserProfile.Role):
  * Staff   -- the daily work: gowns, New Booking, approving/rejecting new bookings, pick-ups and returns, receipts, reminders;
  * Manager -- everything Staff can do, plus the sensitive actions below;
  * Owner   -- everything (a superuser is always an owner), plus Staff Management, Categories, tag colors and the shop settings.

The sensitive actions a Staff account cannot do -- only a Manager or the owner can:
  delete gowns (one or in bulk), cancel a confirmed booking or some of its gowns, flag/unflag a customer and remove a cancellation
  lock, and delete a receipt photo.

The server checks (arabela_admin.views._manager_error) and the templates' hidden buttons (the `admin_can_manage` context value) both
call can_manage(), so a button can never be shown that the server would refuse, or hidden while the server would allow it.
"""

MANAGER_ONLY_MESSAGE = "Only a Manager or the owner can do that."

_UNSET = object()


def can_manage(user, profile=_UNSET):
    """True for a signed-in admin account that is the owner (or a superuser) or a Manager. An admin account with no profile is
    not a Manager (the safe default), exactly as arabela_admin.views._is_owner treats it. `profile` lets a caller that already
    loaded the account's UserProfile (or knows there is none: pass None) skip the lookup."""
    if not (user and user.is_authenticated and (user.is_staff or user.is_superuser)):
        return False
    if user.is_superuser:
        return True
    from accounts.models import UserProfile      # imported here so reservations.models can import this module without a cycle
    if profile is _UNSET:
        profile = UserProfile.objects.filter(user=user).first()
    return bool(profile and profile.role in (UserProfile.Role.OWNER, UserProfile.Role.MANAGER))


def staff_display_name(user):
    """The one name shown for a staff/manager account everywhere in admin: the real name entered when the account was created,
    else the username. (Not UserProfile.display_name: that is a customer-facing field.)"""
    return f"{user.first_name} {user.last_name}".strip() or user.get_username()
