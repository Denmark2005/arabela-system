from django.urls import path
from . import views 

app_name = 'gowns'

urlpatterns = [
    path("", views.homepage, name="homepage"),
    path("collections/", views.collections, name="collections"),
    path("wedding/", views.collection_wedding, name="collection_wedding"),
    path("reservation/", views.reservation, name="reservation"),
    path("confirmation/", views.confirmation, name="confirmation"),
]