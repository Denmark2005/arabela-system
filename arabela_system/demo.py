"""The practice shop's start page, its two sign-in links and its sample gowns. These routes exist ONLY when ARABELA_DEMO=1 (`python demo.py`), which
always uses its own throwaway database file and refuses to start on Render (see settings.py). Everywhere else they do not exist (404)."""
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.contrib.sites.models import Site
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse

from accounts import terms
from accounts.models import UserProfile
from gowns.models import Gown

OWNER_USERNAME = "demo_owner"
CUSTOMER_USERNAME = "demo_customer"
BACKEND = "django.contrib.auth.backends.ModelBackend"

# (name, category, price, colour, colour code): a small sample catalogue, so there is something to rent in the practice shop.
SAMPLE_GOWNS = (
    ("Demo Aurora Ball Gown", Gown.Category.WEDDING_GOWN, "6500", "Ivory", "IV"),
    ("Demo Celeste Wedding Gown", Gown.Category.WEDDING_GOWN, "7500", "White", "WH"),
    ("Demo Marisol Wedding Gown", Gown.Category.WEDDING_GOWN, "5500", "Champagne", "CH"),
    ("Demo Isla Evening Gown", Gown.Category.EVENING_GOWN, "3500", "Wine", "WN"),
    ("Demo Lumi Evening Gown", Gown.Category.EVENING_GOWN, "3000", "Emerald", "EM"),
    ("Demo Bea Guest Gown", Gown.Category.GUEST_GOWN, "1800", "Dusty Rose", "DR"),
    ("Demo Tala Guest Gown", Gown.Category.GUEST_GOWN, "1500", "Navy", "NV"),
    ("Demo Luna Guest Gown", Gown.Category.GUEST_GOWN, "2000", "Sage", "SG"),
)


def _require_demo():
    if not getattr(settings, "DEMO_MODE", False):
        raise Http404("Not found")


def owner_user():
    User = get_user_model()
    user, created = User.objects.get_or_create(username=OWNER_USERNAME, defaults={
        "email": "demo.owner@example.com", "first_name": "Demo", "last_name": "Owner", "is_staff": True, "is_superuser": True})
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    UserProfile.objects.update_or_create(user=user, defaults={"role": UserProfile.Role.OWNER, "display_name": "Demo Owner"})
    return user


def customer_user():
    User = get_user_model()
    user, created = User.objects.get_or_create(username=CUSTOMER_USERNAME, defaults={
        "email": "demo.customer@example.com", "first_name": "Demo", "last_name": "Customer"})
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    UserProfile.objects.update_or_create(user=user, defaults={"display_name": "Demo Customer"})
    terms.record_acceptance(user)                    # the practice customer has already agreed to the Terms
    return user


def seed():
    """Put the sample data in the practice database (safe to run again: it only adds what is missing)."""
    Site.objects.update_or_create(id=getattr(settings, "SITE_ID", 2), defaults={"domain": "127.0.0.1:8000", "name": "Arabela practice shop"})
    owner_user()
    customer_user()
    for number, (name, category, price, colour, code) in enumerate(SAMPLE_GOWNS, start=1):
        gown_id = f"{category}-DEMO-{number:03d}"
        if Gown.objects.filter(gown_id=gown_id).exists():
            continue
        Gown.objects.create(gown_id=gown_id, name=name, category=category, color_name=colour, color_code=code, size=Gown.Size.MEDIUM,
                            rental_price=Decimal(price), status=Gown.Status.AVAILABLE)


def start_page(request):
    _require_demo()
    html = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Arabela practice shop</title>
<body style="font-family:system-ui,sans-serif;max-width:40rem;margin:2.5rem auto;padding:0 1rem;color:#14211b;line-height:1.5">
<p style="background:#14211b;color:#e3f0a8;padding:.6rem 1rem;border-radius:8px">PRACTICE SHOP &mdash; a throwaway copy on this computer. Payments here are simulated by a practice payment page (not PayMongo). Nothing touches the real shop.</p>
<h1>Arabela practice shop</h1>
<ol>
<li><a href="{reverse('demo_signin_customer')}"><b>Sign in as the demo customer</b></a>, open a gown, choose a date, add it to the bag and press Proceed to Reservation. Choose <i>Pay Security Deposit Only</i> or <i>Pay in Full</i>, fill in your details and press Pay.</li>
<li>On the practice payment page choose GCash, Maya or QR Ph and press <b>Authorize payment</b>. The booking is confirmed by itself, with a Payment Receipt.</li>
<li><a href="{reverse('demo_signin_owner')}"><b>Sign in as the demo owner</b></a> to see the booking, the receipt, the <i>Payments &rarr; Online payments</i> tab, and to hand a deposit back to the customer automatically.</li>
</ol>
<p>The customer and the owner are signed in with separate browser cookies, so you can keep both open in two tabs.</p>
</body>"""
    return HttpResponse(html)


def signin_customer(request):
    _require_demo()
    login(request, customer_user(), backend=BACKEND)
    return redirect("gowns:homepage")


def signin_owner(request):
    _require_demo()
    login(request, owner_user(), backend=BACKEND)
    request.session.set_expiry(0)
    return redirect("arabela_admin:dashboard")
