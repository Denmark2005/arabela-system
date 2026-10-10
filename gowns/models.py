import re
import uuid

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

# The one canonical list of "known" gown colors and their 2-letter codes -- read by
# BOTH the Add/Edit Gown dropdown (rendered from this exact list, never hand-typed
# into the template again) and _validate_gown_fields's collision check
# (arabela_admin/views.py) that rejects a code already claimed by a different color.
# Before this, the dropdown's list lived only in the page's own JavaScript, with no
# server-side equivalent at all -- nothing stopped a bad pairing (Blue saved with
# Blush's code) from being written directly, whether through a hand-typed "Other"
# entry or a script bypassing the form entirely, and the one time that happened it
# went undetected until a staff member noticed the Edit screen couldn't recognize its
# own gown's color.
#
# `color_name`/`color_code` stay plain CharFields (not a choices enum) on purpose --
# "Other" must always remain a real escape hatch for a color no one has thought to add
# yet, so nothing here restricts what CAN be saved, only what's checked against once a
# code from this list (or already used by a real gown) means something specific.
#
# Ordered by family (neutrals, reds, blues, greens, metallics/purples, warm tones,
# neutrals-dark, multi) purely so the rendered dropdown reads sensibly top to bottom --
# order has no effect on validation.
GOWN_COLOR_PRESETS = (
    ("White", "WH"), ("Ivory", "IV"), ("Off-White", "OW"), ("Champagne", "CH"),
    ("Blush", "BL"), ("Nude", "ND"), ("Beige", "BG"), ("Black", "BK"),
    ("Red", "RD"), ("Maroon", "MR"), ("Burgundy", "BD"),
    ("Navy", "NV"), ("Blue", "BU"), ("Royal Blue", "RB"), ("Sky Blue", "SB"),
    ("Green", "GN"), ("Sage Green", "SG"), ("Emerald", "EM"), ("Teal", "TL"),
    ("Turquoise", "TQ"), ("Mint", "MT"),
    ("Gold", "GD"), ("Silver", "SV"), ("Rose Gold", "RG"),
    ("Purple", "PU"), ("Mauve", "MV"), ("Lavender", "LV"),
    ("Pink", "PK"), ("Coral", "CO"), ("Peach", "PE"),
    ("Yellow", "YL"), ("Orange", "OR"), ("Brown", "BR"), ("Charcoal", "CL"), ("Grey", "GY"),
    ("Multicolor", "MC"),
)

# Colors for the PHYSICAL tag/ribbon on a gown -- one fixed color per CATEGORY, so a
# staffer can tell what category a gown is from across the room. This is deliberately
# unrelated to Gown.color_name/color_code (the dress's own real color): a white
# Wedding Gown and a white Long Gown look alike, which is exactly why the tag color
# has to come from the category instead.
#
# The owner can reassign these from Gown Catalog (SiteSettings.category_tag_colors
# stores only what they've changed); DEFAULT_CATEGORY_TAG_COLORS fills in the rest, so
# a brand-new category or a fresh database always resolves to a real color.
TAG_COLOR_PALETTE = (
    ("White", "#FFFFFF"), ("Black", "#111827"), ("Red", "#DC2626"), ("Orange", "#F97316"),
    ("Yellow", "#FACC15"), ("Green", "#16A34A"), ("Teal", "#0D9488"), ("Blue", "#2563EB"),
    ("Navy", "#1E3A8A"), ("Purple", "#9333EA"), ("Pink", "#EC4899"), ("Brown", "#92400E"),
    ("Gray", "#6B7280"), ("Gold", "#CA8A04"),
)
TAG_COLOR_HEX = dict(TAG_COLOR_PALETTE)

