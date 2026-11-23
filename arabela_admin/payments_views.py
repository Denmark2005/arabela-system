"""The Payments page: what every booking still owes, its late fee and the deposit it can give back; recording a payment, handing the deposit back,
deductions, voiding a mistake, and the printed Payment Receipts. The rules live in reservations/ledger.py (the arithmetic) and
reservations/payments.py (what is accepted); this file is only the web side.

  staff, manager, owner   GET  payments/                              the list of bookings with their balances
                          GET  api/payments/booking/<id>/             one booking's whole money picture (the window)
                          POST api/payments/booking/<id>/record/      a payment received
                          POST api/payments/booking/<id>/return-deposit/
                          GET  payments/receipt/<id>/                 the printed Payment Receipt (counts as a print)
                          POST api/payments/<id>/send/                send the receipt to the customer again
  owner, or staff allowed "Void receipts and add deductions":
                          POST api/payments/booking/<id>/deduction/   take an amount off the deposit
                          POST api/payments/<id>/void/                void a receipt that was a mistake
                          POST api/deductions/<id>/void/              remove a deduction
  owner only              GET  payments/receipts/                     every receipt, with search
                          POST api/late-fee/                          the late fee per gown per day (profile page)

Kept out of views.py on purpose: it is a self-contained feature, and views.py is already very large."""
import json
from datetime import datetime, time, timedelta

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from accounts.audit import audited
from gowns import late_fee, typed_payment
from gowns.models import SiteSettings
from reservations import ledger, payments
from reservations.models import Deduction, Payment, Reservation, ReservationItem

from . import audit_describe
from .views import _can, _is_admin_staff, _is_owner, _permission_error, _require_admin_staff, _require_owner

RECEIPTS_PER_PAGE = 25


def _body(request):
    try:
        data = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _tracked_bookings():
    """Every booking whose money is tracked (see ledger.counts_as_tracked), with everything the statement reads loaded in a few queries."""
    return (
        Reservation.objects.filter(Q(status__in=ledger.LIVE_STATUSES) | Q(status=Reservation.Status.CANCELLED, reviewed_at__isnull=False))
        .select_related("customer__profile")
        .prefetch_related("items", "payments", "deductions", "receipt_records")
    )


def _php(value):
    return ledger.peso(value)


def _gown_text(items):
    live = [i for i in items if i.stage != ReservationItem.Stage.CANCELLED] or list(items)
    if not live:
        return "No gowns"
    return live[0].gown_name if len(live) == 1 else f"{live[0].gown_name} + {len(live) - 1} more"


def _deposit_text(s):
    if s.cancelled:
        return "Kept (cancelled)"
    if s.deposit_unpaid > 0:
        return "Not paid yet"
    if s.refunded > 0 and s.refund_available <= 0 and s.all_returned:
        return f"Returned {_php(s.refunded)}"
    if s.refund_available > 0:
        return f"Give back {_php(s.refund_available)}"
    if s.all_returned:
        return "Used for fees" if s.deposit_used > 0 else "Settled"
    return "Held"


# ---------------------------------------------------------------- the page
@_require_admin_staff
def payments_view(request):
    today = timezone.localdate()
    rows, balance_total = [], ledger.ZERO
    counts = {"all": 0, ledger.BALANCE: 0, ledger.REFUND: 0, "late": 0}
    for reservation in _tracked_bookings().order_by("-created_at", "-id"):
        s = ledger.statement(reservation, today)
        if not s.tracked:
            continue
        items = list(reservation.items.all())
        live = [i for i in items if i.stage != ReservationItem.Stage.CANCELLED] or items
        reservation.stmt = s
        reservation.gown_text = _gown_text(items)
        reservation.span_start = min((i.rental_date for i in live), default=None)
        reservation.span_end = max((i.return_date for i in live), default=None)
        reservation.deposit_text = _deposit_text(s)
        reservation.search_text = " ".join([reservation.reference_code, reservation.display_customer_name, reservation.phone or ""] +
                                           [i.gown_name for i in items]).lower()
        counts["all"] += 1
        if s.state in (ledger.BALANCE, ledger.REFUND):
            counts[s.state] += 1
        if s.late_now:
            counts["late"] += 1
        balance_total += s.balance_due
        rows.append(reservation)
    return render(request, "arabela_admin/payments.html", {
        "page": "payments", "mode": "bookings", "rows": rows, "counts": counts, "balance_total": balance_total,
        "is_owner": _is_owner(request), "can_void": _can(request, "void_receipts"),
        "late_fee_per_day": late_fee.per_day(), "open_id": request.GET.get("open", ""),
    })


