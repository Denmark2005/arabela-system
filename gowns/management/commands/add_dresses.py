"""python manage.py add_dresses [--photos FOLDER] [--dry-run]

Adds the 9 Dresses from the owner's table (rows 1-9), each in Large and Extra Large (18 gowns, rental ₱1,800), exactly the way Add Gown in
the admin would -- the same engine as `add_barongs` / `add_bridesmaids` (gowns/management/commands/add_barongs.py): the same checks, the same
photo storage (Cloudinary on the live site), the same category-wide tag numbers, one photo shared by a dress's two sizes, so the customer
site shows each dress as ONE product with L / XL buttons.

Names follow the same style, colour first and numbered per colour in the table's order, a lone colour included: "Light Pink Dress 1",
"Light Pink Dress 2", "Light Pink Dress 3", "Yellow Dress 1". They must differ from each other (gowns with the same name are ONE product on
the customer site, so three plain "Light Pink" dresses would merge into one card with one photo), and the "1" on a lone colour means a second
dress of that colour added later never forces a rename of the first.

The colour field uses the admin's own preset where there is one (Yellow, Beige, Pink, White; the table's "navy blue" is the preset Navy);
Old Rose reuses the code the shop already has for it (the bridesmaid dresses use it), and Light Pink gets a free 2-letter code of its own.

Safe to run again, `--dry-run` changes nothing, and `python live.py add_dresses` reaches the live shop on purpose.
The photos are read from `Downloads\\AREBELA GOWN\\Dresses` (Dresses 01.jpg ... 09.jpg) unless `--photos` says otherwise. (The folder also holds
a 10th photo; the table has nine rows, so it is left alone.)"""
from gowns.management.commands.add_barongs import Command as BatchCommand
from gowns.models import Gown

CATEGORY = Gown.Category.DRESSES
SIZES = (Gown.Size.LARGE, Gown.Size.EXTRA_LARGE)
PRICE = "1800"
# (gown name, photo file, colour) -- table order. Photo n is the table's row n.
DRESSES = (
    ("Old Rose Dress 1", "Dresses 01.jpg", "Old Rose"),
    ("Yellow Dress 1", "Dresses 02.jpg", "Yellow"),
    ("Beige Dress 1", "Dresses 03.jpg", "Beige"),
    ("Light Pink Dress 1", "Dresses 04.jpg", "Light Pink"),
    ("Light Pink Dress 2", "Dresses 05.jpg", "Light Pink"),
    ("Light Pink Dress 3", "Dresses 06.jpg", "Light Pink"),
    ("Pink Dress 1", "Dresses 07.jpg", "Pink"),
    ("Navy Blue Dress 1", "Dresses 08.jpg", "Navy"),
    ("White Dress 1", "Dresses 09.jpg", "White"),
)


class Command(BatchCommand):
    help = "Add the 9 Dresses in Large and Extra Large (re-runnable; --dry-run changes nothing)."

    category = CATEGORY
    sizes = SIZES
    price = PRICE
    designs = DRESSES
    former_names = {}
    earlier_names = {}
    code_hints = {}
    folder_name = "Dresses"
    noun = "Dress"
    rename_command = ""
