"""The Payments page: what every booking still owes, its date change and overdue fees and the deposit it can give back; recording a payment, handing the deposit back,
deductions, voiding a mistake, and the printed Payment Receipts. The rules live in reservations/ledger.py (the arithmetic) and
reservations/payments.py (what is accepted); this file is only the web side.

  staff, manager, owner   GET  payments/                              the list of bookings with their balances
                          GET  api/payments/booking/<id>/             one booking's whole money picture (the window)
                          POST api/payments/booking/<id>/record/      a CASH payment received (an open QR code of the booking is closed first)
                          POST api/payments/booking/<id>/pay/start/   "Pay online": a QR code for what the booking owes (gowns/counter_payment.py)
                          GET  api/payments/pay/<token>/status/       has the customer paid? (asked every few seconds)
                          POST api/payments/pay/<token>/cancel/       close the QR code
                          POST api/payments/booking/<id>/return-deposit/   "Pay online" (back to the account they paid with) or Cash
                          GET  payments/receipt/<id>/                 the printed Payment Receipt (counts as a print)
                          POST api/payments/<id>/send/                send the receipt to the customer again
  owner, or staff allowed "Void receipts and add deductions":
                          POST api/payments/booking/<id>/deduction/   take an amount off the deposit
                          POST api/payments/<id>/void/                void a receipt that was a mistake
                          POST api/deductions/<id>/void/              remove a deduction
  staff, manager, owner   GET  payments/online/                       payments made on PayMongo's page that need a look (and all of them)
  owner, or staff allowed "Void receipts and add deductions":
                          POST api/payments/online/<id>/refund/       send a paid-but-not-booked online payment back to the customer
                          POST api/payments/online/<id>/handled/      note that a person sorted such a payment out by hand
  owner only              GET  payments/receipts/                     every receipt, with search
                          POST api/late-fee/                          the overdue / date change fee per gown per day (profile page)

Kept out of views.py on purpose: it is a self-contained feature, and views.py is already very large."""
import json
import logging
from datetime import datetime, time, timedelta

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_http_methods

from accounts.audit import audited
from gowns import counter_payment, late_fee, online_checkout, typed_payment
from gowns.models import SiteSettings
from reservations import gateway, ledger, payments
from reservations.models import Deduction, Payment, PaymentAttempt, Reservation, ReservationItem

from . import audit_describe
from .views import _can, _is_admin_staff, _is_owner, _permission_error, _require_admin_staff, _require_owner

