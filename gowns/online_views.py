"""The web side of the online checkout through PayMongo (the work itself is gowns/online_checkout.py):

  * POST reservation/pay/start/            the checkout's Pay button
  * GET  reservation/pay/return/<token>/   where PayMongo sends the customer after paying (a small "confirming" page)
  * GET  reservation/pay/status/<token>/   what that page asks every second or two
  * GET  reservation/pay/cancel/<token>/   where PayMongo's "back" link sends the customer
  * POST paymongo/webhook/                 PayMongo telling us a payment (or refund) happened

The customer's own addresses only ever work for the customer who started the attempt (anyone else gets a 404), and nothing PayMongo or the browser says is
believed: the money is always read back from PayMongo (online_checkout.confirm_attempt)."""
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from gowns import online_checkout
from gowns import availability
from gowns.context_processors import clear_reservation_hold, continue_shopping_links
from gowns.views import _signed_in_or_json
from reservations import gateway, payments
from reservations.models import PaymentAttempt

logger = logging.getLogger(__name__)


def _own_attempt(request, token):
    return get_object_or_404(PaymentAttempt, token=token, customer=request.user)


@_signed_in_or_json
@require_http_methods(["POST"])
def pay_start(request):
    return online_checkout.start_payment(request)


@login_required(login_url="accounts:login")
@never_cache
def payment_return(request, token):
    """A page that only SHOWS the result: the work is the status address below, called as soon as the page opens (so the page itself never waits on PayMongo)."""
    attempt = _own_attempt(request, token)
    return render(request, "payment_return.html", {
        "token": attempt.token,
        "status_url": reverse("gowns:payment_status", args=[attempt.token]),
        "checkout_url": reverse("gowns:reservation"),
        "orders_url": reverse("gowns:orders"),
        "amount": attempt.amount,
        "test_mode": not attempt.livemode,
        # "Continue Renting": back to the category of the gown added last (the bag still knows it in this browser), else the All page.
        "continue_links": continue_shopping_links(),
        "all_url": reverse("gowns:collection_all"),
        "pickup_hours": availability.PICKUP_HOURS_TEXT,
    })


@login_required(login_url="accounts:login")
@never_cache
def payment_status(request, token):
    attempt = _own_attempt(request, token)
    attempt = online_checkout.confirm_attempt(attempt.pk, source="poll") or attempt
    state = online_checkout.attempt_state(attempt)
    if state["state"] in ("booked", "refunded"):
        clear_reservation_hold(request)             # the checkout's countdown is over: the booking exists (or the money went back)
    return JsonResponse({"success": True, **state})


@login_required(login_url="accounts:login")
@never_cache
def payment_cancel(request, token):
    attempt = _own_attempt(request, token)
    attempt = online_checkout.cancel_attempt(attempt.pk) or attempt
    if attempt.status in (PaymentAttempt.Status.PAID, PaymentAttempt.Status.BOOKED, PaymentAttempt.Status.REFUNDED, PaymentAttempt.Status.FLAGGED):
        return redirect("gowns:payment_return", token=attempt.token)          # it WAS paid: show what happened, never a "cancelled" page
    return redirect(reverse("gowns:reservation") + "?payment=cancelled")


@csrf_exempt
@require_http_methods(["POST"])
def paymongo_webhook(request):
    """The payment service's message (PayMongo or Xendit, whichever the shop uses; both addresses reach this view). Believed only when it proves itself
    (PayMongo: its signature; Xendit: the callback token); then used only as a prompt to read the real state back from the service.
    200 once handled (or when it is an event we do not use), 400 for a bad signature or token, 503 when we could not finish (the service then tries again)."""
    event = gateway.verify_webhook(request)
    if event is None:
        logger.warning("A %s webhook with a bad or missing signature or token was refused.", gateway.label())
        return HttpResponse("bad signature", status=400, content_type="text/plain")
    kind = gateway.event_type(event)
    resource = gateway.event_resource(event)
    try:
        if kind == "checkout_session.payment.paid":
            attempt = PaymentAttempt.objects.filter(session_id=str(resource.get("id") or "")).first()
            if attempt is not None:
                PaymentAttempt.objects.filter(pk=attempt.pk).update(webhook_at=timezone.now())
                result = online_checkout.confirm_attempt(attempt.pk, source="webhook")
                if result is not None and result.status == PaymentAttempt.Status.CREATED:
                    return HttpResponse("could not confirm yet", status=503, content_type="text/plain")
        elif kind == "payment.refund.updated":
            online_checkout.on_refund_updated(resource)                  # a refund of a payment that could not be booked
            refund = gateway.parse_refund(resource)
            if refund.id and refund.status:
                payments.on_online_refund_updated(refund.id, refund.status)       # a refund of a returned deposit
        online_checkout.expire_stale(limit=2)
    except Exception:                                   # noqa: BLE001
        logger.exception("A %s webhook (%s) could not be handled", gateway.label(), kind)
        return HttpResponse("error", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")