# Keyed by Gown.Category value. A test pins this to exactly Gown.Category.values so
# adding a category without giving it a tag color fails loudly instead of silently
# rendering a tag with no color.
DEFAULT_CATEGORY_TAG_COLORS = {
    'Wedding Gown': 'White',
    'Evening Gown': 'Blue',
    'Long Gown': 'Red',
    'Luxury Gown': 'Purple',
    'Mother Gown': 'Pink',
    'Suit': 'Black',
    'Filipiniana': 'Green',
    'Guest Gown': 'Orange',
    'Dresses': 'Yellow',
    'Kids Gown': 'Teal',
    'Barong': 'Brown',
    'Ball Gown Tulle': 'Navy',
    'Bridesmaid Dresses': 'Gray',
}


def resolve_tag_colors(saved):
    """Every category's tag color as {category: color name}: whatever the owner has
    saved, falling back to the default for any category they haven't touched -- or
    whose saved value is no longer a color on the palette (a stale/hand-edited row
    must never produce a tag with no color)."""
    saved = saved if isinstance(saved, dict) else {}
    resolved = {}
    for category, default in DEFAULT_CATEGORY_TAG_COLORS.items():
        chosen = saved.get(category)
        resolved[category] = chosen if chosen in TAG_COLOR_HEX else default
    return resolved


class Gown(models.Model):
    class Category(models.TextChoices):
        WEDDING_GOWN = 'Wedding Gown', 'Wedding Gown'
        EVENING_GOWN = 'Evening Gown', 'Evening Gown'
        LONG_GOWN = 'Long Gown', 'Long Gown'
        LUXURY_GOWN = 'Luxury Gown', 'Luxury Gown'
        MOTHER_GOWN = 'Mother Gown', 'Mother Gown'
        SUIT = 'Suit', 'Suit'
        FILIPINIANA = 'Filipiniana', 'Filipiniana'
        GUEST_GOWN = 'Guest Gown', 'Guest Gown'
        DRESSES = 'Dresses', 'Dresses'
        KIDS_GOWN = 'Kids Gown', 'Kids Gown'
        BARONG = 'Barong', 'Barong'
        BALL_GOWN_TULLE = 'Ball Gown Tulle', 'Ball Gown Tulle'
        BRIDESMAID_DRESSES = 'Bridesmaid Dresses', 'Bridesmaid Dresses'

    class Size(models.TextChoices):
        SMALL = 'Small', 'Small'
        MEDIUM = 'Medium', 'Medium'
        LARGE = 'Large', 'Large'
        EXTRA_LARGE = 'Extra Large', 'Extra Large'
        FREE_SIZE = 'Free Size', 'Free Size'

    class Condition(models.TextChoices):
        NEW = 'New', 'New'
        GOOD = 'Good', 'Good'
        FAIR = 'Fair', 'Fair'
        NEEDS_REPAIR = 'Needs Repair', 'Needs Repair'

    class Status(models.TextChoices):
        AVAILABLE = 'Available', 'Available'
        RESERVED = 'Reserved', 'Reserved'
        OUT_OF_STOCK = 'Out-of-Stock', 'Out-of-Stock'

    gown_id = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=150)
    # URL-safe id for the customer-facing product page (/collections/<cat>/products/<slug>/).
    # Auto-filled in save() rather than by callers, so it's correct no matter how a row
    # gets created (the app's own view, Django admin, a shell). Globally unique -- one
    # physical gown, one URL -- so product_detail can look it up without also needing
    # the category from the URL to disambiguate.
    slug = models.SlugField(max_length=160, unique=True, blank=True)
    category = models.CharField(max_length=20, choices=Category.choices)
    color_name = models.CharField(max_length=40)
    color_code = models.CharField(max_length=2)
    size = models.CharField(max_length=20, choices=Size.choices)
    design_variant = models.CharField(max_length=60, blank=True)
    rental_price = models.DecimalField(max_digits=8, decimal_places=2)
    condition = models.CharField(max_length=20, choices=Condition.choices, default=Condition.GOOD)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.AVAILABLE)
    photo_url = models.URLField(blank=True)
    is_verified = models.BooleanField(default=False)
    last_returned_at = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    # The last time someone physically laid eyes on this gown and marked it present
    # (the periodic walkthrough check). Only the latest check is kept -- enough to answer
    # "when was this last confirmed here, and by whom", which is what narrows a missing
    # gown down to a time window. SET_NULL so removing a staff account never erases the
    # fact that a check happened.
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category', 'color_code', 'gown_id']

    def __str__(self):
        return f'{self.gown_id} — {self.name}'

    @property
    def tracking_number(self):
        """The number at the end of gown_id (the 12 in 'Wedding Gown-WH-012') -- what
        staff read off the physical tag. None for an ID that doesn't end in a number."""
        match = re.search(r'-(\d+)$', self.gown_id or '')
        return int(match.group(1)) if match else None

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._generate_unique_slug()
        super().save(*args, **kwargs)

    def _generate_unique_slug(self):
        from django.utils.text import slugify
        # Never collide with the placeholder catalog's 8 fixed demo slugs (valencia-lace,
        # archive-satin, ...) -- product_detail tries a real Gown first, so a collision
        # would silently shadow one of the fake catalog's own product pages. These are a
        # small, rarely-edited constant rather than DB rows, so they're checked directly
        # here (skip_bare) instead of through GownSlugSequence.
        from gowns.context_processors import _SLUG_ORDER
        base = slugify(self.name) or slugify(self.gown_id) or 'gown'
        skip_bare = base in _SLUG_ORDER
        # Each base has its own counter, and the counters know nothing about each other --
        # yet two different names can land on the same slug: "White (2)" slugifies to
        # "white-2", which is also what the second gown named "White" is handed. Whichever
        # is created second would fail on the unique slug ("Couldn't save that gown"), so
        # look at the real table too and simply ask again -- reserve() never returns the
        # same string twice, so the next answer is a fresh one.
        for _ in range(50):
            slug = GownSlugSequence.reserve(base, skip_bare=skip_bare)
            if not Gown.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                return slug
        return f'{base}-{uuid.uuid4().hex[:8]}'

    @classmethod
    def next_tracking_number(cls, category):
        """Next number for a new gown in this category -- ONE running count for the whole
        category, whatever the gown's color. The number in gown_id used to restart at 001
        for every new color, so a single category ended up with several parallel little
        sequences (Blue-001, Gold-001, White-001, White-002...) and a mixed list of them
        read as noise. Now 001 exists once per category and every later number is higher,
        so "the Nth Wedding Gown ever added" is always what the number says. The color code
        still rides along in the gown_id text for a quick read.

        Delegates the actual number to GownSequence, which hands out each integer under
        a row lock -- see that model's docstring. A number, once handed out, is never
        handed out again -- deleting a gown retires its number for good.
        """
        return GownSequence.next_value_for(category)


