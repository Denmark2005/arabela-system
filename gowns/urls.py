from django.urls import path
from . import views 

app_name = 'gowns'

urlpatterns = [
    path("", views.homepage, name="homepage"),
    path("about/", views.about, name="about"),
    path("how-it-works/", views.how_it_works, name="how_it_works"),
    path("contact/", views.contact, name="contact"),
    path("terms-and-conditions/", views.terms_and_conditions, name="terms_and_conditions"),
    path("faqs/", views.faqs, name="faqs"),
    path("featured/men/", views.featured_men_collections, name="featured_men_collections"),
    path("featured/women/", views.featured_women_collections, name="featured_women_collections"),
    path("collections/", views.collections, name="collections"),
    path("all/", views.collection_all, name="collection_all"),
    path("wedding/", views.collection_wedding, name="collection_wedding"),
    path("dresses/", views.collection_dresses, name="collection_dresses"),
    path("filipiniana/", views.collection_filipiniana, name="collection_filipiniana"),
    path("kid-suit/", views.collection_kid_suit, name="collection_kid_suit"),
    path("ball-gown/", views.collection_ball_gown, name="collection_ball_gown"),
    path("suit/", views.collection_suit, name="collection_suit"),
    path(
        "collections/wedding/products/<slug:slug>/",
        views.legacy_wedding_product_url,
    ),
    path(
        "collections/<str:collection>/products/<slug:slug>/",
        views.product_detail,
        name="product_detail",
    ),
    path("selection/", views.selection, name="selection"),
    path("reservation/", views.reservation, name="reservation"),
    path("confirmation/", views.confirmation, name="confirmation"),
    path("reservations/", views.orders, name="orders"),
    path("profile/", views.profile, name="profile"),
]