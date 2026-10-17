"""python manage.py add_barongs [--photos FOLDER] [--dry-run]

Adds the 7 Barong gowns, each in Small, Medium and Large (21 gowns), exactly the way Add Gown in the admin would: the same checks (colour
code, price, photo), the same photo storage (Cloudinary on the live site), the same category-wide tag numbers, and the same one record per
size -- the customer site then shows each Barong as ONE product with S / M / L buttons.

The names carry the colour and a number (Beige Barong 1 ... White & Light Blue Barong 1). They have to differ from each other: gowns with
the same name are ONE product on the customer site, so three plain "Beige" gowns would merge into a single card with a single photo. A
colour with only one gown still gets its "1", so adding a second one later never means renaming the first.

Safe to run again: a Barong that already has a size is left alone, and only a missing size is added. `--dry-run` shows what would happen
and changes nothing (no database write, no photo upload). To reach the live shop on purpose: `python live.py add_barongs`.

The photos are read from `Downloads\\AREBELA GOWN\\Barong` (Barong 01.jpg ... Barong 07.jpg) unless `--photos` names another folder.

This class is also the engine of `add_bridesmaids` (gowns/management/commands/add_bridesmaids.py): everything that differs between the two
batches -- category, sizes, price, designs, folder -- is a class attribute below, so both go through exactly the same checks."""
import re
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import DataError, IntegrityError, transaction

from arabela_admin.views import (
    _check_color_code_consistency, _parse_rental_price, _save_gown_photo, _validate_gown_fields, _validate_gown_photo,
)
from gowns.models import GOWN_COLOR_PRESETS, Gown

CATEGORY = Gown.Category.BARONG
SIZES = (Gown.Size.SMALL, Gown.Size.MEDIUM, Gown.Size.LARGE)
PRICE = "1000"
# (gown name, photo file, colour)
BARONGS = (
    ("Beige Barong 1", "Barong 01.jpg", "Beige"),
    ("Beige Barong 2", "Barong 02.jpg", "Beige"),
    ("Beige Barong 3", "Barong 03.jpg", "Beige"),
    ("White Barong 1", "Barong 04.jpg", "White"),
    ("White Barong 2", "Barong 05.jpg", "White"),
    ("White Barong 3", "Barong 06.jpg", "White"),
    ("White & Light Blue Barong 1", "Barong 07.jpg", "White & Light Blue"),
)
# The names the same designs were first added under (the photo numbers). The live shop got them that way before they were renamed, so
# `rename_barongs` turns these into the names above -- and until it has, this command refuses to add a design whose old name is still
# there, because it would not recognise it and would add a second copy of all three sizes.
FORMER_NAMES = {
    "Beige Barong 1": "Barong 1",
    "Beige Barong 2": "Barong 2",
    "Beige Barong 3": "Barong 3",
    "White Barong 1": "Barong 4",
    "White Barong 2": "Barong 5",
    "White Barong 3": "Barong 6",
    "White & Light Blue Barong 1": "Barong 7",
}
# Names a design carried for a while before its final one (the lone colour first had no number). A shop may hold either, so the rename
# command and the guard below recognise these too.
EARLIER_NAMES = {
    "White & Light Blue Barong 1": ("White & Light Blue Barong",),
}
# Codes tried, in order, for a colour that is not on the admin's preset list and not used by any gown yet.
# "WL" first: the customer list is ordered by colour code, and WL sorts after White (WH), so Barong 7 lands last, as in the owner's table.
CUSTOM_CODE_CHOICES = ("WL", "WB", "LB", "BW")


def default_photo_folder(folder_name="Barong") -> Path:
    return Path.home() / "Downloads" / "AREBELA GOWN" / folder_name


