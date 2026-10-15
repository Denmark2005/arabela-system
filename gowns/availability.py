"""Which event dates a customer can book, and the day the gown is picked up -- ONE set of rules used by the gown page
calendar, by checkout, and by the admin's "release blocked dates" reminder.

The rules (Philippine time, see TIME_ZONE):
  * The customer picks an EVENT date. The gown comes back 2 days after it, then rests COOLDOWN_DAYS (5) days.
  * Pick-up is the earliest free day up to 2 days before the event. When the days before are taken, it is the event day
    itself. Pick-up is only 1:00 PM - 5:00 PM.
  * Today can be chosen until 5:00 PM. From 5:00 PM no pick-up can still happen today, so the earliest pick-up is tomorrow
    (today is closed as an event date, tomorrow still works with a same-day pick-up).
  * The rental (pick-up .. return) must not touch ANY booking or block of that unit. The rest days after the return must
    not run into a later customer's booking (they may overlap a staff block: the gown is being cleaned anyway).
The planning functions work on plain dates and never touch the database; only bookable_units / load_busy_ranges at the
bottom read it, so every caller feeds the planner the same data.
"""
from collections import defaultdict
from datetime import date, time, timedelta

from django.utils import timezone

from gowns.models import COOLDOWN_DAYS, Gown, GownUnavailability
from reservations.models import Reservation, ReservationItem

PICKUP_OPENS = time(13, 0)       # 1:00 PM
PICKUP_CLOSES = time(17, 0)      # 5:00 PM
RETURN_DAYS = 2                  # the gown is due back this many days after the event
EARLY_PICKUP_DAYS = 2            # ... and can be collected up to this many days before it
PICKUP_HOURS_TEXT = "1:00 PM to 5:00 PM"
HORIZON_DAYS = 120               # how far ahead the calendar is planned (about four months)


def rental_days_needed() -> int:
    """Days in a row one rental needs: the event day, the 2 return days and the cleaning days."""
    return 1 + RETURN_DAYS + COOLDOWN_DAYS


def now_local():
    """The current time in the shop's time zone. One function so tests (and nothing else) can change the clock."""
    return timezone.localtime()


def earliest_pickup_day(now) -> date:
    """The first day a gown can still be collected: today until the shop closes at 5 PM, tomorrow after that."""
    today = now.date()
    return today if now.time() < PICKUP_CLOSES else today + timedelta(days=1)


def day_problem(event: date, now):
    """Why this event date can never be booked right now, whatever the gown: 'past', 'closed' (today, after 5 PM), or None."""
    if event < now.date():
        return "past"
    if event < earliest_pickup_day(now):
        return "closed"
    return None


def _touches(start: date, end: date, ranges) -> bool:
    return any(range_start <= end and range_end >= start for range_start, range_end in ranges)


def plan_unit(event: date, bookings, blocks, earliest: date):
    """Can ONE physical gown be rented for this event date? -> (pickup_date, None) or (None, reason).

    `bookings` and `blocks` are lists of (start, end) date pairs. reason: 'booked' (the rental itself would touch a booking or
    a block) or 'rest' (the rental fits, but its rest days would run into a later booking)."""
    return_day = event + timedelta(days=RETURN_DAYS)
    first = max(event - timedelta(days=EARLY_PICKUP_DAYS), earliest)
    taken = list(bookings) + list(blocks)
    pickup = None
    day = first
    while day <= event:
        if not _touches(day, return_day, taken):
            pickup = day
            break
        day += timedelta(days=1)
    if pickup is None:
        return None, "booked"
    if COOLDOWN_DAYS and _touches(return_day + timedelta(days=1), return_day + timedelta(days=COOLDOWN_DAYS), bookings):
        return None, "rest"
    return pickup, None


def plan_calendar(units, bookings_by_unit, blocks_by_unit, now, horizon_days, size_code_of):
    """The whole calendar of one product, ready to hand to the gown page: {size code or '*': {event ISO date: value}}.

    value = [pickup ISO date, number of free units] when the date can be booked, else the reason as a string
    ('past', 'closed', 'booked', 'rest'). '*' means "any size" (before a size is chosen). Days from today to
    today + horizon_days."""
    today = now.date()
    earliest = earliest_pickup_day(now)
    keys = {"*": list(units)}
    for unit in units:
        code = size_code_of(unit)
        if code:
            keys.setdefault(code, []).append(unit)
    calendar = {key: {} for key in keys}
    for offset in range(horizon_days + 1):
        event = today + timedelta(days=offset)
        problem = day_problem(event, now)
        for key, members in keys.items():
            if problem:
                calendar[key][event.isoformat()] = problem
                continue
            pickups, reasons = [], []
            for unit in members:
                pickup, reason = plan_unit(event, bookings_by_unit.get(unit.id, ()), blocks_by_unit.get(unit.id, ()), earliest)
                if pickup is not None:
                    pickups.append(pickup)
                else:
                    reasons.append(reason)
            if pickups:
                calendar[key][event.isoformat()] = [min(pickups).isoformat(), len(pickups)]
            else:
                # 'rest' only when some unit's rental fitted and only its rest days were in the way -- the more useful hint.
                calendar[key][event.isoformat()] = "rest" if "rest" in reasons else "booked"
    return calendar


def open_event_dates(units, bookings_by_unit, blocks_by_unit, now, horizon_days):
    """The set of event dates (ISO) on which at least one of these units can be rented."""
    calendar = plan_calendar(units, bookings_by_unit, blocks_by_unit, now, horizon_days, lambda unit: "")
    return {day for day, value in calendar["*"].items() if isinstance(value, list)}


def bookable_units(label: str, name: str):
    """A product's physical gowns: same name and category, not Out-of-Stock."""
    return list(
        Gown.objects.filter(category=label, name__iexact=name).exclude(status=Gown.Status.OUT_OF_STOCK).order_by("id")
    )


def load_busy_ranges(unit_ids, today: date, horizon_days: int, exclude_block_id=None):
    """(bookings_by_unit, blocks_by_unit): the date ranges each unit is spoken for from today on.

    bookings = customers' reservations (stage 'Returned' releases a unit; Rejected/Cancelled never held it);
    blocks = GownUnavailability rows (staff blocks and the automatic cooldown). `exclude_block_id` leaves one block out,
    which is how the admin asks "what would opening up if staff released this one?". Reads a little past the horizon so
    the rest days of the last bookable events are checked against real bookings too."""
    far = today + timedelta(days=horizon_days + COOLDOWN_DAYS + RETURN_DAYS + 2)
    bookings, blocks = defaultdict(list), defaultdict(list)
    unit_ids = list(unit_ids)
    if not unit_ids:
        return bookings, blocks
    booked = (
        ReservationItem.objects.filter(gown_id__in=unit_ids, return_date__gte=today, rental_date__lte=far)
        .exclude(stage=ReservationItem.Stage.RETURNED)
        .exclude(reservation__status__in=[Reservation.Status.REJECTED, Reservation.Status.CANCELLED])
        .values_list("gown_id", "rental_date", "return_date")
    )
    for gown_id, start, end in booked:
        bookings[gown_id].append((start, end))
    held = GownUnavailability.objects.filter(gown_id__in=unit_ids, end_date__gte=today, start_date__lte=far)
    if exclude_block_id is not None:
        held = held.exclude(id=exclude_block_id)
    for gown_id, start, end in held.values_list("gown_id", "start_date", "end_date"):
        blocks[gown_id].append((start, end))
    return bookings, blocks
