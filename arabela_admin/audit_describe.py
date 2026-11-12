"""How each admin action reads in the Audit Log (see accounts/audit.py: the @audited decorator calls these after the view answers).

Every `describe_*` function takes the decorator's ctx (request, user, kwargs = the URL arguments, body = what the request sent, data = the
JSON the view answered, before = the state captured before the view ran) and returns {"summary", "target", "details"}:
  * summary -- a past-tense phrase WITHOUT the staff member's name ("approved booking RSV-2026-0007"); the decorator puts the name in front.
  * target  -- (type, id, label) of the booking / gown / customer / staff account the action was about.
  * details -- what the owner sees in the dropdown: before -> after, the reason, counts. Never a password, token or picture.
A describe function may raise or find nothing: the decorator then still writes a plain line, so a line is never skipped.
The `before_*` functions run BEFORE the view and capture what is about to change.
"""
from reservations.models import ReceiptRecord, Reservation, ReservationItem


# ---------------------------------------------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------------------------------------------
def _reservation(pk):
    return Reservation.objects.select_related("customer__profile").filter(pk=pk).first()


def _booking_target(reservation):
    return ("booking", reservation.pk, reservation.reference_code) if reservation else ("booking", "", "")


def _booking_text(reservation):
    if not reservation:
        return "a booking"
    return f"booking {reservation.reference_code} ({reservation.display_customer_name})"


def _item(item_id):
    return ReservationItem.objects.select_related("reservation__customer__profile", "gown").filter(pk=item_id).first()


def _item_text(item):
    if not item:
        return "a gown"
    return f"{item.gown_name} on booking {item.reservation.reference_code}"


def _gown(gown_id):
    from gowns.models import Gown
    return Gown.objects.filter(pk=gown_id).first()


def _gown_text(gown):
    return f"gown {gown.gown_id} ({gown.name})" if gown else "a gown"


def _keep(body, *keys):
    return {k: body[k] for k in keys if k in body and body[k] not in (None, "")}


def _date_text(value):
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else "")


# ---------------------------------------------------------------------------------------------------------------------------------
# bookings
# ---------------------------------------------------------------------------------------------------------------------------------
def booking_create(ctx):
    reference = ctx.data.get("reference") or ctx.data.get("reference_code") or ""
    reservation = Reservation.objects.filter(reference_code=reference).select_related("customer__profile").first() if reference else None
    text = _booking_text(reservation) if reservation else "a new walk-in booking"
    return {"summary": f"recorded {text}", "target": _booking_target(reservation),
            "details": _keep(ctx.data, "reference", "reference_code")}


def booking_approve(ctx):
    reservation = _reservation(ctx.kwargs.get("pk"))
    return {"summary": f"approved {_booking_text(reservation)}", "target": _booking_target(reservation)}


def booking_reject(ctx):
    reservation = _reservation(ctx.kwargs.get("pk"))
    return {"summary": f"rejected {_booking_text(reservation)}", "target": _booking_target(reservation),
            "details": _keep(ctx.body, "reason", "note")}


def booking_cancel(ctx):
    reservation = _reservation(ctx.kwargs.get("pk"))
    return {"summary": f"cancelled {_booking_text(reservation)}", "target": _booking_target(reservation),
            "details": _keep(ctx.body, "reason", "note"), "sensitive": True}


def booking_cancel_gowns(ctx):
    reservation = _reservation(ctx.kwargs.get("pk"))
    gowns = ctx.data.get("gowns") or ""
    count = ctx.data.get("count") or len(ctx.body.get("items") or [])
    return {"summary": f"cancelled {count} gown{'s' if count != 1 else ''} ({gowns}) on {_booking_text(reservation)}".replace(" ()", ""),
            "target": _booking_target(reservation), "details": {**_keep(ctx.body, "reason", "note"), "gowns": gowns,
                                                                "booking_cancelled": bool(ctx.data.get("booking_cancelled"))},
            "sensitive": True}


def booking_owner_check(ctx):
    reservation = _reservation(ctx.kwargs.get("pk"))
    result = ctx.data.get("result") or ctx.body.get("result") or ""
    verdict = "confirmed the GCash payment was received for" if result == "received" else "found no GCash payment for"
    return {"summary": f"{verdict} {_booking_text(reservation)}", "target": _booking_target(reservation),
            "details": {"result": result, "outcome": ctx.data.get("outcome", "")}}


def before_item(request, kwargs):
    item = _item(kwargs.get("item_id"))
    if not item:
        return {}
    return {"pickup": _date_text(item.rental_date), "return": _date_text(item.return_date), "stage": item.stage}


