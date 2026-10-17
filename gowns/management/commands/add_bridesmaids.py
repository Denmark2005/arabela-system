"""python manage.py add_bridesmaids [--photos FOLDER] [--dry-run]

Adds the 15 Bridesmaid Dresses from the owner's table, each in Large and Extra Large (30 gowns, rental ₱1,800), exactly the way Add Gown in
the admin would -- it is the same engine as `add_barongs` (gowns/management/commands/add_barongs.py): the same checks, the same photo
storage (Cloudinary on the live site), the same category-wide tag numbers, one photo shared by a dress's two sizes, so the customer site
shows each dress as ONE product with L / XL buttons.

Names follow the Barong style, colour first: "Black Bridesmaid Dress 1" ... "Black Bridesmaid Dress 4". They have to differ from each other
(gowns with the same name are ONE product on the customer site, so four plain "Black" dresses would merge into one card with one photo).
The number counts per colour, in the table's order. A colour that only one dress has still gets its "1" ("Sage Green Bridesmaid Dress 1"),
so adding a second dress of that colour later never means renaming the first.

The colour field uses the admin's own preset where there is one (the table's "navy blue" is the preset Navy, "emerald green" is Emerald),
so Gown Catalog's colour filter and the colour codes stay consistent; Dusty Blue and Old Rose get a free 2-letter code of their own.

Safe to run again, `--dry-run` changes nothing, and `python live.py add_bridesmaids` reaches the live shop on purpose.
The photos are read from `Downloads\\AREBELA GOWN\\Bridesmaid Dresses` (Bridesmaid Dresses 01.jpg ... 15.jpg) unless `--photos` says otherwise."""
from gowns.management.commands.add_barongs import Command as BatchCommand
from gowns.models import Gown

CATEGORY = Gown.Category.BRIDESMAID_DRESSES
SIZES = (Gown.Size.LARGE, Gown.Size.EXTRA_LARGE)
PRICE = "1800"
# (gown name, photo file, colour) -- table order. Photo n is the table's row n.
BRIDESMAIDS = (
    ("Sage Green Bridesmaid Dress 1", "Bridesmaid Dresses 01.jpg", "Sage Green"),
    ("Black Bridesmaid Dress 1", "Bridesmaid Dresses 02.jpg", "Black"),
    ("Navy Blue Bridesmaid Dress 1", "Bridesmaid Dresses 03.jpg", "Navy"),
    ("Black Bridesmaid Dress 2", "Bridesmaid Dresses 04.jpg", "Black"),
    ("White Bridesmaid Dress 1", "Bridesmaid Dresses 05.jpg", "White"),
    ("Dusty Blue Bridesmaid Dress 1", "Bridesmaid Dresses 06.jpg", "Dusty Blue"),
    ("Beige Bridesmaid Dress 1", "Bridesmaid Dresses 07.jpg", "Beige"),
    ("Navy Blue Bridesmaid Dress 2", "Bridesmaid Dresses 08.jpg", "Navy"),
    ("Black Bridesmaid Dress 3", "Bridesmaid Dresses 09.jpg", "Black"),
    ("Black Bridesmaid Dress 4", "Bridesmaid Dresses 10.jpg", "Black"),
    ("White Bridesmaid Dress 2", "Bridesmaid Dresses 11.jpg", "White"),
    ("Old Rose Bridesmaid Dress 1", "Bridesmaid Dresses 12.jpg", "Old Rose"),
    ("Emerald Green Bridesmaid Dress 1", "Bridesmaid Dresses 13.jpg", "Emerald"),
    ("Dusty Blue Bridesmaid Dress 2", "Bridesmaid Dresses 14.jpg", "Dusty Blue"),
    ("White Bridesmaid Dress 3", "Bridesmaid Dresses 15.jpg", "White"),
)
# The lone colours' names before they got their "1". A shop that was filled in between holds these, so `rename_bridesmaids` (and the guard in
# add_bridesmaids) recognise them.
EARLIER_NAMES = {
    "Sage Green Bridesmaid Dress 1": ("Sage Green Bridesmaid Dress",),
    "Beige Bridesmaid Dress 1": ("Beige Bridesmaid Dress",),
    "Old Rose Bridesmaid Dress 1": ("Old Rose Bridesmaid Dress",),
    "Emerald Green Bridesmaid Dress 1": ("Emerald Green Bridesmaid Dress",),
}


class Command(BatchCommand):
    help = "Add the 15 Bridesmaid Dresses in Large and Extra Large (re-runnable; --dry-run changes nothing)."

    category = CATEGORY
    sizes = SIZES
    price = PRICE
    designs = BRIDESMAIDS
    former_names = {}
    earlier_names = EARLIER_NAMES
    code_hints = {}
    folder_name = "Bridesmaid Dresses"
    noun = "Bridesmaid dress"
    rename_command = "rename_bridesmaids"
