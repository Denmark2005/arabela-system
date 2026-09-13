"""Automatic pick-up and return reminders for customers.

Why this exists: a rental shop's worst operational cost is a gown that comes back
late. Every late day is a day that gown cannot go out to the next customer, and
until now nothing in this system told a customer their return date was coming --
staff had to notice on the calendar and chase people by hand.

How it runs without a scheduler: this project has no cron, no Celery, nothing with
its own clock. The sweep is triggered instead by staff opening the admin dashboard
(`run_daily_sweep_if_due`), and the ReminderRun singleton stops it firing more than
once a day. The trade-off is honest: if nobody opens the admin panel on a given day,
that day's pass is skipped and catches up the next time someone does.

Three deliberate design rules:

1. A WRONG reminder is worse than a missing one. Telling a customer their gown is
   overdue when they never picked it up would embarrass the shop, so every rule is
   keyed on the gown's actual stage, not just on dates.
2. Nobody gets the same reminder twice. The dedupe ledger is the ReservationStatusEvent
   log itself rather than a new set of boolean columns -- the record that a reminder
   was sent IS the proof the shop can point at in a deposit dispute, and it shows up
   in the customer's own timeline, so the notification is never deniable by either side.
3. Sending a reminder must never be able to break the page that triggered it. Staff
   opening the dashboard cannot be shown an error because a customer message failed.
"""

from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import CustomerMessage

from .models import Reservation, ReservationItem, ReservationStatusEvent, ReminderRun

# How long a gown may sit overdue before the customer is reminded again. Daily nagging
# would read as harassment and would bury the customer's real history under repeats;
# silence after one notice would let a gown drift for weeks. Three days is the middle.
OVERDUE_REPEAT_DAYS = 3

# When the system stops nagging about an overdue gown and leaves it to staff.
#
# Two reasons, both learned from the real data. A gown two months late is no longer a
# customer who forgot -- it is a collections problem that needs a phone call, and an
# automated "please return this" every three days for a year would be absurd. And on the
# very first sweep, without this cap, every long-abandoned booking still sitting in the
# system would fire a reminder at once, so shipping the feature would itself spam people
# about bookings everyone had moved on from.
OVERDUE_MAX_DAYS = 30

# Stages where the gown is physically with the customer, so RETURN dates are the ones
# that matter. Pick-up means it is still in the shop; Returned means it is back.
_OUT_STAGES = (
    ReservationItem.Stage.RESERVED,
    ReservationItem.Stage.RETURN,
    ReservationItem.Stage.OVERDUE,
)

# Only approved bookings get reminders. A Pending reservation is waiting on STAFF, not
# on the customer -- nagging someone to collect a gown nobody has approved yet would be
# the system blaming the customer for the shop's own queue.
_REMINDABLE_STATUSES = (Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE)

PICKUP_TOMORROW = 'pickup_tomorrow'
PICKUP_TODAY = 'pickup_today'
RETURN_TOMORROW = 'return_tomorrow'
RETURN_TODAY = 'return_today'
OVERDUE = 'overdue'

# The label written to the timeline for each reminder kind. These double as the dedupe
# key, so they must stay stable: editing one would make the sweep think that reminder
# was never sent and re-send it to everyone it had already reached.
EVENT_LABELS = {
    PICKUP_TOMORROW: 'Reminder sent: pick-up is tomorrow',
    PICKUP_TODAY: 'Reminder sent: pick-up is today',
    RETURN_TOMORROW: 'Reminder sent: return due tomorrow',
    RETURN_TODAY: 'Reminder sent: return due today',
    OVERDUE: 'Reminder sent: return is overdue',
}

_MESSAGE_CATEGORIES = {
    PICKUP_TOMORROW: CustomerMessage.Category.RESERVATION_REMINDER,
    PICKUP_TODAY: CustomerMessage.Category.RESERVATION_REMINDER,
    RETURN_TOMORROW: CustomerMessage.Category.RESERVATION_REMINDER,
    RETURN_TODAY: CustomerMessage.Category.RESERVATION_REMINDER,
    OVERDUE: CustomerMessage.Category.RETURN_OVERDUE,
}


def _due_kind(item, today):
    """Which reminder this gown needs today, or None.

    Keyed on stage first so the message can never contradict reality: a gown still
    sitting in the shop gets pick-up reminders, a gown that is out gets return
    reminders, and a gown already checked back in gets nothing at all.
    """
    if item.stage == ReservationItem.Stage.PICKUP:
        days_until = (item.rental_date - today).days
        if days_until == 1:
            return PICKUP_TOMORROW
        if days_until == 0:
            return PICKUP_TODAY
        # A pick-up date that has already passed is a no-show, which is a staff
        # conversation, not an automated nudge -- and it is emphatically not "overdue".
        return None

    if item.stage in _OUT_STAGES:
        days_until = (item.return_date - today).days
        if days_until == 1:
            return RETURN_TOMORROW
        if days_until == 0:
            return RETURN_TODAY
        if days_until < 0:
            # Past the cap this stops being an automated nudge and becomes staff work.
            return OVERDUE if -days_until <= OVERDUE_MAX_DAYS else None

    return None


