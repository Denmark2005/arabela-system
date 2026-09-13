"""Seed GownSequence from the gown_id values that already exist.

Without this, the next gown added to a category+color group that already has real
gowns would start counting from 001 again -- immediately colliding with a real,
already-issued id like Belo-RD-001 instead of continuing from 002.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    Gown = apps.get_model('gowns', 'Gown')
    GownSequence = apps.get_model('gowns', 'GownSequence')

    highest_by_group = {}
    # Values, not full rows: this must stay cheap regardless of how large a shop's
    # catalog has grown by the time this runs.
    for gown_id in Gown.objects.values_list('gown_id', flat=True):
        # "Belo-RD-001" -> ('Belo', 'RD', '001'). rsplit from the right, not a plain
        # split, because category values can themselves contain a space ("Wedding
        # Gown") but never a hyphen -- taking the LAST two hyphen-separated segments
        # as color_code and sequence is what actually matches how gown_id is built
        # in gown_create_view, regardless of how many words the category is.
        parts = (gown_id or '').rsplit('-', 2)
        if len(parts) != 3:
            continue
        category, color_code, seq_str = parts
        try:
            sequence = int(seq_str)
        except ValueError:
            continue
        key = (category, color_code)
        if sequence > highest_by_group.get(key, 0):
            highest_by_group[key] = sequence

    for (category, color_code), highest in highest_by_group.items():
        # next_value is the NEXT number to hand out, one past the highest already used.
        GownSequence.objects.update_or_create(
            category=category, color_code=color_code,
            defaults={'next_value': highest + 1},
        )


def unbackfill(apps, schema_editor):
    """Reverse cleanly: this migration is the only thing that seeds these rows at the
    point it runs, so removing them restores the pre-migration state exactly."""
    GownSequence = apps.get_model('gowns', 'GownSequence')
    GownSequence.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('gowns', '0009_gownsequence'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
