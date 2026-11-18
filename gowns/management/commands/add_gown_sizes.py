"""python manage.py add_gown_sizes [--dry-run]

Grows the catalog to 650 gowns by adding SIZES that a design does not have yet (Small, Medium, Large, Extra Large), inside the categories that are
already there. Nothing else changes: no new category, no existing gown or booking is touched.

How it chooses (the owner agreed to an automatic split, which is an ESTIMATE of the real stock):
  * every category has a fixed target (TARGETS below, 650 in all: the categories' current sizes scaled up in proportion; Barong can only take the
    7 sizes it lacks, so its leftover went to the bigger categories);
  * a category needs `target - the gowns it has now`, so running the command again adds nothing (the target is reached, not "added again");
  * inside a category every design gets ONE new size before any design gets a second, and a design gets the size closest to one it already has
    (a Medium-only gown gets Large before Small, Large before Extra Large...). When two sizes are equally close the one the category has more of
    wins, so the split follows what the shop really rents;
  * a new gown is a copy of the design's existing gown (same name, colour, price and photo) with the new size, status Available, and its own
    tag number from the category's running count, exactly like Add Gown makes it. The customer site shows one product with more size buttons;
  * a design whose gowns are all Out-of-Stock is left alone, and Extra Small / Double XL / Free Size are never added.

Everything is planned first and written in ONE transaction: if anything is wrong, nothing is added. `--dry-run` prints the plan and changes
nothing. `python live.py add_gown_sizes --dry-run` reads the live shop; without --dry-run it writes to the live shop on purpose."""
import time
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import DataError, IntegrityError, transaction
from django.utils.text import slugify

from gowns.models import Gown, GownSequence, GownSlugSequence

SIZE_ORDER = (Gown.Size.SMALL, Gown.Size.MEDIUM, Gown.Size.LARGE, Gown.Size.EXTRA_LARGE)
# When two sizes are equally close, this is the shop-wide order of preference (what the shop rents most): Large, Extra Large, Medium, Small.
SHOP_PREFERENCE = (Gown.Size.LARGE, Gown.Size.EXTRA_LARGE, Gown.Size.MEDIUM, Gown.Size.SMALL)

# How many gowns each category holds when the command is done. 650 in all.
TARGETS = {
    Gown.Category.BALL_GOWN_TULLE: 83,
    Gown.Category.SUIT: 76,
    Gown.Category.KIDS_GOWN: 56,
    Gown.Category.BRIDESMAID_DRESSES: 51,
    Gown.Category.FILIPINIANA: 51,
    Gown.Category.GUEST_GOWN: 51,
    Gown.Category.MOTHER_GOWN: 51,
    Gown.Category.WEDDING_GOWN: 47,
    Gown.Category.EVENING_GOWN: 42,
    Gown.Category.LONG_GOWN: 42,
    Gown.Category.LUXURY_GOWN: 42,
    Gown.Category.BARONG: 28,
    Gown.Category.DRESSES: 30,
}
TOTAL = 650
assert sum(TARGETS.values()) == TOTAL


def check_copy(template, size):
    """'' when a copy of this gown in `size` is a valid gown, else the reason. Only what the copy could get wrong is looked at (the sizes are fixed
    and valid; the rest is the existing gown's own data), so it costs no database query -- the admin's full check asks the database several things
    per gown, which is slow for hundreds of copies on a remote database and tells nothing new about a gown that already exists."""
    if not template.name or len(template.name) > 150:
        return "the gown name is empty or longer than 150 characters"
    if not template.color_name or len(template.color_name) > 40:
        return "the colour name is empty or longer than 40 characters"
    if not template.color_code or len(template.color_code) > 2:
        return "the colour code must be 1-2 letters"
    if len(template.design_variant or "") > 60:
        return "the design variant is longer than 60 characters"
    if size not in SIZE_ORDER:
        return "that size is not one of Small, Medium, Large, Extra Large"
    return ""


def plan_category(category, units, need):
    """[(template gown, size to add), ...] that bring `category` up by `need` gowns. `units` are the category's gowns.

    Raises ValueError (with the reason) when the category has too few empty size slots to take `need` more gowns."""
    designs = defaultdict(list)
    for unit in units:
        designs[unit.name.casefold()].append(unit)
    category_counts = Counter(unit.size for unit in units)
    # the order sizes win a tie: what this category has most of, then the shop-wide preference
    rank = {size: (-category_counts.get(size, 0), SHOP_PREFERENCE.index(size)) for size in SIZE_ORDER}
    index = {size: i for i, size in enumerate(SIZE_ORDER)}

    candidates = []
    for name, group in designs.items():
        if all(unit.status == Gown.Status.OUT_OF_STOCK for unit in group):
            continue                                                 # a design the shop has taken out of circulation gets nothing new
        group.sort(key=lambda g: (g.rental_price, g.id))
        template = group[0]
        held = {unit.size for unit in group}
        first_tag = min((unit.tracking_number or 0) for unit in group)
        candidates.append({"template": template, "held": set(held), "key": (first_tag, name)})
    candidates.sort(key=lambda d: d["key"])

    def best_missing(held):
        missing = [size for size in SIZE_ORDER if size not in held]
        if not missing:
            return None
        near = [index[h] for h in held if h in index]
        def distance(size):
            return min(abs(index[size] - i) for i in near) if near else 0
        return min(missing, key=lambda size: (distance(size), rank[size]))

    plan = []
    while need > 0:
        added_this_round = 0
        for design in candidates:
            if need == 0:
                break
            size = best_missing(design["held"])
            if size is None:
                continue
            plan.append((design["template"], size))
            design["held"].add(size)
            need -= 1
            added_this_round += 1
        if not added_this_round:
            raise ValueError(f"only {len(plan)} size slot(s) are free, {need} more would be needed")
    return plan


