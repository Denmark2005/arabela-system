import logging
import time

from django.db import close_old_connections
from django.db.utils import OperationalError
from django.http import HttpResponse

logger = logging.getLogger(__name__)

# Supabase's free-tier database plan allows only a small number of simultaneous
# connections (confirmed by directly stress-testing this project and watching real
# requests get rejected past that cap). A sudden spike -- more visitors loading the
# site at the exact same instant than that limit -- makes Postgres briefly refuse
# new connections with an OperationalError. This clears within a second or two as
# earlier requests finish and release their connections; it is a hosting-plan
# limit, not a bug in this project's code.
_MAX_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 0.3

_BUSY_HTML = """<!doctype html>
<html>
<head><title>Please try again</title></head>
<body style="font-family: system-ui, -apple-system, sans-serif; text-align: center; padding: 90px 20px; color: #333;">
  <h1 style="font-size: 22px; margin-bottom: 12px;">We're experiencing high traffic right now</h1>
  <p style="color: #666;">Please refresh this page in a moment.</p>
</body>
</html>"""


def _reset_uploaded_files(request):
    """Rewinds any uploaded files before a retry re-runs the view. Without this, a
    retry after the view had already partially read an upload (e.g. proof-of-payment
    or a gown photo) would silently save an empty file instead of the real one."""
    for key in request.FILES:
        for f in request.FILES.getlist(key):
            try:
                f.seek(0)
            except (AttributeError, ValueError):
                pass


class DatabaseRetryMiddleware:
    """Retries a request a couple of times if it fails only because the database's
    connection pool was momentarily full, instead of showing the visitor a raw
    error screen.

    OperationalError (not IntegrityError/ProgrammingError/DataError) is Django's own
    category for "couldn't talk to the database" -- connection refused, pool
    exhausted, network drop -- as opposed to a bad query, which is a real bug and
    must never be silently retried since it would just fail again identically.

    Safe to retry the whole request: this project wraps every multi-step write in
    transaction.atomic() (reservation submission, gown/reference-code sequence
    counters), so a connection failure that occurs mid-transaction leaves nothing
    committed -- there is nothing partial to duplicate.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        for attempt in range(_MAX_ATTEMPTS):
            if attempt > 0:
                _reset_uploaded_files(request)
            try:
                return self.get_response(request)
            except OperationalError:
                close_old_connections()
                if attempt == _MAX_ATTEMPTS - 1:
                    logger.error(
                        "Database connection pool exhausted after %d attempts for %s",
                        _MAX_ATTEMPTS, request.path,
                    )
                    return HttpResponse(_BUSY_HTML, status=503)
                time.sleep(_RETRY_DELAY_SECONDS)
