"""python manage.py add_suits [--photos FOLDER] [--dry-run]

Adds the 23 suits from the owner's table (the "Armani Suit" photo folder, the Suit category on the website): 45 gowns in all, each suit in
its OWN sizes (the owner's list, drawn once at random from S / M / L / XL and then approved: one to four sizes per suit; the three coat
sequence suits are L only, as in the table) and its own price (collar suits 1,500, coat sequence 1,000). Same engine as `add_barongs`
(gowns/management/commands/add_barongs.py): the same checks as Add Gown, the same photo storage (Cloudinary on the live site), the same
category-wide tag numbers, one photo shared by a suit's sizes -- so the customer site shows each suit as ONE product with its size buttons
(only the sizes listed here clickable, the rest greyed).

Names carry the TYPE and the colour, then a number per name: "Regular Collar Light Gray 1", "Chinese Collar Red 2", "Coat Sequence Black 1".
They must differ from each other (gowns with the same name are ONE product on the customer site, so two plain "Chinese Collar Red" suits
would merge into one card with one photo); a name with only one suit still gets its "1" so a second one added later never forces a rename.

The colour field uses the admin's own preset where there is one (Navy, Royal Blue, Mint, Black, Red, White, Purple; the table's "gray" is the
preset Grey); Light Gray, Light Blue, Khaki, Black & Gold and Black, Green & Gray get a free 2-letter code of their own, and Old Rose / Dusty
Blue reuse the codes the shop already has for them.

Safe to run again, `--dry-run` changes nothing, and `python live.py add_suits` reaches the live shop on purpose.
The photos are read from `Downloads\\AREBELA GOWN\\Armani Suit` (Armani Suit 01.jpg ... 23.jpg) unless `--photos` says otherwise."""
from gowns.management.commands.add_barongs import Command as BatchCommand
from gowns.models import Gown

CATEGORY = Gown.Category.SUIT
_SIZE = {"S": Gown.Size.SMALL, "M": Gown.Size.MEDIUM, "L": Gown.Size.LARGE, "XL": Gown.Size.EXTRA_LARGE}
COLLAR, COAT = "1500", "1000"
# (gown name, photo file, colour, sizes, price) -- the owner's table order; photo n is the table's row n.
SUITS = (
    ("Chinese Collar Navy Blue 1", "Armani Suit 01.jpg", "Navy", "L", COLLAR),
    ("Chinese Collar Light Gray 1", "Armani Suit 02.jpg", "Light Gray", "M XL", COLLAR),
    ("Chinese Collar Royal Blue 1", "Armani Suit 03.jpg", "Royal Blue", "M L", COLLAR),
    ("Coat Sequence Black & Gold 1", "Armani Suit 04.jpg", "Black & Gold", "L", COAT),
    ("Chinese Collar Old Rose 1", "Armani Suit 05.jpg", "Old Rose", "S M L XL", COLLAR),
    ("Regular Collar Light Gray 1", "Armani Suit 06.jpg", "Light Gray", "S", COLLAR),
    ("Regular Collar Mint Green 1", "Armani Suit 07.jpg", "Mint", "L", COLLAR),
    ("Coat Sequence Black 1", "Armani Suit 08.jpg", "Black", "L", COAT),
    ("Regular Collar Black 1", "Armani Suit 09.jpg", "Black", "M L", COLLAR),
    ("Regular Collar Dusty Blue 1", "Armani Suit 10.jpg", "Dusty Blue", "M L", COLLAR),
    ("Chinese Collar Red 1", "Armani Suit 11.jpg", "Red", "S M L", COLLAR),
    ("Chinese Collar Khaki 1", "Armani Suit 12.jpg", "Khaki", "M L", COLLAR),
    ("Regular Collar White 1", "Armani Suit 13.jpg", "White", "S M XL", COLLAR),
    ("Chinese Collar Light Blue 1", "Armani Suit 14.jpg", "Light Blue", "S M XL", COLLAR),
    ("Chinese Collar Light Gray 2", "Armani Suit 15.jpg", "Light Gray", "M L XL", COLLAR),
    ("Chinese Collar Red 2", "Armani Suit 16.jpg", "Red", "L XL", COLLAR),
    ("Chinese Collar White 1", "Armani Suit 17.jpg", "White", "XL", COLLAR),
    ("Coat Sequence Black, Green & Gray 1", "Armani Suit 18.jpg", "Black, Green & Gray", "L", COAT),
    ("Regular Collar Gray 1", "Armani Suit 19.jpg", "Grey", "S", COLLAR),
    ("Regular Collar Light Blue 1", "Armani Suit 20.jpg", "Light Blue", "M", COLLAR),
    ("Regular Collar Royal Blue 1", "Armani Suit 21.jpg", "Royal Blue", "S M L", COLLAR),
    ("Regular Collar Purple 1", "Armani Suit 22.jpg", "Purple", "M XL", COLLAR),
    ("Chinese Collar Gray 1", "Armani Suit 23.jpg", "Grey", "S M XL", COLLAR),
)
DESIGNS = tuple((name, photo, color) for name, photo, color, _sizes, _price in SUITS)
SIZES_BY_NAME = {name: tuple(_SIZE[code] for code in sizes.split()) for name, _p, _c, sizes, _pr in SUITS}
PRICES_BY_NAME = {name: price for name, _p, _c, _s, price in SUITS}


class Command(BatchCommand):
    help = "Add the 23 suits in their own sizes and prices, 45 gowns (re-runnable; --dry-run changes nothing)."

    category = CATEGORY
    sizes = (Gown.Size.LARGE,)                       # not used: every suit names its own sizes
    price = COLLAR
    designs = DESIGNS
    design_sizes = SIZES_BY_NAME
    design_prices = PRICES_BY_NAME
    former_names = {}
    earlier_names = {}
    code_hints = {}
    folder_name = "Armani Suit"
    noun = "Suit"
    rename_command = ""
