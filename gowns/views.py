from django.shortcuts import render
from django.contrib.auth.decorators import login_required

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

def product_detail(request, slug: str):
    wedding_products = {
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

    product = wedding_products.get(slug)
    if not product:
        product = {
            "title": slug.replace("-", " ").title(),
            "price": "₱0",
            "image": "https://lh3.googleusercontent.com/aida-public/AB6AXuDWwrpy8uS-dW3ZxUAzQRBag3p7bHigf95fvt9Qjq3GKrti53LrtFjCIU8hTk7NSu9Rcb56irXvF6VDm6k3QIv3PuwuatCEzUgKwt7OHD3rZc-Zlb7Ulhq3t6_MksIn2empBq_1O7rGoADAHQKDmz6jjTC-tJshsyApRfU_GsEP-b9g1RrBtelVWDnun2znYC7jER7ZFsCROeSDV_720shVeiCzDRohzWaPR-xAqaZJmFH_3ixXmZFbhQF0kUWvPu61-8C9RIKmXiA",
            "availability": "Available Now",
            "collection": "Wedding",
        }

    return render(request, 'products.html', {"product": product, "slug": slug})

def reservation(request):
    return render(request, 'reservation.html', {})


def selection(request):
    return render(request, 'selection.html', {})


def confirmation(request):
    return render(request, 'confirmation.html', {})


@login_required(login_url='accounts:login')
def orders(request):
    return render(request, 'reservations.html', {})