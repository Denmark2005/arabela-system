"""The admin side of ID verification (the work itself is reservations/identity.py):

OWNER ONLY -- the ID Checks page, the pictures, the decision on a check Didit sent for review, and the on/off switch:
  * GET  id-checks/                          the page: checks needing a decision, and every check
  * GET  api/id-checks/<pk>/open/            opens one check for the owner and hands out five-minute links to its pictures (written to the Audit Log)
  * GET  api/id-checks/photo/<token>/        one picture (owner's session AND the link's own signature must agree; never cached)
  * POST api/id-checks/<pk>/decide/          approve / decline a check waiting for review
  * POST api/id-checks/<pk>/delete-photos/   delete the pictures now
  * POST api/id-checks/<pk>/retry-photos/    copy the pictures again from Didit
  * POST api/id-setting/                     "Require a valid ID before booking" (on / off)
ANY SIGNED-IN STAFF -- the walk-in ID row of New Booking:
  * POST api/bookings/id/start/              a QR code for the customer's own phone
  * POST api/bookings/id/counter/            "I checked the original ID at the counter"
  * GET  api/bookings/id/<token>/status/     what the screen shows for that QR code
  * POST api/bookings/id/<token>/cancel/     close the QR code

Staff never see a picture. A picture is opened only through the owner's session and a signed link good for five minutes, and every opening is in the Audit Log."""
import json
import logging

from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from accounts.audit import audited
from arabela_admin import audit_describe
from arabela_admin.views import _is_admin_staff, _is_owner, _require_owner, _staff_display_name
from gowns import online_checkout, walkin_payment
from gowns.models import SiteSettings
from reservations import didit, identity, identity_photos
from reservations.models import IdentityCheck, IdentityPhoto

logger = logging.getLogger(__name__)
S = IdentityCheck.Status
LIST_LIMIT = 300


def _json_body(request):
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _error(exc):
    return JsonResponse({"error": exc.message, **exc.extra}, status=exc.status)


def _unauthorized():
    return JsonResponse({"error": "Unauthorized"}, status=401)


def _owner_only_json(request):
    """None for the signed-in owner; else the refusal (401 for nobody signed in, 403 for staff who are not the owner)."""
    if not _is_admin_staff(request):
        return _unauthorized()
    if not _is_owner(request):
        return JsonResponse({"error": "Only the owner can do that."}, status=403)
    return None


def _who(check):
    """The name to show for a check (reservations/identity.py: the customer's profile name, else the name typed at the shop)."""
    return identity.who(check)


def _day(moment):
    if not moment:
        return ""
    local = timezone.localtime(moment)
    return f"{local:%b} {local.day}, {local.year}"


# Didit's TEST mode (a sandbox key) sends SAMPLE data for the name and the kind of ID whatever is scanned (a Philippine National ID comes back as a Brazilian passport), so
# comparing them with what the customer gave only shows false warnings. They are compared (and shown) once a real key is used.
TEST_MODE_COMPARE_NOTE = "Not compared (Didit's test mode sends sample names and kinds of ID)"


def _row(check):
    kind, words = check.badge
    bookings = [r.reference_code for r in check.reservations.all()][:3]
    flags = []
    if not didit.is_test():
        if check.name_check == "differs":
            flags.append("The name on the ID differs from the name given")
        if check.type_check == "differs":
            flags.append("The ID scanned is not the kind the customer chose")
    return {
        "id": check.pk, "who": _who(check), "phone": check.subject_phone or "", "email": check.customer.email if check.customer_id else "",
        "source": check.get_source_display(), "idType": check.get_id_type_display(), "status": check.status, "kind": kind, "words": words,
        "when": _day(check.decided_at or check.created_at), "bookings": bookings, "flags": flags, "reasons": list(check.reasons or []),
        "photos": check.photos_state, "hasPhotos": check.photos_state == "stored",
        "needsReview": check.status == S.REVIEW,
        "decidedBy": _staff_display_name(check.decided_by) if check.decided_by_id and check.decision_source in ("owner", "counter") else "",
        "note": check.note,
    }


# ---------------------------------------------------------------- the owner's page
@_require_owner
def id_checks_view(request):
    identity.run_cleanup_if_due()                     # the shop has no scheduler: the daily cleanup rides on the owner's visits
    show = "all" if request.GET.get("show") == "all" else "review"
    query = (request.GET.get("q") or "").strip()[:80]
    rows = IdentityCheck.objects.select_related("customer__profile", "decided_by").prefetch_related("reservations")
    review_count = IdentityCheck.objects.filter(status=S.REVIEW).count()
    all_count = IdentityCheck.objects.count()
    if show == "review":
        rows = rows.filter(status=S.REVIEW)
    if query:
        digits = identity.phone_digits(query)
        match = (Q(subject_name__icontains=query) | Q(customer__email__icontains=query) | Q(customer__profile__display_name__icontains=query)
                 | Q(reservations__reference_code__icontains=query))
        if len(digits) >= 4:
            match |= Q(subject_phone__contains=digits)
        rows = rows.filter(match).distinct()
    checks = [_row(check) for check in rows.order_by("-created_at", "-id")[:LIST_LIMIT]]
    return render(request, "arabela_admin/id-checks.html", {
        "page": "id-checks", "checks": checks, "show": show, "query": query, "review_count": review_count, "all_count": all_count,
        "setting_on": identity.switch_on(), "didit_on": didit.enabled(), "didit_test": didit.is_test(), "valid_days": 365, "keep_days": 30,
        "open_id": request.GET.get("open", ""),
    })


