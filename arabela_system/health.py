"""/healthz/ -- "is the site alive and can it reach its database?", for an uptime monitor to check every few minutes.

Public on purpose (a monitor has no login) and tells strangers nothing: only ok / error, and whether error alerts are on. It does
one tiny database question, so it proves the database answers -- and the regular visit also keeps the free hosting from falling asleep."""
import logging
import time

from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from arabela_system import monitoring

logger = logging.getLogger("arabela.health")


def _database_answers() -> bool:
    for attempt in range(2):                      # one quick second try: a momentarily busy connection pool is not an outage
        try:
            with connection.cursor() as cursor:
                cursor.execute("select 1")
                cursor.fetchone()
            return True
        except Exception:
            connection.close()
            if attempt == 0:
                time.sleep(0.3)
    return False


@never_cache
@require_safe
def healthz(request):
    if not _database_answers():
        # A warning, not an error: the monitor alerts you about this itself, and an error here would also flood the alert inbox.
        logger.warning("Health check: the database did not answer.")
        return JsonResponse({"status": "error"}, status=503)
    return JsonResponse({"status": "ok", "error_alerts": "on" if monitoring.is_on() else "off"})