def item_pickup(ctx):
    item = _item(ctx.kwargs.get("item_id"))
    return {"summary": f"marked {_item_text(item)} as picked up", "target": _booking_target(item.reservation if item else None)}


def item_return(ctx):
    item = _item(ctx.kwargs.get("item_id"))
    return {"summary": f"marked {_item_text(item)} as returned", "target": _booking_target(item.reservation if item else None),
            "details": _keep(ctx.body, "condition", "result", "note", "notes")}


def item_undo_pickup(ctx):
    item = _item(ctx.kwargs.get("item_id"))
    return {"summary": f"undid the pick-up of {_item_text(item)}", "target": _booking_target(item.reservation if item else None)}


def item_reminder(ctx):
    item = _item(ctx.kwargs.get("item_id"))
    return {"summary": f"sent a reminder about {_item_text(item)}", "target": _booking_target(item.reservation if item else None)}


def item_change_pickup(ctx):
    item = _item(ctx.kwargs.get("item_id"))
    old = ctx.before.get("pickup", "")
    new = _date_text(item.rental_date) if item else ""
    return {"summary": f"changed the pick-up date of {_item_text(item)} from {old} to {new}",
            "target": _booking_target(item.reservation if item else None),
            "details": {"pick-up date": [old, new], "return date": [ctx.before.get("return", ""), _date_text(item.return_date) if item else ""]}}


def item_change_return(ctx):
    item = _item(ctx.kwargs.get("item_id"))
    old = ctx.before.get("return", "")
    new = _date_text(item.return_date) if item else ""
    return {"summary": f"changed the return date of {_item_text(item)} from {old} to {new}",
            "target": _booking_target(item.reservation if item else None), "details": {"return date": [old, new]}}


# ---------------------------------------------------------------------------------------------------------------------------------
# receipts (Payments)
# ---------------------------------------------------------------------------------------------------------------------------------
def receipt_upload(ctx):
    code = (ctx.body.get("reference_code") or "").strip().upper()
    reservation = Reservation.objects.filter(reference_code=code).select_related("customer__profile").first()
    receipt = ctx.data.get("receipt") or {}
    return {"summary": f"attached a receipt photo to {_booking_text(reservation)}", "target": _booking_target(reservation),
            "details": {"covers": receipt.get("covers", ""), "photo": receipt.get("photoUrl", "")}}


def before_receipt(request, kwargs):
    receipt = ReceiptRecord.objects.select_related("reservation__customer__profile").filter(pk=kwargs.get("receipt_id")).first()
    if not receipt:
        return {}
    return {"photo": receipt.photo_url, "reference": receipt.reservation.reference_code, "booking": receipt.reservation_id,
            "name": receipt.reservation.display_customer_name, "covers": receipt.covers,
            "uploaded_by": receipt.uploaded_by_id, "uploaded_at": receipt.uploaded_at.isoformat()}


def _before_booking_target(ctx):
    return ("booking", ctx.before.get("booking", ""), ctx.before.get("reference", ""))


def receipt_replace(ctx):
    receipt = ctx.data.get("receipt") or {}
    return {"summary": f"replaced the receipt photo on booking {ctx.before.get('reference', '')} ({ctx.before.get('name', '')})",
            "target": _before_booking_target(ctx),
            "details": {"photo before": ctx.before.get("photo", ""), "photo after": receipt.get("photoUrl", "")}}


def receipt_delete(ctx):
    return {"summary": f"deleted a receipt photo from booking {ctx.before.get('reference', '')} ({ctx.before.get('name', '')})",
            "target": _before_booking_target(ctx), "sensitive": True,
            "details": {"photo": ctx.before.get("photo", ""), "covers": ctx.before.get("covers", ""),
                        "deleted within the 15-minute undo": bool(ctx.data.get("undo"))}}


# ---------------------------------------------------------------------------------------------------------------------------------
# gowns
# ---------------------------------------------------------------------------------------------------------------------------------
_GOWN_FIELDS = ("name", "category", "color_name", "size", "design_variant", "rental_price", "condition", "status", "notes")


def before_gown(request, kwargs):
    gown = _gown(kwargs.get("gown_id"))
    return {field: str(getattr(gown, field)) for field in _GOWN_FIELDS} if gown else {}


