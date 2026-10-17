"""python manage.py reset_kids_gowns [--dry-run]

Cleans up the Kids Gown category after `add_kids_gowns` was run on the live shop more than once. The first runs used placeholder sizes and the later
run added the owner's real sizes next to them (it only adds a size that is missing), leaving 62 gowns for 33 designs. The owner wants exactly 33 gowns,
one per design in the owner's size, with gown IDs 001 to 033.

What it does, in one atomic step:
  1. keeps, for every design, the gown that has the OWNER'S size (the list in add_kids_gowns.py);
  2. removes every other gown of that design (through the same Removal Log the admin uses, reason Other, with a note saying why);
  3. renumbers the kept gowns 001-033 in the owner's list order (Kids Gown 01 = 001 ... Kids Gown 33 = 033) and sets the number counter so the
     next kids gown added gets 034.
The kept gowns are the same records, so their photo, price, colour, slug (web address) and name do not change.

It refuses -- changing nothing -- when
  * a gown is not one of the owner's 33 designs, or a design has no gown in the owner's size (run add_kids_gowns first), or has two,
  * a gown it would remove has a booking or a blocked date (nothing is deleted that a customer or staff member is attached to).
Already reset (33 gowns, IDs 001-033 in the owner's order, every size the owner's): it just says so. `--dry-run` prints the plan and writes nothing.
To reach the live shop on purpose: `python live.py reset_kids_gowns`. Take a backup first."""
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import Error as DatabaseFailure
from django.db import transaction
from django.utils import timezone

from arabela_admin.views import _REMOVAL_NOTE_MAX
from gowns.management.commands.add_kids_gowns import KIDS_GOWNS, SIZES_BY_NAME
from gowns.models import Gown, GownRemoval, GownSequence, GownUnavailability
from reservations.models import ReservationItem

CATEGORY = Gown.Category.KIDS_GOWN
TOTAL = len(KIDS_GOWNS)                                      # 33
NOTE = "Kids Gown cleanup: extra gown from an earlier upload whose size is not the owner's size for this design."[:_REMOVAL_NOTE_MAX]


def final_gown_id(code, number):
    return f"{CATEGORY}-{code}-{number:03d}"


