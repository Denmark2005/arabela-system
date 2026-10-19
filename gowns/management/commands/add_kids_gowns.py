"""python manage.py add_kids_gowns [--photos FOLDER] [--dry-run]

Adds the 33 Kids Gowns from the owner's list (photos Kids Gown 01 ... 33): exactly 33 gowns in all, ONE physical gown per design with ONE size
and its own price -- no extra sizes, no extra quantity -- so the gown IDs run 001 to 033 in the owner's order. The size of each gown is the
owner's own (Small, Medium, Large or XL, from the owner's list), written out below.

The four sizes stand for the usual kids' age bands, which the size guide will show later:
    Small (S) = 1-3 years   Medium (M) = 4-6 years   Large (L) = 7-9 years   Extra Large (XL) = 10-12 years

Same engine as `add_barongs` (gowns/management/commands/add_barongs.py): the same checks as Add Gown, the same photo storage (Cloudinary on the
live site) and the same category-wide tag numbers -- so the customer site shows each gown as ONE product whose single size is the only size
button that can be chosen (the rest are greyed).

Names are colour first, then the type, then a number per colour: "Pink Kids Gown 1" ... "Pink Kids Gown 10". They must differ from each other
(gowns with the same name are ONE product on the customer site, so ten plain "Pink" gowns would merge into a single card with a single photo); a
colour with only one gown still gets its "1" so a second one added later never forces a rename.

The colour field uses the admin's own preset where there is one (Red, Rose Gold, White, Lavender, Pink, Blue, Peach, Yellow, Maroon, Purple; the
list's "Mint Green" is the preset Mint); Rose, Violet, Pink and Violet and Pink and Blue get a free 2-letter code of their own.

Safe to run again, `--dry-run` changes nothing, and `python live.py add_kids_gowns` reaches the live shop on purpose.
The photos are read from `Downloads\\AREBELA GOWN\\Kids Gown` unless `--photos` says otherwise."""
from gowns.management.commands.add_barongs import Command as BatchCommand
from gowns.models import Gown

CATEGORY = Gown.Category.KIDS_GOWN
_SIZE = {"S": Gown.Size.SMALL, "M": Gown.Size.MEDIUM, "L": Gown.Size.LARGE, "XL": Gown.Size.EXTRA_LARGE}
# The age band each size stands for, as the owner's Kids Gown size chart says it (gowns/size_guides.py KIDS_GUIDE shows it on the gown page; its first row,
# 3-4 years "XS", is a size the shop does not make).
AGE_BANDS = {"S": "5-6 years", "M": "7-8 years", "L": "9-10 years", "XL": "11-12 years"}
# (gown name, photo file, colour, sizes, price) -- the owner's order; photo n is list item n.
KIDS_GOWNS = (
    ("Red Kids Gown 1", "Kids Gown 01.jpg", "Red", "S", "2500"),
    ("Rose Gold Kids Gown 1", "Kids Gown 02.jpg", "Rose Gold", "S", "2500"),
    ("White Kids Gown 1", "Kids Gown 03.jpg", "White", "S", "1800"),
    ("Lavender Kids Gown 1", "Kids Gown 04.jpg", "Lavender", "L", "2800"),
    ("Pink Kids Gown 1", "Kids Gown 05.jpg", "Pink", "L", "2800"),
    ("Red Kids Gown 2", "Kids Gown 06.jpg", "Red", "L", "2800"),
    ("Rose Kids Gown 1", "Kids Gown 07.jpg", "Rose", "M", "2200"),
    ("Pink Kids Gown 2", "Kids Gown 08.jpg", "Pink", "M", "2500"),
    ("Violet Kids Gown 1", "Kids Gown 09.jpg", "Violet", "XL", "3000"),
    ("Pink Kids Gown 3", "Kids Gown 10.jpg", "Pink", "XL", "3000"),
    ("Blue Kids Gown 1", "Kids Gown 11.jpg", "Blue", "XL", "3000"),
    ("Lavender Kids Gown 2", "Kids Gown 12.jpg", "Lavender", "M", "2500"),
    ("Red Kids Gown 3", "Kids Gown 13.jpg", "Red", "L", "2800"),
    ("Pink Kids Gown 4", "Kids Gown 14.jpg", "Pink", "M", "2500"),
    ("Pink Kids Gown 5", "Kids Gown 15.jpg", "Pink", "L", "2800"),
    ("Peach Kids Gown 1", "Kids Gown 16.jpg", "Peach", "S", "2200"),
    ("Pink and Violet Kids Gown 1", "Kids Gown 17.jpg", "Pink and Violet", "L", "2800"),
    ("Yellow Kids Gown 1", "Kids Gown 18.jpg", "Yellow", "M", "2500"),
    ("Pink Kids Gown 6", "Kids Gown 19.jpg", "Pink", "L", "2800"),
    ("Lavender Kids Gown 3", "Kids Gown 20.jpg", "Lavender", "XL", "3000"),
    ("Yellow Kids Gown 2", "Kids Gown 21.jpg", "Yellow", "XL", "3000"),
    ("Maroon Kids Gown 1", "Kids Gown 22.jpg", "Maroon", "L", "2800"),
    ("Pink and Blue Kids Gown 1", "Kids Gown 23.jpg", "Pink and Blue", "L", "2800"),
    ("Pink Kids Gown 7", "Kids Gown 24.jpg", "Pink", "S", "2500"),
    ("Purple Kids Gown 1", "Kids Gown 25.jpg", "Purple", "XL", "3000"),
    ("Pink Kids Gown 8", "Kids Gown 26.jpg", "Pink", "L", "2800"),
    ("Blue Kids Gown 2", "Kids Gown 27.jpg", "Blue", "S", "2200"),
    ("Pink Kids Gown 9", "Kids Gown 28.jpg", "Pink", "S", "2000"),
    ("Purple Kids Gown 2", "Kids Gown 29.jpg", "Purple", "M", "2200"),
    ("Mint Green Kids Gown 1", "Kids Gown 30.jpg", "Mint", "S", "2200"),
    ("Pink Kids Gown 10", "Kids Gown 31.jpg", "Pink", "L", "2800"),
    ("Purple Kids Gown 3", "Kids Gown 32.jpg", "Purple", "L", "2800"),
    ("Blue Kids Gown 3", "Kids Gown 33.jpg", "Blue", "L", "2800"),
)
DESIGNS = tuple((name, photo, color) for name, photo, color, _sizes, _price in KIDS_GOWNS)
SIZES_BY_NAME = {name: tuple(_SIZE[code] for code in sizes.split()) for name, _p, _c, sizes, _pr in KIDS_GOWNS}
PRICES_BY_NAME = {name: price for name, _p, _c, _s, price in KIDS_GOWNS}


class Command(BatchCommand):
    help = "Add the 33 Kids Gowns, one gown each with its own size and price (re-runnable; --dry-run changes nothing)."

    category = CATEGORY
    sizes = (Gown.Size.MEDIUM,)                      # not used: every gown names its own sizes
    price = "2500"
    designs = DESIGNS
    design_sizes = SIZES_BY_NAME
    design_prices = PRICES_BY_NAME
    former_names = {}
    earlier_names = {}
    code_hints = {"Pink and Violet": ("PV",), "Pink and Blue": ("PB",)}
    folder_name = "Kids Gown"
    noun = "Kids gown"
    rename_command = ""