@require_http_methods(["GET"])
@never_cache
@audited("id.view", "Customers", audit_describe.id_view, sensitive=True)
def id_check_open_view(request, pk):
    """Opens one check for the owner: its facts, and a five-minute link for each picture. This is the moment a picture is "seen", so this is what the Audit Log records."""
    refused = _owner_only_json(request)
    if refused:
        return refused
    check = IdentityCheck.objects.select_related("customer__profile", "decided_by", "staff").filter(pk=pk).first()
    if check is None:
        return JsonResponse({"error": "That ID check was not found. Please refresh the page."}, status=404)
    photos = [{"side": photo.get_side_display(), "url": reverse("arabela_admin:id_photo", args=[identity_photos.view_token(request.user.pk, photo.pk)])}
              for photo in sorted(check.photos.all(), key=lambda p: 0 if p.side == IdentityPhoto.Side.FRONT else 1)]           # the front first, then the back
    kind, words = check.badge
    data = _row(check)
    data.update({
        "success": True, "photosList": photos, "verifiedAt": _day(check.verified_at), "until": _day(check.expires_at),
        "decisionSource": check.get_decision_source_display() if check.decision_source else "", "staff": _staff_display_name(check.staff) if check.staff_id else "",
        "canDecide": check.status == S.REVIEW, "canDelete": bool(photos),
        "canRetry": check.photos_state in ("failed", "pending") and check.status in (S.APPROVED, S.DECLINED, S.REVIEW) and bool(check.session_id),
        "photosWords": {"stored": "Front and back kept", "failed": "The pictures could not be copied", "pending": "The pictures are being copied", "copying": "The pictures are being copied",
                        "deleted": "Deleted", "": "None kept"}.get(check.photos_state, ""),
        "name": check.subject_name,
        "nameWords": "" if didit.is_test() else {"match": "Matches the name given", "differs": "Differs from the name given"}.get(check.name_check, ""),
        "typeWords": "" if didit.is_test() else {"match": "The kind the customer chose", "differs": "Not the kind the customer chose"}.get(check.type_check, ""),
        "compareNote": TEST_MODE_COMPARE_NOTE if didit.is_test() and (check.name_check or check.type_check) else "",
    })
    return JsonResponse(data)


@require_http_methods(["GET"])
def id_photo_view(request, token):
    """One picture. Needs the owner's own session AND a link signed for that owner and that picture a few minutes ago. Never cached, never indexed."""
    if not _is_admin_staff(request) or not _is_owner(request):
        return HttpResponse(status=403)
    parsed = identity_photos.read_view_token(token)
    if parsed is None or parsed[1] != request.user.pk:
        return HttpResponse(status=403)
    photo = IdentityPhoto.objects.filter(pk=parsed[0]).first()
    if photo is None:
        return HttpResponse(status=404)
    try:
        data = identity_photos.read_photo(photo)
    except Exception:       # noqa: BLE001 -- a missing file is a 404, never a crash page
        logger.warning("An ID picture could not be read (check %s)", photo.id_check_id)
        return HttpResponse(status=404)
    response = HttpResponse(data, content_type="image/jpeg")
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["Pragma"] = "no-cache"
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Content-Disposition"] = 'inline; filename="id.jpg"'
    return response


@require_http_methods(["POST"])
@audited("id.decide", "Customers", audit_describe.id_decide, sensitive=True)
def id_check_decide_view(request, pk):
    refused = _owner_only_json(request)
    if refused:
        return refused
    data = _json_body(request)
    if data is None or not isinstance(data.get("approve"), bool):
        return JsonResponse({"error": "Choose Approve or Decline."}, status=400)
    try:
        check = identity.owner_decide(pk, approve=data["approve"], user=request.user, note=data.get("note") or "")
    except identity.IdentityError as exc:
        return _error(exc)
    return JsonResponse({"success": True, "approved": check.status == S.APPROVED, "status": check.status})


@require_http_methods(["POST"])
@audited("id.delete_photos", "Customers", audit_describe.id_delete_photos, sensitive=True)
def id_check_delete_photos_view(request, pk):
    refused = _owner_only_json(request)
    if refused:
        return refused
    check = IdentityCheck.objects.filter(pk=pk).first()
    if check is None:
        return JsonResponse({"error": "That ID check was not found. Please refresh the page."}, status=404)
    deleted = identity_photos.delete_photos(check)
    return JsonResponse({"success": True, "deleted": deleted})


