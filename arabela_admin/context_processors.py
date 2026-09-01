import os
from datetime import datetime, time
from urllib.parse import urlencode

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from arabela_admin.views import _staff_display_name
from gowns.models import Gown
from reservations.models import Reservation, ReservationItem


def admin_asset_version(request):
    """A cache-busting stamp for the admin panel's compiled bundle.

    bundle.js is a static file, so a browser will happily keep serving the copy it
    already has after the file changes on disk -- which is exactly what happened when
    the Monthly Rentals chart kept rendering the old demo numbers even though the
    served file was correct. Deriving the stamp from the file's own modification time
    means the URL changes automatically on every edit; there is no version constant
    for anyone to forget to bump.

    Falls back to a fixed value if the file is missing (e.g. a fresh checkout before
    static assets are in place) so a template render can never blow up over this.
    """
    path = os.path.join(settings.BASE_DIR, "static", "arabela_admin", "bundle.js")
    try:
        return {"admin_asset_version": int(os.path.getmtime(path))}
    except OSError:
        return {"admin_asset_version": "0"}


def site_asset_version(request):
    """The same cache-busting stamp, for the customer site's own scripts.

    reservation-flow.js drives the cart hand-off and the checkout Order Summary, so
    a browser holding a stale copy silently keeps the old checkout behaviour after a
    fix ships -- the customer-side twin of the bundle.js problem above. Stamped from
    the newest mtime across the scripts base.html loads, so touching any of them
    invalidates the URL.
    """
    names = ("reservation-flow.js", "collection-sort.js")
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


def _ago(when):
    """Compact relative age ('5 min ago', '3 days ago') for a datetime OR a date."""
    if when is None:
        return ""
    now = timezone.now()
    if not hasattr(when, "hour"):  # a plain date -- compare at day granularity
        days = (timezone.localdate() - when).days
        if days <= 0:
            return "today"
        if days == 1:
            return "yesterday"
        return f"{days} days ago"
    seconds = (now - when).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    return when.strftime("%b %d, %Y")


def _searchable(url_name, value):
    """A destination URL that lands on the exact row, not just the right page.

    `pending-approval.html`/`payment-verification.html` already read `?search=` on
    load and filter rows against it (proven, pre-existing); this round adds the same
    read to clients.html/security-deposits.html/staff-management.html and extends
    gown-catalog.html's existing `?category=` read. `value` MUST be something unique
    per row on that page (reference_code, gown_id, username, email) -- a display NAME
    is not safe here since more than one real account can share one (confirmed in
    this shop's own data: several customer rows are all named 'Denmark Concepcion').
    Each target template also highlights the row whose own unique field exactly
    matches this value, so there's no ambiguity even if the text search still leaves
    more than one row visible."""
    return f"{reverse(url_name)}?{urlencode({'search': value})}"