def gown_create(ctx):
    gown_id = ctx.data.get("gown_id") or (ctx.data.get("gown") or {}).get("gown_id") or ctx.data.get("id") or ""
    return {"summary": f"added a new gown{f' {gown_id}' if gown_id else ''}", "target": ("gown", "", str(gown_id)),
            "details": _keep(ctx.body, "name", "category", "color_name", "size", "rental_price", "status")}


def gown_update(ctx):
    gown = _gown(ctx.kwargs.get("gown_id"))
    changes = {}
    if gown and ctx.before:
        for field in _GOWN_FIELDS:
            old, new = ctx.before.get(field, ""), str(getattr(gown, field))
            if old != new:
                changes[field] = [old, new]
    return {"summary": f"edited {_gown_text(gown)}", "target": ("gown", gown.pk if gown else "", gown.gown_id if gown else ""),
            "details": changes}


def gown_status(ctx):
    gown = _gown(ctx.kwargs.get("gown_id"))
    old = ctx.before.get("status", "")
    new = str(gown.status) if gown else ctx.body.get("status", "")
    return {"summary": f"changed {_gown_text(gown)} from {old} to {new}", "target": ("gown", gown.pk if gown else "", gown.gown_id if gown else ""),
            "details": {"status": [old, new]}}


def before_gown_delete(request, kwargs):
    gown = _gown(kwargs.get("gown_id"))
    return {"gown_id": gown.gown_id, "name": gown.name, "category": gown.category} if gown else {}


def gown_delete(ctx):
    label = ctx.before.get("gown_id", "")
    return {"summary": f"deleted gown {label} ({ctx.before.get('name', '')})", "target": ("gown", ctx.kwargs.get("gown_id", ""), label),
            "sensitive": True, "details": {**_keep(ctx.body, "reason", "note"), "category": ctx.before.get("category", "")}}


def before_gown_bulk(request, kwargs):
    """The gown numbers the bulk action was sent for, read before a delete makes them disappear."""
    import json
    from gowns.models import Gown
    try:
        data = json.loads(request.body or b"{}")
        ids = [int(x) for x in data.get("ids", [])][:200]
    except Exception:       # noqa: BLE001
        return {}
    return {"gowns": list(Gown.objects.filter(id__in=ids).order_by("gown_id").values_list("gown_id", flat=True))}


def gown_bulk(ctx):
    """ONE line for the whole batch: how many, and the first ten gown numbers."""
    action = ctx.body.get("action", "")
    numbers = ctx.before.get("gowns", [])
    shown = ", ".join(numbers[:10]) + (f" and {len(numbers) - 10} more" if len(numbers) > 10 else "")
    if action == "delete":
        count = ctx.data.get("deleted", 0)
        skipped = ctx.data.get("skipped") or []
        return {"summary": f"deleted {count} gown{'s' if count != 1 else ''} at once", "sensitive": True,
                "details": {"gowns": shown, **_keep(ctx.body, "reason", "note"), "skipped (still booked)": len(skipped) or ""}}
    count = ctx.data.get("updated", 0)
    if action == "checked":
        return {"summary": f"marked {count} gown{'s' if count != 1 else ''} as checked at once", "details": {"gowns": shown}}
    status = ctx.body.get("status", "")
    return {"summary": f"changed {count} gown{'s' if count != 1 else ''} to {status} at once", "details": {"gowns": shown, "status": status}}


def gown_photo(ctx):
    gown = _gown(ctx.kwargs.get("gown_id"))
    return {"summary": f"changed the photo of {_gown_text(gown)}", "target": ("gown", gown.pk if gown else "", gown.gown_id if gown else "")}


def gown_block_create(ctx):
    gown = _gown(ctx.kwargs.get("gown_id"))
    return {"summary": f"blocked dates on {_gown_text(gown)}", "target": ("gown", gown.pk if gown else "", gown.gown_id if gown else ""),
            "details": _keep(ctx.body, "start_date", "end_date", "reason", "note")}


def gown_block_delete(ctx):
    return {"summary": "released a blocked-dates period on a gown", "details": {"block": ctx.kwargs.get("block_id", "")}}


def tag_colors(ctx):
    return {"summary": "changed the category tag colors", "details": {"colors": ctx.body.get("colors", {})}}


# ---------------------------------------------------------------------------------------------------------------------------------
# customers
# ---------------------------------------------------------------------------------------------------------------------------------
def _customer(user_id):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.filter(pk=user_id).first()


def _customer_name(user):
    from accounts.models import UserProfile
    return UserProfile.customer_display_name(user) if user else "a customer"


