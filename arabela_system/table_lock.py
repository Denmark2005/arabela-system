"""Keeps every table in the live database locked against Supabase's public web API (row-level security).

Supabase can expose the database's `public` schema through its own REST API. A table with row-level security OFF can be read
and changed through that API by anyone who has the project address and public key -- without going through this website.
With the lock ON and no policies, the API sees nothing at all, while the website keeps working: it connects as the `postgres`
account, which skips the lock (confirmed with `select rolbypassrls from pg_roles`).

The lock is a per-table setting, and every new table a migration creates starts UNLOCKED. So this runs after every `migrate`
and locks whatever is not locked yet. Running it again is harmless (it only touches tables that are still unlocked), and it
can never make a migration fail."""
import logging

from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections

logger = logging.getLogger("arabela.table_lock")


def lock_public_tables(sender=None, using=DEFAULT_DB_ALIAS, **kwargs):
    """post_migrate receiver: turn row-level security on for every `public` table that does not have it yet."""
    connection = connections[using]
    if connection.vendor != "postgresql":          # the local SQLite fallback has no such thing
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute("select tablename from pg_tables where schemaname = 'public' and not rowsecurity order by 1")
            unlocked = [row[0] for row in cursor.fetchall()]
            for table in unlocked:
                cursor.execute(f"alter table public.{connection.ops.quote_name(table)} enable row level security")
        if unlocked:
            logger.info("Locked %d table(s) against the public API: %s", len(unlocked), ", ".join(unlocked))
    except DatabaseError as exc:
        # A courtesy that must never stop a deploy: say so loudly and let the migration finish.
        logger.warning("Could not lock the database tables (%s). Run the lock script in the Supabase SQL editor.", exc)
