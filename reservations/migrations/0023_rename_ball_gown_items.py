"""Reservations for gowns that were Ball Gowns now say Evening Gown ("Ball Gown 79" -> "Evening Gown 79").

Each booked item keeps its own copy of the gown's name. Only items whose gown is in the renamed category change,
and only the name that starts with the old category name; a booking for a gown that has since been deleted keeps
the name it had (that is history). The "last updated" stamp is left alone on purpose, so no customer is told their
booking has news. Runs after gowns/0024, which does the renaming of the gowns themselves. Reversible.
"""
from django.db import migrations

OLD_LABEL, NEW_LABEL = "Ball Gown", "Evening Gown"


def swap_prefix(text, old, new):
    """'Ball Gown 79' -> 'Evening Gown 79'; 'Ball Gown Tulle 51', 'Ball Gowns' and 'Emerald Ball Gown' are left alone."""
    if not text or not text.startswith(old):
        return text
    rest = text[len(old):]
    if rest[:1].isalpha():
        return text
    if rest.lstrip().lower().startswith("tulle"):
        return text
    return new + rest


def rename_items(apps, from_name, to_name):
    ReservationItem = apps.get_model("reservations", "ReservationItem")
    Gown = apps.get_model("gowns", "Gown")
    # Going forward gowns/0024 has already moved the gowns to Evening Gown; going back it has not been undone yet.
    # Either way the gowns whose bookings are renamed are the ones in Evening Gown.
    gown_ids = list(Gown.objects.filter(category=NEW_LABEL).values_list("id", flat=True))
    items = ReservationItem.objects.filter(gown_id__in=gown_ids, gown_name__startswith=from_name)
    for pk, name in list(items.values_list("id", "gown_name")):
        renamed = swap_prefix(name, from_name, to_name)
        if renamed != name:
            ReservationItem.objects.filter(pk=pk).update(gown_name=renamed[:150])


def forwards(apps, schema_editor):
    rename_items(apps, OLD_LABEL, NEW_LABEL)


def backwards(apps, schema_editor):
    rename_items(apps, NEW_LABEL, OLD_LABEL)


class Migration(migrations.Migration):

    dependencies = [
        ('reservations', '0022_gcash_payment_details'),
        ('gowns', '0024_rename_ball_gown_to_evening_gown'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
