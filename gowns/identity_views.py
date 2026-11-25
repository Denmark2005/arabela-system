"""The web side of ID verification for customers (the work itself is reservations/identity.py):

  * POST identity/start/               the "Verify my ID" button (checkout and profile)
  * GET  identity/state/               the customer's ID situation, as JSON (the buttons ask it when a page opens or is brought back by the Back button)
  * GET  identity/return/<token>/      where Didit sends the customer after they finish (a small "checking your ID" page)
  * GET  identity/status/<token>/      what that page asks every couple of seconds
  * GET  identity/shop/<token>/done/   where a WALK-IN customer's phone lands after scanning the shop's QR code (no sign-in, shows nothing about the person)
  * POST didit/webhook/                Didit's message that a check changed (believed only when it proves itself; then used only as a prompt)

A customer can only ever see their own check. Nothing here trusts the browser: the result is always read back from Didit."""
import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from gowns import online_checkout
from gowns.views import _signed_in_or_json
from reservations import didit, identity
from reservations.models import IdentityCheck

logger = logging.getLogger(__name__)


def _answer_error(exc):
    return JsonResponse({"success": False, "error": exc.message, **exc.extra}, status=exc.status)


@_signed_in_or_json
@require_http_methods(["POST"])
def identity_start(request):
    """The customer pressed "Verify my ID". Body (JSON): {id_type, consent: true}. Answers {success, state, url?, token?} -- the page then sends the browser to `url`."""
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"success": False, "error": "Invalid request."}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({"success": False, "error": "Invalid request."}, status=400)

    def callback_for(token):
        return online_checkout.absolute(request, reverse("gowns:identity_return", args=[token]))

    try:
        result = identity.start_for_customer(user=request.user, id_type=str(data.get("id_type") or ""), consent=data.get("consent"), callback_for=callback_for)
    except identity.IdentityError as exc:
        return _answer_error(exc)
    return JsonResponse({"success": True, **result})


@_signed_in_or_json
@never_cache
@require_http_methods(["GET"])
def identity_state(request):
    """The signed-in customer's ID situation. An open check is looked at first (at most every couple of seconds), so a customer who finished while the page was
    away sees it."""
    open_check = IdentityCheck.objects.filter(customer=request.user, source=IdentityCheck.Source.ONLINE, status=IdentityCheck.Status.PENDING).order_by("-id").first()
    if open_check is not None:
        identity.refresh(open_check.pk, source="poll")
    return JsonResponse({"success": True, **identity.customer_state(request.user)})


def _own_check(request, token):
    return get_object_or_404(IdentityCheck, token=token, customer=request.user, source=IdentityCheck.Source.ONLINE)


@login_required(login_url="accounts:login")
@never_cache
def identity_return(request, token):
    """A page that only SHOWS the result: it asks the status address below as soon as it opens."""
    check = _own_check(request, token)
    return render(request, "identity_return.html", {
        "token": check.token, "status_url": reverse("gowns:identity_status", args=[check.token]), "checkout_url": reverse("gowns:reservation"),
        "profile_url": reverse("gowns:profile"), "home_url": reverse("gowns:homepage"),
    })


@_signed_in_or_json
@never_cache
@require_http_methods(["GET"])
def identity_status(request, token):
    check = _own_check(request, token)
    check = identity.refresh(check.pk, source="poll") or check
    return JsonResponse({"success": True, **identity.page_state(check)})


@never_cache
def identity_walkin_done(request, token):
    """Where a walk-in customer's phone lands after the verification page. It says nothing about the result or the person (the shop's own screen shows that), so it needs
    no sign-in."""
    get_object_or_404(IdentityCheck, token=token, source=IdentityCheck.Source.WALKIN)
    return render(request, "walkin_id_done.html", {})


@csrf_exempt
@require_http_methods(["POST"])
def didit_webhook(request):
    """Didit's message that a verification changed. Believed only when it is signed with the shop's webhook secret and fresh (reservations/didit.py verify_webhook); then it is
    only a PROMPT: the decision is read back from Didit before anything is written. 200 once handled (or for a session we do not know), 400 for a bad signature, 503 when we
    could not confirm what it says (Didit then tries again)."""
    event = didit.verify_webhook(request)
    if event is None:
        logger.warning("A Didit webhook with a bad or missing signature was refused.")
        return HttpResponse("bad signature", status=400, content_type="text/plain")
    session_id = didit.event_session_id(event)
    check = IdentityCheck.objects.filter(session_id=session_id).first() if session_id else None
    if check is None:
        return HttpResponse("ignored", content_type="text/plain")
    try:
        IdentityCheck.objects.filter(pk=check.pk).update(webhook_at=timezone.now())
        result = identity.refresh(check.pk, source="webhook")
        said = str(event.get("status") or "").strip().lower()
        decided = {"approved", "declined", "in review", "expired", "abandoned", "kyc expired"}
        if said in decided and result is not None and result.status == IdentityCheck.Status.PENDING:
            return HttpResponse("could not confirm yet", status=503, content_type="text/plain")
    except Exception:           # noqa: BLE001
        logger.exception("A Didit webhook could not be handled")
        return HttpResponse("error", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")
