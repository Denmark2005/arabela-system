"""What the site remembers between two tries of the SAME receipt picture, per browser session (2026-11-19).

Two things, both keyed by the picture's own hash, so a different picture always starts fresh:

  * "try again" (gowns/receipt_check.STATUS_TRY_AGAIN): the first reading said "refuse" and the second could not be made in time. The customer is asked
    to try again; the first reading is kept here, so the second try does not read (or wait for) it again and gets a fresh time budget for the second
    reading. If the second try cannot finish either, the booking goes to the staff, so an honest customer is never stuck.
  * "refused as too old on one reading": if the customer sends the very same picture again, it takes the slower two-reading path, so a receipt whose
    date was misread cannot refuse an honest customer twice.

Small on purpose (a handful of pictures, one short reading each): it lives in the session."""
import json

SESSION_KEY = "receipt_tries"
MAX_KEPT = 5


def _all(session):
    data = session.get(SESSION_KEY)
    return dict(data) if isinstance(data, dict) else {}


def _count(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _entry(data, image_hash):
    """A copy of what is kept for one picture; anything damaged in the session counts as nothing."""
    entry = data.get(image_hash)
    return dict(entry) if isinstance(entry, dict) else {}


def state(session, image_hash):
    """{"count": tries so far, "old_refused": bool, "first": the first reading as JSON text ('' when there is none)} for this picture."""
    entry = _all(session).get(image_hash)
    if not isinstance(entry, dict):
        return {"count": 0, "old_refused": False, "first": ""}
    return {"count": _count(entry.get("n")), "old_refused": bool(entry.get("old")), "first": str(entry.get("first") or "")}


def _save(session, data):
    while len(data) > MAX_KEPT:                       # forget the oldest picture first
        data.pop(next(iter(data)))
    session[SESSION_KEY] = data


def remember_try_again(session, image_hash, first_reading_json):
    """The first reading of this picture would have refused it and the second could not be made: keep the first, count one try. Returns the count."""
    data = _all(session)
    entry = _entry(data, image_hash)
    entry["n"] = _count(entry.get("n")) + 1
    entry["first"] = first_reading_json or ""
    data.pop(image_hash, None)
    data[image_hash] = entry
    _save(session, data)
    return entry["n"]


def remember_old_refusal(session, image_hash):
    data = _all(session)
    entry = _entry(data, image_hash)
    entry["old"] = True
    data.pop(image_hash, None)
    data[image_hash] = entry
    _save(session, data)


def forget(session, image_hash):
    data = _all(session)
    if image_hash in data:
        data.pop(image_hash)
        session[SESSION_KEY] = data


def first_reading(session, image_hash, builder):
    """The first reading kept for this picture, rebuilt with `builder(dict)` (gowns.receipt_check.reading_from_dict), or None when there is none
    or what was kept cannot be read back."""
    kept = state(session, image_hash)["first"]
    if not kept:
        return None
    try:
        return builder(json.loads(kept))
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
