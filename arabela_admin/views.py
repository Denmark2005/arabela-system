from django.shortcuts import render
from django.http import Http404


def dashboard_view(request):
    return render(request, "arabela_admin/dashboard.html")


def calendar_view(request):
    return render(request, "arabela_admin/calendar.html")


def page_view(request, page: str):
    allowed_pages = {
        "404",
        "alerts",
        "avatars",
        "badge",
        "bar-chart",
        "basic-tables",
        "blank",
        "buttons",
        "calendar",
        "form-elements",
        "images",
        "line-chart",
        "profile",
        "sidebar",
        "signin",
        "signup",
        "videos",
    }
    if page not in allowed_pages:
        raise Http404("Page not found")
    return render(request, f"arabela_admin/{page}.html")