class Command(BaseCommand):
    help = "Cut the Kids Gowns down to the owner's 33 (one per design, the owner's size), IDs 001-033. --dry-run changes nothing."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show the plan; change nothing.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        self.stdout.write(("DRY RUN -- nothing will be changed.\n" if dry_run else "") + f"Category: {CATEGORY}")
        try:
            with transaction.atomic():
                gowns = list(Gown.objects.select_for_update().filter(category=CATEGORY).order_by("id"))
                keep, remove = self.plan(gowns)
                if not remove and self.numbered(keep):
                    self.stdout.write(self.style.SUCCESS(f"The {CATEGORY} category is already reset ({TOTAL} gowns, 001-{TOTAL:03d}). Nothing to do."))
                    return
                self.show(keep, remove)
                if dry_run:
                    self.stdout.write(self.style.SUCCESS(f"\nWould remove {len(remove)} gown(s) and renumber {TOTAL}. Nothing was changed."))
                    return
                self.apply(keep, remove)
        except DatabaseFailure as exc:                       # the connection dropped or the database refused part-way: all-or-nothing, so none of it happened
            raise CommandError(
                "The connection to the database failed part-way, so the whole change was rolled back and NOTHING was changed. "
                "Check your internet and run the same command again (if you are ever unsure, run it with --dry-run: it says whether the cleanup is already done). "
                f"The error was: {exc.__class__.__name__}: {exc}"
            ) from exc
        self.stdout.write(self.style.SUCCESS(f"\nDone: {len(remove)} extra gown(s) removed; {TOTAL} Kids Gowns remain, IDs 001-{TOTAL:03d} in the owner's order; the next gown added gets {TOTAL + 1:03d}."))

    # ---- decide ----
    def plan(self, gowns):
        """(keep, remove): keep = [gown per design in the owner's order], remove = [the others]. CommandError (every problem listed) if unsafe."""
        by_name, problems = {}, []
        for gown in gowns:
            if gown.name not in SIZES_BY_NAME:
                problems.append(f'{gown.gown_id}: "{gown.name}" is not one of the owner\'s 33 Kids Gowns.')
            else:
                by_name.setdefault(gown.name, []).append(gown)
        keep, remove = [], []
        for name, *_rest in KIDS_GOWNS:
            owner_size = str(SIZES_BY_NAME[name][0])
            units = by_name.get(name, [])
            right = [g for g in units if g.size == owner_size]
            if not right:
                problems.append(f'"{name}" has no gown in the owner\'s size ({owner_size}) -- run add_kids_gowns first.')
                continue
            if len(right) > 1:
                problems.append(f'"{name}" has {len(right)} gowns in size {owner_size} ({", ".join(g.gown_id for g in right)}); keeping one needs a decision.')
                continue
            keep.append(right[0])
            remove.extend(g for g in units if g is not right[0])
        doomed = [gown.pk for gown in remove]
        held = Counter(ReservationItem.objects.filter(gown_id__in=doomed).values_list("gown_id", flat=True))        # two calls for all of them,
        blocked = Counter(GownUnavailability.objects.filter(gown_id__in=doomed).values_list("gown_id", flat=True))  # not two per gown
        for gown in remove:
            if held[gown.pk] or blocked[gown.pk]:
                problems.append(f'{gown.gown_id} "{gown.name}" ({gown.size}) would be removed but has {held[gown.pk]} booking line(s) and {blocked[gown.pk]} blocked date(s).')
        if problems:
            raise CommandError("The Kids Gown category cannot be reset safely, so NOTHING was changed:\n  - " + "\n  - ".join(problems))
        return keep, remove

    def numbered(self, keep):
        return all(g.tracking_number == i and g.gown_id == final_gown_id(g.color_code, i) for i, g in enumerate(keep, start=1))

    def show(self, keep, remove):
        self.stdout.write(f"\nRemoving {len(remove)} extra gown(s) (kept per design: the owner's size):")
        for gown in sorted(remove, key=lambda g: g.tracking_number or 0):
            self.stdout.write(f"  - {gown.gown_id:<22} {gown.name:<30} {gown.size}")
        self.stdout.write(f"\nRenumbering the {len(keep)} kept gowns:\n  {'now':<24}{'becomes':<24}{'name':<30}size")
        for number, gown in enumerate(keep, start=1):
            self.stdout.write(f"  {gown.gown_id:<24}{final_gown_id(gown.color_code, number):<24}{gown.name:<30}{gown.size}")

    # ---- do ----
    def apply(self, keep, remove):
        # Everything is done in a handful of database calls (the first version made about 250 one-row calls over the internet link, and a connection
        # that drops half-way through such a long run rolls it all back).
        now = timezone.now()
        # The same Removal Log entry the admin writes for one removed gown (arabela_admin.views._remove_gown), for all of them at once.
        GownRemoval.objects.bulk_create([
            GownRemoval(
                gown_id=gown.gown_id, tracking_number=gown.tracking_number, name=gown.name, category=gown.category, color_name=gown.color_name,
                size=gown.size, photo_url=gown.photo_url, reason=GownRemoval.Reason.OTHER, note=NOTE, removed_by=None, removed_by_name="", removed_at=now,
            ) for gown in remove
        ])
        Gown.objects.filter(pk__in=[gown.pk for gown in remove]).delete()
        # Two steps: a kept gown's final ID may be the one another kept gown still holds, and IDs are unique -- temporary IDs first.
        # (bulk_update skips the auto_now stamp, so updated_at is set by hand.)
        for gown in keep:
            gown.gown_id, gown.updated_at = f"RESET-{gown.pk}", now
        Gown.objects.bulk_update(keep, ["gown_id", "updated_at"])
        for number, gown in enumerate(keep, start=1):
            gown.gown_id = final_gown_id(gown.color_code, number)
        Gown.objects.bulk_update(keep, ["gown_id", "updated_at"])
        row, _created = GownSequence.objects.select_for_update().get_or_create(category=CATEGORY, color_code=GownSequence.CATEGORY_WIDE)
        row.next_value = TOTAL + 1
        row.save(update_fields=["next_value"])
        self.verify(keep)

    def verify(self, keep):
        now = list(Gown.objects.filter(category=CATEGORY))
        if len(now) != TOTAL:
            raise CommandError(f"Expected {TOTAL} gowns, found {len(now)}; nothing was changed.")
        if sorted(g.tracking_number or 0 for g in now) != list(range(1, TOTAL + 1)):
            raise CommandError("The gown IDs are not 001-033 in a row; nothing was changed.")
        fresh_by_pk = {g.pk: g for g in now}
        for number, gown in enumerate(keep, start=1):
            fresh = fresh_by_pk.get(gown.pk)
            if fresh is None or fresh.gown_id != final_gown_id(fresh.color_code, number) or fresh.size != str(SIZES_BY_NAME[fresh.name][0]):
                raise CommandError(f"{gown.name} did not end up right; nothing was changed.")
        if GownSequence.objects.get(category=CATEGORY, color_code=GownSequence.CATEGORY_WIDE).next_value != TOTAL + 1:
            raise CommandError("The number counter was not reset; nothing was changed.")
