from django.db import models
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
        IN_CLEANING = 'In-Cleaning', 'In-Cleaning'
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
        # would silently shadow one of the fake catalog's own product pages.
        from gowns.context_processors import _SLUG_ORDER
        base = slugify(self.name) or slugify(self.gown_id) or 'gown'
        candidate, n = base, 2
        while candidate in _SLUG_ORDER or Gown.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
            candidate = f'{base}-{n}'
            n += 1
        return candidate

    @classmethod
    def next_tracking_number(cls, category, color_code):
        """Next 3-digit sequence within the same category+color group — automates the
        seeding plan's manual 'check the sheet for the next available tracking number' step."""
        last = (cls.objects.filter(category=category, color_code=color_code)
                            .order_by('-id').first())
        if not last:
            return 1
        try:
            return int(last.gown_id.split('-')[2]) + 1
        except (IndexError, ValueError):
            return cls.objects.filter(category=category, color_code=color_code).count() + 1


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
