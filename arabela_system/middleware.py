import logging
import time

from django.conf import settings
from django.contrib.auth import SESSION_KEY, get_user_model
from django.contrib.sessions.backends.base import UpdateError
from django.contrib.sessions.exceptions import SessionInterrupted
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.db.models import Q
from django.db.utils import OperationalError
from django.http import HttpResponse
from django.utils.cache import patch_vary_headers
from django.utils.http import http_date

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


# --- Separate sign-in for the admin panel and the customer site -----------------------------------------------------
# Both live on one web address, and a browser keeps ONE session cookie per address. With a single cookie, signing in to
# the admin panel in one tab silently replaced the customer's sign-in in every other tab (and signing in as a customer
# replaced the admin's), so a customer tab "changed account" on the next refresh -- and a booking made in it could be
# saved under the wrong account. The admin area now has its own cookie, so the two sign-ins sit side by side.
ADMIN_AREAS = ("/admin-panel/", "/admin/")


def is_admin_area(path):
    return path == "/admin-panel" or path.startswith(ADMIN_AREAS)


def admin_cookie_name():
    return f"{settings.SESSION_COOKIE_NAME}_admin"


class SplitSessionMiddleware(SessionMiddleware):
    """Django's SessionMiddleware with two cookies: the usual one for the customer site, a second for the admin area.

    One carry-over so nobody is signed out by the change: when a request to the admin area has no admin cookie yet but
    carries the usual cookie of a STAFF sign-in (an admin who was already signed in before this existed), that session is
    still honoured. The usual cookie of an ordinary customer is never used in the admin area."""

    @staticmethod
    def _is_staff_session(store):
        user_id = store.get(SESSION_KEY)
        if not user_id:
            return False
        User = get_user_model()
        try:
            pk = User._meta.pk.to_python(user_id)
        except ValidationError:
            return False
        return User.objects.filter(pk=pk, is_active=True).filter(Q(is_staff=True) | Q(is_superuser=True)).exists()

    def process_request(self, request):
        customer_name = settings.SESSION_COOKIE_NAME
        if not is_admin_area(request.path):
            request._session_cookie_name = customer_name
            request.session = self.SessionStore(request.COOKIES.get(customer_name))
            return
        admin_name = admin_cookie_name()
        request._session_cookie_name = admin_name
        store = self.SessionStore(request.COOKIES.get(admin_name))
        if admin_name not in request.COOKIES and request.COOKIES.get(customer_name):
            carried = self.SessionStore(request.COOKIES[customer_name])
            if self._is_staff_session(carried):
                request._session_cookie_name = customer_name
                store = carried
        request.session = store

    def process_response(self, request, response):
        # Django's own SessionMiddleware.process_response, with the cookie name taken from the request instead of the
        # setting (kept line for line so a Django upgrade is easy to compare).
        try:
            accessed = request.session.accessed
            modified = request.session.modified
            empty = request.session.is_empty()
        except AttributeError:
            return response
        name = getattr(request, "_session_cookie_name", settings.SESSION_COOKIE_NAME)
        if name in request.COOKIES and empty:
            response.delete_cookie(
                name,
                path=settings.SESSION_COOKIE_PATH,
                domain=settings.SESSION_COOKIE_DOMAIN,
                samesite=settings.SESSION_COOKIE_SAMESITE,
            )
            patch_vary_headers(response, ("Cookie",))
        else:
            if accessed:
                patch_vary_headers(response, ("Cookie",))
            if (modified or settings.SESSION_SAVE_EVERY_REQUEST) and not empty:
                if request.session.get_expire_at_browser_close():
                    max_age = None
                    expires = None
                else:
                    max_age = request.session.get_expiry_age()
                    expires = http_date(time.time() + max_age)
                if response.status_code < 500:
                    try:
                        request.session.save()
                    except UpdateError:
                        raise SessionInterrupted(
                            "The request's session was deleted before the "
                            "request completed. The user may have logged "
                            "out in a concurrent request, for example."
                        )
                    response.set_cookie(
                        name,
                        request.session.session_key,
                        max_age=max_age,
                        expires=expires,
                        domain=settings.SESSION_COOKIE_DOMAIN,
                        path=settings.SESSION_COOKIE_PATH,
                        secure=settings.SESSION_COOKIE_SECURE or None,
                        httponly=settings.SESSION_COOKIE_HTTPONLY or None,
                        samesite=settings.SESSION_COOKIE_SAMESITE,
                    )
        return response
