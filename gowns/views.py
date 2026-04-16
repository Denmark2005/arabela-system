from django.shortcuts import render

def homepage(request):
    return render(request, 'home.html', {})

def collections(request):
    return render(request, 'collections.html', {})

def collection_wedding(request):
    return render(request, 'wedding.html', {})

def reservation(request):
    return render(request, 'reservation.html', {})


def confirmation(request):
    return render(request, 'confirmation.html', {})