class Command(BaseCommand):
    help = "Add the 7 Barong gowns in Small, Medium and Large (re-runnable; --dry-run changes nothing)."

    # What this batch is. add_bridesmaids overrides these; nothing else in the class is specific to Barongs.
    category = CATEGORY
    sizes = SIZES
    price = PRICE
    designs = BARONGS
    design_sizes = {}                                               # design name -> its own sizes, when they differ from `sizes`
    design_prices = {}                                              # design name -> its own price, when it differs from `price`
    former_names = FORMER_NAMES
    earlier_names = EARLIER_NAMES
    code_hints = {"White & Light Blue": CUSTOM_CODE_CHOICES}        # colour name -> codes to try first
    folder_name = "Barong"
    noun = "Barong"
    rename_command = "rename_barongs"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._codes = {}                                            # colour (lower case) -> the code this run uses for it

    def add_arguments(self, parser):
        first, last = self.designs[0][1], self.designs[-1][1]
        parser.add_argument("--photos", default=str(default_photo_folder(self.folder_name)), help=f"Folder with {first} ... {last}")
        parser.add_argument("--dry-run", action="store_true", help="Show what would be added; change nothing.")

    def old_names(self, name):
        """Every earlier name this design may be sitting under in the shop (first name, then the in-between ones)."""
        first = self.former_names.get(name, "")
        return tuple(n for n in ((first,) if first else ()) + tuple(self.earlier_names.get(name, ())) if n)

    def sizes_for(self, name):
        return tuple(self.design_sizes.get(name, self.sizes))

    def price_for(self, name):
        return self.design_prices.get(name, self.price)

    def parsed_price(self, name):
        return _parse_rental_price(self.price_for(name))[0]

    def sizes_text(self, sizes=None):
        names = [str(size) for size in (self.sizes if sizes is None else sizes)]
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]

    def candidate_codes(self, color_name):
        """2-letter codes to try for a colour that has no preset: the ones this batch hints at, then the initials ("Dusty Blue" -> DB),
        then the first two letters, then the first letter with each later letter of the name. Never a letter pair that is not 2 letters."""
        words = [w for w in (re.sub(r"[^A-Za-z]", "", part).upper() for part in color_name.split()) if w]
        out = list(self.code_hints.get(color_name, ()))
        if len(words) >= 2:
            out.append(words[0][0] + words[1][0])
        if words and len(words[0]) >= 2:
            out.append(words[0][:2])
        if words:
            letters = "".join(words)
            out.extend(letters[0] + ch for ch in letters[1:])
        seen, unique = set(), []
        for code in out:
            if len(code) == 2 and code not in seen:
                seen.add(code)
                unique.append(code)
        return unique

    def color_code_for(self, color_name):
        """The 2-letter code this colour must carry: the admin's preset code, else the code a gown of that colour already has, else the
        first free one -- never a code that already means a different colour, in the shop OR earlier in this same run (the same rule Add
        Gown enforces). The answer is remembered, so a dry run and the real run agree and one colour never gets two codes."""
        key = color_name.casefold()
        if key in self._codes:
            return self._codes[key]
        code = self._pick_code(color_name)
        self._codes[key] = code
        return code

    def _pick_code(self, color_name):
        for name, code in GOWN_COLOR_PRESETS:
            if name.casefold() == color_name.casefold():
                return code
        existing = Gown.objects.filter(color_name__iexact=color_name).values_list("color_code", flat=True).first()
        if existing:
            return existing
        used_here = {code for key, code in self._codes.items() if key != color_name.casefold()}
        for code in self.candidate_codes(color_name):
            if code not in used_here and not _check_color_code_consistency(code, color_name):
                return code
        raise CommandError(f'No free colour code for "{color_name}".')

    def handle(self, *args, **options):
        folder = Path(options["photos"])
        dry_run = options["dry_run"]
        if not folder.is_dir():
            raise CommandError(f"Photo folder not found: {folder}")
        for price_text in {self.price, *self.design_prices.values()}:       # every price the batch uses must be a valid one, before anything happens
            _price, price_error = _parse_rental_price(price_text)
            if price_error:
                raise CommandError(f"{price_text}: {price_error}")

        self.stdout.write(("DRY RUN -- nothing will be changed.\n" if dry_run else "") + f"Photos: {folder}")
        created_total = skipped_total = problems = 0
        for name, photo_name, color_name in self.designs:
            result = self.add_one(name, photo_name, color_name, folder, self.parsed_price(name), dry_run)
            if result is None:
                problems += 1
                continue
            created, skipped = result
            created_total += created
            skipped_total += skipped

        verb = "Would add" if dry_run else "Added"
        self.stdout.write(self.style.SUCCESS(f"\n{verb} {created_total} gown(s); {skipped_total} size(s) were already there."))
        if problems:
            raise CommandError(f"{problems} {self.noun}(s) could not be added -- see the lines above. Nothing for those was changed.")

    def add_one(self, name, photo_name, color_name, folder, price, dry_run):
        """(sizes added, sizes already there) for one design, or None when it could not be added (and nothing was changed for it)."""
        code = self.color_code_for(color_name)
        sizes = self.sizes_for(name)
        have = {g.size: g for g in Gown.objects.filter(category=self.category, name__iexact=name)}
        missing = [size for size in sizes if size not in have]
        label = f"{name}  [{color_name} / {code}, {self.price_for(name)}]"
        for former in self.old_names(name):
            if Gown.objects.filter(category=self.category, name__iexact=former).exists():
                self.stderr.write(f'  {label}: the shop still has this design under its earlier name "{former}". Run {self.rename_command} first '
                                  "(adding it now would make a second copy). Nothing was changed for it.")
                return None
        if not missing:
            self.stdout.write(f"  {label}: already has {self.sizes_text(sizes)} -- left alone.")
            return 0, len(sizes)

        for size in missing:
            error = _validate_gown_fields(name, self.category, color_name, code, size)
            if error:
                self.stderr.write(f"  {label}: {error}")
                return None

        # One photo per design, shared by its sizes (a size added later reuses the one an earlier size already has).
        photo_url = next((g.photo_url for g in have.values() if g.photo_url), "")
        photo_file = None
        if not photo_url:
            path = folder / photo_name
            if not path.is_file():
                self.stderr.write(f"  {label}: photo not found: {path}")
                return None
            photo_file = SimpleUploadedFile(photo_name.lower().replace(" ", "-"), path.read_bytes(), content_type="image/jpeg")
            error = _validate_gown_photo(photo_file)
            if error:
                self.stderr.write(f"  {label}: {error}")
                return None

        sizes_text = ", ".join(str(s) for s in missing)
        if dry_run:
            self.stdout.write(f"  {label}: would add {sizes_text}" + (f" with photo {photo_name} ({photo_file.size // 1024} KB)" if photo_file else " (photo already on file)"))
            return len(missing), len(sizes) - len(missing)

        if photo_file:
            try:
                photo_url = _save_gown_photo(photo_file)
            except Exception as exc:                       # the photo is stored before anything is written, so a failure leaves nothing behind
                self.stderr.write(f"  {label}: the photo could not be uploaded ({exc}). Nothing was added for it.")
                return None

        tags = []
        try:
            with transaction.atomic():                     # all of one design's sizes, or none of them
                for size in missing:
                    tags.append(self.create_unit(name, color_name, code, size, price, photo_url))
        except (IntegrityError, DataError) as exc:
            self.stderr.write(f"  {label}: could not be saved ({exc}). Nothing was added for it.")
            return None
        self.stdout.write(f"  {label}: added {sizes_text} -> {', '.join(tags)}")
        return len(missing), len(sizes) - len(missing)

    def create_unit(self, name, color_name, code, size, price, photo_url):
        """One physical gown record, the way gown_create_view makes it (retrying the tag number if two requests ever collide)."""
        for _attempt in range(3):
            tracking_number = Gown.next_tracking_number(self.category)
            gown_id = f"{self.category}-{code}-{tracking_number:03d}"
            try:
                with transaction.atomic():
                    Gown.objects.create(
                        gown_id=gown_id, name=name, category=self.category, color_name=color_name, color_code=code, size=size,
                        rental_price=price, condition=Gown.Condition.GOOD, status=Gown.Status.AVAILABLE, photo_url=photo_url,
                    )
            except IntegrityError:
                continue
            return gown_id
        raise IntegrityError("could not get a free tag number")
