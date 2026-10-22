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


FULLY_BOOKED_TEXT = "Fully booked for now"


def booking_badge(units, bookings_by_unit, blocks_by_unit, now, horizon_days=HORIZON_DAYS):
    """The ONE label a gown shows customers about when it can be booked -- on the collection card AND on the gown page, so the two
    can never disagree (they used to: the card read the gown's saved "Reserved" status, the page read the dates).

    -> {"state", "date", "text"}:
      * "now"  -- an event today (or tomorrow) can be booked: no label at all ("text" is "").
      * "from" -- the first event date that can be booked is later: "Available from Oct 26". The same day is the first one the
                  calendar lets the customer pick, because it is worked out by the same plan_unit rule, unit by unit.
      * "full" -- no event date can be booked in the whole calendar (the next `horizon_days` days): "Fully booked for now"."""
    today = now.date()
    earliest = earliest_pickup_day(now)
    first_open = None
    for offset in range(horizon_days + 1):
        event = today + timedelta(days=offset)
        if day_problem(event, now):
            continue
        if any(
            plan_unit(event, bookings_by_unit.get(unit.id, ()), blocks_by_unit.get(unit.id, ()), earliest)[0] is not None
            for unit in units
        ):
            first_open = event
            break
    if first_open is None:
        return {"state": "full", "date": None, "text": FULLY_BOOKED_TEXT}
    if first_open <= today + timedelta(days=1):
        return {"state": "now", "date": first_open, "text": ""}
    label = f"{first_open:%b} {first_open.day}" + (f", {first_open.year}" if first_open.year != today.year else "")
    return {"state": "from", "date": first_open, "text": f"Available from {label}"}


def open_event_dates(units, bookings_by_unit, blocks_by_unit, now, horizon_days):
    """The set of event dates (ISO) on which at least one of these units can be rented."""
    calendar = plan_calendar(units, bookings_by_unit, blocks_by_unit, now, horizon_days, lambda unit: "")
    return {day for day, value in calendar["*"].items() if isinstance(value, list)}


# --- ONE physical gown on ONE event date: the admin's "Check availability" and bookings staff record (walk-in, Facebook,
# phone). Same rules as the gown page and checkout -- plan_unit above -- so the answer can never disagree with them; the only
# extra is the walk-in who takes the gown at the counter (a fixed pick-up day, the shop's 5 PM pick-up time doesn't apply to a
# customer who is standing there).
OWNER_BACKDATE_DAYS = 30         # the owner may record a booking up to this many days back (a missed walk-in, a correction)

# Why a check said no (`reason`), besides plan_unit's own 'booked' / 'rest'.
OUT_OF_STOCK = "out_of_stock"
PAST = "past"
CLOSED = "closed"
PICKUP_IN_FUTURE = "pickup_in_future"
PICKUP_AFTER_EVENT = "pickup_after_event"


def unit_busy(unit_id, start: date, end: date):
    """What holds ONE physical gown on any day of start..end (the past included), as (bookings, blocks): lists of
    (start, end, row). The same rule as load_busy_ranges: a gown line that is back or that staff cancelled, and a Rejected or
    Cancelled booking, hold nothing; every block counts (staff blocks and the automatic rest days)."""
    items = (
        ReservationItem.objects.filter(gown_id=unit_id, rental_date__lte=end, return_date__gte=start)
        .exclude(stage__in=ReservationItem.FINISHED_STAGES)
        .exclude(reservation__status__in=[Reservation.Status.REJECTED, Reservation.Status.CANCELLED])
        .select_related("reservation__customer__profile")
        .order_by("rental_date", "id")
    )
    blocks = (
        GownUnavailability.objects.filter(gown_id=unit_id, start_date__lte=end, end_date__gte=start)
        .select_related("auto_for_item__reservation")
        .order_by("start_date", "id")
    )
    return [(i.rental_date, i.return_date, i) for i in items], [(b.start_date, b.end_date, b) for b in blocks]


def _conflict(row_start, row_end, row):
    """One thing in the way, the way staff read it: who has the gown (and the booking number), or why it is blocked."""
    if isinstance(row, ReservationItem):
        reservation = row.reservation
        label = f"{reservation.display_customer_name} ({reservation.reference_code})"
        if reservation.status == Reservation.Status.PENDING:
            label += " · awaiting payment approval"
        return {"kind": "booking", "start": row_start, "end": row_end, "label": label,
                "reference": reservation.reference_code}
    if row.auto_for_item_id and row.auto_for_item is not None:
        label = f"Rest days after {row.auto_for_item.reservation.reference_code}"
    else:
        label = f"Blocked for {row.get_reason_display().lower()}"
    return {"kind": "block", "start": row_start, "end": row_end, "label": label, "reference": ""}