def pick_representative_gown(units):
    """The one unit whose photo/price stand in for a product with several physical
    units sharing a name. Lowest price wins (ties broken by id, for a stable pick) --
    a customer can never be shown a lower price than any real unit actually costs, or
    be quoted one thing and charged another.

    `units` must be non-empty; every caller has already filtered to bookable
    (non-Out-of-Stock) units before calling this."""
    return min(units, key=lambda g: (g.rental_price, g.id))


def group_gowns_by_name(units):
    """Groups Gown rows into one list per distinct name (case-insensitive, matching
    the same identity `gowns.views._find_available_unit` already books against),
    preserving the order names first appear in `units`. This is what turns "3
    physical dresses" into "1 product with 3 units" everywhere customers browse: the
    collection grid, the search overlay, the product page's sibling count, and "You
    may also like" -- shared here (not duplicated per caller) so a future fix to how
    grouping works only ever needs to happen once."""
    groups: dict[str, list] = {}
    order: list[str] = []
    for g in units:
        key = g.name.strip().lower()
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(g)
    return [groups[key] for key in order]


class GownSequence(models.Model):
    """The next tracking number to hand out for a category -- the same fix as
    reservations.models.ReservationSequence, applied to Gown.gown_id instead of
    Reservation.reference_code. See that model's docstring for the full reasoning; the
    short version:

    "Read the highest existing gown_id in this group, add one" (the old approach)
    reads, then separately writes, with nothing stopping two staff adding a gown to
    the same category at the same moment from both reading the same "last" row before
    either has written. `next_value_for()` closes that with a real Postgres row lock
    (`select_for_update()`): a second request asking for the same category does not
    race the first, it waits its turn and then reads the value the first one left
    behind. No number of simultaneous requests can defeat that.

    `get_or_create` covers the one thing a row lock cannot protect -- a row that does
    not exist yet, for the very first gown ever added to a category -- by catching the
    unique-constraint violation from two requests both creating that row for the first
    time and re-fetching the winner's row, which is standard, well-tested Django
    behaviour, not something left to chance here.

    ONE COUNTER PER CATEGORY. It used to be one per (category, color_code), which made
    the number restart at 001 for every new color. The category-wide counter is the
    row whose `color_code` is '' (CATEGORY_WIDE). The old per-color rows are left in
    place and simply no longer read: dropping the column would break the still-deployed
    code that shares this database until it's updated, and they double as the record of
    which numbers past gowns already used (see `_highest_used`).
    """

    CATEGORY_WIDE = ''

    category = models.CharField(max_length=20)
    color_code = models.CharField(max_length=2, blank=True)
    next_value = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['category', 'color_code'], name='unique_gown_sequence_group'),
        ]

    def __str__(self):
        scope = self.color_code or 'all colors'
        return f'{self.category} ({scope}): next is {self.next_value}'

    @classmethod
    def _highest_used(cls, category):
        """The highest tracking number this category has EVER handed out, as far as the
        database can tell: the numbers on gowns that still exist, plus the old per-color
        counters and the Removal Log (which both remember numbers used by gowns since
        deleted). Seeds a new category-wide counter so it starts above every number
        already spoken for -- otherwise the very first new gown could be issued a number
        an existing (or long deleted) gown already carries."""
        highest = 0
        for gown_id in Gown.objects.filter(category=category).values_list('gown_id', flat=True):
            match = re.search(r'-(\d+)$', gown_id or '')
            if match:
                highest = max(highest, int(match.group(1)))
        for used_up_to in (
            cls.objects.filter(category=category).exclude(color_code=cls.CATEGORY_WIDE)
            .values_list('next_value', flat=True)
        ):
            highest = max(highest, used_up_to - 1)
        # ...and the Removal Log, which remembers the number of every gown removed since,
        # even when the category has no gowns (or no legacy counter) left to remember it.
        logged = GownRemoval.objects.filter(category=category).aggregate(m=models.Max('tracking_number'))['m']
        if logged:
            highest = max(highest, logged)
        return highest

    @classmethod
    def next_value_for(cls, category):
        with transaction.atomic():
            row, created = cls.objects.get_or_create(category=category, color_code=cls.CATEGORY_WIDE)
            # Locks THIS row until this transaction commits -- any other request
            # asking for the same category blocks here rather than racing.
            row = cls.objects.select_for_update().get(pk=row.pk)
            if created:
                # Only the one request that actually created the row gets here; anyone
                # else was blocked on the insert and sees the seeded value below.
                row.next_value = cls._highest_used(category) + 1
            value = row.next_value
            row.next_value = value + 1
            row.save(update_fields=['next_value'])
        return value


