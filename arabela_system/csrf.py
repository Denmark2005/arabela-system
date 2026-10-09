"""What happens when Django's CSRF check refuses a request.

The check is a security feature: a form or button only works if it carries the secret from the page it came from
AND the browser sends back the matching cookie. When a phone opens an old copy of a page, or its browser does not
keep cookies (private or in-app browsers), the cookie and the page disagree and Django answers with a bare
"Forbidden (403) CSRF verification failed" page that tells the person nothing. This replaces it with a short page
that says what to do (one button to a fresh page), a clear JSON answer for the pages' own buttons, and a log line
saying WHY it was refused, so the next time can be diagnosed from the server log instead of guessed.
"""
import logging

from django.http import HttpResponse, JsonResponse
from django.urls import NoReverseMatch, reverse
from django.utils.html import escape

logger = logging.getLogger("arabela.csrf")

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Please try again | Arabela</title>
<style>
  body {{ margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center; background: #fcf9f8; color: #1b1c1c;
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
  main {{ max-width: 420px; margin: 24px; padding: 32px 28px; background: #fff; border: 1px solid #e5e2e1; border-radius: 12px; text-align: center; }}
  h1 {{ margin: 0 0 12px; font-size: 22px; font-weight: 600; }}
  p {{ margin: 0 0 14px; font-size: 15px; line-height: 1.55; color: #444748; }}
  a.button {{ display: inline-block; margin-top: 6px; padding: 12px 22px; border-radius: 8px; background: #465fff; color: #fff; font-weight: 600; text-decoration: none; }}
  small {{ display: block; margin-top: 18px; font-size: 12.5px; line-height: 1.5; color: #7a7d7e; }}
</style>
</head>
<body>
<main>
  <h1>Please try again</h1>
  <p>This page was open for too long, or your browser did not keep its security cookie, so nothing was sent. Nothing was changed.</p>
  <a class="button" href="{href}">{button}</a>
  <small>If it keeps happening, make sure cookies are allowed for this site and you are not in a private or in-app browser tab.</small>
</main>
</body>
</html>
"""

JSON_MESSAGE = "This page has expired. Please refresh it and try again."


def _fresh_page_for(request):
    """(address, button text) of a fresh page to start again from: the admin sign-in for the admin panel, else the home page."""
    if request.path.startswith("/admin-panel/"):
        try:
            return reverse("arabela_admin:admin_login"), "Open the sign-in page"
        except NoReverseMatch:
            pass
    return "/", "Back to the website"


def _wants_json(request):
    """The pages' own buttons (fetch) want a short JSON answer they can show; a form that navigated wants a page."""
    if request.headers.get("X-CSRFToken") or "application/json" in request.headers.get("Accept", ""):
        return True
    mode = request.headers.get("Sec-Fetch-Mode")
    return bool(mode) and mode != "navigate"


def csrf_failure(request, reason=""):
    logger.warning(
        "CSRF refused %s %s: %s [csrf cookie sent: %s, origin: %s, referer sent: %s, browser: %s]",
        request.method, request.path, reason or "no reason given",
        "csrftoken" in request.COOKIES, request.headers.get("Origin", "-"), bool(request.headers.get("Referer")),
        (request.headers.get("User-Agent", "-"))[:100],
    )
    if _wants_json(request):
        return JsonResponse({"error": JSON_MESSAGE}, status=403)
    href, button = _fresh_page_for(request)
    return HttpResponse(_PAGE.format(href=escape(href), button=escape(button)), status=403)
