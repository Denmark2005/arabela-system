"""python manage.py add_long_gowns [--photos FOLDER] [--dry-run]

Adds the 25 Long Gowns from the owner's list: exactly 25 gowns, ONE physical gown per design, ALL in Large, at the price the owner wrote (1,000 or 2,500) --
no extra sizes, no extra quantity. The list's numbers are the PHOTO numbers (Long Gown 31 ... 40 and 42 ... 56; photo 41 is not on the list, and the folder
holds many more photos that are not on the list -- all left alone), so list item 11 uses the photo "Long Gown 42.jpg". The gown IDs run 001 to 025 in the
owner's order. (This command only ever ADDS what is missing and never removes or renames anything: a name changed in this file AFTER it has been run on the
live shop would add a new gown instead of renaming the old one -- rename in Gown Catalog instead.)

Same engine as `add_barongs` (gowns/management/commands/add_barongs.py): the same checks as Add Gown, the same photo storage (Cloudinary on the live
site) and the same category-wide tag numbers -- so the customer site shows each gown as ONE product whose single size is the only size button that can
be chosen (the rest are greyed).

Names are colour first, then the type, then a number per colour: "Rose Gold Long Gown 1" ... "Rose Gold Long Gown 5". They must differ from each other
(gowns with the same name are ONE product on the customer site, so five plain "Rose Gold" gowns would merge into a single card with a single photo); a colour
with only one gown still gets its "1" so a second one added later never forces a rename. The number counts per colour in the owner's order; it is NOT the
photo number. A two-colour gown is written "Black and White" (the way the Filipiniana's "Black and White" is, so it keeps that colour's code).

The colour field uses the admin's own preset where there is one (Rose Gold, Burgundy, White, Black, Silver, Champagne, Blue; the list's "Navy Blue" is the
preset Navy); Dusty Pink has no preset, and "Black and White" / "Dusty Pink" each reuse the code a gown of that colour already has in the shop, else get a
free 2-letter code of their own.

Safe to run again, `--dry-run` changes nothing, and `python live.py add_long_gowns` reaches the live shop on purpose.
The photos are read from `Downloads\\AREBELA GOWN\\Long Gown` unless `--photos` says otherwise."""
from gowns.management.commands.add_barongs import Command as BatchCommand
from gowns.models import Gown

CATEGORY = Gown.Category.LONG_GOWN
# The owner's list, in order: (photo number, colour as the owner wrote it, rental price). Every gown is Large.
LIST = (
    (31, "Rose Gold", "2500"), (32, "Burgundy", "2500"), (33, "White", "1000"), (34, "Black", "2500"), (35, "Burgundy", "2500"),
    (36, "Silver", "2500"), (37, "Rose Gold", "2500"), (38, "Navy Blue", "2500"), (39, "Black", "2500"), (40, "Champagne", "2500"),
    (42, "Silver", "2500"), (43, "Black and White", "2500"), (44, "Champagne", "2500"), (45, "Black", "2500"), (46, "Rose Gold", "2500"),
    (47, "Black", "2500"), (48, "Silver", "2500"), (49, "Rose Gold", "2500"), (50, "Dusty Pink", "2500"), (51, "White", "1000"),
    (52, "Rose Gold", "2500"), (53, "Black", "2500"), (54, "Blue", "2500"), (55, "White", "2500"), (56, "Silver", "2500"),
)
# The colour FIELD where it differs from the colour in the gown's name: the admin's preset spelling.
_FIELD_COLOUR = {"Navy Blue": "Navy"}


def _build():
    counts, rows = {}, []
    for photo_number, colour, price in LIST:
        counts[colour] = counts.get(colour, 0) + 1
        rows.append((f"{colour} Long Gown {counts[colour]}", f"Long Gown {photo_number:02d}.jpg", _FIELD_COLOUR.get(colour, colour), price))
    return tuple(rows)


# (gown name, photo file, colour field, price) -- the owner's order.
LONG_GOWNS = _build()
DESIGNS = tuple((name, photo, colour) for name, photo, colour, _price in LONG_GOWNS)
SIZES_BY_NAME = {name: (Gown.Size.LARGE,) for name, _p, _c, _pr in LONG_GOWNS}
PRICES_BY_NAME = {name: price for name, _p, _c, price in LONG_GOWNS}


class Command(BatchCommand):
    help = "Add the 25 Long Gowns, one Large gown each at the owner's price (re-runnable; --dry-run changes nothing)."

    category = CATEGORY
    sizes = (Gown.Size.LARGE,)
    price = "2500"
    designs = DESIGNS
    design_sizes = SIZES_BY_NAME
    design_prices = PRICES_BY_NAME
    former_names = {}
    earlier_names = {}
    code_hints = {}
    folder_name = "Long Gown"
    noun = "Long Gown"
    rename_command = ""