class GownSlugSequence(models.Model):
    """One row per base slug text (a gown's name, slugified, with any numeric suffix
    already stripped off), holding the next numeric suffix to try if that exact base
    is ever needed again -- the same row-locked-counter fix as GownSequence, applied
    to Gown.slug instead of Gown.gown_id.

    Slugs are derived from free-text (the gown's name) rather than a plain numeric
    sequence, but the race is identical in shape: "does anything already use this
    exact string?" is a check, then a separate write, with no lock -- two staff
    adding two gowns with the exact same name at the exact same moment could both see
    "no" and both try to save the same slug.

    `reserve()`'s very first call for a brand new base returns the bare text with no
    suffix at all -- that is what a human expects the first gown named "White Wedding
    Gown" to be called ("white-wedding-gown", not "white-wedding-gown-1"). Everything
    after that gets "-2", "-3", and so on. Postgres itself provides the safety for
    that very first moment, with no extra locking needed: two concurrent callers
    racing to INSERT the same brand-new `base` cannot both succeed -- the second
    blocks until the first's transaction resolves, then either takes the locked
    "not first" path below (if the first committed) or becomes "first" itself (if the
    first rolled back) -- so only one caller in the world is ever told "you're first".
    """

    base = models.CharField(max_length=160, unique=True)
    next_suffix = models.PositiveIntegerField(default=2)

    def __str__(self):
        return f'{self.base}: next is -{self.next_suffix}'

    @classmethod
    def reserve(cls, base, *, skip_bare=False):
        """Returns a slug reserved for the caller alone -- no other concurrent caller
        asking for this same base can ever receive the same string back.

        `skip_bare=True` is for a base that is already permanently spoken for by
        something this table doesn't track (the placeholder catalog's fixed demo
        slugs): it forces straight past the "first ever caller gets the bare text"
        shortcut and into the locked, always-suffixed path below.
        """
        with transaction.atomic():
            _, created = cls.objects.get_or_create(base=base, defaults={'next_suffix': 2})
            if created and not skip_bare:
                return base
            # Locks THIS row until this transaction commits -- any other request
            # asking for the same base blocks here rather than racing.
            row = cls.objects.select_for_update().get(base=base)
            suffix = row.next_suffix
            row.next_suffix = suffix + 1
            row.save(update_fields=['next_suffix'])
            return f'{base}-{suffix}'