@require_http_methods(["POST"])
@audited("id.retry_photos", "Customers", audit_describe.id_retry_photos)
def id_check_retry_photos_view(request, pk):
    refused = _owner_only_json(request)
    if refused:
        return refused
    check = IdentityCheck.objects.filter(pk=pk).first()
    if check is None:
        return JsonResponse({"error": "That ID check was not found. Please refresh the page."}, status=404)
    if check.photos_state not in ("failed", "pending", "") or check.status not in (S.APPROVED, S.DECLINED, S.REVIEW) or not check.session_id:
        return JsonResponse({"error": "The pictures of this check cannot be copied again."}, status=409)
    kept = identity_photos.copy_photos(check.pk)
    return JsonResponse({"success": True, "kept": bool(kept), "photos": IdentityCheck.objects.get(pk=check.pk).photos_state})


@require_http_methods(["POST"])
@audited("settings.require_id", "Settings", audit_describe.id_setting, sensitive=True, before=audit_describe.before_id_setting)
def id_setting_update_view(request):
    """The owner's switch: a valid ID must be verified before a customer pays online and before a walk-in booking is saved."""
    refused = _owner_only_json(request)
    if refused:
        return refused
    data = _json_body(request)
    if data is None or not isinstance(data.get("require"), bool):
        return JsonResponse({"error": "Invalid request."}, status=400)
    shop = SiteSettings.load()
    shop.require_id_before_payment = data["require"]
    shop.save(update_fields=["require_id_before_payment", "updated_at"])
    return JsonResponse({"success": True, "require": shop.require_id_before_payment})


# ---------------------------------------------------------------- the walk-in ID row (any signed-in staff)
def _callback_for(request):
    def build(token):
        return online_checkout.absolute(request, reverse("gowns:identity_walkin_done", args=[token]))
    return build


@require_http_methods(["POST"])
@audited("id.walkin_start", "Customers", audit_describe.id_walkin_start)
def booking_id_start_view(request):
    """Staff pressed "Scan to verify": a QR code for the customer's own phone (the customer's name and mobile number are asked first: the check is for that customer)."""
    if not _is_admin_staff(request):
        return _unauthorized()
    post = request.POST
    try:
        result = identity.start_walkin(staff=request.user, name=post.get("name"), phone=post.get("phone"), id_type=(post.get("id_type") or "").strip(),
                                       callback_for=_callback_for(request))
    except identity.IdentityError as exc:
        return _error(exc)
    check = IdentityCheck.objects.get(token=result["token"])
    return JsonResponse({
        "success": True, **identity.staff_state(check), "url": result["url"], "qr": walkin_payment.qr_image(result["url"]),
        "statusUrl": reverse("arabela_admin:booking_id_status", args=[check.token]), "cancelUrl": reverse("arabela_admin:booking_id_cancel", args=[check.token]),
        "phone": check.subject_phone,
    })


@require_http_methods(["POST"])
@audited("id.counter_check", "Customers", audit_describe.id_counter)
def booking_id_counter_view(request):
    """Staff say they looked at the customer's ORIGINAL valid ID at the counter. They must tick that they did; their name and the time are kept."""
    if not _is_admin_staff(request):
        return _unauthorized()
    post = request.POST
    if (post.get("confirmed") or "") not in ("1", "true", "on"):
        return JsonResponse({"error": "Please tick the box to say you checked the customer's original ID."}, status=400)
    try:
        check = identity.counter_check(staff=request.user, name=post.get("name"), phone=post.get("phone"), id_type=(post.get("id_type") or "").strip(), note=post.get("note") or "")
    except identity.IdentityError as exc:
        return _error(exc)
    return JsonResponse({"success": True, **identity.staff_state(check), "phone": check.subject_phone, "staffName": _staff_display_name(request.user)})


@never_cache
@require_http_methods(["GET"])
def booking_id_status_view(request, token):
    """What the staff screen shows for a QR code: Didit is asked (at most every couple of seconds), never the screen."""
    if not _is_admin_staff(request):
        return _unauthorized()
    check = IdentityCheck.objects.filter(token=token, source=IdentityCheck.Source.WALKIN).first()
    if check is None:
        return JsonResponse({"error": "That ID check was not found."}, status=404)
    check = identity.refresh(check.pk, source="poll") or check
    return JsonResponse({"success": True, **identity.staff_state(check), "phone": check.subject_phone})


@require_http_methods(["POST"])
@audited("id.walkin_cancel", "Customers", audit_describe.id_walkin_cancel)
def booking_id_cancel_view(request, token):
    """Staff close a QR code that is still open (the customer changed their mind, or something was typed wrong). A check that already finished is left as it is."""
    if not _is_admin_staff(request):
        return _unauthorized()
    check = IdentityCheck.objects.filter(token=token, source=IdentityCheck.Source.WALKIN).first()
    if check is None:
        return JsonResponse({"error": "That ID check was not found."}, status=404)
    IdentityCheck.objects.filter(pk=check.pk, status=S.PENDING).update(status=S.CANCELLED, finished_at=timezone.now(), note="Closed by staff.")
    return JsonResponse({"success": True, **identity.staff_state(IdentityCheck.objects.get(pk=check.pk))})
