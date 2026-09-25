from datetime import timedelta

from django.db import migrations


def backfill_cooldown_blocks(apps, schema_editor):
    """Give every already-in-flight booking (made before this rule existed) the
    same 3-day trailing cooldown block a fresh reservation now gets automatically
    -- so a booking submitted yesterday isn't treated differently from one
    submitted tomorrow. Skips anything that already has one (re-running this is
    always safe)."""
    ReservationItem = apps.get_model('reservations', 'ReservationItem')
    GownUnavailability = apps.get_model('gowns', 'GownUnavailability')

    already_linked = set(
        GownUnavailability.objects.filter(auto_for_item__isnull=False)
        .values_list('auto_for_item_id', flat=True)
    )
    items = (
        ReservationItem.objects.filter(gown__isnull=False)
        .exclude(stage='Returned')
        .exclude(reservation__status__in=['Rejected', 'Cancelled'])
        .exclude(id__in=already_linked)
    )
    for item in items:
        GownUnavailability.objects.create(
            gown_id=item.gown_id,
            start_date=item.return_date + timedelta(days=1),
            end_date=item.return_date + timedelta(days=3),
            reason='Cooldown',
            note="Auto-added post-rental cooldown, backfilled for a booking made before this rule shipped.",
            auto_for_item_id=item.id,
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('gowns', '0014_gownunavailability_auto_for_item_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_cooldown_blocks, noop_reverse),
    ]
