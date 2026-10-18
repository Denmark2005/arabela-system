"""python manage.py add_ball_gown_tulle [--photos FOLDER] [--dry-run]

Adds the 50 Ball Gown Tulle gowns from the owner's inventory list (photos Ball Gown Tulle 01 ... 50): exactly 50 gowns, ONE physical gown per design, ALL in
Large (the list's items 39-50 also say XL; the owner asked for Large only), at the price the owner wrote (items 1-30 1,500, items 31-50 4,000) -- no extra
sizes, no extra quantity -- so the gown IDs run 001 to 050 in the owner's order. (Same lesson as the kids gowns and Filipiniana: this command only ever
ADDS what is missing and never removes anything.) The folder also holds photos 51 and 52; they are not on the list and are left alone.

Same engine as `add_barongs` (gowns/management/commands/add_barongs.py): the same checks as Add Gown, the same photo storage (Cloudinary on the live
site) and the same category-wide tag numbers -- so the customer site shows each gown as ONE product whose single size is the only size button that can
be chosen (the rest are greyed).

Names are colour first, then the type, then a number per colour: "Yellow Ball Gown Tulle 1" ... "Yellow Ball Gown Tulle 5". They must differ from each
other (gowns with the same name are ONE product on the customer site, so five plain "Yellow" gowns would merge into a single card with a single photo);
a colour with only one gown still gets its "1" so a second one added later never forces a rename. A two-colour gown is written "Red and Black"
(the way "Black and Beige Filipiniana" is).

The colour field uses the admin's own preset where there is one (the list's "Emerald Green" is the preset Emerald, "Navy Blue" is Navy, "Gray" is Grey,
exactly like the bridesmaid dresses and the Filipiniana); a colour with no preset (Old Rose, Light Yellow, Light Pink, Light Blue, Light Purple, Light Green,
Dusty Blue and the two-colour ones) reuses the code a gown of that colour already has in the shop, else gets a free 2-letter code of its own.

Safe to run again, `--dry-run` changes nothing, and `python live.py add_ball_gown_tulle` reaches the live shop on purpose.
The photos are read from `Downloads\\AREBELA GOWN\\Ball Gown Tulle` unless `--photos` says otherwise."""
from gowns.management.commands.add_barongs import Command as BatchCommand
from gowns.models import Gown

CATEGORY = Gown.Category.BALL_GOWN_TULLE
# The owner's list, in order: item n = photo "Ball Gown Tulle nn.jpg". (colour as the owner wrote it, rental price)
LIST = (
    ("Yellow", "1500"), ("Old Rose", "1500"), ("White", "1500"), ("Old Rose", "1500"), ("Red", "1500"),
    ("Beige", "1500"), ("Yellow", "1500"), ("Light Yellow", "1500"), ("Black", "1500"), ("Red", "1500"),
    ("Light Pink", "1500"), ("Purple", "1500"), ("Old Rose", "1500"), ("Emerald Green", "1500"), ("Yellow", "1500"),
    ("Red", "1500"), ("Red", "1500"), ("Emerald Green", "1500"), ("Red & Black", "1500"), ("Red", "1500"),
    ("Yellow", "1500"), ("Pink", "1500"), ("Peach", "1500"), ("Light Blue", "1500"), ("Red", "1500"),
    ("Navy Blue", "1500"), ("Light Blue", "1500"), ("Yellow & Black", "1500"), ("Green", "1500"), ("Red", "1500"),
    ("Pink", "4000"), ("Green", "4000"), ("Black & Beige", "4000"), ("Light Purple", "4000"), ("Pink", "4000"),
    ("White", "4000"), ("White", "4000"), ("Light Green", "4000"), ("Pink & Light Blue", "4000"), ("Light Blue", "4000"),
    ("Dusty Blue", "4000"), ("Pink & Beige", "4000"), ("Pink & Gray", "4000"), ("Purple", "4000"), ("Pink & Light Blue", "4000"),
    ("Pink & Light Blue", "4000"), ("Pink", "4000"), ("Pink & Gray", "4000"), ("Light Green", "4000"), ("Pink", "4000"),
)
# The colour FIELD where it differs from the colour in the gown's name: the admin's preset spelling.
_FIELD_COLOUR = {"Emerald Green": "Emerald", "Navy Blue": "Navy", "Gray": "Grey"}


def _words(colour):
    """'Pink & Gray' -> 'Pink and Gray' (the name wording)."""
    return colour.replace(" & ", " and ")


def _field_colour(colour):
    """The colour field: preset spellings swapped in, two-colour gowns written with 'and'."""
    return " ".join(_FIELD_COLOUR.get(word, word) for word in _words(colour).split(" ")) if " and " in _words(colour) else _FIELD_COLOUR.get(colour, colour)


def _build():
    counts, rows = {}, []
    for index, (colour, price) in enumerate(LIST, start=1):
        base = _words(colour)
        counts[base] = counts.get(base, 0) + 1
        rows.append((f"{base} Ball Gown Tulle {counts[base]}", f"Ball Gown Tulle {index:02d}.jpg", _field_colour(colour), price))
    return tuple(rows)


# (gown name, photo file, colour field, price) -- the owner's order; photo n is list item n.
BALL_GOWN_TULLES = _build()
DESIGNS = tuple((name, photo, colour) for name, photo, colour, _price in BALL_GOWN_TULLES)
SIZES_BY_NAME = {name: (Gown.Size.LARGE,) for name, _p, _c, _pr in BALL_GOWN_TULLES}
PRICES_BY_NAME = {name: price for name, _p, _c, price in BALL_GOWN_TULLES}


class Command(BatchCommand):
    help = "Add the 50 Ball Gown Tulle gowns, one Large gown each at the owner's price (re-runnable; --dry-run changes nothing)."

    category = CATEGORY
    sizes = (Gown.Size.LARGE,)
    price = "1500"
    designs = DESIGNS
    design_sizes = SIZES_BY_NAME
    design_prices = PRICES_BY_NAME
    former_names = {}
    earlier_names = {}
    # the codes the other batches already use for these colours, so one colour keeps one code across the whole shop
    code_hints = {"Old Rose": ("OL",), "Dusty Blue": ("DB",), "Light Pink": ("LP",), "Light Blue": ("LB",)}
    folder_name = "Ball Gown Tulle"
    noun = "Ball Gown Tulle gown"
    rename_command = ""
