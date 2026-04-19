from django.urls import path

from .views import (
    rental_schedule_view,
    dashboard_view,
    payment_verification_view,
    security_deposits_view,
    page_view,
)

app_name = "arabela_admin"

urlpatterns = [
    path("", dashboard_view, name="dashboard"),
    path("rental-schedule/", rental_schedule_view, name="rental_schedule"),
    path("calendar/", rental_schedule_view, name="calendar"),
    path("payment-verification/", payment_verification_view, name="payment_verification"),
    path("security-deposits/", security_deposits_view, name="security_deposits"),
    path("<str:page>.html", page_view, name="page"),
]
