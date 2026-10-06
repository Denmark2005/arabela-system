"""The admin panel's "Needs Attention" feed -- the bell, its dropdown and its "View all" window.

One builder (`build_context`) serves BOTH ways the bell is filled in:

  * every admin page load, through the `admin_notifications` context processor, and
  * the live feed (`feed_payload`, served at api/notifications/), which the bell's script asks
    every few seconds so a customer's reservation shows up without anyone refreshing.

Because both come from the same function -- and the live feed's HTML is rendered from the very
same small templates the page itself includes -- a live bell can never disagree with a refreshed
page.

Deliberately DERIVED from current data rather than stored as rows: every entry is something that
still needs doing, so the count falls on its own as staff work through it (approve a reservation
and it disappears) and can never drift out of sync with reality. There is no per-user read state
for the same reason -- 'unread' here means 'unhandled', which is the more useful signal for a
shop floor. (Which entries are NEW to a given person is tracked in their own browser, see
static/arabela_admin/notifications-live.js; nothing about it is stored on the server.)
"""
import hashlib
from datetime import datetime, time
from urllib.parse import urlencode

from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from arabela_admin.views import _SCHEDULED_STATUSES, _staff_display_name
from gowns.models import Gown
from reservations.models import Reservation, ReservationItem

# What each part of the bell is drawn from. The page includes these templates; the live feed
# renders the same ones and hands the HTML to the script to swap in.
REGION_TEMPLATES = {
    "badge": "arabela_admin/partials/notif_badge.html",
    "chip": "arabela_admin/partials/notif_chip.html",
    "list": "arabela_admin/partials/notif_list.html",
    "footer": "arabela_admin/partials/notif_footer.html",
    "modal": "arabela_admin/partials/notif_modal_body.html",
}

# The filter tabs in the "View all" window. Overdue returns and missed pick-ups share one tab:
# they are both "a gown that should be somewhere else by now".
GROUP_LABELS = {
    "reservation": "Reservations",
    "payment": "Payments",
    "returns": "Returns & pick-ups",
    "inventory": "Inventory",
    "customer": "Customers",
    "staff": "Staff",
}
_KIND_GROUP = {
    "reservation": "reservation",
    "payment": "payment",
    "overdue": "returns",
    "late_pickup": "returns",
    "inventory": "inventory",
    "customer": "customer",
    "staff": "staff",
}

# Things staff themselves cause (marking a gown Out-of-Stock, deactivating an account) still
# appear in the bell, but they never chime, pop a toast or light the "New" pill -- announcing
# someone's own action back to them is just noise.
SILENT_KINDS = frozenset({"inventory", "staff"})


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


def _item(*, key, kind, level, actor, text, subject, module, url, when, sort):
    return {
        # Stable identity of THIS piece of work (the reservation's reference code, the booking
        # line's id...). The bell's script calls an entry "new" when it sees a key it has not
        # announced before -- which keeps working when the count itself doesn't change.
        "key": key,
        "kind": kind,
        "group": _KIND_GROUP[kind],
        "level": level,
        "icon": kind,
        "alert": kind not in SILENT_KINDS,
        "actor": actor,
        "text": text,
        "subject": subject,
        # The same sentence as plain text, for the toast and the desktop pop-up.
        "title": f"{actor} {text} {subject}",
        "module": module,
        "url": url,
        "when": when,
        "ago": _ago(when),
        "sort": sort,
    }


def _user_is_owner(user, profile):
    role = profile.role if profile else UserProfile.Role.OWNER
    return bool(user.is_superuser or role == UserProfile.Role.OWNER)


