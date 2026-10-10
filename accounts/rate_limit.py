"""How often one customer (or one connection) has done something recently -- kept in the DATABASE, so an app restart cannot
reset it and every server process sees the same count. Used to stop scripts spamming reservation submit, the checkout hold,
cart saving, search and the AI chat. The limits are set far above anything a real customer does, so nobody normal ever meets them.

Counts live in the same small table as the admin sign-in lockout (accounts.LoginThrottle: key / count / time), under keys that
start with "rl:", so this needs no new database table. Each count runs in a fixed window: when the window has passed, it
starts again from 1. If the table cannot be reached the same rule runs on the cache, so a limit can never take the site down."""
import random
from datetime import timedelta

from django.core.cache import cache
from django.db import DatabaseError, IntegrityError, transaction
from django.utils import timezone

from .models import LoginThrottle

KEY_PREFIX = "rl:"
SLOW_DOWN = "You're doing that very quickly. Please wait a minute and try again."
_KEEP_ROWS_FOR = timedelta(days=1, hours=1)   # longer than the longest window (the AI chat's day), so no live count is tidied away


def _now():
    return timezone.now()


def _key(name: str) -> str:
    return (KEY_PREFIX + name)[:120]


def client_ip(request) -> str:
    """The visitor's IP address behind Render's proxy: the LAST X-Forwarded-For entry, which Render itself appends. The first
    entry is whatever the visitor chose to send, so trusting it would let anyone dodge a limit."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def hit(name: str, window_seconds: int) -> int:
    """Counts one action in the current window and returns how many there have been in it so far (1 = the first)."""
    key = _key(name)
    now = _now()
    try:
        for attempt in range(2):
            try:
                with transaction.atomic():
                    row, _created = LoginThrottle.objects.select_for_update().get_or_create(
                        key=key, defaults={"failures": 0, "last_failure_at": now},
                    )
                    if (now - row.last_failure_at).total_seconds() >= window_seconds:
                        row.failures = 0                  # the window has passed: start counting again
                        row.last_failure_at = now         # (for these keys this time is the start of the window)
                    row.failures += 1
                    row.save(update_fields=["failures", "last_failure_at"])
                    count = row.failures
                break
            except IntegrityError:
                if attempt:                               # two first visits at the same instant: the second retry finds the row
                    raise
    except DatabaseError:
        count = _hit_cache(key, window_seconds)
    if random.random() < 0.01:                            # tidy up now and then, so one-off visitors never pile up
        try:
            LoginThrottle.objects.filter(key__startswith=KEY_PREFIX, last_failure_at__lt=now - _KEEP_ROWS_FOR).delete()
        except DatabaseError:
            pass
    return count


def allowed(name: str, limit: int, window_seconds: int) -> bool:
    """True while this is at most the `limit`-th action in the window."""
    return hit(name, window_seconds) <= limit


def count(name: str) -> int:
    """How many actions are recorded under `name` right now (0 if none) -- for checks and tests."""
    row = LoginThrottle.objects.filter(key=_key(name)).first()
    return row.failures if row else 0


def _hit_cache(key: str, window_seconds: int) -> int:
    cache_key = "rate_limit:" + key
    if cache.add(cache_key, 1, window_seconds):
        return 1
    try:
        return cache.incr(cache_key)
    except ValueError:                                    # expired between add() and incr()
        cache.set(cache_key, 1, window_seconds)
        return 1
