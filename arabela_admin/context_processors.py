import os

from django.conf import settings

from accounts.models import UserProfile
from arabela_admin import notifications


def admin_asset_version(request):
    """A cache-busting stamp for the admin panel's compiled bundle.

    bundle.js is a static file, so a browser will happily keep serving the copy it
    already has after the file changes on disk -- which is exactly what happened when
    the Monthly Rentals chart kept rendering the old demo numbers even though the
    served file was correct. Deriving the stamp from the file's own modification time
    means the URL changes automatically on every edit; there is no version constant
    for anyone to forget to bump.

    Also covers notifications-live.js (the live bell), for the same reason.

    Falls back to a fixed value if the files are missing (e.g. a fresh checkout before
    static assets are in place) so a template render can never blow up over this.
    """
    newest = 0
    for name in ("bundle.js", "notifications-live.js"):
        try:
            newest = max(newest, int(os.path.getmtime(os.path.join(settings.BASE_DIR, "static", "arabela_admin", name))))
        except OSError:
            continue
    return {"admin_asset_version": newest or "0"}


def site_asset_version(request):
    """The same cache-busting stamp, for the customer site's own scripts.

    reservation-flow.js drives the cart hand-off and the checkout Order Summary, so
    a browser holding a stale copy silently keeps the old checkout behaviour after a
    fix ships -- the customer-side twin of the bundle.js problem above. Stamped from
    the newest mtime across the scripts base.html loads, so touching any of them
    invalidates the URL.
    """
    names = ("reservation-flow.js", "collection-sort.js", "customer-badges-live.js", "picked-file.js")
    newest = 0
    for name in names:
        try:
            newest = max(newest, int(os.path.getmtime(
                os.path.join(settings.BASE_DIR, "static", "js", name)
            )))
        except OSError:
            continue
    return {"site_asset_version": newest or "0"}


def admin_user(request):
    """Identity of the signed-in staff member for the admin panel's header/dropdown.

    Every admin template hardcoded the same placeholder ("Alucard Balmond") because
    the panel has no shared header partial -- the markup is duplicated in each file.
    Rather than plumb context through ~20 separate views, this processor supplies it
    globally so each template just reads the variables. Returns blanks for anonymous
    or customer sessions so it costs nothing on the customer-facing side.
    """
    blank = {
        "admin_display_name": "",
        "admin_full_name": "",
        "admin_email": "",
        "admin_avatar_url": "",
        "admin_avatar_position": "50% 50%",
        "admin_avatar_position_x": 50,
        "admin_avatar_position_y": 50,
        "admin_is_owner": False,
        "admin_role": "",
    }

    user = getattr(request, "user", None)
    if not (user and user.is_authenticated and (user.is_staff or user.is_superuser)):
        return blank

    full_name = f"{user.first_name} {user.last_name}".strip()
    profile = UserProfile.objects.filter(user=user).first()
    pos_x = profile.avatar_position_x if profile else 50
    pos_y = profile.avatar_position_y if profile else 50

    # Owner = superuser, or an explicit OWNER role. Drives whether the Staff Management
    # menu item and the shop's business-settings sections are shown. Every other admin
    # is a restricted Manager/Staff.
    role = profile.role if profile else UserProfile.Role.OWNER
    is_owner = bool(user.is_superuser or role == UserProfile.Role.OWNER)

    return {
        # Short label next to the avatar; falls back to the username when no name is set.
        "admin_display_name": user.first_name or user.get_username(),
        "admin_full_name": full_name or user.get_username(),
        "admin_email": user.email,
        "admin_avatar_url": (profile.profile_picture_url if profile else "") or "",
        # CSS object-position, matching the drag-to-place choice made in Edit Profile.
        "admin_avatar_position": f"{pos_x}% {pos_y}%",
        "admin_avatar_position_x": pos_x,
        "admin_avatar_position_y": pos_y,
        "admin_is_owner": is_owner,
        "admin_role": role,
    }


def admin_notifications(request):
    """The header bell's data -- see arabela_admin.notifications, which also serves the live feed
    that keeps the bell current without a refresh. Blank for anonymous or customer sessions."""
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated and (user.is_staff or user.is_superuser)):
        return notifications.empty_context()
    return notifications.build_context(user)
