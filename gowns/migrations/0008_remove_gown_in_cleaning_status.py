from django.db import migrations, models


def convert_in_cleaning_to_out_of_stock(apps, schema_editor):
    """The retired 'In-Cleaning' status becomes 'Out-of-Stock' -- the conservative
    choice: the gown stays withdrawn from availability until staff explicitly bring
    it back (Mark Available) or schedule a dated Blocked Dates range. A scheduled
    cleaning/repair gap with a known end date is what GownUnavailability (Blocked
    Dates) is for, and it already blocks the customer calendar and auto-expires."""
    Gown = apps.get_model("gowns", "Gown")
    Gown.objects.filter(status="In-Cleaning").update(status="Out-of-Stock")


class Migration(migrations.Migration):

    dependencies = [
        ("gowns", "0007_gown_slug"),
    ]

    operations = [
        # Data first: no row may still hold 'In-Cleaning' once the choices tighten.
        # Reverse is a safe no-op -- we can't know which of the merged rows were
        # originally In-Cleaning, and leaving them Out-of-Stock is harmless.
        migrations.RunPython(
            convert_in_cleaning_to_out_of_stock,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="gown",
            name="status",
            field=models.CharField(
                choices=[
                    ("Available", "Available"),
                    ("Reserved", "Reserved"),
                    ("Out-of-Stock", "Out-of-Stock"),
                ],
                default="Available",
                max_length=20,
            ),
        ),
    ]
