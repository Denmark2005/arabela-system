from datetime import timedelta

from django.db import migrations


def backfill(apps, schema_editor):
    """Give every existing booking a default event date (2 days after the pick-up start,
    matching the product page's 2-before/event/2-after window) and reset every still-active
    booking to the 'Pick-up' status -- the correct default now that the Rental Schedule
    shows one admin-chosen status at a time. Returned bookings are left untouched."""
    ReservationItem = apps.get_model('reservations', 'ReservationItem')
    for item in ReservationItem.objects.all():
        updates = {}
        if item.event_date is None:
            updates['event_date'] = item.rental_date + timedelta(days=2)
        if item.stage != 'Returned':
            updates['stage'] = 'Pick-up'
        if updates:
            for field, value in updates.items():
                setattr(item, field, value)
            item.save(update_fields=list(updates.keys()))


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('reservations', '0006_remove_reservationitem_overdue_date_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill, noop_reverse),
    ]
