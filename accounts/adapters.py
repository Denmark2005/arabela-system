import logging

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.providers.base import AuthError
from django.contrib.auth import get_user_model
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import urlencode

from . import login_next

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


class ArabelaSocialAccountAdapter(DefaultSocialAccountAdapter):
    def on_authentication_error(self, request, provider, error=None, exception=None, extra_context=None):
        """Google's answer could not be used. Instead of allauth's bare "Third-Party Login Failure" page:
          * someone who is ALREADY signed in goes back to where they were headed (usually the checkout page), or the homepage when
            that is not known. The usual cause is the same Google answer arriving twice -- a second tap on the account, a slow
            connection, or Chrome replaying the whole sign-in when "Desktop site" is switched on or off. The first answer already
            signed them in and only the repeat failed, so there is nothing to tell them and they must not lose their place;
          * anyone else goes back to the login page, which says what happened and to try once more (keeping where they were headed).
        The login page is a redirect (not the callback URL itself), so a refresh can never replay the failed answer."""
        destination = login_next.recall(request)
        if request.user.is_authenticated:
            raise ImmediateHttpResponse(redirect(destination or reverse("gowns:homepage")))
        logger.warning("Google sign-in did not finish: error=%s exception=%r", error, exception)
        problem = "cancelled" if error in (AuthError.CANCELLED, AuthError.DENIED) else "incomplete"
        query = {"problem": problem, **({"next": destination} if destination else {})}
        raise ImmediateHttpResponse(redirect(f"{reverse('accounts:login')}?{urlencode(query)}"))

    def pre_social_login(self, request, sociallogin):
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
