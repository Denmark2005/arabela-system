from django.contrib.auth import logout
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme


def _allowed_next_url(request, candidate) -> str:
    url = (candidate or '').strip()
    if not url:
        return ''
    if url_has_allowed_host_and_scheme(
        url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return url
    return ''


# Why a Google sign-in did not finish (accounts.adapters sends people here with ?problem=...). Only these words can ever be shown.
SIGN_IN_PROBLEMS = {
    "incomplete": "Sign-in with Google didn't finish. Please tap Continue with Google once and wait a moment for it to load.",
    "cancelled": "Sign-in was cancelled. Tap Continue with Google whenever you're ready.",
}


def login_view(request):
    """The only customer-facing sign-in/sign-up entry point: one "Continue with
    Google" card, shared by the accounts:login and accounts:signup URLs. The
    same click either creates a new account (SOCIALACCOUNT_AUTO_SIGNUP) or signs
    an existing one straight in, so one page covers what used to be a manual
    signup form, an email OTP step, and a separate password login form.

    `next` is passed straight through to Google's own login URL rather than
    juggled across a multi-step session flow like the old manual signup did --
    allauth natively honors a `next` GET param on its login views and returns
    the customer there once the Google round-trip completes."""
    if request.user.is_authenticated:
        return redirect('gowns:homepage')
    return render(request, 'login.html', {
        'next': _allowed_next_url(request, request.GET.get('next')),
        'error': SIGN_IN_PROBLEMS.get(request.GET.get('problem', ''), ''),
    })


def logout_view(request):
    logout(request)
    return redirect('gowns:homepage')
