"""python manage.py rename_barongs [--dry-run]

Gives the 7 Barongs their final names (Beige Barong 1 ... White & Light Blue Barong 1) in place of the names the shop may hold: the first
ones they were added under (Barong 1 ... Barong 7, the photo numbers) or the lone colour's in-between name ("White & Light Blue Barong",
before it got its "1"). Every size of a design is renamed together, so the customer site keeps showing ONE product per design with its
S / M / L buttons.

Nothing else changes: gown IDs (the tags), photos, sizes, prices, statuses, bookings and the product page web addresses all stay as they are
(the same promise Edit Gown makes -- see arabela_admin.views.gown_update_view).

Safe to run again: a design that already has its final name is left alone. A design is refused, with nothing changed for it, when
  * a gown with the FINAL name already exists next to the old one (renaming would merge two different products into one), or
  * the design is found under two different old names at once (it cannot be told which gowns belong together), or
  * its colour is not the colour the new name says (the owner edited it since), or
  * the new name would not pass the same checks Edit Gown applies.
`--dry-run` shows what would happen and writes nothing. To reach the live shop on purpose: `python live.py rename_barongs`.

The same engine renames the bridesmaid dresses (`rename_bridesmaids`): what differs is only the class attributes below."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from arabela_admin.views import _validate_gown_fields
from gowns.management.commands.add_barongs import BARONGS, CATEGORY, EARLIER_NAMES, FORMER_NAMES
from gowns.models import Gown


class Command(BaseCommand):
    help = "Rename the 7 Barongs to their final names (all sizes together; --dry-run changes nothing)."

    # What this batch is. rename_bridesmaids overrides these.
    category = CATEGORY
    designs = BARONGS
    former_names = FORMER_NAMES
    earlier_names = EARLIER_NAMES
    noun = "Barong"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show what would be renamed; change nothing.")

    def old_names(self, name):
        first = self.former_names.get(name, "")
        return tuple(n for n in ((first,) if first else ()) + tuple(self.earlier_names.get(name, ())) if n)

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        self.stdout.write("DRY RUN -- nothing will be changed.\n" if dry_run else f"Renaming the {self.noun}s:")
        renamed_total = problems = 0
        for new_name, _photo, color_name in self.designs:
            if not self.old_names(new_name):
                continue                                   # a design that never had another name has nothing to rename
            result = self.rename_one(new_name, color_name, dry_run)
            if result is None:
                problems += 1
            else:
                renamed_total += result

        verb = "Would rename" if dry_run else "Renamed"
        self.stdout.write(self.style.SUCCESS(f"\n{verb} {renamed_total} gown(s)."))
        if problems:
            raise CommandError(f"{problems} {self.noun}(s) could not be renamed -- see the lines above. Nothing for those was changed.")

    def rename_one(self, new_name, color_name, dry_run):
        """How many gowns were (or would be) renamed for one design, or None when it was refused (and nothing was changed for it)."""
        olds = self.old_names(new_name)
        with transaction.atomic():                         # lock, check and rename one design's sizes together: all of them, or none
            found = {}
            for old in olds:
                rows = list(Gown.objects.select_for_update().filter(category=self.category, name__iexact=old).order_by("id"))
                if rows:
                    found[old] = rows
            already = Gown.objects.filter(category=self.category, name__iexact=new_name).exists()
            if not found:
                if already:
                    self.stdout.write(f'  "{new_name}": already has its name -- left alone.')
                else:
                    self.stdout.write(f'  "{new_name}": no gown named {" or ".join(chr(34) + n + chr(34) for n in olds)} or "{new_name}" in the shop -- nothing to rename.')
                return 0
            label = " / ".join(f'"{old}"' for old in found)
            if len(found) > 1:
                self.stderr.write(f'  {label} -> "{new_name}": gowns were found under more than one of these names, so it is not clear which '
                                  "belong together. Nothing was changed for it.")
                return None
            units = next(iter(found.values()))
            if already:
                self.stderr.write(f'  {label} -> "{new_name}": a gown called "{new_name}" already exists next to it, and renaming '
                                  "would merge two different products into one. Nothing was changed for it.")
                return None
            for unit in units:
                if unit.color_name.casefold() != color_name.casefold():
                    self.stderr.write(f'  {label} -> "{new_name}": {unit.gown_id} is {unit.color_name}, not {color_name}, so the new '
                                      "name would be wrong. Nothing was changed for it.")
                    return None
                error = _validate_gown_fields(new_name, self.category, unit.color_name, unit.color_code, unit.size, unit.design_variant,
                                              exclude_gown_id=unit.id)
                if error:
                    self.stderr.write(f'  {label} -> "{new_name}": {error} Nothing was changed for it.')
                    return None

            tags = ", ".join(f"{unit.gown_id} ({unit.size})" for unit in units)
            if dry_run:
                self.stdout.write(f'  {label} -> "{new_name}": would rename {tags}')
                return len(units)
            for unit in units:
                unit.name = new_name
                # updated_at is listed on purpose: save(update_fields=...) does not bump an auto_now field that is not named.
                unit.save(update_fields=["name", "updated_at"])
            self.stdout.write(f'  {label} -> "{new_name}": renamed {tags}')
            return len(units)
