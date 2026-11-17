"""Developer tool: run the receipt pictures in a folder through the REAL receipt check and print what was read and what was decided.

    python manage.py check_receipts "C:/path/to/folder" --shop-number 09488745992 --shop-name "DE****K C." --deposit 1 --fresh

Nothing is saved or changed. It pretends the shop's GCash number / account name are the ones given (so the live settings are never touched) and
only READS the reservations table (to look for a reused reference). Each picture goes through the same cleaning as a customer's upload, the same
first reading, and -- when the first reading would refuse it or cannot read it -- the same second reading and reconciliation as the site.
The reader is Google Gemini, so this sends the pictures to Google exactly as the site does (it needs GEMINI_API_KEY; use the isolated test
settings to be sure no live database is involved).

--fresh judges each receipt as if it had been uploaded 5 minutes after the payment time printed on it (an old test receipt would otherwise be
refused as too old).
"""
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from gowns import gcash_numbers, proof_image, receipt_check as rc, receipt_quality
from gowns.models import SiteSettings

PICTURES = {".png", ".jpg", ".jpeg", ".webp"}


class Command(BaseCommand):
    help = "Run receipt pictures from a folder through the real receipt check (nothing is saved)."

    def add_arguments(self, parser):
        parser.add_argument("folder")
        parser.add_argument("--shop-number", default="", help="pretend this is the shop's GCash number (11 digits)")
        parser.add_argument("--shop-name", default="", help="pretend this is the shop's GCash account name")
        parser.add_argument("--deposit", default="2000", help="the deposit the booking would need (pesos)")
        parser.add_argument("--fresh", action="store_true", help="judge each receipt as if uploaded 5 minutes after its own payment time")
        parser.add_argument("--only", default="", help="only pictures whose file name contains this text")
        parser.add_argument("--read-live", action="store_true", help="allow this to run against a non-SQLite (live) database; it only reads")

    def handle(self, *args, **options):
        engine = settings.DATABASES["default"]["ENGINE"]
        if "sqlite" not in engine and not options["read_live"]:
            raise CommandError("This is not the isolated test database. Use the test settings, or pass --read-live (it only reads).")
        folder = Path(options["folder"])
        if not folder.is_dir():
            raise CommandError(f"No such folder: {folder}")
        pictures = sorted(p for p in folder.iterdir() if p.suffix.lower() in PICTURES and options["only"] in p.name)
        if not pictures:
            raise CommandError("No pictures found.")
        shop_number = gcash_numbers.normalize(options["shop_number"])
        if options["shop_number"] and not shop_number:
            raise CommandError("--shop-number must be 11 digits starting with 09.")
        deposit = Decimal(options["deposit"])

        shop = SiteSettings(pk=1, gcash_number=shop_number, gcash_account_name=options["shop_name"])
        numbers = {shop_number} if shop_number else set()
        patches = [
            patch.object(SiteSettings, "load", classmethod(lambda cls: shop)),
            patch.object(gcash_numbers, "accepted_numbers_at", lambda moment=None: [(shop_number, False)] if shop_number else []),
            patch.object(gcash_numbers, "known_numbers", lambda: set(numbers)),
        ]
        for p in patches:
            p.start()
        try:
            for picture in pictures:
                self.run_one(picture, deposit, options["fresh"])
        finally:
            for p in patches:
                p.stop()

    def run_one(self, picture, deposit, fresh):
        data = picture.read_bytes()
        cleaned, error = proof_image.clean_proof_image(SimpleUploadedFile(picture.name, data))
        self.stdout.write(f"\n=== {picture.name}")
        if cleaned is None:
            self.stdout.write(f"    the upload cleaning refused it: {error}")
            return
        image = cleaned.read()
        quality = receipt_quality.picture_quality(image)
        self.stdout.write(f"    picture: {quality['width']}x{quality['height']}  sharpness {quality['sharpness']} ({quality['label']})  brightness {quality['brightness']}  poor={quality['poor']}")
        now = None
        started_at = None
        if fresh:
            mime = rc._mime_for(cleaned.name)
            first, why = rc.read_receipt(image, mime)
            if first is None or first.paid_at is None:
                self.stdout.write(f"    --fresh could not get a payment time from the first reading ({why or 'no date'}); using the real time")
            else:
                now = first.paid_at + timedelta(minutes=5)
                started_at = now - timedelta(minutes=20)
        began = time.monotonic()
        verdict = rc.check_receipt(image, cleaned.name, expected_amount=deposit, started_at=started_at, now=now)
        took = time.monotonic() - began
        reading = verdict.reading
        if reading is not None:
            self.stdout.write(
                f"    read: layout={reading.layout} picture={reading.picture} clarity={reading.clarity} app={reading.app} direction={reading.direction}\n"
                f"          recipient={reading.recipient_number or '-'} pattern={reading.recipient_pattern or '-'} name={reading.recipient_name or '-'}\n"
                f"          amount={reading.amount} ref={reading.reference or '-'} paid_at={reading.paid_at}")
        self.stdout.write(f"    VERDICT: {verdict.status.upper()}  codes={verdict.codes}  ({took:.1f}s)")
        if verdict.message:
            self.stdout.write(f"    customer sees: {verdict.message}")
        for note in verdict.notes:
            self.stdout.write(f"    note: {note}")