# ---------------------------------------------------------------- one booking's window
def _payment_json(payment, request):
    from gowns import typed_payment as tp
    return {
        "id": payment.pk, "receiptNumber": payment.receipt_number or "", "amount": f"{ledger.money(payment.amount):,.2f}",
        "rawAmount": f"{ledger.money(payment.amount):.2f}",
        "direction": payment.direction, "purpose": payment.purpose, "purposeLabel": payment.get_purpose_display(),
        "method": payment.get_method_display(), "reference": tp.format_reference(payment.reference) if payment.reference else "",
        "when": timezone.localtime(payment.paid_at).strftime("%b %d, %Y %I:%M %p").replace(" 0", " "),
        "by": payment.recorded_by_name, "note": payment.note, "status": payment.status, "void": payment.is_void,
        "voidReason": payment.void_reason, "voidedBy": payment.voided_by_name, "legacy": payment.is_legacy,
        "printUrl": reverse("arabela_admin:payment_receipt_print", args=[payment.pk]) if payment.receipt_number else "",
        "reprints": payment.reprint_count, "notified": payment.customer_notified_at is not None,
    }


def _detail(reservation, request):
    s = ledger.statement(reservation)
    items = list(reservation.items.all())
    stored = list(reservation.payments.all())
    if stored:
        rows = [_payment_json(p, request) for p in stored]
    else:                      # an old booking the backfill has not reached: show what its statement is made of
        rows = [{"id": 0, "receiptNumber": "", "amount": f"{r['amount']:,.2f}", "rawAmount": f"{r['amount']:.2f}", "direction": r["direction"],
                 "purpose": r["purpose"], "purposeLabel": Payment.Purpose(r["purpose"]).label, "method": Payment.Method(r["method"]).label,
                 "reference": r["reference"], "when": "", "by": "Before the Payments page", "note": r["note"], "status": "Recorded", "void": False,
                 "voidReason": "", "voidedBy": "", "legacy": True, "printUrl": "", "reprints": 0, "notified": False} for r in ledger.legacy_rows(reservation)]
    stmt = s.as_dict()
    stmt.pop("late_lines", None)
    return {
        "id": reservation.pk, "code": reservation.reference_code, "customer": reservation.display_customer_name, "phone": reservation.phone or "",
        "channel": reservation.get_channel_display(), "status": reservation.status, "hasAccount": reservation.customer_id is not None,
        "gowns": [{"name": i.gown_name, "size": i.size, "pickup": f"{i.rental_date:%b} {i.rental_date.day}", "return": f"{i.return_date:%b} {i.return_date.day}",
                   "stage": i.stage} for i in items],
        "s": stmt,
        "peso": {k: f"{getattr(s, k):,.2f}" for k in ("rental", "deposit", "total", "received", "refunded", "deposit_paid", "rental_paid", "late_fee",
                                                       "deductions_total", "refundable_total", "refund_available", "balance_due", "extra_owed")},
        "lateLines": [{"gown": l["gown"], "days": l["days"], "rate": f"{l['rate']:,.0f}", "amount": f"{l['amount']:,.2f}", "back": l["back"]} for l in s.late_lines],
        "choices": payments.choices(s),
        "payments": rows,
        "deductions": [{"id": d.pk, "amount": f"{ledger.money(d.amount):,.2f}", "reason": d.reason, "by": d.recorded_by_name, "void": d.is_void,
                        "voidReason": d.void_reason, "when": timezone.localtime(d.created_at).strftime("%b %d, %Y").replace(" 0", " ")}
                       for d in reservation.deductions.all()],
        "canReturn": bool(s.refund_available > 0),
        "canDeduct": bool(s.tracked and not s.cancelled),
    }


def _get_booking(pk):
    booking = _tracked_bookings().filter(pk=pk).first()
    if booking is None:
        return None
    return booking


