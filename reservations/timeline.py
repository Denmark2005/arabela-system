"""Building the display timeline out of the raw ReservationStatusEvent log.

The log itself is append-only and deliberately dumb -- it stores what happened, when,
and who did it. Everything about how a timeline is *assembled and presented* lives here
instead, so the customer's order page and the admin's tables can never drift apart on
what "this gown's history" means.

Bulk helpers exist because the admin pages render dozens of reservations at once: the
per-object version would fire one query per row.
"""

from collections import defaultdict

from django.db.models import Q

from .models import ReservationStatusEvent

# Words that make an event bad news, matched against the stored label. Purely a
# presentation concern (which colour the dot gets) -- the label itself is written once
# when the event happens and is never rewritten, so changing this list restyles old
# history without rewriting it.
NEGATIVE_WORDS = ("rejected", "cancelled", "overdue", "needs repair")


def _annotate(events):
    """Tag an already-ordered, newest-first list for the templates, so the markup never
    has to do string matching. Returns the same list."""
    for index, event in enumerate(events):
        lowered = event.label.lower()
        event.is_latest = index == 0
        event.is_negative = any(word in lowered for word in NEGATIVE_WORDS)
    return events


def _newest_first(events):
    """Reverse of the model's stored (oldest-first) ordering. Ties are broken by id
    descending so two events written in the same millisecond -- which happens when one
    staff action records two things -- still come out in the order they were written."""
    return sorted(events, key=lambda e: (e.occurred_at, e.id), reverse=True)


def for_item(item, *, include_staff_only=False):
    """The ordered history to show for ONE gown: everything that happened to its
    reservation as a whole, plus everything that happened to this gown specifically.
    A sibling gown's pick-up is not this gown's history.

    Newest first, the way every order tracker presents it -- the thing the customer
    opened the page to check is the most recent thing, so it goes on top.

    This is what the CUSTOMER's order page shows, so staff-only notes (e.g. "needs
    repair" at check-in) are left out unless asked for. The admin pages build their
    timelines with attach_to_items / attach_to_reservations below, which always
    include them.
    """
    events = item.reservation.status_events.filter(Q(item__isnull=True) | Q(item_id=item.id))
    if not include_staff_only:
        events = events.filter(staff_only=False)
    return _annotate(list(events.order_by('-occurred_at', '-id')))


def attach_to_items(items):
    """Set `.timeline` on every item in one pass, using two queries total regardless of
    how many rows the page renders. `items` must already be a list."""
    if not items:
        return items

    reservation_ids = {item.reservation_id for item in items}
    events = list(ReservationStatusEvent.objects.filter(reservation_id__in=reservation_ids))

    reservation_wide = defaultdict(list)   # reservation_id -> events with no item
    per_item = defaultdict(list)           # item_id -> that item's own events
    for event in events:
        if event.item_id is None:
            reservation_wide[event.reservation_id].append(event)
        else:
            per_item[event.item_id].append(event)

    for item in items:
        combined = reservation_wide.get(item.reservation_id, []) + per_item.get(item.id, [])
        item.timeline = _annotate(_newest_first(combined))
    return items


def attach_to_reservations(reservations):
    """Set `.timeline` on every reservation in one extra query -- the whole booking's
    history, every gown included, which is what the admin's reservation-level tables
    want. Accepts a queryset and returns a list (the rows get iterated more than once).
    """
    reservations = list(reservations)
    if not reservations:
        return reservations

    by_reservation = defaultdict(list)
    events = ReservationStatusEvent.objects.filter(
        reservation_id__in=[r.id for r in reservations]
    ).order_by('-occurred_at', '-id')
    for event in events:
        by_reservation[event.reservation_id].append(event)

    for reservation in reservations:
        reservation.timeline = _annotate(by_reservation.get(reservation.id, []))
    return reservations
