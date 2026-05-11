from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from smtplib import SMTPException
import random
import re
from allauth.account.models import EmailAddress
from .models import UserProfile


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


def _generate_verification_code() -> str:
    return f"{random.randint(0, 999999):06d}"


def _send_verification_code(request, email: str, code: str) -> None:
    subject = 'Arabela Email Verification Code'
    message = (
        f'Hello,\n\nYour Arabela verification code is {code}.\n\n'
        'This code will expire in 5 minutes.\n\n'
        'Enter this 6-digit code on the verification page to activate your account.\n\n'
        'If you did not request this, please ignore this email.\n'
    )
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', settings.EMAIL_HOST_USER) or 'no-reply@arabela.com'
    send_mail(subject, message, from_email, [email], fail_silently=False)


def login_view(request):
    context = {}
    notice = request.session.pop('auth_notice', '')
    if notice:
        context['error'] = notice
    next_from_get = (request.GET.get('next') or '').strip()
    if next_from_get:
        context['next'] = next_from_get

    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()
        password = request.POST.get('password', '')
        next_url = (request.POST.get('next') or request.GET.get('next') or '').strip()
        if next_url:
            context['next'] = next_url

        try:
            validate_email(email)
        except ValidationError:
            context.update({'error': 'Please enter a valid email address.', 'email': email})
            return render(request, 'login.html', context)

        if not password:
            context.update({'error': 'Please enter your password.', 'email': email})
            return render(request, 'login.html', context)

        user = User.objects.filter(email__iexact=email).first()
        if not user:
            context.update({'error': 'No account found for this email. Please sign up first.', 'email': email})
            return render(request, 'login.html', context)

        if not user.has_usable_password():
            context.update({'error': "This account uses Google Sign In. Please click 'Sign in with Google' instead.", 'email': email})
            return render(request, 'login.html', context)

        is_verified = EmailAddress.objects.filter(user=user, email__iexact=email, verified=True).exists()
        if not is_verified:
            context.update({'error': 'Please verify your email first', 'email': email})
            return render(request, 'login.html', context)

        authenticated_user = authenticate(request, username=user.username, password=password)
        if not authenticated_user:
            context.update({'error': 'Invalid email or password.', 'email': email})
            return render(request, 'login.html', context)

        login(request, authenticated_user)
        remember_me = request.POST.get('remember_me') == 'on'
        request.session.set_expiry(settings.REMEMBER_ME_AGE if remember_me else 0)
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
            return redirect(next_url)
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

        existing_user = User.objects.filter(email__iexact=email).first()
        if existing_user:
            is_verified = EmailAddress.objects.filter(user=existing_user, email__iexact=email, verified=True).exists()
            if not is_verified:
                profile, _ = UserProfile.objects.get_or_create(user=existing_user)
                if not profile.display_name:
                    display_name = " ".join(part for part in [first_name, last_name] if part).strip()
                    if display_name:
                        profile.display_name = display_name
                        profile.save(update_fields=["display_name"])

                request.session['pending_verification_email'] = email
                verification_code = _generate_verification_code()
                request.session['verification_code'] = verification_code
                try:
                    _send_verification_code(request, email, verification_code)
                except SMTPException:
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
                return redirect(f"{reverse('accounts:verify_email_pending')}?email={email}&resent=1")

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
        UserProfile.objects.create(user=user, display_name=f"{first_name} {last_name}".strip())
        verification_code = _generate_verification_code()
        request.session['pending_verification_email'] = email
        request.session['verification_code'] = verification_code

        try:
            _send_verification_code(request, email, verification_code)
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
    return render(request, 'verification.html', context)


def resend_verification_email_view(request):
    if request.method != 'POST':
        return redirect('accounts:signup')

    email = (request.POST.get('email') or request.session.get('pending_verification_email') or '').strip().lower()
    if not email:
        return redirect('accounts:signup')

    user = User.objects.filter(email__iexact=email).first()
    if not user:
        return redirect('accounts:signup')

    verification_code = _generate_verification_code()
    request.session['pending_verification_email'] = email
    request.session['verification_code'] = verification_code

    try:
        _send_verification_code(request, email, verification_code)
    except SMTPException:
        return render(request, 'verification.html', {'error': 'Unable to resend the verification code. Please try again later.', 'email': email})

    return redirect(f"{reverse('accounts:verify_email_pending')}?email={email}&resent=1")


def verify_email_code_view(request):
    if request.method != 'POST':
        return redirect('accounts:verify_email_pending')

    email = (request.session.get('pending_verification_email') or '').strip().lower()
    code = request.POST.get('code', '').strip()

    if not email:
        return redirect('accounts:signup')

    if not code:
        return render(request, 'verification.html', {'error': 'Please enter the 6-digit verification code.', 'email': email})

    if code != request.session.get('verification_code'):
        return render(request, 'verification.html', {'error': 'The code you entered is invalid. Please try again.', 'email': email})

    user = User.objects.filter(email__iexact=email).first()
    if not user:
        return redirect('accounts:signup')

    profile, _ = UserProfile.objects.get_or_create(user=user)
    if not profile.display_name:
        display_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip()
        if display_name:
            profile.display_name = display_name
            profile.save(update_fields=["display_name"])

    email_address, created = EmailAddress.objects.get_or_create(
        user=user,
        email=email,
        defaults={'primary': True, 'verified': True},
    )
    if not email_address.verified:
        email_address.verified = True
        email_address.primary = True
        email_address.save()

    request.session.pop('verification_code', None)
    request.session.pop('pending_verification_email', None)
    request.session['auth_notice'] = 'Your email has been verified. Please sign in.'
    return redirect('accounts:login')


def logout_view(request):
    logout(request)
    return redirect('gowns:homepage')