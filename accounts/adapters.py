from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib.auth import get_user_model

from .models import UserProfile


class ArabelaSocialAccountAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin):
        # Prevent duplicate accounts: if a local user already has this email, connect it.
        if sociallogin.is_existing:
            return

        email = (sociallogin.user.email or '').strip().lower()
        if not email:
            return

        User = get_user_model()
        existing_user = User.objects.filter(email__iexact=email).first()
        if existing_user:
            sociallogin.connect(request, existing_user)

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form=form)
        picture_url = sociallogin.account.extra_data.get('picture', '')
        profile, _ = UserProfile.objects.get_or_create(user=user)
        if picture_url and profile.profile_picture_url != picture_url:
            profile.profile_picture_url = picture_url
            profile.save(update_fields=['profile_picture_url'])
        return user
