from django.shortcuts import render
from django.http import Http404


def dashboard_view(request):
    return render(request, "arabela_admin/dashboard.html")


def rental_schedule_view(request):
    return render(request, "arabela_admin/calendar.html", {"page": "rental"})


def payment_verification_view(request):
    return render(request, "arabela_admin/payment-verification.html", {"page": "payment-verification"})


def security_deposits_view(request):
    return render(request, "arabela_admin/security-deposits.html", {"page": "security-deposits"})


def page_view(request, page: str):
    allowed_pages = {
        "alerts",
        "badge",
        "basic-tables",
        "blank",
        "buttons",
        "calendar",
        "form-elements",
        "profile",
        "sidebar",
        "payment-verification",
        "security-deposits",
    }
    if page not in allowed_pages:
        raise Http404("Page not found")
    return render(request, f"arabela_admin/{page}.html")
