from django.shortcuts import render, redirect
from django.core.validators import validate_email
from django.core.exceptions import ValidationError

def login_view(request):
    if request.method == 'POST':
        email = request.POST.get('email', '').strip()

        # Check if it's a valid email format
        try:
            validate_email(email)
        except ValidationError:
            return render(request, 'login.html', {
                'error': 'Please enter a valid Gmail address.',
                'email': email
            })

        # Check if email ends with gmail.com
        if not email.endswith('@gmail.com'):
            return render(request, 'login.html', {
                'error': 'Gmail account not found. Please check your spelling or create an account.',
                'email': email
            })

        # Valid Gmail — save to session and redirect to OTP page
        request.session['login_email'] = email
        return redirect('accounts:verify_otp')

    return render(request, 'login.html', {})

def verify_otp(request):
    return render(request, 'login.html', {})