def build_items(user):
    """Live work-queue notifications for the admin panel header, most urgent first.

    Each notification carries the URL of the module it belongs to, so clicking one lands
    on the page where the work is actually done.

    Role-aware: staff/manager accounts see the operational queues they can act on; the
    owner additionally sees staff-account items, mirroring the existing rule that staff
    get every module except Staff Management (see _is_owner / _require_owner in views).
    """
    profile = UserProfile.objects.filter(user=user).first()
    is_owner = _user_is_owner(user, profile)

    today = timezone.localdate()
    items = []

    # --- Reservations awaiting approval -------------------------------------------
    for r in (
        Reservation.objects.filter(status=Reservation.Status.PENDING)
        .select_related("customer__profile")
        .order_by("-created_at")[:10]
    ):
        items.append(_item(
            key=f"reservation:{r.reference_code}",
            kind="reservation",
            level="info",
            actor=r.display_customer_name,
            text="is waiting for approval on",
            subject=r.reference_code,
            module="Reservations",
            url=_searchable("arabela_admin:pending_approval", r.reference_code),
            when=r.created_at,
            sort=r.created_at,
        ))

    # --- Payment proofs uploaded but not yet verified ------------------------------
    for r in (
        Reservation.objects.filter(status=Reservation.Status.PENDING)
        .exclude(payment_proof_url="")
        .select_related("customer__profile")
        .order_by("-created_at")[:10]
    ):
        items.append(_item(
            key=f"payment:{r.reference_code}",
            kind="payment",
            level="warning",
            actor=r.display_customer_name,
            text="uploaded payment proof for",
            subject=r.reference_code,
            module="Payments",
            url=_searchable("arabela_admin:payment_verification", r.reference_code),
            when=r.created_at,
            sort=r.created_at,
        ))

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
        items.append(_item(
            key=f"overdue:{it.id}",
            kind="overdue",
            level="critical",
            actor=it.gown_name,
            text=f"is {days_late} day{'s' if days_late != 1 else ''} overdue for return from",
            subject=it.reservation.display_customer_name,
            module="Schedule",
            url=reverse("arabela_admin:rental_schedule"),
            when=it.return_date,
            # A plain date has no time; midnight is close enough for ordering, and the
            # sort only ever calls .timestamp() on each key in isolation.
            sort=datetime.combine(it.return_date, time.min),
        ))

    # --- Missed pick-ups (approved, still sitting in the shop past the pick-up date) --
    # Deliberately just a staff-facing nudge, no customer message -- unlike the overdue-
    # return reminder, nothing automatically contacts the customer for this one. Scoped
    # to _SCHEDULED_STATUSES (the same filter Active Reservations itself queries on) so
    # a click always lands on a row that's actually visible there.
    late_pickup_items = (
        ReservationItem.objects.filter(
            stage=ReservationItem.Stage.PICKUP,
            rental_date__lt=today,
            reservation__status__in=_SCHEDULED_STATUSES,
        )
        .select_related("reservation__customer__profile")
        .order_by("rental_date")[:10]
    )
    for it in late_pickup_items:
        days_late = (today - it.rental_date).days
        items.append(_item(
            key=f"late_pickup:{it.id}",
            kind="late_pickup",
            level="warning",
            actor=it.gown_name,
            text=f"is {days_late} day{'s' if days_late != 1 else ''} late for pick-up by",
            subject=it.reservation.display_customer_name,
            module="Reservations",
            url=_searchable("arabela_admin:active_reservations", it.reservation.reference_code),
            when=it.rental_date,
            sort=datetime.combine(it.rental_date, time.min),
        ))

    # --- Inventory needing attention ----------------------------------------------
    for g in (
        Gown.objects.filter(status=Gown.Status.OUT_OF_STOCK)
        .order_by("-updated_at")[:6]
    ):
        items.append(_item(
            key=f"inventory:{g.id}",
            kind="inventory",
            level="critical",
            actor=g.gown_id,
            text="is marked",
            subject=g.status,
            module="Inventory",
            url=_searchable("arabela_admin:gown_catalog", g.gown_id),
            when=g.updated_at,
            sort=g.updated_at,
        ))

    # --- Flagged customers ---------------------------------------------------------
    for p in (
        UserProfile.objects.filter(is_flagged=True)
        .select_related("user")[:6]
    ):
        items.append(_item(
            key=f"customer:{p.user_id}",
            kind="customer",
            level="warning",
            # Same resolution Client List itself uses for this row -- otherwise the
            # notification can name the account something that never appears on the
            # page it links to (this is the exact bug being fixed here: a blank
            # display_name plus a real first/last name showed as the username here
            # but as the full name on Client List, for the very same account).
            actor=UserProfile.customer_display_name(p.user),
            text="is flagged for review in",
            subject="Client List",
            module="Customers",
            # Search by email, not name: this shop's own data already has several
            # different customer accounts sharing the exact display name "Denmark
            # Concepcion" (different emails), so a name search would not narrow the
            # list to the one flagged account it's actually about.
            url=_searchable("arabela_admin:clients", p.user.email),
            when=None,
            sort=None,
        ))

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
            items.append(_item(
                key=f"staff:{p.user_id}",
                kind="staff",
                level="info",
                # Same resolution Staff Management's own table uses (_staff_row) --
                # deliberately not customer_display_name; that one is a customer
                # concept (checks profile.display_name), staff are named from their
                # real name instead. See _staff_display_name's own docstring.
                actor=_staff_display_name(p.user),
                text="is a deactivated staff account in",
                subject="Staff Management",
                module="Staff",
                # Username is unique, unlike a name -- narrows to exactly one row.
                url=_searchable("arabela_admin:staff_management", p.user.username),
                when=None,
                sort=None,
            ))

    # Critical first, then newest -- an overdue gown should never sit below a routine
    # approval just because the approval happens to be more recent.
    level_rank = {"critical": 0, "warning": 1, "info": 2}
    items.sort(key=lambda n: (
        level_rank.get(n["level"], 3),
        -(n["sort"].timestamp() if n["sort"] is not None else 0),
    ))
    return items


