import logging

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.providers.base import AuthError
from django.contrib.auth import get_user_model
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import urlencode

from . import admin_google, login_next

logger = logging.getLogger(__name__)

from .models import UserProfile

STAFF_GOOGLE_MESSAGE = (
    "This Google account is used by an Arabela staff account, so it can't be used to book gowns. "
    "Please continue with a different Google account."
)


def _is_staff(user):
    return bool(user is not None and (user.is_staff or user.is_superuser))


def _refuse_staff(request):
    """Stop the sign-in and show the customer login page with the reason. A staff account must never be reachable by
    "Continue with Google": it would sign the shop owner in as a customer and, since the admin panel trusts any staff
    session, open the admin panel without its password."""
    raise ImmediateHttpResponse(render(request, 'login.html', {'next': '', 'error': STAFF_GOOGLE_MESSAGE}, status=403))


def _refuse_admin(request, message, status=403):
    """Stop an ADMIN Google sign-in and show the admin sign-in page with the reason."""
    raise ImmediateHttpResponse(render(request, "arabela_admin/admin-login.html", admin_google.sign_in_context(request, message), status=status))


def _admin_google_sign_in(request, sociallogin, next_url):
    """The Google answer belongs to the ADMIN sign-in page (it asked to come back to the admin hand-over page). Never signs anybody in
    here: an allowed Gmail is handed over to /admin-panel/google-finish/ with a short-lived signed link, so the session is created
    in the admin area (its own cookie), and everyone else is refused. Always raises ImmediateHttpResponse."""
    email, verified = admin_google.google_identity(sociallogin)
    if not email or not verified:
        _refuse_admin(request, admin_google.MESSAGES["not_verified"])
    change_to = ""
    change_token = admin_google.change_token_from_next(next_url)
    if change_token:
        # The owner is switching her own sign-in Gmail: it only counts when the Google account that answered IS the new Gmail.
        owner, new_email = admin_google.read_change_token(change_token)
        if owner is None:
            _refuse_admin(request, admin_google.MESSAGES["change_expired"], status=400)
        if email != new_email:
            _refuse_admin(request, f"You signed in with {email}, but you asked to switch to {new_email}. Nothing was changed.", status=400)
        problem = admin_google.gmail_problem(new_email, exclude_pk=owner.pk)
        if problem:
            _refuse_admin(request, problem, status=400)
        user, change_to = owner, new_email
    else:
        user, why = admin_google.resolve_admin_user(email)
        if user is None:
            _refuse_admin(request, admin_google.MESSAGES[why])
    token = admin_google.make_signin_token(user, change_to=change_to)
    response = redirect(f"{admin_google.finish_path()}?{urlencode({'t': token})}")
    state = admin_google.answer_state(request)
    if state:
        # Remember WHICH Google answer this was. If the very same answer arrives again (Chrome on a phone replays the whole sign-in when
        # "Desktop site" is switched on or off), on_authentication_error recognises it and keeps the person in the admin panel.
        response.set_cookie(admin_google.STATE_COOKIE, state, max_age=admin_google.STATE_COOKIE_MAX_AGE, httponly=True, samesite="Lax",
                            secure=request.is_secure())
    raise ImmediateHttpResponse(response)


class ArabelaSocialAccountAdapter(DefaultSocialAccountAdapter):
    def on_authentication_error(self, request, provider, error=None, exception=None, extra_context=None):
        """Google's answer could not be used. Instead of allauth's bare "Third-Party Login Failure" page:
          * someone who is ALREADY signed in goes back to where they were headed (usually the checkout page), or the homepage when
            that is not known. The usual cause is the same Google answer arriving twice -- a second tap on the account, a slow
            connection, or Chrome replaying the whole sign-in when "Desktop site" is switched on or off. The first answer already
            signed them in and only the repeat failed, so there is nothing to tell them and they must not lose their place;
          * anyone else goes back to the login page, which says what happened and to try once more (keeping where they were headed).
        The login page is a redirect (not the callback URL itself), so a refresh can never replay the failed answer."""
        problem = "cancelled" if error in (AuthError.CANCELLED, AuthError.DENIED) else "incomplete"
        state = admin_google.answer_state(request)
        if state and state == request.COOKIES.get(admin_google.STATE_COOKIE):
            # The same Google answer that already signed an admin in, arriving a second time (Chrome on a phone replays the whole sign-in
            # when "Desktop site" is switched on or off). Nothing is wrong: the first answer did its job. This answer lands outside the
            # admin area, so it cannot see the admin sign-in; it must not be taken for a customer's and sent to the customer pages. The
            # admin sign-in page sends a signed-in admin on to the dashboard, and shows the sign-in again if the session has ended.
            raise ImmediateHttpResponse(redirect(reverse("arabela_admin:admin_login")))
        if request.COOKIES.get(admin_google.TRY_COOKIE):
            # The admin sign-in page was open a moment ago in this browser: send the person back to IT with the reason.
            logger.warning("Admin Google sign-in did not finish: error=%s exception=%r", error, exception)
            response = redirect(f"{reverse('arabela_admin:admin_login')}?{urlencode({'problem': problem})}")
            response.delete_cookie(admin_google.TRY_COOKIE)
            raise ImmediateHttpResponse(response)
        destination = login_next.recall(request)
        if request.user.is_authenticated:
            raise ImmediateHttpResponse(redirect(destination or reverse("gowns:homepage")))
        logger.warning("Google sign-in did not finish: error=%s exception=%r", error, exception)
        query = {"problem": problem, **({"next": destination} if destination else {})}
        raise ImmediateHttpResponse(redirect(f"{reverse('accounts:login')}?{urlencode(query)}"))

    def pre_social_login(self, request, sociallogin):
        # The ADMIN sign-in page asked Google to come back to the admin hand-over page: the owner / staff Gmail check, not a customer login.
        next_url = (getattr(sociallogin, "state", None) or {}).get("next") or ""
        if admin_google.is_admin_next(next_url):
            _admin_google_sign_in(request, sociallogin, next_url)       # always raises
            return

        # A Google sign-in that was already linked to a staff account (an older sign-in that matched by email).
        if sociallogin.is_existing:
            if _is_staff(sociallogin.user):
                _refuse_staff(request)
            return

        email = (sociallogin.user.email or '').strip().lower()
        if not email:
            return

        # Prevent duplicate accounts: if a local CUSTOMER already has this email, connect it. A staff account with the
        # same email is never connected to (and is refused when it is the only match).
        matches = get_user_model().objects.filter(email__iexact=email)
        customer = matches.filter(is_staff=False, is_superuser=False).order_by('pk').first()
        if customer:
            sociallogin.connect(request, customer)
        elif matches.exists():
            _refuse_staff(request)

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form=form)
        picture_url = sociallogin.account.extra_data.get('picture', '')
        profile, _ = UserProfile.objects.get_or_create(user=user)
        if picture_url and profile.profile_picture_url != picture_url:
            profile.profile_picture_url = picture_url
            profile.save(update_fields=['profile_picture_url'])
        return user
