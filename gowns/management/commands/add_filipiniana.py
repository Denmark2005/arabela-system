"""python manage.py add_filipiniana [--photos FOLDER] [--dry-run]

Adds the 30 Filipiniana gowns from the owner's list (photos Filipiniana 01 ... 30): exactly 30 gowns, ONE physical gown per design, each in the size
and at the price the owner wrote (Large 1,500 or XL 3,000) -- no extra sizes, no extra quantity -- so the gown IDs run 001 to 030 in the owner's order.
(Same lesson as the kids gowns: this command only ever ADDS what is missing and never removes anything.)

Same engine as `add_barongs` (gowns/management/commands/add_barongs.py): the same checks as Add Gown, the same photo storage (Cloudinary on the live
site) and the same category-wide tag numbers -- so the customer site shows each gown as ONE product whose single size is the only size button that can
be chosen (the rest are greyed).

Names are colour first, then the type, then a number per colour: "Beige Filipiniana 1" ... "Beige Filipiniana 9". They must differ from each other (gowns
with the same name are ONE product on the customer site, so nine plain "Beige" gowns would merge into a single card with a single photo); a colour with
only one gown still gets its "1" so a second one added later never forces a rename.

The colour field uses the admin's own preset where there is one (Beige, Black, Pink, Peach, Gold, Silver, Blue, Red; the list's "Gray" is the preset Grey,
"Navy Blue" is Navy, "Multicolored" is Multicolor); Cream, Black and Beige, Black and White and Black and Purple get a free 2-letter code of their own.

Safe to run again, `--dry-run` changes nothing, and `python live.py add_filipiniana` reaches the live shop on purpose.
The photos are read from `Downloads\\AREBELA GOWN\\Filipiniana` unless `--photos` says otherwise."""
from gowns.management.commands.add_barongs import Command as BatchCommand
from gowns.models import Gown

CATEGORY = Gown.Category.FILIPINIANA
_SIZE = {"S": Gown.Size.SMALL, "M": Gown.Size.MEDIUM, "L": Gown.Size.LARGE, "XL": Gown.Size.EXTRA_LARGE}
# (gown name, photo file, colour, size, price) -- the owner's order; photo n is list item n.
FILIPINIANAS = (
    ("Beige Filipiniana 1", "Filipiniana 01.jpg", "Beige", "L", "1500"),
    ("Beige Filipiniana 2", "Filipiniana 02.jpg", "Beige", "XL", "3000"),
    ("Black and Beige Filipiniana 1", "Filipiniana 03.jpg", "Black and Beige", "XL", "3000"),
    ("Black Filipiniana 1", "Filipiniana 04.jpg", "Black", "L", "1500"),
    ("Beige Filipiniana 3", "Filipiniana 05.jpg", "Beige", "L", "1500"),
    ("Gray Filipiniana 1", "Filipiniana 06.jpg", "Grey", "XL", "3000"),
    ("Pink Filipiniana 1", "Filipiniana 07.jpg", "Pink", "XL", "3000"),
    ("Beige Filipiniana 4", "Filipiniana 08.jpg", "Beige", "L", "1500"),
    ("Beige Filipiniana 5", "Filipiniana 09.jpg", "Beige", "L", "1500"),
    ("Beige Filipiniana 6", "Filipiniana 10.jpg", "Beige", "L", "1500"),
    ("Beige Filipiniana 7", "Filipiniana 11.jpg", "Beige", "XL", "3000"),
    ("Beige Filipiniana 8", "Filipiniana 12.jpg", "Beige", "L", "1500"),
    ("Black and White Filipiniana 1", "Filipiniana 13.jpg", "Black and White", "XL", "3000"),
    ("Beige Filipiniana 9", "Filipiniana 14.jpg", "Beige", "L", "1500"),
    ("Pink Filipiniana 2", "Filipiniana 15.jpg", "Pink", "L", "1500"),
    ("Pink Filipiniana 3", "Filipiniana 16.jpg", "Pink", "L", "1500"),
    ("Black Filipiniana 2", "Filipiniana 17.jpg", "Black", "XL", "3000"),
    ("Peach Filipiniana 1", "Filipiniana 18.jpg", "Peach", "XL", "3000"),
    ("Pink Filipiniana 4", "Filipiniana 19.jpg", "Pink", "XL", "3000"),
    ("Cream Filipiniana 1", "Filipiniana 20.jpg", "Cream", "L", "1500"),
    ("Cream Filipiniana 2", "Filipiniana 21.jpg", "Cream", "XL", "3000"),
    ("Navy Blue Filipiniana 1", "Filipiniana 22.jpg", "Navy", "XL", "3000"),
    ("Black Filipiniana 3", "Filipiniana 23.jpg", "Black", "XL", "3000"),
    ("Gold Filipiniana 1", "Filipiniana 24.jpg", "Gold", "XL", "3000"),
    ("Multicolored Filipiniana 1", "Filipiniana 25.jpg", "Multicolor", "XL", "3000"),
    ("Silver Filipiniana 1", "Filipiniana 26.jpg", "Silver", "XL", "3000"),
    ("Blue Filipiniana 1", "Filipiniana 27.jpg", "Blue", "XL", "3000"),
    ("Silver Filipiniana 2", "Filipiniana 28.jpg", "Silver", "XL", "3000"),
    ("Black and Purple Filipiniana 1", "Filipiniana 29.jpg", "Black and Purple", "XL", "3000"),
    ("Red Filipiniana 1", "Filipiniana 30.jpg", "Red", "XL", "3000"),
)
DESIGNS = tuple((name, photo, color) for name, photo, color, _size_code, _price in FILIPINIANAS)
SIZES_BY_NAME = {name: (_SIZE[size_code],) for name, _p, _c, size_code, _pr in FILIPINIANAS}
PRICES_BY_NAME = {name: price for name, _p, _c, _s, price in FILIPINIANAS}


class Command(BatchCommand):
    help = "Add the 30 Filipiniana gowns, one gown each in the owner's size and price (re-runnable; --dry-run changes nothing)."

    category = CATEGORY
    sizes = (Gown.Size.LARGE,)                       # not used: every gown names its own size
    price = "1500"
    designs = DESIGNS
    design_sizes = SIZES_BY_NAME
    design_prices = PRICES_BY_NAME
    former_names = {}
    earlier_names = {}
    code_hints = {"Black and Beige": ("BB",), "Black and White": ("BW",), "Black and Purple": ("BP",)}
    folder_name = "Filipiniana"
    noun = "Filipiniana gown"
    rename_command = ""
