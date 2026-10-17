"""python manage.py reset_wedding_gowns [--dry-run]

Renumbers the Wedding Gown category to 28 gowns in a row, as the owner asked. The shop's wedding gowns were named after their photos
(Wedding Gown 01 ... 33), and five numbers never existed (18, 19, 21, 22, 23), so the numbers had holes. After this the 28 gowns are
Wedding Gown 1 ... Wedding Gown 28 with gown IDs `Wedding Gown-WH-001` ... `Wedding Gown-WH-028` -- the photo-20 gown becomes 18, photo 24 becomes
19, ..., and the last one (photo 33) becomes 28 -- every gown is White (colour code WH) and carries the price from the owner's list.

Photo n of the old list -> new number:  1-17 stay 1-17,  20 -> 18,  24 -> 19,  25 -> 20,  26 -> 21,  27 -> 22,  28 -> 23,  29 -> 24,
30 -> 25,  31 -> 26,  32 -> 27,  33 -> 28.

What changes: the gown's name, its gown ID (tag number), its colour (White / WH), its price, and the category's number counter (the next gown added
gets 029). What does NOT change: the gown itself (its row, so every booking, block and check still points at it), its photo, size, status,
condition, notes, and its page web address (slug, so saved links and bags keep working). Booking lines that point at one of these gowns get the
gown's new name, so the admin never shows an old number for the gown that is physically on the rack.

It refuses -- changing nothing at all -- unless the category is EXACTLY the 28 gowns the owner described: each old number once (no two gowns of
the same number, none missing, no extras) and every name readable as "Wedding Gown <number>" (or "One"). Run on a category that is already
reset, it just says so. The whole change is one atomic step: it is all done or none of it is.
`--dry-run` prints the plan and writes nothing. To reach the live shop on purpose: `python live.py reset_wedding_gowns`. Take a backup first."""
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from gowns.models import Gown, GownRemoval, GownSequence
from reservations.models import ReservationItem

CATEGORY = Gown.Category.WEDDING_GOWN
COLOR_NAME, COLOR_CODE = "White", "WH"
# (the number in the gown's old name = its photo number, price) in the owner's order; the position in this list (1-28) is the new number.
OLD_LIST = (
    (1, 8000), (2, 8000), (3, 5500), (4, 8000), (5, 17000), (6, 8000), (7, 10000), (8, 9000), (9, 10000), (10, 12000), (11, 11000), (12, 12000),
    (13, 12000), (14, 6000), (15, 16000), (16, 4500), (17, 10000), (20, 14000), (24, 10000), (25, 14000), (26, 7000), (27, 12000), (28, 11000),
    (29, 9000), (30, 13000), (31, 5500), (32, 8000), (33, 12000),
)
TOTAL = len(OLD_LIST)                                       # 28
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}


def number_in_name(name):
    """17 for 'Wedding Gown 17' / 'Wedding Gown 017' / 'wedding gown seventeen'-style 'One'..'Twelve'; None when the name is anything else."""
    match = re.fullmatch(r"\s*wedding\s+gown\s+(\d+|[a-z]+)\s*", name or "", flags=re.IGNORECASE)
    if not match:
        return None
    token = match.group(1).lower()
    return int(token) if token.isdigit() else _WORDS.get(token)


def final_name(number):
    return f"Wedding Gown {number}"


def final_gown_id(number):
    return f"{CATEGORY}-{COLOR_CODE}-{number:03d}"


