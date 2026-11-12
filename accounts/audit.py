"""The Audit Log: one append-only line for everything a signed-in admin account does (accounts.models.AuditLog).

How a line gets written
  * @audited(...) wraps every admin view that changes something. After the view answers, the decorator writes the line: the one the
    owner reads ("Maria approved booking RSV-2026-0007"), who did it, what it was about, and -- for the important ones -- what it
    was before and after. Because the decorator itself writes the line, an endpoint cannot forget it (a test also fails when a new
    admin POST endpoint is left without the decorator).
  * A refused attempt (a staff account the owner has not allowed) is written too, marked sensitive and with the address it came from.
  * Sign-ins, sign-outs and failed sign-ins are written from Django's own signals (connected in AccountsConfig.ready).

Rules that never bend
  * Writing a line must never break the action it describes: any error here is logged to the server log and swallowed.
  * Never a password, token, session key or picture: scrub() drops those keys and shortens every value.
  * IP address and device are kept only for sign-in lines and refused attempts, not for everyday work.
  * Nothing edits or deletes a line (AuditLogQuerySet); only purge_old() removes lines older than 12 months.
"""
import json
import logging
import re
from datetime import timedelta
from functools import wraps
from types import SimpleNamespace

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from accounts.models import AuditLog
from accounts.roles import staff_display_name

logger = logging.getLogger(__name__)

RETENTION_DAYS = 365
_PURGE_KEY = "arabela:audit-log:last-purge"
_PURGE_EVERY_SECONDS = 24 * 3600

_SECRET_KEY = re.compile(r"pass|token|secret|csrf|session|cookie|base64|credential|photo_data|image_data|authorization", re.I)
_VALUE_MAX = 200
_MAX_DEPTH = 3
_MAX_ITEMS = 25


def _clip(value, limit):
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def scrub(value, depth=0):
    """A copy of `value` that is safe to keep in the log: keys that could hold a secret are dropped, strings are shortened,
    nesting and list length are limited. Anything that is not plain data becomes its text."""
    if depth > _MAX_DEPTH:
        return _clip(value, _VALUE_MAX)
    if isinstance(value, dict):
        clean = {}
        for key, item in list(value.items())[:_MAX_ITEMS]:
            if _SECRET_KEY.search(str(key)):
                continue
            clean[_clip(key, 60)] = scrub(item, depth + 1)
        return clean
    if isinstance(value, (list, tuple, set)):
        return [scrub(item, depth + 1) for item in list(value)[:_MAX_ITEMS]]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    return _clip(value, _VALUE_MAX)


def actor_role(user):
    if user is None:
        return ""
    if user.is_superuser:
        return "Owner"
    from accounts.models import UserProfile
    profile = UserProfile.objects.filter(user=user).first()
    return "Owner" if profile is not None and profile.role == UserProfile.Role.OWNER else "Staff"


def client_network(request):
    """(ip address, short device text) of a request. The address is the first one the host's proxy reports, else the connection's."""
    if request is None:
        return "", ""
    forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    ip = forwarded or request.META.get("REMOTE_ADDR") or ""
    return _clip(ip, 45), _clip(request.META.get("HTTP_USER_AGENT") or "", 120)


def record(actor, *, category, action, summary, target_type="", target_id="", target_label="", details=None,
           sensitive=False, outcome="ok", request=None, network=False, actor_name=""):
    """Write one Audit Log line. Returns the row, or None when it could not be written (never raises)."""
    try:
        ip, device = client_network(request) if network else ("", "")
        name = actor_name or (staff_display_name(actor) if actor is not None else "")
        with transaction.atomic():
            row = AuditLog.objects.create(
                actor=actor if getattr(actor, "pk", None) else None,
                actor_name=_clip(name, 150),
                actor_role=actor_role(actor) if getattr(actor, "pk", None) else "",
                category=category,
                action=_clip(action, 60),
                summary=_clip(summary, 300),
                target_type=_clip(target_type, 30),
                target_id=_clip(target_id, 40),
                target_label=_clip(target_label, 160),
                details=scrub(details or {}),
                sensitive=bool(sensitive),
                outcome=outcome,
                ip_address=ip,
                device=device,
            )
    except Exception:       # noqa: BLE001 -- the log must never break the thing it records
        logger.exception("Audit Log line could not be written (%s)", action)
        return None
    if request is not None:
        request._audit_written = True
    return row


# ---------------------------------------------------------------------------------------------------------------------------------
# The decorator
# ---------------------------------------------------------------------------------------------------------------------------------
def _request_body(request):
    """What the request sent, as a dict: the JSON body, else the form fields. Never raises."""
    try:
        if "json" in (request.content_type or ""):
            data = json.loads(request.body or b"{}")
            return data if isinstance(data, dict) else {}
        return {key: request.POST.get(key) for key in request.POST}
    except Exception:       # noqa: BLE001
        return {}


def _response_data(response):
    try:
        if "json" in (response.get("Content-Type") or ""):
            data = json.loads(response.content or b"{}")
            return data if isinstance(data, dict) else {}
    except Exception:       # noqa: BLE001
        pass
    return {}


