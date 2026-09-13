"""Seed GownSlugSequence from the slugs that already exist, plus the placeholder
catalog's 8 fixed demo slugs.

Without the first part, a base text some existing gown's slug is ALREADY using (bare,
or with a numeric suffix) could be handed out again to a brand new gown, colliding
with a real, already-issued slug.

Without the second part, a brand new gown whose name happens to slugify to one of the
placeholder catalog's fixed demo slugs (valencia-lace, archive-satin, ...) could be
saved with that exact slug -- which would silently shadow one of the fake catalog's
own product pages, since product_detail tries a real Gown by that slug first.
"""

import re

from django.db import migrations

_TRAILING_SUFFIX = re.compile(r'^(.*)-(\d+)$')

# Mirrors gowns.context_processors._SLUG_ORDER. Copied as a literal here, not
# imported, so this migration's behaviour is pinned to what it actually needs to
# seed at the moment it runs, and never silently changes meaning if that list is
# edited later -- the same reason every other data migration in this project embeds
# its own literal values rather than importing the live, still-changing app code.
_PLACEHOLDER_SLUGS = (
    "valencia-lace",
    "archive-satin",
    "florence-organza",
    "modernist-crepe",
    "opulence-pearl",
    "heritage-lace",
    "city-reception",
    "lumiere-silk",
)


def backfill(apps, schema_editor):
    Gown = apps.get_model('gowns', 'Gown')
    GownSlugSequence = apps.get_model('gowns', 'GownSlugSequence')

    # base -> highest suffix level already used for it. A bare slug (no trailing
    # -N) counts as level 1 -- "the plain form is taken" -- so a base seen ONLY in
    # its bare form still ends up seeded to next_suffix=2, correctly refusing to
    # ever hand that bare form out a second time.
    highest_by_base = {}
    for slug in Gown.objects.values_list('slug', flat=True):
        if not slug:
            continue
        match = _TRAILING_SUFFIX.match(slug)
        if match:
            base, level = match.group(1), int(match.group(2))
        else:
            base, level = slug, 1
        if level > highest_by_base.get(base, 0):
            highest_by_base[base] = level

    for base in _PLACEHOLDER_SLUGS:
        if base not in highest_by_base:
            highest_by_base[base] = 1

    for base, highest in highest_by_base.items():
        GownSlugSequence.objects.update_or_create(
            base=base, defaults={'next_suffix': highest + 1},
        )


def unbackfill(apps, schema_editor):
    """Reverse cleanly: this migration is the only thing that seeds these rows at the
    point it runs, so removing them restores the pre-migration state exactly."""
    GownSlugSequence = apps.get_model('gowns', 'GownSlugSequence')
    GownSlugSequence.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('gowns', '0011_gownslugsequence'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