def admin_notifications(request):
    """Live work-queue notifications for the admin panel header.

    Deliberately DERIVED from current data rather than stored as rows: every entry is
    something that still needs doing, so the badge count falls on its own as staff work
    through it (approve a reservation and it disappears) and can never drift out of sync
    with reality. There is no per-user read state for the same reason -- 'unread' here
    means 'unhandled', which is the more useful signal for a shop floor.

    Each notification carries the URL of the module it belongs to, so clicking one lands
    on the page where the work is actually done.

    Role-aware: staff/manager accounts see the operational queues they can act on; the
    owner additionally sees staff-account items, mirroring the existing rule that staff
    get every module except Staff Management (see _is_owner / _require_owner in views).
    """
    empty = {
        "admin_notifications": [],
        "admin_notification_count": 0,
        "admin_notification_urgent": 0,
    }

    user = getattr(request, "user", None)
    if not (user and user.is_authenticated and (user.is_staff or user.is_superuser)):
        return empty

    profile = UserProfile.objects.filter(user=user).first()
    role = profile.role if profile else UserProfile.Role.OWNER
    is_owner = bool(user.is_superuser or role == UserProfile.Role.OWNER)

    today = timezone.localdate()
    items = []

    # --- Reservations awaiting approval -------------------------------------------
    for r in (
        Reservation.objects.filter(status=Reservation.Status.PENDING)
        .select_related("customer__profile")
        .order_by("-created_at")[:10]
    ):
        items.append({
            "kind": "reservation",
            "level": "info",
            "icon": "reservation",
            "actor": r.display_customer_name,
            "text": "is waiting for approval on",
            "subject": r.reference_code,
            "module": "Reservations",
            "url": _searchable("arabela_admin:pending_approval", r.reference_code),
            "when": r.created_at,
            "ago": _ago(r.created_at),
            "sort": r.created_at,
        })

    # --- Payment proofs uploaded but not yet verified ------------------------------
    for r in (
        Reservation.objects.filter(status=Reservation.Status.PENDING)
        .exclude(payment_proof_url="")
        .select_related("customer__profile")
        .order_by("-created_at")[:10]
    ):
        items.append({
            "kind": "payment",
            "level": "warning",
            "icon": "payment",
            "actor": r.display_customer_name,
            "text": "uploaded payment proof for",
            "subject": r.reference_code,
            "module": "Payments",
            "url": _searchable("arabela_admin:payment_verification", r.reference_code),
            "when": r.created_at,
            "ago": _ago(r.created_at),
            "sort": r.created_at,
        })

    # --- Overdue returns (the most time-critical queue) ----------------------------
    overdue_items = (
        ReservationItem.objects.filter(return_date__lt=today)
        .exclude(stage=ReservationItem.Stage.RETURNED)
        .exclude(reservation__status__in=[
            Reservation.Status.REJECTED,
            Reservation.Status.CANCELLED,
        ])
        .select_related("reservation__customer__profile")
        .order_by("return_date")[:10]
    )
    for it in overdue_items:
        days_late = (today - it.return_date).days
        items.append({
            "kind": "overdue",
            "level": "critical",
            "icon": "overdue",
            "actor": it.gown_name,
            "text": f"is {days_late} day{'s' if days_late != 1 else ''} overdue for return from",
            "subject": it.reservation.display_customer_name,
            "module": "Schedule",
            "url": reverse("arabela_admin:rental_schedule"),
            "when": it.return_date,
            "ago": _ago(it.return_date),
            # A plain date has no time; midnight is close enough for ordering, and the
            # sort only ever calls .timestamp() on each key in isolation.
            "sort": datetime.combine(it.return_date, time.min),
        })

    # --- Deposits still held on fully-returned rentals -----------------------------
    for r in (
        Reservation.objects.filter(
            status__in=[
                Reservation.Status.CONFIRMED,
                Reservation.Status.ACTIVE,
                Reservation.Status.OVERDUE,
            ],
            deposit_returned_at__isnull=True,
        )
        .select_related("customer__profile")
        .prefetch_related("items")
        .order_by("-updated_at")[:20]
    ):
        its = list(r.items.all())
        if its and all(i.stage == ReservationItem.Stage.RETURNED for i in its):
            items.append({
                "kind": "deposit",
                "level": "info",
                "icon": "deposit",
                "actor": r.display_customer_name,
                "text": "returned everything — deposit still held on",
                "subject": r.reference_code,
                "module": "Deposits",
                "url": _searchable("arabela_admin:security_deposits", r.reference_code),
                "when": r.updated_at,
                "ago": _ago(r.updated_at),
                "sort": r.updated_at,
            })

    # --- Inventory needing attention ----------------------------------------------
    for g in (
        Gown.objects.filter(
            status__in=[Gown.Status.IN_CLEANING, Gown.Status.OUT_OF_STOCK]
        ).order_by("-updated_at")[:6]
    ):
        items.append({
            "kind": "inventory",
            "level": "warning" if g.status == Gown.Status.IN_CLEANING else "critical",
            "icon": "inventory",
            "actor": g.gown_id,
            "text": "is marked",
            "subject": g.status,
            "module": "Inventory",
            "url": _searchable("arabela_admin:gown_catalog", g.gown_id),
            "when": g.updated_at,
            "ago": _ago(g.updated_at),
            "sort": g.updated_at,
        })

    # --- Flagged customers ---------------------------------------------------------
    for p in (
        UserProfile.objects.filter(is_flagged=True)
        .select_related("user")[:6]
    ):
        items.append({
            "kind": "customer",
            "level": "warning",
            "icon": "customer",
            # Same resolution Client List itself uses for this row -- otherwise the
            # notification can name the account something that never appears on the
            # page it links to (this is the exact bug being fixed here: a blank
            # display_name plus a real first/last name showed as the username here
            # but as the full name on Client List, for the very same account).
            "actor": UserProfile.customer_display_name(p.user),
            "text": "is flagged for review in",
            "subject": "Client List",
            "module": "Customers",
            # Search by email, not name: this shop's own data already has several
            # different customer accounts sharing the exact display name "Denmark
            # Concepcion" (different emails), so a name search would not narrow the
            # list to the one flagged account it's actually about.
            "url": _searchable("arabela_admin:clients", p.user.email),
            "when": None,
            "ago": "",
            "sort": None,
        })

    # --- Owner-only: staff roster ---------------------------------------------------
    # Staff accounts are managed solely by the owner (Staff Management is owner-gated),
    # so surfacing roster items to a staff member would just link them somewhere they
    # can't go.
    if is_owner:
        inactive_staff = UserProfile.objects.filter(
            role__in=[UserProfile.Role.MANAGER, UserProfile.Role.STAFF],
            user__is_staff=True,
            user__is_active=False,
        ).select_related("user")[:5]
        for p in inactive_staff:
            items.append({
                "kind": "staff",
                "level": "info",
                "icon": "staff",
                # Same resolution Staff Management's own table uses (_staff_row) --
                # deliberately not customer_display_name; that one is a customer
                # concept (checks profile.display_name), staff are named from their
                # real name instead. See _staff_display_name's own docstring.
                "actor": _staff_display_name(p.user),
                "text": "is a deactivated staff account in",
                "subject": "Staff Management",
                "module": "Staff",
                # Username is unique, unlike a name -- narrows to exactly one row.
                "url": _searchable("arabela_admin:staff_management", p.user.username),
                "when": None,
                "ago": "",
                "sort": None,
            })

    # Critical first, then newest -- an overdue gown should never sit below a routine
    # approval just because the approval happens to be more recent.
    level_rank = {"critical": 0, "warning": 1, "info": 2}
    items.sort(key=lambda n: (
        level_rank.get(n["level"], 3),
        -(n["sort"].timestamp() if n["sort"] is not None else 0),
    ))

    return {
        "admin_notifications": items,
        "admin_notification_count": len(items),
        "admin_notification_urgent": sum(1 for n in items if n["level"] == "critical"),
    }