RECEIPTS_PER_PAGE = 25
logger = logging.getLogger(__name__)


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
        if s.overdue_now:
            counts["late"] += 1
        balance_total += s.balance_due
        rows.append(reservation)
    return render(request, "arabela_admin/payments.html", {
        "page": "payments", "mode": "bookings", "rows": rows, "counts": counts, "balance_total": balance_total,
        "is_owner": _is_owner(request), "can_void": _can(request, "void_receipts"),
        "late_fee_per_day": late_fee.per_day(), "open_id": request.GET.get("open", ""), "online_attention": online_attention_count(),
        "payment_provider": gateway.label(), "paymongo_mode": gateway.mode(), "paymongo_demo": gateway.is_demo(),
        "online_enabled": gateway.enabled(), "online_max": int(counter_payment.max_amount()) if gateway.enabled() else 0,
        "online_minutes": counter_payment.MINUTES,
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
    stmt.pop("fee_lines", None)
    return {
        "id": reservation.pk, "code": reservation.reference_code, "customer": reservation.display_customer_name, "phone": reservation.phone or "",
        "channel": reservation.get_channel_display(), "status": reservation.status, "hasAccount": reservation.customer_id is not None,
        "gowns": [{"name": i.gown_name, "size": i.size, "pickup": f"{i.rental_date:%b} {i.rental_date.day}", "return": f"{i.return_date:%b} {i.return_date.day}",
                   "stage": i.stage} for i in items],
        "s": stmt,
        "peso": {k: f"{getattr(s, k):,.2f}" for k in ("rental", "deposit", "total", "received", "refunded", "deposit_paid", "rental_paid", "late_fee",
                                                       "early_fee", "later_fee", "change_fee", "overdue_fee", "deductions_total", "total_deduction",
                                                       "refundable_total", "refund_available", "balance_due", "extra_owed", "extra_received")},
        "feeLines": [{**{k: l[k] for k in ("kind", "label", "gown", "days", "back", "growing", "booked", "agreed", "due", "returned", "pickup", "free_from", "free_until", "until")},
                      "rate": f"{l['rate']:,.0f}", "amount": f"{l['amount']:,.2f}"} for l in s.fee_lines],
        "choices": payments.choices(s),
        "onlineRefund": payments.online_refund_choice(reservation, s),
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
    if _get_booking(pk) is None:
        return JsonResponse({"error": "That booking's payments are not tracked (it is still waiting for approval, or it was rejected)."}, status=404)
    try:
        qr = counter_payment.window_state(pk)               # first: a payment that came in is recorded by this look, so the numbers below include it
    except Exception:                                       # noqa: BLE001 -- the booking's window must open even when the payment service can't be asked
        logger.exception("The open payment QR code of booking %s could not be checked", pk)
        qr = None
    data = _detail_for(pk, request)
    data["pay"] = qr
    return JsonResponse(data)


def _detail_for(reservation_id, request):
    """The window's data for one booking, as this person may use it; None when its payments are not tracked."""
    booking = _get_booking(reservation_id)
    if booking is None:
        return None
    data = _detail(booking, request)
    data["canVoid"] = _can(request, "void_receipts")
    data["isOwner"] = _is_owner(request)
    return data


def _answer(reservation_id, request, **extra):
    out = {"success": True, "detail": _detail_for(reservation_id, request)}
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
    if str(data.get("method") or "") != Payment.Method.CASH:
        return JsonResponse({"error": "Choose Cash or Pay online. If this page has been open for a while, refresh it."}, status=400)
    try:
        amount = _amount(data.get("amount"))
    except payments.PaymentError as exc:
        return _fail(exc)
    if counter_payment.close_open(pk):                      # a QR code of this booking was open and the money came in: look before taking cash too
        return JsonResponse({"error": "The customer's online payment just arrived. Check the amounts before recording cash.", "refresh": True}, status=409)
    try:
        payment = payments.record_payment(
            reservation_id=pk, purpose=str(data.get("purpose") or ""), amount=amount, method=Payment.Method.CASH,
            note=str(data.get("note") or ""), user=request.user, request_id=str(data.get("request_id") or ""))
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
        if str(data.get("method") or "") == "PayMongo":
            # Sent back automatically, through PayMongo, to the account the customer paid with.
            payment = payments.return_deposit_online(
                reservation_id=pk, amount=_amount(data.get("amount")), note=str(data.get("note") or ""), user=request.user,
                request_id=str(data.get("request_id") or ""))
        elif str(data.get("method") or "") == Payment.Method.CASH:
            payment = payments.return_deposit(
                reservation_id=pk, amount=_amount(data.get("amount")), method=Payment.Method.CASH,
                note=str(data.get("note") or ""), user=request.user, request_id=str(data.get("request_id") or ""))
        else:
            return JsonResponse({"error": "Choose Cash or Pay online. If this page has been open for a while, refresh it."}, status=400)
    except payments.PaymentError as exc:
        return _fail(exc)
    return _answer(pk, request, payment=_payment_json(payment, request))


# ---------------------------------------------------------------- "Pay online": a QR code for what the booking owes
@require_http_methods(["POST"])
@audited("payment.pay_start", "Payments", audit_describe.payment_pay_start)
def payment_pay_start_view(request, pk):
    """"Show payment QR" in the booking's window: the amount is checked like cash at the counter, a payment page is opened and its QR code answered. Nothing is
    recorded until Xendit confirms the money (gowns/counter_payment.py)."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    data = _body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    try:
        result = counter_payment.start(
            request=request, staff=request.user, staff_name=payments.staff_display_name(request.user), reservation_id=pk,
            purpose=str(data.get("purpose") or ""), amount=_amount(data.get("amount")), note=str(data.get("note") or ""),
            request_id=str(data.get("request_id") or ""))
    except payments.PaymentError as exc:
        return _fail(exc)
    except Exception:                                       # noqa: BLE001
        logger.exception("The payment QR code of booking %s could not be opened", pk)
        return JsonResponse({"error": "The payment page could not be opened just now. Nothing was recorded or charged. Please try again."}, status=500)
    if result.get("state") == "recorded":
        result["detail"] = _detail_for(pk, request)
    return JsonResponse(result)


def _counter_attempt(token):
    return PaymentAttempt.objects.filter(token=token, source=PaymentAttempt.Source.SHOP).first()


def _pay_answer(attempt, now_state, request):
    out = {"success": True, "token": attempt.token, "bookingId": attempt.reservation_id, **now_state}
    if now_state["state"] == "recorded":
        out["detail"] = _detail_for(attempt.reservation_id, request)
    return JsonResponse(out)


@never_cache
@require_http_methods(["GET"])
def payment_pay_status_view(request, token):
    """The window asks every few seconds: has the customer paid? (Xendit is asked, never the screen.)"""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    attempt = _counter_attempt(token)
    if attempt is None:
        return JsonResponse({"error": "That payment was not found."}, status=404)
    attempt, now_state = counter_payment.refreshed_state(attempt)
    return _pay_answer(attempt, now_state, request)


@require_http_methods(["POST"])
@audited("payment.pay_cancel", "Payments", audit_describe.payment_pay_cancel)
def payment_pay_cancel_view(request, token):
    """Staff close the QR code (the customer pays another way, or the amount was wrong). A payment that already arrived is honoured instead."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    attempt = _counter_attempt(token)
    if attempt is None:
        return JsonResponse({"error": "That payment was not found."}, status=404)
    attempt = online_checkout.cancel_attempt(attempt.pk) or attempt
    return _pay_answer(attempt, counter_payment.state(attempt), request)


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
# Shown inside the Receipt window (an iframe on the SAME site), so it may be framed by this site and nobody else; the rest of the shop stays unframeable.
@xframe_options_sameorigin
@_require_admin_staff
def payment_receipt_print_view(request, payment_id):
    payment = Payment.objects.select_related("reservation__customer__profile").filter(pk=payment_id).first()
    if payment is None or not payment.receipt_number:
        raise Http404("No such receipt")
    # `?look=1` shows the paper without counting a print (the Receipts list preview); every other open counts: the first is the original.
    reprint = 0 if request.GET.get("look") else payments.mark_printed(payment.pk)
    payment.refresh_from_db()
    return render(request, "arabela_admin/receipt_print.html", {
        # `embed` = inside the Receipt preview window: just the paper, no toolbar.
        "payment": payment, "r": payment.snapshot, "reprint": reprint, "copy_label": "", "embed": bool(request.GET.get("embed")),
        "back_url": reverse("arabela_admin:payments") + "?open=" + str(payment.reservation_id),
    })


# ---------------------------------------------------------------- one booking's receipts (the Receipt preview window)
def _receipt_pill(payment):
    """The status pill of one receipt: Void, Deposit returned, Paid, or Partly paid (with what is still to pay at that moment)."""
    if payment.is_void:
        return {"kind": "void", "text": "Void"}
    if payment.direction == Payment.Direction.OUT:
        return {"kind": "returned", "text": "Deposit returned"}
    try:
        balance = ledger.money((payment.snapshot or {}).get("balance_after") or "0")
    except Exception:       # noqa: BLE001 -- an odd snapshot must not break the window
        balance = ledger.ZERO
    if balance > 0:
        return {"kind": "partly", "text": f"Partly paid \u00b7 {ledger.peso(balance)} to pay"}
    return {"kind": "paid", "text": "Paid"}


def _receipt_item(payment):
    out = payment.direction == Payment.Direction.OUT
    label = "Deposit return" if out else payment.get_purpose_display()
    amount = ledger.money(payment.amount)
    base = reverse("arabela_admin:payment_receipt_print", args=[payment.pk])
    return {
        "id": payment.pk, "number": payment.receipt_number, "label": label, "direction": payment.direction,
        "signed": ("-" if out else "+") + ledger.peso(amount), "amount": f"{amount:.2f}", "method": payment.get_method_display(),
        "when": timezone.localtime(payment.paid_at).strftime("%b %d, %Y %I:%M %p").replace(" 0", " "), "by": payment.recorded_by_name or "",
        "void": payment.is_void, "pill": _receipt_pill(payment),
        # The preview shows the paper without counting a print; pressing Print Receipt opens the other address, which counts like the Payments page's Print.
        "lookUrl": base + "?look=1&embed=1", "printUrl": base + "?embed=1",
    }


@require_http_methods(["GET"])
def receipt_list_view(request, pk):
    """The receipts of one booking, newest first, for the Receipt preview window (any admin account that can open the page it is pressed on)."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    reservation = Reservation.objects.select_related("customer__profile").prefetch_related("payments").filter(pk=pk).first()
    if reservation is None:
        return JsonResponse({"error": "That booking was not found."}, status=404)
    receipts = [_receipt_item(p) for p in reversed(payments.printable_receipts(reservation))]
    return JsonResponse({"booking": {"id": reservation.pk, "code": reservation.reference_code, "customer": reservation.display_customer_name}, "receipts": receipts})


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
        "is_owner": True, "can_void": True, "online_attention": online_attention_count(), "payment_provider": gateway.label(), "paymongo_mode": gateway.mode(),
        "paymongo_demo": gateway.is_demo(),
    })


# ---------------------------------------------------------------- payments made on PayMongo's page
ONLINE_PER_PAGE = 25
_CHIP = {                       # attempt status -> (the Payments page's chip colour, its words)
    PaymentAttempt.Status.CREATED: ("refund", "Waiting for payment"),
    PaymentAttempt.Status.PAID: ("refund", "Making the booking"),
    PaymentAttempt.Status.BOOKED: ("paid", "Booked"),
    PaymentAttempt.Status.REFUNDED: ("settled", "Sent back"),
    PaymentAttempt.Status.FLAGGED: ("balance", "Needs a look"),
    PaymentAttempt.Status.HANDLED: ("settled", "Handled"),
    PaymentAttempt.Status.EXPIRED: ("cancelled", "Not paid"),
    PaymentAttempt.Status.CANCELLED: ("cancelled", "Not paid"),
}


def online_attention_count():
    """How many online payments need a person (the tab's badge and the bell)."""
    return PaymentAttempt.objects.filter(PaymentAttempt.attention_filter()).count()


def _online_row(attempt, now):
    chip, words = _CHIP.get(attempt.status, ("settled", attempt.get_status_display()))
    attention = attempt.needs_a_look
    if attention:
        chip, words = "balance", ("Refund failed" if attempt.status == PaymentAttempt.Status.REFUNDED else "Needs a look")
    elif attempt.status == PaymentAttempt.Status.REFUNDED:
        words = "Sent back" if attempt.refund_status in ("succeeded", "") else f"Sent back ({attempt.refund_status})"
    elif attempt.status == PaymentAttempt.Status.CREATED and attempt.expires_at < now - online_checkout.SWEEP_GRACE:
        chip, words = "cancelled", "Not paid"
    attempt.chip, attempt.chip_words, attempt.attention = chip, words, attention
    attempt.kind_label = attempt.get_purpose_display()
    attempt.channel_label = gateway.CHANNEL_LABELS.get(attempt.channel, attempt.channel or "-")
    attempt.who = attempt.customer_name or (attempt.customer.get_username() if attempt.customer_id else "-")
    attempt.booking_code = attempt.reservation.reference_code if attempt.reservation_id else ""
    at_shop = attempt.source in (PaymentAttempt.Source.WALKIN, PaymentAttempt.Source.SHOP)
    attempt.staff_label = (attempt.payload or {}).get("staff_name", "") if at_shop else ""
    if not attention and attempt.source == PaymentAttempt.Source.SHOP and attempt.status == PaymentAttempt.Status.BOOKED:
        attempt.chip_words = "Recorded"                  # the booking existed already: the money was recorded on it
    attempt.can_refund = attempt.attention and attempt.refund_can_be_asked
    attempt.can_handle = attempt.attention
    return attempt


@_require_admin_staff
def payment_online_view(request):
    q = (request.GET.get("q") or "").strip()
    show = "all" if request.GET.get("show") == "all" else "attention"
    now = timezone.now()
    rows = PaymentAttempt.objects.select_related("reservation", "customer")
    if show == "attention":
        rows = rows.filter(PaymentAttempt.attention_filter(now))
    if q:
        rows = rows.filter(Q(customer_name__icontains=q) | Q(reservation__reference_code__icontains=q) | Q(provider_payment_id__icontains=q)
                           | Q(session_id__icontains=q) | Q(token__startswith=q) | Q(note__icontains=q))
    page = Paginator(rows.order_by("-created_at", "-id"), ONLINE_PER_PAGE).get_page(request.GET.get("page"))
    for attempt in page:
        _online_row(attempt, now)
    return render(request, "arabela_admin/payments.html", {
        "page": "payments", "mode": "online", "attempts": page, "q": q, "show": show, "online_attention": online_attention_count(),
        "payment_provider": gateway.label(), "paymongo_mode": gateway.mode(), "paymongo_demo": gateway.is_demo(), "is_owner": _is_owner(request), "can_void": _can(request, "void_receipts"),
    })


def _online_attempt(attempt_id):
    attempt = PaymentAttempt.objects.select_related("reservation", "customer").filter(pk=attempt_id).first()
    if attempt is None:
        raise Http404("That online payment was not found.")
    return attempt


@require_http_methods(["POST"])
@audited("payment.online_refund", "Payments", audit_describe.online_refund, sensitive=True)
def payment_online_refund_view(request, attempt_id):
    refused = _permission_error(request, "void_receipts")
    if refused:
        return refused
    attempt = _online_attempt(attempt_id)
    if not attempt.refund_can_be_asked or not attempt.needs_a_look:
        return JsonResponse({"error": "This payment can't be sent back from here. Refresh the page."}, status=409)
    data = _body(request) or {}
    reason = (str(data.get("reason") or "").strip() or "The shop sent it back.")[:200]
    result = online_checkout.refund_attempt(attempt.pk, f"The shop checked this payment: {reason}")
    if result.refund_id:
        return JsonResponse({"success": True, "message": "The money is on its way back to the customer."})
    return JsonResponse({"error": f"{gateway.label()} would not send it back right now. Try again later, or refund it in {gateway.label()}'s dashboard and press Mark as handled."}, status=502)


@require_http_methods(["POST"])
@audited("payment.online_handled", "Payments", audit_describe.online_handled, sensitive=True)
def payment_online_handled_view(request, attempt_id):
    refused = _permission_error(request, "void_receipts")
    if refused:
        return refused
    attempt = _online_attempt(attempt_id)
    data = _body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    note = str(data.get("note") or "").strip()
    if len(note) < 3:
        return JsonResponse({"error": "Please write what was done, so the record explains itself."}, status=400)
    who = payments.staff_display_name(request.user)
    updated = PaymentAttempt.objects.filter(pk=attempt.pk).filter(PaymentAttempt.attention_filter()).update(
        status=PaymentAttempt.Status.HANDLED, note=f"{who}: {note}"[:300], finished_at=timezone.now())
    if not updated:
        return JsonResponse({"error": "This payment no longer needs a look. Refresh the page."}, status=409)
    return JsonResponse({"success": True})


# ---------------------------------------------------------------- the overdue / date change fee setting (owner, on the profile page)
@require_http_methods(["POST"])
@audited("settings.late_fee", "Settings", audit_describe.late_fee_setting, sensitive=True, before=audit_describe.before_settings_late_fee)
def late_fee_setting_update_view(request):
    """Save the fee per gown per day, for overdue days and moved return days (whole pesos, 0 to 10,000). Owner only. It applies to NEW bookings: every booking keeps the rate it was
    made with."""
    if not _is_admin_staff(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    if not _is_owner(request):
        return JsonResponse({"error": "Only the owner can change the overdue and date change fee."}, status=403)
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
