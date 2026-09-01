from django.db import migrations


def backfill(apps, schema_editor):
    """Default every booking's overdue date to its return date, so the Overdue status has
    a sensible day to mark right away; staff can adjust it in the booking panel."""
    ReservationItem = apps.get_model('reservations', 'ReservationItem')
    for item in ReservationItem.objects.filter(overdue_date__isnull=True):
        item.overdue_date = item.return_date
        item.save(update_fields=['overdue_date'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('reservations', '0008_reservationitem_overdue_date'),
    ]

    operations = [
        migrations.RunPython(backfill, noop_reverse),
    ]
