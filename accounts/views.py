from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.shortcuts import redirect, render
from django.urls import reverse
from smtplib import SMTPException
import re
from allauth.account.models import EmailAddress


User = get_user_model()
PASSWORD_PATTERN = re.compile(r'^(?=.*[A-Za-z])(?=.*\d)[A-Za-z\d]{8,16}$')


def _build_unique_username(email: str) -> str:
    base = email.split('@')[0][:120] or 'user'
    username = base
    index = 1
    while User.objects.filter(username=username).exists():
        username = f'{base[:110]}{index}'
        index += 1
    return username


def login_view(request):
    context = {}
    notice = request.session.pop('auth_notice', '')
    if notice:
        context['error'] = notice

    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()
        password = request.POST.get('password', '')

        try:
            validate_email(email)
        except ValidationError:
            return render(request, 'login.html', {'error': 'Please enter a valid email address.', 'email': email})

        if not password:
            return render(request, 'login.html', {'error': 'Please enter your password.', 'email': email})

        user = User.objects.filter(email__iexact=email).first()
        if not user:
            return render(
                request,
                'login.html',
                {'error': 'No account found for this email. Please sign up first.', 'email': email},
            )

        if not user.has_usable_password():
            return render(
                request,
                'login.html',
                {'error': 'This account uses Google sign in. Please continue with Google.', 'email': email},
            )

        is_verified = EmailAddress.objects.filter(user=user, email__iexact=email, verified=True).exists()
        if not is_verified:
            return render(request, 'login.html', {'error': 'Please verify your email first', 'email': email})

        authenticated_user = authenticate(request, username=user.username, password=password)
        if not authenticated_user:
            return render(request, 'login.html', {'error': 'Invalid email or password.', 'email': email})

        login(request, authenticated_user)
        remember_me = request.POST.get('remember_me') == 'on'
        request.session.set_expiry(settings.REMEMBER_ME_AGE if remember_me else 0)
        return redirect('gowns:homepage')

    return render(request, 'login.html', context)


def signup_view(request):
    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        email = request.POST.get('email', '').strip().lower()
        password = request.POST.get('password', '').strip()
        accepted_terms = request.POST.get('accept_terms') == 'on'

        if not first_name or not last_name or not password:
            return render(
                request,
                'signup.html',
                {
                    'error': 'Please complete all required fields.',
                    'first_name': first_name,
                    'last_name': last_name,
                    'email': email,
                },
            )

        try:
            validate_email(email)
        except ValidationError:
            return render(
                request,
                'signup.html',
                {
                    'error': 'Please enter a valid email address.',
                    'first_name': first_name,
                    'last_name': last_name,
                    'email': email,
                },
            )

        if not email.endswith('@gmail.com'):
            return render(
                request,
                'signup.html',
                {
                    'error': 'Please use a valid Gmail address (example: info@gmail.com).',
                    'first_name': first_name,
                    'last_name': last_name,
                    'email': email,
                },
            )

        if not PASSWORD_PATTERN.match(password):
            return render(
                request,
                'signup.html',
                {
                    'error': 'Password must be 8-16 characters and include both letters and numbers.',
                    'first_name': first_name,
                    'last_name': last_name,
                    'email': email,
                },
            )

        if not accepted_terms:
            return render(
                request,
                'signup.html',
                {
                    'error': 'You must accept the Terms and Conditions to continue.',
                    'first_name': first_name,
                    'last_name': last_name,
                    'email': email,
                },
            )

        if User.objects.filter(email__iexact=email).exists():
            return render(
                request,
                'signup.html',
                {
                    'error': 'This email is already registered. Please sign in instead.',
                    'first_name': first_name,
                    'last_name': last_name,
                    'email': email,
                },
            )

        user = User.objects.create_user(
            username=_build_unique_username(email),
            first_name=first_name,
            last_name=last_name,
            email=email,
            password=password,
            is_active=True,
        )

        email_address = EmailAddress.objects.create(user=user, email=email, primary=True, verified=False)
        try:
            email_address.send_confirmation(request, signup=True)
        except SMTPException:
            user.delete()
            return render(
                request,
                'signup.html',
                {
                    'error': 'We could not send the verification email. Please check your Gmail SMTP settings and try again.',
                    'first_name': first_name,
                    'last_name': last_name,
                    'email': email,
                },
            )
        request.session['pending_verification_email'] = email
        return redirect('accounts:verify_email_pending')

    return render(request, 'signup.html', {})


def verify_email_pending_view(request):
    email = (request.GET.get('email') or request.session.get('pending_verification_email') or '').strip().lower()
    if not email:
        return redirect('accounts:signup')

    context = {
        'email': email,
        'resent': request.GET.get('resent') == '1',
    }
    return render(request, 'verify_email_pending.html', context)


def resend_verification_email_view(request):
    if request.method != 'POST':
        return redirect('accounts:signup')

    email = (request.POST.get('email') or request.session.get('pending_verification_email') or '').strip().lower()
    if not email:
        return redirect('accounts:signup')

    user = User.objects.filter(email__iexact=email).first()
    if not user:
        return redirect('accounts:signup')

    email_address, _ = EmailAddress.objects.get_or_create(
        user=user,
        email=email,
        defaults={'primary': True, 'verified': False},
    )

    if not email_address.verified:
        email_address.send_confirmation(request, signup=False)

    request.session['pending_verification_email'] = email
    return redirect(f"{reverse('accounts:verify_email_pending')}?email={email}&resent=1")


def logout_view(request):
    logout(request)
    return redirect('accounts:login')