class GownUnavailability(models.Model):
    """A date range where one physical gown unit is off the rental pool for a reason
    other than a customer booking -- cleaning, repair, alterations.

    Why this exists: marking a ReservationItem 'Returned' frees the gown for new
    bookings the same instant, but a real returned gown usually needs a few days
    before it can go out again (it has to be washed, or it came back damaged). Staff
    block those days here, from the gown's own row in the Gown Catalog.

    Customer-facing availability (gowns.views._blocked_dates_for_category) treats a
    blocked unit exactly like a booked one: it comes out of that date's capacity. So
    a category with 3 gowns where 1 is blocked for cleaning can still take 2 bookings
    that day -- the date only greys out on the customer calendar once every unit is
    spoken for. Deleting the row hands the days back immediately."""

    class Reason(models.TextChoices):
        CLEANING = 'Cleaning', 'Cleaning'
        REPAIR = 'Repair', 'Repair'
        ALTERATION = 'Alteration', 'Alteration'
        COOLDOWN = 'Cooldown', 'Cooldown'
        OTHER = 'Other', 'Other'

    gown = models.ForeignKey(
        Gown, on_delete=models.CASCADE, related_name='unavailabilities'
    )
    start_date = models.DateField()
    end_date = models.DateField()  # inclusive -- the gown is unavailable ON this day too
    reason = models.CharField(max_length=20, choices=Reason.choices, default=Reason.CLEANING)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # The ReservationItem this block was auto-created for, if it was the system that
    # created it (reason=COOLDOWN) rather than a staff member typing in a manual
    # cleaning/repair range. Lets reschedule/mark-returned find and resync "their"
    # block, without ever recreating one a staff member already deleted on purpose.
    # String reference: reservations.models already imports Gown from this module,
    # so importing ReservationItem here directly would be a circular import.
    auto_for_item = models.OneToOneField(
        'reservations.ReservationItem', null=True, blank=True,
        on_delete=models.CASCADE, related_name='auto_cooldown_block',
    )

    class Meta:
        ordering = ['start_date', 'id']
        verbose_name = 'gown unavailability'
        verbose_name_plural = 'gown unavailabilities'

    def __str__(self):
        return f'{self.gown.gown_id} unavailable {self.start_date} → {self.end_date} ({self.reason})'

    @property
    def covers_today(self):
        today = timezone.localdate()
        return self.start_date <= today <= self.end_date


