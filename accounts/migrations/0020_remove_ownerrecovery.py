from django.db import migrations


class Migration(migrations.Migration):
    """The admin panel signs in with Google only, so the owner's password-reset recovery email (accounts/owner_recovery.py, now deleted) has
    no use. Drops its table. Nothing else reads it: the code that used it was removed first."""

    dependencies = [
        ("accounts", "0019_managers_become_staff"),
    ]

    operations = [
        migrations.DeleteModel(name="OwnerRecovery"),
    ]
