"""Which event dates a customer can book, and the days the gown is picked up and returned -- ONE set of rules used by the gown
page calendar, by checkout, and by the admin's "release blocked dates" reminder.

The rules (Philippine time, see TIME_ZONE):
  * The customer picks an EVENT date. Which event dates can be booked: the gown must be free from a pick-up day up to 2 days
    before the event (when the days before are taken, the event day itself) through 2 days after it, and then rest
    COOLDOWN_DAYS (5) days that must not run into a later customer's booking.
  * The FREE days are 2 days before the event and 2 days after it (free_days). By default the pick-up is the earliest free day
    of those (plan_unit) and the return is 2 days after the event.
  * The customer may also choose an earlier pick-up or a later return (unit_window): each day outside the free days costs the
    shop's fee per day (reservations/ledger.py). A return 1 day after the event may be chosen too; returning any day up to 2 days
    after it is free, so the gown stays booked until then (kept_until).
  * Pick-up is only 1:00 PM - 5:00 PM. Today can be chosen until 5:00 PM; from then the earliest pick-up is tomorrow.
  * The rental (pick-up .. the day the gown is kept until) must not touch ANY booking or block of that unit. The rest days after
    it must not run into a later customer's booking (they may overlap a staff block: the gown is being cleaned anyway).
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
LONGEST_RETURN_DAYS = 365        # how far from today a return day can be chosen: the same one-year limit staff have when they move one


def rental_days_needed() -> int:
    """Days in a row one rental needs: the event day, the 2 return days and the cleaning days."""
    return 1 + RETURN_DAYS + COOLDOWN_DAYS


def free_days(event: date):
    """(the first free pick-up day, the last free return day) of an event: 2 days before it and 2 days after it."""
    return event - timedelta(days=EARLY_PICKUP_DAYS), event + timedelta(days=RETURN_DAYS)


def extra_days(event: date, pickup: date, ret: date):
    """(days picked up before the free days, days returned after them) for a booking of these dates."""
    free_pickup, free_return = free_days(event)
    return max(0, (free_pickup - pickup).days), max(0, (ret - free_return).days)


def kept_until(event: date, ret: date) -> date:
    """The last day the gown is held for a booking returned on `ret`: never before the free return day (returning up to 2 days after
    the event is free, so a customer who chose 1 day after may still bring it on the second day)."""
    return max(ret, event + timedelta(days=RETURN_DAYS))


def last_return_day(today: date, event: date) -> date:
    """The latest return day the website offers for an event (one year from today, never before the free return day)."""
    return max(today + timedelta(days=LONGEST_RETURN_DAYS), event + timedelta(days=RETURN_DAYS))


def pickup_allowed(event: date, pickup: date, lowest: date) -> bool:
    """Whether `pickup` may be chosen on a unit whose earliest possible pick-up is `lowest`: any day from it up to the day before the
    event -- the event day itself only when no earlier day is possible (the same rule the default pick-up follows)."""
    if lowest >= event:
        return pickup == event
    return lowest <= pickup <= event - timedelta(days=1)


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


def unit_window(event: date, bookings, blocks, earliest: date, last_return: date):
    """The days ONE physical gown can be booked around this event date -> ((default pick-up, lowest pick-up, highest return), None) or
    (None, reason). The event must be bookable on the unit (plan_unit); then:
      * lowest pick-up: the earliest day the gown is free in a row up to the default pick-up (never before `earliest`);
      * highest return: the latest day the gown is free in a row after the free return day, whose rest days still stay clear of the next
        customer's booking (never after `last_return`).
    Every pick-up from the lowest one (pickup_allowed) together with every return from 1 day after the event up to the highest one can be
    booked on this unit: the free stretch is one block of days, so any smaller stretch inside it is free too."""
    pickup, reason = plan_unit(event, bookings, blocks, earliest)
    if pickup is None:
        return None, reason
    taken = list(bookings) + list(blocks)
    free_return = event + timedelta(days=RETURN_DAYS)
    # Nothing touches pickup .. free_return (plan_unit checked it), so every range ends before the pick-up or starts after the free return.
    lowest = max([earliest] + [end + timedelta(days=1) for start, end in taken if end < pickup])
    highest = last_return
    for start, _end in taken:
        if start > free_return:
            highest = min(highest, start - timedelta(days=1))
    if COOLDOWN_DAYS:
        for start, _end in bookings:
            if start > free_return:
                highest = min(highest, start - timedelta(days=COOLDOWN_DAYS + 1))
    return (pickup, min(lowest, pickup), max(highest, free_return)), None


def unit_fits(event: date, pickup: date, ret: date, bookings, blocks, earliest: date, last_return: date) -> bool:
    """Can ONE physical gown be booked from `pickup` to `ret` for this event (the rule the gown page offers and checkout keeps)?"""
    window, _reason = unit_window(event, bookings, blocks, earliest, last_return)
    if window is None:
        return False
    _default, lowest, highest = window
    return pickup_allowed(event, pickup, lowest) and event + timedelta(days=1) <= ret <= highest


def planned_days(bookings_by_unit, blocks_by_unit, today: date, at_least: int = HORIZON_DAYS) -> int:
    """How far ahead the gown page's calendar is planned: HORIZON_DAYS, or further when something is booked or blocked later than that (up
    to a year), so a far-off event -- a wedding next year -- is judged like any other. Past the planned days nothing is booked (open_from)."""
    ends = [end for ranges in list(bookings_by_unit.values()) + list(blocks_by_unit.values()) for _start, end in ranges]
    if not ends:
        return at_least
    return max(at_least, min((max(ends) - today).days + EARLY_PICKUP_DAYS + 1, LONGEST_RETURN_DAYS))


def open_from(units, bookings_by_unit, blocks_by_unit, earliest: date, size_code_of):
    """{size code or '*': ISO date}: from this day on, one gown of that size has nothing booked or blocked any more. The gown page uses it for
    event days further ahead than the calendar was planned: any pick-up from it (and any return up to a year from today) is free."""
    result = {}
    for unit in units:
        taken = list(bookings_by_unit.get(unit.id, ())) + list(blocks_by_unit.get(unit.id, ()))
        free = max([earliest] + [end + timedelta(days=1) for _start, end in taken])
        for key in ("*", size_code_of(unit)):
            if key and (key not in result or free < result[key]):
                result[key] = free
    return {key: day.isoformat() for key, day in result.items()}


def plan_calendar(units, bookings_by_unit, blocks_by_unit, now, horizon_days, size_code_of):
    """The whole calendar of one product, ready to hand to the gown page: {size code or '*': {event ISO date: value}}.

    value = [default pick-up ISO date, number of free units, windows] when the date can be booked, else the reason as a string
    ('past', 'closed', 'booked', 'rest'). windows: one [lowest pick-up, highest return] per free unit (duplicates left out), each
    as days from the event (-9 = 9 days before it, 30 = 30 days after it) -- what the page needs to offer an earlier pick-up or a
    later return on a gown that really is free for all those days (see unit_window). '*' means "any size" (before a size is
    chosen). Days from today to today + horizon_days."""
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
            pickups, reasons, windows = [], [], []
            last_return = last_return_day(today, event)
            for unit in members:
                window, reason = unit_window(event, bookings_by_unit.get(unit.id, ()), blocks_by_unit.get(unit.id, ()), earliest, last_return)
                if window is not None:
                    pickups.append(window[0])
                    span = [(window[1] - event).days, (window[2] - event).days]
                    if span not in windows:
                        windows.append(span)
                else:
                    reasons.append(reason)
            if pickups:
                calendar[key][event.isoformat()] = [min(pickups).isoformat(), len(pickups), sorted(windows)]
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


def check_unit(gown, event: date, *, picked_up_on=None, now=None, backdate_days=0, rows=None):
    """Can this exact gown be booked for this event date? -> dict
        ok, reason, event, pickup, return_date, rest_until, conflicts (list of _conflict dicts).

    picked_up_on: the customer takes the gown at the counter -- the pick-up is that fixed day (today; the owner may enter an
    earlier day to record a walk-in after the fact). Otherwise the pick-up is planned exactly like checkout: the earliest free
    day up to 2 days before the event, never a day the shop is closed for pick-ups.
    backdate_days: how far back a date may be (0 for staff, OWNER_BACKDATE_DAYS for the owner).
    rows: (bookings, blocks) exactly as unit_busy returns them, already read for a wider range -- calendar_days hands the same
    rows to every day of a month so the month costs one database read instead of two per day. The rules are the same code either
    way; None (what every other caller passes) reads what this one date needs."""
    now = now or now_local()

    def busy(start, end):
        if rows is None:
            return unit_busy(gown.id, start, end)
        held, blocked = rows
        return ([r for r in held if r[0] <= end and r[1] >= start], [r for r in blocked if r[0] <= end and r[1] >= start])

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
        bookings, blocks = busy(picked_up_on, rest_until)
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
    bookings, blocks = busy(first, rest_until)
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


def calendar_days(gown, first: date, last: date, *, picked_up_on=None, now=None, backdate_days=0):
    """check_unit's answer for every event date from `first` to `last` -> {date: result}, for the New Booking calendar.

    Each day is judged by check_unit itself (so the calendar can never disagree with the answer given after a date is chosen), fed
    from one read of everything that holds the gown over the whole stretch those answers can look at."""
    now = now or now_local()
    rows = ([], [])
    if gown.status != Gown.Status.OUT_OF_STOCK:
        low = first - timedelta(days=EARLY_PICKUP_DAYS)
        if picked_up_on is not None:
            low = min(low, picked_up_on)
        rows = unit_busy(gown.id, low, last + timedelta(days=RETURN_DAYS + COOLDOWN_DAYS))
    days, day = {}, first
    while day <= last:
        days[day] = check_unit(gown, day, picked_up_on=picked_up_on, now=now, backdate_days=backdate_days, rows=rows)
        day += timedelta(days=1)
    return days


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