def _body_for(kind, item, today):
    """The message the customer actually reads. Written in plain language, always
    naming the gown and the date so it stands on its own in an inbox."""
    gown = item.gown_name
    code = item.reservation.reference_code

    if kind == PICKUP_TOMORROW:
        return (
            f"Reminder: your rental of {gown} starts tomorrow, "
            f"{item.rental_date:%B %d, %Y}. Please visit Arabela to pick up your gown. "
            f"Reference: {code}."
        )
    if kind == PICKUP_TODAY:
        return (
            f"Reminder: your rental of {gown} starts today, "
            f"{item.rental_date:%B %d, %Y}. Your gown is ready for pick-up at Arabela. "
            f"Reference: {code}."
        )
    if kind == RETURN_TOMORROW:
        return (
            f"Reminder: {gown} is due back tomorrow, {item.return_date:%B %d, %Y}. "
            f"Returning on time keeps your security deposit fully refundable. "
            f"Reference: {code}."
        )
    if kind == RETURN_TODAY:
        return (
            f"Reminder: {gown} is due back today, {item.return_date:%B %d, %Y}. "
            f"Returning on time keeps your security deposit fully refundable. "
            f"Reference: {code}."
        )

    days_late = (today - item.return_date).days
    day_word = "day" if days_late == 1 else "days"
    return (
        f"{gown} was due back on {item.return_date:%B %d, %Y} and is now {days_late} "
        f"{day_word} overdue. Please return it to Arabela as soon as possible -- "
        f"overdue rentals may affect your security deposit refund. Reference: {code}."
    )


def _already_sent(kind, item_id, sent_map, today):
    """Whether this exact reminder has already gone out.

    Every kind but OVERDUE is once-and-done: the date it refers to happens once. OVERDUE
    is the exception, because a gown can stay out for weeks and one notice on day one
    would be forgotten -- so it repeats, but only after OVERDUE_REPEAT_DAYS of silence.
    """
    last_sent_on = sent_map.get((item_id, EVENT_LABELS[kind]))
    if last_sent_on is None:
        return False
    if kind != OVERDUE:
        return True
    return (today - last_sent_on).days < OVERDUE_REPEAT_DAYS


def find_due_reminders(today=None):
    """Every reminder that should go out today, as (item, kind) pairs.

    Split out from sending so the decision logic can be tested on its own, and so a
    future email channel can reuse it without duplicating a single rule.
    """
    today = today or timezone.localdate()
    horizon = today + timedelta(days=1)

    candidates = list(
        ReservationItem.objects
        .filter(reservation__status__in=_REMINDABLE_STATUSES)
        .filter(
            Q(stage=ReservationItem.Stage.PICKUP, rental_date__lte=horizon)
            | Q(stage__in=_OUT_STAGES, return_date__lte=horizon,
                return_date__gte=today - timedelta(days=OVERDUE_MAX_DAYS))
        )
        .select_related('reservation__customer')
    )
    if not candidates:
        return []

    # One query for the whole sweep's dedupe ledger, keyed (item, label) -> last sent
    # date. Checking per item would be one query per gown every single day.
    sent_map = {}
    history = ReservationStatusEvent.objects.filter(
        item_id__in=[item.id for item in candidates],
        label__in=list(EVENT_LABELS.values()),
    ).values_list('item_id', 'label', 'occurred_at')
    for item_id, label, occurred_at in history:
        occurred_on = timezone.localtime(occurred_at).date()
        key = (item_id, label)
        if key not in sent_map or occurred_on > sent_map[key]:
            sent_map[key] = occurred_on

    due = []
    for item in candidates:
        kind = _due_kind(item, today)
        if kind and not _already_sent(kind, item.id, sent_map, today):
            due.append((item, kind))
    return due


def send_reminder(item, kind, today=None):
    """Deliver one reminder: a message in the customer's inbox plus a timeline entry.

    The two are written together or not at all, and that is the whole point of the
    atomic block. The timeline entry is also the dedupe key, and
    ReservationStatusEvent.record() deliberately swallows its own errors -- correct
    everywhere else, but here it would mean the customer receives the message while
    nothing records that they did. The next sweep would see no history, send it again,
    and keep sending it every single day forever. Raising instead lets
    send_due_reminders skip this one item, and rolls the message back with it, so the
    reminder is simply retried next time rather than repeated endlessly.
    """
    today = today or timezone.localdate()
    body = _body_for(kind, item, today)

    with transaction.atomic():
        CustomerMessage.objects.create(
            recipient=item.reservation.customer,
            category=_MESSAGE_CATEGORIES[kind],
            body=body,
        )
        recorded = ReservationStatusEvent.record(
            item.reservation, EVENT_LABELS[kind], item=item, detail=body,
            actor=ReservationStatusEvent.Actor.SYSTEM,
        )
        if recorded is None:
            raise RuntimeError(
                f"Could not record the reminder sent for item {item.id}; "
                f"rolling the message back rather than risk re-sending it daily."
            )


