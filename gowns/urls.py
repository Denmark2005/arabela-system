from django.urls import path
from django.views.generic import RedirectView

from . import views

app_name = 'gowns'

urlpatterns = [
    path("", views.homepage, name="homepage"),

    # Info / content pages -- nested under /pages/ (Vestido-style, e.g. /pages/how-it-works/)
    path("pages/about/", views.about, name="about"),
    path("pages/how-it-works/", views.how_it_works, name="how_it_works"),
    path("pages/contact/", views.contact, name="contact"),
    path("pages/terms-and-conditions/", views.terms_and_conditions, name="terms_and_conditions"),
    path("pages/faqs/", views.faqs, name="faqs"),

    path("featured/men/", views.featured_men_collections, name="featured_men_collections"),
    path("featured/women/", views.featured_women_collections, name="featured_women_collections"),

    # Collections grid + the category pages, now nested under /collections/ (Vestido-style,
    # e.g. /collections/wedding/). These are FIXED exact paths, so they can never be shadowed
    # by the generic product-detail pattern below (which requires a /products/<slug>/ suffix).
    path("collections/", views.collections, name="collections"),
    path("collections/all/", views.collection_all, name="collection_all"),
    path("collections/wedding/", views.collection_wedding, name="collection_wedding"),
    path("collections/evening-gown/", views.collection_evening_gown, name="collection_evening_gown"),
    path("collections/long-gown/", views.collection_long_gown, name="collection_long_gown"),
    path("collections/luxury-gown/", views.collection_luxury_gown, name="collection_luxury_gown"),
    path("collections/mother-gown/", views.collection_mother_gown, name="collection_mother_gown"),
    path("collections/suit/", views.collection_suit, name="collection_suit"),
    path("collections/filipiniana/", views.collection_filipiniana, name="collection_filipiniana"),
    path("collections/guest-gown/", views.collection_guest_gown, name="collection_guest_gown"),
    path("collections/dresses/", views.collection_dresses, name="collection_dresses"),
    path("collections/kids-gown/", views.collection_kids_gown, name="collection_kids_gown"),
    path("collections/barong/", views.collection_barong, name="collection_barong"),
    path("collections/ball-gown-tulle/", views.collection_ball_gown_tulle, name="collection_ball_gown_tulle"),
    path("collections/bridesmaid-dresses/", views.collection_bridesmaid_dresses, name="collection_bridesmaid_dresses"),
    # Ball Gown was renamed Evening Gown: the old address (and its ?page=) still lands on the right page. Before the
    # owner's-category route below, which would otherwise answer 404 for it.
    path(
        "collections/ball-gown/",
        RedirectView.as_view(pattern_name="gowns:collection_evening_gown", permanent=False, query_string=True),
    ),
    # Categories the owner adds in the admin. LAST of the fixed category paths, so a built-in
    # one always wins; /collections/<collection>/products/<slug>/ below has more segments.
    path("collections/<slug:key>/", views.collection_custom, name="collection_custom"),

    # Product detail -- legacy (unnamed) route must stay BEFORE the generic one.
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
    path("reservation/submit/", views.reservation_submit, name="reservation_submit"),
    path("reservation/hold/start/", views.reservation_hold_start, name="reservation_hold_start"),
    path("reservation/hold/release/", views.reservation_hold_release, name="reservation_hold_release"),
    path("cart/fetch/", views.cart_fetch_view, name="cart_fetch"),
    path("cart/save/", views.cart_save_view, name="cart_save"),
    path("confirmation/", views.confirmation, name="confirmation"),
    path("reservations/", views.orders, name="orders"),
    path("reservations/<int:item_id>/", views.reservation_item_detail, name="reservation_item_detail"),
    path("reservations/<int:item_id>/cancel/", views.reservation_item_cancel, name="reservation_item_cancel"),
    path("reservations/<int:item_id>/upload-proof/", views.reservation_upload_proof, name="reservation_upload_proof"),
    path("messages/", views.messages_view, name="messages"),
    path("notifications/badges/", views.notification_badges, name="notification_badges"),
    path("profile/", views.profile, name="profile"),
]

# Backward-compatibility: the old bare-slug paths (e.g. /wedding/, /how-it-works/) now
# 302-redirect to their new /collections/… and /pages/… homes, so any typed or bookmarked
# old URL keeps working. Temporary (302), not permanent, since the site's URLs are still
# evolving and browsers cache 301s aggressively.
_LEGACY_REDIRECTS = [
    ("about/", "gowns:about"),
    ("how-it-works/", "gowns:how_it_works"),
    ("contact/", "gowns:contact"),
    ("terms-and-conditions/", "gowns:terms_and_conditions"),
    ("faqs/", "gowns:faqs"),
    ("all/", "gowns:collection_all"),
    ("wedding/", "gowns:collection_wedding"),
    ("ball-gown/", "gowns:collection_evening_gown"),
    ("suit/", "gowns:collection_suit"),
    ("filipiniana/", "gowns:collection_filipiniana"),
    ("guest-gown/", "gowns:collection_guest_gown"),
    ("dresses/", "gowns:collection_dresses"),
]
urlpatterns += [
    path(old, RedirectView.as_view(pattern_name=name, permanent=False))
    for old, name in _LEGACY_REDIRECTS
]