def check_unit(gown, event: date, *, picked_up_on=None, now=None, backdate_days=0):
    """Can this exact gown be booked for this event date? -> dict
        ok, reason, event, pickup, return_date, rest_until, conflicts (list of _conflict dicts).

    picked_up_on: the customer takes the gown at the counter -- the pick-up is that fixed day (today; the owner may enter an
    earlier day to record a walk-in after the fact). Otherwise the pick-up is planned exactly like checkout: the earliest free
    day up to 2 days before the event, never a day the shop is closed for pick-ups.
    backdate_days: how far back a date may be (0 for staff, OWNER_BACKDATE_DAYS for the owner)."""
    now = now or now_local()
    today = now.date()
    return_day = event + timedelta(days=RETURN_DAYS)
    rest_until = return_day + timedelta(days=COOLDOWN_DAYS)
    result = {"ok": False, "reason": "", "event": event, "pickup": None, "return_date": return_day,
              "rest_until": rest_until, "conflicts": []}
    if gown.status == Gown.Status.OUT_OF_STOCK:
        result["reason"] = OUT_OF_STOCK
        return result
    oldest = today - timedelta(days=max(0, backdate_days))

    if picked_up_on is not None:
        if picked_up_on > today:
            result["reason"] = PICKUP_IN_FUTURE
            return result
        if picked_up_on < oldest or event < oldest:
            result["reason"] = PAST
            return result
        if picked_up_on > event:
            result["reason"] = PICKUP_AFTER_EVENT
            return result
        bookings, blocks = unit_busy(gown.id, picked_up_on, rest_until)
        in_the_way = [r for r in bookings + blocks if r[0] <= return_day and r[1] >= picked_up_on]
        if in_the_way:
            result["reason"] = "booked"
            result["conflicts"] = [_conflict(*r) for r in in_the_way]
            return result
        resting_into = [r for r in bookings if r[0] <= rest_until and r[1] > return_day]
        if COOLDOWN_DAYS and resting_into:
            result["reason"] = "rest"
            result["conflicts"] = [_conflict(*r) for r in resting_into]
            return result
        result.update(ok=True, pickup=picked_up_on)
        return result

    # A planned pick-up can't be in the past: a booking for a date already gone is one where the customer already has the gown.
    if event < today:
        result["reason"] = PAST
        return result
    if day_problem(event, now) == "closed":
        result["reason"] = CLOSED
        return result
    earliest = earliest_pickup_day(now)
    first = max(event - timedelta(days=EARLY_PICKUP_DAYS), earliest)
    bookings, blocks = unit_busy(gown.id, first, rest_until)
    pickup, reason = plan_unit(
        event, [(s, e) for s, e, _ in bookings], [(s, e) for s, e, _ in blocks], earliest,
    )
    if pickup is None:
        result["reason"] = reason
        if reason == "booked":
            # Even a pick-up on the event day itself would touch these -- they are what makes the date impossible.
            rows = [r for r in bookings + blocks if r[0] <= return_day and r[1] >= event]
        else:
            rows = [r for r in bookings if r[0] <= rest_until and r[1] > return_day]
        result["conflicts"] = [_conflict(*r) for r in rows]
        return result
    result.update(ok=True, pickup=pickup)
    return result


def next_free_event(gown, after: date, now=None, horizon_days=HORIZON_DAYS):
    """The first event date after `after` (and not before today) on which this exact gown can be booked with a planned
    pick-up, or None within the calendar's horizon -- what "the next free date" means everywhere a gown is checked."""
    now = now or now_local()
    today = now.date()
    if gown.status == Gown.Status.OUT_OF_STOCK:
        return None
    bookings, blocks = load_busy_ranges([gown.id], today, horizon_days)
    earliest = earliest_pickup_day(now)
    day = max(after + timedelta(days=1), today)
    last = today + timedelta(days=horizon_days)
    while day <= last:
        if not day_problem(day, now) and plan_unit(day, bookings.get(gown.id, ()), blocks.get(gown.id, ()), earliest)[0]:
            return day
        day += timedelta(days=1)
    return None


def check_message(gown, result, with_names=True) -> str:
    """The sentence staff read for a check_unit result. with_names=False leaves out what is in the way -- for a screen that
    lists those itself underneath (with links), so nothing is said twice."""
    label = f"{gown.name} ({gown.gown_id})"
    reason = result["reason"]
    if result["ok"]:
        return (
            f"Available. Pick-up {_day_text(result['pickup'])}, event {_day_text(result['event'])}, "
            f"return {_day_text(result['return_date'])}. After that the gown rests until {_day_text(result['rest_until'])}."
        )
    if reason == OUT_OF_STOCK:
        return f"{label} is Out-of-Stock, so it can't be booked."
    if reason == PAST:
        return ("That date has already passed. If the customer already has the gown, tick \"Customer takes the gown now\" "
                "and enter the day they took it (only the owner can enter an earlier day).")
    if reason == CLOSED:
        return (f"Today's pick-up time ended at {PICKUP_CLOSES.strftime('%I:%M %p').lstrip('0')}. If the customer is here and "
                "takes the gown now, tick \"Customer takes the gown now\".")
    if reason == PICKUP_IN_FUTURE:
        return "\"Customer takes the gown now\" means the gown leaves today -- the pick-up day can't be later than today."
    if reason == PICKUP_AFTER_EVENT:
        return "The pick-up day can't be after the event date."
    names = "; ".join(f"{c['label']}, {_day_text(c['start'])} – {_day_text(c['end'])}" for c in result["conflicts"]) if with_names else ""
    if reason == "rest":
        return (f"Not available. {label} would not get its {COOLDOWN_DAYS} rest days before the next booking"
                + (f" ({names})." if names else "."))
    return f"Not available. {label} is already taken on these dates" + (f": {names}." if names else ".")


def _day_text(day: date) -> str:
    return f"{day:%b} {day.day}, {day.year}"


def bookable_units(label: str, name: str):
    """A product's physical gowns: same name and category, not Out-of-Stock."""
    return list(
        Gown.objects.filter(category=label, name__iexact=name).exclude(status=Gown.Status.OUT_OF_STOCK).order_by("id")
    )


def load_busy_ranges(unit_ids, today: date, horizon_days: int, exclude_block_id=None):
    """(bookings_by_unit, blocks_by_unit): the date ranges each unit is spoken for from today on.

    bookings = customers' reservations (a gown that is Returned, or that staff cancelled before pick-up, releases its unit;
    Rejected/Cancelled bookings never held it);
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
        .exclude(stage__in=ReservationItem.FINISHED_STAGES)
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
