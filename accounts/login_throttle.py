"""Brute-force protection for the admin sign-in: 5 wrong attempts for a username lock that username for 15 minutes.

The count is kept in the database (accounts.LoginThrottle), so it survives a restart of the app and is the same for every
server process -- the in-memory counter it replaces was forgotten whenever the host restarted or put the app to sleep.
Until the table exists (the code is live before its migration has run) the same rule runs on the cache, exactly as before,
so signing in never breaks.

Rule, unchanged: attempts are counted per username (ignoring capitals); a lockout ends 15 minutes after the 5th wrong
attempt; attempts made while locked do not extend it; a correct sign-in clears the count."""
import math
import random
from datetime import timedelta

from django.core.cache import cache
from django.db import DatabaseError, transaction
from django.utils import timezone

from .models import LoginThrottle

ATTEMPT_LIMIT = 5
LOCKOUT_SECONDS = 15 * 60
_KEEP_ROWS_FOR = timedelta(days=1)   # a row older than this can no longer lock anyone


def _now():
    return timezone.now()


def username_key(username: str) -> str:
    return f"user:{(username or '').strip().lower()}"[:120]


def lockout_message(seconds: int) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    return f"Too many failed login attempts. Please try again in {minutes} minute{'s' if minutes != 1 else ''}."


def _cache_key(key: str) -> str:
    return f"admin_login_throttle:{key}"


def seconds_locked(key: str) -> int:
    """How many seconds until this username may try again (0 = not locked)."""
    now = _now()
    try:
        row = LoginThrottle.objects.filter(key=key).first()
        failures, last = (row.failures, row.last_failure_at) if row else (0, None)
    except DatabaseError:
        failures, last = cache.get(_cache_key(key), (0, None))
    if failures < ATTEMPT_LIMIT or last is None:
        return 0
    return max(0, math.ceil(LOCKOUT_SECONDS - (now - last).total_seconds()))


def record_failure(key: str) -> int:
    """Count one wrong attempt; returns how many tries are left before the lockout (0 = locked now)."""
    now = _now()
    try:
        with transaction.atomic():
            row, _created = LoginThrottle.objects.select_for_update().get_or_create(
                key=key, defaults={"failures": 0, "last_failure_at": now},
            )
            if now - row.last_failure_at >= timedelta(seconds=LOCKOUT_SECONDS):
                row.failures = 0                      # the old count has run out: start again
            row.failures += 1
            row.last_failure_at = now
            row.save(update_fields=["failures", "last_failure_at"])
            failures = row.failures
    except DatabaseError:
        failures, last = cache.get(_cache_key(key), (0, None))
        if last is None or (now - last).total_seconds() >= LOCKOUT_SECONDS:
            failures = 0
        failures += 1
        cache.set(_cache_key(key), (failures, now), LOCKOUT_SECONDS)
    if random.random() < 0.05:                         # tidy up now and then, so made-up usernames never pile up
        try:
            LoginThrottle.objects.filter(last_failure_at__lt=now - _KEEP_ROWS_FOR).delete()
        except DatabaseError:
            pass
    return max(0, ATTEMPT_LIMIT - failures)


def clear(key: str) -> None:
    """A correct sign-in wipes the count for that username."""
    try:
        LoginThrottle.objects.filter(key=key).delete()
    except DatabaseError:
        pass
    cache.delete(_cache_key(key))
