from django.shortcuts import render, redirect
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from accounts.models import UserProfile

_SLUG_ORDER = (
    "valencia-lace",
    "archive-satin",
    "florence-organza",
    "modernist-crepe",
    "opulence-pearl",
    "heritage-lace",
    "city-reception",
    "lumiere-silk",
)
_NUMERIC = ["One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight"]

_VALID_COLLECTIONS = frozenset(
    {"wedding", "filipiniana", "kid-suit", "ball-gown", "dresses", "suit"}
)


def _title_and_label(collection_key: str, slug: str) -> tuple[str, str]:
    idx = _SLUG_ORDER.index(slug) if slug in _SLUG_ORDER else 0
    n = _NUMERIC[idx]
    if collection_key == "ball-gown":
        return (f"Ball Gown {n}", "Ball Gown")
    if collection_key == "kid-suit":
        return (f"Kid Suit {n}", "Kid Suit")
    if collection_key == "suit":
        return (f"Suit {n}", "Suit")
    if collection_key == "filipiniana":
        return (f"Filipiniana {n}", "Filipiniana")
    if collection_key == "dresses":
        return (f"Dresses {n}", "Dresses")
    if collection_key == "wedding":
        return (f"Wedding {n}", "Wedding")
    return (f"Wedding {n}", "Wedding")


def homepage(request):
    return render(request, 'home.html', {})


def collections(request):
    return render(request, 'collections.html', {})


def collection_wedding(request):
    return render(request, 'wedding.html', {})


def collection_dresses(request):
    return render(request, 'dresses.html', {})


def collection_filipiniana(request):
    return render(request, 'filipiniana.html', {})


def collection_kid_suit(request):
    return render(request, 'kid_suit.html', {})


def collection_ball_gown(request):
    return render(request, 'ball_gown.html', {})


def collection_suit(request):
    return render(request, 'suit.html', {})


def legacy_wedding_product_url(request, slug: str):
    """Old path /collections/wedding/products/<slug>/ — redirect to routed product URL."""
    target = reverse("gowns:product_detail", kwargs={"collection": "wedding", "slug": slug})
    if request.path == target:
        return product_detail(request, "wedding", slug)
    if request.GET:
        target += "?" + request.GET.urlencode()
    return redirect(target)


_WEDDING_PRODUCTS = {
    "valencia-lace": {
        "title": "Wedding One",
        "price": "₱1,600",
        "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        "availability": "Available Now",
        "collection": "Wedding",
    },
    "archive-satin": {
        "title": "Wedding Two",
        "price": "₱1,800",
        "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        "availability": "Available Now",
        "collection": "Wedding",
    },
    "florence-organza": {
        "title": "Wedding Three",
        "price": "₱2,000",
        "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        "availability": "Available Now",
        "collection": "Wedding",
    },
    "modernist-crepe": {
        "title": "Wedding Four",
        "price": "₱2,200",
        "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        "availability": "Available Now",
        "collection": "Wedding",
    },
    "opulence-pearl": {
        "title": "Wedding Five",
        "price": "₱2,400",
        "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        "availability": "Available Now",
        "collection": "Wedding",
    },
    "heritage-lace": {
        "title": "Wedding Six",
        "price": "₱2,600",
        "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        "availability": "Available Now",
        "collection": "Wedding",
    },
    "city-reception": {
        "title": "Wedding Seven",
        "price": "₱2,800",
        "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        "availability": "Available Now",
        "collection": "Wedding",
    },
    "lumiere-silk": {
        "title": "Wedding Eight",
        "price": "₱3,000",
        "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
        "availability": "Available Now",
        "collection": "Wedding",
    },
}


_FALLBACK_IMG = (
    "https://lh3.googleusercontent.com/aida-public/"
    "AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA"
)


def product_detail(request, collection: str, slug: str):
    col = collection.strip().lower()
    if col not in _VALID_COLLECTIONS:
        col = "wedding"

    base = dict(_WEDDING_PRODUCTS[slug]) if slug in _WEDDING_PRODUCTS else None

    title, lbl = _title_and_label(col, slug)
    if not base:
        product = {
            "title": slug.replace("-", " ").title(),
            "price": "₱0",
            "image": _FALLBACK_IMG,
            "availability": "Available Now",
            "collection": lbl,
        }
    else:
        base["title"] = title
        base["collection"] = lbl
        product = base

    return render(
        request,
        'products.html',
        {"product": product, "slug": slug, "collection": col},
    )


def reservation(request):
    profile_display_name = ""
    profile_first_name = ""
    profile_last_name = ""

    if request.user.is_authenticated:
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        profile_display_name = profile.display_name or ""
        name_parts = profile_display_name.split(maxsplit=1) if profile_display_name else []
        profile_first_name = name_parts[0] if len(name_parts) > 0 else ""
        profile_last_name = name_parts[1] if len(name_parts) > 1 else ""

    return render(
        request,
        'reservation.html',
        {
            "profile_display_name": profile_display_name,
            "profile_first_name": profile_first_name,
            "profile_last_name": profile_last_name,
        },
    )


def selection(request):
    return render(request, 'selection.html', {})


def confirmation(request):
    return render(request, 'confirmation.html', {})


@login_required(login_url='accounts:login')
def orders(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    return render(request, 'reservations.html', {"display_name": profile.display_name})


@login_required(login_url='accounts:login')
def profile(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)

    if request.method == "POST":
        first_name = (request.POST.get("first_name") or "").strip()
        last_name = (request.POST.get("last_name") or "").strip()
        full_name = " ".join(part for part in [first_name, last_name] if part).strip()
        profile.display_name = full_name
        profile.save(update_fields=["display_name"])
        return redirect("gowns:profile")

    name_parts = profile.display_name.split(maxsplit=1) if profile.display_name else []
    first_name = name_parts[0] if len(name_parts) > 0 else ""
    last_name = name_parts[1] if len(name_parts) > 1 else ""
    return render(
        request,
        'profile.htmls',
        {
            "display_name": profile.display_name,
            "profile_first_name": first_name,
            "profile_last_name": last_name,
        },
    )
