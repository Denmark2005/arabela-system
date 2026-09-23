from django.db import migrations
from django.db.models import F


def backfill_original_dates(apps, schema_editor):
    """Rows created before original_rental_date/original_return_date existed have
    no other record of what was first promised -- their current rental_date/
    return_date is the best available stand-in, since as far as this migration
    can tell nothing has rescheduled them (yet)."""
    ReservationItem = apps.get_model("reservations", "ReservationItem")
    ReservationItem.objects.filter(original_rental_date__isnull=True).update(
        original_rental_date=F("rental_date")
    )
    ReservationItem.objects.filter(original_return_date__isnull=True).update(
        original_return_date=F("return_date")
    )


def noop_reverse(apps, schema_editor):
    """Nothing to undo -- the fields themselves are removed by reversing 0019."""


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0019_reservationitem_original_rental_date_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_original_dates, noop_reverse),
    ]
