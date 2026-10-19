"""python manage.py add_luxury_gowns [--photos FOLDER] [--dry-run]

Adds the 21 Luxury Gown designs from the owner's list (photos Luxury Gown 01 ... 21), each in the size(s) and at the price the owner wrote -- 25 physical
gowns in all: designs 3, 9, 10 and 19 come in Large AND XL (8 gowns), designs 1, 2, 4-8, 11 and 12 in Large only (9 gowns), designs 13-18, 20 and 21 in XL
only (8 gowns). A design's two sizes are two gowns that share one name and one photo, so the customer site shows it as ONE product with an L and an XL button
(21 products); the gown IDs are handed out in the owner's order (001 ... 025), a design's Large first and then its XL. (This command only ever ADDS what is
missing and never removes or renames anything: a name changed in this file AFTER it has been run on the live shop would add new gowns instead of renaming the
old ones -- rename in Gown Catalog instead.)

Same engine as `add_barongs` (gowns/management/commands/add_barongs.py): the same checks as Add Gown, the same photo storage (Cloudinary on the live
site) and the same category-wide tag numbers.

Names are colour first, then the type, then a number per colour: "Burgundy Luxury Gown 1" ... "Burgundy Luxury Gown 3". Different designs must have different
names (gowns with the same name are ONE product on the customer site, so three plain "Burgundy" designs would merge into a single card with a single photo); a
colour with only one design still gets its "1" so a second one added later never forces a rename. The number counts per colour in the owner's order; it is NOT
the photo number. A two-colour gown is written "Black and Red" / "Red and Gold" (the way "Black and White" is elsewhere).

The colour field uses the admin's own preset where there is one (Lavender, Red, Burgundy, Charcoal, Maroon, Silver, Royal Blue, Peach, Black, Rose Gold,
Purple; the list's "Navy Blue" is the preset Navy and "Emerald Green" is Emerald, like the bridesmaid dresses); "Light Pink" reuses the code the shop's Light
Pink gowns already have, and "Black and Red" / "Red and Gold" each reuse the code a gown of that colour already has in the shop, else get a free 2-letter code.

Safe to run again, `--dry-run` changes nothing, and `python live.py add_luxury_gowns` reaches the live shop on purpose.
The photos are read from `Downloads\\AREBELA GOWN\\Luxury Gown` unless `--photos` says otherwise."""
from gowns.management.commands.add_barongs import Command as BatchCommand
from gowns.models import Gown

CATEGORY = Gown.Category.LUXURY_GOWN
_SIZE = {"L": Gown.Size.LARGE, "XL": Gown.Size.EXTRA_LARGE}
_BOTH = ("L", "XL")
_L = ("L",)
_XL = ("XL",)
# The owner's list, in order: item n = photo "Luxury Gown nn.jpg". (colour as the owner wrote it, sizes, rental price)
LIST = (
    ("Lavender", _L, "5500"), ("Black and Red", _L, "5500"), ("Red", _BOTH, "3500"), ("Burgundy", _L, "5500"), ("Burgundy", _L, "5500"),
    ("Charcoal", _L, "5500"), ("Maroon", _L, "3000"), ("Silver", _L, "3000"), ("Navy Blue", _BOTH, "3000"), ("Royal Blue", _BOTH, "3000"),
    ("Emerald Green", _L, "3000"), ("Burgundy", _L, "3000"), ("Silver", _XL, "3500"), ("Peach", _XL, "4500"), ("Navy Blue", _XL, "4500"),
    ("Black", _XL, "3500"), ("Rose Gold", _XL, "2500"), ("Emerald Green", _XL, "2500"), ("Light Pink", _BOTH, "2500"),
    ("Red and Gold", _XL, "10000"), ("Purple", _XL, "2500"),
)
# The colour FIELD where it differs from the colour in the gown's name: the admin's preset spelling.
_FIELD_COLOUR = {"Navy Blue": "Navy", "Emerald Green": "Emerald"}


def _build():
    counts, rows = {}, []
    for index, (colour, size_codes, price) in enumerate(LIST, start=1):
        counts[colour] = counts.get(colour, 0) + 1
        rows.append((f"{colour} Luxury Gown {counts[colour]}", f"Luxury Gown {index:02d}.jpg", _FIELD_COLOUR.get(colour, colour), size_codes, price))
    return tuple(rows)


# (gown name, photo file, colour field, size codes, price) -- the owner's order; photo n is list item n.
LUXURY_GOWNS = _build()
DESIGNS = tuple((name, photo, colour) for name, photo, colour, _sizes, _price in LUXURY_GOWNS)
SIZES_BY_NAME = {name: tuple(_SIZE[code] for code in size_codes) for name, _p, _c, size_codes, _pr in LUXURY_GOWNS}
PRICES_BY_NAME = {name: price for name, _p, _c, _s, price in LUXURY_GOWNS}


class Command(BatchCommand):
    help = "Add the 21 Luxury Gown designs in the owner's sizes and prices, 25 gowns (re-runnable; --dry-run changes nothing)."

    category = CATEGORY
    sizes = (Gown.Size.LARGE,)                       # not used: every design names its own sizes
    price = "3000"
    designs = DESIGNS
    design_sizes = SIZES_BY_NAME
    design_prices = PRICES_BY_NAME
    former_names = {}
    earlier_names = {}
    code_hints = {}
    folder_name = "Luxury Gown"
    noun = "Luxury Gown"
    rename_command = ""
