"""`manage.py backup_database`: a compressed copy of all the data that can be read back, leaves out what `migrate` rebuilds,
keeps only the newest few, saves nothing if it fails -- and loading one back (the restore) puts the same data back without
emailing a single customer."""
import gzip
import io
import json
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.management.commands.backup_database import FILE_PATTERN, default_folder
from accounts.models import CustomerMessage, LoginThrottle, UserProfile
from gowns.models import Gown
from reservations.models import Reservation, ReservationItem

User = get_user_model()


def millisecond(moment):
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


class BackupTestBase(TestCase):
    """The shop data and helpers both groups of tests use (it has no tests of its own)."""

    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(username="backup_customer", password="x" * 12, email="c@example.test")
        UserProfile.objects.get_or_create(user=cls.customer)
        cls.gown = Gown.objects.create(
            gown_id="BKP-0001", name="Backup Gown", category="Wedding Gown", color_name="White", color_code="WH",
            size=Gown.Size.MEDIUM, rental_price=Decimal("20000"), status=Gown.Status.RESERVED)
        cls.reservation = Reservation.objects.create(
            customer=cls.customer, customer_name="Backup Customer", status=Reservation.Status.CONFIRMED)
        today = date.today()
        ReservationItem.objects.create(
            reservation=cls.reservation, gown=cls.gown, gown_name="Backup Gown", stage=ReservationItem.Stage.RESERVED,
            rental_date=today, event_date=today + timedelta(days=2), return_date=today + timedelta(days=4),
            overdue_date=today + timedelta(days=5))

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="arabela-backup-test-"))
        self.addCleanup(self.cleanup_folder)

    def cleanup_folder(self):
        for item in self.folder.iterdir():
            item.unlink()
        self.folder.rmdir()

    def backup(self, **options):
        call_command("backup_database", output_dir=str(self.folder), stdout=io.StringIO(), **options)
        return sorted(p for p in self.folder.iterdir() if FILE_PATTERN.match(p.name))

    @staticmethod
    def records(path):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)



class BackupCommandTests(BackupTestBase):
    # ---- the file ---------------------------------------------------------------------------------------------------
    def test_it_saves_one_timestamped_compressed_file_that_can_be_read_back(self):
        files = self.backup()
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^arabela-backup-\d{8}-\d{6}\.json\.gz$")
        models = {r["model"] for r in self.records(files[0])}
        for expected in ("auth.user", "gowns.gown", "reservations.reservation", "reservations.reservationitem", "accounts.userprofile"):
            self.assertIn(expected, models)

    def test_it_holds_the_real_values(self):
        records = self.records(self.backup()[0])
        gown = next(r for r in records if r["model"] == "gowns.gown" and r["fields"]["gown_id"] == "BKP-0001")
        self.assertEqual(gown["fields"]["name"], "Backup Gown")
        self.assertEqual(gown["fields"]["rental_price"], "20000.00")
        user = next(r for r in records if r["model"] == "auth.user" and r["fields"]["username"] == "backup_customer")
        self.assertTrue(user["fields"]["password"].startswith(("pbkdf2_", "argon2", "bcrypt", "scrypt")))   # hashed, as stored

    def test_it_leaves_out_what_migrate_rebuilds_and_the_temporary_things(self):
        Session.objects.create(session_key="abc123", session_data="x", expire_date=timezone.now() + timedelta(days=1))
        LoginThrottle.objects.create(key="user:someone", failures=3, last_failure_at=timezone.now())
        models = {r["model"] for r in self.records(self.backup()[0])}
        for left_out in ("sessions.session", "contenttypes.contenttype", "auth.permission", "accounts.loginthrottle", "admin.logentry"):
            with self.subTest(model=left_out):
                self.assertNotIn(left_out, models)

    # ---- keeping only the newest ----------------------------------------------------------------------------------------
    def test_it_keeps_only_the_newest_few_and_never_touches_other_files(self):
        for stamp in ("20200101-000001", "20200102-000001", "20200103-000001"):
            (self.folder / f"arabela-backup-{stamp}.json.gz").write_bytes(b"old")
        (self.folder / "my-notes.txt").write_text("keep me")
        files = self.backup(keep=2)
        self.assertEqual(len(files), 2)
        self.assertTrue(files[-1].name > "arabela-backup-20200103", files)          # the new one is among them
        self.assertNotIn("arabela-backup-20200101-000001.json.gz", [f.name for f in files])
        self.assertTrue((self.folder / "my-notes.txt").exists())

    def test_keep_must_be_at_least_one(self):
        with self.assertRaises(CommandError):
            call_command("backup_database", output_dir=str(self.folder), keep=0)

    # ---- failing safely ----------------------------------------------------------------------------------------------------
    def test_a_failed_backup_saves_nothing_and_leaves_no_half_written_file(self):
        with patch("accounts.management.commands.backup_database.call_command", side_effect=RuntimeError("database went away")):
            with self.assertRaisesMessage(CommandError, "nothing was saved"):
                call_command("backup_database", output_dir=str(self.folder))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_an_empty_result_is_refused_rather_than_saved_as_a_backup(self):
        def write_nothing(*args, stdout=None, **kwargs):
            stdout.write("[]")
        with patch("accounts.management.commands.backup_database.call_command", side_effect=write_nothing):
            with self.assertRaises(CommandError):
                call_command("backup_database", output_dir=str(self.folder))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_the_default_folder_comes_from_the_environment_or_the_home_folder(self):
        with patch.dict("os.environ", {"ARABELA_BACKUP_DIR": "/somewhere/safe"}):
            self.assertEqual(default_folder(), Path("/somewhere/safe"))
        with patch.dict("os.environ", {"ARABELA_BACKUP_DIR": ""}):
            self.assertEqual(default_folder(), Path.home() / "ArabelaBackups")

    # ---- the restore ----------------------------------------------------------------------------------------------------------
    def test_loading_a_backup_puts_the_same_data_back(self):
        path = self.backup()[0]
        before = {
            "reservation_created": Reservation.objects.get(pk=self.reservation.pk).created_at,
            "gown_updated": Gown.objects.get(pk=self.gown.pk).updated_at,
        }
        ReservationItem.objects.all().delete()
        Reservation.objects.all().delete()
        Gown.objects.all().delete()
        User.objects.filter(username="backup_customer").delete()
        self.assertEqual(Gown.objects.count(), 0)

        call_command("loaddata", str(path), verbosity=0)

        restored_gown = Gown.objects.get(gown_id="BKP-0001")
        self.assertEqual((restored_gown.name, restored_gown.rental_price, restored_gown.status), ("Backup Gown", Decimal("20000.00"), Gown.Status.RESERVED))
        restored = Reservation.objects.get(pk=self.reservation.pk)
        self.assertEqual(restored.reference_code, self.reservation.reference_code)
        self.assertEqual(restored.items.count(), 1)
        self.assertEqual(restored.items.first().gown_id, restored_gown.pk)
        self.assertEqual(restored.customer.username, "backup_customer")
        # Dates are put back as they were, not set to "now". A backup keeps times to the millisecond (Django's format), so compare to that.
        self.assertEqual(restored.created_at, millisecond(before["reservation_created"]))
        self.assertEqual(restored_gown.updated_at, millisecond(before["gown_updated"]))

    @override_settings(CUSTOMER_EMAILS_ENABLED=True)
    def test_restoring_never_emails_customers_but_a_normal_save_still_does(self):
        with self.captureOnCommitCallbacks() as normal:
            message = CustomerMessage.objects.create(
                recipient=self.customer, category=CustomerMessage.Category.RESERVATION_REMINDER, body="Due back tomorrow.")
        self.assertEqual(len(normal), 1)                                         # the email hook is alive for real saves

        path = self.backup()[0]
        CustomerMessage.objects.filter(pk=message.pk).delete()
        with self.captureOnCommitCallbacks() as restored:
            call_command("loaddata", str(path), verbosity=0)
        self.assertTrue(CustomerMessage.objects.filter(pk=message.pk).exists())  # the old message is back...
        self.assertEqual(len(restored), 0)                                       # ...without sending anything


