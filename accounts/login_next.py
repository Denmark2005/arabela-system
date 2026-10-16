"""Where a customer was headed when they pressed "Continue with Google" -- kept for a few minutes.

Why: the Google answer can reach the site TWICE (a second tap, a slow phone, or Chrome re-running the whole sign-in when "Desktop
site" is switched on or off, which replays the original Google address). The first answer signs the customer in and takes them
where they were going (usually the checkout page); the repeat fails, because Google's one-time code was already used. All the
failing repeat can do is put the customer back where they were headed instead of dropping them on the homepage with an empty
checkout. The destination is only a page address, never trusted: it must be a path on this site."""
import time

from django.utils.http import url_has_allowed_host_and_scheme

SESSION_KEY = "arabela_login_next"
KEEP_SECONDS = 15 * 60      # a sign-in takes seconds; this only has to outlast a repeated answer


def _safe(request, url):
    url = (url or "").strip()
    if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return url
    return ""


def remember(request, url):
    """Called when the login page is shown. No (or an unsafe) destination clears whatever was kept before."""
    url = _safe(request, url)
    if url:
        request.session[SESSION_KEY] = {"url": url, "at": time.time()}
    else:
        request.session.pop(SESSION_KEY, None)


def recall(request):
    """The remembered destination if it is recent and still a safe path on this site, else ''."""
    kept = request.session.get(SESSION_KEY)
    if not isinstance(kept, dict):
        return ""
    try:
        if time.time() - float(kept.get("at", 0)) > KEEP_SECONDS:
            return ""
    except (TypeError, ValueError):
        return ""
    return _safe(request, kept.get("url"))
