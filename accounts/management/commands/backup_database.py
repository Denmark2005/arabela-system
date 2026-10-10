"""python manage.py backup_database [--output-dir DIR] [--keep N]

Writes one compressed, timestamped copy of all the shop's data (gowns, reservations, customers, receipts, settings...) to a
folder outside the project, then reads the file back to prove it is complete, and removes copies beyond the newest N.

It only READS the database, so it is safe to run any time, against the live database too. To put a backup back, see the
"If the database is lost" section of the README (new empty database -> migrate -> loaddata)."""
import gzip
import json
import os
import re
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

# Left out on purpose: they are rebuilt by `migrate` (content types, permissions), are only the current logins (sessions), the
# admin's own click log, or are temporary (failed-login counters would re-lock people when restored).
EXCLUDED = ["contenttypes", "auth.permission", "sessions", "admin.logentry", "accounts.loginthrottle"]
FILE_PATTERN = re.compile(r"^arabela-backup-\d{8}-\d{6}\.json\.gz$")


def default_folder() -> Path:
    return Path(os.environ.get("ARABELA_BACKUP_DIR") or (Path.home() / "ArabelaBackups"))


class Command(BaseCommand):
    help = "Save a compressed copy of all the data to a folder, check it, and keep only the newest copies."

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", default=None, help="Where to save (default: ARABELA_BACKUP_DIR, else ~/ArabelaBackups).")
        parser.add_argument("--keep", type=int, default=30, help="How many backups to keep (default 30). Older ones are deleted.")

    def handle(self, *args, **options):
        folder = Path(options["output_dir"]) if options["output_dir"] else default_folder()
        keep = options["keep"]
        if keep < 1:
            raise CommandError("--keep must be at least 1.")
        folder.mkdir(parents=True, exist_ok=True)
        name = f"arabela-backup-{datetime.now():%Y%m%d-%H%M%S}.json.gz"
        final = folder / name

        # Write to a temporary file first, so a half-written backup can never be mistaken for a good one.
        handle, temp_name = tempfile.mkstemp(prefix=".writing-", suffix=".gz", dir=folder)
        os.close(handle)
        temp = Path(temp_name)
        try:
            with gzip.open(temp, "wt", encoding="utf-8") as out:
                call_command("dumpdata", natural_foreign=True, exclude=EXCLUDED, stdout=out, verbosity=0)
            counts = self.read_back(temp)
            temp.replace(final)
        except Exception as exc:
            temp.unlink(missing_ok=True)
            raise CommandError(f"The backup failed and nothing was saved: {exc}") from exc

        self.remove_old(folder, keep)
        size_mb = final.stat().st_size / 1_000_000
        self.stdout.write(self.style.SUCCESS(f"Backup saved: {final}"))
        self.stdout.write(f"  {sum(counts.values())} records in {len(counts)} tables, {size_mb:.2f} MB (compressed)")
        for label, number in sorted(counts.items(), key=lambda item: -item[1])[:8]:
            self.stdout.write(f"    {label:38} {number}")

    @staticmethod
    def read_back(path: Path) -> Counter:
        """Opens the finished file and counts what is in it -- a backup that cannot be read back is not a backup."""
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            records = json.load(fh)
        if not isinstance(records, list) or not records:
            raise CommandError("the backup file came out empty")
        counts = Counter(record["model"] for record in records)
        if "auth.user" not in counts:
            raise CommandError("the backup has no user accounts in it, so something went wrong")
        return counts

    def remove_old(self, folder: Path, keep: int):
        mine = sorted((p for p in folder.iterdir() if p.is_file() and FILE_PATTERN.match(p.name)), key=lambda p: p.name, reverse=True)
        for old in mine[keep:]:
            old.unlink()
            self.stdout.write(f"  removed old backup {old.name}")
