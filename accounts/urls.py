from django.urls import path
from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("signup/", views.signup_view, name="signup"),
    path("verify-email/", views.verify_email_pending_view, name="verify_email_pending"),
    path("verify-email/resend/", views.resend_verification_email_view, name="resend_verification_email"),
    path("logout/", views.logout_view, name="logout"),
]