from django.db import migrations

ALL_PERMISSIONS = "delete_gowns,cancel_bookings,flag_customers,delete_receipts"


def managers_become_staff(apps, schema_editor):
    """There is no Manager role any more. Every existing Manager becomes a Staff account that is allowed all four sensitive actions,
    so nobody loses anything the day this runs; the owner then trims each account in Staff Management."""
    UserProfile = apps.get_model("accounts", "UserProfile")
    UserProfile.objects.filter(role="Manager").update(role="Staff", permissions=ALL_PERMISSIONS)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0018_permissions_and_audit_log"),
    ]

    operations = [
        # Not reversible on purpose: after this nobody can tell which Staff accounts used to be Managers. Going back changes nothing
        # (the code still treats a leftover Manager row as having all four permissions).
        migrations.RunPython(managers_become_staff, migrations.RunPython.noop),
    ]
