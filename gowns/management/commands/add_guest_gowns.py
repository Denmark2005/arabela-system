"""python manage.py add_guest_gowns [--photos FOLDER] [--dry-run]

Adds the 17 Guest Gown designs from the owner's list (photos Guest Gown 01 ... 17), each in the size(s) and at the price the owner wrote -- 30 physical gowns
in all: designs 1-13 come in Large AND XL (26 gowns), designs 14 and 15 in Large only, designs 16 and 17 in XL only (4 gowns). A design's two sizes are two
gowns that share one name and one photo, so the customer site shows it as ONE product with an L and an XL button; the gown IDs are handed out in the owner's
order (001 ... 030), a design's Large first and then its XL. (This command only ever ADDS what is missing and never removes or renames anything: a name
changed in this file AFTER it has been run on the live shop would add new gowns instead of renaming the old ones -- rename in Gown Catalog instead.)

Same engine as `add_barongs` (gowns/management/commands/add_barongs.py): the same checks as Add Gown, the same photo storage (Cloudinary on the live
site) and the same category-wide tag numbers.

Names are colour first, then the type, then a number per colour: "Sage Green Guest Gown 1" ... "Sage Green Guest Gown 3". Different designs must have
different names (gowns with the same name are ONE product on the customer site, so three plain "Sage Green" designs would merge into a single card with
a single photo); a colour with only one design still gets its "1" so a second one added later never forces a rename.

The colour field uses the admin's own preset where there is one (Sage Green, Champagne, Pink, Purple, Blue, Silver, Red, Rose Gold, Brown; the list's
"Blush Pink" is the preset Blush and "Navy Blue" is Navy, like the bridesmaid dresses); "Slate Blue" has no preset and gets a free 2-letter code of its own
(or the code a gown of that colour already has in the shop).

Safe to run again, `--dry-run` changes nothing, and `python live.py add_guest_gowns` reaches the live shop on purpose.
The photos are read from `Downloads\\AREBELA GOWN\\Guest Gown` unless `--photos` says otherwise."""
from gowns.management.commands.add_barongs import Command as BatchCommand
from gowns.models import Gown

CATEGORY = Gown.Category.GUEST_GOWN
_SIZE = {"L": Gown.Size.LARGE, "XL": Gown.Size.EXTRA_LARGE}
_BOTH = ("L", "XL")
# The owner's list, in order: item n = photo "Guest Gown nn.jpg". (colour as the owner wrote it, sizes, rental price)
LIST = (
    ("Sage Green", _BOTH, "1500"), ("Slate Blue", _BOTH, "3500"), ("Champagne", _BOTH, "3500"), ("Slate Blue", _BOTH, "3500"),
    ("Champagne", _BOTH, "1500"), ("Pink", _BOTH, "1500"), ("Purple", _BOTH, "3500"), ("Blush Pink", _BOTH, "1500"),
    ("Blush Pink", _BOTH, "3500"), ("Sage Green", _BOTH, "3500"), ("Blue", _BOTH, "1500"), ("Silver", _BOTH, "1500"),
    ("Red", _BOTH, "3500"), ("Rose Gold", ("L",), "3500"), ("Brown", ("L",), "1500"), ("Navy Blue", ("XL",), "3500"),
    ("Sage Green", ("XL",), "3500"),
)
# The colour FIELD where it differs from the colour in the gown's name: the admin's preset spelling.
_FIELD_COLOUR = {"Blush Pink": "Blush", "Navy Blue": "Navy"}


def _build():
    counts, rows = {}, []
    for index, (colour, size_codes, price) in enumerate(LIST, start=1):
        counts[colour] = counts.get(colour, 0) + 1
        rows.append((f"{colour} Guest Gown {counts[colour]}", f"Guest Gown {index:02d}.jpg", _FIELD_COLOUR.get(colour, colour), size_codes, price))
    return tuple(rows)


# (gown name, photo file, colour field, size codes, price) -- the owner's order; photo n is list item n.
GUEST_GOWNS = _build()
DESIGNS = tuple((name, photo, colour) for name, photo, colour, _sizes, _price in GUEST_GOWNS)
SIZES_BY_NAME = {name: tuple(_SIZE[code] for code in size_codes) for name, _p, _c, size_codes, _pr in GUEST_GOWNS}
PRICES_BY_NAME = {name: price for name, _p, _c, _s, price in GUEST_GOWNS}


class Command(BatchCommand):
    help = "Add the 17 Guest Gown designs in the owner's sizes and prices, 30 gowns (re-runnable; --dry-run changes nothing)."

    category = CATEGORY
    sizes = (Gown.Size.LARGE,)                       # not used: every design names its own sizes
    price = "3500"
    designs = DESIGNS
    design_sizes = SIZES_BY_NAME
    design_prices = PRICES_BY_NAME
    former_names = {}
    earlier_names = {}
    code_hints = {}
    folder_name = "Guest Gown"
    noun = "Guest Gown"
    rename_command = ""