def customer_flag(ctx):
    user = _customer(ctx.kwargs.get("user_id"))
    flagged = bool(ctx.body.get("flagged"))
    return {"summary": f"{'flagged' if flagged else 'unflagged'} the account of {_customer_name(user)}",
            "target": ("customer", user.pk if user else "", _customer_name(user)), "sensitive": True,
            "details": {"reason": ctx.body.get("reason", "")}}


def customer_unlock(ctx):
    user = _customer(ctx.kwargs.get("user_id"))
    return {"summary": f"lifted the cancellation lock on {_customer_name(user)}'s account",
            "target": ("customer", user.pk if user else "", _customer_name(user)), "sensitive": True}


# ---------------------------------------------------------------------------------------------------------------------------------
# staff accounts, own account, profile
# ---------------------------------------------------------------------------------------------------------------------------------
def _staff_target(ctx):
    user = _customer(ctx.kwargs.get("user_id"))
    if user is None:
        return None, ("staff", ctx.kwargs.get("user_id", ""), "")
    from accounts.roles import staff_display_name
    return user, ("staff", user.pk, staff_display_name(user))


def before_staff(request, kwargs):
    from accounts.models import UserProfile
    from accounts.roles import PERMISSION_KEYS, granted_permissions, staff_display_name
    user = _customer(kwargs.get("user_id"))
    if user is None:
        return {}
    return {"name": staff_display_name(user), "active": user.is_active, "permissions": sorted(granted_permissions(user))}


def staff_create(ctx):
    account = ctx.data.get("account") or {}
    return {"summary": f"created the staff account {account.get('name', '')} ({account.get('username', '')})".replace(" ()", ""),
            "target": ("staff", account.get("id", ""), account.get("name", "")), "sensitive": True,
            "details": {"allowed to": account.get("permissions", [])}}


def staff_update(ctx):
    account = ctx.data.get("account") or {}
    before = ctx.before or {}
    now = sorted(account.get("permissions", []))
    changes = {}
    if before.get("permissions") != now:
        changes["allowed to"] = [before.get("permissions", []), now]
    if before.get("name") and before.get("name") != account.get("name"):
        changes["name"] = [before.get("name"), account.get("name")]
    if ctx.body.get("password"):
        changes["sign-in"] = "password was reset"          # never the password itself (and the log drops any key that looks like one)
    return {"summary": f"changed the staff account of {account.get('name') or before.get('name', '')}",
            "target": ("staff", account.get("id", ctx.kwargs.get("user_id", "")), account.get("name", "")), "sensitive": True,
            "details": changes}


def staff_status(ctx):
    account = ctx.data.get("account") or {}
    active = account.get("is_active")
    return {"summary": f"{'activated' if active else 'deactivated'} the staff account of {account.get('name', '')}",
            "target": ("staff", account.get("id", ctx.kwargs.get("user_id", "")), account.get("name", "")), "sensitive": True}


def staff_delete(ctx):
    return {"summary": f"deleted the staff account of {ctx.before.get('name', '')}",
            "target": ("staff", ctx.kwargs.get("user_id", ""), ctx.before.get("name", "")), "sensitive": True}


def change_password(ctx):
    return {"summary": "changed their own password", "sensitive": True}


def change_username(ctx):
    return {"summary": f"changed their own username to {ctx.data.get('username', '')}", "sensitive": True}


def recovery_email(ctx):
    return {"summary": "set the recovery email for password reset", "sensitive": True}


def save_profile(ctx):
    return {"summary": "saved their profile", "details": _keep(ctx.body, "first_name", "last_name", "email")}


def profile_picture(ctx):
    return {"summary": "changed their profile picture"}


def profile_picture_remove(ctx):
    return {"summary": "removed their profile picture"}


def avatar_position(ctx):
    return {"summary": "moved their profile picture"}


# ---------------------------------------------------------------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------------------------------------------------------------
def before_settings(request, kwargs):
    from gowns.models import SiteSettings
    s = SiteSettings.load()
    return {"gcash_account_name": s.gcash_account_name, "gcash_number": s.gcash_number, "deposit": s.security_deposit_per_gown,
            "gcash_qr": bool(s.gcash_qr_url)}


def gcash_details(ctx):
    from gowns.models import SiteSettings
    s = SiteSettings.load()
    changes = {}
    if ctx.before.get("gcash_account_name") != s.gcash_account_name:
        changes["GCash account name"] = [ctx.before.get("gcash_account_name", ""), s.gcash_account_name]
    if ctx.before.get("gcash_number") != s.gcash_number:
        changes["GCash number"] = [ctx.before.get("gcash_number", ""), s.gcash_number]
    return {"summary": "changed the shop's GCash details", "sensitive": True, "details": changes}


