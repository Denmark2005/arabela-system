"""python manage.py add_mother_gowns [--photos FOLDER] [--dry-run]

Adds the 30 Mother Gowns from the owner's list (photos Mother Gown 01 ... 30): exactly 30 gowns, ONE physical gown per design, each in the size and at
the price the owner wrote (items 1-15 Large, items 16-30 XL; 1,500 or 3,500) -- no extra sizes, no extra quantity -- so the gown IDs run 001 to 030 in the
owner's order. (This command only ever ADDS what is missing and never removes or renames anything: a name changed in this file AFTER it has been run on the
live shop would add a second gown instead of renaming the first -- rename in Gown Catalog instead.) The folder also holds photos 31 to 38; they are not on
the list and are left alone.

Same engine as `add_barongs` (gowns/management/commands/add_barongs.py): the same checks as Add Gown, the same photo storage (Cloudinary on the live
site) and the same category-wide tag numbers -- so the customer site shows each gown as ONE product whose single size is the only size button that can
be chosen (the rest are greyed).

Names are colour first, then the type, then a number per colour: "Gold Mother Gown 1" ... "Gold Mother Gown 6". They must differ from each other (gowns
with the same name are ONE product on the customer site, so six plain "Gold" gowns would merge into a single card with a single photo); a colour with only
one gown still gets its "1" so a second one added later never forces a rename.

The colour field uses the admin's own preset where there is one (Gold, Rose Gold, Blue, Silver, Pink, Champagne, Beige, Purple; the list's "Navy Blue" is
the preset Navy and "Emerald Green" is Emerald, exactly like the bridesmaid dresses).

Safe to run again, `--dry-run` changes nothing, and `python live.py add_mother_gowns` reaches the live shop on purpose.
The photos are read from `Downloads\\AREBELA GOWN\\Mother Gown` unless `--photos` says otherwise."""
from gowns.management.commands.add_barongs import Command as BatchCommand
from gowns.models import Gown

CATEGORY = Gown.Category.MOTHER_GOWN
_SIZE = {"L": Gown.Size.LARGE, "XL": Gown.Size.EXTRA_LARGE}
# The owner's list, in order: item n = photo "Mother Gown nn.jpg". (colour as the owner wrote it, size, rental price)
LIST = (
    ("Navy Blue", "L", "3500"), ("Gold", "L", "3500"), ("Rose Gold", "L", "3500"), ("Blue", "L", "3500"), ("Silver", "L", "3500"),
    ("Pink", "L", "3500"), ("Champagne", "L", "3500"), ("Beige", "L", "3500"), ("Rose Gold", "L", "3500"), ("Gold", "L", "3500"),
    ("Pink", "L", "1500"), ("Silver", "L", "1500"), ("Beige", "L", "1500"), ("Gold", "L", "3500"), ("Gold", "L", "3500"),
    ("Silver", "XL", "3500"), ("Pink", "XL", "1500"), ("Beige", "XL", "3500"), ("Champagne", "XL", "3500"), ("Gold", "XL", "3500"),
    ("Silver", "XL", "3500"), ("Purple", "XL", "3500"), ("Blue", "XL", "3500"), ("Gold", "XL", "3500"), ("Champagne", "XL", "1500"),
    ("Pink", "XL", "3500"), ("Emerald Green", "XL", "3500"), ("Emerald Green", "XL", "3500"), ("Emerald Green", "XL", "3500"),
    ("Blue", "XL", "3500"),
)
# The colour FIELD where it differs from the colour in the gown's name: the admin's preset spelling.
_FIELD_COLOUR = {"Navy Blue": "Navy", "Emerald Green": "Emerald"}


def _build():
    counts, rows = {}, []
    for index, (colour, size_code, price) in enumerate(LIST, start=1):
        counts[colour] = counts.get(colour, 0) + 1
        rows.append((f"{colour} Mother Gown {counts[colour]}", f"Mother Gown {index:02d}.jpg", _FIELD_COLOUR.get(colour, colour), size_code, price))
    return tuple(rows)


# (gown name, photo file, colour field, size, price) -- the owner's order; photo n is list item n.
MOTHER_GOWNS = _build()
DESIGNS = tuple((name, photo, colour) for name, photo, colour, _size_code, _price in MOTHER_GOWNS)
SIZES_BY_NAME = {name: (_SIZE[size_code],) for name, _p, _c, size_code, _pr in MOTHER_GOWNS}
PRICES_BY_NAME = {name: price for name, _p, _c, _s, price in MOTHER_GOWNS}


class Command(BatchCommand):
    help = "Add the 30 Mother Gowns, one gown each in the owner's size and price (re-runnable; --dry-run changes nothing)."

    category = CATEGORY
    sizes = (Gown.Size.LARGE,)                       # not used: every gown names its own size
    price = "3500"
    designs = DESIGNS
    design_sizes = SIZES_BY_NAME
    design_prices = PRICES_BY_NAME
    former_names = {}
    earlier_names = {}
    code_hints = {}
    folder_name = "Mother Gown"
    noun = "Mother Gown"
    rename_command = ""
