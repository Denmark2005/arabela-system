"""The laptop's own database, so clicking around locally never changes the live shop.

`.env` holds the live database (Render and the laptop used to share it). When a second file, `.env.laptop-db`, sits next to it,
the laptop uses the database named there instead -- a separate free Supabase project holding a copy of the live data. Render has
no such file, so the live site is unaffected. To run ONE command against the live database on purpose (a migration, the daily
backup), use `python live.py <command>`: it sets ARABELA_LIVE_DB=1 for that command only.

Only the five DATABASE_* lines are read from the file. Anything typed in the terminal itself (for example DATABASE_NAME=test_postgres
for the tests) still wins, exactly as it does over `.env`. A half-filled file, or one that points back at the live database, stops
everything with a clear message instead of quietly falling back to live.
Deliberately imports nothing from Django apart from the exception, because settings.py runs it before Django is set up."""
from django.core.exceptions import ImproperlyConfigured

FILE_NAME = ".env.laptop-db"
LIVE_SWITCH = "ARABELA_LIVE_DB"
KEYS = ("DATABASE_NAME", "DATABASE_USER", "DATABASE_PASSWORD", "DATABASE_HOST", "DATABASE_PORT")
REQUIRED = ("DATABASE_USER", "DATABASE_PASSWORD", "DATABASE_HOST")


def read_env_file(path):
    """KEY=value lines of a file, the same way settings.py reads `.env` (comments and blank lines skipped, quotes stripped)."""
    values = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _where(values):
    return (values.get("DATABASE_HOST", "").lower(), values.get("DATABASE_USER", ""), values.get("DATABASE_NAME", "") or "postgres")


def apply(base_dir, environ, shell_keys):
    """Points `environ` at the laptop database when `.env.laptop-db` exists and ARABELA_LIVE_DB is not 1.

    Returns True when the laptop database is in use. `shell_keys` are the names that were set in the terminal before `.env`
    was read; those are never overwritten."""
    path = base_dir / FILE_NAME
    if environ.get(LIVE_SWITCH) == "1" or not path.exists():
        return False
    laptop = {key: value for key, value in read_env_file(path).items() if key in KEYS and value}
    missing = [key for key in REQUIRED if key not in laptop]
    if missing:
        raise ImproperlyConfigured(
            f"{FILE_NAME} is missing {', '.join(missing)}. Fill it in from the laptop Supabase project's Connect page, "
            f"or delete {FILE_NAME} to go back to using the live database.")
    live = {key: environ.get(key, "") for key in KEYS}
    if _where({**live, **laptop}) == _where(live):
        raise ImproperlyConfigured(
            f"{FILE_NAME} points at the LIVE database (same host, user and name as .env). It must name the separate laptop "
            f"Supabase project.")
    for key, value in laptop.items():
        if key not in shell_keys:
            environ[key] = value
    return True
