"""The owner's Audit Log page, its CSV export and the "Staff activity" summary (the lines themselves: accounts/audit.py).

Kept out of views.py on purpose: it is a self-contained feature, and views.py is already very large. Owner only -- a staff account that
opens the address is sent to the dashboard (the same rule as Staff Management)."""
import csv
import datetime as dt

from django.contrib.auth import get_user_model
from django.db.models import Count, Max, Q
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from accounts import audit
from accounts.models import AuditLog
from accounts.roles import staff_display_name

from .views import _require_owner

User = get_user_model()

MAX_ROWS = 500           # shown on the page, 15 at a time by pager.js; the CSV holds more
MAX_EXPORT_ROWS = 10000
RANGES = (("today", "Today"), ("7", "Last 7 days"), ("30", "Last 30 days"), ("all", "Everything kept"), ("custom", "Pick dates..."))
DEFAULT_RANGE = "7"
_CATEGORY_ORDER = [value for value, _label in AuditLog.Category.choices]

# Where a line's "about" chip leads: the page that lists that kind of thing, searched for the label.
_TARGET_PAGES = {
    "booking": "arabela_admin:reservation_records",
    "gown": "arabela_admin:gown_catalog",
    "customer": "arabela_admin:clients",
    "staff": "arabela_admin:staff_management",
}


def _local_midnight(day):
    return timezone.make_aware(dt.datetime.combine(day, dt.time.min))


def _parse_day(text):
    try:
        return dt.date.fromisoformat((text or "").strip())
    except ValueError:
        return None


def read_filters(params):
    """The filters in the address, cleaned. Anything unknown falls back to 'show everything' rather than an error."""
    category = params.get("category", "all")
    if category not in _CATEGORY_ORDER:
        category = "all"
    staff = params.get("staff", "all")
    staff = int(staff) if staff.isdigit() else "all"
    range_key = params.get("range", DEFAULT_RANGE)
    if range_key not in dict(RANGES):
        range_key = DEFAULT_RANGE
    start, end = _parse_day(params.get("from")), _parse_day(params.get("to"))
    if range_key == "custom" and start is None and end is None:
        range_key = DEFAULT_RANGE
    return {
        "category": category, "staff": staff, "range": range_key,
        "from": start.isoformat() if start else "", "to": end.isoformat() if end else "",
        "q": (params.get("q") or "").strip()[:100],
        "important": params.get("important") == "1",
    }


def _base_queryset(filters, now=None):
    """Every line that matches the filters EXCEPT the category (the category buttons count against this)."""
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    rows = AuditLog.objects.all()
    key = filters["range"]
    if key == "today":
        rows = rows.filter(created_at__gte=_local_midnight(today))
    elif key in ("7", "30"):
        rows = rows.filter(created_at__gte=_local_midnight(today - dt.timedelta(days=int(key) - 1)))
    elif key == "custom":
        if filters["from"]:
            rows = rows.filter(created_at__gte=_local_midnight(dt.date.fromisoformat(filters["from"])))
        if filters["to"]:
            rows = rows.filter(created_at__lt=_local_midnight(dt.date.fromisoformat(filters["to"]) + dt.timedelta(days=1)))
    if filters["staff"] != "all":
        rows = rows.filter(actor_id=filters["staff"])
    if filters["q"]:
        rows = rows.filter(Q(summary__icontains=filters["q"]) | Q(target_label__icontains=filters["q"]) | Q(actor_name__icontains=filters["q"]))
    if filters["important"]:
        rows = rows.filter(Q(sensitive=True) | ~Q(outcome=AuditLog.Outcome.OK))
    return rows


def filtered_queryset(filters, now=None):
    rows = _base_queryset(filters, now)
    if filters["category"] != "all":
        rows = rows.filter(category=filters["category"])
    return rows


def _tone(row):
    if row.outcome != AuditLog.Outcome.OK:
        return "bad"
    return "sensitive" if row.sensitive else "normal"


def _when(moment):
    local = timezone.localtime(moment)
    return f"{date_format(local, 'M j')}, {date_format(local, 'g:i A')}"