@require_http_methods(["GET"])
def payment_detail_view(request, pk):
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    booking = _get_booking(pk)
    if booking is None:
        return JsonResponse({"error": "That booking's payments are not tracked (it is still waiting for approval, or it was rejected)."}, status=404)
    data = _detail(booking, request)
    data["canVoid"] = _can(request, "void_receipts")
    data["isOwner"] = _is_owner(request)
    return JsonResponse(data)


def _answer(reservation_id, request, **extra):
    booking = _get_booking(reservation_id)
    out = {"success": True, "detail": _detail(booking, request) if booking else None}
    if out["detail"] is not None:
        out["detail"]["canVoid"] = _can(request, "void_receipts")
        out["detail"]["isOwner"] = _is_owner(request)
    out.update(extra)
    return JsonResponse(out)


def _fail(exc):
    return JsonResponse({"error": exc.message}, status=exc.status)


def _amount(raw):
    amount = typed_payment.parse_amount(str(raw if raw is not None else ""))
    if amount is None:
        raise payments.PaymentError("Please type the amount as numbers only, like 2000 or 2,000.50.")
    return amount


# ---------------------------------------------------------------- the actions
@require_http_methods(["POST"])
@audited("payment.record", "Payments", audit_describe.payment_record)
def payment_record_view(request, pk):
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    data = _body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    try:
        payment = payments.record_payment(
            reservation_id=pk, purpose=str(data.get("purpose") or ""), amount=_amount(data.get("amount")), method=str(data.get("method") or ""),
            reference=str(data.get("reference") or ""), note=str(data.get("note") or ""), user=request.user, request_id=str(data.get("request_id") or ""))
    except payments.PaymentError as exc:
        return _fail(exc)
    return _answer(pk, request, payment=_payment_json(payment, request))


@require_http_methods(["POST"])
@audited("payment.return_deposit", "Payments", audit_describe.deposit_return, sensitive=True)
def deposit_return_view(request, pk):
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    data = _body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    try:
        payment = payments.return_deposit(
            reservation_id=pk, amount=_amount(data.get("amount")), method=str(data.get("method") or ""), reference=str(data.get("reference") or ""),
            note=str(data.get("note") or ""), user=request.user, request_id=str(data.get("request_id") or ""))
    except payments.PaymentError as exc:
        return _fail(exc)
    return _answer(pk, request, payment=_payment_json(payment, request))


@require_http_methods(["POST"])
@audited("payment.deduction", "Payments", audit_describe.deduction_add, sensitive=True)
def deduction_add_view(request, pk):
    refused = _permission_error(request, "void_receipts")
    if refused:
        return refused
    data = _body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    try:
        deduction = payments.add_deduction(
            reservation_id=pk, amount=_amount(data.get("amount")), reason=str(data.get("reason") or ""), user=request.user,
            request_id=str(data.get("request_id") or ""))
    except payments.PaymentError as exc:
        return _fail(exc)
    return _answer(pk, request, deduction={"id": deduction.pk, "amount": f"{ledger.money(deduction.amount):,.2f}"})


@require_http_methods(["POST"])
@audited("payment.void", "Payments", audit_describe.payment_void, sensitive=True)
def payment_void_view(request, payment_id):
    refused = _permission_error(request, "void_receipts")
    if refused:
        return refused
    data = _body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    try:
        payment = payments.void_payment(payment_id=payment_id, reason=str(data.get("reason") or ""), user=request.user)
    except payments.PaymentError as exc:
        return _fail(exc)
    return _answer(payment.reservation_id, request, payment=_payment_json(payment, request))


@require_http_methods(["POST"])
@audited("payment.deduction_void", "Payments", audit_describe.deduction_void, sensitive=True)
def deduction_void_view(request, deduction_id):
    refused = _permission_error(request, "void_receipts")
    if refused:
        return refused
    data = _body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    try:
        deduction = payments.void_deduction(deduction_id=deduction_id, reason=str(data.get("reason") or ""), user=request.user)
    except payments.PaymentError as exc:
        return _fail(exc)
    return _answer(deduction.reservation_id, request)


