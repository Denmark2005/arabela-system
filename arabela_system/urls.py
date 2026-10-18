"""
URL configuration for arabela_system project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.http import Http404
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic.base import RedirectView

from arabela_system.health import healthz


def not_found(request, *args, **kwargs):
    """A page this shop does not offer, shown as the ordinary "page not found"."""
    raise Http404("Page not found")


urlpatterns = [
    path('favicon.ico', RedirectView.as_view(url='/static/arabela_admin/images/logo/favicon-arabela.svg', permanent=False)),
    path('healthz/', healthz, name='healthz'),
    # Django's built-in admin site is not used (the shop's panel is /admin-panel/). Its sign-in page was a SECOND door to the owner's
    # password with no "5 wrong tries = locked" rule, so it is closed. Nothing links to it.
    re_path(r'^admin/', not_found),
    path('', include('gowns.urls')),
    path('accounts/', include('accounts.urls')),
    # Customers sign in with Google only. allauth's own password and email pages would otherwise answer here -- including a
    # "reset password" that mails a link to ANY account holding an address (owner, staff and customers alike) -- so they are
    # closed. The owner's reset is the separate, owner-only one under /admin-panel/forgot-password/. Google sign-in is untouched.
    re_path(r'^accounts/(?:password/|email/|confirm-email/|reauthenticate/)', not_found),
    path('accounts/', include('allauth.urls')),
    path('reservations/', include('reservations.urls')),
    path('ai/', include('ai_recommendation.urls')),
    path('admin-panel/', include('arabela_admin.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
