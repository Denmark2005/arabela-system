"""python manage.py cleanup_id_photos [--dry-run]

Deletes the pictures of valid IDs that are due to go: 30 days after the customer's last return (or, for a check with no booking, 30 days after it was verified).
It also tries again the pictures that could not be copied from the verification service the first time.
The shop's own pages already do this by themselves (the dashboard and the ID Checks page, at most once an hour), so this command is only for running it on a clock
from a scheduled job. It is safe to run at any time, any number of times. --dry-run only counts what would be deleted."""
from django.core.management.base import BaseCommand
from django.utils import timezone

from reservations import identity
from reservations.models import IdentityCheck


class Command(BaseCommand):
    help = "Delete the ID pictures that are due (30 days after the last return) and retry the ones that failed to copy."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Only count the pictures that are due; delete nothing.")

    def handle(self, *args, **options):
        if options["dry_run"]:
            now = timezone.now()
            due = sum(1 for check in IdentityCheck.objects.filter(photos_state__in=("stored", "failed", "pending", "copying")).order_by("id")
                      if identity.photos_due(check, now))
            self.stdout.write(f"{due} ID check(s) have pictures that are due to be deleted. Nothing was deleted (--dry-run).")
            return
        deleted, retried = identity.cleanup_photos()
        self.stdout.write(self.style.SUCCESS(f"Deleted the pictures of {deleted} ID check(s); copied again for {retried}."))