@require_http_methods(["POST"])
@audited("payment.send", "Payments", audit_describe.receipt_send)
def receipt_send_view(request, payment_id):
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        sent = payments.send_receipt_again(payment_id)
    except payments.PaymentError as exc:
        return _fail(exc)
    except Payment.DoesNotExist:
        return JsonResponse({"error": "That receipt was not found."}, status=404)
    if not sent:
        return JsonResponse({"error": "This booking has no website account, so there is nobody to send it to. Print it for the customer."}, status=409)
    return JsonResponse({"success": True})


# ---------------------------------------------------------------- the printed receipt
@_require_admin_staff
def payment_receipt_print_view(request, payment_id):
    payment = Payment.objects.select_related("reservation__customer__profile").filter(pk=payment_id).first()
    if payment is None or not payment.receipt_number:
        raise Http404("No such receipt")
    # `?look=1` shows the paper without counting a print (the Receipts list preview); every other open counts: the first is the original.
    reprint = 0 if request.GET.get("look") else payments.mark_printed(payment.pk)
    payment.refresh_from_db()
    return render(request, "arabela_admin/receipt_print.html", {
        "payment": payment, "r": payment.snapshot, "reprint": reprint, "copy_label": "",
        "back_url": reverse("arabela_admin:payments") + "?open=" + str(payment.reservation_id),
    })


# ---------------------------------------------------------------- every receipt (owner)
@_require_owner
def payment_receipts_view(request):
    q = (request.GET.get("q") or "").strip()
    kind = request.GET.get("kind") or "all"
    status = request.GET.get("status") or "all"
    start, end = (request.GET.get("from") or "").strip(), (request.GET.get("to") or "").strip()
    rows = Payment.objects.filter(is_legacy=False, receipt_number__isnull=False).select_related("reservation")
    if q:
        rows = rows.filter(Q(receipt_number__icontains=q) | Q(reservation__reference_code__icontains=q) | Q(reservation__customer_name__icontains=q) |
                           Q(reference__icontains=q.replace(" ", "")) | Q(recorded_by_name__icontains=q))
    if kind == "in":
        rows = rows.filter(direction=Payment.Direction.IN)
    elif kind == "out":
        rows = rows.filter(direction=Payment.Direction.OUT)
    if status == "void":
        rows = rows.filter(status=Payment.Status.VOID)
    elif status == "ok":
        rows = rows.filter(status=Payment.Status.RECORDED)
    tz = timezone.get_current_timezone()
    try:
        if start:
            rows = rows.filter(paid_at__gte=timezone.make_aware(datetime.combine(datetime.strptime(start, "%Y-%m-%d").date(), time.min), tz))
        if end:
            rows = rows.filter(paid_at__lt=timezone.make_aware(datetime.combine(datetime.strptime(end, "%Y-%m-%d").date() + timedelta(days=1), time.min), tz))
    except ValueError:
        start = end = ""
    page = Paginator(rows.order_by("-paid_at", "-id"), RECEIPTS_PER_PAGE).get_page(request.GET.get("page"))
    for p in page:
        p.customer_name = p.reservation.display_customer_name
    return render(request, "arabela_admin/payments.html", {
        "page": "payments", "mode": "receipts", "receipts": page, "q": q, "kind": kind, "status": status, "from": start, "to": end,
        "is_owner": True, "can_void": True,
    })


# ---------------------------------------------------------------- the late fee setting (owner, on the profile page)
@require_http_methods(["POST"])
@audited("settings.late_fee", "Settings", audit_describe.late_fee_setting, sensitive=True, before=audit_describe.before_settings_late_fee)
def late_fee_setting_update_view(request):
    """Save the late fee per gown per day (whole pesos, 0 to 10,000). Owner only. It applies to NEW bookings: every booking keeps the rate it was
    made with."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    if not _is_owner(request):
        return JsonResponse({"error": "Only the owner can change the late fee."}, status=403)
    data = _body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    amount = late_fee.clean(data.get("amount"))
    if amount is None:
        return JsonResponse({"error": f"Enter a whole number of pesos from {late_fee.MIN_PER_DAY} to {late_fee.MAX_PER_DAY:,}, like 200."}, status=400)
    shop = SiteSettings.load()
    shop.late_fee_per_day = amount
    shop.save(update_fields=["late_fee_per_day", "updated_at"])
    return JsonResponse({"success": True, "amount": amount, "is_default": amount == late_fee.DEFAULT_PER_DAY})
