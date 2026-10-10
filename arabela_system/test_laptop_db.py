"""The laptop's own database (arabela_system/laptop_db.py): used only when .env.laptop-db exists, never overriding what the terminal
says, never quietly falling back to live, and never letting the laptop delete a photo the live shop still shows."""
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings

from arabela_system import laptop_db

LIVE = {"DATABASE_NAME": "postgres", "DATABASE_USER": "postgres.liveref", "DATABASE_PASSWORD": "live-pw",
        "DATABASE_HOST": "aws-0-ap-northeast-2.pooler.supabase.com", "DATABASE_PORT": "5432"}
LAPTOP_FILE = ("# laptop copy\nDATABASE_USER=postgres.laptopref\nDATABASE_PASSWORD='laptop-pw'\n"
               "DATABASE_HOST=aws-0-ap-northeast-2.pooler.supabase.com\nSECRET_KEY=ignored\n")


class LaptopDatabaseTests(SimpleTestCase):
    def folder(self, text=None):
        root = Path(tempfile.mkdtemp(prefix="arabela-laptop-db-"))
        if text is not None:
            (root / laptop_db.FILE_NAME).write_text(text, encoding="utf-8")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def test_without_the_file_nothing_changes(self):
        env = dict(LIVE)
        self.assertFalse(laptop_db.apply(self.folder(), env, set()))
        self.assertEqual(env, LIVE)

    def test_with_the_file_the_database_lines_come_from_it_and_nothing_else_does(self):
        env = dict(LIVE)
        self.assertTrue(laptop_db.apply(self.folder(LAPTOP_FILE), env, set()))
        self.assertEqual(env["DATABASE_USER"], "postgres.laptopref")
        self.assertEqual(env["DATABASE_PASSWORD"], "laptop-pw")
        self.assertEqual(env["DATABASE_NAME"], "postgres")             # not in the file: kept
        self.assertNotIn("SECRET_KEY", env)                            # only DATABASE_* lines are read

    def test_what_the_terminal_says_still_wins(self):
        env = dict(LIVE, DATABASE_NAME="test_postgres")
        laptop_db.apply(self.folder(LAPTOP_FILE + "DATABASE_NAME=postgres\n"), env, {"DATABASE_NAME"})
        self.assertEqual(env["DATABASE_NAME"], "test_postgres")
        self.assertEqual(env["DATABASE_USER"], "postgres.laptopref")

    def test_live_py_switch_ignores_the_file_for_that_command(self):
        env = dict(LIVE, ARABELA_LIVE_DB="1")
        self.assertFalse(laptop_db.apply(self.folder(LAPTOP_FILE), env, set()))
        self.assertEqual(env["DATABASE_USER"], "postgres.liveref")

    def test_a_half_filled_file_stops_instead_of_quietly_using_live(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "missing DATABASE_PASSWORD"):
            laptop_db.apply(self.folder("DATABASE_USER=postgres.laptopref\nDATABASE_HOST=h\nDATABASE_PASSWORD=\n"), dict(LIVE), set())

    def test_a_file_that_points_back_at_live_is_refused(self):
        same = "".join(f"{key}={value}\n" for key, value in LIVE.items())
        with self.assertRaisesMessage(ImproperlyConfigured, "points at the LIVE database"):
            laptop_db.apply(self.folder(same), dict(LIVE), set())

    def test_the_example_file_has_no_secrets_and_the_real_one_is_never_committed(self):
        base = Path(__file__).resolve().parent.parent
        example = laptop_db.read_env_file(base / ".env.laptop-db.example")
        self.assertEqual(example["DATABASE_PASSWORD"], "")
        self.assertIn(".env.laptop-db", (base / ".gitignore").read_text(encoding="utf-8").splitlines())

    def test_the_laptop_never_deletes_a_cloudinary_file(self):
        from cloudinary_storage.storage import MediaCloudinaryStorage
        from arabela_system.laptop_storage import LaptopSafeCloudinaryStorage
        with patch.object(MediaCloudinaryStorage, "delete") as real_delete:
            LaptopSafeCloudinaryStorage.delete(object.__new__(LaptopSafeCloudinaryStorage), "media/gowns/one.jpg")
        real_delete.assert_not_called()

    @override_settings(USING_LAPTOP_DB=True)
    def test_the_daily_backup_refuses_to_back_up_the_laptop_copy(self):
        with self.assertRaisesMessage(CommandError, "python live.py backup_database"):
            call_command("backup_database", output_dir=str(self.folder()))
