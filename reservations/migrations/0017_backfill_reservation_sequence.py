"""Seed ReservationSequence from the reference codes that already exist.

Without this, the very next reservation created after this migration would start
counting from 0001 again for the current year -- immediately colliding with real,
already-issued codes like RSV-2026-0019 instead of continuing from 0020. This has to
run once, here, before the new counter is ever consulted.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    Reservation = apps.get_model('reservations', 'Reservation')
    ReservationSequence = apps.get_model('reservations', 'ReservationSequence')

    highest_by_year = {}
    # Values, not full rows: this can run against however many reservations a live
    # shop has accumulated, and only three fields per row are needed.
    for reference_code in Reservation.objects.values_list('reference_code', flat=True):
        # RSV-2026-0019 -> ('RSV', '2026', '0019'). Anything that doesn't match this
        # shape (blank, hand-edited, from before reference codes existed) is skipped
        # rather than guessed at -- it must never be allowed to lower a year's count.
        parts = (reference_code or '').split('-')
        if len(parts) != 3 or parts[0] != 'RSV':
            continue
        try:
            year = int(parts[1])
            number = int(parts[2])
        except ValueError:
            continue
        if number > highest_by_year.get(year, 0):
            highest_by_year[year] = number

    for year, highest in highest_by_year.items():
        # next_value is the NEXT number to hand out, one past the highest already used.
        ReservationSequence.objects.update_or_create(
            year=year, defaults={'next_value': highest + 1},
        )


def unbackfill(apps, schema_editor):
    """Reverse cleanly: this migration is the only thing that seeds these rows at the
    point it runs, so removing them restores the pre-migration state exactly."""
    ReservationSequence = apps.get_model('reservations', 'ReservationSequence')
    ReservationSequence.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('reservations', '0016_reservationsequence'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