def send_due_reminders(today=None):
    """Send everything due today. Returns how many went out.

    Each reminder is isolated: one customer whose row is somehow broken must not stop
    every other customer from being reminded.
    """
    today = today or timezone.localdate()
    sent = 0
    for item, kind in find_due_reminders(today):
        try:
            send_reminder(item, kind, today)
            sent += 1
        except Exception:
            continue
    return sent


def run_daily_sweep_if_due(today=None):
    """The entry point staff traffic triggers. Runs the sweep at most once a day.

    The day is CLAIMED before any sending happens, with a conditional UPDATE that only
    one request can win. Two staff opening the dashboard at the same moment would
    otherwise both see "not run yet" and every customer would get two of each reminder.
    Claiming first means a crash mid-sweep costs at most one skipped day, which is far
    cheaper than double-messaging every customer in the shop.

    Returns the number of reminders sent, or None if today's pass had already been done.
    """
    today = today or timezone.localdate()
    ReminderRun.load()

    claimed = ReminderRun.objects.filter(
        Q(last_run_on__isnull=True) | Q(last_run_on__lt=today), pk=1,
    ).update(last_run_on=today, last_run_at=timezone.now())
    if not claimed:
        return None

    sent = send_due_reminders(today)
    ReminderRun.objects.filter(pk=1).update(last_sent_count=sent)
    return sent


# --------------------------------------------------------------------------------
# Staff-initiated reminders
#
# The automatic sweep is a safety net, not a replacement for judgement. Staff looking
# at the schedule often know something the dates do not -- a customer who called ahead,
# a gown needed back early for the next booking -- so they can also send a reminder by
# hand, at any time, on any active booking.
# --------------------------------------------------------------------------------

STAFF_EVENT_LABEL = 'Reminder sent by staff'

# Matches CustomerMessage.body being a TextField in practice but keeps a sane ceiling:
# this text is typed into a dialog and read on a phone, not an essay.
MANUAL_BODY_MAX_CHARS = 1000


def suggested_message(item, today=None):
    """The reminder text to pre-fill for staff, for ANY active booking.

    When the gown matches one of the automatic rules, staff get exactly the wording the
    system would have sent. When it matches none -- a return still a week away, say --
    they get a neutral, factual note instead of nothing, because the whole point of the
    manual path is that staff may have a reason the dates don't know about.
    """
    today = today or timezone.localdate()

    kind = _due_kind(item, today)
    if kind:
        return _body_for(kind, item, today)

    code = item.reservation.reference_code
    if item.stage == ReservationItem.Stage.PICKUP:
        return (
            f"Reminder about your rental of {item.gown_name}: pick-up is scheduled for "
            f"{item.rental_date:%B %d, %Y}. Reference: {code}."
        )
    return (
        f"Reminder about your rental of {item.gown_name}: it is due back on "
        f"{item.return_date:%B %d, %Y}. Reference: {code}."
    )


def send_manual_reminder(item, body, sent_by=None):
    """Send a reminder a staff member wrote or approved.

    Deliberately does NOT consult the dedupe ledger. A staff member clicking send has
    decided this customer needs to hear from the shop right now, and silently swallowing
    that because a robot already messaged them today would be the system overruling the
    person who can actually see the situation.

    Logged under its own label and as a Staff action, so the timeline never blurs
    "the system noticed" with "a person decided" -- which is exactly the distinction
    that matters if the record is ever produced in a deposit dispute.
    """
    body = (body or '').strip()
    if not body:
        raise ValueError("A reminder cannot be empty.")
    body = body[:MANUAL_BODY_MAX_CHARS]

    detail = body
    if sent_by:
        detail = f"{body} (sent by {sent_by})"

    with transaction.atomic():
        CustomerMessage.objects.create(
            recipient=item.reservation.customer,
            category=CustomerMessage.Category.RESERVATION_REMINDER,
            body=body,
        )
        recorded = ReservationStatusEvent.record(
            item.reservation, STAFF_EVENT_LABEL, item=item, detail=detail,
            actor=ReservationStatusEvent.Actor.STAFF,
        )
        if recorded is None:
            raise RuntimeError(
                "Could not record that this reminder was sent; rolling the message back "
                "so the shop is never left with a notification it has no proof of."
            )
    return body