class Command(BaseCommand):
    help = "Add missing sizes to the current designs until the catalog holds 650 gowns (re-runnable; --dry-run changes nothing)."

    targets = TARGETS

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show the plan; change nothing.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        self.stdout.write(("DRY RUN -- nothing will be changed.\n" if dry_run else "") + f"Target: {sum(self.targets.values())} gowns in {len(self.targets)} categories.")
        all_units = list(Gown.objects.all().order_by("id"))
        by_category = defaultdict(list)
        for unit in all_units:
            by_category[unit.category].append(unit)

        plans = {}
        problems = []
        for category, target in self.targets.items():
            units = by_category.get(category, [])
            need = target - len(units)
            if need <= 0:
                plans[category] = []
                self.stdout.write(f"  {category}: {len(units)} now, target {target} -- nothing to add.")
                continue
            try:
                plan = plan_category(category, units, need)
            except ValueError as exc:
                problems.append(f"{category}: {exc}.")
                continue
            for template, size in plan:
                error = check_copy(template, size)
                if error:
                    problems.append(f"{category}, {template.name} ({size}): {error}")
            plans[category] = plan
            self.stdout.write(f"  {category}: {len(units)} now -> {len(units) + len(plan)} (adds {len(plan)}).")
        if problems:
            for line in problems:
                self.stderr.write("  " + line)
            raise CommandError(f"{len(problems)} problem(s) above. Nothing was added.")

        total_add = sum(len(plan) for plan in plans.values())
        if dry_run:
            self.stdout.write("")
            for category, plan in plans.items():
                for template, size in plan:
                    self.stdout.write(f"    {category}: {template.name} + {size}")
            self.stdout.write(self.style.SUCCESS(f"\nWould add {total_add} gown(s): {len(all_units)} -> {len(all_units) + total_add}."))
            return

        started = time.monotonic()
        self.say(f"Writing {total_add} gown(s) in one go -- this should take well under a minute. Please wait; do not press Ctrl+C.")
        try:
            with transaction.atomic():                         # every category, or nothing
                created = self.write(plans, all_units)
                final = Gown.objects.count()
                expected = len(all_units) + total_add
                if final != expected:
                    raise CommandError(f"The catalog would hold {final} gowns instead of {expected}. Nothing was added.")
        except (IntegrityError, DataError) as exc:
            raise CommandError(f"A gown could not be saved ({exc}). Nothing was added.")
        self.stdout.write(self.style.SUCCESS(
            f"\nAdded {len(created)} gown(s) in {time.monotonic() - started:.0f} seconds. The catalog now holds {Gown.objects.count()}."))

    def say(self, text):
        self.stdout.write(text)
        self.stdout.flush()

    def write(self, plans, existing):
        """Save every planned gown. The tag numbers and the web addresses (slugs) are reserved for all of them at once, and the gowns are saved in a
        few batches, so the number of trips to the database does not grow with the number of gowns (one trip per gown made 260 gowns take minutes on
        the remote database). Call inside the transaction: any failure rolls the numbers back too."""
        taken_slugs = {unit.slug for unit in existing}
        taken_ids = {unit.gown_id for unit in existing}
        self.say("  reserving the tag numbers ...")
        tags = {category: GownSequence.reserve_block(category, len(plan)) for category, plan in plans.items()}
        items = []                                              # (category, template, size, slug base), in plan order
        wanted = Counter()
        for category, plan in plans.items():
            for template, size in plan:
                base = slugify(template.name) or slugify(template.gown_id) or "gown"
                items.append((category, template, size, base))
                wanted[base] += 1
        self.say("  reserving the web addresses ...")
        slugs = GownSlugSequence.reserve_many(dict(wanted), taken_slugs)
        used = Counter()
        position = Counter()
        units = []
        for category, template, size, base in items:
            tag = tags[category][position[category]]
            position[category] += 1
            gown_id = f"{category}-{template.color_code}-{tag:03d}"
            if gown_id in taken_ids:
                raise CommandError(f"The tag {gown_id} is already used by another gown. Nothing was added.")
            taken_ids.add(gown_id)
            slug = slugs[base][used[base]]
            used[base] += 1
            units.append(Gown(
                gown_id=gown_id, slug=slug, name=template.name, category=category, color_name=template.color_name, color_code=template.color_code,
                size=size, design_variant=template.design_variant, rental_price=template.rental_price, condition=Gown.Condition.GOOD,
                status=Gown.Status.AVAILABLE, photo_url=template.photo_url))
        self.say(f"  saving {len(units)} gowns ...")
        Gown.objects.bulk_create(units, batch_size=100)
        return units
