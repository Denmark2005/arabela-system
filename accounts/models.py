from django.conf import settings
from django.db import models


class UserProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    profile_picture_url = models.URLField(blank=True)
    age = models.PositiveSmallIntegerField(null=True, blank=True)
    terms_accepted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f'Profile<{self.user.email}>'
