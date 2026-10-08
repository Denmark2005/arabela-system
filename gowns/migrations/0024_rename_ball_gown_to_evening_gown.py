"""Ball Gown -> Evening Gown, in the database.

The category's NAME is stored as text in several places, so renaming it in the code alone would leave every
existing Ball Gown gown pointing at a category that no longer exists. This moves all of it together, in one
transaction (so it either all changes or none of it does):

  * every gown in the category: its category, its ID prefix ("Ball Gown-BU-012" -> "Evening Gown-BU-012") and its
    name when the name starts with the category ("Ball Gown 79" -> "Evening Gown 79");
  * the number counters, so the next gown continues from where Ball Gown stopped;
  * the Removal Log entries, so the "this number was removed, and why" check still lines up;
  * the "removed category" list, the owner's saved tag colour, and the owner's uploaded category picture.

Never touched: "Ball Gown Tulle" (a different category), gown web addresses (slugs), and what customers and staff
wrote in the past (messages, notes). The names on existing reservations are renamed by reservations/0023.

It stops with a clear message, before changing anything, if the owner already added a category called
"Evening Gown" of their own -- two categories can't share a name. Reversible.
"""
from django.db import migrations, models

OLD_LABEL, NEW_LABEL = "Ball Gown", "Evening Gown"
OLD_KEY, NEW_KEY = "ball-gown", "evening-gown"


def swap_prefix(text, old, new):
    """'Ball Gown 79' -> 'Evening Gown 79'. Only a name that STARTS with the old name changes: 'Ball Gown Tulle 51'
    (another category), 'Ball Gowns' (another word) and 'Emerald Ball Gown' are left exactly as they are."""
    if not text or not text.startswith(old):
        return text
    rest = text[len(old):]
    if rest[:1].isalpha():
        return text
    if rest.lstrip().lower().startswith("tulle"):
        return text
    return new + rest


def swap_gown_id(gown_id, old, new):
    """'Ball Gown-BU-012' -> 'Evening Gown-BU-012' (the part after the category stays, so the number on the tag is the same)."""
    if gown_id and gown_id.startswith(old + "-"):
        return new + gown_id[len(old):]
    return gown_id


def rename_category(apps, old_label, new_label, old_key, new_key):
    Gown = apps.get_model("gowns", "Gown")
    GownSequence = apps.get_model("gowns", "GownSequence")
    GownRemoval = apps.get_model("gowns", "GownRemoval")
    HiddenCategory = apps.get_model("gowns", "HiddenCategory")
    CustomCategory = apps.get_model("gowns", "CustomCategory")
    SiteSettings = apps.get_model("gowns", "SiteSettings")
    CategoryCover = apps.get_model("gowns", "CategoryCover")

    if CustomCategory.objects.filter(models.Q(name__iexact=new_label) | models.Q(slug=new_key)).exists():
        raise RuntimeError(
            f'Cannot rename "{old_label}" to "{new_label}": a category called "{new_label}" was already added '
            f"from Gown Catalog. Remove or rename that one first, then run the migration again."
        )

    # Everything is changed with .update() on purpose: no "last updated" stamps move, and nothing is re-saved
    # through code that could do more than a rename.
    for pk, name, gown_id in list(Gown.objects.filter(category=old_label).values_list("id", "name", "gown_id")):
        Gown.objects.filter(pk=pk).update(
            category=new_label,
            name=swap_prefix(name, old_label, new_label)[:150],
            gown_id=swap_gown_id(gown_id, old_label, new_label),
        )

    for row in list(GownSequence.objects.filter(category=old_label)):
        twin = GownSequence.objects.filter(category=new_label, color_code=row.color_code).first()
        if twin is None:
            GownSequence.objects.filter(pk=row.pk).update(category=new_label)
        else:  # never hand out a number twice: keep the higher of the two counters
            GownSequence.objects.filter(pk=twin.pk).update(next_value=max(twin.next_value, row.next_value))
            row.delete()

    for pk, name, gown_id in list(GownRemoval.objects.filter(category=old_label).values_list("id", "name", "gown_id")):
        GownRemoval.objects.filter(pk=pk).update(
            category=new_label,
            name=swap_prefix(name, old_label, new_label)[:150],
            gown_id=swap_gown_id(gown_id, old_label, new_label),
        )

    for row in list(HiddenCategory.objects.filter(name=old_label)):
        if HiddenCategory.objects.filter(name=new_label).exists():
            row.delete()
        else:
            HiddenCategory.objects.filter(pk=row.pk).update(name=new_label)

    for site in list(SiteSettings.objects.all()):
        colors = site.category_tag_colors
        if isinstance(colors, dict) and old_label in colors:
            colors = dict(colors)
            chosen = colors.pop(old_label)
            colors.setdefault(new_label, chosen)
            SiteSettings.objects.filter(pk=site.pk).update(category_tag_colors=colors)

    for cover in list(CategoryCover.objects.filter(key=old_key)):
        if CategoryCover.objects.filter(key=new_key).exists():
            cover.delete()
        else:
            CategoryCover.objects.filter(pk=cover.pk).update(key=new_key)


def forwards(apps, schema_editor):
    rename_category(apps, OLD_LABEL, NEW_LABEL, OLD_KEY, NEW_KEY)


def backwards(apps, schema_editor):
    rename_category(apps, NEW_LABEL, OLD_LABEL, NEW_KEY, OLD_KEY)


class Migration(migrations.Migration):

    dependencies = [
        ('gowns', '0023_category_cover'),
    ]

    operations = [
        migrations.AlterField(
            model_name='gown',
            name='category',
            field=models.CharField(choices=[('Wedding Gown', 'Wedding Gown'), ('Evening Gown', 'Evening Gown'), ('Long Gown', 'Long Gown'), ('Luxury Gown', 'Luxury Gown'), ('Mother Gown', 'Mother Gown'), ('Suit', 'Suit'), ('Filipiniana', 'Filipiniana'), ('Guest Gown', 'Guest Gown'), ('Dresses', 'Dresses'), ('Kids Gown', 'Kids Gown'), ('Barong', 'Barong'), ('Ball Gown Tulle', 'Ball Gown Tulle'), ('Bridesmaid Dresses', 'Bridesmaid Dresses')], max_length=20),
        ),
        migrations.RunPython(forwards, backwards),
    ]
