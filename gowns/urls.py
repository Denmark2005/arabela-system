from django.urls import path
from . import views 

app_name = 'gowns'

urlpatterns = [
    path("", views.homepage, name="homepage"),
    path("collections/", views.collections, name="collections"),
    path("wedding/", views.collection_wedding, name="collection_wedding"),
    path("dresses/", views.collection_dresses, name="collection_dresses"),
    path("filipiniana/", views.collection_filipiniana, name="collection_filipiniana"),
    path("kid-suit/", views.collection_kid_suit, name="collection_kid_suit"),
    path("ball-gown/", views.collection_ball_gown, name="collection_ball_gown"),
    path("suit/", views.collection_suit, name="collection_suit"),
    path("collections/wedding/products/<slug:slug>/", views.product_detail, name="product_detail"),
    path("reservation/", views.reservation, name="reservation"),
    path("confirmation/", views.confirmation, name="confirmation"),
]