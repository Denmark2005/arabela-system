"""python manage.py rename_bridesmaids [--dry-run]

For a shop that already holds the bridesmaid dresses under the names they had BEFORE the lone colours got their "1" (for example
"Beige Bridesmaid Dress"): gives them the final names ("Beige Bridesmaid Dress 1"). Same engine and same promises as `rename_barongs`
(gowns/management/commands/rename_barongs.py): every size of a dress is renamed together, nothing else changes, safe to run twice,
`--dry-run` writes nothing. A shop that has none of them -- or has them under their final names already -- simply reports that.
To reach the live shop on purpose: `python live.py rename_bridesmaids`."""
from gowns.management.commands.add_bridesmaids import BRIDESMAIDS, CATEGORY, EARLIER_NAMES
from gowns.management.commands.rename_barongs import Command as RenameCommand


class Command(RenameCommand):
    help = "Rename bridesmaid dresses that were added before the lone colours got a number (all sizes together; --dry-run changes nothing)."

    category = CATEGORY
    designs = BRIDESMAIDS
    former_names = {}
    earlier_names = EARLIER_NAMES
    noun = "Bridesmaid dress"