def deposit_setting(ctx):
    from gowns.models import SiteSettings
    new = SiteSettings.load().security_deposit_per_gown
    return {"summary": f"changed the security deposit per gown from ₱{ctx.before.get('deposit', '')} to ₱{new}", "sensitive": True,
            "details": {"deposit per gown": [ctx.before.get("deposit", ""), new]}}


def gcash_qr_upload(ctx):
    return {"summary": "uploaded a new GCash QR code", "sensitive": True}


def gcash_qr_remove(ctx):
    return {"summary": "removed the GCash QR code", "sensitive": True}


def email_test(ctx):
    return {"summary": "sent a test email", "details": _keep(ctx.body, "kind")}


# categories
def category_create(ctx):
    return {"summary": f"added the category {ctx.body.get('name', '')}", "details": _keep(ctx.body, "name", "audience")}


def category_delete(ctx):
    return {"summary": "removed a custom category", "details": {"category": ctx.kwargs.get("category_id", "")}, "sensitive": True}


def category_audience(ctx):
    return {"summary": "changed who a custom category is for", "details": _keep(ctx.body, "audience")}


def category_remove_builtin(ctx):
    return {"summary": f"hid the built-in category {ctx.body.get('name', '')}", "details": _keep(ctx.body, "name"), "sensitive": True}


def category_cover_upload(ctx):
    return {"summary": f"changed the picture of the category {ctx.body.get('key', ctx.body.get('category', ''))}".replace(" the category ", " a category picture ").strip()}


def category_cover_reset(ctx):
    return {"summary": "reset a category picture to the original"}


# cancellation requests
def _cr_phrase(whole, what, reference):
    """'booking RSV-2026-0008' for a whole booking, 'Gown A, Gown B on booking RSV-2026-0008' for some gowns."""
    return f"booking {reference}" if whole else f"{what or 'some gowns'} on booking {reference}"


def _cr_facts(ctx):
    """(requested_by, whole, what, reference, reservation id, reason) of the request the URL names -- read from the database, so a refused
    or failed answer (which carries no data) still reads in words."""
    from reservations import cancel_requests
    from reservations.models import CancelRequest
    waiting = CancelRequest.objects.select_related("reservation").filter(pk=ctx.kwargs.get("request_id")).first()
    if waiting is None:
        return "", True, "", "", "", ""
    return (waiting.requested_by_name, waiting.is_whole_booking, cancel_requests.what_text(waiting), waiting.reservation.reference_code,
            waiting.reservation_id, cancel_requests.reason_text(waiting))


def cancel_request_create(ctx):
    reservation = _reservation(ctx.kwargs.get("pk"))
    request_data = ctx.data.get("request") or {}
    ref = reservation.reference_code if reservation else ""
    phrase = _cr_phrase(request_data.get("whole", True), request_data.get("what", ""), ref)
    name = f" ({reservation.display_customer_name})" if reservation else ""
    return {"summary": f"asked the owner to cancel {phrase}{name}", "target": _booking_target(reservation),
            "details": {**_keep(ctx.body, "reason", "note")}, "sensitive": True}


def cancel_request_approve(ctx):
    who, whole, what, ref, res_id, reason = _cr_facts(ctx)
    target = ("booking", res_id, ref)
    if ctx.data.get("outcome") == "closed":
        return {"summary": f"tried to approve the request of {who} for booking {ref}, but it was no longer possible", "target": target,
                "details": {"why": ctx.data.get("message", "")}, "sensitive": True}
    details = {"reason": reason, "cancelled": ctx.data.get("gowns", "") or "the whole booking"} if ctx.outcome == "ok" else {}
    return {"summary": f"approved the request of {who} to cancel {_cr_phrase(whole, what, ref)}", "target": target,
            "details": details, "sensitive": True}


def cancel_request_decline(ctx):
    who, whole, what, ref, res_id, _reason = _cr_facts(ctx)
    return {"summary": f"declined the request of {who} to cancel {_cr_phrase(whole, what, ref)}", "target": ("booking", res_id, ref),
            "details": {"note": ctx.data.get("note", "") or ctx.body.get("note", "")}, "sensitive": True}


def cancel_request_withdraw(ctx):
    who, whole, what, ref, res_id, _reason = _cr_facts(ctx)
    return {"summary": f"withdrew their request to cancel {_cr_phrase(whole, what, ref)}", "target": ("booking", res_id, ref)}