def _detail_rows(details):
    """[(label, before, after)] for a change and [(label, value, None)] for a plain fact, for the opened line."""
    rows = []
    for key, value in (details or {}).items():
        label = str(key).replace("_", " ").capitalize()
        if isinstance(value, list) and len(value) == 2 and not any(isinstance(v, (list, dict)) for v in value):
            rows.append((label, "—" if value[0] in ("", None) else str(value[0]), "—" if value[1] in ("", None) else str(value[1])))
        elif isinstance(value, (list, dict)):
            text = ", ".join(str(v) for v in value) if isinstance(value, list) else "; ".join(f"{k}: {v}" for k, v in value.items())
            rows.append((label, text or "—", None))
        elif isinstance(value, bool):
            rows.append((label, "Yes" if value else "No", None))
        else:
            rows.append((label, "—" if value in ("", None) else str(value), None))
    return rows


def _target_link(row):
    name = _TARGET_PAGES.get(row.target_type)
    if not name or not row.target_label:
        return ""
    from django.utils.http import urlencode
    return reverse(name) + "?" + urlencode({"search": row.target_label})


def _what(row):
    """The sentence without the name in front: the Who column already says who, so the line reads "Approved booking RSV-..." and not
    "Maria Approved booking ..." beside "Maria". (The stored sentence and the CSV keep the name: they stand alone.)"""
    text, who = row.summary, row.actor_name
    if who and text.startswith(f"{who}'s attempt was refused: "):
        text = "Refused: " + text[len(f"{who}'s attempt was refused: "):]
    elif who and text.startswith(who + " "):
        text = text[len(who) + 1:]
    return text[:1].upper() + text[1:]


def row_for_page(row):
    local = timezone.localtime(row.created_at)
    what, link, label = _what(row), _target_link(row), row.target_label
    parts, extra = None, ""
    if label and link and label in what:
        before, _, after = what.partition(label)
        parts = [before, label, after]          # the number in the sentence is the link: nothing repeats it
    elif label and not (label == row.actor_name):
        extra = label                           # a target the sentence does not name: shown in the opened line instead
    return {
        "id": row.id,
        "when": _when(row.created_at),
        "when_full": f"{date_format(local, 'l, F j, Y')} at {date_format(local, 'g:i A')}",
        "who": row.actor_name or "Unknown",
        "role": row.actor_role,
        "category": row.category,
        "summary": row.summary,
        "what": what,
        "what_parts": parts,
        "about_extra": extra,
        "tone": _tone(row),
        "outcome": row.get_outcome_display(),
        "sensitive": row.sensitive,
        "target_label": row.target_label,
        "target_type": row.target_type,
        "target_link": link,
        "details": _detail_rows(row.details),
        "ip": row.ip_address,
        "device": row.device,
    }


def _role_of(user):
    """'Owner' or 'Staff' from the profile already loaded with the account (no query per person)."""
    from accounts.models import UserProfile
    if user.is_superuser:
        return "Owner"
    try:
        return "Owner" if user.profile.role == UserProfile.Role.OWNER else "Staff"
    except UserProfile.DoesNotExist:
        return "Staff"


def staff_activity(now=None):
    """One line per staff account (and the owner): last sign-in, last action, what they did today and over the last 7 days. Four
    grouped queries in all, however many accounts there are."""
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    since_today, since_week = _local_midnight(today), _local_midnight(today - dt.timedelta(days=6))
    work = AuditLog.objects.exclude(category=AuditLog.Category.SIGN_IN).exclude(actor__isnull=True)
    last_in = dict(AuditLog.objects.filter(action="signin.ok", actor__isnull=False).values_list("actor_id").annotate(m=Max("created_at")))
    last_act = dict(work.values_list("actor_id").annotate(m=Max("created_at")))
    today_n = dict(work.filter(created_at__gte=since_today, outcome="ok").values_list("actor_id").annotate(n=Count("id")))
    week = {}
    for actor_id, outcome, sensitive, action in work.filter(created_at__gte=since_week).values_list("actor_id", "outcome", "sensitive", "action"):
        entry = week.setdefault(actor_id, {"actions": 0, "refused": 0, "important": 0, "requests": 0})
        if outcome == "ok":
            entry["actions"] += 1
        else:
            entry["refused"] += 1
        if sensitive:
            entry["important"] += 1
        if action.startswith("cancel_request."):
            entry["requests"] += 1
    rows = []
    for user in User.objects.filter(is_staff=True).select_related("profile").order_by("-is_superuser", "first_name", "username"):
        entry = week.get(user.id, {})
        rows.append({
            "id": user.id,
            "name": staff_display_name(user),
            "role": _role_of(user),
            "active": user.is_active,
            "last_in": _when(last_in[user.id]) if user.id in last_in else "Never",
            "last_action": _when(last_act[user.id]) if user.id in last_act else "—",
            "today": today_n.get(user.id, 0),
            "week": entry.get("actions", 0),
            "refused": entry.get("refused", 0),
            "important": entry.get("important", 0),
            "requests": entry.get("requests", 0),
        })
    return rows


