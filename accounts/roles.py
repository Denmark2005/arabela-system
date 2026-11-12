"""Who may do what in the admin panel -- the one place access is decided.

Two kinds of admin account (UserProfile.Role):
  * Owner -- everything (a superuser is always an owner), plus Staff Management, the Audit Log, Categories, tag colors and the shop settings;
  * Staff -- the daily work: gowns, New Booking, approving/rejecting new bookings, pick-ups and returns, receipts, reminders.

On top of the daily work the owner can allow each staff account any of four sensitive actions (PERMISSIONS below), one by one in
Staff Management. A staff account without a permission never sees its button, and the server refuses the request anyway.

There used to be a Manager role. It is gone from the screens; an old Manager row that has not been converted yet (the data migration
turns each one into Staff with all four permissions) is still treated as having all four, so nobody loses access while the shop's
database and the running site are updated one after the other.

The server checks (arabela_admin.views._permission_error) and the templates' hidden buttons (the `admin_perms` context value) both call
can() / granted_permissions(), so a button can never be shown that the server would refuse, or hidden while the server would allow it.
"""

# key -> what the owner reads next to the checkbox, and how the refusal reads ("You are not allowed to <verb>.")
PERMISSIONS = {
    "delete_gowns": {
        "label": "Delete gowns",
        "help": "Remove a gown from the catalog, one at a time or several at once. A removed gown's number is retired for good.",
        "verb": "delete gowns",
    },
    "cancel_bookings": {
        "label": "Cancel bookings",
        "help": "Cancel a booking, or single gowns of it, that nobody picked up. This decides whether the customer keeps the security deposit.",
        "verb": "cancel bookings",
    },
    "flag_customers": {
        "label": "Flag customers",
        "help": "Flag or unflag a customer's account, and lift a customer's temporary cancellation lock.",
        "verb": "flag customers or lift a lock",
    },
    "delete_receipts": {
        "label": "Delete receipt photos",
        "help": "Delete a receipt photo that has already been attached. (Everyone can delete their own photo within 15 minutes of adding it.)",
        "verb": "delete receipt photos",
    },
}
PERMISSION_KEYS = tuple(PERMISSIONS)

# The two quick settings in Staff Management: (id, label, permissions)
PRESETS = (
    ("daily", "Daily work only", ()),
    ("senior", "Senior staff", PERMISSION_KEYS),
)

# How long the person who attached a receipt photo may delete it again without the "Delete receipt photos" permission.
RECEIPT_UNDO_MINUTES = 15

_UNSET = object()


def permission_message(key):
    """The refusal a staff member reads when the server says no to `key`."""
    verb = PERMISSIONS.get(key, {}).get("verb", "do that")
    return f"You don't have permission to {verb}. Ask the owner to allow it."


def parse_permissions(raw):
    """The valid permission keys in `raw` (a comma-separated string as stored, or a list from a form), in the registry's order,
    each once. Anything unknown is dropped."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.replace(";", ",").split(",")
    wanted = {str(item).strip() for item in raw}
    return [key for key in PERMISSION_KEYS if key in wanted]


def join_permissions(keys):
    return ",".join(parse_permissions(keys))


def _is_admin(user):
    return bool(user and getattr(user, "is_authenticated", False) and (user.is_staff or user.is_superuser))


def granted_permissions(user, profile=_UNSET):
    """The set of sensitive actions this account may do. The owner (and a superuser) may do all of them; a Staff account only the ones
    the owner allowed; an admin account with no profile, a deactivated account and anyone who is not an admin none at all. `profile` lets
    a caller that already loaded the account's UserProfile (or knows there is none: pass None) skip the lookup."""
    if not _is_admin(user) or not user.is_active:
        return frozenset()
    if user.is_superuser:
        return frozenset(PERMISSION_KEYS)
    from accounts.models import UserProfile      # imported here so reservations.models can import this module without a cycle
    if profile is _UNSET:
        profile = UserProfile.objects.filter(user=user).first()
    if profile is None:
        return frozenset()
    if profile.role in (UserProfile.Role.OWNER, UserProfile.Role.MANAGER):
        return frozenset(PERMISSION_KEYS)
    return frozenset(parse_permissions(profile.permissions))


def can(user, key, profile=_UNSET):
    """True when this account may do the sensitive action `key` (one of PERMISSION_KEYS)."""
    return key in granted_permissions(user, profile)


def staff_display_name(user):
    """The one name shown for a staff account everywhere in admin: the real name entered when the account was created,
    else the username. (Not UserProfile.display_name: that is a customer-facing field.)"""
    return f"{user.first_name} {user.last_name}".strip() or user.get_username()
