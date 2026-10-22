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

import re
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
            f"{item.rental_date:%B %d, %Y}. Please visit Arabela to pick up your gown (1:00 PM to 5:00 PM only). "
            f"Reference: {code}."
        )
    if kind == PICKUP_TODAY:
        return (
            f"Reminder: your rental of {gown} starts today, "
            f"{item.rental_date:%B %d, %Y}. Your gown is ready for pick-up at Arabela until 5:00 PM today. "
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


def _gown_list(items):
    """The gowns' names for a sentence: 'A', 'A and B', 'A, B and C'. Two units of the same gown read as 'A (x2)'."""
    counts = {}
    for item in items:
        counts[item.gown_name] = counts.get(item.gown_name, 0) + 1
    names = [name if count == 1 else f"{name} (x{count})" for name, count in counts.items()]
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _days_overdue(item, today):
    days_late = (today - item.return_date).days
    return f"{days_late} {'day' if days_late == 1 else 'days'}"


def _group_body_for(kind, items, today):
    """One message for every gown of a booking that needs the same reminder today.

    A single gown reads exactly as it always has (_body_for). Several gowns of the same booking share the date for
    every kind but OVERDUE -- the kind is worked out from that date -- so they share one sentence. Overdue gowns may
    have been due back on different days, so each is then named with its own date."""
    if len(items) == 1:
        return _body_for(kind, items[0], today)
    gowns = _gown_list(items)
    first = items[0]
    code = first.reservation.reference_code

    if kind == PICKUP_TOMORROW:
        return (
            f"Reminder: your rental of {gowns} starts tomorrow, "
            f"{first.rental_date:%B %d, %Y}. Please visit Arabela to pick up your gowns (1:00 PM to 5:00 PM only). "
            f"Reference: {code}."
        )
    if kind == PICKUP_TODAY:
        return (
            f"Reminder: your rental of {gowns} starts today, "
            f"{first.rental_date:%B %d, %Y}. Your gowns are ready for pick-up at Arabela until 5:00 PM today. "
            f"Reference: {code}."
        )
    if kind == RETURN_TOMORROW:
        return (
            f"Reminder: {gowns} are due back tomorrow, {first.return_date:%B %d, %Y}. "
            f"Returning on time keeps your security deposit fully refundable. "
            f"Reference: {code}."
        )
    if kind == RETURN_TODAY:
        return (
            f"Reminder: {gowns} are due back today, {first.return_date:%B %d, %Y}. "
            f"Returning on time keeps your security deposit fully refundable. "
            f"Reference: {code}."
        )

    please = ("Please return them to Arabela as soon as possible -- "
              "overdue rentals may affect your security deposit refund.")
    if len({item.return_date for item in items}) == 1:
        return (
            f"{gowns} were due back on {first.return_date:%B %d, %Y} and are now "
            f"{_days_overdue(first, today)} overdue. {please} Reference: {code}."
        )
    each = [f"{item.gown_name} (due back {item.return_date:%B %d, %Y}, now {_days_overdue(item, today)} overdue)"
            for item in items]
    listed = " and ".join(each) if len(each) == 2 else ", ".join(each[:-1]) + " and " + each[-1]
    return f"These gowns are overdue: {listed}. {please} Reference: {code}."


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
        # A booking staff recorded for a customer with no website account has nobody to message or email -- the staff bell
        # lists those for a phone call instead (arabela_admin/notifications.py).
        .filter(reservation__customer__isnull=False)
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


def _sort_key(item):
    # Earliest due first, then the order the gowns were booked in (not alphabetical: "Wedding Gown 11" would come
    # before "Wedding Gown 9").
    return (item.return_date, item.rental_date, item.id)


def _open_overdue_message(reservation):
    """The customer's latest overdue notice for this booking, to bring up to date instead of adding another one.

    Every overdue notice the sweep writes ends with "Reference: <code>." (staff-written reminders use a different
    category), so the booking is recognised from the text itself and no database change is needed."""
    return (
        CustomerMessage.objects
        .filter(recipient_id=reservation.customer_id, category=CustomerMessage.Category.RETURN_OVERDUE,
                body__endswith=f"Reference: {reservation.reference_code}.")
        .order_by('-created_at', '-id')
        .first()
    )


def send_reminder_group(items, kind, today=None):
    """Deliver one reminder for one booking: ONE message naming every gown it covers, plus a timeline entry per gown.

    Several gowns of the same booking used to send one message (and one email) each. Now they share one. An overdue
    notice goes further: the booking keeps a single overdue message that is brought up to date every time ("now 7
    days overdue", "now 10 days overdue"), moved back to the top and marked unread again so the bell still rings,
    and emailed again -- instead of a new message piling up every three days. If the customer deleted it, a new one
    is written.

    The message and the timeline entries are written together or not at all, and that is the whole point of the
    atomic block. The timeline entry is also the dedupe key, and ReservationStatusEvent.record() deliberately
    swallows its own errors -- correct everywhere else, but here it would mean the customer receives the message
    while nothing records that they did. The next sweep would see no history, send it again, and keep sending it
    every single day forever. Raising instead lets send_due_reminders skip this booking, and rolls the message back
    with it, so the reminder is simply retried next time rather than repeated endlessly.
    """
    today = today or timezone.localdate()
    reservation = items[0].reservation
    if not reservation.has_account:
        return      # nobody to message (find_due_reminders already leaves these out; this is the belt to its braces)
    if kind == OVERDUE:
        # Every gown of the booking that is overdue right now, not only the ones whose own three-day wait ran out
        # today: the notice always names them all, and from here on they share one cadence.
        overdue_now = [
            item for item in ReservationItem.objects.filter(reservation=reservation)
            .select_related('reservation__customer')
            if _due_kind(item, today) == OVERDUE
        ]
        if {item.id for item in items} <= {item.id for item in overdue_now}:
            items = overdue_now
    items = sorted(items, key=_sort_key)
    body = _group_body_for(kind, items, today)

    with transaction.atomic():
        message = _open_overdue_message(reservation) if kind == OVERDUE else None
        if message is None:
            CustomerMessage.objects.create(
                recipient=reservation.customer,
                category=_MESSAGE_CATEGORIES[kind],
                body=body,
            )
        else:
            # created_at is the time the Messages page shows and sorts by, so the update reads as the newest message.
            now = timezone.now()
            CustomerMessage.objects.filter(pk=message.pk).update(body=body, is_read=False, created_at=now)
            message.body, message.is_read, message.created_at = body, False, now
            from accounts import customer_emails
            customer_emails.email_customer_message(message)
        for item in items:
            recorded = ReservationStatusEvent.record(
                reservation, EVENT_LABELS[kind], item=item, detail=body,
                actor=ReservationStatusEvent.Actor.SYSTEM,
            )
            if recorded is None:
                raise RuntimeError(
                    f"Could not record the reminder sent for item {item.id}; "
                    f"rolling the message back rather than risk re-sending it daily."
                )


def send_reminder(item, kind, today=None):
    """One gown's reminder -- the same delivery as send_reminder_group, for a booking with just this gown due."""
    send_reminder_group([item], kind, today)


def send_due_reminders(today=None):
    """Send everything due today, one message per booking and kind. Returns how many messages went out
    (an overdue notice brought up to date counts as one).

    Each booking is isolated: one customer whose row is somehow broken must not stop every other customer from
    being reminded.
    """
    today = today or timezone.localdate()
    groups = {}
    for item, kind in find_due_reminders(today):
        groups.setdefault((item.reservation_id, kind), []).append(item)
    sent = 0
    for (_reservation_id, kind), items in groups.items():
        try:
            send_reminder_group(items, kind, today)
            sent += 1
        except Exception:
            continue
    return sent


_REFERENCE_AT_END = re.compile(r"Reference: (\S+)\.\s*$")


def closed_overdue_notices(user, messages):
    """{message id: (how many gowns came back, booking code)} for the overdue notices whose booking has since been
    fully returned.

    Worked out when the Messages page is drawn (nothing stored), so a notice can never keep shouting "overdue" at a
    customer who has already brought everything back -- old notices from before this change included."""
    codes = {}
    for message in messages:
        if message.category == CustomerMessage.Category.RETURN_OVERDUE:
            match = _REFERENCE_AT_END.search(message.body or "")
            if match:
                codes[message.id] = match.group(1)
    if not codes:
        return {}
    returned = {}
    for reservation in (Reservation.objects.filter(customer=user, reference_code__in=set(codes.values()))
                        .prefetch_related('items')):
        stages = [item.stage for item in reservation.items.all()]
        back = stages.count(ReservationItem.Stage.RETURNED)
        if back and not any(stage in _OUT_STAGES for stage in stages):
            returned[reservation.reference_code] = back
    return {message_id: (returned[code], code) for message_id, code in codes.items() if code in returned}


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
    if not item.reservation.has_account:
        phone = item.reservation.phone or "the number on the booking"
        raise ValueError(
            f"This booking has no website account, so a message can't reach the customer. Please call them at {phone}."
        )
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