def today_strip(now=None):
    """The dashboard's one-line summary for the owner: what staff did today. Only STAFF accounts count (not her own work)."""
    now = now or timezone.now()
    since = _local_midnight(timezone.localtime(now).date())
    rows = AuditLog.objects.filter(created_at__gte=since, actor_role="Staff").exclude(category=AuditLog.Category.SIGN_IN)
    done = rows.filter(outcome="ok")
    return {
        "actions": done.count(),
        "people": done.values("actor_name").distinct().count(),
        "important": rows.filter(Q(sensitive=True) | ~Q(outcome="ok")).count(),
    }


@_require_owner
def audit_log_view(request):
    audit.maybe_purge()
    filters = read_filters(request.GET)
    base = _base_queryset(filters)
    counts = {row["category"]: row["n"] for row in base.values("category").annotate(n=Count("id"))}
    rows = filtered_queryset(filters)
    total = rows.count()
    shown = [row_for_page(row) for row in rows.select_related("actor")[:MAX_ROWS]]
    first = AuditLog.objects.order_by("created_at").values_list("created_at", flat=True).first()
    categories = [("all", "All", sum(counts.values()))] + [(value, value, counts.get(value, 0)) for value in _CATEGORY_ORDER]

    def query(**changes):
        """The filters as an address query (a category button, the export): this page's filters with some changed."""
        from django.utils.http import urlencode
        params = {"category": filters["category"], "staff": filters["staff"], "range": filters["range"], "from": filters["from"],
                  "to": filters["to"], "q": filters["q"], "important": "1" if filters["important"] else ""}
        params.update(changes)
        clean = {k: v for k, v in params.items() if v not in ("", "all", None)}
        if clean.get("range") == DEFAULT_RANGE:
            clean.pop("range")
        return urlencode(clean)

    def link(**changes):
        return "?" + query(**changes)

    return render(request, "arabela_admin/audit-log.html", {
        "page": "audit-log",
        "filters": filters,
        "rows": shown,
        "total": total,
        "capped": total > MAX_ROWS,
        "max_rows": MAX_ROWS,
        "categories": [{"key": key, "label": label, "count": count, "link": link(category=key)} for key, label, count in categories],
        "important_link": link(important="" if filters["important"] else "1"),
        "export_link": reverse("arabela_admin:audit_log_export") + link(),
        "ranges": RANGES,
        "staff_choices": [{"id": u.id, "name": staff_display_name(u)} for u in User.objects.filter(is_staff=True).order_by("first_name", "username")],
        "starts_on": date_format(timezone.localtime(first), "M j, Y") if first else "",
        "activity": staff_activity(),
    })


def _csv_cell(value):
    """A cell a spreadsheet cannot run as a formula: text starting with = + - @ gets an apostrophe in front."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@_require_owner
def audit_log_export_view(request):
    filters = read_filters(request.GET)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="audit-log-{timezone.localdate().isoformat()}.csv"'
    response.write("﻿")        # so Excel reads the peso sign and accents right
    writer = csv.writer(response)
    writer.writerow(["When", "Who", "Role", "Category", "What happened", "About", "Result", "Important", "Details", "Address", "Device"])
    for row in filtered_queryset(filters)[:MAX_EXPORT_ROWS]:
        details = "; ".join(
            f"{label}: {before}" + (f" -> {after}" if after is not None else "") for label, before, after in _detail_rows(row.details)
        )
        writer.writerow([_csv_cell(v) for v in (
            timezone.localtime(row.created_at).strftime("%Y-%m-%d %H:%M"), row.actor_name, row.actor_role, row.category, row.summary,
            row.target_label, row.get_outcome_display(), "Yes" if row.sensitive or row.outcome != "ok" else "No", details,
            row.ip_address, row.device)])
    return response
