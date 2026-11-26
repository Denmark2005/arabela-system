"""python manage.py add_gown_descriptions [--dry-run]

Fills the Fabric and Description customers read on each gown's page, from gowns/gown_descriptions.json (one entry per design: category + name,
written from the design's photo; the fabric names are estimates the shop should check, and staff can change any of them in Gown Catalog).

  * Only EMPTY boxes are filled -- nothing staff typed is ever overwritten. Every gown of a design gets the same text: where some gown of the
    design already has text, its other sizes get that text instead of the file's.
  * A design the file doesn't know (added later) is left as it is, and listed.
  * Everything is written in ONE transaction. Running it again changes nothing. `--dry-run` shows what would happen and changes nothing.
    `python live.py add_gown_descriptions --dry-run` reads the live shop; without --dry-run it writes to the live shop on purpose."""
import json
from collections import OrderedDict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from gowns import design_details
from gowns.models import Gown

DATA_FILE = Path(__file__).resolve().parents[2] / "gown_descriptions.json"


def key_of(category, name):
    return (category, str(name).strip().casefold())


def load(path):
    """{(category, name): (fabric, description, name as written)} from the file; CommandError if anything in it could not be saved."""
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CommandError(f"Could not read {path}: {exc}")
    texts = {}
    for number, row in enumerate(rows, start=1):
        fabric = design_details.clean_fabric(row.get("fabric"))
        description = design_details.clean_description(row.get("description"))
        where = f"entry {number} ({row.get('category')}, {row.get('name')})"
        if row.get("category") not in Gown.Category.values or not str(row.get("name") or "").strip():
            raise CommandError(f"{where}: the category or name is missing or unknown. Nothing was changed.")
        if not fabric or not description or design_details.problem(fabric, description):
            raise CommandError(f"{where}: {design_details.problem(fabric, description) or 'the fabric or description is empty.'} Nothing was changed.")
        key = key_of(row["category"], row["name"])
        if key in texts:
            raise CommandError(f"{where}: this design is in the file twice. Nothing was changed.")
        texts[key] = (fabric, description, str(row["name"]).strip())
    return texts


def plan(units, texts):
    """-> (gowns to save, designs filled from the file, designs that kept staff text, designs left without any, file designs with no gown)."""
    designs = OrderedDict()
    for unit in units:
        designs.setdefault(key_of(unit.category, unit.name), []).append(unit)
    now = timezone.now()
    changed, filled, kept, missing = [], [], [], []
    for key, group in designs.items():
        typed_fabric, typed_description = design_details.text_of(group)
        file_fabric, file_description, _ = texts.get(key, ("", "", ""))
        fabric, description = typed_fabric or file_fabric, typed_description or file_description
        if typed_fabric or typed_description:
            kept.append(group[0])
        if not fabric and not description:
            missing.append(group[0])
            continue
        touched = False
        for unit in group:
            new_fabric, new_description = unit.fabric or fabric, unit.description or description
            if (new_fabric, new_description) != (unit.fabric, unit.description):
                unit.fabric, unit.description, unit.updated_at = new_fabric, new_description, now
                changed.append(unit)
                touched = True
        if touched and not (typed_fabric and typed_description):
            filled.append(group[0])
    unknown = [(key[0], entry[2]) for key, entry in texts.items() if key not in designs]
    return changed, filled, kept, missing, unknown


class Command(BaseCommand):
    help = "Fill the empty Fabric and Description of every gown design from gowns/gown_descriptions.json (re-runnable; --dry-run changes nothing)."

    data_file = DATA_FILE

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show what would happen; change nothing.")

    def handle(self, *args, **options):
        texts = load(self.data_file)
        dry_run = options["dry_run"]
        self.stdout.write(("DRY RUN -- nothing will be changed.\n" if dry_run else "") + f"The file has {len(texts)} designs.")
        with transaction.atomic():
            units = Gown.objects.all().order_by("id")
            if not dry_run:
                units = units.select_for_update()          # a text staff type while this runs waits, and is never overwritten
            changed, filled, kept, missing, unknown = plan(list(units), texts)
            if not dry_run and changed:
                Gown.objects.bulk_update(changed, ["fabric", "description", "updated_at"], batch_size=100)
        verb = "Would fill" if dry_run else "Filled"
        self.stdout.write(f"{verb} {len(changed)} gown(s) in {len({key_of(u.category, u.name) for u in changed})} design(s).")
        if kept:
            self.stdout.write(f"{len(kept)} design(s) already had text typed in Gown Catalog: it was kept.")
        if missing:
            self.stdout.write(f"{len(missing)} design(s) have no text yet (not in the file) -- type it in Gown Catalog:")
            for unit in missing[:40]:
                self.stdout.write(f"    {unit.category}: {unit.name}")
        if unknown:
            self.stdout.write(f"{len(unknown)} design(s) in the file are not in the catalog (renamed or removed since): skipped.")
            for category, name in unknown[:40]:
                self.stdout.write(f"    {category}: {name}")
        for unit in filled[:3]:
            self.stdout.write(f"  e.g. {unit.name} -- {unit.fabric}. {unit.description}")
        self.stdout.write(self.style.SUCCESS("Nothing was changed (dry run)." if dry_run else "Done."))
