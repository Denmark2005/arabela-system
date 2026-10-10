"""Every table stays locked against Supabase's public web API: after `migrate` nothing is left open, a table created later is
locked the next time it runs, running it twice is harmless, it can never break a migration, and the app itself still reads and
writes normally with the lock on (it connects as an account that skips the lock)."""
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection
from django.test import TestCase

from arabela_system import table_lock

PROBE = "arb_lock_probe"


def unlocked_tables():
    with connection.cursor() as cursor:
        cursor.execute("select tablename from pg_tables where schemaname = 'public' and not rowsecurity order by 1")
        return [row[0] for row in cursor.fetchall()]


class PostgresOnly(TestCase):
    @classmethod
    def setUpClass(cls):
        if connection.vendor != "postgresql":
            from unittest import SkipTest
            raise SkipTest("row-level security is a PostgreSQL feature")
        super().setUpClass()


class TableLockTests(PostgresOnly):
    def test_after_migrate_no_public_table_is_left_open(self):
        self.assertEqual(unlocked_tables(), [])

    def test_a_table_created_later_is_locked_the_next_time_it_runs(self):
        with connection.cursor() as cursor:
            cursor.execute(f"create table public.{PROBE} (id integer)")
        self.assertIn(PROBE, unlocked_tables())
        table_lock.lock_public_tables()
        self.assertEqual(unlocked_tables(), [])

    def test_running_it_again_changes_nothing_and_does_not_fail(self):
        table_lock.lock_public_tables()
        table_lock.lock_public_tables()
        self.assertEqual(unlocked_tables(), [])

    def test_it_runs_by_itself_after_migrate(self):
        from django.db.models.signals import post_migrate
        receivers = [r for r in post_migrate.receivers if r[0][0] == "arabela_lock_public_tables"]
        self.assertEqual(len(receivers), 1)

    def test_it_never_breaks_a_migration_if_the_database_refuses(self):
        broken = MagicMock()
        broken.vendor = "postgresql"
        broken.cursor.side_effect = DatabaseError("permission denied")
        with patch("arabela_system.table_lock.connections", {"default": broken}):
            with self.assertLogs("arabela.table_lock", level="WARNING") as logged:
                table_lock.lock_public_tables()              # must not raise
        self.assertIn("Could not lock the database tables", logged.output[0])
        self.assertIn("Supabase SQL editor", logged.output[0])

    def test_the_app_still_reads_and_writes_normally_with_the_lock_on(self):
        User = get_user_model()
        User.objects.create_user(username="lock_check", password="x" * 12)
        self.assertEqual(User.objects.filter(username="lock_check").count(), 1)
        self.assertEqual(unlocked_tables(), [])


class OtherDatabasesAreLeftAlone(TestCase):
    def test_sqlite_or_any_non_postgres_database_is_skipped_without_touching_it(self):
        fake = MagicMock()
        fake.vendor = "sqlite"
        with patch("arabela_system.table_lock.connections", {"default": fake}):
            table_lock.lock_public_tables()
        fake.cursor.assert_not_called()
