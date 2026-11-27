"""The admin's side of a date change request (reservations/date_requests.py): send one from the Change dates window, and, on the booking in
Active Reservations, show its QR code, take cash, send it again or cancel it. Everything is decided by that module; these only answer
the page."""
import json
import logging

from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from accounts.audit import audited
from accounts.roles import staff_display_name
from arabela_admin import audit_describe
from gowns import date_payment
from reservations import date_requests, gateway, payments
from reservations.date_changes import DateChangeError
from reservations.ledger import money

logger = logging.getLogger(__name__)


def request_row(row, today=None):
    """What the page shows about one waiting request."""
    left = max(0, int((row.expires_at - timezone.now()).total_seconds()))
    hours, minutes = left // 3600, (left % 3600) // 60
    return {
        "id": row.pk, "itemId": row.item_id, "gown": row.item.gown_name, "amount": f"{money(row.amount):,.2f}",
        "dates": date_requests.new_dates_text(row), "until": date_requests.moment_text(row.expires_at),
        "left": f"{hours} h {minutes} min left" if hours else f"{max(minutes, 1)} min left",
        "hasAccount": row.reservation.customer_id is not None, "by": row.requested_by_name,
    }


def _staff_or_401(request):
    from arabela_admin.views import _is_admin_staff

    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    return None


def _body(request):
    try:
        data = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _fail(exc):
    return JsonResponse({"error": exc.message}, status=exc.status)


@require_http_methods(["POST"])
@audited("item.request_payment", "Bookings", audit_describe.item_request_payment)
def item_request_payment_view(request, item_id):
    """"Send payment request" in the Change dates window: {"pickup", "return", "event"} like change-dates."""
    refused = _staff_or_401(request)
    if refused:
        return refused
    from arabela_admin.views import _date_choices

    chosen, error = _date_choices(request, "pickup", "return", "event")
    if error:
        return error
    try:
        row = date_requests.create(item_id, want_pickup=chosen["pickup"], want_return=chosen["return"], want_event=chosen["event"], user=request.user)
    except DateChangeError as exc:
        return _fail(exc)
    return JsonResponse({"success": True, "request": request_row(row), "amount": f"{money(row.amount):,.2f}"})


@require_http_methods(["POST"])
@audited("date_request.resend", "Bookings", audit_describe.date_request_resend)
def date_request_resend_view(request, request_id):
    refused = _staff_or_401(request)
    if refused:
        return refused
    try:
        sent = date_requests.send_again(request_id)
    except DateChangeError as exc:
        return _fail(exc)
    if not sent:
        return JsonResponse({"error": "This booking has no website account, so a message can't reach the customer. Show them the QR code or take cash."}, status=400)
    return JsonResponse({"success": True})


@require_http_methods(["POST"])
@audited("date_request.cancel", "Bookings", audit_describe.date_request_cancel)
def date_request_cancel_view(request, request_id):
    refused = _staff_or_401(request)
    if refused:
        return refused
    try:
        date_requests.cancel(request_id, user=request.user)
    except DateChangeError as exc:
        return _fail(exc)
    return JsonResponse({"success": True})


@require_http_methods(["POST"])
@audited("date_request.cash", "Payments", audit_describe.date_request_cash)
def date_request_cash_view(request, request_id):
    """The customer pays the request in cash at the counter: the payment is recorded and the new dates saved together."""
    refused = _staff_or_401(request)
    if refused:
        return refused
    data = _body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    try:
        row, payment = date_requests.pay_cash(request_id, user=request.user, request_key=str(data.get("request_id") or ""))
    except (DateChangeError, payments.PaymentError) as exc:
        return _fail(exc)
    return JsonResponse({
        "success": True, "amount": f"{money(payment.amount):,.2f}", "receiptNumber": payment.receipt_number, "paymentId": payment.pk,
        "printUrl": reverse("arabela_admin:payment_receipt_print", args=[payment.pk]),
    })


@require_http_methods(["POST"])
@audited("date_request.qr", "Payments", audit_describe.date_request_qr)
def date_request_qr_view(request, request_id):
    """"Show QR": a payment page for the request, and its code. The window then asks the usual payment status address."""
    refused = _staff_or_401(request)
    if refused:
        return refused
    try:
        result = date_payment.start_qr(request, request_id, staff=request.user, staff_name=staff_display_name(request.user))
    except payments.PaymentError as exc:
        return _fail(exc)
    except Exception:                                       # noqa: BLE001
        logger.exception("The payment QR code of date change request %s could not be opened", request_id)
        return JsonResponse({"error": f"The {gateway.label()} payment page could not be opened just now. Nothing was charged. Please try again."}, status=500)
    return JsonResponse(result)