def feed_version(items):
    """A short fingerprint of everything the bell would show for these items. The live feed
    compares it with the version the browser already has and, when they match, answers with a
    tiny 'unchanged' instead of re-sending the bell's HTML. The relative ages ('5 min ago') are
    part of it, so those tick over on their own."""
    digest = hashlib.sha1()
    for n in items:
        digest.update("|".join((n["key"], n["level"], n["ago"], n["title"], n["url"])).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()[:16]


def _context_for(items, user_id):
    groups = []
    for key, label in GROUP_LABELS.items():
        count = sum(1 for n in items if n["group"] == key)
        if count:
            groups.append({"key": key, "label": label, "count": count})
    return {
        "admin_notifications": items,
        "admin_notification_count": len(items),
        "admin_notification_urgent": sum(1 for n in items if n["level"] == "critical"),
        "admin_notification_groups": groups,
        "admin_notification_keys": [n["key"] for n in items],
        "admin_notification_version": feed_version(items),
        "admin_notification_user": user_id,
    }


def empty_context():
    """What anonymous visitors and customers get: nothing, and it costs nothing."""
    return _context_for([], 0)


def build_context(user):
    """The bell's template variables for a signed-in staff member."""
    return _context_for(build_items(user), user.pk)


def feed_payload(user, known_version=""):
    """The live feed's JSON body. `known_version` is what the browser already shows; when the
    data hasn't changed since, the answer is just {"unchanged": true} -- no HTML, no items."""
    context = build_context(user)
    version = context["admin_notification_version"]
    if known_version and known_version == version:
        return {"version": version, "unchanged": True}
    return {
        "version": version,
        "count": context["admin_notification_count"],
        "urgent": context["admin_notification_urgent"],
        "items": [
            {
                "key": n["key"], "kind": n["kind"], "group": n["group"], "level": n["level"],
                "alert": n["alert"], "title": n["title"], "module": n["module"], "url": n["url"],
            }
            for n in context["admin_notifications"]
        ],
        # Rendered from the same templates the page includes, so a live update looks exactly like
        # what a refresh would show. No `request` on purpose: that would run every context
        # processor again -- including this feed -- for nothing.
        "html": {name: render_to_string(template, context) for name, template in REGION_TEMPLATES.items()},
    }