def audited(action, category, describe=None, *, sensitive=False, before=None):
    """Write an Audit Log line for the admin view this decorates.

    `describe(ctx)` returns a dict with `summary` (one readable sentence), optionally `target` = (type, id, label), `details`
    (what changed: before -> after, the reason, counts) and `sensitive`. ctx has: request, user, kwargs (the URL arguments), body
    (what the request sent), response, data (the JSON the view answered), before (what `before(request, kwargs)` returned
    before the view ran: the state to compare with).
    Without `describe`, or if it fails, a plain line is still written -- the line is never skipped.

    A 2xx answer is written as done; 403 as refused (always sensitive); a server error or exception as failed. An answer that
    only says "check what you typed" (400, 404, 409, ...) is not an action and is not written; nor is a request from nobody signed in."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            user = getattr(request, "user", None)
            signed_in = bool(user and user.is_authenticated and (user.is_staff or user.is_superuser))
            before_state = {}
            if signed_in and before is not None:
                try:
                    before_state = before(request, kwargs) or {}
                except Exception:       # noqa: BLE001
                    logger.exception("Audit 'before' state failed (%s)", action)
            try:
                response = view(request, *args, **kwargs)
            except Exception:
                if signed_in:
                    _write(request, user, action, category, describe, sensitive, kwargs, before_state, None, "failed")
                raise
            if signed_in:
                status = getattr(response, "status_code", 200)
                data = _response_data(response)
                if status == 403:
                    _write(request, user, action, category, describe, True, kwargs, before_state, response, "refused", data)
                elif status >= 500:
                    _write(request, user, action, category, describe, sensitive, kwargs, before_state, response, "failed", data)
                elif 200 <= status < 300 and not data.get("error"):
                    _write(request, user, action, category, describe, sensitive, kwargs, before_state, response, "ok", data)
            return response

        wrapper.audited = True
        wrapper.audit_action = action
        return wrapper

    return decorator


def _write(request, user, action, category, describe, sensitive, kwargs, before_state, response, outcome, data=None):
    if getattr(request, "_audit_written", False):
        return          # the view wrote its own, richer line (e.g. a bulk action with its counts)
    info = {}
    if describe is not None:
        try:
            ctx = SimpleNamespace(request=request, user=user, kwargs=kwargs, body=_request_body(request), response=response,
                                  data=data or {}, before=before_state, outcome=outcome)
            info = describe(ctx) or {}
        except Exception:       # noqa: BLE001
            logger.exception("Audit description failed (%s)", action)
            info = {}
    target = info.get("target") or ("", "", "")
    # `summary` is a past-tense phrase without the name ("approved booking RSV-2026-0007"); the name goes in front here.
    phrase = info.get("summary") or action.replace(".", " ").replace("_", " ")
    who = staff_display_name(user)
    if outcome == "refused":
        summary = f"{who}'s attempt was refused: {phrase}"
    elif outcome == "failed":
        summary = f"{who} {phrase}, but it did not go through"
    else:
        summary = f"{who} {phrase}"
    record(
        user, category=category, action=action, summary=summary,
        target_type=target[0], target_id=str(target[1]), target_label=target[2],
        details=info.get("details") or {}, sensitive=bool(sensitive or info.get("sensitive") or outcome == "refused"),
        outcome=outcome, request=request, network=(outcome == "refused"),
    )


# ---------------------------------------------------------------------------------------------------------------------------------
# Sign-in lines (Django's signals)
# ---------------------------------------------------------------------------------------------------------------------------------
def _is_admin_account(user):
    return bool(user is not None and getattr(user, "pk", None) and (user.is_staff or user.is_superuser))


def on_logged_in(sender, request, user, **kwargs):
    if _is_admin_account(user):
        record(user, category=AuditLog.Category.SIGN_IN, action="signin.ok", summary=f"{staff_display_name(user)} signed in",
               request=request, network=True)


def on_logged_out(sender, request, user, **kwargs):
    if _is_admin_account(user):
        record(user, category=AuditLog.Category.SIGN_IN, action="signin.out", summary=f"{staff_display_name(user)} signed out",
               request=request, network=True)


def on_login_failed(sender, credentials, request=None, **kwargs):
    """A wrong password for an ADMIN username. Wrong guesses for a username that is not an admin account (bots, customers) are not
    written: they would only fill the log."""
    username = (credentials or {}).get("username")
    if not username or request is None:
        return
    try:
        from django.contrib.auth import get_user_model
        account = get_user_model().objects.filter(username__iexact=str(username)).first()
    except Exception:       # noqa: BLE001
        return
    if _is_admin_account(account):
        record(account, category=AuditLog.Category.SIGN_IN, action="signin.failed",
               summary=f"Wrong password for {staff_display_name(account)}'s account", outcome="failed", sensitive=True,
               request=request, network=True)


def connect_signals():
    from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
    user_logged_in.connect(on_logged_in, dispatch_uid="arabela_audit_in")
    user_logged_out.connect(on_logged_out, dispatch_uid="arabela_audit_out")
    user_login_failed.connect(on_login_failed, dispatch_uid="arabela_audit_failed")


# ---------------------------------------------------------------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------------------------------------------------------------
def purge_old(now=None):
    """Delete lines older than 12 months. The only way a line ever leaves the log."""
    now = now or timezone.now()
    return AuditLog.objects.purge_older_than(now - timedelta(days=RETENTION_DAYS))


def maybe_purge():
    """The throttled daily call (the Audit Log page and the dashboard make it); never raises."""
    try:
        if cache.add(_PURGE_KEY, 1, _PURGE_EVERY_SECONDS):
            return purge_old()
    except Exception:       # noqa: BLE001
        logger.exception("Audit Log clean-up failed (pages are unaffected)")
    return 0
