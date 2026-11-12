"""The four web endpoints for cancellation requests (the rules: reservations/cancel_requests.py).

  staff  POST api/reservations/<pk>/cancel-request/    ask the owner to cancel a booking or some of its gowns
  owner  POST api/cancel-requests/<id>/approve/        say yes: the normal cancellation runs
  owner  POST api/cancel-requests/<id>/decline/        say no, with a short note staff read
  staff  POST api/cancel-requests/<id>/withdraw/       take the request back while it is waiting
Kept out of views.py on purpose: it is a self-contained feature, and views.py is already very large."""
import json

from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from accounts.audit import audited
from reservations import cancel_requests
from reservations.cancellation import CannotCancel
from reservations.models import CancelRequest

from . import audit_describe
from .views import _can, _is_admin_staff, _require_owner


def _body(request):
    try:
        data = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def request_row(waiting, now=None, user=None):
    """One waiting request as the Active Reservations page and the bell need it."""
    from django.utils import timezone
    from django.utils.timesince import timesince
    now = now or timezone.now()
    return {
        "id": waiting.id,
        "by": waiting.requested_by_name,
        "what": cancel_requests.what_text(waiting),
        "reason": cancel_requests.reason_text(waiting),
        "whole": waiting.is_whole_booking,
        "mine": bool(user is not None and waiting.requested_by_id is not None and waiting.requested_by_id == user.pk),
        "age": "just now" if (now - waiting.created_at).total_seconds() < 60 else timesince(waiting.created_at, now).split(",")[0] + " ago",
        "overdue": cancel_requests.is_overdue(waiting, now),
        "approve_url": reverse("arabela_admin:cancel_request_approve", args=[waiting.id]),
        "decline_url": reverse("arabela_admin:cancel_request_decline", args=[waiting.id]),
        "withdraw_url": reverse("arabela_admin:cancel_request_withdraw", args=[waiting.id]),
    }


@require_http_methods(["POST"])
@audited("cancel_request.create", "Bookings", audit_describe.cancel_request_create, sensitive=True)
def cancel_request_create_view(request, pk):
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    if _can(request, "cancel_bookings"):
        return JsonResponse({"error": "You are allowed to cancel this yourself, so there is nothing to ask."}, status=400)
    data = _body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    item_ids = data.get("items")
    if item_ids is not None and (not isinstance(item_ids, list) or not item_ids or len(item_ids) > 200
                                 or any(isinstance(i, bool) or not isinstance(i, int) for i in item_ids)):
        return JsonResponse({"error": "Choose which gown to cancel."}, status=400)
    try:
        waiting = cancel_requests.create(pk, request.user, reason_key=str(data.get("reason") or ""), note=str(data.get("note") or ""),
                                         item_ids=item_ids)
    except CannotCancel as exc:
        return JsonResponse({"error": exc.message}, status=exc.status)
    return JsonResponse({"success": True, "reference": waiting.reservation.reference_code, "request": request_row(waiting, user=request.user)})


@require_http_methods(["POST"])
@audited("cancel_request.approve", "Bookings", audit_describe.cancel_request_approve, sensitive=True)
@_require_owner
def cancel_request_approve_view(request, request_id):
    try:
        outcome = cancel_requests.approve(request_id, request.user)
    except CannotCancel as exc:
        return JsonResponse({"error": exc.message}, status=exc.status)
    waiting = outcome.request
    payload = {
        "success": True, "outcome": outcome.kind, "message": outcome.message, "reference": waiting.reservation.reference_code,
        "requested_by": waiting.requested_by_name, "what": cancel_requests.what_text(waiting), "reason": cancel_requests.reason_text(waiting),
    }
    result = outcome.result
    if result is not None:          # some gowns were cancelled: say which, like the direct cancel does
        payload.update({"count": len(result.cancelled), "gowns": ", ".join(item.gown_name for item in result.cancelled),
                        "booking_cancelled": result.booking_cancelled})
    return JsonResponse(payload)


@require_http_methods(["POST"])
@audited("cancel_request.decline", "Bookings", audit_describe.cancel_request_decline, sensitive=True)
@_require_owner
def cancel_request_decline_view(request, request_id):
    data = _body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    try:
        waiting = cancel_requests.decline(request_id, request.user, str(data.get("note") or ""))
    except CannotCancel as exc:
        return JsonResponse({"error": exc.message}, status=exc.status)
    return JsonResponse({"success": True, "reference": waiting.reservation.reference_code, "requested_by": waiting.requested_by_name,
                         "what": cancel_requests.what_text(waiting), "note": waiting.decision_note})


@require_http_methods(["POST"])
@audited("cancel_request.withdraw", "Bookings", audit_describe.cancel_request_withdraw)
def cancel_request_withdraw_view(request, request_id):
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        waiting = cancel_requests.withdraw(request_id, request.user)
    except CannotCancel as exc:
        return JsonResponse({"error": exc.message}, status=exc.status)
    return JsonResponse({"success": True, "reference": waiting.reservation.reference_code, "what": cancel_requests.what_text(waiting)})
