"""python manage.py restore_database BACKUP_FILE

Puts a backup made by `backup_database` into a NEW, EMPTY database (one where `migrate` has just been run).

Safety first: it refuses to touch a database that already has accounts, gowns or reservations in it, so it can never be pointed at the
live data by mistake. Customers are never emailed during a restore."""
from pathlib import Path

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from accounts.management.commands.backup_database import Command as BackupCommand


class Command(BaseCommand):
    help = "Load a backup file into a new, empty (freshly migrated) database."

    def add_arguments(self, parser):
        parser.add_argument("backup_file", help="A file made by backup_database, e.g. arabela-backup-20261010-163908.json.gz")

    def handle(self, *args, **options):
        path = Path(options["backup_file"])
        if not path.is_file():
            raise CommandError(f"There is no file at {path}")

        from gowns.models import Gown
        from reservations.models import Reservation

        for label, has_data in (("accounts", get_user_model().objects.exists()), ("gowns", Gown.objects.exists()),
                                ("reservations", Reservation.objects.exists())):
            if has_data:
                raise CommandError(
                    f"This database already has {label} in it, so nothing was changed. A restore only goes into a brand-new, "
                    f"empty database (set the DATABASE_* settings to the new one, run `python manage.py migrate`, then restore)."
                )

        expected = BackupCommand.read_back(path)           # also proves the file can be read at all

        # `migrate` pre-fills a few tables (the default website entry, for one). The backup brings its own copy of every row in
        # the tables it holds, so clear what is already there first -- otherwise the same entry would clash with itself.
        for label in expected:
            model = apps.get_model(label)
            if model._default_manager.exists():
                self.stdout.write(f"  clearing {model._default_manager.count()} pre-filled row(s) in {label}")
                model._default_manager.all().delete()

        call_command("loaddata", str(path), verbosity=0)

        problems = []
        for label, count in sorted(expected.items()):
            actual = apps.get_model(label)._default_manager.count()
            if actual != count:
                problems.append(f"{label}: the file has {count}, the database now has {actual}")
        if problems:
            raise CommandError("The restore finished but these do not match: " + "; ".join(problems))
        self.stdout.write(self.style.SUCCESS(f"Restored {sum(expected.values())} records in {len(expected)} tables, all counts match the file."))