class Command(BaseCommand):
    help = "Renumber the Wedding Gowns to 28 in a row (Wedding Gown 1-28, IDs 001-028, all White, the owner's prices). --dry-run changes nothing."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show the plan; change nothing.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        self.stdout.write(("DRY RUN -- nothing will be changed.\n" if dry_run else "") + f"Category: {CATEGORY}")
        with transaction.atomic():
            gowns = list(Gown.objects.select_for_update().filter(category=CATEGORY).order_by("id"))
            if self.already_reset(gowns):
                self.stdout.write(self.style.SUCCESS(f"The {CATEGORY} category is already reset ({TOTAL} gowns, 001-{TOTAL:03d}). Nothing to do."))
                return
            plan = self.plan(gowns)
            self.show(plan)
            if dry_run:
                self.stdout.write(self.style.SUCCESS(f"\nWould renumber {TOTAL} gowns. Nothing was changed."))
                return
            self.apply(plan)
        self.stdout.write(self.style.SUCCESS(f"\nDone: {TOTAL} Wedding Gowns are now Wedding Gown 1-{TOTAL}, IDs 001-{TOTAL:03d}, all White; the next gown added gets {TOTAL + 1:03d}."))

    # ---- what state is the category in? ----
    def already_reset(self, gowns):
        if len(gowns) != TOTAL:
            return False
        names = sorted(number_in_name(g.name) or 0 for g in gowns)
        tags = sorted(g.tracking_number or 0 for g in gowns)
        return names == list(range(1, TOTAL + 1)) and tags == list(range(1, TOTAL + 1)) and all(g.gown_id == final_gown_id(g.tracking_number) for g in gowns)

    def plan(self, gowns):
        """[(gown, old_number, new_number, price)] in the owner's order, or CommandError -- listing every problem at once -- when the category is
        not exactly the 28 gowns the owner described."""
        wanted = {old: (new, price) for new, (old, price) in enumerate(OLD_LIST, start=1)}
        by_number, problems = {}, []
        for gown in gowns:
            number = number_in_name(gown.name)
            if number is None:
                problems.append(f'{gown.gown_id}: the name "{gown.name}" is not "Wedding Gown <number>".')
            elif number not in wanted:
                problems.append(f'{gown.gown_id} "{gown.name}": there is no Wedding Gown {number} in the owner\'s list.')
            else:
                by_number.setdefault(number, []).append(gown)
        for number, same in by_number.items():
            if len(same) > 1:
                problems.append(f"Wedding Gown {number} is on {len(same)} gowns ({', '.join(g.gown_id for g in same)}); the owner's list has one gown per number.")
        missing = [old for old in wanted if old not in by_number]
        if missing:
            problems.append("These numbers from the owner's list are not in the shop: " + ", ".join(str(n) for n in missing) + ".")
        if problems:
            raise CommandError("The Wedding Gown category is not the 28 gowns the owner described, so NOTHING was changed:\n  - " + "\n  - ".join(problems))
        return [(by_number[old][0], old, new, price) for new, (old, price) in enumerate(OLD_LIST, start=1)]

    def show(self, plan):
        self.stdout.write(f"{'':3}{'now':<44}{'becomes':<44}price")
        for gown, old, new, price in plan:
            self.stdout.write(f"  {gown.gown_id + ' ' + gown.name:<44.44}{final_gown_id(new) + ' ' + final_name(new):<44.44}{price:,}"
                              + ("" if int(gown.rental_price) == price else f"  (was {int(gown.rental_price):,})"))
        lines = ReservationItem.objects.filter(gown__in=[g for g, *_ in plan]).count()
        logged = GownRemoval.objects.filter(category=CATEGORY).count()
        recolored = sum(1 for g, *_ in plan if (g.color_name, g.color_code) != (COLOR_NAME, COLOR_CODE))
        self.stdout.write(f"\n{lines} booking line(s) point at these gowns and will show the new name; {recolored} gown(s) change colour to {COLOR_NAME}; "
                          f"{logged} Wedding Gown entr{'y' if logged == 1 else 'ies'} in the Removal Log stay as history.")

    # ---- do it ----
    def apply(self, plan):
        # Two steps: a final ID may be the one ANOTHER gown still holds (the photo-25 gown becomes 020 while the photo-20 gown is still 020), and
        # gown IDs are unique, so every gown first gets a temporary ID, then its final one.
        for gown, *_ in plan:
            gown.gown_id = f"RESET-{gown.pk}"
            gown.save(update_fields=["gown_id", "updated_at"])
        for gown, _old, new, price in plan:
            gown.name = final_name(new)
            gown.gown_id = final_gown_id(new)
            gown.color_name, gown.color_code = COLOR_NAME, COLOR_CODE
            gown.rental_price = price
            gown.save(update_fields=["name", "gown_id", "color_name", "color_code", "rental_price", "updated_at"])   # updated_at listed on purpose
            # .update() skips the auto_now stamp, so the booking line's updated_at is set by hand
            ReservationItem.objects.filter(gown=gown).update(gown_name=gown.name, updated_at=timezone.now())
        row, _created = GownSequence.objects.select_for_update().get_or_create(category=CATEGORY, color_code=GownSequence.CATEGORY_WIDE)
        row.next_value = TOTAL + 1
        row.save(update_fields=["next_value"])
        self.verify(plan)

    def verify(self, plan):
        """Re-read everything and refuse (rolling the whole change back) unless it is exactly what was promised."""
        now = list(Gown.objects.filter(category=CATEGORY))
        if len(now) != TOTAL:
            raise CommandError(f"Expected {TOTAL} gowns, found {len(now)}; nothing was changed.")
        if sorted(g.tracking_number or 0 for g in now) != list(range(1, TOTAL + 1)) or any(g.gown_id != final_gown_id(g.tracking_number) for g in now):
            raise CommandError("The gown IDs are not 001-028 in a row; nothing was changed.")
        for gown, _old, new, price in plan:
            fresh = Gown.objects.get(pk=gown.pk)
            if (fresh.name, fresh.gown_id, fresh.color_name, int(fresh.rental_price)) != (final_name(new), final_gown_id(new), COLOR_NAME, price):
                raise CommandError(f"{fresh.gown_id} did not take its new values; nothing was changed.")
        if GownSequence.objects.get(category=CATEGORY, color_code=GownSequence.CATEGORY_WIDE).next_value != TOTAL + 1:
            raise CommandError("The number counter was not reset; nothing was changed.")
