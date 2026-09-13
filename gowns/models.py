from django.db import models, transaction
from django.utils import timezone


class Gown(models.Model):
    class Category(models.TextChoices):
        WEDDING_GOWN = 'Wedding Gown', 'Wedding Gown'
        BALL_GOWN = 'Ball Gown', 'Ball Gown'
        SEXY_GOWN = 'Sexy Gown', 'Sexy Gown'
        NINANG_GOWN = 'Ninang Gown', 'Ninang Gown'
        SUIT = 'Suit', 'Suit'
        FILIPINIANA = 'Filipiniana', 'Filipiniana'
        GUEST_GOWN = 'Guest Gown', 'Guest Gown'
        FLOWER_GIRL = 'Flower Girl', 'Flower Girl'
        BELO = 'Belo', 'Belo'
        THAILAND_GOWN = 'Thailand Gown', 'Thailand Gown'
        DRESSES = 'Dresses', 'Dresses'

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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category', 'color_code', 'gown_id']

    def __str__(self):
        return f'{self.gown_id} — {self.name}'

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
        return GownSlugSequence.reserve(base, skip_bare=base in _SLUG_ORDER)

    @classmethod
    def next_tracking_number(cls, category, color_code):
        """Next 3-digit sequence within the same category+color group — automates the
        seeding plan's manual 'check the sheet for the next available tracking number' step.

        Delegates the actual number to GownSequence, which hands out each integer under
        a row lock -- see that model's docstring. This used to read the highest existing
        gown_id in the group and add one: the same shape of race that
        reservations.models.ReservationSequence replaced for Reservation.reference_code,
        after that approach was proven -- with real concurrent threads against live
        Postgres -- to fail under load. Same problem here, same fix.
        """
        return GownSequence.next_value_for(category, color_code)


class GownSequence(models.Model):
    """One row per (category, color_code) group, holding the next tracking number to
    hand out for it -- the same fix as reservations.models.ReservationSequence,
    applied to Gown.gown_id instead of Reservation.reference_code. See that model's
    docstring for the full reasoning; the short version:

    "Read the highest existing gown_id in this group, add one" (the old approach)
    reads, then separately writes, with nothing stopping two staff adding a gown to
    the same category+color at the same moment from both reading the same "last" row
    before either has written. `next_value_for()` closes that with a real Postgres row
    lock (`select_for_update()`): a second request asking for the same group does not
    race the first, it waits its turn and then reads the value the first one left
    behind. No number of simultaneous requests can defeat that.

    `get_or_create` covers the one thing a row lock cannot protect -- a row that does
    not exist yet, for the very first gown ever added to a given category+color -- by
    catching the unique-constraint violation from two requests both creating that row
    for the first time and re-fetching the winner's row, which is standard, well-
    tested Django behaviour, not something left to chance here.
    """

    category = models.CharField(max_length=20)
    color_code = models.CharField(max_length=2)
    next_value = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['category', 'color_code'], name='unique_gown_sequence_group'),
        ]

    def __str__(self):
        return f'{self.category}-{self.color_code}: next is {self.next_value}'

    @classmethod
    def next_value_for(cls, category, color_code):
        with transaction.atomic():
            cls.objects.get_or_create(category=category, color_code=color_code)
            # Locks THIS row until this transaction commits -- any other request
            # asking for the same category+color group blocks here rather than racing.
            row = cls.objects.select_for_update().get(category=category, color_code=color_code)
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
        OTHER = 'Other', 'Other'

    gown = models.ForeignKey(
        Gown, on_delete=models.CASCADE, related_name='unavailabilities'
    )
    start_date = models.DateField()
    end_date = models.DateField()  # inclusive -- the gown is unavailable ON this day too
    reason = models.CharField(max_length=20, choices=Reason.choices, default=Reason.CLEANING)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

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
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return 'Site settings'

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
