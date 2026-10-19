"""python manage.py add_evening_gowns [--photos FOLDER] [--dry-run]

Adds the 25 Evening Gowns from the owner's list: exactly 25 gowns, ONE physical gown per design, ALL in Large, at the price the owner wrote (1,500 or
3,500) -- no extra sizes, no extra quantity. The list's numbers are the PHOTO numbers (Evening Gown 01 ... 10, 21 ... 28, 71, 72, 82, 87, 89, 91, 92 -- the
folder holds many more photos that are not on the list and are left alone), so list item 25 uses the photo "Evening Gown 92.jpg". The gown IDs run 001 to 025
in the owner's order. (This command only ever ADDS what is missing and never removes or renames anything: a name changed in this file AFTER it has been run
on the live shop would add a new gown instead of renaming the old one -- rename in Gown Catalog instead.)

Same engine as `add_barongs` (gowns/management/commands/add_barongs.py): the same checks as Add Gown, the same photo storage (Cloudinary on the live
site) and the same category-wide tag numbers -- so the customer site shows each gown as ONE product whose single size is the only size button that can
be chosen (the rest are greyed).

Names are colour first, then the type, then a number per colour: "Burgundy Evening Gown 1" ... "Burgundy Evening Gown 4". They must differ from each other
(gowns with the same name are ONE product on the customer site, so four plain "Burgundy" gowns would merge into a single card with a single photo); a colour
with only one gown still gets its "1" so a second one added later never forces a rename. The number counts per colour in the owner's order; it is NOT the
photo number.

The colour field uses the admin's own preset where there is one (Burgundy, Black, White, Maroon, Silver, Rose Gold, Champagne, Gold; the list's "Navy Blue" is
the preset Navy, "Emerald Green" is Emerald and "Blush Pink" is Blush, like the bridesmaid dresses); Rust Brown, Dusty Rose and Cream have no preset, so each
reuses the code a gown of that colour already has in the shop, else gets a free 2-letter code of its own.

Safe to run again, `--dry-run` changes nothing, and `python live.py add_evening_gowns` reaches the live shop on purpose.
The photos are read from `Downloads\\AREBELA GOWN\\Evening Gown` unless `--photos` says otherwise."""
from gowns.management.commands.add_barongs import Command as BatchCommand
from gowns.models import Gown

CATEGORY = Gown.Category.EVENING_GOWN
# The owner's list, in order: (photo number, colour as the owner wrote it, rental price). Every gown is Large.
LIST = (
    (1, "Burgundy", "3500"), (2, "Black", "1500"), (3, "Rust Brown", "1500"), (4, "Burgundy", "3500"), (5, "Navy Blue", "3500"),
    (6, "White", "1500"), (7, "Maroon", "3500"), (8, "White", "1500"), (9, "White", "1500"), (10, "Silver", "3500"),
    (21, "Rose Gold", "3500"), (22, "Champagne", "3500"), (23, "Black", "1500"), (24, "Black", "3500"), (25, "White", "1500"),
    (26, "Gold", "1500"), (27, "Champagne", "1500"), (28, "Dusty Rose", "1500"), (71, "Emerald Green", "3500"), (72, "Burgundy", "3500"),
    (82, "Black", "3500"), (87, "Blush Pink", "1500"), (89, "Dusty Rose", "1500"), (91, "Cream", "3500"), (92, "Burgundy", "3500"),
)
# The colour FIELD where it differs from the colour in the gown's name: the admin's preset spelling.
_FIELD_COLOUR = {"Navy Blue": "Navy", "Emerald Green": "Emerald", "Blush Pink": "Blush"}


def _build():
    counts, rows = {}, []
    for photo_number, colour, price in LIST:
        counts[colour] = counts.get(colour, 0) + 1
        rows.append((f"{colour} Evening Gown {counts[colour]}", f"Evening Gown {photo_number:02d}.jpg", _FIELD_COLOUR.get(colour, colour), price))
    return tuple(rows)


# (gown name, photo file, colour field, price) -- the owner's order.
EVENING_GOWNS = _build()
DESIGNS = tuple((name, photo, colour) for name, photo, colour, _price in EVENING_GOWNS)
SIZES_BY_NAME = {name: (Gown.Size.LARGE,) for name, _p, _c, _pr in EVENING_GOWNS}
PRICES_BY_NAME = {name: price for name, _p, _c, price in EVENING_GOWNS}


class Command(BatchCommand):
    help = "Add the 25 Evening Gowns, one Large gown each at the owner's price (re-runnable; --dry-run changes nothing)."

    category = CATEGORY
    sizes = (Gown.Size.LARGE,)
    price = "3500"
    designs = DESIGNS
    design_sizes = SIZES_BY_NAME
    design_prices = PRICES_BY_NAME
    former_names = {}
    earlier_names = {}
    code_hints = {}
    folder_name = "Evening Gown"
    noun = "Evening Gown"
    rename_command = ""
