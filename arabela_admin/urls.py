from django.urls import path

from .views import (
    rental_schedule_view,
    dashboard_view,
    payment_verification_view,
    security_deposits_view,
    admin_login_view,
    page_view,
    save_profile_view,
)

app_name = "arabela_admin"

urlpatterns = [
    path("admin-login/", admin_login_view, name="admin_login"),
    path("", dashboard_view, name="dashboard"),
    path("rental-schedule/", rental_schedule_view, name="rental_schedule"),
    path("calendar/", rental_schedule_view, name="calendar"),
    path("payment-verification/", payment_verification_view, name="payment_verification"),
    path("security-deposits/", security_deposits_view, name="security_deposits"),
    path("save-profile/", save_profile_view, name="save_profile"),
    path("<str:page>.html", page_view, name="page"),
]