class RestoreCommandTests(BackupTestBase):
    """`manage.py restore_database FILE`: only into a new, empty database; brings back everything; clears what `migrate` pre-fills."""

    def wipe_shop_data(self):
        ReservationItem.objects.all().delete()
        Reservation.objects.all().delete()
        Gown.objects.all().delete()
        User.objects.all().delete()

    def restore(self, path):
        out = io.StringIO()
        call_command("restore_database", str(path), stdout=out)
        return out.getvalue()

    def test_it_refuses_a_database_that_already_has_shop_data_and_changes_nothing(self):
        path = self.backup()[0]
        users, gowns = User.objects.count(), Gown.objects.count()
        with self.assertRaisesMessage(CommandError, "already has"):
            self.restore(path)
        self.assertEqual((User.objects.count(), Gown.objects.count()), (users, gowns))

    def test_it_brings_everything_back_into_an_empty_database(self):
        from django.contrib.sites.models import Site
        path = self.backup()[0]
        sites_in_backup = Site.objects.count()
        self.wipe_shop_data()
        self.assertFalse(User.objects.exists())

        report = self.restore(path)

        self.assertIn("all counts match the file", report)
        self.assertEqual(Gown.objects.get(gown_id="BKP-0001").name, "Backup Gown")
        reservation = Reservation.objects.get(reference_code=self.reservation.reference_code)
        self.assertEqual(reservation.items.count(), 1)
        self.assertEqual(reservation.customer.username, "backup_customer")
        self.assertEqual(Site.objects.count(), sites_in_backup)                  # the pre-filled website entry did not clash

    def test_it_clears_what_migrate_pre_fills_instead_of_clashing_with_it(self):
        from django.contrib.sites.models import Site
        path = self.backup()[0]
        self.wipe_shop_data()
        Site.objects.all().delete()
        Site.objects.create(pk=99, domain="example.com", name="example.com")    # what a brand-new database starts with
        report = self.restore(path)
        self.assertIn("clearing 1 pre-filled row(s) in sites.site", report)
        self.assertFalse(Site.objects.filter(pk=99).exists())

    def test_it_refuses_a_missing_file_and_a_file_that_is_not_a_backup(self):
        self.wipe_shop_data()
        with self.assertRaisesMessage(CommandError, "There is no file"):
            self.restore(self.folder / "nope.json.gz")
        empty = self.folder / "arabela-backup-20200101-000001.json.gz"
        with gzip.open(empty, "wt", encoding="utf-8") as fh:
            fh.write("[]")
        with self.assertRaises(CommandError):
            self.restore(empty)
        self.assertFalse(User.objects.exists())                                  # nothing was half-loaded