class GownRemoval(models.Model):
    """A permanent record that a gown was removed from the catalog, and why.

    Deleting a Gown is a hard delete -- the row is gone -- so without this there was no
    way to find out afterwards why a number in the sequence (say 003) no longer exists:
    damaged and thrown out, sold, or simply vanished. Every removal now writes one of
    these first, copying enough of the gown (ID, name, category, color, size, photo) that
    the log still makes sense once the gown itself is gone. That is what separates a
    gap that is ACCOUNTED FOR (a row here says why) from one that is not -- the second
    kind being the real red flag.

    Gown IDs are never reused, so one ID is never expected to appear here twice.
    `removed_at` is null for the handful of removals that happened before this log
    existed: the number is known to be missing, the date and reason are not, and the
    log says exactly that rather than inventing them."""

    class Reason(models.TextChoices):
        DAMAGED = 'Damaged', 'Damaged beyond repair'
        LOST_STOLEN = 'Lost or stolen', 'Lost or stolen'
        RETIRED = 'Retired', 'Retired (sold or no longer offered)'
        OTHER = 'Other', 'Other'

    gown_id = models.CharField(max_length=40, db_index=True)
    tracking_number = models.PositiveIntegerField(null=True, blank=True)
    name = models.CharField(max_length=150, blank=True)
    category = models.CharField(max_length=20, blank=True)
    color_name = models.CharField(max_length=40, blank=True)
    size = models.CharField(max_length=20, blank=True)
    photo_url = models.URLField(blank=True)
    reason = models.CharField(max_length=20, choices=Reason.choices)
    note = models.CharField(max_length=300, blank=True)
    removed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    # A snapshot of the name, so the log keeps saying who did it even if that staff
    # account is later renamed or deleted (removed_by would go null).
    removed_by_name = models.CharField(max_length=150, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Newest first; the undated historical rows sink to the bottom rather than
        # floating to the top the way Postgres sorts NULLs in a descending order.
        ordering = [models.F('removed_at').desc(nulls_last=True), '-id']

    def __str__(self):
        return f'{self.gown_id} removed ({self.reason})'


# A category the owner adds without a developer gets this tag colour until they pick another.
CUSTOM_CATEGORY_DEFAULT_TAG_COLOR = 'Gray'


class CustomCategory(models.Model):
    """A rental category the owner added from Gown Catalog -> Add Category. The 13 original
    categories live in code (Gown.Category); these sit beside them and are treated the same
    everywhere: a gown stores the NAME in Gown.category, the customer site gets a collection
    page at /collections/<slug>/, and the owner can give it a tag colour.

    `name` is capped at 20 characters because that is Gown.category's column size; `slug` is
    its URL key and is checked against the built-in keys when the category is created."""

    class Audience(models.TextChoices):
        WOMEN = 'women', "Women's collection"
        MEN = 'men', "Men's collection"

    name = models.CharField(max_length=20, unique=True)
    slug = models.SlugField(max_length=40, unique=True)
    # Which of the customer site's two collection pages (Women's / Men's) lists it. db_default so
    # code that doesn't know this column yet can still insert a row.
    audience = models.CharField(
        max_length=5, choices=Audience.choices, default=Audience.WOMEN, db_default='women',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']

    def __str__(self):
        return self.name


class HiddenCategory(models.Model):
    """A built-in category (one of Gown.Category) the owner removed from Gown Catalog. The 13
    original categories live in code and can't really be deleted, so removing one just hides it
    everywhere -- Add Gown, the catalog, the customer site, its page (404) -- and only while no
    gown is filed under it. Adding a category with the same name brings it back (the row goes)."""

    name = models.CharField(max_length=20, unique=True)  # the built-in's name, e.g. 'Wedding Gown'
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.name} (hidden)'


class CategoryCover(models.Model):
    """The picture the owner chose for a category's tile on the customer site (Admin -> Categories).

    One row per category, keyed the way the category's page is ("long-gown", or the slug of one the
    owner added). No row means the category shows the picture bundled with the site -- or the plain
    placeholder when it has none -- so deleting the row is exactly what "Reset" does."""

    key = models.CharField(max_length=40, unique=True)
    image_url = models.URLField(max_length=500)
    # The name the storage gave the file when it saved it -- exactly what it needs to delete that file later
    # (on Cloudinary it is not the same as the address in image_url).
    storage_name = models.CharField(max_length=300, blank=True, default='', db_default='')
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.key} cover'


