"""The admin panel's side of owner password recovery (the rules and emails live in accounts/owner_recovery.py):

  * Account Settings -> Recovery email: send a code to a new address, then confirm the code (owner only, current password needed);
  * the public "Forgot password?" page and the page the emailed link opens.

Kept out of views.py on purpose: it is a self-contained feature, and views.py is already very large."""
import json

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from accounts import login_throttle, owner_recovery, rate_limit

from .views import OWNER_PASSWORD_MAX_LENGTH, OWNER_PASSWORD_MIN_LENGTH, _require_owner

LOGIN_NOTICE_PARAM = "reset"          # /admin-panel/admin-login/?reset=done shows "your password was changed"
LOGIN_NOTICE_VALUE = "done"


def _json_body(request):
    try:
        data = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ---- Account Settings: the owner's recovery email ------------------------------------------------------------------------
@require_http_methods(["POST"])
@_require_owner
def recovery_send_code_view(request):
    """Check the current password, then mail a 6-digit code to the NEW address. The recovery email itself is not changed here."""
    data = _json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    user = request.user
    if not owner_recovery.status(user)["available"]:
        return JsonResponse({"error": "Recovery email is not ready yet: the database update has not been applied. "
                                      "Ask the developer to finish the update."}, status=503)
    locked = owner_recovery.password_lock_seconds(user)
    if locked:
        return JsonResponse({"error": owner_recovery.lock_message(locked)}, status=429)
    if not user.check_password(str(data.get("current_password") or "")):
        owner_recovery.password_failed(user)
        return JsonResponse({"error": "Your current password is incorrect."}, status=400)
    owner_recovery.password_ok(user)
    address = owner_recovery.normalize_email(data.get("email"))
    if address is None:
        return JsonResponse({"error": "Enter a real email address, for example name@gmail.com."}, status=400)
    existing = owner_recovery.get_recovery(user)
    if existing is not None and existing.email == address:
        return JsonResponse({"error": "That is already your recovery email."}, status=400)
    outcome, message = owner_recovery.begin_verification(request, user, address)
    if outcome != "sent":
        return JsonResponse({"error": message}, status={"wait": 429, "limit": 429}.get(outcome, 502))
    return JsonResponse({"success": True, "email_masked": owner_recovery.mask(address)})


@require_http_methods(["POST"])
@_require_owner
def recovery_verify_code_view(request):
    """Check the code the owner typed; the recovery email is saved only when it is right."""
    data = _json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    ok, message = owner_recovery.confirm_code(request, request.user, str(data.get("code") or ""))
    if not ok:
        return JsonResponse({"error": message}, status=400)
    return JsonResponse({"success": True, "email_masked": owner_recovery.status(request.user)["email_masked"]})


# ---- the public pages -----------------------------------------------------------------------------------------------------
@never_cache
@require_http_methods(["GET", "POST"])
def forgot_password_view(request):
    """'Forgot password?': a username in, the SAME answer out -- whoever asks and whatever the username is."""
    if request.method == "POST":
        owner_recovery.request_reset(request.POST.get("username", ""), rate_limit.client_ip(request))
        return redirect(reverse("arabela_admin:forgot_password") + "?sent=1")     # redirect, so a refresh cannot ask twice
    return render(request, "arabela_admin/forgot-password.html", {"sent": request.GET.get("sent") == "1"})


def _password_problem(user, first, second):
    """The same rules as Account Settings -> Change Password, in plain words; '' when the new password is fine."""
    if not first:
        return "Please type a new password."
    if len(first) < OWNER_PASSWORD_MIN_LENGTH:
        return f"Use at least {OWNER_PASSWORD_MIN_LENGTH} characters for your new password."
    if len(first) > OWNER_PASSWORD_MAX_LENGTH:
        return f"Use {OWNER_PASSWORD_MAX_LENGTH} characters or fewer for your new password."
    if first != second:
        return "The two passwords do not match."
    if user.check_password(first):
        return "That is your current password. Please choose a different one."
    try:
        validate_password(first, user)
    except ValidationError as exc:
        return " ".join(exc.messages)
    return ""


def _page(request, context, status=200):
    response = render(request, "arabela_admin/reset-password.html", context, status=status)
    # The link carries the secret, so it must never be handed to another site. "same-origin" does that (nothing is sent to other
    # sites) WITHOUT breaking this page's own form: with "no-referrer" the browser also sends "Origin: null" on the form POST,
    # which Django's CSRF check rightly refuses -- the new password could never be saved.
    response["Referrer-Policy"] = "same-origin"
    return response


@never_cache
@require_http_methods(["GET", "POST"])
def reset_password_view(request, uidb64, token):
    """The page the emailed link opens. Works once, for 30 minutes, for an owner with a confirmed recovery email."""
    if not rate_limit.allowed(f"reset-page:{rate_limit.client_ip(request)}", *owner_recovery.RESET_PAGE_PER_IP):
        return _page(request, {"invalid": True})
    user = owner_recovery.user_for_reset(uidb64, token)
    if user is None:
        return _page(request, {"invalid": True})
    if request.method == "POST":
        problem = _password_problem(user, request.POST.get("password1", ""), request.POST.get("password2", ""))
        if problem:
            return _page(request, {"error": problem}, status=400)
        user.set_password(request.POST["password1"])
        user.save(update_fields=["password"])        # a new password also signs out every device that was signed in
        login_throttle.clear(login_throttle.username_key(user.username))   # and lifts any "too many tries" lock, so she can sign in now
        owner_recovery.notify_password_changed(user)
        return redirect(reverse("arabela_admin:admin_login") + f"?{LOGIN_NOTICE_PARAM}={LOGIN_NOTICE_VALUE}")
    return _page(request, {})
