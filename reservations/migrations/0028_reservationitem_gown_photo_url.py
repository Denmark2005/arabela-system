from django.db import migrations, models


def keep_the_pictures(apps, schema_editor):
    """Give every existing booking line the picture of the gown it was booked for.

    1. A line whose gown is still in the catalog: that gown's picture now.
    2. A line whose gown was deleted (its link is empty): the picture from the removal log (GownRemoval, which copies the gown's name and photo
       before a removal), but ONLY when it is certain -- exactly one removal has that gown's name, and it happened after the booking was made.
       Anything less certain stays empty: the page then says the photo is not available instead of showing another gown (a catalog reset gave
       new gowns the old names, so matching by name alone would show the wrong picture)."""
    Item = apps.get_model("reservations", "ReservationItem")
    Removal = apps.get_model("gowns", "GownRemoval")
    for item in Item.objects.filter(gown__isnull=False, gown_photo_url="").select_related("gown").iterator():
        if item.gown.photo_url:
            Item.objects.filter(pk=item.pk).update(gown_photo_url=item.gown.photo_url[:500])
    for item in Item.objects.filter(gown__isnull=True, gown_photo_url="").select_related("reservation").iterator():
        removals = list(Removal.objects.filter(name__iexact=item.gown_name)[:2])
        if len(removals) != 1:
            continue
        removal = removals[0]
        if not removal.photo_url:
            continue
        if removal.removed_at is not None and removal.removed_at < item.reservation.created_at:
            continue                                # removed before this booking was made: it cannot be the gown that was booked
        Item.objects.filter(pk=item.pk).update(gown_photo_url=removal.photo_url[:500])


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0027_cancel_request"),
        ("gowns", "0027_deposit_setting_and_gcash_number_history"),
    ]

    operations = [
        migrations.AddField(
            model_name="reservationitem",
            name="gown_photo_url",
            field=models.URLField(blank=True, db_default="", default="", max_length=500),
        ),
        # Not reversible on purpose: going back drops the column, so there is nothing to undo.
        migrations.RunPython(keep_the_pictures, migrations.RunPython.noop),
    ]