def custom_categories():
    """The owner's own categories, oldest first. Asked once per page visit (gowns/request_memo.py); callers get their own list."""
    from gowns.request_memo import remember
    return list(remember("custom_categories", lambda: list(CustomCategory.objects.all())))


def custom_category_names():
    return [category.name for category in custom_categories()]


def hidden_category_names():
    """The built-in categories the owner removed. Asked once per page visit (gowns/request_memo.py); callers get their own set."""
    from gowns.request_memo import remember
    return set(remember("hidden_category_names", lambda: set(HiddenCategory.objects.values_list('name', flat=True))))


def visible_builtin_names():
    """The built-in categories the owner hasn't removed, in their usual order."""
    hidden = hidden_category_names()
    return [name for name in Gown.Category.values if name not in hidden]


def all_category_names():
    """Every category a gown can be filed under: the built-in ones that are still in use, then
    the owner's own."""
    return visible_builtin_names() + custom_category_names()


class SiteSettings(models.Model):
    """Singleton (always pk=1) holding the business's public contact info -- the
    admin Edit Profile page and every customer-facing template (footer, Contact
    page) both read/write this same row, so there's one source of truth instead
    of the same Facebook link hardcoded independently in five templates. Field
    defaults are today's real, already-live values, so the first `load()` call
    lazily seeds a row that matches the current site exactly -- no visual change
    on deploy, no separate data migration needed."""

    facebook_url = models.URLField(blank=True, default='https://www.facebook.com/share/1EHAS1iemQ/')
    phone = models.CharField(max_length=20, blank=True, default='09635215485')
    shop_street = models.CharField(max_length=150, blank=True, default='3rd Flr Park Place Bldg Centre')
    shop_city = models.CharField(max_length=100, blank=True, default='Antipolo')
    shop_country = models.CharField(max_length=100, blank=True, default='Philippines')
    shop_postal_code = models.CharField(max_length=10, blank=True, default='1830')
    # Where customers pay the security deposit (shown in the checkout's GCash window). Set by
    # the owner in Edit Profile; empty until then. db_default keeps a database-level default,
    # so code that doesn't know these columns yet (an older deployment sharing this database)
    # can still insert a row.
    gcash_account_name = models.CharField(max_length=100, blank=True, default='', db_default='')
    gcash_number = models.CharField(max_length=20, blank=True, default='', db_default='')
    gcash_qr_url = models.URLField(max_length=500, blank=True, default='', db_default='')
    # Only the tag colors the owner has CHANGED from the defaults, keyed by category --
    # see resolve_tag_colors(), which layers these over DEFAULT_CATEGORY_TAG_COLORS.
    # Storing overrides (not a full copy) means a category added later just picks up
    # its default instead of showing up blank.
    category_tag_colors = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return 'Site settings'

    def tag_colors(self):
        """{category: tag color name} for every category, defaults filled in -- the built-in
        ones and any the owner added."""
        resolved = resolve_tag_colors(self.category_tag_colors)
        saved = self.category_tag_colors if isinstance(self.category_tag_colors, dict) else {}
        for name in custom_category_names():
            chosen = saved.get(name)
            resolved[name] = chosen if chosen in TAG_COLOR_HEX else CUSTOM_CATEGORY_DEFAULT_TAG_COLOR
        return resolved

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
