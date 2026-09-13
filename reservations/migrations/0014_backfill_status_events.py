"""Reconstruct as much timeline history as the existing columns allow.

Without this, every reservation that existed before the event log shipped would show
a blank timeline -- the feature would make old orders look broken rather than better.
What can be recovered honestly:

  created_at          -> "Reservation submitted"      (real timestamp)
  reviewed_at         -> "approved"/"rejected"        (real timestamp)
  deposit_returned_at -> "Security deposit returned"  (real timestamp)
  picked_up_on        -> "Picked up"                  (DATE only -> time_known=False)
  returned_on         -> "Marked as returned"         (DATE only -> time_known=False)

Cancellations are deliberately NOT reconstructed: nothing ever recorded when one
happened (only the shared updated_at, which any later save overwrites), so inventing
a timestamp for it would be a guess presented as a fact. Those reservations simply
start their timeline at submission, and their current Cancelled badge still shows.
"""

from datetime import datetime, time, timedelta, timezone as dt_timezone

from django.db import migrations

# The shop's real-world clock. Fixed +08:00 with no DST ever, so a plain offset is
# exact -- no tz database lookup needed inside a migration.
MANILA = dt_timezone(timedelta(hours=8))


def _sort_anchor(d, after):
    """Pick the stored timestamp for a DATE-ONLY event (its real time of day was never
    recorded, so this value is only ever used for ordering -- it is never displayed).

    Two things have to hold. The DATE must stay correct when rendered in Manila time,
    and the event must not sort ahead of something that provably happened before it:
    without this, a gown picked up on the 15th lands at local midday and jumps above a
    reservation submitted at 7pm that same day, so the customer reads "picked up"
    before "submitted".

    So: anchor at local midday, and if an earlier event already sits at or past that
    point, step just after it instead -- but only while that keeps the event on the
    same Manila calendar day. When even that would push it onto the next day, the two
    records genuinely contradict each other, and showing the true date beats inventing
    a tidy order.
    """
    base = datetime.combine(d, time(12, 0), tzinfo=MANILA).astimezone(dt_timezone.utc)
    if after is None or base > after:
        return base
    nudged = after + timedelta(minutes=1)
    return nudged if nudged.astimezone(MANILA).date() == d else base


def backfill(apps, schema_editor):
    Reservation = apps.get_model('reservations', 'Reservation')
    ReservationStatusEvent = apps.get_model('reservations', 'ReservationStatusEvent')

    events = []
    for reservation in Reservation.objects.prefetch_related('items').all():
        # Built in causal order (submitted -> reviewed -> per gown -> deposit), with
        # `latest` carrying the newest timestamp placed so far. Real recorded times are
        # never adjusted -- only the synthesized date-only anchors bend around them.
        latest = reservation.created_at

        def add(label, when, *, item=None, detail='', actor='Staff', time_known=True):
            nonlocal latest
            events.append(ReservationStatusEvent(
                reservation=reservation, item=item, label=label, detail=detail,
                actor=actor, occurred_at=when, time_known=time_known,
            ))
            if when > latest:
                latest = when

        add('Reservation submitted', reservation.created_at, actor='Customer',
            detail='Waiting for staff to review your payment.')

        if reservation.reviewed_at:
            if reservation.status == 'Rejected':
                add('Reservation rejected', reservation.reviewed_at,
                    detail=(reservation.notes or '')[:300])
            else:
                add('Reservation approved', reservation.reviewed_at,
                    detail='Your booking is confirmed.')

        for item in reservation.items.all():
            if item.picked_up_on:
                add(f'{item.gown_name} picked up', _sort_anchor(item.picked_up_on, latest),
                    item=item, time_known=False)
            if item.returned_on:
                add(f'{item.gown_name} returned', _sort_anchor(item.returned_on, latest),
                    item=item, time_known=False,
                    detail=(f'Checked in as: {item.return_condition}' if item.return_condition else ''))

        if reservation.deposit_returned_at:
            add('Security deposit returned', reservation.deposit_returned_at)

    ReservationStatusEvent.objects.bulk_create(events, batch_size=500)


def unbackfill(apps, schema_editor):
    """Reverse cleanly: this migration is the only thing that has written events at
    the point it runs, so removing them all restores the pre-migration state exactly."""
    ReservationStatusEvent = apps.get_model('reservations', 'ReservationStatusEvent')
    ReservationStatusEvent.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('reservations', '0013_reservationstatusevent'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
